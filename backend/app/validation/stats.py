"""Pure statistics of the walk-forward validation (numpy only, seeded, deterministic).

Conventions: R multiples are net of commissions and slippage; `*_pct` values are percent units; rates are fractions.
Intervals are 95 % percentile-bootstrap intervals, and only with at least MIN_CI_N values: resampling a handful of
trades gives intervals far narrower than the real uncertainty (two trades gave an interval under half an R wide), so
below that there is no interval at all; between MIN_CI_N and 30 the texts flag it as "muy incierto" (verdict.ci_for).
Nothing here touches the network, the database or the engine.
"""
from __future__ import annotations

import math
import zlib
from typing import Any, Iterable, Sequence

import numpy as np

from app.sim import metrics as M

CI_LEVEL = 0.95
BOOTSTRAP_CHUNK = 500  # bootstrap resamples drawn at once (bounds memory with large pooled samples)
MIN_BLOCKS = 8  # a block bootstrap with fewer distinct blocks says nothing
MIN_CI_N = 10  # no bootstrap interval with fewer values (frontend: ValidationCard MIN_CI_N)


def seed_for(*parts: Any) -> int:
    """Stable 32-bit seed from any parts (same inputs -> same random draws, different series -> different draws)."""
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8")) & 0x7FFFFFFF


def _num(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, np.floating, np.integer)):
        return math.nan
    return float(v)


def finite_array(values: Iterable[Any] | np.ndarray | None) -> np.ndarray:
    """1-D float array with the non-finite values (None, NaN, ±inf, non-numbers) dropped."""
    if values is None:
        return np.zeros(0)
    if isinstance(values, np.ndarray):
        arr = values.astype(float, copy=False).ravel()
    else:
        arr = np.asarray([_num(v) for v in values], dtype=float)
    return arr[np.isfinite(arr)]


def _pct_interval(samples: np.ndarray, level: float = CI_LEVEL) -> tuple[float, float]:
    tail = (1.0 - level) / 2.0 * 100.0
    lo, hi = np.percentile(samples, [tail, 100.0 - tail])
    return float(lo), float(hi)


def bootstrap_mean_ci(values: Any, *, runs: int = 2000, seed: int = 0, level: float = CI_LEVEL,
                      min_n: int = MIN_CI_N) -> tuple[float, float] | None:
    """Percentile-bootstrap interval of the mean (resampling the values with replacement). None with fewer than
    `min_n` finite values (never fewer than 2)."""
    x = finite_array(values)
    if x.size < max(int(min_n), 2) or runs < 10:
        return None
    rng = np.random.default_rng(int(seed))
    means = np.empty(int(runs))
    done = 0
    while done < runs:
        k = min(BOOTSTRAP_CHUNK, int(runs) - done)
        idx = rng.integers(0, x.size, size=(k, x.size))
        means[done:done + k] = x[idx].mean(axis=1)
        done += k
    return _pct_interval(means, level)


def bootstrap_diff_ci(a: Any, b: Any, *, runs: int = 2000, seed: int = 0, level: float = CI_LEVEL,
                      min_n: int = MIN_CI_N) -> tuple[float, float] | None:
    """Interval of mean(a) − mean(b), resampling each sample on its own. With overlapping samples (the gated trades
    are mostly a subset of the ungated ones) this ignores their positive correlation, so it is wider (conservative).
    None when either sample has fewer than `min_n` values."""
    xa, xb = finite_array(a), finite_array(b)
    k = max(int(min_n), 2)
    if xa.size < k or xb.size < k or runs < 10:
        return None
    rng = np.random.default_rng(int(seed))
    diffs = np.empty(int(runs))
    done = 0
    while done < runs:
        k = min(BOOTSTRAP_CHUNK, int(runs) - done)
        ma = xa[rng.integers(0, xa.size, size=(k, xa.size))].mean(axis=1)
        mb = xb[rng.integers(0, xb.size, size=(k, xb.size))].mean(axis=1)
        diffs[done:done + k] = ma - mb
        done += k
    return _pct_interval(diffs, level)


def block_bootstrap_mean_ci(values: Any, blocks: Any, *, runs: int = 2000, seed: int = 0, level: float = CI_LEVEL,
                            min_blocks: int = MIN_BLOCKS, min_n: int = MIN_CI_N) -> tuple[float, float] | None:
    """Interval of the pooled mean resampling whole blocks (e.g. calendar periods holding the trades of every asset):
    trades of correlated assets in the same period are drawn together, so they are not counted as independent
    evidence. None when there are fewer than `min_blocks` distinct blocks or fewer than `min_n` values."""
    x = np.asarray(list(values) if not isinstance(values, np.ndarray) else values, dtype=float).ravel()
    b = np.asarray(list(blocks) if not isinstance(blocks, np.ndarray) else blocks).ravel()
    if x.size != b.size:
        raise ValueError("values y blocks deben tener la misma longitud")
    ok = np.isfinite(x)
    x, b = x[ok], b[ok]
    if x.size < max(int(min_n), 2) or runs < 10:
        return None
    uniq, inv = np.unique(b, return_inverse=True)
    k = int(uniq.size)
    if k < max(int(min_blocks), 2):
        return None
    sums = np.bincount(inv, weights=x, minlength=k)
    counts = np.bincount(inv, minlength=k).astype(float)
    rng = np.random.default_rng(int(seed))
    means = np.empty(int(runs))
    done = 0
    while done < runs:
        m = min(BOOTSTRAP_CHUNK, int(runs) - done)
        pick = rng.integers(0, k, size=(m, k))
        means[done:done + m] = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
        done += m
    return _pct_interval(means, level)


def rank_percentile(value: float | None, dist: Any) -> tuple[float | None, float | None]:
    """(percentile of `value` inside `dist` in 0..100 with ties counted half, one-sided p-value of "value is better than
    the distribution" = (1 + #{dist ≥ value}) / (N + 1))."""
    d = finite_array(dist)
    if value is None or not math.isfinite(float(value)) or d.size == 0:
        return None, None
    v = float(value)
    below = float(np.count_nonzero(d < v))
    ties = float(np.count_nonzero(d == v))
    pct = 100.0 * (below + 0.5 * ties) / d.size
    p = (1.0 + float(np.count_nonzero(d >= v))) / (d.size + 1.0)
    return pct, p


def profit_factor(r: Any) -> float | None:
    """Gross wins / gross losses of R multiples (None without trades or without losing trades)."""
    x = finite_array(r)
    loss = float(-x[x < 0].sum())
    if x.size == 0 or loss <= 0:
        return None
    return float(x[x > 0].sum()) / loss


def equity_from_r(r: Any, risk_pct: float, start: float = 100.0) -> np.ndarray:
    """Closed-trade equity compounding `risk_pct` % of equity per R (a −1R trade loses risk_pct %)."""
    x = finite_array(r)
    growth = np.maximum(1.0 + float(risk_pct) / 100.0 * x, 1e-9)
    return np.concatenate([[float(start)], float(start) * np.cumprod(growth)])


def max_dd_from_r(r: Any, risk_pct: float) -> float:
    """Max drawdown (%) of the closed-trade equity of an R sequence (in trade order)."""
    return float(M.max_drawdown_pct(equity_from_r(r, risk_pct)))


def r_summary(r: Any, *, risk_pct: float, runs: int, seed: int) -> dict[str, Any]:
    """Trades, wins, win rate, expectancy (mean R) with its 95 % bootstrap interval (None below MIN_CI_N trades),
    profit factor, total R, the compounded return and max drawdown of the closed-trade equity (in the given order:
    the same measure as the random sequences' `trade_max_dd_p50`)."""
    x = finite_array(r)
    n = int(x.size)
    if n == 0:
        return {"trades": 0, "wins": 0, "win_rate": None, "expectancy_r": None, "ci95": None, "profit_factor": None,
                "total_r": 0.0, "trade_return_pct": 0.0, "trade_max_dd_pct": 0.0}
    eq = equity_from_r(x, risk_pct)
    ci = bootstrap_mean_ci(x, runs=runs, seed=seed)
    wins = int(np.count_nonzero(x > 0))
    return {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n,
        "expectancy_r": float(x.mean()),
        "ci95": [ci[0], ci[1]] if ci else None,
        "profit_factor": profit_factor(x),
        "total_r": float(x.sum()),
        "trade_return_pct": float(eq[-1] / eq[0] * 100.0 - 100.0),
        "trade_max_dd_pct": float(M.max_drawdown_pct(eq)),
    }


def equity_summary(equity: np.ndarray, times: np.ndarray, tf_s: int) -> dict[str, Any]:
    """Return, max drawdown and annualised Sharpe of a per-bar equity (or price) curve."""
    eq = np.asarray(equity, dtype=float)
    if eq.size < 2 or not np.isfinite(eq).all() or eq[0] <= 0:
        return {"return_pct": None, "max_dd_pct": None, "sharpe": None}
    rets = M.simple_returns(eq)
    ppy = M.periods_per_year(np.asarray(times, dtype=np.int64), int(tf_s))
    return {
        "return_pct": float(eq[-1] / eq[0] * 100.0 - 100.0),
        "max_dd_pct": float(M.max_drawdown_pct(eq)),
        "sharpe": M.sharpe_ratio(rets, ppy),
    }


def run_matrix_stats(R: np.ndarray, order_key: np.ndarray | None, risk_pct: float) -> dict[str, np.ndarray]:
    """Per-row stats of a Monte Carlo matrix of R multiples (runs × trades): mean, win rate, profit factor (NaN when a
    row has no losing trade) and the max drawdown of the closed-trade equity with the trades in `order_key` order."""
    R = np.asarray(R, dtype=float)
    if R.ndim != 2 or R.shape[1] == 0:
        empty = np.zeros(R.shape[0] if R.ndim == 2 else 0)
        return {"mean": empty, "win_rate": empty, "profit_factor": np.full(empty.size, np.nan), "max_dd": empty}
    if order_key is not None:
        order = np.argsort(np.asarray(order_key), axis=1, kind="stable")
        R = np.take_along_axis(R, order, axis=1)
    pos = np.where(R > 0, R, 0.0).sum(axis=1)
    neg = -np.where(R < 0, R, 0.0).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        pf = np.where(neg > 0, pos / neg, np.nan)
    growth = np.maximum(1.0 + float(risk_pct) / 100.0 * R, 1e-9)
    eq = np.concatenate([np.ones((R.shape[0], 1)), np.cumprod(growth, axis=1)], axis=1)
    peak = np.maximum.accumulate(eq, axis=1)
    dd = ((peak - eq) / peak * 100.0).max(axis=1)
    return {"mean": R.mean(axis=1), "win_rate": (R > 0).mean(axis=1), "profit_factor": pf, "max_dd": dd}


def quantiles(values: Any, qs: Sequence[float] = (5, 50, 95)) -> list[float] | None:
    x = finite_array(values)
    if x.size == 0:
        return None
    return [float(v) for v in np.percentile(x, list(qs))]


# ----------------------------------------------------------------------------- cross-asset dependence


def daily_log_returns(times: Any, closes: Any) -> dict[int, float]:
    """{UTC day index: log return from the previous day's last close} (only between consecutive days)."""
    t = np.asarray(times, dtype=np.int64)
    c = np.asarray(closes, dtype=float)
    if t.size < 2:
        return {}
    day = t // 86400
    last = np.append(np.nonzero(np.diff(day))[0], t.size - 1)  # last bar of each day
    days, close = day[last], c[last]
    out: dict[int, float] = {}
    for i in range(1, days.size):
        if days[i] - days[i - 1] == 1 and close[i - 1] > 0 and close[i] > 0:
            out[int(days[i])] = float(math.log(close[i] / close[i - 1]))
    return out


def mean_pairwise_correlation(series: Sequence[dict[int, float]], min_overlap: int = 30) -> tuple[float | None, int]:
    """Average Pearson correlation of the daily returns of every pair with ≥ `min_overlap` common days."""
    rhos: list[float] = []
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            common = sorted(set(series[i]) & set(series[j]))
            if len(common) < min_overlap:
                continue
            a = np.asarray([series[i][d] for d in common])
            b = np.asarray([series[j][d] for d in common])
            if a.std() <= 0 or b.std() <= 0:
                continue
            rhos.append(float(np.corrcoef(a, b)[0, 1]))
    if not rhos:
        return None, 0
    return float(np.mean(rhos)), len(rhos)


def effective_count(n: int, rho: float | None) -> float:
    """Effective number of independent assets for n equally correlated ones: n / (1 + (n − 1)·ρ) (ρ clipped to 0..1)."""
    if n <= 0:
        return 0.0
    if rho is None or not math.isfinite(rho):
        return float(n)
    r = min(max(float(rho), 0.0), 1.0)
    return float(n / (1.0 + (n - 1) * r))
