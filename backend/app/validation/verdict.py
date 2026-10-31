"""Honest verdicts of the validation (Spanish texts). Pure functions, no I/O.

A series (or a group of series) only reads "Indicios de ventaja" when ALL of these hold:
- at least MIN_TRADES closed trades;
- the 95 % interval of the expectancy (mean R after commissions and slippage) is entirely above 0;
- NEXUS + gate beats the random entries clearly (percentile ≥ EDGE_PERCENTILE of the random sequences; for groups:
  the 95 % interval of the excess over random, by time blocks, is above 0);
- the result is not carried by a single period / asset (positive in ≥ 60 % of the folds or series; a series needs
  trades in at least two folds).
Everything else is "Sin evidencia de ventaja" with the concrete reason. Never "prueba" nor "garantía".
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from app.sim.common import fmt_num, fmt_r
from app.validation import stats as S

MIN_TRADES = 30
EDGE_PERCENTILE = 95.0
MAJORITY = 0.6
EDGE_LABEL = "Indicios de ventaja"
NO_EDGE_LABEL = "Sin evidencia de ventaja"
NOT_A_GUARANTEE = "No es una garantía: es una medición sobre datos pasados y el mercado cambia."


def r2(x: float | None) -> str:
    return fmt_r(x, 2)


def ci_text(ci: Sequence[float] | None) -> str:
    return f"{r2(ci[0])} a {r2(ci[1])}" if ci else "sin intervalo"


UNCERTAIN_TEXT = "muy incierto"


def ci_for(ci: Sequence[float] | None, n: int | None) -> str:
    """The 95 % interval of a sample of `n` trades as shown to people: none below S.MIN_CI_N trades (also for older
    reports that still carry one), flagged «muy incierto» below MIN_TRADES."""
    k = int(n or 0)
    if k < S.MIN_CI_N:
        return f"sin intervalo: menos de {S.MIN_CI_N} operaciones" if k > 0 else "sin intervalo"
    if not ci:
        return "sin intervalo"
    return f"{ci_text(ci)} · {UNCERTAIN_TEXT}" if k < MIN_TRADES else ci_text(ci)


def count_text(n: int, one: str, many: str) -> str:
    """'1 serie' / '3 series' (Spanish agreement for the messages of this feature)."""
    return f"{n} {one if n == 1 else many}"


def _trades_text(n: int) -> str:
    return count_text(n, "operación cerrada", "operaciones cerradas")


def _majority(count: int, total: int) -> bool:
    return total > 0 and count >= math.ceil(MAJORITY * total)


def _verdict(code: str, detail: str, extra: str | None = None) -> dict[str, Any]:
    label = EDGE_LABEL if code == "edge" else NO_EDGE_LABEL
    text = f"{label}: {detail}."
    if extra:
        text = f"{text} {extra}"
    return {"code": code, "label": label, "text": text}


def series_verdict(n: int, expectancy: float | None, ci: Sequence[float] | None, percentile: float | None,
                   folds_positive: int, folds_with_trades: int) -> dict[str, Any]:
    """Verdict of one (symbol, tf): insufficient | negative | no_edge | edge."""
    if n < MIN_TRADES or expectancy is None:
        return _verdict("insufficient", f"muestra insuficiente ({_trades_text(n)}; hacen falta al menos {MIN_TRADES})")
    pct_txt = "sin dato" if percentile is None else fmt_num(percentile, 0)
    if ci and ci[1] < 0:
        extra = None
        if percentile is not None and percentile >= EDGE_PERCENTILE:
            extra = (f"Pierde menos que las entradas al azar (percentil {pct_txt}), pero después de costes no gana "
                     "dinero.")
        return _verdict("negative", f"pierde después de comisiones y deslizamiento ({r2(expectancy)} por operación; "
                                    f"IC 95 % {ci_text(ci)}, todo por debajo de 0)", extra)
    reasons = []
    if not ci or ci[0] <= 0:
        reasons.append(f"el intervalo del 95 % de la expectativa ({ci_text(ci)}) incluye el 0")
    if percentile is None or percentile < EDGE_PERCENTILE:
        reasons.append(f"no supera claramente a las entradas al azar (percentil {pct_txt}; hace falta "
                       f"{fmt_num(EDGE_PERCENTILE, 0)} o más)")
    if folds_with_trades == 1:  # one period carries every trade: a single stretch of the market, not a pattern
        reasons.append("todas las operaciones caen en un solo periodo")
    elif not _majority(folds_positive, folds_with_trades):
        reasons.append(f"solo {folds_positive} de {folds_with_trades} periodos con expectativa positiva")
    if reasons:
        return _verdict("no_edge", "; ".join(reasons))
    return _verdict("edge", f"expectativa {r2(expectancy)} por operación (IC 95 % {ci_text(ci)}), mejor que el "
                            f"{pct_txt} % de las secuencias al azar y positiva en {folds_positive} de "
                            f"{folds_with_trades} periodos", NOT_A_GUARANTEE)


def gate_effect(gated: Sequence[float], ungated: Sequence[float], *, runs: int, seed: int) -> dict[str, Any]:
    """What the Trade Gate adds per trade: mean R with the gate − mean R taking every candidate."""
    a, b = S.finite_array(gated), S.finite_array(ungated)
    out: dict[str, Any] = {"trades_gated": int(a.size), "trades_ungated": int(b.size), "diff_r": None, "ci95": None}
    if a.size == 0 or b.size == 0:
        out.update(code="insufficient", text="Efecto del filtro: sin operaciones suficientes para compararlo.")
        return out
    diff = float(a.mean() - b.mean())
    ci = S.bootstrap_diff_ci(a, b, runs=runs, seed=seed)
    out.update(diff_r=diff, ci95=[ci[0], ci[1]] if ci else None)
    if a.size < MIN_TRADES or b.size < MIN_TRADES:
        out.update(code="insufficient", text=f"Efecto del filtro: muestra insuficiente ({a.size} con filtro y "
                                             f"{b.size} sin filtro; hacen falta {MIN_TRADES} por lado).")
    elif ci and ci[0] > 0:
        out.update(code="better", text=f"El filtro mejora la expectativa en {r2(diff)} por operación "
                                       f"(IC 95 % {ci_text(ci)}).")
    elif ci and ci[1] < 0:
        out.update(code="worse", text=f"El filtro empeora la expectativa en {r2(diff)} por operación "
                                      f"(IC 95 % {ci_text(ci)}).")
    else:
        out.update(code="unclear", text=f"Sin evidencia de que el filtro cambie la expectativa: {r2(diff)} por "
                                        f"operación (IC 95 % {ci_text(ci)}, incluye el 0).")
    return out


def correlation_text(assets: int, rho: float | None, n_eff: float | None) -> str | None:
    """How much independent evidence several assets give (None for a single asset or an unknown correlation)."""
    if assets <= 1 or rho is None or n_eff is None:
        return None
    if n_eff <= assets - 0.5:
        return (f"Los activos están correlacionados (correlación diaria media {fmt_num(rho, 2)}): {assets} equivalen "
                f"a unos {fmt_num(n_eff, 1)} independientes, así que la evidencia conjunta vale menos de lo que "
                "parece.")
    return f"Correlación diaria media entre activos {fmt_num(rho, 2)} (≈ {fmt_num(n_eff, 1)} independientes de {assets})."


def group_verdict(n: int, mean: float | None, ci: Sequence[float] | None, excess: float | None,
                  excess_ci: Sequence[float] | None, positive_series: int, series_count: int, assets: int,
                  n_eff: float | None, rho: float | None = None) -> dict[str, Any]:
    """Verdict of several series pooled (one timeframe or all): intervals by time blocks across assets."""
    corr_txt = correlation_text(assets, rho, n_eff)
    if n < MIN_TRADES or mean is None:
        return _verdict("insufficient", f"muestra insuficiente ({_trades_text(n)}; hacen falta al menos "
                                        f"{MIN_TRADES})", corr_txt)
    if not ci:
        return _verdict("insufficient", "no hay suficientes periodos distintos para un intervalo por bloques de "
                                        "tiempo", corr_txt)
    if ci[1] < 0:
        return _verdict("negative", f"pierde después de comisiones y deslizamiento ({r2(mean)} por operación; "
                                    f"IC 95 % por bloques {ci_text(ci)}, todo por debajo de 0)", corr_txt)
    reasons = []
    if ci[0] <= 0:
        reasons.append(f"el intervalo del 95 % por bloques de tiempo ({ci_text(ci)}) incluye el 0")
    if not excess_ci or excess_ci[0] <= 0:
        reasons.append(f"no supera a las entradas al azar del mismo periodo y lado ({r2(excess)} por operación; "
                       f"IC 95 % {ci_text(excess_ci)})")
    if not _majority(positive_series, series_count):
        reasons.append(f"solo {positive_series} de {series_count} series con expectativa positiva")
    if reasons:
        return _verdict("no_edge", "; ".join(reasons), corr_txt)
    extra = " ".join(x for x in (corr_txt, NOT_A_GUARANTEE) if x)
    return _verdict("edge", f"expectativa conjunta {r2(mean)} por operación (IC 95 % por bloques {ci_text(ci)}), "
                            f"{r2(excess)} por encima del azar (IC 95 % {ci_text(excess_ci)}) y positiva en "
                            f"{positive_series} de {series_count} series", extra)
