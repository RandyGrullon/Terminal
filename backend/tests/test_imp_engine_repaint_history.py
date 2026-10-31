"""engine-repaint (improvement 7): NEXUS history never repaints, anchor steps included.

300 consecutive bar closes at limit 1000, with an empty memory (cold) and with memory stored over an earlier stretch
plus a trained global gate state, pooled into the last bar's decisions (warm). Every close runs the bar-close
decision (watcher, `upto`) and the live view of that moment (forming bar):
- past markers, trades and bar values of consecutive closed-bar runs never change: 0 changes over the 299 pairs,
  through at least two anchor steps (the window moves to the next anchor and continues the stored path), the
  previous run's last bar included (its bar-close decision, markers and action; only the fill of an entry decided
  at that close moves from the close to the next open, by design);
- the live view shows exactly the closed-bar decision and history (checked at every close cold, every 3rd warm);
- the history shown equals one uninterrupted causal run from the chain's cold start (cold case), so zero lookahead
  follows from the engine's prefix property (test_engine_nexus.test_no_lookahead_prefix);
- the mean analysis time is printed (measure; the engine budget is test_imp_engine_repaint_service).
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time

import pytest

from app.engine import service as service_mod
from app.engine.nexus import run_nexus
from app.engine.service import ANCHOR_SPAN, NexusService
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_fix_engine_watch import with_forming
from tests.test_imp_engine_repaint_gates import global_gate

SYM = "SIM:REPAINT"
LIMIT = 1000
CLOSES = 300
PER_BAR = ("score", "prob_up", "action", "regime", "ema_fast", "ema_slow", "ema_trend", "pos_entry", "pos_sl",
           "pos_tp1", "pos_tp2", "pos_tp3")


class Hub:
    def __init__(self, cs) -> None:
        self.cs = cs

    async def get_candles(self, symbol: str, tf: str = "1h", limit: int = 500):
        return self.cs.tail(limit)


def first_processed(resp: dict) -> int:
    bars = resp["bars"]
    return next(t for t, s in zip(bars["time"], bars["score"]) if s is not None)


def history(resp: dict, lo: int, hi: int, now: int | None = None) -> tuple:
    """Bar values, markers and closed trades of `resp` for the bars in [lo, hi]. `now` = a bar that is the last one
    of the run: an entry decided at its close fills there (the next run fills it at the next open), so its levels
    and the trades closed at that close are left out; its decision (action, score...) and markers are kept."""
    bars = resp["bars"]
    idx = [i for i, t in enumerate(bars["time"]) if lo <= t <= hi]
    vals = {k: [bars[k][i] for i in idx] for k in PER_BAR}
    if now is not None and idx and bars["time"][idx[-1]] == now:
        for k in ("pos_entry", "pos_sl", "pos_tp1", "pos_tp2", "pos_tp3"):
            vals[k] = vals[k][:-1]
    marks = [m for m in resp["markers"] if lo <= m["time"] <= hi]
    trades = [t for t in resp["trades"] if t["exit_time"] is not None and lo <= t["exit_time"] < (now or hi + 1)]
    return [bars["time"][i] for i in idx], vals, marks, trades


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 0}
    monkeypatch.setattr(service_mod, "now_s", lambda: now["t"])
    monkeypatch.setattr(service_mod, "CACHE_TTL_S", 0.0)  # every call sees the hub's newest bars
    return now


async def _closes(svc: NexusService, rt, cs, clock, k0: int, report: dict, live_every: int = 1) -> dict:
    prev, changed, live_diff, steps, seen = None, 0, 0, 0, {"markers": 0, "trades": 0, "live": 0}
    times: list[float] = []
    for k in range(k0, k0 + CLOSES):
        t_k = int(cs.time[k])
        clock["t"] = int(cs.time[k + 1]) + 120  # bar k closed two minutes ago, bar k+1 is forming
        rt.hub = Hub(with_forming(cs, k))
        a = time.perf_counter()
        closed = await svc.analyze(SYM, "1h", LIMIT, upto=t_k)
        times.append(time.perf_counter() - a)
        assert closed["ok"] and closed["bars"]["time"][-1] == t_k
        assert LIMIT <= len(closed["bars"]["time"]) <= LIMIT + 150
        lo = first_processed(closed)
        if (k - k0) % live_every == 0:
            live = await svc.analyze(SYM, "1h", LIMIT)
            assert live["bars"]["time"][-1] == int(cs.time[k + 1])
            seen["live"] += 1
            # the live view of this moment shows the closed-bar decision and the same history
            if history(live, lo, t_k, t_k) != history(closed, lo, t_k, t_k) or \
                    live["bars"]["action"][-2] != closed["current"]["action"]:
                live_diff += 1
        if prev is not None:
            p_last = prev["bars"]["time"][-1]
            steps += first_processed(prev) != lo
            start = max(first_processed(prev), lo)
            old, new = history(prev, start, p_last, p_last), history(closed, start, p_last, p_last)
            changed += old != new
            seen["markers"] += len(old[2])
            seen["trades"] += len(old[3])
        prev = closed
    report.update(changed=changed, live_diff=live_diff, steps=steps, seen=seen, last=prev,
                  mean_ms=1000 * statistics.mean(times))
    return report


def _series(n: int):
    return generate_candleset(n=n, seed=3, tf="1h", end_time=FIXED_END, symbol=SYM)


def test_300_bar_closes_never_repaint_from_a_cold_memory(runtime_factory, clock):
    cs = _series(LIMIT + ANCHOR_SPAN + CLOSES + 10)
    k0 = LIMIT + ANCHOR_SPAN + 2

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            out = await _closes(svc, rt, cs, clock, k0, {})
            print(f"\n[engine-repaint] cold: {out['steps']} anchor steps, mean {out['mean_ms']:.0f} ms per analysis")
            assert out["changed"] == 0 and out["live_diff"] == 0 and out["seen"]["live"] == CLOSES
            assert out["steps"] >= 2  # the window moved to a new anchor at least twice
            assert out["seen"]["markers"] > 1000 and out["seen"]["trades"] > 100  # real markers and trades compared
            # the history shown is one uninterrupted causal run from the cold start of the first (plain) window
            last = out["last"]
            single = run_nexus(cs.slice(k0 - LIMIT + 1, k0 + CLOSES), svc.params(SYM)).to_response(cs)
            lo, hi = first_processed(last), last["bars"]["time"][-1]
            assert history(single, lo, hi) == history(last, lo, hi)
            assert single["current"]["action"] == last["current"]["action"]

    asyncio.run(scenario())


def test_300_bar_closes_never_repaint_with_a_warm_memory(runtime_factory, clock):
    pre = 700
    cs = _series(LIMIT + ANCHOR_SPAN + pre + CLOSES + 10)
    k0 = LIMIT + ANCHOR_SPAN + 2

    async def scenario():
        async with runtime_factory() as rt:
            await rt.db.execute("INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                                "VALUES ('gate', 'global', ?, 600, NULL, 0)", (json.dumps(global_gate()),))
            svc = NexusService(rt)
            await svc.reload_gate_state()  # what the evaluator trains on real outcomes: pooled in at the last bar
            assert svc.status()["gate_updates"] == 600
            for k in range(k0, k0 + pre, 25):  # memory from an earlier stretch: anchors and a trained learner
                clock["t"] = int(cs.time[k + 1]) + 120
                rt.hub = Hub(with_forming(cs, k))
                await svc.analyze(SYM, "1h", LIMIT, upto=int(cs.time[k]))
            out = await _closes(svc, rt, cs, clock, k0 + pre, {}, live_every=3)
            print(f"\n[engine-repaint] warm: {out['steps']} anchor steps, mean {out['mean_ms']:.0f} ms per analysis")
            assert out["changed"] == 0 and out["live_diff"] == 0
            assert out["steps"] >= 2 and out["seen"]["markers"] > 1000 and out["seen"]["trades"] > 100
            assert out["last"]["learning"]["updates"] > 1500  # the learner kept training across the anchors
            # the global state changed some bar-close decisions: stored, and replayed as taken once history
            assert len(await svc._gates_for(SYM, "1h")) >= 3

    asyncio.run(scenario())
