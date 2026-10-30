"""Trade levels ("hasta aquí"), first-passage probabilities and the expected value of a trade plan.

Plan: SL = entry ∓ max(sl_atr·ATR, distance beyond the last swing ± 0.25·ATR), R = |entry − SL|,
TP1..3 = entry ± tp_r·R. TP1 → close 1/3 and move the stop to break-even; TP2 → close 1/3 and trail the rest
with a chandelier stop (trail_atr·ATR from the extreme); TP3 → close the rest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

_NORMAL = NormalDist()
SWING_BUFFER_ATR = 0.25
SWING_MAX_MULT = 2.0  # a swing farther than 2 × sl_atr·ATR is ignored (the stop would be too wide)


@dataclass
class Levels:
    side: int  # +1 long, -1 short
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.sl, self.tp1, self.tp2, self.tp3


def stop_distance(side: int, entry: float, atr: float, swing: float | None, sl_atr: float) -> float:
    """Distance from entry to the protective stop (always > 0)."""
    base = max(sl_atr, 0.1) * atr if atr == atr and atr > 0 else abs(entry) * 0.01
    if swing is not None and swing == swing:
        beyond = (entry - swing if side > 0 else swing - entry) + SWING_BUFFER_ATR * (atr if atr == atr else 0.0)
        if 0 < beyond <= SWING_MAX_MULT * base:
            base = max(base, beyond)
    return max(base, abs(entry) * 1e-6, 1e-12)


def plan_levels(side: int, entry: float, atr: float, swing: float | None, sl_atr: float,
                tp_r: tuple[float, float, float]) -> Levels:
    d = stop_distance(side, entry, atr, swing, sl_atr)
    sl = entry - side * d
    return Levels(side, entry, sl, entry + side * tp_r[0] * d, entry + side * tp_r[1] * d, entry + side * tp_r[2] * d)


def levels_from_user(side: int, entry: float, atr: float, swing: float | None, sl_atr: float,
                     tp_r: tuple[float, float, float], sl: float | None, tp: float | None) -> Levels:
    """Levels with the user's stop and/or target. A single target replaces TP1..TP3 (one exit)."""
    if sl is None:
        sl = entry - side * stop_distance(side, entry, atr, swing, sl_atr)
    d = abs(entry - sl)
    if tp is not None:
        return Levels(side, entry, float(sl), float(tp), float(tp), float(tp))
    return Levels(side, entry, float(sl), entry + side * tp_r[0] * d, entry + side * tp_r[1] * d,
                  entry + side * tp_r[2] * d)


def drift_k(p_dir: float, sigma: float, horizon: int) -> float:
    """2μ/σ² of a Brownian motion whose P(move over `horizon` bars in our favour) equals p_dir."""
    p = min(max(p_dir, 0.02), 0.98)
    if not (sigma == sigma) or sigma <= 0:
        return 0.0
    return 2.0 * _NORMAL.inv_cdf(p) / (sigma * math.sqrt(max(horizon, 1)))


def first_passage(a: float, b: float, k: float) -> float:
    """P(hit +a before -b) for a Brownian motion with 2μ/σ² = k (a, b >= 0, price units)."""
    if a <= 0:
        return 1.0
    if b <= 0:
        return 0.0
    if abs(k) * (a + b) < 1e-6:
        return b / (a + b)
    if k > 0:
        return math.expm1(-k * b) / math.expm1(-k * (a + b))
    return math.exp(k * a) * math.expm1(k * b) / math.expm1(k * (a + b))


@dataclass
class PlanEV:
    p_tp1: float
    p_tp2: float
    p_tp3: float
    ev_r: float
    rr: float
    fee_r: float


def plan_ev(lv: Levels, p_win: float, k: float, atr: float, trail_atr: float, fee_pct: float) -> PlanEV:
    """Expected value (R, after fees) of the 1/3-1/3-1/3 plan given P(TP1 before SL) = p_win and drift k."""
    b = lv.risk
    if b <= 0:
        return PlanEV(0.0, 0.0, 0.0, -1.0, 0.0, 0.0)
    a1 = max(lv.side * (lv.tp1 - lv.entry), 0.0)
    a2 = max(lv.side * (lv.tp2 - lv.entry), a1)
    a3 = max(lv.side * (lv.tp3 - lv.entry), a2)
    trail = trail_atr * atr if atr == atr and atr > 0 else a2
    d3 = min(a2, trail)
    p2 = first_passage(a2 - a1, a1, k)  # from TP1 with the stop at break-even
    p3 = first_passage(a3 - a2, d3, k)  # from TP2 with the chandelier stop
    gross = p_win * (a1 / 3.0 + p2 * (a2 / 3.0 + p3 * a3 / 3.0 + (1.0 - p3) * (a2 - d3) / 3.0)) / b - (1.0 - p_win)
    fee_cost = 2.0 * fee_pct / 100.0 * abs(lv.entry)
    fee_r = fee_cost / b
    rr = (a2 - fee_cost) / (b + fee_cost)
    return PlanEV(p_win, p_win * p2, p_win * p2 * p3, gross - fee_r, rr, fee_r)


def ev_hold(side: int, price: float, stop: float, next_tp: float | None, risk: float, p_dir: float,
            sigma: float, horizon: int) -> tuple[float, float]:
    """Expected value of holding (R) = p·(dist_to_next_tp/R) − (1 − p)·(dist_to_sl/R).

    p is the calibrated probability of reaching the next target before the stop, derived from the calibrated
    directional probability through the drifted first-passage model (so a trade near its target is not judged
    with the probability of a whole-horizon move). Returns (ev_r, p).
    """
    if risk <= 0:
        return 0.0, 0.5
    dist_sl = max(side * (price - stop), 0.0)
    dist_tp = max(side * (next_tp - price), 0.0) if next_tp is not None else risk
    p = first_passage(dist_tp, dist_sl, drift_k(p_dir, sigma, horizon))
    return p * dist_tp / risk - (1.0 - p) * dist_sl / risk, p
