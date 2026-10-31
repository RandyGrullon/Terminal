"""DATA (data-rt): robust stamping of TradingView snapshots (app.data.stamping, shared by the recorder and the
hub's snapshot feed). TradingView announces a delay (900 s here) but its data runs J seconds later (J > 0) or earlier
(J < 0); bars must still land under the right minute, and no bar may appear after the close. Offline, fakes only."""
from __future__ import annotations

import asyncio
import math
import random

import pytest

from app.data import stamping as ST
from app.data import tradingview_parse as P
from app.util import floor_time
from tests.test_data_hub_fakes import T0, Clock, FakeTV, make_hub

AAPL = "NASDAQ:AAPL"
H = 3600
DAY = floor_time(T0, "1d")  # a Thursday
DELAY = 900
SFX = {"1m": ("|1", 60), "5m": ("|5", 300), "15m": ("|15", 900), "1d": ("", 86400)}


# ----------------------------------------------------------------------------- truth model (one US session a day)


def day_open(d: int) -> int:
    return d + 13 * H + 1800


def day_close(d: int) -> int:
    return d + 20 * H


def price(m: int) -> float:
    return round(100.0 + 3.0 * math.sin(m / 700.0) + 0.5 * math.sin(m / 37.0) + 0.05 * math.sin(m * 1.7), 4)


def truth_bar(start: int, end: int) -> tuple:
    ps = [price(s) for s in range(start, end)]
    return ps[0], max(ps), min(ps), ps[-1], float(len(ps))


def tv_row(real: int, jitter: int) -> dict:
    """Scanner row TradingView answers at real time `real`: market time real - DELAY - jitter, frozen outside the
    session (before the open: the previous session's final bars)."""
    m = real - DELAY - jitter
    d = floor_time(m, "1d")
    if m < day_open(d):
        d -= 86400
    o = day_open(d)
    m = min(m, day_close(d) - 1)
    row = {"close": price(m), "update_mode": f"delayed_streaming_{DELAY}"}
    for tf, (sfx, tf_s) in SFX.items():
        start = o if tf_s >= 86400 else o + ((m - o) // tf_s) * tf_s
        b = truth_bar(start, m + 1)
        row.update({f"open{sfx}": b[0], f"high{sfx}": b[1], f"low{sfx}": b[2], f"close{sfx}": b[3],
                    f"volume{sfx}": b[4]})
    return row


class JitterTV(FakeTV):
    def __init__(self, clock: Clock, jitter: int):
        super().__init__()
        self.clock, self.jitter = clock, jitter

    async def current_bars(self, symbols, tfs):
        return {s: P.row_current_bars(tv_row(self.clock(), self.jitter), tfs, self.clock()) for s in symbols}


async def _session(rt, hub, clock, start_market: int, end_market: int, seed: int, feed: bool,
                   closes: list | None = None) -> list[dict]:
    """Recorder every ~20 s (+ the live snapshot feed every ~15 s when `feed`) from start to end market time."""
    rnd = random.Random(seed)
    tfs = ("1m", "5m", "15m", "1h")
    if feed:
        async def listener(symbol, tf, candle, closed):
            if closed and closes is not None:
                closes.append((tf, candle["time"]))
        hub.add_listener(listener)
        for tf in tfs:
            hub.subscribe(AAPL, tf, "ws:test")
    t = float(start_market + DELAY + rnd.randint(0, 59))
    next_rec, next_feed = t, t + rnd.uniform(0, 15)
    while t < end_market + DELAY:
        clock.t = int(t)
        if t >= next_rec:
            await hub.recorder.run_once([AAPL])
            next_rec = t + 20 + rnd.uniform(0.3, 1.5)
        if feed and t >= next_feed:
            await hub._poll_snapshots([(AAPL, tf) for tf in tfs])
            next_feed = t + 15 + rnd.uniform(0.0, 1.0)
        t = min(next_rec, next_feed) if feed else next_rec
    return await rt.db.fetchall("SELECT tf, time, open, close FROM candles WHERE symbol = ? ORDER BY tf, time",
                                (AAPL,))


def _check(rows: list[dict], lo: int, hi: int, d: int) -> None:
    for tf, (_sfx, tf_s) in SFX.items():
        if tf == "1d":
            continue
        got = [r for r in rows if r["tf"] == tf]
        wrong = [(r["time"] - d, r["open"]) for r in got if day_open(d) <= r["time"] < day_close(d)
                 and r["open"] != truth_bar(r["time"], r["time"] + tf_s)[0]]
        assert not wrong, (tf, wrong[:4])
        phantom = [r["time"] - d for r in got if r["time"] >= day_close(d) or day_close(d - 86400) <= r["time"] < day_open(d)]
        assert not phantom, (tf, phantom)
        times = {r["time"] for r in got if lo <= r["time"] < hi}
        first = -(-lo // tf_s) * tf_s  # first whole bucket of the checked window
        missing = [x - d for x in range(first, hi, tf_s) if x not in times]
        assert not missing, (tf, missing[:4])


# ----------------------------------------------------------------------------- pure helpers


def test_same_bar_needs_open_volume_and_range():
    base = (10.0, 11.0, 9.0, 10.5, 100.0)
    assert ST.same_bar(base, (10.0, 11.2, 8.9, 10.1, 120.0))
    assert not ST.same_bar(base, (10.1, 11.2, 8.9, 10.1, 120.0))  # another open
    assert not ST.same_bar(base, (10.0, 11.2, 8.9, 10.1, 80.0))  # volume reset
    assert not ST.same_bar(base, (10.0, 10.8, 9.5, 10.1, 120.0))  # narrower range: a fresh bar with the same open


def test_max_overlap_is_robust_to_outliers():
    obs = [(-41, -20), (-38, -17), (-35, -14), (-44, -23), (15, 36)]  # one outlier (a minute without trades)
    mid, depth = ST.max_overlap(obs)
    assert depth == 4 and -35 <= mid <= -23
    assert ST.consistent(obs) == mid
    assert ST.consistent([(-41, -20), (10, 30), (50, 70)]) is None  # scattered: no agreement
    assert ST.max_overlap([]) is None and ST.consistent([(1, 2)]) is None


# ----------------------------------------------------------------------------- jitter sessions


@pytest.mark.parametrize("jitter", [-30, -12, 25, 35])
def test_jitter_session_until_the_close_has_no_misstamped_or_phantom_bars(runtime_factory, monkeypatch, jitter):
    clock = Clock()
    tv = JitterTV(clock, jitter)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            start = DAY + 19 * H + 20 * 60  # 19:20 market time, random phase; recorder only
            rows = await _session(rt, hub, clock, start, day_close(DAY) + 600, seed=jitter + 100, feed=False)
            _check(rows, start + 15 * 60, day_close(DAY), DAY)
            st = hub.recorder.status()
            assert st["skipped_rollover"] == 0
            if abs(jitter) >= 25:  # rollovers of 5m / 15m bars pinned the real lag
                assert abs(st["lag_s"][AAPL] - jitter) <= 6

    asyncio.run(scenario())


@pytest.mark.parametrize("jitter", [-30, 30])
def test_session_open_with_feed_and_recorder(runtime_factory, monkeypatch, jitter):
    """First bars of a session (gap since yesterday's close) with the data running 30 s early / late, recorder and
    live feed together: early phantom bars before the open are moved to the right buckets once the lag is known."""
    clock = Clock()
    tv = JitterTV(clock, jitter)
    d = DAY + 86400

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            prev = DAY
            ins = ("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recorder')")
            for tf, (_sfx, tf_s) in SFX.items():
                t0 = prev if tf_s >= 86400 else day_close(prev) - tf_s
                await rt.db.execute(ins, (AAPL, tf, t0, *truth_bar(day_open(prev) if tf_s >= 86400 else t0,
                                                                  day_close(prev))))
            closes: list = []
            rows = await _session(rt, hub, clock, d + 13 * H + 20 * 60, d + 14 * H + 25 * 60, seed=7 + jitter,
                                  feed=True, closes=closes)
            _check(rows, day_open(d), d + 14 * H + 20 * 60, d)
            daily = [r for r in rows if r["tf"] == "1d"]
            assert [r["time"] for r in daily] == [prev, d]

    asyncio.run(scenario())


def test_close_with_late_data_emits_each_bar_closed_once_and_nothing_after(runtime_factory, monkeypatch):
    clock = Clock()
    tv = JitterTV(clock, 35)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            closes: list = []
            start = DAY + 19 * H + 32 * 60
            rows = await _session(rt, hub, clock, start, day_close(DAY) + 600, seed=3, feed=True, closes=closes)
            _check(rows, start + 15 * 60, day_close(DAY), DAY)
            m1 = [t for tf, t in closes if tf == "1m"]
            assert m1 and len(m1) == len(set(m1)) and max(m1) == day_close(DAY) - 60
            assert all(t < day_close(DAY) for _tf, t in closes)
            assert [t for tf, t in closes if tf == "1h"] == [DAY + 19 * H]

    asyncio.run(scenario())
