"""NEXUS engine: causality (no lookahead), determinism, JSON hygiene, contract shapes and performance."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from app.engine.nexus import NexusParams, NexusResult, run_nexus
from app.sim.synthetic import generate_candleset
from app.util import clean_json

END = 1_760_000_400
PER_BAR = ("time", "score", "prob_up", "action", "regime", "ema_fast", "ema_slow", "ema_trend",
           "pos_entry", "pos_sl", "pos_tp1", "pos_tp2", "pos_tp3")
DECISION = ("score", "prob_up", "regime", "action")
POS_KEYS = ("pos_entry", "pos_sl", "pos_tp1", "pos_tp2", "pos_tp3")


@pytest.fixture(scope="module")
def cs():
    return generate_candleset(n=900, seed=11, tf="1h", end_time=END)


@pytest.fixture(scope="module")
def full(cs):
    return run_nexus(cs, NexusParams())


def _prefix_ends(full: NexusResult) -> list[int]:
    """Prefix end bars covering every kind of decision (entries, exits, holds, waits)."""
    acts = full.bars["action"]
    ends: list[int] = []
    for kind in ("BUY", "SELL", "EXIT_LONG", "EXIT_SHORT", "HOLD_LONG", "HOLD_SHORT", "WAIT"):
        idx = [i for i, a in enumerate(acts) if a == kind and 150 < i < len(acts) - 2]
        ends += idx[:1] + idx[len(idx) // 2 : len(idx) // 2 + 1]
    return sorted(set(ends))


def test_no_lookahead_prefix(cs, full):
    """A run on the prefix ending at bar k must equal the full run for every bar < k. At bar k itself the decision
    (score, prob_up, regime, action) is identical; only the execution of a decision taken at that last close differs:
    the prefix fills it at close[k] (what the user should do now), the full run at open[k+1]. So at bar k the pos_*
    levels may differ only when the action is BUY/SELL, and trades opened/closed by a close-decision at k differ only
    in their fill price/time."""
    ends = _prefix_ends(full)
    kinds = {full.bars["action"][k] for k in ends}
    assert {"BUY", "HOLD_LONG", "WAIT"} <= kinds or {"SELL", "HOLD_SHORT", "WAIT"} <= kinds
    for k in ends:
        pre = run_nexus(cs.upto(k), NexusParams())
        assert len(pre.bars["time"]) == k + 1
        for key in PER_BAR:
            assert pre.bars[key][:k] == full.bars[key][:k], (k, key)
        for key in DECISION:
            assert pre.bars[key][k] == full.bars[key][k], (k, key)
        action = full.bars["action"][k]
        if action not in ("BUY", "SELL"):
            for key in POS_KEYS:
                assert pre.bars[key][k] == full.bars[key][k], (k, key)
        t_k = full.bars["time"][k]
        assert [m for m in pre.markers if m["time"] <= t_k] == [m for m in full.markers if m["time"] <= t_k]
        done = lambda res: [t for t in res.trades if t["exit_time"] is not None and t["exit_time"] < t_k]  # noqa: E731
        assert done(pre) == done(full)
        cur = pre.current
        assert cur["time"] == t_k and cur["action"] == action
        if action in ("BUY", "SELL"):
            pos = cur["position"]
            assert pos is not None and pos["entry_time"] == t_k and pos["entry_price"] == pytest.approx(cs.close[k])
            nxt = next(t for t in full.trades if t["entry_time"] == full.bars["time"][k + 1])
            assert nxt["entry_price"] == pytest.approx(cs.open[k + 1])
            assert (nxt["sl_initial"], nxt["tp1"], nxt["tp2"], nxt["tp3"]) == pytest.approx(
                (pos["sl"], pos["tp1"], pos["tp2"], pos["tp3"]))
            gate = cur["gate"]["long" if action == "BUY" else "short"]
            assert gate["sl"] == pytest.approx(pos["sl"]) and gate["entry"] == pytest.approx(cs.close[k])


def test_deterministic(cs, full):
    again = run_nexus(cs, NexusParams())
    a = json.dumps(clean_json(full.to_response(cs)), sort_keys=True)
    b = json.dumps(clean_json(again.to_response(cs)), sort_keys=True)
    assert a == b
    assert json.dumps(full.snapshots[-1]["state"], sort_keys=True) == json.dumps(again.snapshots[-1]["state"], sort_keys=True)


def test_response_shape_and_json_safety(cs, full):
    resp = full.to_response(cs)
    text = json.dumps(clean_json(resp), allow_nan=False)
    assert len(text) > 1000
    for key in ("symbol", "tf", "history", "ok", "message", "params", "bars", "markers", "trades", "current",
                "prediction", "learning", "stats"):
        assert key in resp
    n = len(cs)
    for key in PER_BAR:
        assert len(resp["bars"][key]) == n
    cur = resp["current"]
    for key in ("time", "price", "action", "action_label", "score", "prob_up", "confidence", "regime", "regime_label",
                "position", "gate", "advice", "warnings", "contributions", "levels", "reasons"):
        assert key in cur
    assert set(cur["gate"]) == {"long", "short"}
    assert cur["gate"]["long"]["entry"] == pytest.approx(cs.close[-1])
    assert -1.0 <= cur["score"] <= 1.0 and 0.0 <= cur["prob_up"] <= 1.0 and 0.0 <= cur["confidence"] <= 1.0
    assert isinstance(cur["advice"], str) and cur["advice"].endswith(".")
    assert len(cur["contributions"]) >= 10
    for m in resp["markers"]:
        assert set(m) >= {"time", "kind", "position", "shape", "color", "text"}
    times = [m["time"] for m in resp["markers"]]
    assert times == sorted(times)
    for t in resp["trades"]:
        assert set(t) == {"id", "side", "entry_time", "entry_price", "exit_time", "exit_price", "sl_initial", "tp1",
                          "tp2", "tp3", "exit_reason", "exit_label", "pnl_pct", "r_multiple", "bars", "mfe_pct",
                          "mae_pct", "gate_score"}
    pred = resp["prediction"]
    assert pred["horizon_bars"] == 5 and len(pred["cone"]) == 6
    assert all(p["p10"] <= p["p50"] <= p["p90"] for p in pred["cone"])
    assert [p["time"] for p in pred["cone"]] == [int(cs.time[-1]) + k * 3600 for k in range(6)]
    learning = resp["learning"]
    assert learning["updates"] > 500 and 0.0 <= learning["hit_rate"] <= 1.0
    assert set(learning["weights"]) == {"trend_up", "trend_down", "range", "volatile"}
    stats = resp["stats"]
    assert set(stats) == {"trades", "win_rate", "profit_factor", "total_return_pct", "max_dd_pct", "sharpe", "avg_r",
                          "expectancy_pct", "rejected_signals", "rejected_resolved", "rejected_would_win_rate"}
    assert 0 <= stats["rejected_resolved"] <= stats["rejected_signals"]


def test_markers_match_trades(full):
    """Every exit marker corresponds to a closed trade (intrabar exits on the same bar, close-decisions next bar)."""
    times = full.bars["time"]
    nxt = {times[i]: times[i + 1] for i in range(len(times) - 1)}
    exits = {(t["exit_time"], t["exit_reason"]) for t in full.trades if t["exit_time"] is not None}
    reason_of = {"EXIT_SL": "SL", "EXIT_TRAIL": "TRAIL", "EXIT_EARLY": "EARLY", "EXIT_REVERSE": "REVERSE",
                 "EXIT_TIME": "TIME", "EXIT_SIGNAL": "SIGNAL", "TP3": "TP3"}
    for m in full.markers:
        reason = reason_of.get(m["kind"])
        if reason is None:
            continue
        if reason in ("SL", "TRAIL", "TP3"):
            assert (m["time"], reason) in exits
        else:
            assert (nxt.get(m["time"]), reason) in exits or (m["time"], reason) in exits
    buys = sum(1 for m in full.markers if m["kind"] in ("BUY", "SELL"))
    assert buys >= len(full.trades) - 1  # each trade comes from an entry marker (gaps may void a few entries)


def test_snapshots_and_prior(cs):
    first = run_nexus(cs.slice(0, 600), NexusParams())
    assert first.snapshots and all(s["time"] <= int(cs.time[599]) for s in first.snapshots)
    json.dumps(first.snapshots, allow_nan=False)
    window = cs.slice(300, 900)
    usable = [s for s in first.snapshots if s["time"] <= int(window.time[0])]
    assert usable, "a snapshot must exist before the window start"
    prior = usable[-1]["state"]
    with_prior = run_nexus(window, NexusParams(), prior=prior)
    without = run_nexus(window, NexusParams())
    assert with_prior.learning["updates"] > without.learning["updates"]
    assert with_prior.learning["memory_bars"] > without.learning["memory_bars"]
    assert run_nexus(window, NexusParams(), prior={"garbage": True}).ok


def test_params_from_settings_contract():
    from app.config import DEFAULT_SETTINGS

    p = NexusParams.from_settings(DEFAULT_SETTINGS)
    assert p.gate_enabled is True and p.tp_r == (1.0, 2.0, 3.0) and p.fee_pct == pytest.approx(0.1)
    q = NexusParams.from_settings({**DEFAULT_SETTINGS, "gate.enabled": False, "nexus.horizon": 8}, sl_atr=3.0)
    assert q.gate_enabled is False and q.horizon == 8 and q.sl_atr == 3.0
    with pytest.raises(TypeError):
        NexusParams.from_settings(DEFAULT_SETTINGS, not_a_param=1)


def test_short_history_is_graceful():
    tiny = generate_candleset(n=40, seed=2, tf="1h", end_time=END)
    res = run_nexus(tiny, NexusParams())
    assert res.ok is False and res.current is None and "insuficiente" in res.message
    json.dumps(clean_json(res.to_response(tiny)), allow_nan=False)


def test_performance_budget():
    big = generate_candleset(n=2000, seed=5, tf="1h", end_time=END)
    run_nexus(big.tail(300), NexusParams())  # warm-up (imports, caches)
    best2 = min(_timed(big) for _ in range(2))
    best1 = min(_timed(big.tail(1000)) for _ in range(2))
    assert best2 < 1.0, f"2000 velas: {best2:.3f}s"
    assert best1 < 0.5, f"1000 velas: {best1:.3f}s"


def _timed(cs) -> float:
    t0 = time.perf_counter()
    run_nexus(cs, NexusParams())
    return time.perf_counter() - t0


def test_ungated_takes_every_candidate(cs, full):
    ungated = run_nexus(cs, NexusParams(gate_enabled=False))
    assert ungated.stats["rejected_signals"] == 0
    assert not any(m["kind"].startswith("REJECTED") for m in ungated.markers)
    entries = lambda r: sum(1 for m in r.markers if m["kind"] in ("BUY", "SELL"))  # noqa: E731
    assert entries(ungated) >= entries(full)
    assert np.isfinite(ungated.stats["total_return_pct"])


def test_global_gate_state_only_affects_the_last_bar(cs, full):
    """The live gate state (learned from outcomes up to now) must not leak into historical decisions."""
    from app.engine.gate import GateLearner

    learner = GateLearner()
    for _ in range(200):
        learner.update({"volume": 1, "momentum": 0}, True)
        learner.update({"volume": 0, "momentum": 1}, False)
    res = run_nexus(cs, NexusParams(), gate_state=learner.to_state())
    t_last = full.bars["time"][-1]
    assert [m for m in res.markers if m["time"] < t_last] == [m for m in full.markers if m["time"] < t_last]
    assert res.bars["action"][:-1] == full.bars["action"][:-1]
    assert res.trades[: len(full.trades) - 1] == full.trades[: len(full.trades) - 1]
    assert res.learning["gate_weights"] != full.learning["gate_weights"]
    assert res.learning["gate_weights"]["volume"] > full.learning["gate_weights"]["volume"]
    assert res.snapshots[-2]["state"]["gate"] == full.snapshots[-2]["state"]["gate"]
    assert res.state["gate"] == full.state["gate"]  # what is persisted stays on the causal chain


def test_non_positive_prices_are_rejected_gracefully():
    from app.data.base import CandleSet

    base = generate_candleset(n=300, seed=8, tf="1h", end_time=END)
    low = base.low.copy()
    low[150] = -1.0
    bad = CandleSet(base.symbol, base.tf, base.time, base.open, base.high, low, base.close, base.volume)
    res = run_nexus(bad, NexusParams())
    assert res.ok is False and "positivos" in res.message and res.current is None
    json.dumps(clean_json(res.to_response(bad)), allow_nan=False)
