"""The trading calendar of NEXUS automática (bitácora por día): every closed trade of its accounts (10k = 3 and the
small / challenge accounts) in a time range, with why it opened, how it closed and, in plain Spanish, why it won or
lost (app.sim.journal's cause plus its excursions in R). Pure helpers; the engine side is PaperEngine.calendar.

The UI groups the trades by the viewer's local day (a day is a local calendar day, the server only filters by time)."""
from __future__ import annotations

import json
from typing import Any

from app.sim import journal as J
from app.sim.common import fmt_num
from app.util import safe_float

MAX_TRADES = 3000
NOTE = ("Bitácora de las operaciones cerradas de NEXUS automática por día. La causa sale de cómo terminó cada una (su "
        "resultado en R, su salida y cuánto llegó a ir a favor o en contra). Una racha corta no dice si hay ventaja: "
        "la validación no la encontró. Simulado; no es asesoría financiera.")


def _r(v: float | None) -> str:
    return f"{fmt_num(v, 2)}R" if v is not None else "?"


def lesson(cause: str, r: float | None, mae: float | None, mfe: float | None, why_exit: str | None) -> str:
    """One sentence, in Spanish, on why the trade ended as it did."""
    fav = f"llegó a ir {_r(mfe)} a favor" if mfe is not None else "sin datos de cuánto fue a favor"
    con = f"y {_r(mae)} en contra" if mae is not None else ""
    res = _r(r)
    if cause == "en_contra":
        return (f"El precio se fue en su contra casi desde la entrada ({fav}) y tocó el stop: {res}. La lectura de "
                "entrada no se cumplió.")
    if cause == "stop_tras_favor":
        return (f"Empezó bien ({fav}) pero se dio la vuelta antes de un objetivo y tocó el stop: {res}. Un objetivo "
                "más cercano o subir el stop antes la habría salvado, a costa de cortar otras ganadoras.")
    if cause == "devolvio":
        return (f"Ganaba más de 1R ({fav}) y lo devolvió todo: {res}. El stop no acompañó la ganancia a tiempo.")
    if cause in ("salida_nexus_perdida", "salida_estrategia_perdida"):
        who = "NEXUS" if cause == "salida_nexus_perdida" else "Su estrategia"
        return f"{who} la cerró antes del stop porque su señal cambió ({fav} {con}): {res}. Cortó la pérdida antes."
    if cause == "manual":
        return f"Cerrada a mano ({fav} {con}): {res}.".replace("  ", " ")
    if cause == "vencimiento":
        return f"La opción venció: {res}."
    if cause == "objetivo":
        return f"Llegó a su objetivo ({fav}): {res}."
    if cause == "parcial_equilibrio":
        return f"Cobró un objetivo parcial y el resto salió en equilibrio: {res}."
    if cause == "stop_dinamico":
        return f"El stop dinámico aseguró la ganancia tras ir {_r(mfe)} a favor: {res}."
    if cause in ("salida_nexus_ganancia", "salida_estrategia_ganancia"):
        who = "NEXUS" if cause == "salida_nexus_ganancia" else "Su estrategia"
        return f"{who} la cerró con ganancia cuando su señal cambió ({fav}): {res}."
    return f"Salida: {why_exit or 'otra'} · {res}."


def entry(row: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    """CalendarTrade from a closed paper_positions row."""
    try:
        meta = json.loads(row.get("meta") or "{}") or {}
    except (TypeError, ValueError):
        meta = {}
    ctx = meta.get("context") or {}
    jr = meta.get("journal") or {}
    r = safe_float(row.get("r_multiple"))
    reason = str(row.get("exit_reason") or "").upper() or None
    mfe, mae = safe_float(jr.get("mfe_r")), safe_float(jr.get("mae_r"))
    cause = jr.get("cause") if jr.get("cause") in J.CAUSE_LABEL else J.cause_of(r, reason, mfe)
    src = str(ctx.get("source") or "")
    if src.startswith("script:") and cause.startswith("salida_nexus_"):
        cause = cause.replace("salida_nexus_", "salida_estrategia_")
    strategy = src.split(":", 2)[2] if src.startswith("script:") and src.count(":") >= 2 else None
    aid = int(row["account_id"])
    pnl = round(float(safe_float(row.get("pnl")) or 0.0), 2)
    return {
        "id": int(row["id"]),
        "account_id": aid,
        "account": names.get(aid) or f"Cuenta {aid}",
        "symbol": row["symbol"],
        "tf": row.get("tf") or "1h",
        "side": row["side"],
        "qty": float(safe_float(row.get("qty")) or 0.0),
        "entry_time": int(row["entry_time"]),
        "exit_time": int(row.get("exit_time") or row["entry_time"]),
        "entry_price": safe_float(row.get("entry_price")),
        "exit_price": safe_float(row.get("exit_price")),
        "pnl": pnl,
        "r": round(r, 3) if r is not None else None,
        "result": "win" if pnl > 0 else "loss" if pnl < 0 else "flat",
        "exit_reason": reason,
        "exit_label": meta.get("exit_label"),
        "cause": cause,
        "cause_label": J.CAUSE_LABEL.get(cause, cause),
        "mfe_r": mfe,
        "mae_r": mae,
        "held_s": safe_float(jr.get("held_s")),
        "why": ctx.get("why"),
        "strategy": strategy,
        "nexus_read": ctx.get("nexus_read"),
        "gate_line": ctx.get("gate_line"),
        "regime": ctx.get("regime"),
        "lesson": lesson(cause, r, mae, mfe, meta.get("exit_label") or reason),
    }
