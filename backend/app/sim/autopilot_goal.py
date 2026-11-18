"""NEXUS automática's challenge («reto»): a target equity by a deadline, optionally in an aggressive mode.

While a challenge runs, the paper engine itself watches the account on its monitor loop (every few seconds). When the
equity reaches the target, or at the deadline, it closes the account's trades (its buy-and-hold portfolio stays),
records the result and goes back to its normal settings (or pauses, if asked). No outside script is involved, so it
only needs the NEXUS server running. A restart keeps the challenge (meta `autopilot.goal`); a deadline that passed
while the server was down ends it at the next start, marked as late.

The aggressive mode is an overlay of the settings while the challenge lasts, and the user's saved settings never
change: faster bars, more risk per trade, MT5 lots, more leverage, more candidates and PyChart strategies without the
gate. For account 3 only, the daily loss limit and the open-risk cap are widened to the loss brake. Nothing here is
validated. A challenge does not create an edge (docs/VALIDATION.md): it reaches its target or its brake sooner.

Pure helpers only (validation, the overlay, the result and its texts). The engine side lives in app.sim.paper.
"""
from __future__ import annotations

from typing import Any

from app.sim.common import fmt_num
from app.util import safe_float

GOAL_KEY = "autopilot.goal"  # meta: the running challenge (JSON object, "null" when none)
GOAL_LAST_KEY = "autopilot.goal_last"  # meta: how the last challenge ended
GOAL_THEN = ("normal", "pause")  # after the challenge: back to its normal settings · or pause the account
GOAL_OUTCOMES = ("reached", "time", "cancelled", "brake")  # brake: challenge accounts only
GOAL_MAX_S = 7 * 86400  # a deadline at most a week ahead
GOAL_MIN_S = 60  # ... and at least a minute ahead
GOAL_MAX_MULT = 10.0  # a target at most 10 times the current equity

# the aggressive mode: setting -> value while the challenge lasts (read through PaperEngine._ap_setting)
AGGRESSIVE: dict[str, Any] = {
    "autopilot.tfs": ["5m", "15m", "1h"],
    "autopilot.risk_pct": 4.0,
    "autopilot.lots": True,
    "autopilot.lot_max_risk_pct": 10.0,
    "autopilot.perp_leverage": 20.0,
    "autopilot.scanner_top": 30,
    "autopilot.max_positions": 20,
    "autopilot.futures_margin_pct": 100.0,
    "autopilot.script_gate": False,
}
AGGRESSIVE_OPEN_RISK_PCT = 20.0  # account 3's open-risk cap in the aggressive mode (% of equity)
AGGRESSIVE_DAILY_DEFAULT = 20.0  # its daily loss limit when the loss brake is off
AGGRESSIVE_TEXT = (
    "Modo agresivo: velas de 5m, 15m y 1h · 4 % de riesgo por operación en lotes de MT5 · apalancamiento 20x en "
    "perpetuos · hasta 20 posiciones y 30 candidatos del escáner · estrategias de PyChart sin el filtro · límite de "
    "pérdida diaria igual al freno de pérdidas. Tus ajustes guardados no cambian: vuelven solos al terminar."
)
GOAL_NOTE = (
    "Un reto no crea ventaja: la validación no la encontró, y más riesgo por operación solo hace que llegue antes a "
    "la meta o al freno de pérdidas. Simulado, sin dinero real; no es asesoría financiera."
)
OUTCOME_LABEL = {"reached": "Meta cumplida", "time": "Se acabó el tiempo", "cancelled": "Reto terminado a mano",
                 "brake": "Freno de pérdidas"}


def aggressive_risk(brake_pct: float) -> dict[str, float]:
    """Account 3's portfolio-risk limits in the aggressive mode: its daily loss limit is its loss brake (the money at
    stake for the whole challenge), so a single 4 % loss does not block it for the rest of the UTC day."""
    brake = float(brake_pct or 0.0)
    return {"daily_loss_limit_pct": brake if brake > 0 else AGGRESSIVE_DAILY_DEFAULT,
            "max_open_risk_pct": AGGRESSIVE_OPEN_RISK_PCT}


def parse_goal(target: Any, until: Any, aggressive: Any, then: Any, *, equity: float, now: int) -> dict[str, Any]:
    """A new challenge from the user's values (ValueError in Spanish on a bad one). `until`: unix seconds or None."""
    t = safe_float(target)
    eq = float(equity)
    if t is None or t <= 0:
        raise ValueError("La meta debe ser un importe mayor que 0")
    if eq <= 0:
        raise ValueError("La cuenta de NEXUS no tiene capital: reiníciala antes de empezar un reto")
    if t <= eq:
        raise ValueError(f"La meta debe ser mayor que el valor actual de la cuenta ({fmt_num(eq, 2)})")
    if t > eq * GOAL_MAX_MULT:
        raise ValueError(f"La meta puede ser como mucho {fmt_num(GOAL_MAX_MULT, 0)} veces el valor actual de la cuenta "
                         f"({fmt_num(eq * GOAL_MAX_MULT, 0)})")
    deadline: int | None = None
    if until not in (None, "", 0):
        u = safe_float(until)
        if u is None:
            raise ValueError("La hora límite no es válida")
        if u > 1e12:  # milliseconds
            u /= 1000.0
        deadline = int(u)
        if deadline < now + GOAL_MIN_S:
            raise ValueError("La hora límite tiene que ser al menos 1 minuto más tarde que ahora")
        if deadline > now + GOAL_MAX_S:
            raise ValueError("La hora límite puede ser como mucho dentro de 7 días")
    after = str(then or "normal").strip().lower()
    if after not in GOAL_THEN:
        raise ValueError("Al terminar: usa «normal» (vuelve a sus ajustes) o «pause» (se pausa)")
    return {"target": round(float(t), 2), "until": deadline, "started_at": int(now), "start_equity": round(eq, 2),
            "aggressive": bool(aggressive), "then": after}


def load_goal(raw: Any) -> dict[str, Any] | None:
    """A saved challenge (meta) or None when there is none or it is unreadable."""
    if not isinstance(raw, dict):
        return None
    target, start, started = (safe_float(raw.get(k)) for k in ("target", "start_equity", "started_at"))
    if target is None or start is None or started is None or target <= 0:
        return None
    until = safe_float(raw.get("until"))
    then = raw.get("then") if raw.get("then") in GOAL_THEN else "normal"
    return {"target": float(target), "until": int(until) if until else None, "started_at": int(started),
            "start_equity": float(start), "aggressive": bool(raw.get("aggressive")), "then": then}


def goal_view(goal: dict[str, Any], equity: float, now: int) -> dict[str, Any]:
    """AutopilotGoal: the running challenge and how far it has come (from its start to its target)."""
    span = goal["target"] - goal["start_equity"]
    done = (equity - goal["start_equity"]) / span * 100.0 if span > 0 else 0.0
    until = goal["until"]
    return {
        "target": goal["target"],
        "until": until,
        "started_at": goal["started_at"],
        "start_equity": goal["start_equity"],
        "aggressive": goal["aggressive"],
        "then": goal["then"],
        "equity": round(float(equity), 2),
        "pnl": round(float(equity) - goal["start_equity"], 2),
        "remaining": round(max(goal["target"] - float(equity), 0.0), 2),
        "progress_pct": round(min(max(done, 0.0), 100.0), 2),
        "seconds_left": max(int(until) - int(now), 0) if until else None,
        "mode_text": AGGRESSIVE_TEXT if goal["aggressive"] else None,
        "note": GOAL_NOTE,
    }


def goal_result(goal: dict[str, Any], outcome: str, *, equity: float, now: int, closed: int,
                trades: list[dict[str, Any]], late: bool = False) -> dict[str, Any]:
    """AutopilotGoalResult: how a challenge ended. `trades`: the account's trades closed since it started (not its
    portfolio), the ones closed at the end included."""
    pnl = float(equity) - goal["start_equity"]
    wins = sum(1 for t in trades if (safe_float(t.get("pnl")) or 0.0) > 0)
    out = {
        "outcome": outcome,
        "target": goal["target"],
        "until": goal["until"],
        "started_at": goal["started_at"],
        "ended_at": int(now),
        "start_equity": goal["start_equity"],
        "end_equity": round(float(equity), 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / goal["start_equity"] * 100.0, 2) if goal["start_equity"] > 0 else 0.0,
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "closed": int(closed),
        "aggressive": goal["aggressive"],
        "late": bool(late),
    }
    out["text"] = result_text(out)
    return out


def result_text(r: dict[str, Any]) -> str:
    """One Spanish line for the decision log, the events and the page."""
    head = OUTCOME_LABEL.get(r["outcome"], "Reto terminado")
    sign = "+" if r["pnl"] >= 0 else "−"
    money = f"{sign}{fmt_num(abs(r['pnl']), 2)} ({sign}{fmt_num(abs(r['pnl_pct']), 2)} %)"
    n = r["trades"]
    record = (f"{r['wins']} de {n} operaciones ganadoras" if n else "sin operaciones cerradas")
    closed = r["closed"]
    shut = ("" if not closed else f" · cerró {closed} {'operación abierta' if closed == 1 else 'operaciones abiertas'}"
            " al terminar")
    late = " · el servidor estaba apagado a la hora límite: terminó al volver" if r.get("late") else ""
    return (f"{head}: meta {fmt_num(r['target'], 2)}, de {fmt_num(r['start_equity'], 2)} a "
            f"{fmt_num(r['end_equity'], 2)} {money} · {record}{shut}{late}")
