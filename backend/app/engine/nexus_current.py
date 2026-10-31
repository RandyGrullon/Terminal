"""`current` of a NEXUS run: position state, warnings, the one-line Spanish advice, contributions and reasons."""
from __future__ import annotations

from typing import Any

from app.engine import fmt
from app.engine.experts import EXPERT_IDS, EXPERT_LABEL
from app.engine.features import Features
from app.engine.regime import TREND_DOWN, TREND_UP, VOLATILE, regime_detail, regime_label, regime_name

SIDE_WORD = {1: "largo", -1: "corto"}
_SEVERITY_RANK = {"critical": 0, "warn": 1, "info": 2}
EARLY_EV = -0.10  # EV of holding (R) at or below which the engine exits early when the score is against


def risk_flags(f: Features, i: int, side: int, pos: Any, regime_code: int) -> set[str]:
    """Risk conditions of an open position at the close of bar i."""
    flags: set[str] = set()
    if (side > 0 and bool(f.div_bear[i])) or (side < 0 and bool(f.div_bull[i])):
        flags.add("DIVERGENCE")
    if f.value("range_atr", i, 0.0) > 2.5 and side * (f.close[i] - f.open[i]) < 0:
        flags.add("VOL_SPIKE")
    adverse = (TREND_DOWN, VOLATILE) if side > 0 else (TREND_UP, VOLATILE)
    if regime_code != pos.entry_regime and regime_code in adverse:
        flags.add("REGIME_CHANGE")
    htf = f.value("htf_trend", i)
    if htf == htf and side * htf <= -0.3:
        flags.add("COUNTER_TREND")
    if pos.risk > 0 and side * (f.close[i] - pos.stop) / pos.risk <= 0.3:
        flags.add("NEAR_SL")
    return flags


def _w(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def position_state(run: Any, i: int) -> tuple[dict | None, float | None]:
    """PositionState of the open position at bar i and its ev_hold_r."""
    from app.engine.levels import ev_hold

    pos = run.pos
    if pos is None:
        return None, None
    f = run.f
    price = float(f.close[i])
    p_up = float(run.prob[i])
    p_dir = p_up if pos.side > 0 else 1.0 - p_up
    ev, _ = ev_hold(pos.side, price, pos.stop, pos.next_tp, pos.risk, p_dir, f.value("sigma", i, 0.0), run.h)
    _, r_mult = pos.result(price, run.p.fee_pct)
    state = {
        "side": "long" if pos.side > 0 else "short",
        "entry_price": pos.entry_price,
        "entry_time": pos.entry_time,
        "sl": pos.stop,
        "tp1": pos.tps[0],
        "tp2": pos.tps[1],
        "tp3": pos.tps[2],
        "trail": pos.trail,
        "tp_hits": int(pos.tp_hits),
        "size_left": round(pos.size_left, 4),
        "unrealized_pct": round(pos.side * (price / pos.entry_price - 1.0) * 100.0, 4),
        "r_multiple": round(r_mult, 4),
        "bars_in_trade": int(pos.bars),
        "ev_hold_r": round(ev, 4),
    }
    return state, ev


def build_warnings(run: Any, i: int, ev: float | None) -> list[dict]:
    f, p, pos = run.f, run.p, run.pos
    out: list[dict] = []
    updates = run.learner.updates
    bars = run.n - int(getattr(run, "out_start", 0))  # the bars shown (not the extra ones a resumed run needs)
    if bars < 300 or updates < 50:
        out.append(_w("LOW_DATA", "info", f"Pocos datos: {bars} velas y {updates} actualizaciones del modelo; "
                                          "trata las señales con cautela"))
    exit_info = run.bar_info.get("exit")
    if exit_info is not None and exit_info.get("reason") == "EARLY" and not exit_info.get("at_open"):
        out.append(_w("EARLY_EXIT", "critical", exit_info.get("detail") or "Salida anticipada recomendada"))
    range_atr = f.value("range_atr", i, 0.0)
    if pos is None:
        if range_atr > 2.5:
            out.append(_w("VOL_SPIKE", "warn", f"Vela de {fmt.num(range_atr, 1)} ATR: volatilidad anómala, "
                                               "evita entrar ahora"))
        if bool(f.div_bear[i]):
            out.append(_w("DIVERGENCE", "info", "Divergencia bajista del RSI: cuidado con nuevas compras"))
        elif bool(f.div_bull[i]):
            out.append(_w("DIVERGENCE", "info", "Divergencia alcista del RSI: cuidado con nuevas ventas"))
        return out
    side = pos.side
    word = SIDE_WORD[side]
    flags = risk_flags(f, i, side, pos, int(run.regime[i]))
    if ev is not None and ev < 0 and run.action[i] not in ("BUY", "SELL"):
        # Still holding: the engine's exit rule did not fire (it exits EARLY only when the EV is <= −0.10R with
        # the score against, see nexus_decide). A negative EV is an amber "vigila la salida", never a close
        # instruction: EARLY_EXIT (critical) is reserved for an actual EXIT_LONG / EXIT_SHORT. (Not shown next to
        # the entry the engine has just decided at this close.)
        why = ", aunque el score NEXUS aún no gira en contra" if ev <= EARLY_EV else ""
        out.append(_w("WATCH_EXIT", "warn",
                      f"Vigila la salida: el valor esperado de mantener es negativo ({fmt.rr(ev, 2)}){why}"))
    if "NEAR_SL" in flags:
        dist_r = side * (f.close[i] - pos.stop) / pos.risk
        out.append(_w("NEAR_SL", "warn", f"Precio a {fmt.num(max(dist_r, 0.0), 1)}R de tu stop ({fmt.price(pos.stop)})"))
    if "DIVERGENCE" in flags:
        kind = "bajista" if side > 0 else "alcista"
        out.append(_w("DIVERGENCE", "warn", f"Divergencia {kind} del RSI en contra de tu {word}"))
    if "VOL_SPIKE" in flags:
        out.append(_w("VOL_SPIKE", "critical", f"Vela de {fmt.num(range_atr, 1)} ATR en contra de tu {word}"))
    if pos.tp_hits == 0 and pos.bars >= 0.75 * p.time_stop_bars:
        out.append(_w("TIME_STOP", "warn", f"La operación lleva {pos.bars} velas sin alcanzar TP1 "
                                           f"(cierre por tiempo a las {p.time_stop_bars})"))
    if "COUNTER_TREND" in flags:
        out.append(_w("COUNTER_TREND", "warn", f"Tu {word} va contra la tendencia {f.htf_label} "
                                               f"({fmt.num(f.value('htf_trend', i), 2, True)})"))
    if "REGIME_CHANGE" in flags:
        out.append(_w("REGIME_CHANGE", "warn",
                      f"El régimen cambió a {regime_label(regime_name(int(run.regime[i])))} desde la entrada"))
    out.sort(key=lambda w: _SEVERITY_RANK.get(w["severity"], 3))
    return out


def _pct_to(price: float, level: float) -> str:
    return fmt.pct((level / price - 1.0) * 100.0, 1) if price else "—"


def build_advice(run: Any, i: int, action: str, gates: dict, warnings: list[dict]) -> str:
    f, p, pos = run.f, run.p, run.pos
    price = float(f.close[i])
    info = run.bar_info
    if action in ("BUY", "SELL"):
        g = info.get("entry") or gates["long" if action == "BUY" else "short"]
        verb = "Comprar" if action == "BUY" else "Vender en corto"
        ex = info.get("exit") or {}
        if ex.get("reason") == "REVERSE" and not ex.get("at_open"):
            verb = "Cerrar el corto y comprar" if action == "BUY" else "Cerrar el largo y vender en corto"
        filt = f"filtro {g['verdict_label']} {fmt.num(g['score'], 0)}/100"
        if g.get("verdict") != "TAKE":
            filt = f"filtro desactivado ({g['verdict_label']} {fmt.num(g['score'], 0)}/100)"
        return (f"{verb}: {filt}, stop {fmt.price(g['sl'])}, objetivos {fmt.price(g['tp1'])} / "
                f"{fmt.price(g['tp2'])} / {fmt.price(g['tp3'])}.")
    if action in ("HOLD_LONG", "HOLD_SHORT") and pos is not None:
        word = SIDE_WORD[pos.side]
        stop_txt = f"{fmt.price(pos.stop)} ({_pct_to(price, pos.stop)})"
        serious = [w for w in warnings if w["severity"] == "critical" and w["code"] != "LOW_DATA"]
        cautious = [w for w in warnings if w["severity"] == "warn"]
        if serious:
            msg = fmt.lower_first(serious[0]["message"])
            return f"Mantener el {word} con cautela: {msg}; tu stop está en {stop_txt}."
        if pos.tp_hits >= 2:
            return f"Mantener el {word}: stop dinámico en {stop_txt}; objetivo final {fmt.price(pos.tps[2])}."
        if pos.tp_hits == 1:
            return f"Mantener el {word}: TP1 asegurado y stop en punto de equilibrio {stop_txt}."
        if cautious:
            msg = cautious[0]["message"]
            if cautious[0]["code"] == "WATCH_EXIT":
                return f"Mantener el {word} y vigilar la salida: {msg.split(': ', 1)[-1]}; tu stop está en {stop_txt}."
            return f"Mantener el {word} con cautela: {fmt.lower_first(msg)}; tu stop está en {stop_txt}."
        return f"Mantener el {word}: tendencia intacta; tu stop está en {stop_txt}."
    if action in ("EXIT_LONG", "EXIT_SHORT"):
        ex = info.get("exit") or info.get("exit_decision") or {}
        reason = ex.get("reason")
        r_txt = f" ({fmt.rr(ex.get('r'))})" if ex.get("r") is not None else ""
        px = fmt.price(ex.get("price", price))
        if reason == "EARLY":
            ev = info.get("ev_hold")
            ev_txt = f" ({fmt.rr(ev)})" if ev is not None else ""
            why = str(ex.get("detail") or "")
            if "valor esperado" in why or not why:
                return f"Cerrar ahora: el valor esperado de mantener es negativo{ev_txt}."
            return f"Cerrar ahora: {why.split(': ', 1)[-1]}."
        if reason == "SL":
            if ex.get("label") == fmt.EXIT_LABEL["BE"]:
                return f"Cerrar: el stop en punto de equilibrio se activó en {px}{r_txt}."
            return f"Cerrar: el stop se alcanzó en {px}{r_txt}."
        if reason == "TRAIL":
            return f"Cerrar: el stop dinámico se activó en {px}{r_txt}."
        if reason == "TP3":
            return f"Objetivo final alcanzado en {px}: operación cerrada{r_txt}."
        if reason == "TIME":
            return f"Cerrar: la operación agotó su tiempo sin alcanzar el objetivo 1{r_txt}."
        if reason == "SIGNAL":
            return f"Cerrar ahora: el score NEXUS giró en contra ({fmt.num(run.score[i], 2, True)})."
        return f"Cerrar la posición: {ex.get('detail') or 'señal de salida'}."
    rej = info.get("rejected")
    if rej is not None:
        side_txt = "compra" if rej["direction"] == "long" else "venta"
        reason = fmt.lower_first((rej.get("reasons_against") or [rej.get("summary") or ""])[0]).rstrip(". ")
        if rej["verdict"] == "WAIT":
            return (f"Esperar: señal de {side_txt} pendiente de confirmación (filtro ESPERAR "
                    f"{fmt.num(rej['score'], 0)}/100): {reason}.")
        return f"No operar: el filtro rechaza la {side_txt} (NO TOMAR {fmt.num(rej['score'], 0)}/100): {reason}."
    reg = int(run.regime[i])
    score = float(run.score[i])
    if reg == VOLATILE:
        return f"No operar: volatilidad extrema (ATR en el percentil {fmt.num(f.value('atr_rank', i, 0.0) * 100, 0)})."
    if regime_name(reg) == "range" and abs(score) < p.entry_threshold:
        return "No operar: mercado lateral y sin ventaja estadística."
    return (f"Esperar: sin señal clara (score {fmt.num(score, 2, True)}; se necesita ±"
            f"{fmt.num(p.entry_threshold, 2)}).")


def contributions(run: Any, i: int) -> list[dict]:
    r = int(run.regime[i])
    w = run.learner.weights_for(r)
    sig = run.S[i]
    out = [{"expert": eid,
            "label": EXPERT_LABEL[eid] + (" (contrario)" if w[k] < 0 else ""),
            "signal": round(float(sig[k]), 4),
            "weight": round(abs(float(w[k])), 4),
            "contribution": round(float(sig[k] * w[k]), 4)}
           for k, eid in enumerate(EXPERT_IDS)]
    out.sort(key=lambda c: -abs(c["contribution"]))
    return out


def build_reasons(run: Any, i: int, contribs: list[dict]) -> list[str]:
    f, learner = run.f, run.learner
    reg = regime_name(int(run.regime[i]))
    score = float(run.score[i])
    out = [f"Régimen: {regime_label(reg)} ({regime_detail(f, i)})"]
    hit = learner.hit_rate
    hit_txt = f"; acierto {fmt.prob(hit)} en {learner.n_eval} predicciones" if hit is not None else ""
    out.append(f"Probabilidad de subida {fmt.prob(run.prob[i])} (modelo con {learner.updates} actualizaciones{hit_txt})")
    out.append(f"Score NEXUS {fmt.num(score, 2, True)} (umbral ±{fmt.num(run.p.entry_threshold, 2)}) · "
               f"confianza {fmt.prob(run.conf[i])}")
    sign = 1.0 if score >= 0 else -1.0
    pro = [c["label"] for c in contribs if sign * c["contribution"] > 0.005][:3]
    con = [c["label"] for c in contribs if sign * c["contribution"] < -0.005][:3]
    if pro:
        out.append(("A favor de subir: " if sign > 0 else "A favor de bajar: ") + ", ".join(pro))
    if con:
        out.append("En contra: " + ", ".join(con))
    htf = f.value("htf_trend", i)
    if htf == htf:
        word = "alcista" if htf > 0.15 else "bajista" if htf < -0.15 else "plana"
        out.append(f"Tendencia {f.htf_label}: {word} ({fmt.num(htf, 2, True)})")
    return out
