"""DATA-HUB: candles (SIM, crypto DB cache + incremental fetch, fallbacks, dedupe), push_candle, live feed."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.data.base import ProviderError
from app.data.providers_http import DataProviderError
from tests.test_data_hub_fakes import Clock, FakeProviders, bar, make_hub

BTC = "BINANCE:BTCUSDT"
H = 3600


def market_msgs(rt) -> list[tuple[str, dict]]:
    return [(t, m) for t, m in rt.ws.messages if t.startswith("market:")]


def test_sim_candles_deterministic_and_synthetic(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (rt, hub):
            a = await hub.get_candles("SIM:NEXUS", "1h", 300)
            b = await hub.get_candles("sim:nexus", "60", 300)
            assert len(a) == 300 and a.history == "synthetic" and a.provider == "synthetic" and a.note
            assert np.array_equal(a.close, b.close) and np.array_equal(a.time, b.time)
            assert np.all(np.diff(a.time) == H)
            big = await hub.get_candles("SIM:NEXUS", "1h", 2000)  # more than the 1500 generated -> regenerated
            assert len(big) == 2000
            other = await hub.get_candles("SIM:OTHER", "1h", 300)
            assert not np.array_equal(other.close, a.close)  # seed = crc32(symbol)
            row = await rt.db.fetchone("SELECT provider, history, market FROM symbols WHERE symbol = 'SIM:NEXUS'")
            assert row == {"provider": "synthetic", "history": "synthetic", "market": "sim"}
            first = a.close.copy()
        async with make_hub(runtime_factory, monkeypatch) as (_rt, hub2):  # a fresh hub: same series
            again = await hub2.get_candles("SIM:NEXUS", "1h", 300)
            assert np.array_equal(again.close, first)

    asyncio.run(scenario())


def test_invalid_input_raises_provider_error(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (_rt, hub):
            with pytest.raises(ProviderError):
                await hub.get_candles("SIM:NEXUS", "7x", 10)
            with pytest.raises(ProviderError):
                await hub.get_candles("not a symbol!", "1h", 10)
            with pytest.raises(ProviderError):
                await hub.get_candles("SIM:NEXUS", "1h", "muchas")

    asyncio.run(scenario())


def test_crypto_cache_roundtrip_incremental_and_backfill(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            cs = await hub.get_candles(BTC, "1h", 100)
            assert len(cs) == 100 and cs.history == "full" and cs.provider == "binance" and cs.note is None
            assert fake.calls[-1] == {"provider": "binance", "native": "BTCUSDT", "tf": "1h", "limit": 100,
                                      "end_time": None}
            assert int(cs.time[-1]) == (clock() // H) * H  # includes the forming bar
            stored = await rt.db.fetchval("SELECT COUNT(*) FROM candles WHERE symbol = ? AND tf = '1h'", (BTC,))
            assert stored == 100
            assert await rt.db.fetchval("SELECT source FROM candles WHERE symbol = ? LIMIT 1", (BTC,)) == "binance"

            n_calls = len(fake.calls)
            again = await hub.get_candles(BTC, "1h", 100)  # forming bar still fresh -> pure cache hit
            assert len(fake.calls) == n_calls and hub.cache_hits == 1
            assert np.array_equal(again.close, cs.close)

            clock.advance(2 * H)  # two new bars: incremental fetch from the last cached bar
            hub._fresh.clear()
            cs2 = await hub.get_candles(BTC, "1h", 100)
            assert fake.calls[-1]["limit"] == 3 and fake.calls[-1]["end_time"] is None
            assert int(cs2.time[-1]) == int(cs.time[-1]) + 2 * H and len(cs2) == 100

            cs3 = await hub.get_candles(BTC, "1h", 150)  # older bars: backfill ending at the oldest cached bar
            oldest = int(cs2.time[0]) - 2 * H
            assert fake.calls[-1]["end_time"] == oldest and fake.calls[-1]["limit"] == 150 - 102 + 1
            assert len(cs3) == 150 and np.all(np.diff(cs3.time) == H)
            assert cs3.close[-1] == pytest.approx(bar(int(cs3.time[-1]))["close"])

    asyncio.run(scenario())


def test_crypto_listing_start_stops_backfill(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock, listing_start=clock() - 49 * H)  # only 50 bars exist

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (_rt, hub):
            cs = await hub.get_candles(BTC, "1h", 200)
            assert len(cs) == 50
            calls = len(fake.calls)
            await hub.get_candles(BTC, "1h", 200)  # fresh + listing start known -> no request at all
            assert len(fake.calls) == calls

    asyncio.run(scenario())


def test_crypto_fallback_venue_then_recorder_history(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock, fail={"binance": ProviderError("Binance: límite de peticiones")})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            cs = await hub.get_candles(BTC, "1h", 50)
            assert len(cs) == 50 and cs.provider == "okx" and "par equivalente" in (cs.note or "")
            assert [c["provider"] for c in fake.calls] == ["binance", "okx"]
            assert fake.calls[-1]["native"] == "BTC-USDT"
            assert hub.history_kind(BTC) == ("okx", "full")
            # every venue answers "pair not found" and nothing is cached -> local (recorder) history + Spanish note
            fake.fail = {p: DataProviderError(f"{p}: par no encontrado", provider=p, kind="not_found")
                         for p in ("binance", "okx", "kucoin", "coinbase", "kraken")}
            eth = await hub.get_candles("BINANCE:ETHUSDT", "1h", 50)
            assert len(eth) == 0 and eth.history == "recorder" and eth.provider == "recorder"
            assert "Sin datos de los exchanges" in eth.note
            assert hub.history_kind("BINANCE:ETHUSDT")[1] == "recorder"
            # a cached series survives provider failures (served with a note)
            hub._fresh.clear()
            clock.advance(H)
            stale = await hub.get_candles(BTC, "1h", 50)
            assert len(stale) == 50 and "No se pudo actualizar" in stale.note

    asyncio.run(scenario())


def test_offline_serves_db_cache_only(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, offline=True, providers=fake, clock=clock) as (rt, hub):
            empty = await hub.get_candles(BTC, "1h", 10)
            assert len(empty) == 0 and "sin conexión" in empty.note.lower()
            rows = [bar(clock() // H * H - i * H) for i in range(5)]
            await rt.db.executemany(
                "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'binance')",
                [(BTC, "1h", r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows],
            )
            cs = await hub.get_candles(BTC, "1h", 10)
            assert len(cs) == 5 and cs.history == "full" and "caché" in cs.note
            assert fake.calls == []

    asyncio.run(scenario())


def test_concurrent_identical_requests_are_deduplicated(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock, delay=0.05)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (_rt, hub):
            results = await asyncio.gather(*(hub.get_candles(BTC, "1h", 80) for _ in range(6)))
            assert len(fake.calls) == 1
            assert all(len(r) == 80 for r in results)
            assert not hub._inflight

    asyncio.run(scenario())


def test_push_candle_updates_sim_listeners_and_ws(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (rt, hub):
            seen: list[tuple] = []

            async def good(symbol, tf, candle, closed):
                seen.append((symbol, tf, candle["time"], closed))

            async def broken(symbol, tf, candle, closed):
                raise RuntimeError("fallo del oyente")

            hub.add_listener(broken)
            hub.add_listener(good)
            hub.add_listener(good)  # registered once
            await hub.get_candles("SIM:NEXUS", "1h", 300)  # generated history
            t0 = 1_900_000_000 - 1_900_000_000 % H
            await hub.push_candle("SIM:NEXUS", "1h", {**bar(t0), "time": t0}, True)  # simulator takes over
            await hub.push_candle("SIM:NEXUS", "1h", {**bar(t0 + H), "time": t0 + H}, False)
            await hub.push_candle("SIM:NEXUS", "1h", {**bar(t0 + H), "time": t0 + H, "close": 123.0}, True)
            assert seen == [("SIM:NEXUS", "1h", t0, True), ("SIM:NEXUS", "1h", t0 + H, False),
                            ("SIM:NEXUS", "1h", t0 + H, True)]
            cs = await hub.get_candles("SIM:NEXUS", "1h", 500)
            assert list(cs.time) == [t0, t0 + H] and cs.close[-1] == 123.0
            assert await hub.last_price("SIM:NEXUS") == 123.0
            topics = [t for t, _ in market_msgs(rt)]
            assert topics == ["market:SIM:NEXUS:1h"] * 3
            msg = market_msgs(rt)[-1][1]
            assert msg["type"] == "candle" and msg["closed"] is True and msg["candle"]["close"] == 123.0
            await hub.push_candle("SIM:NEXUS", "1h", {**bar(t0 - 5 * H), "time": t0 - 5 * H}, True)  # rewind
            assert len(await hub.get_candles("SIM:NEXUS", "1h", 500)) == 1
            with pytest.raises(ProviderError):
                await hub.push_candle("SIM:NEXUS", "1h", {"time": t0}, True)
            hub.remove_listener(good)
            assert hub._listeners == [broken]

    asyncio.run(scenario())


def test_subscribe_is_ref_counted_by_owner(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (_rt, hub):
            hub.subscribe("binance:btcusdt", "60", owner="ws:1")
            hub.subscribe(BTC, "1h", owner="ws:1")  # same owner twice -> idempotent
            hub.subscribe(BTC, "1h", owner="paper")
            assert hub.subscriptions() == [{"symbol": BTC, "tf": "1h", "owners": ["paper", "ws:1"]}]
            hub.unsubscribe(BTC, "1h", owner="ws:1")
            assert hub.subscribed_symbols() == [BTC]
            hub.unsubscribe(BTC, "1h", owner="paper")
            assert hub.subscriptions() == [] and hub.status()["subscriptions"] == 0
            hub.unsubscribe(BTC, "1h", owner="nadie")  # unknown -> no error
            with pytest.raises(ProviderError):
                hub.subscribe(BTC, "13m", owner="x")
            assert hub._feed_task is None  # BACKGROUND disabled in tests

    asyncio.run(scenario())


def test_live_feed_detects_bar_close(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            events: list[tuple] = []

            async def listener(symbol, tf, candle, closed):
                events.append((candle["time"], candle["close"], closed))

            hub.add_listener(listener)
            hub.subscribe(BTC, "1h", owner="t")
            t1 = clock() // H * H
            await hub._poll_crypto([(BTC, "1h")])  # previous closed bar stored, forming bar emitted
            assert events == [(t1, bar(t1)["close"], False)]
            await hub._poll_crypto([(BTC, "1h")])  # unchanged -> nothing new
            assert len(events) == 1
            clock.advance(H)
            await hub._poll_crypto([(BTC, "1h")])  # new bar -> previous one closed, then the forming one
            assert events[1:] == [(t1, bar(t1)["close"], True), (t1 + H, bar(t1 + H)["close"], False)]
            times = [r["time"] for r in await rt.db.fetchall(
                "SELECT time FROM candles WHERE symbol = ? AND tf = '1h' ORDER BY time", (BTC,))]
            assert times == [t1 - H, t1]  # closed bars only
            assert [m["closed"] for _, m in market_msgs(rt)] == [False, True, False]
            assert await hub.last_price(BTC) == pytest.approx(bar(t1 + H)["close"])
            hub.unsubscribe(BTC, "1h", owner="t")
            await hub._apply_live(BTC, "1h", [bar(t1 + 2 * H)], "binance")  # not subscribed -> ignored
            assert len(events) == 3

    asyncio.run(scenario())


def test_feed_failures_switch_symbol_to_snapshot_feed(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock, fail={"binance": ProviderError("Binance: sin red")})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (_rt, hub):
            hub.subscribe(BTC, "1m", owner="t")
            for _ in range(4):
                await hub._poll_crypto([(BTC, "1m")])
            assert hub._feed_tv.get(BTC) is None
            await hub._poll_crypto([(BTC, "1m")])  # 5th consecutive failure
            # only the live updates move to TradingView: candles keep coming from the exchanges
            assert hub._feed_tv.get(BTC) is not None and hub.history_kind(BTC) == ("binance", "full")
            assert any("TradingView temporalmente" in e["message"] for e in hub.rt.bus.recent(20, source="hub"))
            await hub.rt.settings.set("providers.crypto_default", "okx")  # provider settings reset the fallback
            assert hub._feed_tv.get(BTC) is None and hub.history_kind(BTC) == ("binance", "full")

    asyncio.run(scenario())


def test_settings_listener_starts_and_stops_recorder(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (rt, hub):
            from app import config as nexus_config

            assert hub.recorder.running is False  # BACKGROUND disabled at start
            monkeypatch.setattr(nexus_config, "BACKGROUND", True)
            await rt.settings.set("recorder.enabled", False)
            await rt.settings.set("recorder.enabled", True)
            assert hub.recorder.running is True
            await rt.settings.set("recorder.enabled", False)
            await asyncio.sleep(0.05)
            assert hub.recorder.running is False
            monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    asyncio.run(scenario())
