"""DATA fixes: cache holes after a restart / feed stall, missed bars emitted in order, per-key live polls (one slow
venue does not stall the feed) and transient exchange errors that no longer mark a symbol as "down"."""
from __future__ import annotations

import asyncio
import time

import numpy as np

from app import config as nexus_config
from app.data import hub as hub_mod
from app.data.providers_http import DataProviderError
from app.util import floor_time
from tests.test_data_hub_fakes import Clock, FakeProviders, WSStub, bar, make_hub

BTC = "BINANCE:BTCUSDT"
ETH = "BINANCE:ETHUSDT"
H = 3600
VENUES = ("binance", "okx", "coinbase", "kraken", "kucoin")


def gaps(times: list[int], step: int) -> list[tuple[int, int]]:
    return [(a, b) for a, b in zip(times, times[1:]) if b - a != step]


async def db_times(rt, symbol: str, tf: str) -> list[int]:
    rows = await rt.db.fetchall("SELECT time FROM candles WHERE symbol = ? AND tf = ? ORDER BY time", (symbol, tf))
    return [int(r["time"]) for r in rows]


def partial(c: dict) -> dict:
    """What a poll sees of a bar that is still forming (its final close / high / volume come later)."""
    return {**c, "close": c["open"], "high": max(c["open"], c["low"]), "volume": c["volume"] / 3.0}


class FormingProviders(FakeProviders):
    """The bar forming at clock() is partial; closed bars are final (like a real exchange)."""

    async def fetch_candles(self, provider, native, tf, limit, *, client, end_time=None):
        rows = await super().fetch_candles(provider, native, tf, limit, client=client, end_time=end_time)
        now_bar = floor_time(self.clock(), tf)
        return [partial(r) if r["time"] == now_bar else r for r in rows]


def new_hub(rt, providers) -> hub_mod.MarketDataHub:
    """A second hub on the same DB: what the app has after a restart."""
    hub = hub_mod.MarketDataHub(rt)
    rt.hub = hub
    rt.ws = WSStub()
    hub._http = lambda: None
    hub._providers = lambda: providers
    hub._tv_error, hub._tv_error_at = "sin cliente en las pruebas", float("inf")
    return hub


# ----------------------------------------------------------------------------- cache holes


def test_restart_first_poll_never_stores_across_a_gap(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FormingProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            await hub.get_candles(BTC, "1h", 200)
            before = await db_times(rt, BTC, "1h")
            assert not gaps(before, H)
            clock.advance(10 * H)  # the app was closed for 10 hours
            hub2 = new_hub(rt, fake)
            await hub2.start()
            try:
                hub2.subscribe(BTC, "1h", "nexus")  # the watcher subscribes before anything loads candles
                await hub2._poll_crypto([(BTC, "1h")])
                # the gap is downloaded in the background
                await asyncio.gather(*list(getattr(hub2, "_gap_jobs", {}).values()))
                times = await db_times(rt, BTC, "1h")
                assert not gaps(times, H), gaps(times, H)
                assert times[-1] == floor_time(clock(), "1h")
                cs = await hub2.get_candles(BTC, "1h", 1000)
                assert np.all(np.diff(cs.time) == H) and not cs.stale
                # the bar that was forming when the app closed was replaced by its final version
                last_before = before[-1]
                row = await rt.db.fetchone("SELECT close FROM candles WHERE symbol = ? AND tf = '1h' AND time = ?",
                                           (BTC, last_before))
                assert row["close"] == bar(last_before)["close"]
            finally:
                await hub2.stop()

    asyncio.run(scenario())


def test_holes_in_cached_window_are_refetched_once(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            now_bar = floor_time(clock(), "1h")
            # a series damaged by an older version: 8 bars missing in the middle (restart), last bar fresh
            rows = [bar(now_bar - i * H) for i in range(60) if not 20 <= i < 28]
            await rt.db.executemany(
                "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                "VALUES (?, '1h', ?, ?, ?, ?, ?, ?, 'binance')",
                [(BTC, r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows],
            )
            cs = await hub.get_candles(BTC, "1h", 50)
            assert len(cs) == 50 and np.all(np.diff(cs.time) == H)
            assert fake.calls[-1]["limit"] == 28 and fake.calls[-1]["end_time"] is None  # from the hole to now
            assert not gaps(await db_times(rt, BTC, "1h"), H)
            calls = len(fake.calls)
            await hub.get_candles(BTC, "1h", 50)  # contiguous and fresh: no request
            assert len(fake.calls) == calls

    asyncio.run(scenario())


def test_stalled_feed_emits_every_missed_bar_in_order(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FormingProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            events: list[tuple[int, bool, float]] = []

            async def listener(symbol, tf, candle, closed):
                events.append((candle["time"], closed, candle["close"]))

            hub.add_listener(listener)
            await hub.get_candles(BTC, "1h", 100)
            hub.subscribe(BTC, "1h", "paper")
            t0 = floor_time(clock(), "1h")
            await hub._poll_crypto([(BTC, "1h")])  # forming t0 (partial)
            clock.advance(3 * H)  # laptop asleep for 3 bars
            await hub._poll_crypto([(BTC, "1h")])
            closed = [(t, c) for t, is_closed, c in events if is_closed]
            assert closed == [(t0 + i * H, bar(t0 + i * H)["close"]) for i in range(3)]  # final bars, oldest first
            assert events[-1][:2] == (t0 + 3 * H, False)
            times = await db_times(rt, BTC, "1h")
            assert not gaps(times, H) and times[-1] == t0 + 2 * H  # closed bars only (the forming one is not)
            stored = await rt.db.fetchone("SELECT close FROM candles WHERE symbol = ? AND tf = '1h' AND time = ?",
                                          (BTC, t0))
            assert stored["close"] == bar(t0)["close"]  # never the partial snapshot seen at t0

    asyncio.run(scenario())


def test_normal_bar_close_stores_final_bar_once(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FormingProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            await hub.get_candles(BTC, "1h", 50)
            hub.subscribe(BTC, "1h", "t")
            t0 = floor_time(clock(), "1h")
            await hub._poll_crypto([(BTC, "1h")])
            clock.advance(H)
            await hub._poll_crypto([(BTC, "1h")])
            row = await rt.db.fetchone("SELECT close FROM candles WHERE symbol = ? AND tf = '1h' AND time = ?",
                                       (BTC, t0))
            assert row["close"] == bar(t0)["close"]
            assert [c["limit"] for c in fake.calls[-1:]] == [2]  # no catch-up download for a normal close

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- head-of-line blocking


class SlowKraken(FakeProviders):
    def __init__(self, clock: Clock, delay: float):
        super().__init__(clock)
        self.slow_delay = delay
        self.latest: list[tuple[str, float, dict]] = []

    async def fetch_latest(self, provider, native, tf, *, client, retries=2, timeout=None):
        self.latest.append((provider, time.monotonic(), {"retries": retries, "timeout": timeout}))
        if provider == "kraken":
            await asyncio.sleep(self.slow_delay)
        return await super().fetch_latest(provider, native, tf, client=client)


def test_slow_venue_does_not_stall_other_keys(runtime_factory, monkeypatch):
    clock = Clock()
    fake = SlowKraken(clock, delay=1.5)
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (_rt, hub):
            assert hub.route("KRAKEN:XBTUSD").venues[0].provider == "kraken"
            hub.subscribe(BTC, "1m", "ws:1")
            hub.subscribe("KRAKEN:XBTUSD", "1m", "ws:1")
            start = time.monotonic()
            tasks = await hub._feed_tick()
            assert time.monotonic() - start < 0.5  # the tick only starts the polls
            assert len(tasks) == 2
            await asyncio.sleep(0.2)
            assert sum(1 for p, *_ in fake.latest if p == "binance") == 1
            hub._next_poll = {k: 0.0 for k in hub._next_poll}  # both keys due again
            again = await hub._feed_tick()
            assert len(again) == 1  # Kraken's previous poll is still in flight: skipped
            await asyncio.sleep(0.2)
            assert sum(1 for p, *_ in fake.latest if p == "binance") == 2
            assert sum(1 for p, *_ in fake.latest if p == "kraken") == 1
            await asyncio.gather(*tasks, *again)
            # live polls: no retries and a short per-request timeout
            assert all(kw == {"retries": 0, "timeout": hub_mod.LIVE_HTTP_TIMEOUT_S} for _p, _t, kw in fake.latest)

    asyncio.run(scenario())


def test_hanging_venue_poll_times_out(runtime_factory, monkeypatch):
    clock = Clock()
    fake = SlowKraken(clock, delay=30.0)
    monkeypatch.setattr(hub_mod, "LIVE_POLL_TIMEOUT_S", 0.2)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (_rt, hub):
            hub.subscribe("KRAKEN:XBTUSD", "1m", "ws:1")
            start = time.monotonic()
            await hub._poll_crypto([("KRAKEN:XBTUSD", "1m")])
            assert time.monotonic() - start < 2.0
            assert hub._feed_failures.get("KRAKEN:XBTUSD") == 1

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- transient errors


def test_network_blip_does_not_mark_symbol_down(runtime_factory, monkeypatch):
    clock = Clock()
    blip = DataProviderError("BINANCE: error de red (ConnectTimeout)", provider="binance", kind="network")
    fake = FakeProviders(clock, fail={p: blip for p in VENUES})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (_rt, hub):
            cs = await hub.get_candles(ETH, "4h", 300)
            assert len(cs) == 0 and "Error temporal" in (cs.note or "") and "vuelve a intentarlo" in cs.note
            assert hub._crypto_down.get(ETH) is None and hub.history_kind(ETH) == ("binance", "full")
            calls = len(fake.calls)
            again = await hub.get_candles(ETH, "4h", 300)  # short per-(symbol, tf) backoff: no new requests
            assert len(again) == 0 and len(fake.calls) == calls
            fake.fail.clear()  # the network is back
            other = await hub.get_candles(ETH, "1h", 300)  # other timeframes are not affected at all
            assert len(other) == 300 and other.history == "full" and other.provider == "binance"
            hub._backoff.clear()  # backoff over
            cs = await hub.get_candles(ETH, "4h", 300)
            assert len(cs) == 300 and cs.history == "full"

    asyncio.run(scenario())


def test_mixed_answers_are_transient_only_definitive_ones_mark_down(runtime_factory, monkeypatch):
    clock = Clock()
    not_found = {p: DataProviderError(f"{p}: par no encontrado", provider=p, kind="not_found") for p in VENUES}
    fake = FakeProviders(clock, fail={**not_found, "okx": DataProviderError("OKX: HTTP 503", provider="okx",
                                                                             kind="server")})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (_rt, hub):
            cs = await hub.get_candles(ETH, "1h", 50)
            assert len(cs) == 0 and hub._crypto_down.get(ETH) is None  # OKX may have it: retry later
            fake.fail = dict(not_found)
            hub._backoff.clear()
            cs = await hub.get_candles(ETH, "1h", 50)
            assert cs.history == "recorder" and hub._crypto_down.get(ETH) is not None

    asyncio.run(scenario())
