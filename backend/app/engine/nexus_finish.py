"""Assemble the NexusResult of a finished run (bars, current, prediction, learning, stats)."""
from __future__ import annotations

from typing import Any

import numpy as np

from app.engine.fmt import ACTION_LABEL
from app.engine.gate import evaluate_gate
from app.engine.nexus import NexusResult
from app.engine.nexus_current import build_advice, build_reasons, build_warnings, contributions, position_state
from app.engine.nexus_out import compute_stats, empty_stats
from app.engine.predict import build_prediction
from app.engine.regime import regime_label, regime_name
from app.util import to_list


def learning_dict(run: Any) -> dict:
    lr = run.learner
    return {
        "updates": int(lr.updates),
        "hit_rate": None if lr.hit_rate is None else round(lr.hit_rate, 4),
        "brier": None if lr.brier is None else round(lr.brier, 4),
        "weights": lr.weights_dict(),
        "gate_weights": run.decision_gate.weights(),
        "trained_until": lr.trained_until,
        "memory_bars": int(lr.memory_bars),
    }


def _bars(run: Any) -> dict:
    """Per-bar output from `run.out_start` (a resumed run leaves out the extra bars its features restarted from)."""
    f = run.f
    k = int(getattr(run, "out_start", 0))
    regimes = [regime_name(int(r)) for r in run.regime[k:].tolist()]
    return {
        "time": [int(t) for t in f.time[k:].tolist()],
        "score": to_list(run.score[k:], 4),
        "prob_up": to_list(run.prob[k:], 4),
        "action": list(run.action[k:]),
        "regime": regimes,
        "ema_fast": to_list(f.ema_fast[k:]),
        "ema_slow": to_list(f.ema_slow[k:]),
        "ema_trend": to_list(f.ema_trend[k:]),
        "pos_entry": list(run.lv_entry[k:]),
        "pos_sl": list(run.lv_sl[k:]),
        "pos_tp1": list(run.lv_tp[0][k:]),
        "pos_tp2": list(run.lv_tp[1][k:]),
        "pos_tp3": list(run.lv_tp[2][k:]),
    }


def build_current(run: Any) -> dict:
    i = run.n - 1
    f, p = run.f, run.p
    ctx = run.gate_ctx(i)
    # a direction whose last-bar candidate was replayed from its bar-close record shows the GateResult it was decided
    # with (the action, advice and marker come from it), not one evaluated with the global state of this moment
    now = getattr(run, "replayed_now", None) or {}
    gates = {
        "long": dict(now[1]) if 1 in now else evaluate_gate(ctx, "long", p, gate_state=run.decision_gate),
        "short": dict(now[-1]) if -1 in now else evaluate_gate(ctx, "short", p, gate_state=run.decision_gate),
    }
    action = run.action[i]
    pos_state, ev = position_state(run, i)
    warnings = build_warnings(run, i, ev)
    contribs = contributions(run, i)
    reg = regime_name(int(run.regime[i]))
    return {
        "time": int(f.time[i]),
        "price": float(f.close[i]),
        "action": action,
        "action_label": ACTION_LABEL.get(action, action),
        "score": round(float(run.score[i]), 4),
        "prob_up": round(float(run.prob[i]), 4),
        "confidence": round(float(run.conf[i]), 4),
        "regime": reg,
        "regime_label": regime_label(reg),
        "position": pos_state,
        "gate": gates,
        "advice": build_advice(run, i, action, gates, warnings),
        "warnings": warnings,
        "contributions": contribs,
        "levels": {"support": ctx.supports, "resistance": ctx.resistances},
        "reasons": build_reasons(run, i, contribs),
    }


def finish_run(run: Any) -> NexusResult:
    f, p = run.f, run.p
    last = run.n - 1
    trades = list(run.trades)
    if run.pos is not None:
        trades.append(run.pos.to_trade(None, None, "OPEN", None, float(f.close[last]), p.fee_pct))
    current = build_current(run)
    prediction = build_prediction(f.close, f.time, f.tf_s, run.score, run.regime, last, float(run.prob[last]),
                                  run.h, run.start)
    stats = compute_stats(trades, [c.get("won") for c in run.rejected], run.bar_ret, f.tf_s, f.time)
    markers = sorted(run.markers, key=lambda m: m["time"])
    return NexusResult(
        symbol=str(getattr(run.cs, "symbol", "")), tf=str(f.tf), history=str(getattr(run.cs, "history", "full")),
        params=p, ok=True, message=None, bars=_bars(run), markers=markers, trades=trades, current=current,
        prediction=prediction, learning=learning_dict(run), stats=stats, snapshots=run.snapshots,
        state=run.state_dict(), context=run.gate_ctx(last), decision_gate=run.decision_gate.to_state(),
        origin=run.origin, live_gates=list(run.live_records),
    )


def short_result(run: Any, message: str | None = None) -> NexusResult:
    """The engine cannot run (too few bars / invalid prices): empty-but-valid shapes and an explanatory message."""
    from app.engine.nexus_loop import MIN_RUN_BARS

    f, p = run.f, run.p
    k = int(getattr(run, "out_start", 0))
    n = run.n - k
    nan = np.full(n, np.nan)
    bars = {
        "time": [int(t) for t in f.time[k:].tolist()], "score": to_list(nan), "prob_up": to_list(nan),
        "action": ["WAIT"] * n, "regime": ["unknown"] * n,
        "ema_fast": to_list(f.ema_fast[k:]) if n else [], "ema_slow": to_list(f.ema_slow[k:]) if n else [],
        "ema_trend": to_list(f.ema_trend[k:]) if n else [],
        "pos_entry": [None] * n, "pos_sl": [None] * n, "pos_tp1": [None] * n, "pos_tp2": [None] * n,
        "pos_tp3": [None] * n,
    }
    return NexusResult(
        symbol=str(getattr(run.cs, "symbol", "")), tf=str(f.tf), history=str(getattr(run.cs, "history", "none")),
        params=p, ok=False,
        message=message or f"Historial insuficiente para NEXUS: {n} velas (mínimo {MIN_RUN_BARS}).",
        bars=bars, markers=[], trades=[], current=None, prediction=None, learning=learning_dict(run),
        stats=empty_stats(), snapshots=[], state=run.state_dict(),
    )
