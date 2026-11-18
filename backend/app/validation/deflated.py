"""Deflated Sharpe Ratio and «plateaus»: how much of a good backtest is explained by the number of variants tried.

Ported from the GenXAU research (genxau-mt5/analizar.py, XAU M30 on MetaTrader 5): of 16,651 strategies tried there,
516 looked good in 2019-24 and none passed this deflation; the in-sample result did not predict the forward one
(Spearman −0.02). Bailey and López de Prado (2014):

- SR = mean / standard deviation of the R per trade (not annualised); T = number of trades; skew and kurtosis are the
  sample ones (kurtosis NOT in excess: a normal distribution has 3).
- σ(SR) comes from the trials only, and only those with at least MIN_TRADES_TRIAL trades: variants with few trades
  inflate σ with estimation noise.
- The effective N is the number of distinct IDEAS (the structure without its numbers), not of numeric variants: the
  variants of one idea are strongly correlated, and counting each as independent overstates the penalty.
- SR0 = σ(SR) · ((1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))), γ = 0.5772: the per-trade Sharpe that luck alone reaches
  by trying N ideas.
- DSR = Φ((SR − SR0)·√(T − 1) / √(1 − skew·SR + (kurt − 1)/4·SR²)). Indicative threshold: DSR ≥ 0.5.
- Plateaus: for every idea with at least MIN_VARIANTS variants, the median of its score, its median profit factor and
  the share of positive variants, in sample and out of sample. An idea is judged by its MEDIAN variant, never by its
  best one: an idea that only works with one exact stop or target is chance.

Pure functions (statistics.NormalDist, no scipy, no numpy needed). Nothing here can say a strategy has an edge: a
passing DSR only says the result is not explained by the number of trials, and it is still a past result.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any, Iterable, Sequence

ND = NormalDist()
EULER = 0.5772156649
MIN_TRADES_TRIAL = 120  # a trial counts for σ(SR) only with this many trades (GenXAU)
MIN_VARIANTS = 4  # an idea needs this many variants for a plateau
DSR_THRESHOLD = 0.5


def _finite(values: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def trade_stats(r: Iterable[Any]) -> dict[str, float | int | None]:
    """{n, sr, skew, kurt, mean, pf, sum} of the R per trade. sr = mean / sample standard deviation (not annualised);
    skew and kurt are the sample moments (kurt not in excess: a normal has 3). None where undefined."""
    xs = _finite(r)
    n = len(xs)
    out: dict[str, float | int | None] = {"n": n, "sr": None, "skew": None, "kurt": None, "mean": None, "pf": None,
                                          "sum": round(sum(xs), 6) if xs else 0.0}
    if n == 0:
        return out
    mean = sum(xs) / n
    out["mean"] = mean
    gains, losses = sum(x for x in xs if x > 0), -sum(x for x in xs if x < 0)
    out["pf"] = gains / losses if losses > 0 else None
    if n < 2:
        return out
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return out
    out["sr"] = mean / sd
    m2 = sum((x - mean) ** 2 for x in xs) / n
    if m2 > 0:
        out["skew"] = (sum((x - mean) ** 3 for x in xs) / n) / m2 ** 1.5
        out["kurt"] = (sum((x - mean) ** 4 for x in xs) / n) / m2 ** 2
    return out


def expected_max_sr(sigma_sr: float, n_ideas: int) -> float:
    """SR0: the per-trade Sharpe that luck alone reaches by trying `n_ideas` ideas with this spread of Sharpes."""
    n = max(int(n_ideas), 2)
    return float(sigma_sr) * ((1 - EULER) * ND.inv_cdf(1 - 1 / n) + EULER * ND.inv_cdf(1 - 1 / (n * math.e)))


def deflated_sharpe(sr: float | None, t: int, skew: float | None, kurt: float | None, sr0: float) -> float:
    """DSR = Φ((SR − SR0)·√(T − 1) / √(1 − skew·SR + (kurt − 1)/4·SR²)); 0 without a usable SR (as GenXAU)."""
    if sr is None or not math.isfinite(sr) or t < 3:
        return 0.0
    g3 = 0.0 if skew is None else float(skew)
    g4 = 3.0 if kurt is None else float(kurt)
    denom = 1 - g3 * sr + (g4 - 1) / 4 * sr * sr
    if denom <= 0:
        return 0.0
    return ND.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def trial_family(trials: Sequence[dict[str, Any]], *, min_trades: int = MIN_TRADES_TRIAL) -> dict[str, Any]:
    """The multiple-testing family of `trials` ([{idea, sr, n}]): {variants, eligible, ideas, sigma_sr, sr0, best_sr}.
    sr0 is None (no deflation possible) with fewer than two eligible trials: one variant has no spread to compare."""
    eligible = [t for t in trials if (t.get("n") or 0) >= min_trades and t.get("sr") is not None
                and math.isfinite(float(t["sr"]))]
    srs = [float(t["sr"]) for t in eligible]
    ideas = len({t.get("idea") for t in eligible}) if eligible else 0
    out: dict[str, Any] = {"variants": len(trials), "eligible": len(eligible), "ideas": ideas,
                           "ideas_all": len({t.get("idea") for t in trials}), "sigma_sr": None, "sr0": None,
                           "best_sr": max(srs) if srs else None}
    if len(srs) < 2:
        return out
    mean = sum(srs) / len(srs)
    sigma = math.sqrt(sum((s - mean) ** 2 for s in srs) / (len(srs) - 1))
    out["sigma_sr"] = sigma
    out["sr0"] = expected_max_sr(sigma, max(ideas, 2))
    return out


def deflate(stats: dict[str, Any], family: dict[str, Any], *, min_trades: int = MIN_TRADES_TRIAL) -> dict[str, Any]:
    """{dsr, passes, reason}: the trial's DSR against its family. reason (Spanish) when it cannot be computed."""
    n = int(stats.get("n") or 0)
    if n < min_trades:
        return {"dsr": None, "passes": False,
                "reason": f"Con {n} operaciones no se calcula: hacen falta al menos {min_trades}"}
    if family.get("sr0") is None:
        return {"dsr": None, "passes": False,
                "reason": f"Hace falta más de una variante con {min_trades} operaciones o más para saber cuánto "
                          "alcanza el azar al probar varias"}
    value = deflated_sharpe(stats.get("sr"), n, stats.get("skew"), stats.get("kurt"), float(family["sr0"]))
    return {"dsr": value, "passes": value >= DSR_THRESHOLD, "reason": None}


def median(values: Iterable[Any]) -> float | None:
    xs = sorted(_finite(values))
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def plateaus(variants: Sequence[dict[str, Any]], *, min_variants: int = MIN_VARIANTS,
             min_trades: int = MIN_TRADES_TRIAL) -> list[dict[str, Any]]:
    """Ideas with at least `min_variants` variants of at least `min_trades` trades, judged by the median variant:
    [{idea, label, variants, is: {score, pf, positive}, oos: {variants, score, pf, positive} | None}], best median
    in-sample score first. Each variant: {idea, label?, n, is: {score, pf, sum}, oos: {n, score, pf, sum} | None}."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for v in variants:
        if (v.get("n") or 0) >= min_trades and isinstance(v.get("is"), dict):
            groups.setdefault(v.get("idea"), []).append(v)
    out: list[dict[str, Any]] = []
    for idea, sel in groups.items():
        if len(sel) < min_variants:
            continue
        oos = [v["oos"] for v in sel if isinstance(v.get("oos"), dict) and (v["oos"].get("n") or 0) > 0]
        out.append({
            "idea": idea,
            "label": next((v.get("label") for v in sel if v.get("label")), str(idea)),
            "variants": len(sel),
            "is": {"score": median(v["is"].get("score") for v in sel),
                   "pf": median(v["is"].get("pf") for v in sel),
                   "positive": sum(1 for v in sel if (v["is"].get("sum") or 0) > 0) / len(sel)},
            "oos": None if not oos else {
                "variants": len(oos),
                "score": median(o.get("score") for o in oos),
                "pf": median(o.get("pf") for o in oos),
                "positive": sum(1 for o in oos if (o.get("sum") or 0) > 0) / len(oos)},
        })
    out.sort(key=lambda p: -(p["is"]["score"] if p["is"]["score"] is not None else -math.inf))
    return out
