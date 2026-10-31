"""DATA (data-rt, second verifier): regressions of the shared stamper and of the real-time feed found with adversarial
scenarios. Offline, fakes only.

- The recorder and the live feed ask TradingView independently: overlapping requests come back out of order, so an
  EARLIER state of the followed bar (same open, lower volume) can arrive after a newer one. It was taken for a new bar
  and pushed the followed bar, and with it the whole run, one bucket on (87 % of 1m bars wrong with +-2 s of noise).
- A change of the lag (the PC clock corrected by NTP, a lagging server) moved whole runs of correctly stamped bars
  back and forth on every rollover. Runs whose rollovers agreed with the lag are now confirmed and never moved.
- A run move onto a stored bar outside the run deleted the moved bar (the merge into the other bar is a no-op).
- A chart of a keyed symbol whose real-time poll could never be scheduled (plan limits) skipped TradingView and froze.
"""
from __future__ import annotations

import asyncio
import random

import httpx
import pytest

from app.data import realtime as R
from app.data import stamping as ST
from app.data import tradingview_parse as P
from tests import test_data_stamping as TS
from tests.test_data_hub_fakes import Clock, FakeTV, make_hub
from tests.test_data_realtime import AAPL, EUR, OPEN_NOW, keyed_hub, td_quote

M = 60
B0 = TS.DAY + 19 * 3600  # 19:00 UTC, inside the US session of the fake


def _snap(open_: float, high: float, low: float, close: float, volume: float) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


async def _no_seed(_symbol, _tf):
    return 0, None


class NoisyTV(TS.JitterTV):
    """Every answer describes the market up to `noise` s earlier or later than its request time (independent
    requests of the recorder and of the live feed are served at slightly different times)."""

    def __init__(self, clock: Clock, jitter: int, noise: int, seed: int):
        super().__init__(clock, jitter)
        self.noise, self.rnd = noise, random.Random(seed)

    async def current_bars(self, symbols, tfs):
        j = self.jitter + self.rnd.randint(-self.noise, self.noise)
        return {s: P.row_current_bars(TS.tv_row(self.clock(), j), tfs, self.clock()) for s in symbols}


class ClockStepTV(TS.JitterTV):
    """The PC clock is corrected by `step` s at real time `at`: the stamps (asof) jump, TradingView's data does not."""

    def __init__(self, clock: Clock, jitter: int, at: int, step: int):
        super().__init__(clock, jitter)
        self.at, self.step = at, step

    async def current_bars(self, symbols, tfs):
        real = self.clock()
        pc = real + (self.step if real >= self.at else 0)
        return {s: P.row_current_bars(TS.tv_row(real, self.jitter), tfs, pc) for s in symbols}


# ----------------------------------------------------------------------------- stamper units


def test_an_earlier_state_arriving_after_a_newer_one_is_dropped_not_a_new_bar():
    moves: list = []

    async def move(*args):
        moves.append(args)
        return 0

    async def scenario():
        st = ST.BarStamper(_no_seed, move)
        assert (await st.place(TS.AAPL, "1m", _snap(100.0, 100.1, 99.9, 100.05, 10.0), B0 + 10)).status == "first"
        assert (await st.place(TS.AAPL, "1m", _snap(100.0, 100.3, 99.9, 100.2, 40.0), B0 + 30)).status == "same"
        # the other poller's request, answered from a moment earlier, comes back 3 s later
        late = await st.place(TS.AAPL, "1m", _snap(100.0, 100.2, 99.9, 100.1, 35.0), B0 + 33)
        assert (late.bucket, late.status, late.reason) == (None, "drop", "stale")
        assert st.tracks[(TS.AAPL, "1m")].sig[4] == 40.0  # the newer state is still the one followed
        # a data step back of ~20 s (lagging server) with most of the volume: still the same bar, not a new one
        back = await st.place(TS.AAPL, "1m", _snap(100.0, 100.25, 99.9, 100.1, 30.0), B0 + 52)
        assert back.bucket is None and back.reason == "stale"
        nxt = await st.place(TS.AAPL, "1m", _snap(100.0, 100.4, 99.8, 100.3, 55.0), B0 + 55)
        assert (nxt.bucket, nxt.status) == (B0, "same")
        # a real new bar that opens at the followed bar's open: volume reset, seen later than the stale window
        new = await st.place(TS.AAPL, "1m", _snap(100.0, 100.0, 100.0, 100.0, 2.0), B0 + M + 18)
        assert (new.bucket, new.status) == (B0 + M, "new")
        assert not moves

    asyncio.run(scenario())


def test_a_confirmed_run_is_never_moved():
    moves: list = []

    async def move(*args):
        moves.append(args)
        return 1

    async def scenario():
        st = ST.BarStamper(_no_seed, move)
        key = (TS.AAPL, "1m")
        st._start_run(key, B0)
        run = st._runs[key][0]
        run.end, run.confirmed = B0 + 10 * M, True
        st.tracks[key] = ST.Track(B0 + 10 * M, (1.0, 1.0, 1.0, 1.0, 1.0), B0 + 10 * M + 50, born_at=B0 + 10 * M + 2)
        await st._shift_run(key, run, -M)
        assert not moves and (run.since, run.end) == (B0, B0 + 10 * M)
        # an unconfirmed run moves, unless the database refuses (another stored bar in the way)
        run.confirmed = False

        async def refuse(*args):
            moves.append(args)
            return -1

        st._move = refuse
        await st._shift_run(key, run, -M)
        assert len(moves) == 1 and (run.since, run.end) == (B0, B0 + 10 * M)
        assert st.tracks[key].bucket == B0 + 10 * M and st.corrections == 0

    asyncio.run(scenario())


def test_relocate_refuses_to_move_onto_another_stored_bar(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=FakeTV(), clock=Clock(B0 + 3600)) as (rt, hub):
            ins = ("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                   "VALUES (?, '1m', ?, ?, ?, ?, ?, ?, 'recorder')")
            await rt.db.execute(ins, (AAPL, B0 - M, 99.0, 99.5, 98.5, 99.2, 50.0))  # an earlier, different bar
            await rt.db.execute(ins, (AAPL, B0, 100.0, 100.5, 99.5, 100.2, 60.0))
            await rt.db.execute(ins, (AAPL, B0 + M, 101.0, 101.5, 100.5, 101.2, 70.0))
            rec = hub.recorder
            assert await rec._relocate(AAPL, "1m", B0, B0 + M, -M) == -1  # B0 - M is taken: nothing moves
            rows = await rt.db.fetchall("SELECT time, open FROM candles WHERE symbol = ? AND tf = '1m' ORDER BY time",
                                        (AAPL,))
            assert [(r["time"], r["open"]) for r in rows] == [(B0 - M, 99.0), (B0, 100.0), (B0 + M, 101.0)]
            assert await rec._relocate(AAPL, "1m", B0, B0 + M, M) == 2  # onto its own range and a free bucket
            rows = await rt.db.fetchall("SELECT time, open FROM candles WHERE symbol = ? AND tf = '1m' ORDER BY time",
                                        (AAPL,))
            assert [(r["time"], r["open"]) for r in rows] == [(B0 - M, 99.0), (B0 + M, 100.0), (B0 + 2 * M, 101.0)]

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- sessions (recorder + live feed)


@pytest.mark.parametrize("jitter,noise", [(0, 3), (25, 3)])
def test_out_of_order_answers_of_the_two_pollers_keep_every_bar_in_place(runtime_factory, monkeypatch, jitter, noise):
    clock = Clock()
    tv = NoisyTV(clock, jitter, noise, seed=jitter + 5)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            start = TS.DAY + 19 * 3600 + 20 * 60
            rows = await TS._session(rt, hub, clock, start, TS.day_close(TS.DAY) + 300, seed=jitter + 21, feed=True)
            TS._check(rows, start + 15 * 60, TS.day_close(TS.DAY), TS.DAY)

    asyncio.run(scenario())


@pytest.mark.parametrize("step", [30, -30])
def test_a_clock_correction_mid_session_moves_no_stored_bar(runtime_factory, monkeypatch, step):
    clock = Clock()
    at = TS.DAY + 19 * 3600 + 35 * 60 + TS.DELAY  # real time of the correction (market 19:35)
    tv = ClockStepTV(clock, 0, at, step)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            start = TS.DAY + 19 * 3600 + 5 * 60
            rows = await TS._session(rt, hub, clock, start, TS.day_close(TS.DAY) + 300, seed=step + 50, feed=True)
            TS._check(rows, start + 15 * 60, TS.day_close(TS.DAY), TS.DAY)

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- real-time feed


def test_a_keyed_chart_without_credits_for_quotes_stays_live_on_tradingview(runtime_factory, monkeypatch):
    asof = OPEN_NOW - 900
    tv = FakeTV(bars={AAPL: {"1m": {"time": asof // 60 * 60, "open": 99.0, "high": 99.5, "low": 98.9, "close": 99.2,
                                    "volume": 5.0, "asof": asof}}})

    def no_request(request):
        raise AssertionError("no real-time request expected")

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=Clock(OPEN_NOW)) as (rt, hub):
            await keyed_hub(rt, hub, OPEN_NOW, no_request)
            await rt.settings.set("realtime.reserve_pct", 90)  # 8 credits a minute, 90 % kept for candles
            hub.subscribe(AAPL, "1m", "ws:1")
            assert hub.realtime.select() == {AAPL: "twelvedata"} and hub.realtime.due() == []
            for _ in range(2):
                hub._next_poll.clear()
                await asyncio.gather(*(await hub._feed_tick()))
            assert not hub.realtime.serves(AAPL)
            assert [c for c in tv.calls if c[0] == "current_bars"]
            msgs = [m for t, m in rt.ws.messages if t == "market:NASDAQ:AAPL:1m"]
            assert msgs and msgs[-1]["source"] == "tradingview" and msgs[-1]["delay_s"] == 900

    asyncio.run(scenario())


def test_first_poll_in_flight_serves_the_chart_and_polygon_counts_one_call_per_market(runtime_factory, monkeypatch):
    gate = asyncio.Event()

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=FakeTV(), clock=Clock(OPEN_NOW)) as (rt, hub):
            await keyed_hub(rt, hub, OPEN_NOW, lambda r: httpx.Response(200, json={}))
            hub.subscribe(AAPL, "1m", "ws:1")
            hub.realtime.select()
            [(provider, symbols)] = hub.realtime.due()

            async def slow():
                await gate.wait()

            job = asyncio.ensure_future(slow())
            hub.realtime.track(provider, job)
            assert hub.realtime.serves(AAPL)  # its first poll is in flight: TradingView is skipped meanwhile
            gate.set()
            await job
            assert not hub.realtime.serves(AAPL)  # it answered without this symbol: TradingView again
            await rt.settings.set("providers.twelvedata_key", "")
            await keyed_hub(rt, hub, OPEN_NOW, provider="polygon")
            await rt.settings.set("realtime.polygon_calls_min", 7)  # 5 usable after the reserve
            rq = hub.realtime
            assert rq.interval_for("polygon", [AAPL]) == 15.0
            assert rq.interval_for("polygon", [AAPL, "NASDAQ:NVDA"]) == 15.0  # one stocks snapshot call
            assert rq.interval_for("polygon", [AAPL, EUR]) == 24.0  # stocks + forex: two calls

    asyncio.run(scenario())


def test_quotes_wait_when_candle_downloads_used_the_minute(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=FakeTV(), clock=Clock(OPEN_NOW)) as (rt, hub):
            await keyed_hub(rt, hub, OPEN_NOW)
            hub.subscribe(AAPL, "1m", "ws:1")
            hub.subscribe("NASDAQ:NVDA", "1m", "ws:1")
            rq = hub.realtime
            rq.select()
            rq.budget("twelvedata").spend(7)  # candle downloads of the open charts this minute (plan: 8 a minute)
            assert rq.due() == [] and not rq.serves(AAPL)  # 7 + 2 > 8: no request now, TradingView meanwhile
            rq.clock.t += 61  # the minute rolled over
            [(provider, symbols)] = rq.due()
            assert provider == "twelvedata" and sorted(symbols) == [AAPL, "NASDAQ:NVDA"]

    asyncio.run(scenario())


def test_a_closed_market_quote_does_not_freeze_the_chart_after_the_open(runtime_factory, monkeypatch):
    pre = TS.DAY + 13 * 3600 + 20 * 60  # 09:20 New York (EDT): closed, the session opens at 13:30 UTC
    state = {"open": False}

    def handler(request):
        now = int(hub_ref["hub"].realtime.clock())
        return httpx.Response(200, json=td_quote("AAPL", 101.0, now - 20, open_=state["open"]))

    hub_ref: dict = {}

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=FakeTV(), clock=Clock(pre)) as (rt, hub):
            hub_ref["hub"] = hub
            await keyed_hub(rt, hub, pre, handler)
            hub.subscribe(AAPL, "1m", "ws:1")
            await asyncio.gather(*(await hub._feed_tick()))
            rq = hub.realtime
            assert rq.quote(AAPL)["market_open"] is False
            assert rq._next[AAPL] - rq.clock() == 10 * 60 + 5  # right after the open, not 30 min later
            rq.clock.t = TS.DAY + 13 * 3600 + 30 * 60 + 4  # 09:30:04: open by the session guess
            assert rq.quote(AAPL) is None and not rq.serves(AAPL)  # the closed quote no longer serves the chart
            state["open"] = True
            [(provider, symbols)] = rq.due()
            got = await rq.poll(provider, symbols)
            assert got[AAPL]["realtime"] is True and rq.serves(AAPL)

    asyncio.run(scenario())


def test_terminal_label_shows_the_age_of_an_old_keyed_price():
    now = 1_800_000_000_000
    q = {"source": "twelvedata", "realtime": True, "ts": now - 240_000}
    assert R.freshness_label(q, now) == "Twelve Data · hace 4 min"
    assert R.freshness_label({**q, "ts": now - 60_000}, now) == "Tiempo real · Twelve Data"
    assert R.freshness_label({"source": "binance", "realtime": True, "ts": now - 240_000}, now) == "Tiempo real · Binance"
