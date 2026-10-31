"""DATA (data-rt, verifier): stamping regressions found with tick-sized prices. Real prices move in ticks, so a new
TradingView bar often opens exactly where one of the last bars did; that must never be taken for a late copy of the
earlier bar (the new bar was dropped and its minute went missing, ~10 % of 1m bars with realistic volatility). 15m
rollovers are also 5m rollovers seen in the same snapshots: they must not count twice when the lag is estimated.
Offline, fakes only."""
from __future__ import annotations

import asyncio
import math

import pytest

from app.data import stamping as ST
from tests import test_data_stamping as TS
from tests.test_data_hub_fakes import Clock, make_hub

M = 60
B0 = TS.DAY + 19 * 3600  # 19:00 UTC, inside the US session of the fake


def _snap(open_: float, high: float, low: float, close: float, volume: float) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def test_new_bar_opening_at_an_earlier_open_is_not_a_late_copy():
    async def seed(_symbol, _tf):
        return 0, None

    async def move(*_args):
        return 0

    async def scenario():
        st = ST.BarStamper(seed, move)
        opens = [100.10, 100.20, 100.15, 100.10]  # the 4th minute opens where the 1st did (tick prices)
        placed = []
        for i, o in enumerate(opens):
            b = B0 + i * M
            for k, at in enumerate((5, 25, 45)):
                c = _snap(o, o + 0.02 * (k + 1), o - 0.01 * (k + 1), o + 0.01, 10.0 * (k + 1))
                placed.append((i, k, await st.place(TS.AAPL, "1m", c, b + at)))
        for i, k, p in placed:
            assert p.bucket == B0 + i * M, (i, k, p)  # every snapshot of every minute in its own bucket
        assert [p.status for i, k, p in placed if k == 0] == ["first", "new", "new", "new"]

    asyncio.run(scenario())


def test_late_copy_right_after_a_rollover_is_still_recognised():
    async def seed(_symbol, _tf):
        return 0, None

    async def move(*_args):
        return 0

    async def scenario():
        st = ST.BarStamper(seed, move)
        assert (await st.place(TS.AAPL, "1m", _snap(100.0, 100.1, 99.9, 100.05, 10.0), B0 + 20)).status == "first"
        assert (await st.place(TS.AAPL, "1m", _snap(100.0, 100.2, 99.9, 100.1, 30.0), B0 + 50)).status == "same"
        new = await st.place(TS.AAPL, "1m", _snap(100.3, 100.3, 100.3, 100.3, 2.0), B0 + M + 2)
        assert (new.bucket, new.status) == (B0 + M, "new")
        # a lagging server answers a few seconds later with the previous bar, finishing: merged into its own bucket
        late = await st.place(TS.AAPL, "1m", _snap(100.0, 100.25, 99.9, 100.2, 34.0), B0 + M + 6)
        assert (late.bucket, late.status) == (B0, "old")
        # ... and an earlier state of it is dropped, never stored in the new bar's bucket
        assert (await st.place(TS.AAPL, "1m", _snap(100.0, 100.1, 99.9, 100.0, 12.0), B0 + M + 9)).bucket is None

    asyncio.run(scenario())


def test_coarse_lag_counts_a_15m_rollover_once():
    """One 15m boundary seen by the 5m and the 15m tracks is a single observation: two 5m rollovers far apart plus
    that boundary are three disagreeing observations, never an agreed lag of the boundary's value."""
    async def seed(_symbol, _tf):
        return 0, None

    async def move(*_args):
        return 0

    st = ST.BarStamper(seed, move)
    for tf in ("5m", "15m"):
        st._start_run((TS.AAPL, tf), B0)
    st._runs[(TS.AAPL, "5m")][0].obs.extend([(19.0, 40.0), (10.0, 30.0), (44.0, 65.0), (59.0, 80.0)])
    st._runs[(TS.AAPL, "15m")][0].obs.append((44.0, 65.0))  # same snapshots as the 5m (44, 65)
    assert not st.lag_known(TS.AAPL) and st.lag(TS.AAPL) == 0.0


def _tick_price(s: int) -> float:
    """A stock that moves in cents: opens repeat every few minutes."""
    return round(100.0 + 0.03 * math.sin(s / 23.0) + 0.02 * math.sin(s / 61.0), 2)


@pytest.mark.parametrize("jitter", [0, 25, -25])
def test_tick_prices_keep_every_minute(runtime_factory, monkeypatch, jitter):
    monkeypatch.setattr(TS, "price", _tick_price)
    clock = Clock()
    tv = TS.JitterTV(clock, jitter)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            start = TS.DAY + 19 * 3600 + 20 * 60
            rows = await TS._session(rt, hub, clock, start, TS.day_close(TS.DAY) + 300, seed=jitter + 11, feed=False)
            m1 = [r for r in rows if r["tf"] == "1m"]
            repeated = sum(1 for a, b in zip(m1, m1[2:]) if a["open"] == b["open"])
            assert repeated >= 3  # the scenario really has bars opening where an earlier one did
            TS._check(rows, start + 15 * 60, TS.day_close(TS.DAY), TS.DAY)

    asyncio.run(scenario())
