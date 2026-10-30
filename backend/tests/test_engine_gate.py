"""Trade Gate: verdict rules, user overrides, learned weights and behaviour across market regimes."""
from __future__ import annotations

import json

import pytest

from app.engine.gate import GateLearner, checks_vector, evaluate_gate
from app.engine.gate_checks import CHECK_IDS, GateContext
from app.engine.nexus import NexusParams
from app.engine.nexus_loop import NexusRun
from app.sim.synthetic import generate_candleset

END = 1_760_000_400


def favorable_ctx(**kw) -> GateContext:
    """A clean bullish setup at 100 with ATR 1."""
    base = dict(
        time=END, close=100.0, atr=1.0, sigma=0.8, ema_slow=99.2, rsi=60.0, macd_hist=0.3, adx=28.0, er=0.45,
        atr_rank=0.5, shock=1.0, vol_z=0.4, htf_trend=0.6, htf_label="4h", prob_up=0.66, score=0.55,
        regime="trend_up", swing_low=98.5, swing_high=101.0, supports=[98.5, 97.0], resistances=[106.0],
        expert_signals=[0.8] * 10 + [-0.2, 0.1], expert_weights=[0.08] * 12,
        edge={"long": (10, 14), "short": (3, 12)}, edge_scope={"long": "", "short": ""},
        loss_streak=1, closed_trades=10,
    )
    base.update(kw)
    return GateContext(**base)


def test_favorable_long_is_taken_and_short_is_not():
    p = NexusParams()
    long = evaluate_gate(favorable_ctx(), "long", p)
    short = evaluate_gate(favorable_ctx(), "short", p)
    assert long["verdict"] == "TAKE" and long["verdict_label"] == "TOMAR"
    assert long["score"] >= p.take_score and long["summary"].startswith("TOMAR")
    assert short["verdict"] == "AVOID" and short["summary"].startswith("NO TOMAR")
    assert [c["id"] for c in long["checks"]] == CHECK_IDS
    assert long["entry"] == 100.0 and long["sl"] < 100.0 < long["tp1"] < long["tp2"] < long["tp3"]
    assert long["rr"] >= p.min_rr and long["ev_r"] > 0.05 and 0.0 < long["win_prob"] < 1.0
    assert all(isinstance(c["detail"], str) and c["detail"] for c in long["checks"])
    assert any(ch.isdigit() for ch in " ".join(long["reasons_for"]))
    json.dumps(long, allow_nan=False)


def test_poor_risk_reward_is_avoided():
    """A target closer than 1.2R (after fees) is a critical failure -> NO TOMAR."""
    g = evaluate_gate(favorable_ctx(), "long", NexusParams(), sl=98.0, tp=102.0)  # R = 2, reward = 2 -> RR < 1.2
    rr = next(c for c in g["checks"] if c["id"] == "risk_reward")
    assert g["rr"] < 1.2 and rr["passed"] is False and rr["critical"] is True
    assert g["verdict"] == "AVOID"


def test_negative_expected_value_is_avoided():
    g = evaluate_gate(favorable_ctx(prob_up=0.2, score=-0.3), "long", NexusParams())
    ev = next(c for c in g["checks"] if c["id"] == "expected_value")
    assert g["ev_r"] <= 0 and ev["passed"] is False and ev["critical"] is True
    assert g["verdict"] == "AVOID" and "negativo" in g["summary"]


def test_shock_bar_is_critical():
    g = evaluate_gate(favorable_ctx(shock=4.2), "long", NexusParams())
    vol = next(c for c in g["checks"] if c["id"] == "volatility")
    assert vol["passed"] is False and vol["critical"] is True and g["verdict"] == "AVOID"


def test_overextension_is_waitable():
    """Over-extended entry with momentum split and a nearby resistance: no critical failure -> ESPERAR."""
    ctx = favorable_ctx(ema_slow=96.5, rsi=79.0, macd_hist=-0.1, vol_z=-1.5, htf_trend=0.05,
                        expert_signals=[0.3, -0.3] * 6, resistances=[101.0], edge={"long": (1, 3), "short": (0, 0)})
    g = evaluate_gate(ctx, "long", NexusParams())
    failed = {c["id"] for c in g["checks"] if c["passed"] is False}
    assert {"overextension", "momentum", "room_to_target"} <= failed
    assert not any(c["critical"] for c in g["checks"] if c["passed"] is False)
    assert g["verdict"] == "WAIT" and g["summary"].startswith("ESPERAR")
    assert 45.0 <= g["score"] < 65.0


def test_user_overrides_are_respected():
    p = NexusParams()
    g = evaluate_gate(favorable_ctx(), "long", p, entry=100.5, sl=97.5, tp=109.5)
    assert (g["entry"], g["sl"], g["tp1"], g["tp2"], g["tp3"]) == (100.5, 97.5, 109.5, 109.5, 109.5)
    assert g["rr"] == pytest.approx((9.0 - 0.2 * 100.5 / 100) / (3.0 + 0.2 * 100.5 / 100), abs=1e-3)
    only_sl = evaluate_gate(favorable_ctx(), "long", p, sl=98.0)
    assert only_sl["sl"] == 98.0 and only_sl["tp1"] == pytest.approx(102.0) and only_sl["tp3"] == pytest.approx(106.0)
    short = evaluate_gate(favorable_ctx(), "short", p, entry=100.0, sl=103.0, tp=94.0)
    assert short["sl"] == 103.0 and short["tp2"] == 94.0
    with pytest.raises(ValueError, match="stop"):
        evaluate_gate(favorable_ctx(), "long", p, sl=101.0)
    with pytest.raises(ValueError, match="objetivo"):
        evaluate_gate(favorable_ctx(), "short", p, tp=105.0)
    with pytest.raises(ValueError):
        evaluate_gate(favorable_ctx(), "sideways", p)


def test_edge_history_neutral_until_eight_samples():
    g = evaluate_gate(favorable_ctx(edge={"long": (5, 7), "short": (0, 0)}), "long", NexusParams())
    edge = next(c for c in g["checks"] if c["id"] == "edge_history")
    assert edge["passed"] is None and "7 < 8" in edge["detail"]
    g2 = evaluate_gate(favorable_ctx(edge={"long": (2, 10), "short": (0, 0)}), "long", NexusParams())
    assert next(c for c in g2["checks"] if c["id"] == "edge_history")["passed"] is False


def test_gate_learner_round_trip_and_learning():
    learner = GateLearner()
    good = {cid: 1 for cid in CHECK_IDS}
    bad = dict(good, momentum=0, trend_alignment=0)
    for _ in range(60):
        learner.update(good, True)
        learner.update(bad, False)
    state = learner.to_state()
    text = json.dumps(state, allow_nan=False)
    clone = GateLearner.from_state(json.loads(text))
    assert clone.predict(good) == pytest.approx(learner.predict(good))
    assert clone.weights() == learner.weights() and clone.updates == 120
    assert learner.predict(good) > 0.7 > 0.3 > learner.predict(bad)
    w = learner.weights()
    assert w["momentum"] > 0.5 and w["trend_alignment"] > 0.7  # both above their prior weights
    assert GateLearner.from_state({"theta": "broken"}).updates == 0
    assert GateLearner.from_state(None).score(good) == pytest.approx(0.5)


def test_checks_vector_formats():
    as_list = [{"id": "a", "passed": True}, {"id": "b", "passed": False}, {"id": "c", "passed": None}]
    assert checks_vector(as_list) == {"a": 1, "b": 0, "c": None}
    assert checks_vector({"a": 1, "b": 0, "c": None, "d": True}) == {"a": 1, "b": 0, "c": None, "d": 1}


def test_learned_gate_state_changes_weights():
    learner = GateLearner()
    for _ in range(80):
        learner.update({"volume": 1}, True)
        learner.update({"volume": 0}, False)
    g = evaluate_gate(favorable_ctx(), "long", NexusParams(), gate_state=learner.to_state())
    vol = next(c for c in g["checks"] if c["id"] == "volume")
    assert vol["weight"] > 0.3  # prior 0.3, raised by the outcomes


def _take_counts(regime: str, seed: int) -> dict[str, int]:
    cs = generate_candleset(n=800, seed=seed, tf="1h", regime=regime, end_time=END, vol=0.006)
    run = NexusRun(cs, NexusParams(), None, None)
    run.run()
    counts = {"long": 0, "short": 0}
    for i in range(200, run.n, 5):
        run.ctx_cache = None
        ctx = run.gate_ctx(i)
        for d in counts:
            if evaluate_gate(ctx, d, run.p, gate_state=run.gate_learner)["verdict"] == "TAKE":
                counts[d] += 1
    return counts


@pytest.mark.parametrize("seed", [4, 5])
def test_bull_trend_gets_more_takes_than_choppy_range(seed):
    bull = _take_counts("bull", seed)
    rng = _take_counts("range", seed)
    assert bull["long"] > rng["long"] and bull["long"] > rng["short"]
    assert bull["long"] > bull["short"]
