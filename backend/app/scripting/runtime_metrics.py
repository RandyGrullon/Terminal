"""BacktestMetrics for PyChart strategies (self-contained so the sandbox worker stays light).

Same conventions as ``app.sim.metrics``: ``*_pct`` in percent units, ``win_rate`` as a fraction, drawdowns as
positive percentages, ratios annualised with the bars-per-year of the data (capped by the nominal grid).
Trade statistics use closed trades only; ``final_equity`` is marked to market (open position included).
"""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

YEAR_S = 365.25 * 86400.0
MIN_CAGR_SPAN_S = 30 * 86400


def drawdown_pct(equity: np.ndarray) -> np.ndarray:
    eq = np.asarray(equity, dtype=float)
    if eq.size == 0:
        return eq
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - eq) / peak * 100.0, 0.0)
    return np.clip(np.nan_to_num(dd, nan=0.0, posinf=100.0, neginf=0.0), 0.0, 100.0)


def bar_seconds(times: np.ndarray) -> float:
    t = np.asarray(times, dtype=np.int64)
    if t.size < 2:
        return 3600.0
    d = np.diff(t)
    d = d[d > 0]
    return float(np.median(d)) if d.size else 3600.0


def periods_per_year(times: np.ndarray) -> float:
    t = np.asarray(times, dtype=np.int64)
    nominal = YEAR_S / max(bar_seconds(t), 1.0)
    if t.size >= 3 and t[-1] > t[0]:
        empirical = (t.size - 1) / ((t[-1] - t[0]) / YEAR_S)
        if empirical > 0:
            return float(min(nominal, empirical))
    return float(nominal)


def _ratio(mean: float, scale: float, ppy: float) -> float | None:
    if not math.isfinite(scale) or scale <= 1e-12:
        return None
    value = mean / scale * math.sqrt(max(ppy, 0.0))
    return float(value) if math.isfinite(value) else None


def max_consecutive_losses(pnls: Sequence[float]) -> int:
    best = run = 0
    for p in pnls:
        run = run + 1 if p < 0 else 0
        best = max(best, run)
    return best


def compute_metrics(
    times: np.ndarray,
    closes: np.ndarray,
    equity: np.ndarray,
    in_position: np.ndarray,
    closed_trades: list[dict[str, Any]],
    initial_capital: float,
    fees_paid: float,
) -> dict[str, Any]:
    """Every BacktestMetrics key (null where not applicable). ``closed_trades`` items need ``pnl`` (money),
    ``account_pct``, ``pnl_pct``, ``r_multiple`` and ``bars``."""
    eq = np.asarray(equity, dtype=float)
    t = np.asarray(times, dtype=np.int64)
    n = int(eq.size)
    final = float(eq[-1]) if n else float(initial_capital)
    total = (final / initial_capital - 1.0) * 100.0 if initial_capital > 0 else 0.0
    span = float(t[-1] - t[0] + bar_seconds(t)) if n else 0.0
    cagr = None
    if n and initial_capital > 0 and span >= MIN_CAGR_SPAN_S:
        if final <= 0:
            cagr = -100.0
        else:
            try:
                value = ((final / initial_capital) ** (YEAR_S / span) - 1.0) * 100.0
                cagr = float(value) if math.isfinite(value) else None
            except (OverflowError, ZeroDivisionError):
                cagr = None
    dd = drawdown_pct(eq)
    max_dd = float(dd.max()) if dd.size else 0.0
    rets = np.zeros(0)
    if n >= 2:
        prev = eq[:-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            rets = np.where(prev > 0, eq[1:] / prev - 1.0, 0.0)
        rets = rets[np.isfinite(rets)]
    ppy = periods_per_year(t) if n else 0.0
    sharpe = sortino = None
    if rets.size >= 3:
        sharpe = _ratio(float(rets.mean()), float(rets.std(ddof=1)), ppy)
        downside = np.minimum(rets, 0.0)
        sortino = _ratio(float(rets.mean()), math.sqrt(float(np.mean(downside * downside))), ppy)
    closes_arr = np.asarray(closes, dtype=float)
    bh = float((closes_arr[-1] / closes_arr[0] - 1.0) * 100.0) if n and closes_arr[0] > 0 else 0.0

    pnls = [float(tr["pnl"]) for tr in closed_trades]
    acct = np.asarray([float(tr["account_pct"]) for tr in closed_trades], dtype=float)
    rs = np.asarray([tr["r_multiple"] for tr in closed_trades if tr.get("r_multiple") is not None], dtype=float)
    bars = np.asarray([float(tr["bars"]) for tr in closed_trades], dtype=float)
    p = np.asarray(pnls, dtype=float)
    k = int(p.size)
    wins, losses = p > 0, p < 0
    gross_win = float(p[wins].sum()) if k else 0.0
    gross_loss = float(-p[losses].sum()) if k else 0.0
    return {
        "initial_capital": float(initial_capital),
        "final_equity": final,
        "total_return_pct": total,
        "cagr_pct": cagr,
        "buy_hold_return_pct": bh,
        "trades": k,
        "win_rate": float(wins.sum() / k) if k else None,
        "profit_factor": (gross_win / gross_loss) if k and gross_loss > 0 else None,
        "avg_win_pct": float(acct[wins].mean()) if k and wins.any() else None,
        "avg_loss_pct": float(acct[losses].mean()) if k and losses.any() else None,
        "expectancy_pct": float(acct.mean()) if k else None,
        "avg_r": float(rs.mean()) if rs.size else None,
        "max_dd_pct": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": float(cagr / max_dd) if cagr is not None and max_dd > 1e-9 else None,
        "exposure_pct": float(np.asarray(in_position, dtype=float).mean() * 100.0) if n else 0.0,
        "avg_bars_in_trade": float(bars.mean()) if bars.size else None,
        "best_trade_pct": float(acct.max()) if k else None,
        "worst_trade_pct": float(acct.min()) if k else None,
        "max_consecutive_losses": max_consecutive_losses(pnls),
        "fees_paid": float(fees_paid),
        "rejected_signals": 0,
        "rejected_would_win_rate": None,
        "gate_lift_pct": None,
    }


def equity_points(times: np.ndarray, equity: np.ndarray) -> list[dict]:
    eq = np.asarray(equity, dtype=float)
    t = np.asarray(times, dtype=np.int64).tolist()
    e = np.round(eq, 2).tolist()
    d = np.round(drawdown_pct(eq), 3).tolist()
    return [{"time": t[i], "equity": e[i], "drawdown_pct": d[i]} for i in range(len(t))]


def benchmark_points(times: np.ndarray, closes: np.ndarray, initial_capital: float) -> list[dict]:
    c = np.asarray(closes, dtype=float)
    if c.size == 0 or not c[0] > 0:
        return []
    t = np.asarray(times, dtype=np.int64).tolist()
    e = np.round(initial_capital * c / c[0], 2).tolist()
    return [{"time": t[i], "equity": e[i]} for i in range(len(t))]
