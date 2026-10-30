"""Pure portfolio / trade math shared by backtests, Monte Carlo, paper trading and the simulator.

Conventions: `*_pct` values are in percent units (8.4 = 8.4 %); `*_rate` values are fractions (0..1).
Drawdowns are positive percentages (12.5 = equity 12.5 % below its running peak).
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np

YEAR_S = 365.25 * 86400.0
MIN_CAGR_SPAN_S = 30 * 86400  # shorter spans make annualised numbers meaningless
DEFAULT_MAX_LEVERAGE = 5.0


# ----------------------------------------------------------------------------- equity curves


def drawdown_pct(equity: Sequence[float] | np.ndarray) -> np.ndarray:
    """Drawdown (percent below the running peak) for every point of an equity curve."""
    eq = np.asarray(equity, dtype=float)
    if eq.size == 0:
        return eq
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - eq) / peak * 100.0, 0.0)
    return np.clip(np.nan_to_num(dd, nan=0.0, posinf=100.0, neginf=0.0), 0.0, 100.0)


def max_drawdown_pct(equity: Sequence[float] | np.ndarray) -> float:
    dd = drawdown_pct(equity)
    return float(dd.max()) if dd.size else 0.0


def simple_returns(equity: Sequence[float] | np.ndarray) -> np.ndarray:
    """Bar-to-bar simple returns of an equity curve (fractions); non-finite values are dropped."""
    eq = np.asarray(equity, dtype=float)
    if eq.size < 2:
        return np.zeros(0)
    prev = eq[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(prev > 0, eq[1:] / prev - 1.0, 0.0)
    return r[np.isfinite(r)]


def periods_per_year(times: Sequence[int] | np.ndarray, tf_s: int) -> float:
    """Bars per year used to annualise ratios.

    The nominal grid (365.25 days / timeframe) is right for 24/7 markets; markets with sessions and weekends
    (stocks, forex) produce fewer bars per year, so the empirical rate from the data span is used when lower.
    """
    nominal = YEAR_S / max(int(tf_s), 1)
    t = np.asarray(times, dtype=np.int64)
    if t.size >= 3:
        span = float(t[-1] - t[0])
        if span > 0:
            empirical = (t.size - 1) / (span / YEAR_S)
            if empirical > 0:
                return float(min(nominal, empirical))
    return float(nominal)


def sharpe_ratio(returns: Sequence[float] | np.ndarray, ppy: float) -> float | None:
    r = np.asarray(returns, dtype=float)
    if r.size < 3:
        return None
    sd = float(r.std(ddof=1))
    if not math.isfinite(sd) or sd <= 1e-12:
        return None
    return float(r.mean() / sd * math.sqrt(max(ppy, 0.0)))


def sortino_ratio(returns: Sequence[float] | np.ndarray, ppy: float) -> float | None:
    r = np.asarray(returns, dtype=float)
    if r.size < 3:
        return None
    downside = np.minimum(r, 0.0)
    dd = math.sqrt(float(np.mean(downside * downside)))
    if dd <= 1e-12:
        return None
    return float(r.mean() / dd * math.sqrt(max(ppy, 0.0)))


def cagr_pct(initial: float, final: float, span_s: float, min_span_s: float = MIN_CAGR_SPAN_S) -> float | None:
    """Compound annual growth rate in percent (None when the span is too short to annualise)."""
    if initial <= 0 or span_s < min_span_s:
        return None
    if final <= 0:
        return -100.0
    years = span_s / YEAR_S
    try:
        value = ((final / initial) ** (1.0 / years) - 1.0) * 100.0
    except (OverflowError, ZeroDivisionError):
        return None
    return float(value) if math.isfinite(value) else None


def calmar_ratio(cagr: float | None, max_dd: float) -> float | None:
    if cagr is None or max_dd <= 1e-9:
        return None
    return float(cagr / max_dd)


# ----------------------------------------------------------------------------- trade statistics


def max_consecutive_losses(pnls: Sequence[float]) -> int:
    best = run = 0
    for p in pnls:
        if p < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def trade_summary(
    pnls: Sequence[float],
    pnl_pcts: Sequence[float],
    r_multiples: Sequence[float | None] = (),
    bars: Sequence[float] = (),
) -> dict:
    """Win rate, profit factor, averages, best/worst and streaks from per-trade net results.

    `pnls` are money amounts (sign decides win/loss), `pnl_pcts` the same trades in percent (e.g. of equity).
    """
    p = np.asarray(list(pnls), dtype=float)
    pct = np.asarray(list(pnl_pcts), dtype=float)
    n = int(p.size)
    rs = np.asarray([r for r in r_multiples if r is not None and math.isfinite(r)], dtype=float)
    bs = np.asarray([b for b in bars if b is not None and math.isfinite(b)], dtype=float)
    if n == 0:
        return {
            "trades": 0,
            "win_rate": None,
            "profit_factor": None,
            "avg_win_pct": None,
            "avg_loss_pct": None,
            "expectancy_pct": None,
            "avg_r": None,
            "best_trade_pct": None,
            "worst_trade_pct": None,
            "avg_bars_in_trade": None,
            "max_consecutive_losses": 0,
        }
    wins = p > 0
    losses = p < 0
    gross_win = float(p[wins].sum())
    gross_loss = float(-p[losses].sum())
    return {
        "trades": n,
        "win_rate": float(wins.sum() / n),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_win_pct": float(pct[wins].mean()) if wins.any() else None,
        "avg_loss_pct": float(pct[losses].mean()) if losses.any() else None,
        "expectancy_pct": float(pct.mean()),
        "avg_r": float(rs.mean()) if rs.size else None,
        "best_trade_pct": float(pct.max()),
        "worst_trade_pct": float(pct.min()),
        "avg_bars_in_trade": float(bs.mean()) if bs.size else None,
        "max_consecutive_losses": max_consecutive_losses(p.tolist()),
    }


# ----------------------------------------------------------------------------- sizing & costs


def position_size(
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    *,
    max_leverage: float | None = DEFAULT_MAX_LEVERAGE,
) -> float:
    """Units such that hitting the stop loses `risk_pct` % of equity, capped at `max_leverage` × equity notional."""
    risk_unit = abs(float(entry) - float(stop))
    if equity <= 0 or entry <= 0 or risk_pct <= 0 or risk_unit <= 0 or not math.isfinite(risk_unit):
        return 0.0
    qty = equity * risk_pct / 100.0 / risk_unit
    if max_leverage is not None and max_leverage > 0:
        qty = min(qty, equity * max_leverage / entry)
    return float(qty) if math.isfinite(qty) and qty > 0 else 0.0


def trade_cost(notional: float, fee_pct: float, slippage_pct: float = 0.0) -> float:
    """Commission + slippage cost of one fill, in money."""
    return abs(float(notional)) * (max(fee_pct, 0.0) + max(slippage_pct, 0.0)) / 100.0


def slipped_price(price: float, side: int, slippage_pct: float, *, opening: bool) -> float:
    """Market fill price including slippage: buying (long entry / short exit) pays more, selling receives less."""
    buying = (side > 0) == opening
    f = max(slippage_pct, 0.0) / 100.0
    return float(price) * (1.0 + f if buying else 1.0 - f)


def r_multiple(side: int, entry: float, exit_price: float, stop: float) -> float | None:
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return float(side * (exit_price - entry) / risk)
