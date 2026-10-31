"""DATA fixes, verification round: the bar that was forming when the app closed is replaced after a restart two
bars later, a cached bar that may have been stored while forming never looks like a live price, and a symbol listed
on its own exchange goes back to it after a restart (an equivalent pair used during an outage is not kept)."""
from __future__ import annotations

import asyncio

from app.data import hub as hub_mod
from app.data.providers_http import DataProviderError
from app.util import floor_time
from tests.test_data_hub_fakes import Clock, FakeProviders, WSStub, bar, make_hub

BTC = "BINANCE:BTCUSDT"
SOL = "BINANCE:SOLUSDT"
H = 3600


def new_hub(rt, providers) -> hub_mod.MarketDataHub:
    """A second hub on the same DB: what the app has after a restart."""
    hub = hub_mod.MarketDataHub(rt)
    rt.hub = hub
    rt.ws = WSStub()
    hub._http = lambda: None
    hub._providers = lambda: providers
    hub._tv_error, hub._tv_error_at = "sin cliente en las pruebas", float("inf")
    return hub


def test_restart_two_bars_later_replaces_the_bar_that_was_forming(runtime_factory, monkeypatch):
    clock = Clock()  # 30 min into bar T0
    fake = FakeProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            t0 = floor_time(clock(), "1h")
            await hub.get_candles(BTC, "1h", 100)  # stores the forming bar t0 as it is now (partial)
            await rt.db.execute("UPDATE candles SET close = 1.0, low = 1.0 WHERE symbol = ? AND tf = '1h' AND time = ?",
                                (BTC, t0))
            clock.advance(2 * H)  # app closed during t0, restarted during t0 + 2h
            hub2 = new_hub(rt, fake)
            await hub2.start()
            try:
                hub2.subscribe(BTC, "1h", "nexus")  # the watcher subscribes before anything loads candles
                await hub2._poll_crypto([(BTC, "1h")])  # first live poll: t0 + 1h closed, t0 + 2h forming
                await asyncio.gather(*list(hub2._gap_jobs.values()))
                row = await rt.db.fetchone("SELECT close FROM candles WHERE symbol = ? AND tf = '1h' AND time = ?",
                                           (BTC, t0))
                assert row["close"] == bar(t0)["close"]  # the final bar, not the one seen while it was forming
                times = [int(r["time"]) for r in await rt.db.fetchall(
                    "SELECT time FROM candles WHERE symbol = ? AND tf = '1h' ORDER BY time", (BTC,))]
                assert all(b - a == H for a, b in zip(times, times[1:])) and times[-1] == t0 + 2 * H
            finally:
                await hub2.stop()

    asyncio.run(scenario())


class FailingTVQuotes:
    async def quotes(self, symbols):
        raise RuntimeError("TradingView: límite de peticiones alcanzado (HTTP 429)")

    def status(self) -> dict:
        return {}

    async def close(self) -> None:
        return None


def test_local_quote_never_takes_a_maybe_partial_bar_for_a_live_price(runtime_factory, monkeypatch):
    clock = Clock(1_760_000_400 + 20 * 60)  # 20 min into a 1h bar
    down = DataProviderError("sin red", provider="binance", kind="network")
    fake = FakeProviders(clock, fail={p: down for p in ("binance", "okx", "coinbase", "kraken", "kucoin")})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, tv=FailingTVQuotes(), clock=clock) as (rt, hub):
            t0 = floor_time(clock(), "1h")
            ins = ("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                   "VALUES (?, '1h', ?, ?, ?, ?, ?, ?, 'binance')")
            # a deep scan stored the bar forming now (its close is the price at 20 min) and the bar before it
            await rt.db.execute(ins, (SOL, t0 - H, 149.0, 150.0, 148.0, 149.5, 10.0))
            await rt.db.execute(ins, (SOL, t0, 150.0, 151.0, 149.0, 150.5, 4.0))
            clock.t = t0 + H + 30  # 30 s after that bar's nominal end: its close is 40 min old
            q = (await hub.quotes([SOL]))[SOL]
            assert q["price"] == 150.5 and q["stale"] is True and q["ts"] == t0 * 1000
            assert await hub.last_price(SOL, allow_stale=False) is None
            # a final bar (an older bar of that timeframe) is priced at its end
            await rt.db.execute("DELETE FROM candles WHERE symbol = ? AND time = ?", (SOL, t0))
            await rt.db.execute(ins, (SOL, t0 + H, 151.0, 151.5, 150.5, 151.2, 1.0))
            clock.t = t0 + 2 * H - 60
            q = (await hub.quotes([SOL]))[SOL]
            assert q["price"] == 151.2 and q["ts"] == (t0 + H) * 1000 and q["stale"] is True

    asyncio.run(scenario())


def test_native_symbol_goes_back_to_its_exchange_after_a_restart(runtime_factory, monkeypatch):
    clock = Clock()
    blip = DataProviderError("BINANCE: error de red (ConnectTimeout)", provider="binance", kind="network")
    fake = FakeProviders(clock, fail={"binance": blip})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            cs = await hub.get_candles(BTC, "1h", 50)  # Binance blip: an equivalent pair serves it meanwhile
            assert cs.provider == "okx" and "par equivalente" in (cs.note or "")
            fake.fail.clear()
            clock.advance(2 * H)
            hub2 = new_hub(rt, fake)
            await hub2.start()
            try:
                cs = await hub2.get_candles(BTC, "4h", 50)
                assert cs.provider == "binance" and cs.note is None
                assert fake.calls[-1]["provider"] == "binance"
                assert hub2.history_kind(BTC) == ("binance", "full")
            finally:
                await hub2.stop()

    asyncio.run(scenario())
