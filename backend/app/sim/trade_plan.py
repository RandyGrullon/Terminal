"""The plan of each trade of NEXUS automática (account 3 and its challenge accounts): why it opened, and its target or,
when it has none, why not. Every trade carries one or the other (PositionPlan in Position.plan).

- `why`: written when the trade opens (meta.context.why: NEXUS's signal and the filter, or the PyChart strategy that
  gave the entry, with NEXUS's own read). Trades from before it get one from what their context kept.
- `target`: the next target still pending (tp1, then tp2, tp3 as they are reached), `target_r` its distance in R.
- `no_target`: why there is none. A PyChart strategy without a limit closes on its own rule or at its stop (as in its
  backtest), and a holding of the buy-and-hold portfolio is held until it is sold or hits its emergency stop.

Pure helpers: the engine side lives in app.sim.paper (position_dict, _auto_check).
"""
from __future__ import annotations

from typing import Any

from app.engine import lots as L
from app.engine.fmt import REGIME_LABEL
from app.sim import journal as J
from app.sim.common import fmt_num, fmt_price
from app.util import safe_float

SIDE_WORD = {"long": "largo", "short": "corto"}
ACTION_WORD = {"long": "COMPRAR", "short": "VENDER"}


def strategy_name(source: Any) -> str | None:
    """'script:12:Supertrend…' -> 'Supertrend…' (None for NEXUS's own signal or no source)."""
    src = str(source or "")
    if not src.startswith("script:"):
        return None
    parts = src.split(":", 2)
    return parts[2] if len(parts) > 2 and parts[2] else f"Script {parts[1] if len(parts) > 1 else '?'}"


def entry_why(*, side: str, tf: str, script_name: str | None = None, entry_id: Any = None, gate: str | None = None,
              gate_required: bool = True, nexus_read: str | None = None, option: str | None = None) -> str:
    """Why the trade opened, in Spanish, as written to its context at the entry."""
    if script_name:
        tag = f" «{entry_id}»" if entry_id else ""
        text = f"Señal de entrada{tag} de la estrategia «{script_name}» al cierre de la vela de {tf}"
        if gate:
            text += f"; filtro {gate}" if gate_required else f"; filtro no exigido ({gate})"
        if nexus_read:
            text += f"; NEXUS decía {nexus_read}"
        return text
    text = f"NEXUS dijo {nexus_read or ACTION_WORD.get(side, side)} al cierre de la vela de {tf}"
    if gate:
        text += f" y el filtro dijo {gate}"
    if option:
        text += f"; lo opera con un {option}"
    return text


def _pct(v: Any, digits: int = 0) -> str | None:
    f = safe_float(v)
    return None if f is None else f"{fmt_num(f * 100.0, digits)} %"


def params_of(pos: dict, meta: dict, ctx: dict, risk_unit: float | None, *, strategy: str | None,
              hold: bool) -> list[dict[str, str]]:
    """What the trade took at its entry, as [{label, value}] in Spanish (what its context kept: trades from before a
    field was kept show fewer)."""
    out: list[dict[str, str]] = []

    def add(label: str, value: Any) -> None:
        if value is not None and str(value).strip():
            out.append({"label": label, "value": str(value)})

    tf = str(pos.get("tf") or "1h")
    entry, qty = safe_float(pos.get("entry_price")), safe_float(pos.get("qty"))
    add("Cuenta", ctx.get("account"))
    if hold:
        add("Fuente", "Cartera de comprar y mantener")
    elif strategy:
        eid = ctx.get("entry_id")
        add("Fuente de la señal", f"Estrategia «{strategy}»" + (f" · entrada «{eid}»" if eid else ""))
    else:
        add("Fuente de la señal", "Señal de NEXUS")
    add("Temporalidad", J.tf_label(tf))
    entry_time = safe_float(pos.get("entry_time"))
    if entry_time is not None:
        add("Sesión al entrar", J.SESSION_LABEL.get(J.session_of(int(entry_time))))
    add("Lectura de NEXUS", ctx.get("nexus_read"))
    gate = ctx.get("gate_line")
    if gate is None and pos.get("gate_score") is not None:
        gate = f"{pos.get('gate_verdict') or ''} {fmt_num(safe_float(pos.get('gate_score')), 0)}/100".strip()
    if gate:
        required = ctx.get("gate_required")
        add("Filtro «¿Tomo esta operación?»", gate + ("" if required is None else " (exigido)" if required
                                                      else " (no exigido)"))
    regime = ctx.get("regime")
    add("Régimen del mercado", REGIME_LABEL.get(regime, regime) if regime else None)
    score = safe_float(ctx.get("score"))
    add("Puntuación de NEXUS", fmt_num(score, 2, True) if score is not None else None)
    add("Probabilidad de subida (NEXUS)", _pct(ctx.get("prob_up")))
    add("Probabilidad de ganar (filtro)", _pct(ctx.get("win_prob")))
    atr = safe_float(ctx.get("atr_pct"))
    add("Volatilidad (ATR)", f"{fmt_num(atr, 2)} % del precio" if atr is not None else None)
    if "aggressive" in ctx:
        add("Modo", "agresivo" if ctx.get("aggressive") else "normal")
    risk_pct = safe_float(ctx.get("risk_pct"))
    add("Riesgo por operación", f"{fmt_num(risk_pct, 1)} % del capital" if risk_pct is not None else None)
    add("Entrada", fmt_price(entry) if entry is not None else None)
    initial = safe_float(meta.get("initial_sl")) or safe_float(pos.get("sl"))
    if initial is not None and entry:
        add("Stop inicial", f"{fmt_price(initial)} ({fmt_num(abs(entry - initial) / entry * 100.0, 2)} % de la entrada)")
    if risk_unit and qty:
        add("Pérdida si toca el stop inicial", f"{fmt_num(risk_unit * qty, 2)} $ (antes de comisiones)")
    lot = L.lot_info(str(pos.get("symbol") or ""), qty, meta.get("instrument"))
    inst = meta.get("instrument") if isinstance(meta.get("instrument"), dict) else {}
    if lot is not None:
        unit = L.unit_word(str(lot["unit"]), qty or 0.0)
        add("Tamaño", f"{fmt_num(lot['lots'], 2)} lotes ({fmt_num(qty or 0.0, 3)} {unit})")
    elif qty:
        add("Tamaño", fmt_num(qty, 4))
    if inst.get("perpetual"):
        n = int(inst.get("contracts") or 0)
        margin = (safe_float(inst.get("margin")) or 0.0) * n
        add("Apalancamiento", f"{fmt_num(safe_float(inst.get('leverage')) or 0.0, 0)}x · margen ~{fmt_num(margin, 2)} $ · "
                              f"liquidación ~{fmt_price(inst.get('liquidation'))}")
    swap = safe_float(meta.get("swap"))
    if swap:
        add("Swap acumulado", f"{fmt_num(swap, 2, True)} $ (rollover de las 17:00 de Nueva York; triple el miércoles)")
    tps = [safe_float(pos.get(k)) for k in ("tp1", "tp2", "tp3")]
    shown = [fmt_price(t) for t in tps if t is not None]
    add("Objetivos", " · ".join(shown) if shown else "ninguno")
    if pos.get("trail") is not None:
        add("Stop dinámico", fmt_price(pos.get("trail")))
    return out


def plan_of(pos: dict, meta: dict, risk_unit: float | None) -> dict[str, Any] | None:
    """PositionPlan of one of NEXUS automática's positions (None for any other): {why, target, target_r, no_target,
    params}."""
    if pos.get("origin") != "autopilot":
        return None
    ctx = meta.get("context") if isinstance(meta.get("context"), dict) else {}
    hold = bool(meta.get("hold"))
    side = str(pos.get("side") or "long")
    tf = str(pos.get("tf") or "1h")
    sl = safe_float(pos.get("sl"))
    name = strategy_name(ctx.get("source"))
    why = ctx.get("why") if isinstance(ctx.get("why"), str) and ctx.get("why") else None
    if why is None:  # a trade from before plans were written: from what its context kept
        if hold:
            why = "Compra de su cartera de comprar y mantener"
        elif name:
            why = f"Señal de entrada de la estrategia «{name}» al cierre de la vela de {tf}"
        else:
            score = safe_float(pos.get("gate_score"))
            gate = (f" y el filtro dijo TOMAR {score:.0f}/100" if pos.get("gate_verdict") == "TAKE" and score is not None
                    else "")
            why = f"NEXUS dijo {ACTION_WORD.get(side, side)} al cierre de la vela de {tf}{gate}"
    tps = [safe_float(pos.get(k)) for k in ("tp1", "tp2", "tp3")]
    hits = int(pos.get("tp_hits") or 0)
    pending = [t for t in tps[hits:] if t is not None] or ([t for t in tps if t is not None][:1] if hits == 0 else [])
    target = pending[0] if pending else None
    entry = safe_float(pos.get("entry_price"))
    target_r = (round(abs(target - entry) / risk_unit, 2)
                if target is not None and entry is not None and risk_unit and risk_unit > 0 else None)
    no_target = None
    if target is None:
        stop = f" o en su stop ({fmt_price(sl)})" if sl is not None else ""
        trail = " (su stop la sigue a favor)" if pos.get("trail") is not None else ""
        if hold:
            no_target = ("Es de su cartera de comprar y mantener: no busca un objetivo; se queda hasta que la vendas"
                         f"{stop.replace(' o en su stop', ' o caiga hasta su stop de emergencia')}")
        elif name:
            no_target = (f"La estrategia «{name}» no fija objetivo: la cierra su propia regla de salida (cuando su "
                         f"señal se da la vuelta){stop}{trail}, como en su prueba histórica")
        elif hits >= 3:
            no_target = "Ya alcanzó sus tres objetivos"
        elif hits > 0:
            no_target = (f"Ya cobró {hits} {'objetivo' if hits == 1 else 'objetivos'}: el resto sigue con su stop "
                         f"dinámico{stop}")
        else:
            no_target = (f"NEXUS no dio objetivos en esa vela: la cierra con sus reglas de salida (salida anticipada o "
                         f"señal contraria){stop}")
    return {"why": why, "target": target, "target_r": target_r, "no_target": no_target,
            "params": params_of(pos, meta, ctx, risk_unit, strategy=name, hold=hold)}
