"""Merge the market-conditions check (rt.macro, the "semáforo") into live NEXUS decisions.

The pure engine never sees macro data (it is not part of the causal price history), so the service layers it on
top of the current decision:
- red (no-trade window around a high-impact event, extreme volatility): the gate says NO TOMAR and BUY/SELL
  become ESPERAR; open positions get a critical warning (they are not force-closed);
- amber: the gate score drops 15 % (a TAKE below the take threshold becomes WAIT) and a warning is added;
- green: the passed check is listed in the checklist ("Calendario y volatilidad ✓").
"""
from __future__ import annotations

import copy
from typing import Any

AMBER_FACTOR = 0.85
RED_MAX_SCORE = 25.0


def merge_gate(gate: dict | None, check: dict | None, take_score: float) -> dict | None:
    """GateResult + macro GateCheck -> new GateResult (inputs are not modified)."""
    if not gate or not check:
        return gate
    g = copy.deepcopy(gate)
    g["checks"] = [dict(check)] + [c for c in g.get("checks") or [] if c.get("id") != "macro"]
    if check.get("passed") is not False:
        return g
    detail = str(check.get("detail") or "Condiciones de mercado adversas")
    g["reasons_against"] = ([detail] + [r for r in g.get("reasons_against") or [] if r != detail])[:6]
    score = float(g.get("score") or 0.0)
    if check.get("critical"):
        g["score"] = round(min(score, RED_MAX_SCORE), 1)
        g["verdict"], g["verdict_label"] = "AVOID", "NO TOMAR"
        g["summary"] = f"NO TOMAR: {detail}"
    else:
        g["score"] = round(score * AMBER_FACTOR, 1)
        if g.get("verdict") == "TAKE" and g["score"] < take_score:
            g["verdict"], g["verdict_label"] = "WAIT", "ESPERAR"
            g["summary"] = f"ESPERAR: {detail}"
    return g


def merge_current(cur: dict | None, checks: dict[str, dict | None], take_score: float) -> dict | None:
    """NexusCurrent + {"long": GateCheck, "short": GateCheck} -> new NexusCurrent."""
    if not cur:
        return cur
    check = checks.get("long") or checks.get("short")
    if not check:
        return cur
    c: dict[str, Any] = copy.deepcopy(cur)
    gate = c.get("gate") or {}
    for side in ("long", "short"):
        if gate.get(side):
            gate[side] = merge_gate(gate[side], checks.get(side) or check, take_score)
    c["gate"] = gate
    if check.get("passed") is not False:
        return c
    red = bool(check.get("critical"))
    detail = str(check.get("detail") or "Condiciones de mercado adversas")
    warnings = [w for w in c.get("warnings") or [] if w.get("code") != "MACRO"]
    warnings.insert(0, {"code": "MACRO", "severity": "critical" if red else "warn", "message": f"Mercado: {detail}"})
    c["warnings"] = warnings
    action = c.get("action")
    if action in ("BUY", "SELL"):
        side = "long" if action == "BUY" else "short"
        if red or (gate.get(side) or {}).get("verdict") != "TAKE":
            c["action"], c["action_label"] = "WAIT", "ESPERAR"
            c["advice"] = (f"No abras posiciones ahora: {detail}" if red else f"Espera: {detail}")
    elif red and action in ("HOLD_LONG", "HOLD_SHORT"):
        base = str(c.get("advice") or "").rstrip(". ")
        c["advice"] = f"{base}. Atención: {detail} Protege la posición con el stop o reduce el tamaño.".lstrip(". ")
    return c
