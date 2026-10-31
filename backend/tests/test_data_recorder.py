"""DATA-HUB: local candle recorder (TradingView current-bar snapshots -> local OHLCV history) and the live
snapshot feed for recorder symbols."""
from __future__ import annotations

import asyncio

from app.data.recorder import RECORDER_TFS, normalize_candle
from app.util import floor_time
from tests.test_data_hub_fakes import Clock, FakeTV, make_hub

AAPL = "NASDAQ:AAPL"


def _snapshot(now: int, close: float, high: float | None = None, low: float | None = None,
              volume: float = 100.0) -> dict[str, dict]:
    return {tf: {"time": floor_time(now, tf), "open": 10.0, "high": high if high is not None else max(close, 10.0),
                 "low": low if low is not None else min(close, 10.0), "close": close, "volume": volume}
            for tf in RECORDER_TFS}


def test_normalize_candle_repairs_and_rejects():
    assert normalize_candle({"time": 1_700_000_000_000, "close": 5}) == {
        "time": 1_700_000_000, "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0, "volume": 0.0}
    c = normalize_candle({"time": 60, "open": 2, "high": 1, "low": 3, "close": 2.5, "volume": -4})
    assert c["high"] == 2.5 and c["low"] == 2.0 and c["volume"] == 0.0
    assert normalize_candle({"time": None, "close": 1}) is None
    assert normalize_candle({"time": 60, "close": float("nan")}) is None
    assert normalize_candle("x") is None


def test_recorder_cycle_upserts_merges_and_skips_stale(runtime_factory, monkeypatch):
    clock = Clock()
    tv = FakeTV(bars={AAPL: _snapshot(clock(), 11.0)})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            rec = hub.recorder
            pending = await rec.symbols_to_record()
            # default watchlist: stocks / forex / gold are recorded; crypto (exchange API) and SIM are not
            assert AAPL in pending and "FX:EURUSD" in pending
            assert "BINANCE:BTCUSDT" not in pending and "SIM:NEXUS" not in pending

            res = await rec.run_once([AAPL])
            # 1h/4h are rebuilt from 15m: TradingView's own 1h/4h bars follow the exchange session (13:30 UTC)
            assert RECORDER_TFS == ("1m", "5m", "15m", "1d")
            assert res == {"symbols": 1, "bars": 4}
            assert tv.calls[-1] == ("current_bars", (AAPL,), RECORDER_TFS)
            rows = await rt.db.fetchall("SELECT tf, source, close FROM candles WHERE symbol = ? ORDER BY tf", (AAPL,))
            assert sorted(r["tf"] for r in rows) == sorted(RECORDER_TFS)
            assert {r["source"] for r in rows} == {"recorder"} and {r["close"] for r in rows} == {11.0}
            starts = [e for e in rt.bus.recent(50, source="recorder") if "empezando a grabar" in e["message"]]
            assert len(starts) == 1

            # same TradingView bar, new snapshot: open kept, high/low only widen, volume grows, close follows
            tv.bars[AAPL] = _snapshot(clock(), 10.5, high=12.0, low=9.0, volume=120.0)
            assert (await rec.run_once([AAPL]))["bars"] == 4
            row = await rt.db.fetchone("SELECT open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = '1d'",
                                       (AAPL,))
            assert row == {"open": 10.0, "high": 12.0, "low": 9.0, "close": 10.5, "volume": 120.0}
            assert (await rec.run_once([AAPL]))["bars"] == 0  # unchanged snapshot -> nothing written

            # market closed: the same values stamped one minute later are a stale repeat
            clock.advance(60)
            tv.bars[AAPL] = _snapshot(clock(), 10.5, high=12.0, low=9.0, volume=120.0)
            assert (await rec.run_once([AAPL]))["bars"] == 0 and rec.skipped_stale >= 1
            n_1m = await rt.db.fetchval("SELECT COUNT(*) FROM candles WHERE symbol = ? AND tf = '1m'", (AAPL,))
            assert n_1m == 1
            starts = [e for e in rt.bus.recent(50, source="recorder") if "empezando a grabar" in e["message"]]
            assert len(starts) == 1  # info event only the first time
            st = rec.status()
            assert st["cycles"] == 4 and st["bars"] == 8 and st["recording"] == [AAPL] and st["running"] is False

            # the recorded history is served by get_candles (history "recorder")
            cs = await hub.get_candles(AAPL, "15m", 50)
            assert len(cs) == 1 and cs.history == "recorder"

    asyncio.run(scenario())


def test_recorder_symbol_selection_and_unavailable_tradingview(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (rt, hub):  # no TradingView client
            hub.subscribe("BCBA:GGAL", "1h", owner="ws:1")
            pending = await hub.recorder.symbols_to_record()
            assert "BCBA:GGAL" in pending and "NASDAQ:AAPL" in pending
            await rt.settings.set("providers.twelvedata_key", "k")  # keyed provider now serves US stocks
            pending = await hub.recorder.symbols_to_record()
            assert "NASDAQ:AAPL" not in pending and "BCBA:GGAL" in pending
            res = await hub.recorder.run_once(["BCBA:GGAL"])
            assert res["bars"] == 0 and res["error"] == "TradingView no disponible"
            await rt.settings.set("recorder.enabled", False)
            assert hub.recorder.status()["enabled"] is False

    asyncio.run(scenario())


def test_snapshot_feed_aggregates_non_recorded_timeframes(runtime_factory, monkeypatch):
    clock = Clock(1_760_000_400 + 1500)  # 25 minutes into the hour
    bucket = floor_time(clock(), "30m")
    tv = FakeTV(bars={AAPL: {"15m": {"time": floor_time(clock(), "15m"), "open": 10.2, "high": 10.9, "low": 10.1,
                                     "close": 10.8, "volume": 7.0}}})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            await rt.db.execute(
                "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                "VALUES (?, '15m', ?, 10.0, 11.5, 9.5, 10.2, 5.0, 'recorder')",
                (AAPL, bucket),
            )
            got: list[tuple] = []

            async def listener(symbol, tf, candle, closed):
                got.append((symbol, tf, candle, closed))

            hub.add_listener(listener)
            hub.subscribe(AAPL, "30m", owner="ws:9")
            await hub._poll_snapshots([(AAPL, "30m")])
            assert tv.calls[-1] == ("current_bars", (AAPL,), ("15m",))
            assert len(got) == 1
            symbol, tf, candle, closed = got[0]
            assert (symbol, tf, closed) == (AAPL, "30m", False)
            assert candle == {"time": bucket, "open": 10.0, "high": 11.5, "low": 9.5, "close": 10.8, "volume": 12.0}

    asyncio.run(scenario())
