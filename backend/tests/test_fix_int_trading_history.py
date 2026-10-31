"""Integration (trading): NEXUS history is the replay of one causal path, and a bar-close decision analyses the
window of a closed-only analysis of that bar.

- nexus-repaint (verifies.engine): every run starts right after a stored anchor snapshot (anchor_window), so
  consecutive runs replay the same path and past markers, trades and bar values do not change as bars arrive; the
  window only moves when the anchor steps to the next snapshot (about every SNAPSHOT_EVERY bars).
- closed-bar window (verifies.paper, contract I1): with `upto` the forming bar is dropped without shifting the
  window start, so the bar-close decision equals a closed-only analysis of that bar.
"""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from app.engine import service as service_mod
from app.engine.features import WARMUP_BARS
from app.engine.nexus_loop import SNAPSHOT_EVERY
from app.engine.service import ANCHOR_SPAN, SNAPSHOT_MIN_GAP_BARS, NexusService, anchor_window
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_fix_engine_watch import with_forming

SYM = "SIM:ANCHOR"
H = 3600
LIMIT = 300
BAR_KEYS = ("score", "prob_up", "action", "pos_entry", "pos_sl")


class RecordingHub:
    """Hub fake serving a fixed candle set; remembers how many bars each request asked for."""

    def __init__(self, cs) -> None:
        self.cs = cs
        self.limits: list[int] = []

    async def get_candles(self, symbol: str, tf: str = "1h", limit: int = 500):
        self.limits.append(int(limit))
        return self.cs.tail(limit)


def make_series(n: int = 900):
    return generate_candleset(n=n, seed=3, tf="1h", end_time=FIXED_END, symbol=SYM)


@pytest.fixture
def no_ttl(monkeypatch):
    monkeypatch.setattr(service_mod, "CACHE_TTL_S", 0.0)  # every call sees the hub's newest bars


async def stored_snapshots(rt, symbol: str = SYM) -> list[dict]:
    row = await rt.db.fetchone("SELECT state FROM model_state WHERE kind='nexus' AND key=?", (f"{symbol}|1h",))
    return json.loads(row["state"])["snapshots"] if row else []


def anchors(snaps: list[dict]) -> dict[int, dict]:
    return {int(s["time"]): s for s in snaps if s.get("kind") in ("grid", "origin")}


def common_view(resp: dict, t_end: int) -> tuple:
    """What a run shows for the bars before `t_end`: bar times and values, markers and the trades closed by then."""
    bars = resp["bars"]
    n = bars["time"].index(t_end)
    return (bars["time"][:n], {k: bars[k][:n] for k in BAR_KEYS},
            [m for m in resp["markers"] if m["time"] < t_end],
            [t for t in resp["trades"] if t["exit_time"] is not None and t["exit_time"] < t_end])


def test_consecutive_runs_replay_the_same_history(runtime_factory, no_ttl):
    """120 consecutive bar closes: the past never repaints while the window keeps its anchor. The last bar of a run
    is left out of the comparison: it is "now" there (an entry decided at its close fills at that close), while
    the next run fills it at the following open."""
    cs = make_series()

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            prev, starts, same, seen = None, [], 0, {"markers": 0, "trades": 0}
            for k in range(560, 680):
                rt.hub = RecordingHub(cs.upto(k))
                resp = await svc.analyze(SYM, "1h", LIMIT)
                bars = resp["bars"]
                assert resp["ok"] and bars["time"][-1] == int(cs.time[k])
                assert LIMIT <= len(bars["time"]) <= LIMIT + SNAPSHOT_EVERY + SNAPSHOT_MIN_GAP_BARS
                # the window starts WARMUP_BARS - 1 bars before a stored anchor snapshot (the state AFTER its bar):
                # the first bar the engine processes is the one right after it, each later bar exactly once
                anchor = anchors(await stored_snapshots(rt)).get(bars["time"][WARMUP_BARS - 1])
                assert anchor is not None
                assert bars["score"][WARMUP_BARS - 1] is None and bars["score"][WARMUP_BARS] is not None
                before = int(((anchor["state"] or {}).get("learner") or {}).get("memory_bars") or 0)
                assert resp["learning"]["memory_bars"] == before + len(bars["time"]) - WARMUP_BARS
                starts.append(bars["time"][0])
                if prev is not None and prev["bars"]["time"][0] == bars["time"][0]:
                    same += 1
                    t_end = prev["bars"]["time"][-1]
                    old, new = common_view(prev, t_end), common_view(resp, t_end)
                    assert old == new, f"history before {t_end} changed at bar {k}"
                    seen["markers"] += len(old[2])
                    seen["trades"] += len(old[3])
                prev = resp
            # the window start only moves when the anchor steps to the next snapshot (a sliding window moved it on
            # every one of these 119 bar closes and repainted the past each time)
            assert len(set(starts)) <= 3 and same >= 116
            assert seen["markers"] > 0 and seen["trades"] > 0  # the comparison covered real markers and trades

    asyncio.run(scenario())


def test_bar_close_decision_analyses_the_closed_only_window(runtime_factory, monkeypatch):
    """With the forming bar in the hub (and after a burst of 3 more closed bars), the closed-bar run of bar k has
    the same window start and length, decisions and history as a closed-only analysis of bar k."""
    cs = make_series(1400)
    k = LIMIT + ANCHOR_SPAN + 40  # the hub holds more bars than one fetch asks for
    t_k, t_next = int(cs.time[k]), int(cs.time[k + 1])
    monkeypatch.setattr(service_mod, "now_s", lambda: t_next + 120)  # bar k+1 opened 2 min ago

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)

            async def analyze(symbol: str, hub_cs, **kw) -> tuple[dict, list[int]]:
                rt.hub = RecordingHub(hub_cs)
                return await svc.analyze(symbol, "1h", LIMIT, **kw), rt.hub.limits

            # one symbol per case: no stored snapshot, so each run uses the plain LIMIT-bar window
            plain, _ = await analyze("SIM:CLOSEDONLY", cs.upto(k))
            closed, limits = await analyze("SIM:FORMING", with_forming(cs, k), upto=t_k)
            burst, burst_limits = await analyze("SIM:BURST", with_forming(cs, k + 3), upto=t_k)
            assert len(plain["bars"]["time"]) == LIMIT and plain["bars"]["time"][-1] == t_k
            for resp in (closed, burst):
                assert resp["bars"]["time"] == plain["bars"]["time"]  # same start and length
                assert resp["bars"]["score"] == plain["bars"]["score"]
                assert resp["markers"] == plain["markers"] and resp["trades"] == plain["trades"]
                cur = resp["current"]
                assert (cur["time"], cur["action"], cur["price"]) == (t_k, plain["current"]["action"],
                                                                       plain["current"]["price"])
            assert limits == [LIMIT + ANCHOR_SPAN + 1]  # only the forming bar was dropped: one fetch
            assert burst_limits == [LIMIT + ANCHOR_SPAN + 1, LIMIT + ANCHOR_SPAN + 4]  # refetched for 3 more bars

            # the live view of that moment (forming bar included) shares the closed decision's window and history
            live, _ = await analyze("SIM:FORMING", with_forming(cs, k))
            assert live["bars"]["time"] == closed["bars"]["time"] + [t_next]
            assert common_view(live, t_k) == common_view(closed, t_k)

    asyncio.run(scenario())


def test_a_forming_bar_never_becomes_an_anchor(runtime_factory, monkeypatch, no_ttl):
    """A still-forming bar's state is provisional: stored as the 'last' snapshot only. The grid anchor of its bucket
    is stored once another bar has closed after it (its own bar-close decision, with any semáforo veto of it, is
    then known), by the first run that processes it as history."""
    cs = make_series()
    bucket = SNAPSHOT_EVERY * H
    k = next(i for i in range(600, 800) if int(cs.time[i + 1]) // bucket != int(cs.time[i]) // bucket)
    t_new = int(cs.time[k + 1])  # first bar of a snapshot bucket
    clock = {"now": t_new + 120}
    monkeypatch.setattr(service_mod, "now_s", lambda: clock["now"])

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            rt.hub = RecordingHub(with_forming(cs, k))
            live = await svc.analyze(SYM, "1h", LIMIT)
            assert live["bars"]["time"][-1] == t_new
            snaps = await stored_snapshots(rt)
            assert t_new not in anchors(snaps)
            assert (snaps[-1]["time"], snaps[-1]["kind"]) == (t_new, "last")
            clock["now"] = t_new + H + 120  # bar k+1 closed, bar k+2 forming: k+1 is the newest closed bar
            rt.hub = RecordingHub(with_forming(cs, k + 1))
            await svc.analyze(SYM, "1h", LIMIT)
            assert t_new not in anchors(await stored_snapshots(rt))
            clock["now"] = t_new + 2 * H + 120  # bar k+2 closed too
            rt.hub = RecordingHub(with_forming(cs, k + 2))
            await svc.analyze(SYM, "1h", LIMIT)
            stored = anchors(await stored_snapshots(rt))[t_new]
            assert stored["kind"] == "grid" and stored["state"]["resume"]["time"] == t_new

    asyncio.run(scenario())


def test_anchor_window_picks_the_latest_fitting_anchor():
    t0 = FIXED_END - 999 * H
    times = np.arange(1000, dtype=np.int64) * H + t0

    def snap(i: int, kind: str | None = "grid") -> dict:
        item = {"time": int(t0 + i * H), "state": {"i": i}}
        return item if kind is None else {**item, "kind": kind}

    snaps = [snap(200), snap(250, "origin"), snap(300), snap(400), snap(500, "last"), snap(990)]
    # newest anchor that leaves >= limit bars: index 400 (window 341..999, the first processed bar is 401)
    start, anchor = anchor_window(times, 999, 600, snaps)
    assert (start, anchor["state"]) == (400 + 1 - WARMUP_BARS, {"i": 400})
    start, anchor = anchor_window(times, 520, 300, snaps)  # origin anchors count too
    assert (start, anchor["kind"]) == (250 + 1 - WARMUP_BARS, "origin")
    # never a 'last' snapshot, a snapshot after the newest closed bar, a bar missing from the data or one whose
    # window would start before the fetched bars: then the plain `limit`-bar window and no anchor
    assert anchor_window(times, 999, 400, [snap(560, "last")]) == (600, None)
    assert anchor_window(times, 950, 400, [snap(990)]) == (551, None)
    assert anchor_window(times, 999, 400, [{"time": int(t0 + 500 * H + 1800), "state": {}, "kind": "grid"}]) \
        == (600, None)
    assert anchor_window(times, 999, 600, [snap(30)]) == (400, None)
    # snapshots stored before kinds existed are anchors
    start, anchor = anchor_window(times, 999, 700, [snap(300, None)])
    assert (start, anchor["state"]) == (300 + 1 - WARMUP_BARS, {"i": 300})
