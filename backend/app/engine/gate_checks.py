"""The individual Trade Gate checks (§8): each returns pass / fail / neutral with a concrete Spanish detail."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.engine import fmt
from app.engine.levels import Levels, PlanEV

# id, label, base weight (prior importance before learning), wait-able failure
CHECK_DEFS: list[tuple[str, str, float, bool]] = [
    ("trend_alignment", "Tendencia mayor a favor", 0.7, False),
    ("regime", "Mercado con dirección (no lateral)", 0.6, False),
    ("momentum", "Momento a favor (MACD y RSI)", 0.5, True),
    ("room_to_target", "Espacio hasta el objetivo", 0.6, False),
    ("risk_reward", "Riesgo/beneficio hasta TP2", 0.8, False),
    ("overextension", "Precio no sobreextendido", 0.5, True),
    ("volatility", "Volatilidad normal", 0.5, False),
    ("volume", "Volumen suficiente", 0.3, True),
    ("probability", "Probabilidad aprendida a favor", 0.8, False),
    ("edge_history", "Historial de señales similares", 0.6, False),
    ("expected_value", "Valor esperado positivo", 0.9, False),
    ("recent_performance", "Sin racha de pérdidas", 0.4, False),
    ("confluence", "Confluencia de expertos", 0.6, False),
]
CHECK_IDS: list[str] = [c[0] for c in CHECK_DEFS]
CHECK_LABEL: dict[str, str] = {c[0]: c[1] for c in CHECK_DEFS}
BASE_WEIGHT: dict[str, float] = {c[0]: c[2] for c in CHECK_DEFS}
WAITABLE: set[str] = {c[0] for c in CHECK_DEFS if c[3]}
EDGE_MIN_N = 8
LOSS_STREAK_LIMIT = 4
RR_CRITICAL = 1.2  # R:R to TP2 below which the plan's own stop/target geometry is poor (critical failure)


def fee_text(fee_pct: float) -> str:
    """Commission per side for messages: 0.1 -> '0,1 %', 0.005 -> '0,005 %'."""
    body = f"{max(float(fee_pct), 0.0):.4f}".rstrip("0").rstrip(".") or "0"
    return f"{body.replace('.', ',')} %"


@dataclass
class GateContext:
    """Everything known at the close of the decision bar."""
    time: int
    close: float
    atr: float
    sigma: float
    ema_slow: float
    rsi: float
    macd_hist: float
    adx: float
    er: float
    atr_rank: float
    shock: float
    vol_z: float
    htf_trend: float
    htf_label: str
    prob_up: float
    score: float
    regime: str
    swing_low: float | None = None
    swing_high: float | None = None
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)
    expert_signals: list[float] = field(default_factory=list)
    expert_weights: list[float] = field(default_factory=list)
    edge: dict[str, tuple[int, int]] = field(default_factory=dict)  # direction -> (wins, n)
    edge_scope: dict[str, str] = field(default_factory=dict)  # direction -> "en este régimen" | ""
    loss_streak: int = 0
    closed_trades: int = 0
    prob_skill: float = 0.5  # 0..1: how much the probability model has proven (Brier vs a coin)


@dataclass
class CheckOut:
    id: str
    passed: bool | None
    critical: bool
    value: float | str | None
    detail: str


def _ok(x: float) -> bool:
    return x == x


def run_checks(ctx: GateContext, side: int, lv: Levels, plan: PlanEV, p_dir: float, min_rr: float,
               min_prob: float) -> list[CheckOut]:
    atr = ctx.atr if _ok(ctx.atr) and ctx.atr > 0 else abs(ctx.close) * 0.01
    word_up = "alcista" if side > 0 else "bajista"
    out: list[CheckOut] = []

    # 1. higher-timeframe trend
    htf = ctx.htf_trend
    if not _ok(htf):
        out.append(CheckOut("trend_alignment", None, False, None,
                            f"Sin datos suficientes de la temporalidad mayor ({ctx.htf_label})"))
    else:
        tw = "alcista" if htf > 0 else "bajista"
        val = round(htf, 3)
        if side * htf >= 0.15:
            out.append(CheckOut("trend_alignment", True, False, val,
                                f"Tendencia {ctx.htf_label} {tw} ({fmt.num(htf, 2, True)}) a favor"))
        elif side * htf <= -0.15:
            out.append(CheckOut("trend_alignment", False, False, val,
                                f"Tendencia {ctx.htf_label} {tw} ({fmt.num(htf, 2, True)}): operarías en contra"))
        else:
            out.append(CheckOut("trend_alignment", None, False, val,
                                f"Tendencia {ctx.htf_label} plana ({fmt.num(htf, 2, True)})"))

    # 2. regime / choppiness
    er = ctx.er if _ok(ctx.er) else 0.0
    adx = ctx.adx if _ok(ctx.adx) else 0.0
    choppy = er < 0.25 and adx < 18.0
    rlabel = fmt.REGIME_LABEL.get(ctx.regime, ctx.regime)
    if not choppy:
        out.append(CheckOut("regime", True, False, ctx.regime,
                            f"{rlabel}: eficiencia {fmt.num(er, 2)} · ADX {fmt.num(adx, 0)}"))
    else:
        critical = ctx.regime == "volatile"
        what = "Volátil y sin dirección" if critical else "Mercado lateral"
        out.append(CheckOut("regime", False, critical, ctx.regime,
                            f"{what}: eficiencia {fmt.num(er, 2)} y ADX {fmt.num(adx, 0)}"))

    # 3. momentum
    if not (_ok(ctx.macd_hist) and _ok(ctx.rsi)):
        out.append(CheckOut("momentum", None, False, None, "Momento sin datos suficientes"))
    else:
        macd_ok = side * ctx.macd_hist > 0
        rsi_ok = side * (ctx.rsi - 50.0) > 0
        macd_txt = f"MACD {'a favor' if macd_ok else 'en contra'} ({fmt.num(ctx.macd_hist / atr, 2, True)} ATR)"
        rsi_txt = f"RSI {fmt.num(ctx.rsi, 0)}"
        if macd_ok and rsi_ok:
            out.append(CheckOut("momentum", True, False, round(ctx.rsi, 1), f"{macd_txt} y {rsi_txt} {word_up}"))
        else:
            out.append(CheckOut("momentum", False, False, round(ctx.rsi, 1), f"Momento dividido: {macd_txt} · {rsi_txt}"))

    # 4. room to the nearest opposing level
    risk = max(lv.risk, 1e-12)
    opposing = [p for p in (ctx.resistances if side > 0 else ctx.supports) if side * (p - lv.entry) > 0]
    word_lvl = "Resistencia" if side > 0 else "Soporte"
    if not opposing:
        what = "resistencias cercanas" if side > 0 else "soportes cercanos"
        out.append(CheckOut("room_to_target", True, False, None, f"Sin {what} que frenen el movimiento"))
    else:
        nearest = min(opposing, key=lambda p: abs(p - lv.entry))
        dist = abs(nearest - lv.entry)
        room_r = dist / risk
        txt = f"{word_lvl} en {fmt.price(nearest)} a {fmt.num(dist / atr, 1)} ATR ({fmt.num(room_r, 1)}R)"
        out.append(CheckOut("room_to_target", room_r >= 1.5, False, round(room_r, 3), txt))

    # 5. risk / reward to TP2 after fees. Critical only when the stop/target geometry itself is poor: when the
    # commission is what drags the R:R down (a short stop in a quiet market), the check fails without vetoing the
    # trade on its own (the expected-value check still charges the cost).
    rr = plan.rr
    fee = fee_text(plan.fee_pct)
    if rr >= min_rr:
        out.append(CheckOut("risk_reward", True, False, round(rr, 3),
                            f"R:R {fmt.num(rr, 1)} hasta TP2 tras comisiones (comisión {fee} por lado; "
                            f"mínimo {fmt.num(min_rr, 1)})"))
    else:
        fee_driven = plan.fee_pct > 0 and plan.rr_gross >= RR_CRITICAL
        if plan.fee_pct > 0 and plan.rr_gross >= min_rr:
            detail = (f"R:R {fmt.num(rr, 1)} tras comisiones ({fmt.num(plan.rr_gross, 1)} sin ellas): la comisión de "
                      f"{fee} por lado pesa demasiado frente a un stop tan corto (mínimo {fmt.num(min_rr, 1)})")
        else:
            detail = (f"R:R {fmt.num(rr, 1)} tras comisiones (comisión {fee} por lado): insuficiente "
                      f"(mínimo {fmt.num(min_rr, 1)})")
        out.append(CheckOut("risk_reward", False, rr < RR_CRITICAL and not fee_driven, round(rr, 3), detail))

    # 6. over-extension
    if not (_ok(ctx.ema_slow) and _ok(ctx.rsi)):
        out.append(CheckOut("overextension", None, False, None, "Sin datos para medir la extensión"))
    else:
        ext = (ctx.close - ctx.ema_slow) / atr
        exhausted = ctx.rsi > 75.0 if side > 0 else ctx.rsi < 25.0
        if abs(ext) > 2.5:
            where = "sobre" if ext > 0 else "bajo"
            out.append(CheckOut("overextension", False, False, round(ext, 2),
                                f"Sobreextendido: {fmt.num(abs(ext), 1)} ATR {where} la EMA21; espera un retroceso"))
        elif exhausted:
            out.append(CheckOut("overextension", False, False, round(ext, 2),
                                f"RSI {fmt.num(ctx.rsi, 0)} agotado; espera un retroceso"))
        else:
            out.append(CheckOut("overextension", True, False, round(ext, 2),
                                f"A {fmt.num(abs(ext), 1)} ATR de la EMA21 · RSI {fmt.num(ctx.rsi, 0)}"))

    # 7. volatility
    shock = ctx.shock if _ok(ctx.shock) else 0.0
    if shock > 3.0:
        out.append(CheckOut("volatility", False, True, round(shock, 2),
                            f"Vela de choque: rango de {fmt.num(shock, 1)} ATR"))
    elif not _ok(ctx.atr_rank):
        out.append(CheckOut("volatility", None, False, None, "Historial corto para medir la volatilidad"))
    else:
        pctl = ctx.atr_rank * 100.0
        if pctl > 95.0:
            out.append(CheckOut("volatility", False, False, round(pctl, 1),
                                f"ATR en el percentil {fmt.num(pctl, 0)} (volatilidad extrema)"))
        elif pctl < 8.0:
            out.append(CheckOut("volatility", False, False, round(pctl, 1),
                                f"ATR en el percentil {fmt.num(pctl, 0)} (mercado dormido)"))
        else:
            out.append(CheckOut("volatility", True, False, round(pctl, 1), f"ATR en el percentil {fmt.num(pctl, 0)}"))

    # 8. volume
    if not _ok(ctx.vol_z):
        out.append(CheckOut("volume", None, False, None, "Sin datos de volumen"))
    elif ctx.vol_z >= -0.5:
        out.append(CheckOut("volume", True, False, round(ctx.vol_z, 2), f"Volumen normal (z {fmt.num(ctx.vol_z, 1, True)})"))
    else:
        out.append(CheckOut("volume", False, False, round(ctx.vol_z, 2), f"Volumen bajo (z {fmt.num(ctx.vol_z, 1, True)})"))

    # 9. learned probability of the direction
    pword = "subida" if side > 0 else "bajada"
    passed = p_dir >= min_prob
    rel = "≥" if passed else "<"
    out.append(CheckOut("probability", passed, False, round(p_dir, 4),
                        f"P({pword}) {fmt.prob(p_dir)} {rel} {fmt.prob(min_prob)}"))

    # 10. history of similar signals
    dir_name = "long" if side > 0 else "short"
    wins, n = ctx.edge.get(dir_name, (0, 0))
    if n < EDGE_MIN_N:
        out.append(CheckOut("edge_history", None, False, n,
                            f"Pocas señales similares resueltas ({n} < {EDGE_MIN_N})"))
    else:
        wr = wins / n
        scope_txt = ctx.edge_scope.get(dir_name, "") if isinstance(ctx.edge_scope, dict) else str(ctx.edge_scope or "")
        scope = f" {scope_txt}" if scope_txt else ""
        out.append(CheckOut("edge_history", wr >= 0.5, False, round(wr, 4),
                            f"Señales similares{scope}: {wins}/{n} ganadoras ({fmt.prob(wr)})"))

    # 11. expected value after fees
    ev = plan.ev_r
    ev_txt = f"EV {fmt.rr(ev, 2)} tras comisiones (P(TP1) {fmt.prob(plan.p_tp1)})"
    # name the cost when it is what removes the edge (a positive plan before commissions)
    fee_note = f"; sin la comisión ({fee} por lado) sería {fmt.rr(ev + plan.fee_r, 2)}" \
        if plan.fee_r > 0 and ev + plan.fee_r > 0.05 else ""
    if ev > 0.05:
        out.append(CheckOut("expected_value", True, False, round(ev, 4), ev_txt))
    elif ev > 0:
        out.append(CheckOut("expected_value", False, False, round(ev, 4), f"{ev_txt}: ventaja mínima{fee_note}"))
    else:
        out.append(CheckOut("expected_value", False, True, round(ev, 4),
                            f"{ev_txt}: valor esperado negativo{fee_note}"))

    # 12. circuit breaker on losing streaks
    if ctx.closed_trades == 0:
        out.append(CheckOut("recent_performance", None, False, 0, "Sin operaciones cerradas todavía"))
    elif ctx.loss_streak >= LOSS_STREAK_LIMIT:
        out.append(CheckOut("recent_performance", False, False, ctx.loss_streak,
                            f"{ctx.loss_streak} pérdidas seguidas: freno de seguridad activo"))
    else:
        out.append(CheckOut("recent_performance", True, False, ctx.loss_streak,
                            f"Racha actual: {ctx.loss_streak} pérdida(s) seguida(s)"))

    # 13. confluence of the weighted experts
    num = den = 0.0
    for s_e, w_e in zip(ctx.expert_signals, ctx.expert_weights):
        eff = w_e * s_e  # net weight may be negative (expert faded by the learner)
        den += abs(eff)
        if side * eff > 0 and abs(s_e) > 0.05:
            num += abs(eff)
    if den < 1e-9:
        out.append(CheckOut("confluence", None, False, None, "Los expertos no dan señal"))
    else:
        share = num / den
        txt = f"{fmt.prob(share)} del peso de los expertos a favor"
        out.append(CheckOut("confluence", share >= 0.6, False, round(share, 4),
                            txt if share >= 0.6 else f"Solo {txt}"))
    return out
