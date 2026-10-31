"""Trade Gate — "¿Tomo esta operación?" (§8).

`evaluate_gate(ctx, direction, params, *, entry=None, sl=None, tp=None, gate_state=None) -> GateResult dict`.
Check weights are learned by `GateLearner` (online logistic regression on the check vector predicting "the trade
reached TP1 before the SL"); its JSON state lives in model_state(kind='gate', key='global').
"""
from __future__ import annotations

import math
from typing import Any

from app.engine import fmt
from app.engine.gate_checks import (
    BASE_WEIGHT, CHECK_DEFS, CHECK_IDS, CHECK_LABEL, WAITABLE, CheckOut, GateContext, run_checks,
)
from app.engine.levels import drift_k, first_passage, levels_from_user, plan_ev
from app.engine.learner import sigmoid

__all__ = ["GateContext", "GateLearner", "evaluate_gate", "CHECK_IDS", "CHECK_LABEL", "checks_vector"]

GATE_STATE_VERSION = 1


def _as_flag(v: Any) -> float:
    """1 / True -> +1 (passed), 0 / False -> -1 (failed), None / unknown -> 0 (neutral)."""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else -1.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return 1.0 if f > 0.5 else -1.0


def checks_vector(checks: Any) -> dict[str, int | None]:
    """GateResult.checks (list of dicts) or a {id: 1|0|None} dict -> {id: 1|0|None}."""
    if isinstance(checks, dict):
        items = checks.items()
    else:
        items = [(c.get("id"), c.get("passed")) for c in (checks or []) if isinstance(c, dict)]
    out: dict[str, int | None] = {}
    for cid, val in items:
        if cid is None:
            continue
        flag = _as_flag(val)
        out[str(cid)] = None if flag == 0.0 else (1 if flag > 0 else 0)
    return out


class GateLearner:
    """Online logistic regression (AdaGrad, L2) of P(TP1 before SL | check vector)."""

    LR = 0.15
    L2 = 1e-3

    def __init__(self) -> None:
        self.theta: dict[str, float] = {cid: 0.0 for cid in CHECK_IDS}
        self.grad_sq: dict[str, float] = {cid: 0.0 for cid in CHECK_IDS}
        self.bias = 0.0
        self.bias_sq = 0.0
        self.updates = 0
        self.wins = 0

    @classmethod
    def from_state(cls, state: dict | None) -> "GateLearner":
        model = cls()
        if not isinstance(state, dict):
            return model
        try:
            for cid, v in (state.get("theta") or {}).items():
                if cid in model.theta and isinstance(v, (int, float)) and math.isfinite(v):
                    model.theta[cid] = max(min(float(v), 5.0), -5.0)
            for cid, v in (state.get("grad_sq") or {}).items():
                if cid in model.grad_sq and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0:
                    model.grad_sq[cid] = float(v)
            b = state.get("bias", 0.0)
            model.bias = max(min(float(b), 5.0), -5.0) if isinstance(b, (int, float)) and math.isfinite(b) else 0.0
            bs = state.get("bias_sq", 0.0)
            model.bias_sq = float(bs) if isinstance(bs, (int, float)) and math.isfinite(bs) and bs >= 0 else 0.0
            model.updates = max(int(state.get("updates") or 0), 0)
            model.wins = min(max(int(state.get("wins") or 0), 0), model.updates)
        except (TypeError, ValueError, AttributeError):
            return cls()
        return model

    def copy(self) -> "GateLearner":
        return GateLearner.from_state(self.to_state())

    @classmethod
    def pooled(cls, a: "GateLearner", b: "GateLearner") -> "GateLearner":
        """Combine two learners (coefficients averaged by their number of updates)."""
        wa, wb = a.updates + 1.0, b.updates + 1.0
        out = cls()
        for cid in CHECK_IDS:
            out.theta[cid] = (a.theta[cid] * wa + b.theta[cid] * wb) / (wa + wb)
            out.grad_sq[cid] = max(a.grad_sq[cid], b.grad_sq[cid])
        out.bias = (a.bias * wa + b.bias * wb) / (wa + wb)
        out.bias_sq = max(a.bias_sq, b.bias_sq)
        out.updates = a.updates + b.updates
        out.wins = a.wins + b.wins
        return out

    def to_state(self) -> dict:
        return {
            "version": GATE_STATE_VERSION,
            "theta": {k: round(v, 6) for k, v in self.theta.items()},
            "grad_sq": {k: round(v, 6) for k, v in self.grad_sq.items()},
            "bias": round(self.bias, 6),
            "bias_sq": round(self.bias_sq, 6),
            "updates": int(self.updates),
            "wins": int(self.wins),
            "weights": self.weights(),
        }

    def _z(self, checks: Any) -> tuple[float, dict[str, float]]:
        vec = checks_vector(checks)
        x = {cid: _as_flag(vec.get(cid)) for cid in CHECK_IDS}
        return self.bias + sum(self.theta[c] * x[c] for c in CHECK_IDS), x

    def predict(self, checks: Any) -> float:
        """Learned probability that a trade with this check vector reaches TP1 before the SL."""
        z, _ = self._z(checks)
        return sigmoid(z)

    def score(self, checks: Any) -> float:
        """Score a check vector (alias of `predict`)."""
        return self.predict(checks)

    def update(self, checks: Any, won: bool) -> None:
        z, x = self._z(checks)
        err = sigmoid(z) - (1.0 if won else 0.0)
        for cid in CHECK_IDS:
            if x[cid] == 0.0:
                continue
            g = err * x[cid] + self.L2 * self.theta[cid]
            self.grad_sq[cid] += g * g
            self.theta[cid] = max(min(self.theta[cid] - self.LR * g / (math.sqrt(self.grad_sq[cid]) + 1e-8), 5.0), -5.0)
        self.bias_sq += err * err
        self.bias = max(min(self.bias - self.LR * err / (math.sqrt(self.bias_sq) + 1e-8), 5.0), -5.0)
        self.updates += 1
        self.wins += 1 if won else 0

    def weights(self) -> dict[str, float]:
        """Importance of each check (0.05..1): prior weight scaled by what the outcomes taught."""
        return {cid: round(min(max(BASE_WEIGHT[cid] * math.exp(1.5 * self.theta[cid]), 0.05), 1.0), 4)
                for cid in CHECK_IDS}

    def confidence(self) -> float:
        """0..1 trust in the learned probability (grows with resolved outcomes)."""
        return min(self.updates / 100.0, 1.0) * 0.8


def _learner(gate_state: Any) -> GateLearner:
    if isinstance(gate_state, GateLearner):
        return gate_state
    return GateLearner.from_state(gate_state if isinstance(gate_state, dict) else None)


def _param(params: Any, name: str, default: Any) -> Any:
    v = getattr(params, name, None)
    if v is None and isinstance(params, dict):
        v = params.get(name)
    return default if v is None else v


_lower_first = fmt.lower_first


def _validate_user_levels(side: int, entry: float, sl: float | None, tp: float | None) -> None:
    if not (math.isfinite(entry) and entry > 0):
        raise ValueError("El precio de entrada debe ser un número mayor que 0")
    if sl is not None:
        if not math.isfinite(sl):
            raise ValueError("El stop debe ser un número válido")
        if side * (entry - sl) <= 0:
            where = "por debajo" if side > 0 else "por encima"
            raise ValueError(f"El stop ({fmt.price(sl)}) debe estar {where} de la entrada ({fmt.price(entry)})")
    if tp is not None:
        if not math.isfinite(tp):
            raise ValueError("El objetivo debe ser un número válido")
        if side * (tp - entry) <= 0:
            where = "por encima" if side > 0 else "por debajo"
            raise ValueError(f"El objetivo ({fmt.price(tp)}) debe estar {where} de la entrada ({fmt.price(entry)})")


def structural_drift(p_dir: float, ctx: GateContext, lv: Any, horizon: int) -> float:
    """Conservative drift (2μ/σ²) for the first-passage model of a planned trade.

    The directional edge is shrunk by the skill the probability model has shown (a cold or coin-like model barely
    moves the odds) and fades after the forecast horizon: over the expected life of the trade (a·b/σ² bars for a
    driftless walk) only `horizon` bars carry the edge."""
    skill = min(max(ctx.prob_skill if ctx.prob_skill == ctx.prob_skill else 0.5, 0.25), 1.0)
    p_eff = 0.5 + skill * (p_dir - 0.5)
    sigma = ctx.sigma if ctx.sigma == ctx.sigma and ctx.sigma > 0 else (ctx.atr * 0.8 if ctx.atr == ctx.atr else 0.0)
    k = drift_k(p_eff, sigma, horizon)
    if sigma > 0:
        life = abs(lv.tp1 - lv.entry) * lv.risk / (sigma * sigma)
        if life > horizon:
            k *= horizon / life
    return k


def evaluate_gate(ctx: GateContext, direction: str, params: Any, *, entry: float | None = None,
                  sl: float | None = None, tp: float | None = None, gate_state: Any = None) -> dict:
    """GateResult for a long/short idea at the context bar (entry defaults to the close; user overrides respected)."""
    direction = str(direction or "").lower()
    if direction not in ("long", "short"):
        raise ValueError("La dirección debe ser 'long' o 'short'")
    side = 1 if direction == "long" else -1
    sl_atr = float(_param(params, "sl_atr", 2.0))
    tp_r_raw = list(_param(params, "tp_r", (1.0, 2.0, 3.0)))[:3]
    while len(tp_r_raw) < 3:
        tp_r_raw.append(tp_r_raw[-1] + 1.0 if tp_r_raw else 1.0)
    tp_r = (float(tp_r_raw[0]), float(tp_r_raw[1]), float(tp_r_raw[2]))
    trail_atr = float(_param(params, "trail_atr", 3.0))
    fee_pct = float(_param(params, "fee_pct", 0.1))
    horizon = int(_param(params, "horizon", 5))
    min_rr = float(_param(params, "min_rr", 1.5))
    min_prob = float(_param(params, "min_prob", 0.52))
    take_score = float(_param(params, "take_score", 65.0))
    wait_score = float(_param(params, "wait_score", 45.0))

    entry_v = float(entry) if entry is not None else float(ctx.close)
    sl_v = float(sl) if sl is not None else None
    tp_v = float(tp) if tp is not None else None
    _validate_user_levels(side, entry_v, sl_v, tp_v)
    swing = ctx.swing_low if side > 0 else ctx.swing_high
    lv = levels_from_user(side, entry_v, ctx.atr, swing, sl_atr, tp_r, sl_v, tp_v)
    p_up = ctx.prob_up if ctx.prob_up == ctx.prob_up else 0.5
    p_dir = p_up if side > 0 else 1.0 - p_up
    k = structural_drift(p_dir, ctx, lv, horizon)
    p_struct = first_passage(abs(lv.tp1 - lv.entry), lv.risk, k)

    learner = _learner(gate_state)
    wins, n = ctx.edge.get(direction, (0, 0))
    w_edge = min(n / 30.0, 1.0) * 0.6 if n >= 8 else 0.0
    p_edge = (wins + 1.0) / (n + 2.0) if n else 0.5
    atr = ctx.atr
    plan = plan_ev(lv, p_struct, k, atr, trail_atr, fee_pct)
    checks = run_checks(ctx, side, lv, plan, p_dir, min_rr, min_prob)
    w_learn = learner.confidence()
    p_learn = learner.predict({c.id: c.passed for c in checks}) if w_learn > 0 else 0.5
    win_prob = (p_struct + w_edge * p_edge + w_learn * p_learn) / (1.0 + w_edge + w_learn)
    win_prob = min(max(win_prob, 0.02), 0.98)
    plan = plan_ev(lv, win_prob, k, atr, trail_atr, fee_pct)
    checks = run_checks(ctx, side, lv, plan, p_dir, min_rr, min_prob)
    return _assemble(ctx, direction, lv, plan, checks, learner.weights(), take_score, wait_score)


def _assemble(ctx: GateContext, direction: str, lv: Any, plan: Any, checks: list[CheckOut],
              weights: dict[str, float], take_score: float, wait_score: float) -> dict:
    num = den = 0.0
    for c in checks:
        if c.passed is None:
            continue
        w = weights.get(c.id, 0.5)
        den += w
        num += w if c.passed else 0.0
    score = 100.0 * num / den if den > 0 else 0.0
    failed = sorted((c for c in checks if c.passed is False),
                    key=lambda c: (not c.critical, -weights.get(c.id, 0.5)))
    passed = sorted((c for c in checks if c.passed), key=lambda c: -weights.get(c.id, 0.5))
    critical = any(c.critical for c in failed)
    main = failed[0] if failed else None
    if critical:
        verdict = "AVOID"
    elif score >= take_score:
        verdict = "TAKE"
    elif score >= wait_score or (main is not None and main.id in WAITABLE):
        verdict = "WAIT"
    else:
        verdict = "AVOID"
    label = fmt.VERDICT_LABEL[verdict]
    if verdict == "TAKE":
        why = " y ".join(_lower_first(CHECK_LABEL[c.id]) for c in passed[:2]) or "condiciones favorables"
        summary = f"{label}: {why} · R:R {fmt.num(plan.rr, 1)} · P(TP1) {fmt.prob(plan.p_tp1)}"
    else:
        parts = [_lower_first(c.detail) for c in failed[:2]]
        summary = f"{label}: " + ("; ".join(parts) if parts else f"puntuación {fmt.num(score, 0)}/100 insuficiente")
    return {
        "direction": direction,
        "verdict": verdict,
        "verdict_label": label,
        "score": round(score, 1),
        "win_prob": round(plan.p_tp1, 4),
        "rr": round(plan.rr, 3),
        "ev_r": round(plan.ev_r, 4),
        "entry": lv.entry,
        "sl": lv.sl,
        "tp1": lv.tp1,
        "tp2": lv.tp2,
        "tp3": lv.tp3,
        "checks": [
            {
                "id": c.id,
                "label": CHECK_LABEL.get(c.id, c.id),
                "passed": c.passed,
                "critical": bool(c.critical),
                "weight": weights.get(c.id, 0.5),
                "value": c.value,
                "detail": c.detail,
            }
            for c in checks
        ],
        "reasons_for": [c.detail for c in passed[:6]],
        "reasons_against": [c.detail for c in failed[:6]],
        "summary": summary,
        "time": int(ctx.time),
    }


def gate_defs() -> list[dict]:
    """Static description of the checks (id, label, prior weight, wait-able)."""
    return [{"id": c[0], "label": c[1], "base_weight": c[2], "waitable": c[3]} for c in CHECK_DEFS]
