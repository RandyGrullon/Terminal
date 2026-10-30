"""Prediction cone for the next `horizon` bars.

Analog forecasting: empirical forward returns of past bars with a similar score and the same regime (only bars
whose whole forward window is already known are used). Falls back to a volatility (√t) cone centred on the drift
implied by the calibrated probability when there are too few analogs.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_NORMAL = NormalDist()
MIN_ANALOGS = 30
QUANTILES = (10, 25, 50, 75, 90)
Z = {10: -1.2815516, 25: -0.6744898, 50: 0.0, 75: 0.6744898, 90: 1.2815516}


def _analog_rows(score: np.ndarray, regime: np.ndarray, i: int, h: int, start: int) -> np.ndarray:
    last = i - h  # forward window [k, k+h] must be fully known at bar i
    if last <= start:
        return np.zeros(0, dtype=np.int64)
    ks = np.arange(start, last + 1)
    sc = score[start : last + 1]
    rg = regime[start : last + 1]
    finite = np.isfinite(sc)
    s0 = score[i]
    r0 = regime[i]
    for tol, same_regime in ((0.15, True), (0.30, True), (0.30, False)):
        sel = finite & (np.abs(sc - s0) <= tol)
        if same_regime:
            sel &= rg == r0
        if int(sel.sum()) >= MIN_ANALOGS:
            return ks[sel]
    return np.zeros(0, dtype=np.int64)


def build_prediction(close: np.ndarray, time: np.ndarray, tf_s: int, score: np.ndarray, regime: np.ndarray,
                     i: int, prob_up: float, horizon: int, start: int) -> dict:
    """Prediction dict (types.ts `Prediction`) at bar i."""
    h = max(int(horizon), 1)
    c0 = float(close[i])
    t0 = int(time[i])
    logc = np.log(np.maximum(close[: i + 1], 1e-300))
    ks = _analog_rows(score, regime, i, h, max(start, 0)) if np.isfinite(score[i]) else np.zeros(0, dtype=np.int64)
    if len(ks) >= MIN_ANALOGS:
        steps = np.arange(1, h + 1)
        fwd = logc[ks[:, None] + steps[None, :]] - logc[ks][:, None]  # (n_analogs, h)
        qs = np.percentile(fwd, QUANTILES, axis=0)  # (5, h)
        final = fwd[:, -1]
        expected = float(np.mean(np.expm1(final))) * 100.0
        method = f"analog ({len(ks)} casos similares)"
        cone_q = {q: qs[j] for j, q in enumerate(QUANTILES)}
    else:
        rets = np.diff(logc[max(0, i - 200) : i + 1])
        rets = rets[np.isfinite(rets)]
        sig = float(np.std(rets)) if len(rets) >= 10 else 0.01
        p = min(max(prob_up if prob_up == prob_up else 0.5, 0.02), 0.98)
        mu_h = _NORMAL.inv_cdf(p) * sig * math.sqrt(h)
        steps = np.arange(1, h + 1, dtype=float)
        frac = steps / h
        cone_q = {q: mu_h * frac + Z[q] * sig * np.sqrt(steps) for q in QUANTILES}
        expected = (math.exp(mu_h + 0.5 * sig * sig * h) - 1.0) * 100.0
        method = "volatilidad (√t)"
    cone = [{"time": t0, "p10": c0, "p25": c0, "p50": c0, "p75": c0, "p90": c0}]
    for s in range(h):
        point = {"time": t0 + (s + 1) * int(tf_s)}
        for q in QUANTILES:
            point[f"p{q}"] = float(c0 * math.exp(float(cone_q[q][s])))
        cone.append(point)
    return {
        "horizon_bars": h,
        "prob_up": round(float(prob_up), 4),
        "expected_return_pct": round(expected, 4),
        "cone": cone,
        "method": method,
    }
