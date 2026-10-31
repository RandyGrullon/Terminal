"""Merge the market-conditions check (rt.macro, the "semáforo") into live NEXUS decisions.

The pure engine never sees live macro data (it is not part of the causal price history), so the service layers it
on top of the current decision:
- red (no-trade window around a high-impact event, extreme volatility): the gate says NO TOMAR and BUY/SELL
  become ESPERAR; open positions get a critical warning (they are not force-closed);
- amber: the gate score drops 15 % (a TAKE below the take threshold becomes WAIT) and a warning is added;
- green: the passed check is listed in the checklist ("Calendario y volatilidad ✓").
A vetoed BUY/SELL must not leave a position behind: the service remembers the veto and re-runs the engine with it
(`run_nexus(..., vetoed_entries=...)`), which rejects that candidate through `veto_gate` instead of opening it.
"""
from __future__ import annotations

import copy
from typing import Any

AMBER_FACTOR = 0.85
RED_MAX_SCORE = 25.0
MACRO_LABEL = "Calendario y volatilidad"


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


def vetoed_side(cur: dict | None, checks: dict[str, dict | None], take_score: float) -> tuple[str, dict] | None:
    """(direction, macro check) when the semáforo vetoes the BUY/SELL of `cur`: red, or amber pushing the
    gate of that side out of TAKE. None otherwise (no entry, no macro data, green)."""
    if not cur or cur.get("action") not in ("BUY", "SELL"):
        return None
    side = "long" if cur["action"] == "BUY" else "short"
    check = checks.get(side) or checks.get("long") or checks.get("short")
    if not check or check.get("passed") is not False:
        return None
    if check.get("critical"):
        return side, check
    merged = merge_gate((cur.get("gate") or {}).get(side), check, take_score)
    return (side, check) if (merged or {}).get("verdict") != "TAKE" else None


def veto_gate(gate: dict, veto: dict, take_score: float) -> dict:
    """GateResult of a candidate the semáforo vetoed when it was live (`veto` = {"level", "detail"} as stored by
    the service): the macro check is merged in and the verdict is never TAKE."""
    red = veto.get("level") == "red"
    detail = str(veto.get("detail") or "Condiciones de mercado adversas")
    check = {"id": "macro", "label": MACRO_LABEL, "passed": False, "critical": red, "weight": 1.0,
             "value": "red" if red else "amber", "detail": detail}
    g = merge_gate(gate, check, take_score) or copy.deepcopy(gate)
    if g.get("verdict") == "TAKE":  # amber that no longer pulls this re-evaluated score below the threshold
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
    vetoed = vetoed_side(cur, checks, take_score)
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
    if vetoed is not None:
        # The service re-runs the engine with the veto, so normally no BUY/SELL reaches this point; this keeps the
        # display safe anyway (no "Comprar" advice and no position opened by the vetoed entry).
        c["action"], c["action_label"] = "WAIT", "ESPERAR"
        c["advice"] = (f"No abras posiciones ahora: {detail}" if red else f"Espera: {detail}")
        pos = c.get("position")
        if isinstance(pos, dict) and pos.get("entry_time") == c.get("time"):
            c["position"] = None
    elif red and action in ("HOLD_LONG", "HOLD_SHORT"):
        base = str(c.get("advice") or "").rstrip(". ")
        c["advice"] = f"{base}. Atención: {detail} Protege la posición con el stop o reduce el tamaño.".lstrip(". ")
    return c
