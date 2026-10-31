"""engine-repaint (verifier).

- A bar-close decision replayed while its bar is still the last one ("now" again: a market sitting on that close, a
  second bar-close view of it) keeps the whole GateResult it was decided with, so `current` never shows an action /
  advice from the recorded decision next to a gate block evaluated with a later global gate state. Without that,
  the action could say SELL while the filter block of the same bar said NO TOMAR.
- The exact resume also holds for data shapes the synthetic default does not have: session gaps (partial
  higher-timeframe buckets, weekends), no volume at all, flat bars, and other timeframes.
"""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from app.data.base import CandleSet
from app.engine import service as service_mod
from app.engine.gate import GateLearner
from app.engine.nexus import NexusParams, run_nexus
from app.engine.nexus_resume import resume_fit, roundtrip
from app.engine.service import NexusService
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_imp_engine_repaint_gates import candidate_bars, global_gate
from tests.test_imp_engine_repaint_history import Hub

H = 3600
SYM = "SIM:REPAINT3"
KEYS = ("time", "score", "prob_up", "action", "regime", "ema_fast", "ema_slow", "ema_trend", "pos_entry", "pos_sl",
        "pos_tp1", "pos_tp2", "pos_tp3")


def _rebuild(cs, keep=None, **arrays) -> CandleSet:
    cols = {k: np.asarray(getattr(cs, k)) for k in ("time", "open", "high", "low", "close", "volume")}
    cols.update(arrays)
    if keep is not None:
        cols = {k: v[keep] for k, v in cols.items()}
    return CandleSet(cs.symbol, cs.tf, cols["time"], cols["open"], cols["high"], cols["low"], cols["close"],
                     cols["volume"], provider="synthetic", history="full")


def sessions(cs) -> CandleSet:
    """Stock-like bars: 7 hourly bars per weekday (14:00-20:00 UTC), nothing on weekends."""
    t = np.asarray(cs.time)
    keep = np.isin((t // H) % 24, (14, 15, 16, 17, 18, 19, 20)) & ((((t // 86400) + 3) % 7) < 5)
    return _rebuild(cs, keep)


def flat_and_no_volume(cs) -> CandleSet:
    """No volume at all (forex / index feeds) and stretches of o = h = l = c bars (illiquid hours)."""
    o, h, lo, c = (np.array(getattr(cs, k), dtype=float) for k in ("open", "high", "low", "close"))
    for s in range(50, len(cs), 37):
        o[s : s + 6] = h[s : s + 6] = lo[s : s + 6] = c[s : s + 6] = c[s - 1]
    return _rebuild(cs, open=o, high=h, low=lo, close=c, volume=np.zeros(len(cs)))


def view(res, lo: int, hi: int) -> tuple:
    b = res.bars
    idx = [i for i, t in enumerate(b["time"]) if lo < t <= hi]
    return ({k: [b[k][i] for i in idx] for k in KEYS}, [m for m in res.markers if lo < m["time"] <= hi],
            [t for t in res.trades if lo < (t["exit_time"] or 10**12) <= hi])


@pytest.mark.parametrize("shape", ["sessions", "flat_no_volume", "15m", "1d"])
def test_resume_is_exact_for_other_data_shapes(shape):
    tf = shape if shape in ("15m", "1d") else "1h"
    cs = generate_candleset(n=6000 if shape == "sessions" else 1400, seed=21, tf=tf, end_time=FIXED_END)
    cs = sessions(cs).slice(0, 1400) if shape == "sessions" else cs
    cs = flat_and_no_volume(cs) if shape == "flat_no_volume" else cs
    p = NexusParams(horizon=12)
    base = run_nexus(cs, p)
    grids = [s for s in base.snapshots if s["kind"] == "grid"]
    t_end = base.bars["time"][-1]
    checked = warm = 0
    for snap in grids[:-1]:
        state = roundtrip(snap["state"])
        fit = resume_fit(state, cs.time, p)
        assert fit is not None, (shape, snap["time"])
        w, _ = fit
        res = run_nexus(cs.slice(w), p, prior=state)
        assert view(res, snap["time"], t_end) == view(base, snap["time"], t_end), (shape, snap["time"])
        checked += 1
        warm += state["resume"]["feat"]["rec"] is not None
    assert checked >= 5 and warm >= 2  # restore points of the recursive filters too, not only cold chain starts


def later_global_gate() -> dict:
    """The global state some resolved decisions later (what the evaluator trains while a market sits on a close)."""
    g = GateLearner.from_state(global_gate())
    for _ in range(40):
        g.update({"volume": 0, "momentum": 1, "trend_alignment": 1, "expected_value": 1}, True)
    return g.to_state()


@pytest.fixture(scope="module")
def cs():
    return generate_candleset(n=1300, seed=7, tf="1h", end_time=FIXED_END, symbol="SIM:GATES")


def test_a_replayed_last_bar_shows_the_gate_it_was_decided_with(cs):
    p, g = NexusParams(), global_gate()
    found = differs = 0
    for k in candidate_bars(cs, p, limit=20):
        t_k = int(cs.time[k])
        live = run_nexus(cs.upto(k), p, gate_state=g)  # the bar-close decision of bar k
        records = roundtrip(live.live_gates)
        if not records:
            continue
        found += 1
        assert all(isinstance(r.get("gate"), dict) for r in records)
        for state in (None, later_global_gate()):  # the global state is gone, or changed since that close
            again = run_nexus(cs.upto(k), p, gate_state=state, live_gates=records)
            assert again.current["action"] == live.current["action"], k
            assert again.current["advice"] == live.current["advice"], k
            assert [m for m in again.markers if m["time"] == t_k] == [m for m in live.markers if m["time"] == t_k]
            for r in records:
                d = r["direction"]
                assert again.current["gate"][d] == live.current["gate"][d], (k, d)
                fresh = run_nexus(cs.upto(k), p, gate_state=state).current["gate"][d]
                differs += (fresh["verdict"], fresh["score"]) != (r["verdict"], r["score"])
        # a record without the whole GateResult (an older bar's, trimmed by the service) still replays the decision
        slim = [{key: v for key, v in r.items() if key != "gate"} for r in records]
        again = run_nexus(cs.upto(k), p, live_gates=slim)
        assert again.current["action"] == live.current["action"]
        # and once the bar is history the replay is the same as before
        nxt = run_nexus(cs.upto(k + 1), p, gate_state=g, live_gates=records)
        assert [m for m in nxt.markers if m["time"] == t_k] == [m for m in live.markers if m["time"] == t_k]
    assert found >= 2 and differs >= 1  # a fresh evaluation would have shown another verdict or score


def test_the_full_gate_is_kept_for_the_newest_bar_only(runtime_factory):
    def rec(t: int) -> dict:
        return {"time": t, "direction": "long", "verdict": "TAKE", "score": 70.0, "reason": None,
                "summary": "TOMAR", "checks": [None] * 13, "fp": "A",
                "gate": {"verdict": "TAKE", "score": 70.0, "checks": []}}

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            await svc._record_gates(SYM, "1h", [rec(100 * H)])
            assert "gate" in (await svc._gates_for(SYM, "1h"))[(100 * H, "long")]
            await svc._record_gates(SYM, "1h", [rec(101 * H)])
            got = await NexusService(rt)._gates_for(SYM, "1h")  # persisted
            assert "gate" not in got[(100 * H, "long")] and got[(100 * H, "long")]["verdict"] == "TAKE"
            assert got[(101 * H, "long")]["gate"]["score"] == 70.0

    asyncio.run(scenario())


def test_a_market_sitting_on_a_close_keeps_the_gate_block_of_that_decision(runtime_factory, monkeypatch, cs):
    """Service: the watcher decides bar k at its close (record stored); the global gate state then changes and the
    market sits on that close (no new bar): every later view shows that decision, gate block included."""
    now = {"t": 0}
    monkeypatch.setattr(service_mod, "now_s", lambda: now["t"])
    monkeypatch.setattr(service_mod, "CACHE_TTL_S", 0.0)
    p = NexusParams()

    async def scenario():
        async with runtime_factory() as rt:
            await rt.db.execute("INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                                "VALUES ('gate', 'global', ?, 600, NULL, 0)", (json.dumps(global_gate()),))
            svc = NexusService(rt)
            await svc.reload_gate_state()
            checked = 0
            for k in candidate_bars(cs, p, limit=20):
                t_k = int(cs.time[k])
                rt.hub = Hub(cs.upto(k))  # the market sits on bar k: no forming bar
                now["t"] = t_k + H + 60
                closed = await svc.analyze(SYM, "1h", 300, upto=t_k)  # the watcher's bar-close decision
                recs = {d: r for (t, d), r in (await svc._gates_for(SYM, "1h")).items() if t == t_k}
                if not recs:
                    continue
                await rt.db.execute("UPDATE model_state SET state=? WHERE kind='gate' AND key='global'",
                                    (json.dumps(later_global_gate()),))
                await svc.reload_gate_state()
                now["t"] = t_k + 30 * H  # a weekend later: a plain view of that same close
                view = await svc.analyze(SYM, "1h", 300)
                assert view["current"]["time"] == t_k and view["current"]["action"] == closed["current"]["action"]
                for d in recs:
                    assert view["current"]["gate"][d] == closed["current"]["gate"][d], (k, d)
                await rt.db.execute("UPDATE model_state SET state=? WHERE kind='gate' AND key='global'",
                                    (json.dumps(global_gate()),))
                await svc.reload_gate_state()
                checked += 1
                if checked >= 3:
                    break
            assert checked >= 1

    asyncio.run(scenario())
