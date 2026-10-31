"""DATA fixes: TradingView snapshot bars stamped in market time (announced delay), never blended across two
TradingView bars, no phantom bars after the session close, hourly bars rebuilt from 15m (UTC buckets), and keyed
(Twelve Data) series that never receive TradingView snapshot bars."""
from __future__ import annotations

import asyncio

from app.data import tradingview_parse as P
from app.data.recorder import RECORDER_BASE, RECORDER_TFS
from app.util import floor_time
from tests.test_data_hub_fakes import T0, Clock, FakeTV, bar, make_hub

AAPL = "NASDAQ:AAPL"
DAY = floor_time(T0, "1d")  # a Thursday
H = 3600
M15 = 900


def tv_row(close: float, m1: tuple, m15: tuple, d1: tuple, mode: str = "delayed_streaming_900") -> dict:
    """Scanner row with current-bar columns: tuples are (open, high, low, close, volume)."""
    row: dict = {"close": close, "update_mode": mode}
    for sfx, vals in (("|1", m1), ("|15", m15), ("", d1)):
        for k, v in zip(("open", "high", "low", "close", "volume"), vals):
            row[f"{k}{sfx}"] = v
    return row


class RowTV(FakeTV):
    """TradingView double that answers current_bars() with the real row_current_bars() stamping."""

    def __init__(self, clock: Clock, row: dict):
        super().__init__()
        self.clock = clock
        self.row = row

    async def current_bars(self, symbols, tfs):
        self.calls.append(("current_bars", tuple(symbols), tuple(tfs)))
        return {s: P.row_current_bars(self.row, tfs, self.clock()) for s in symbols}


# ----------------------------------------------------------------------------- stamping & merge


def test_snapshot_bars_are_stamped_with_the_announced_delay():
    now = DAY + 20 * H + 1200  # 20:20 UTC; NASDAQ data is 15 min late
    row = tv_row(101.0, (1, 2, 0.5, 1.5, 10), (1, 2, 0.5, 1.5, 10), (1, 2, 0.5, 1.5, 10))
    bars = P.row_current_bars(row, ["1m", "15m", "1d"], now)
    assert bars["1m"]["time"] == DAY + 20 * H + 300 and bars["1m"]["asof"] == now - 900  # 20:05 market time
    assert bars["15m"]["time"] == DAY + 20 * H and bars["1d"]["time"] == DAY
    live = P.row_current_bars({**row, "update_mode": "streaming"}, ["1m"], now)
    assert live["1m"]["time"] == floor_time(now, "1m") and live["1m"]["asof"] == now


def test_merge_never_blends_two_tradingview_bars(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (rt, hub):
            rec = hub.recorder
            t = DAY + 19 * H
            base = {"time": t, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0}

            async def row():
                return await rt.db.fetchone(
                    "SELECT open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = '15m' AND time = ?",
                    (AAPL, t))

            await rec.store([(AAPL, "15m", dict(base))])
            # the next TradingView bar inside the same bucket (other open, small volume): ignored, not blended
            await rec.store([(AAPL, "15m", {"time": t, "open": 12.0, "high": 13.0, "low": 11.5, "close": 12.5,
                                            "volume": 20.0})])
            assert await row() == {k: base[k] for k in ("open", "high", "low", "close", "volume")}
            # the same TradingView bar later on: open kept, extremes widen, close follows, volume grows
            await rec.store([(AAPL, "15m", {"time": t, "open": 10.0, "high": 11.5, "low": 8.5, "close": 10.8,
                                            "volume": 150.0})])
            assert await row() == {"open": 10.0, "high": 11.5, "low": 8.5, "close": 10.8, "volume": 150.0}
            await rec.store([(AAPL, "15m", {"time": t, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.1,
                                            "volume": 90.0})])  # volume dropped: another bar
            assert (await row())["close"] == 10.8
            # collect() flags a replacement explicitly
            await rec.store([(AAPL, "15m", {"time": t, "open": 12.0, "high": 12.2, "low": 11.9, "close": 12.1,
                                            "volume": 5.0, "_replace": True})])
            assert await row() == {"open": 12.0, "high": 12.2, "low": 11.9, "close": 12.1, "volume": 5.0}

    asyncio.run(scenario())


def test_collect_handles_rollover_jitter_and_frozen_bars(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (rt, hub):
            rec = hub.recorder
            b = DAY + 15 * H  # a 15m bucket in market time

            def snap(t, asof, o, h, lo, c, v):
                return {AAPL: {"15m": {"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": v,
                                       "asof": asof}}}

            async def stored():
                rows = await rt.db.fetchall(
                    "SELECT time, open, close, volume FROM candles WHERE symbol = ? AND tf = '15m' ORDER BY time",
                    (AAPL,))
                return [(r["time"], r["open"], r["close"], r["volume"]) for r in rows]

            async def cycle(data):
                rows = await rec.collect(data, [AAPL])
                await rec.store(rows)
                return rows

            await cycle(snap(b, b + 300, 10.0, 10.5, 9.8, 10.2, 100.0))
            # late in the bucket TradingView already shows the next session bar: dropped (kept the stored one)
            assert await cycle(snap(b, b + 700, 11.0, 11.1, 10.9, 11.0, 5.0)) == [] and rec.skipped_rollover == 1
            assert await stored() == [(b, 10.0, 10.2, 100.0)]
            # right after our bucket rolled the previous TradingView bar is still finishing: it completes bar b
            rows = await cycle(snap(b + M15, b + M15 + 20, 10.0, 10.6, 9.8, 10.4, 130.0))
            assert [r[2]["time"] for r in rows] == [b]
            assert await stored() == [(b, 10.0, 10.4, 130.0)]
            # the new bar
            await cycle(snap(b + M15, b + M15 + 60, 10.4, 10.5, 10.3, 10.45, 7.0))
            assert await stored() == [(b, 10.0, 10.4, 130.0), (b + M15, 10.4, 10.45, 7.0)]
            # app restarted hours later while the market is closed: the frozen final bar completes b + 15m
            rec._last_snap.clear()
            later = b + 6 * H
            assert [r[2]["time"] for r in await cycle(snap(later, later + 30, 10.4, 10.7, 10.3, 10.6, 40.0))] \
                == [b + M15]
            assert await stored() == [(b, 10.0, 10.4, 130.0), (b + M15, 10.4, 10.6, 40.0)]

    asyncio.run(scenario())


def test_restart_next_morning_completes_yesterdays_daily_bar(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch) as (rt, hub):
            rec = hub.recorder
            # the app was stopped mid-session: the stored daily bar of DAY is partial
            await rec.store([(AAPL, "1d", {"time": DAY, "open": 95.0, "high": 100.0, "low": 94.0, "close": 99.0,
                                           "volume": 30_000.0})])
            rec._last_snap.clear()  # restarted the next morning, before the session opens

            def daily(t, asof, o, h, lo, c, v):
                return {AAPL: {"1d": {"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": v,
                                      "asof": asof}}}

            nxt = DAY + 86400
            rows = await rec.collect(daily(nxt, nxt + 10 * H, 95.0, 102.0, 94.0, 101.3, 52_400.0), [AAPL])
            await rec.store(rows)
            assert [r[2]["time"] for r in rows] == [DAY]  # TradingView still shows yesterday's (final) bar
            # the session opens: today's bar is a new bar, not a "rollover" to drop
            rows = await rec.collect(daily(nxt, nxt + 13 * H + 45 * 60, 101.5, 101.8, 101.2, 101.6, 900.0), [AAPL])
            await rec.store(rows)
            got = await rt.db.fetchall("SELECT time, open, close, volume FROM candles WHERE symbol = ? AND tf = '1d' "
                                       "ORDER BY time", (AAPL,))
            assert [(r["time"], r["open"], r["close"], r["volume"]) for r in got] == [
                (DAY, 95.0, 101.3, 52_400.0), (nxt, 101.5, 101.6, 900.0)]

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- session close


def test_no_phantom_bars_after_the_session_close(runtime_factory, monkeypatch):
    clock = Clock(DAY + 19 * H + 50 * 60)  # 19:50 UTC = 19:35 market time (15 min delay)
    tv = RowTV(clock, tv_row(100.5, (100.4, 100.6, 100.3, 100.5, 50), (100.0, 101.0, 99.5, 100.5, 1000),
                             (95.0, 102.0, 94.0, 100.5, 50_000)))

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            rec = hub.recorder
            closes: list[tuple[str, int, float]] = []

            async def listener(symbol, tf, candle, closed):
                if closed:
                    closes.append((tf, candle["time"], candle["close"]))

            hub.add_listener(listener)
            hub.subscribe(AAPL, "1h", "nexus")
            hub.subscribe(AAPL, "1m", "paper")

            async def tick(at: int, row: dict | None = None):
                clock.t = at
                if row is not None:
                    tv.row = row
                await rec.run_once([AAPL])
                await hub._poll_snapshots([(AAPL, "1h"), (AAPL, "1m")])

            await tick(DAY + 19 * H + 50 * 60)
            await tick(DAY + 20 * H + 5 * 60, tv_row(101.0, (100.9, 101.0, 100.8, 101.0, 30),
                                                     (100.6, 101.2, 100.2, 101.0, 800),
                                                     (95.0, 102.0, 94.0, 101.0, 52_000)))  # 19:50 market time
            await tick(DAY + 20 * H + 14 * 60 + 30, tv_row(101.2, (101.2, 101.3, 101.1, 101.2, 40),
                                                           (100.6, 101.3, 100.2, 101.2, 1100),
                                                           (95.0, 102.0, 94.0, 101.2, 52_300)))  # 19:59:30
            final = tv_row(101.3, (101.2, 101.4, 101.1, 101.3, 90), (100.6, 101.5, 100.2, 101.3, 1200),
                           (95.0, 102.0, 94.0, 101.3, 52_400))
            for hour in range(6):  # after the close TradingView keeps answering the frozen final bars
                await tick(DAY + 20 * H + 16 * 60 + hour * H, final)
            assert [c for c in closes if c[0] == "1h"] == [("1h", DAY + 19 * H, 101.3)]  # once, final close
            assert ("1m", DAY + 19 * H + 59 * 60, 101.3) in closes
            assert all(t < DAY + 20 * H for _tf, t, _c in closes)
            rows = await rt.db.fetchall("SELECT tf, time FROM candles WHERE symbol = ? AND time >= ?",
                                        (AAPL, DAY + 20 * H))
            assert rows == []  # nothing stamped after the 20:00 UTC close
            m15 = await rt.db.fetchall("SELECT time, close, volume FROM candles WHERE symbol = ? AND tf = '15m' "
                                       "ORDER BY time", (AAPL,))
            assert [(r["time"], r["close"], r["volume"]) for r in m15] == [
                (DAY + 19 * H + 30 * 60, 100.5, 1000.0), (DAY + 19 * H + 45 * 60, 101.3, 1200.0)]

    asyncio.run(scenario())


def test_hourly_recorder_bars_are_rebuilt_from_15m(runtime_factory, monkeypatch):
    clock = Clock(DAY + 17 * H + 600)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, clock=clock) as (rt, hub):
            assert RECORDER_BASE["1h"] == "15m" and RECORDER_BASE["4h"] == "15m" and "1h" not in RECORDER_TFS
            ins = ("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recorder')")
            q = [bar(DAY + 15 * H + i * M15) for i in range(8)]  # 15:00 .. 16:45
            await rt.db.executemany(ins, [(AAPL, "15m", c["time"], c["open"], c["high"], c["low"], c["close"],
                                           c["volume"]) for c in q])
            # what an older version stored: TradingView's session-aligned 1h bar under the 15:00 bucket
            await rt.db.execute(ins, (AAPL, "1h", DAY + 15 * H, 999.0, 1000.0, 998.0, 999.5, 1.0))
            cs = await hub.get_candles(AAPL, "1h", 10)
            assert list(cs.time) == [DAY + 15 * H, DAY + 16 * H]
            first = q[:4]
            assert cs.open[0] == first[0]["open"] and cs.close[0] == first[-1]["close"]
            assert cs.high[0] == max(c["high"] for c in first) and cs.volume[0] == sum(c["volume"] for c in first)

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- keyed series


class SessionKeyed:
    """Twelve-Data-like provider: US stock 1h bars stamped 13:30 ... 19:30 UTC (session alignment)."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.calls = 0

    async def fetch_keyed_candles(self, provider, symbol, asset_class, tf, limit, *, api_key, client):
        self.calls += 1
        out, t = [], floor_time(self.clock(), "1h") + 1800
        while len(out) < limit and t > self.clock() - 30 * 86400:
            if t <= self.clock() and 13 * H + 1800 <= t % 86400 <= 19 * H + 1800:
                out.append(bar(t))
            t -= H
        return sorted(out, key=lambda c: c["time"])


def test_keyed_series_never_get_snapshot_bars(runtime_factory, monkeypatch):
    clock = Clock(DAY + 17 * H + 600)  # 17:10 UTC, session open
    keyed = SessionKeyed(clock)

    def tv_bar() -> dict:
        now = clock()
        return {"time": floor_time(now, "1h"), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                "volume": 10.0, "asof": now}

    tv = FakeTV(bars={AAPL: {"1h": tv_bar()}})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, keyed=keyed, tv=tv, clock=clock) as (rt, hub):
            await rt.settings.set("providers.twelvedata_key", "demo-key")
            # leftover of an older version: a TradingView snapshot stored on the hour inside the keyed series
            await rt.db.execute("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                                "VALUES (?, '1h', ?, 1, 1, 1, 1, 1, 'recorder')", (AAPL, DAY + 15 * H))
            assert hub.history_kind(AAPL) == ("twelvedata", "keyed")
            cs = await hub.get_candles(AAPL, "1h", 20)
            assert all(int(t) % H == 1800 for t in cs.time)
            assert await rt.db.fetchval("SELECT COUNT(*) FROM candles WHERE symbol = ? AND source = 'recorder'",
                                        (AAPL,)) == 0
            seen: list[tuple[int, bool]] = []

            async def listener(symbol, tf, candle, closed):
                seen.append((candle["time"], closed))

            hub.add_listener(listener)
            hub.subscribe(AAPL, "1h", "ws:1")
            await hub._poll_snapshots([(AAPL, "1h")])
            assert seen == [(DAY + 16 * H + 1800, False)]  # the provider's forming bar (16:30), not 17:00
            calls = keyed.calls
            clock.advance(H)
            tv.bars[AAPL]["1h"] = {**tv_bar(), "open": 100.5, "close": 101.0, "high": 101.5}
            await hub._poll_snapshots([(AAPL, "1h")])
            assert seen[1:] == [(DAY + 16 * H + 1800, True), (DAY + 17 * H + 1800, False)]
            rows = await rt.db.fetchall("SELECT time, source FROM candles WHERE symbol = ? AND tf = '1h'", (AAPL,))
            assert {r["source"] for r in rows} == {"twelvedata"} and all(r["time"] % H == 1800 for r in rows)
            cs = await hub.get_candles(AAPL, "1h", 20)  # the close made the keyed series refresh
            assert keyed.calls == calls + 1 and int(cs.time[-1]) == DAY + 17 * H + 1800
            assert all(int(t) % H == 1800 for t in cs.time)

    asyncio.run(scenario())
