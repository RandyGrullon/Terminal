"""Causal technical-analysis primitives on numpy arrays.

Every function returns an array aligned with its input where the value at bar ``i`` only uses bars ``<= i``
(no centring, no global normalisation) and ``NaN`` during the warm-up.

Conventions
- Rolling windows are NaN-strict: a window containing a NaN yields NaN.
- Recursive filters (EMA, RMA, ...) start at the first index with ``n`` consecutive finite values, are seeded with
  the simple mean of that window (TA-Lib / TradingView style) and carry their state across later NaN gaps.
- Standard deviations are population (ddof=0) like TradingView's ``ta.stdev``.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

NAN = float("nan")


# ============================================================================= basics


def f64(x: Sequence[float] | np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def _n(n: float) -> int:
    return max(1, int(n))


def full_nan(m: int) -> np.ndarray:
    return np.full(int(m), NAN)


def shift(x: np.ndarray, k: int = 1) -> np.ndarray:
    """x[i - k] (k > 0 looks back); the first k values are NaN."""
    x = f64(x)
    k = int(k)
    out = full_nan(len(x))
    if k <= 0:
        return x.copy()
    if k < len(x):
        out[k:] = x[:-k]
    return out


def diff(x: np.ndarray, k: int = 1) -> np.ndarray:
    return f64(x) - shift(x, k)


def nz(x: np.ndarray, value: float = 0.0) -> np.ndarray:
    x = f64(x)
    return np.where(np.isfinite(x), x, value)


def safe_div(a: np.ndarray, b: np.ndarray, fill: float = NAN) -> np.ndarray:
    a = f64(a)
    b = f64(b)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    bad = ~np.isfinite(out) | (np.abs(b) < 1e-300)
    if np.any(bad):
        out = np.where(bad, fill, out)
    return out


def first_valid_run(x: np.ndarray, n: int) -> int | None:
    """First index i such that x[i-n+1 .. i] are all finite (None when there is no such window)."""
    x = f64(x)
    n = _n(n)
    if len(x) < n:
        return None
    ok = np.isfinite(x).astype(np.int64)
    c = np.concatenate(([0], np.cumsum(ok)))
    win = c[n:] - c[:-n]
    idx = np.flatnonzero(win == n)
    return int(idx[0]) + n - 1 if idx.size else None


def _windows(x: np.ndarray, n: int) -> np.ndarray | None:
    x = f64(x)
    if len(x) < n:
        return None
    return sliding_window_view(x, n)


def _rows(w: np.ndarray) -> np.ndarray:
    """C-contiguous copy of a sliding-window view: per-row reductions then use the same summation order whatever
    the number of rows, so a truncated series gives bit-identical values (exact causality, stable crossovers)."""
    return np.ascontiguousarray(w)


def _wdot(w: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Row-wise dot product ``w @ v`` without BLAS (its kernels group rows, which changes the rounding of the
    last rows depending on how many rows there are)."""
    return (_rows(w) * v).sum(axis=1)


# ============================================================================= rolling statistics


def rolling_sum(x: np.ndarray, n: int) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    m = len(x)
    out = full_nan(m)
    if m < n:
        return out
    bad = ~np.isfinite(x)
    # Offset by the FIRST finite value (improves precision of the cumulative sums). It must not depend on later
    # bars: a whole-array statistic (e.g. the median) changes the last bits of every sum when the series is
    # truncated, which flips exact comparisons downstream (crossovers) -> numerical lookahead.
    first = int(np.argmax(~bad)) if not bad.all() else -1
    offset = float(x[first]) if first >= 0 else 0.0
    xz = np.where(bad, 0.0, x - offset)
    c = np.concatenate(([0.0], np.cumsum(xz)))
    b = np.concatenate(([0], np.cumsum(bad)))
    s = c[n:] - c[:-n] + offset * n
    s[(b[n:] - b[:-n]) > 0] = NAN
    out[n - 1 :] = s
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    n = _n(n)
    return rolling_sum(x, n) / n


def rolling_std(x: np.ndarray, n: int, ddof: int = 0) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    out = full_nan(len(x))
    w = _windows(x, n)
    if w is None or n - ddof <= 0:
        return out
    out[n - 1 :] = _rows(w).std(axis=1, ddof=ddof)
    return out


def rolling_var(x: np.ndarray, n: int, ddof: int = 0) -> np.ndarray:
    s = rolling_std(x, n, ddof)
    return s * s


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    out = full_nan(len(x))
    w = _windows(x, n)
    if w is None:
        return out
    out[n - 1 :] = w.max(axis=1)
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    out = full_nan(len(x))
    w = _windows(x, n)
    if w is None:
        return out
    out[n - 1 :] = w.min(axis=1)
    return out


def bars_since_max(x: np.ndarray, n: int) -> np.ndarray:
    """Bars since the highest value of the last n bars (0 = current bar); ties -> most recent."""
    x = f64(x)
    n = _n(n)
    out = full_nan(len(x))
    w = _windows(x, n)
    if w is None:
        return out
    rev = w[:, ::-1]
    res = np.argmax(rev, axis=1).astype(float)
    res[~np.all(np.isfinite(w), axis=1)] = NAN
    out[n - 1 :] = res
    return out


def bars_since_min(x: np.ndarray, n: int) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    out = full_nan(len(x))
    w = _windows(x, n)
    if w is None:
        return out
    rev = w[:, ::-1]
    res = np.argmin(rev, axis=1).astype(float)
    res[~np.all(np.isfinite(w), axis=1)] = NAN
    out[n - 1 :] = res
    return out


def percent_rank(x: np.ndarray, n: int) -> np.ndarray:
    """Percentage (0..100) of the previous n values that are <= the current value (TradingView percentrank)."""
    x = f64(x)
    n = _n(n)
    m = len(x)
    out = full_nan(m)
    if m <= n:
        return out
    w = sliding_window_view(x[:-1], n)  # w[j] = x[j .. j+n-1], compared with x[j+n]
    cur = x[n:]
    valid = np.all(np.isfinite(w), axis=1) & np.isfinite(cur)
    res = (w <= cur[:, None]).sum(axis=1) * (100.0 / n)
    out[n:] = np.where(valid, res, NAN)
    return out


def rolling_rank_pct(x: np.ndarray, n: int, min_periods: int = 30) -> np.ndarray:
    """Causal percentile (0..1) of the current value among the last n values (current included).

    Uses an expanding window until ``n`` values exist (needs ``min_periods``). NaN values are ignored.
    """
    x = f64(x)
    m = len(x)
    n = _n(n)
    out = full_nan(m)
    if m == 0:
        return out
    pad = np.concatenate((np.full(n - 1, NAN), x))
    w = sliding_window_view(pad, n)  # w[i] = last n values up to i
    cur = x
    fin = np.isfinite(w)
    count = fin.sum(axis=1)
    le = ((w <= cur[:, None]) & fin).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        res = le / count
    ok = np.isfinite(cur) & (count >= max(1, int(min_periods)))
    out[ok] = res[ok]
    return out


def zscore(x: np.ndarray, n: int) -> np.ndarray:
    m = sma(x, n)
    s = rolling_std(x, n)
    return safe_div(f64(x) - m, s)


# ============================================================================= moving averages


def ema(x: np.ndarray, n: int, alpha: float | None = None) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    m = len(x)
    a = 2.0 / (n + 1.0) if alpha is None else float(alpha)
    s = first_valid_run(x, n)
    if s is None:
        return full_nan(m)
    res = [NAN] * m
    prev = float(np.mean(x[s - n + 1 : s + 1]))
    res[s] = prev
    xs = x.tolist()
    for i in range(s + 1, m):
        v = xs[i]
        if v == v:  # not NaN
            prev += a * (v - prev)
            res[i] = prev
    return np.array(res)


def rma(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's moving average (SMMA)."""
    n = _n(n)
    return ema(x, n, alpha=1.0 / n)


def wma(x: np.ndarray, n: int) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    out = full_nan(len(x))
    w = _windows(x, n)
    if w is None:
        return out
    weights = np.arange(1, n + 1, dtype=float)
    out[n - 1 :] = _wdot(w, weights) / weights.sum()
    return out


def hma(x: np.ndarray, n: int) -> np.ndarray:
    n = max(2, int(n))
    half = max(1, n // 2)
    root = max(1, int(round(math.sqrt(n))))
    return wma(2.0 * wma(x, half) - wma(x, n), root)


def dema(x: np.ndarray, n: int) -> np.ndarray:
    e1 = ema(x, n)
    e2 = ema(e1, n)
    return 2.0 * e1 - e2


def tema(x: np.ndarray, n: int) -> np.ndarray:
    e1 = ema(x, n)
    e2 = ema(e1, n)
    e3 = ema(e2, n)
    return 3.0 * e1 - 3.0 * e2 + e3


def trima(x: np.ndarray, n: int) -> np.ndarray:
    n = _n(n)
    a = (n + 1) // 2
    b = n // 2 + 1
    return sma(sma(x, a), b)


def kama(x: np.ndarray, n: int = 10, fast: int = 2, slow: int = 30) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    m = len(x)
    out = [NAN] * m
    er = efficiency_ratio(x, n)
    fast_sc = 2.0 / (max(1, fast) + 1.0)
    slow_sc = 2.0 / (max(1, slow) + 1.0)
    sc = (nz(er, 0.0) * (fast_sc - slow_sc) + slow_sc) ** 2
    start = first_valid_run(er, 1)
    if start is None:
        return np.array(out)
    xs = x.tolist()
    scs = sc.tolist()
    prev = xs[start]
    if prev != prev:
        return np.array(out)
    out[start] = prev
    for i in range(start + 1, m):
        v = xs[i]
        if v == v:
            prev += scs[i] * (v - prev)
            out[i] = prev
    return np.array(out)


def zlema(x: np.ndarray, n: int) -> np.ndarray:
    n = _n(n)
    lag = (n - 1) // 2
    x = f64(x)
    return ema(2.0 * x - shift(x, lag) if lag > 0 else x, n)


def alma(x: np.ndarray, n: int = 9, offset: float = 0.85, sigma: float = 6.0) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    out = full_nan(len(x))
    w = _windows(x, n)
    if w is None:
        return out
    m = offset * (n - 1)
    s = n / max(sigma, 1e-9)
    k = np.arange(n, dtype=float)
    weights = np.exp(-((k - m) ** 2) / (2.0 * s * s))
    out[n - 1 :] = _wdot(w, weights) / weights.sum()
    return out


def vwma(x: np.ndarray, volume: np.ndarray, n: int) -> np.ndarray:
    v = f64(volume)
    return safe_div(rolling_sum(f64(x) * v, n), rolling_sum(v, n))


def mcginley(x: np.ndarray, n: int = 14, k: float = 0.6) -> np.ndarray:
    x = f64(x)
    n = _n(n)
    m = len(x)
    out = [NAN] * m
    s = first_valid_run(x, n)
    if s is None:
        return np.array(out)
    prev = float(np.mean(x[s - n + 1 : s + 1]))
    out[s] = prev
    xs = x.tolist()
    for i in range(s + 1, m):
        v = xs[i]
        if v == v and prev != 0:
            ratio = v / prev
            denom = k * n * (ratio**4) if ratio > 0 else k * n
            prev = prev + (v - prev) / max(denom, 1e-12)
            out[i] = prev
    return np.array(out)


def t3(x: np.ndarray, n: int = 5, v: float = 0.7) -> np.ndarray:
    def gd(series: np.ndarray) -> np.ndarray:
        e1 = ema(series, n)
        e2 = ema(e1, n)
        return e1 * (1.0 + v) - e2 * v

    return gd(gd(gd(f64(x))))


def linreg_stats(y: np.ndarray, n: int) -> dict[str, np.ndarray]:
    """Rolling least-squares fit of y against 0..n-1 over the last n bars.

    Returns slope, intercept, value (fit at the current bar), r2, tstat (slope / se) and resid_std.
    """
    y = f64(y)
    n = max(3, int(n))
    m = len(y)
    keys = ("slope", "intercept", "value", "r2", "tstat", "resid_std")
    out = {k: full_nan(m) for k in keys}
    w = _windows(y, n)
    if w is None:
        return out
    w = _rows(w)
    xk = np.arange(n, dtype=float)
    xm = xk.mean()
    sxx = float(((xk - xm) ** 2).sum())
    ym = w.mean(axis=1)
    slope = _wdot(w - ym[:, None], xk - xm) / sxx
    intercept = ym - slope * xm
    fit = intercept[:, None] + slope[:, None] * xk[None, :]
    resid = w - fit
    ssr = (resid * resid).sum(axis=1)
    sst = ((w - ym[:, None]) ** 2).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        r2 = np.where(sst > 0, 1.0 - ssr / sst, 0.0)
        se = np.sqrt(ssr / (n - 2) / sxx)
        tstat = np.where(se > 0, slope / se, np.sign(slope) * 50.0)
    out["slope"][n - 1 :] = slope
    out["intercept"][n - 1 :] = intercept
    out["value"][n - 1 :] = intercept + slope * (n - 1)
    out["r2"][n - 1 :] = np.clip(r2, 0.0, 1.0)
    out["tstat"][n - 1 :] = np.clip(tstat, -50.0, 50.0)
    out["resid_std"][n - 1 :] = np.sqrt(ssr / n)
    return out


def lsma(x: np.ndarray, n: int) -> np.ndarray:
    return linreg_stats(x, n)["value"]


def ma(kind: str, x: np.ndarray, n: int, volume: np.ndarray | None = None) -> np.ndarray:
    kind = (kind or "EMA").upper()
    if kind == "SMA":
        return sma(x, n)
    if kind == "WMA":
        return wma(x, n)
    if kind in ("RMA", "SMMA"):
        return rma(x, n)
    if kind == "HMA":
        return hma(x, n)
    if kind == "VWMA" and volume is not None:
        return vwma(x, volume, n)
    return ema(x, n)


# ============================================================================= volatility


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high, low, close = f64(high), f64(low), f64(close)
    pc = shift(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    if len(tr):
        tr[0] = high[0] - low[0]
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    return rma(true_range(high, low, close), n)


def bollinger(x: np.ndarray, n: int = 20, k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(x, n)
    sd = rolling_std(x, n)
    return mid, mid + k * sd, mid - k * sd, sd


def keltner(high, low, close, n: int = 20, mult: float = 2.0, atr_n: int = 10):
    mid = ema(close, n)
    a = atr(high, low, close, atr_n)
    return mid, mid + mult * a, mid - mult * a


def donchian(high: np.ndarray, low: np.ndarray, n: int = 20) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    up = rolling_max(high, n)
    lo = rolling_min(low, n)
    return up, lo, (up + lo) / 2.0


def supertrend(high, low, close, n: int = 10, mult: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    """(line, direction) with direction +1 (alcista) / -1 (bajista)."""
    high, low, close = f64(high), f64(low), f64(close)
    m = len(close)
    a = atr(high, low, close, n)
    hl2 = (high + low) / 2.0
    up_b = (hl2 - mult * a).tolist()
    dn_b = (hl2 + mult * a).tolist()
    cl = close.tolist()
    line = [NAN] * m
    direction = [NAN] * m
    fu = fd = fu_prev = fd_prev = NAN
    d = 1.0
    started = False
    for i in range(m):
        ub, db = up_b[i], dn_b[i]
        if ub != ub or db != db:
            continue
        if not started:
            fu, fd, d, started = ub, db, 1.0, True
        else:
            pc = cl[i - 1]
            fu = max(ub, fu) if pc > fu else ub
            fd = min(db, fd) if pc < fd else db
            if d < 0 and cl[i] > fd_prev:
                d = 1.0
            elif d > 0 and cl[i] < fu_prev:
                d = -1.0
        line[i] = fu if d > 0 else fd
        direction[i] = d
        fu_prev, fd_prev = fu, fd
    return np.array(line), np.array(direction)


def psar(high, low, start: float = 0.02, inc: float = 0.02, maximum: float = 0.2) -> np.ndarray:
    high, low = f64(high), f64(low)
    m = len(high)
    out = [NAN] * m
    if m < 2:
        return np.array(out)
    hs, ls = high.tolist(), low.tolist()
    up = hs[1] >= hs[0]
    af = start
    ep = hs[1] if up else ls[1]
    sar = ls[0] if up else hs[0]
    out[1] = sar
    for i in range(2, m):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, ls[i - 1], ls[i - 2])
            if ls[i] < sar:
                up, sar, ep, af = False, ep, ls[i], start
            elif hs[i] > ep:
                ep, af = hs[i], min(af + inc, maximum)
        else:
            sar = max(sar, hs[i - 1], hs[i - 2])
            if hs[i] > sar:
                up, sar, ep, af = True, ep, hs[i], start
            elif ls[i] < ep:
                ep, af = ls[i], min(af + inc, maximum)
        out[i] = sar
    return np.array(out)


def chandelier(high, low, close, n: int = 22, mult: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    a = atr(high, low, close, n)
    return rolling_max(high, n) - mult * a, rolling_min(low, n) + mult * a


# ============================================================================= momentum / oscillators


def rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    x = f64(x)
    d = diff(x, 1)
    up = rma(np.where(np.isfinite(d), np.maximum(d, 0.0), NAN), n)
    dn = rma(np.where(np.isfinite(d), np.maximum(-d, 0.0), NAN), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = up / dn
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where((dn == 0) & (up > 0), 100.0, out)
    out = np.where((dn == 0) & (up == 0), 50.0, out)
    return np.where(np.isfinite(up) & np.isfinite(dn), out, NAN)


def macd(x: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(x, fast) - ema(x, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def stoch(high, low, close, n: int = 14, smooth_k: int = 3, d: int = 3) -> tuple[np.ndarray, np.ndarray]:
    hh = rolling_max(high, n)
    ll = rolling_min(low, n)
    raw = 100.0 * safe_div(f64(close) - ll, hh - ll, fill=NAN)
    raw = np.where(np.isfinite(hh) & np.isfinite(ll) & ~np.isfinite(raw), 50.0, raw)
    k = sma(raw, smooth_k)
    return k, sma(k, d)


def stochrsi(x: np.ndarray, rsi_n: int = 14, stoch_n: int = 14, k: int = 3, d: int = 3) -> tuple[np.ndarray, np.ndarray]:
    r = rsi(x, rsi_n)
    hh = rolling_max(r, stoch_n)
    ll = rolling_min(r, stoch_n)
    raw = 100.0 * safe_div(r - ll, hh - ll, fill=NAN)
    raw = np.where(np.isfinite(hh) & np.isfinite(ll) & ~np.isfinite(raw), 50.0, raw)
    kk = sma(raw, k)
    return kk, sma(kk, d)


def cci(high, low, close, n: int = 20) -> np.ndarray:
    tp = (f64(high) + f64(low) + f64(close)) / 3.0
    n = _n(n)
    out = full_nan(len(tp))
    w = _windows(tp, n)
    if w is None:
        return out
    w = _rows(w)
    mean = w.mean(axis=1)
    md = np.abs(w - mean[:, None]).mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        val = np.where(md > 0, (tp[n - 1 :] - mean) / (0.015 * md), 0.0)
    out[n - 1 :] = val
    return out


def willr(high, low, close, n: int = 14) -> np.ndarray:
    hh = rolling_max(high, n)
    ll = rolling_min(low, n)
    out = -100.0 * safe_div(hh - f64(close), hh - ll, fill=NAN)
    return np.where(np.isfinite(hh) & np.isfinite(ll) & ~np.isfinite(out), -50.0, out)


def roc(x: np.ndarray, n: int = 10) -> np.ndarray:
    return 100.0 * (safe_div(f64(x), shift(x, n)) - 1.0)


def mom(x: np.ndarray, n: int = 10) -> np.ndarray:
    return f64(x) - shift(x, n)


def adx_dmi(high, low, close, n: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(+DI, -DI, ADX)."""
    high, low, close = f64(high), f64(low), f64(close)
    up = diff(high, 1)
    dn = -diff(low, 1)
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pdm[0] = NAN
    mdm[0] = NAN
    tr = true_range(high, low, close)
    tr[0] = NAN
    atr_n = rma(tr, n)
    pdi = 100.0 * safe_div(rma(pdm, n), atr_n, fill=0.0)
    mdi = 100.0 * safe_div(rma(mdm, n), atr_n, fill=0.0)
    valid = np.isfinite(atr_n)
    pdi = np.where(valid, pdi, NAN)
    mdi = np.where(valid, mdi, NAN)
    dx = 100.0 * safe_div(np.abs(pdi - mdi), pdi + mdi, fill=0.0)
    dx = np.where(valid, dx, NAN)
    return pdi, mdi, rma(dx, n)


def aroon(high, low, n: int = 14) -> tuple[np.ndarray, np.ndarray]:
    n = _n(n)
    up = 100.0 * (n - bars_since_max(high, n + 1)) / n
    dn = 100.0 * (n - bars_since_min(low, n + 1)) / n
    return up, dn


def vortex(high, low, close, n: int = 14) -> tuple[np.ndarray, np.ndarray]:
    high, low, close = f64(high), f64(low), f64(close)
    vmp = np.abs(high - shift(low, 1))
    vmm = np.abs(low - shift(high, 1))
    tr = true_range(high, low, close)
    tr[0] = NAN
    trs = rolling_sum(tr, n)
    return safe_div(rolling_sum(vmp, n), trs), safe_div(rolling_sum(vmm, n), trs)


def choppiness(high, low, close, n: int = 14) -> np.ndarray:
    tr = true_range(high, low, close)
    rng = rolling_max(high, n) - rolling_min(low, n)
    ratio = safe_div(rolling_sum(tr, n), rng)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100.0 * np.log10(ratio) / math.log10(max(2, int(n)))


def mass_index(high, low, ema_n: int = 9, sum_n: int = 25) -> np.ndarray:
    hl = f64(high) - f64(low)
    e1 = ema(hl, ema_n)
    e2 = ema(e1, ema_n)
    return rolling_sum(safe_div(e1, e2), sum_n)


def trix(x: np.ndarray, n: int = 18) -> np.ndarray:
    e3 = ema(ema(ema(f64(x), n), n), n)
    return 100.0 * (safe_div(e3, shift(e3, 1)) - 1.0)


def tsi(x: np.ndarray, long: int = 25, short: int = 13) -> np.ndarray:
    pc = diff(x, 1)
    num = ema(ema(pc, long), short)
    den = ema(ema(np.abs(pc), long), short)
    out = 100.0 * safe_div(num, den, fill=0.0)
    return np.where(np.isfinite(num) & np.isfinite(den), out, NAN)


def ultimate(high, low, close, a: int = 7, b: int = 14, c: int = 28) -> np.ndarray:
    high, low, close = f64(high), f64(low), f64(close)
    pc = shift(close, 1)
    lo = np.fmin(low, pc)
    hi = np.fmax(high, pc)
    bp = close - lo
    tr = hi - lo
    bp[0] = NAN
    tr[0] = NAN

    def avg(k: int) -> np.ndarray:
        sb, st = rolling_sum(bp, k), rolling_sum(tr, k)
        return np.where(np.isfinite(sb) & np.isfinite(st), safe_div(sb, st, fill=0.5), NAN)

    return 100.0 * (4.0 * avg(a) + 2.0 * avg(b) + avg(c)) / 7.0


def awesome(high, low, fast: int = 5, slow: int = 34) -> np.ndarray:
    hl2 = (f64(high) + f64(low)) / 2.0
    return sma(hl2, fast) - sma(hl2, slow)


def cmo(x: np.ndarray, n: int = 14) -> np.ndarray:
    d = diff(x, 1)
    up = rolling_sum(np.where(np.isfinite(d), np.maximum(d, 0.0), NAN), n)
    dn = rolling_sum(np.where(np.isfinite(d), np.maximum(-d, 0.0), NAN), n)
    out = 100.0 * safe_div(up - dn, up + dn, fill=0.0)
    return np.where(np.isfinite(up) & np.isfinite(dn), out, NAN)


def efficiency_ratio(x: np.ndarray, n: int = 10) -> np.ndarray:
    x = f64(x)
    change = np.abs(x - shift(x, n))
    vol = rolling_sum(np.abs(diff(x, 1)), n)
    er = safe_div(change, vol, fill=0.0)
    return np.where(np.isfinite(change) & np.isfinite(vol), np.clip(er, 0.0, 1.0), NAN)


def swma(x: np.ndarray) -> np.ndarray:
    """Symmetric weighted MA (1, 2, 2, 1) / 6 over 4 bars."""
    x = f64(x)
    out = full_nan(len(x))
    w = _windows(x, 4)
    if w is None:
        return out
    out[3:] = _wdot(w, np.array([1.0, 2.0, 2.0, 1.0])) / 6.0
    return out


def streak(x: np.ndarray) -> np.ndarray:
    """Consecutive up (+k) / down (-k) closes; 0 when unchanged."""
    xs = f64(x).tolist()
    m = len(xs)
    out = [NAN] * m
    if m == 0:
        return np.array(out)
    s = 0.0
    out[0] = 0.0
    for i in range(1, m):
        a, b = xs[i], xs[i - 1]
        if a != a or b != b:
            out[i] = NAN
            continue
        if a > b:
            s = s + 1.0 if s > 0 else 1.0
        elif a < b:
            s = s - 1.0 if s < 0 else -1.0
        else:
            s = 0.0
        out[i] = s
    return np.array(out)


def fisher(high, low, n: int = 9) -> tuple[np.ndarray, np.ndarray]:
    hl2 = (f64(high) + f64(low)) / 2.0
    hh = rolling_max(hl2, n).tolist()
    ll = rolling_min(hl2, n).tolist()
    src = hl2.tolist()
    m = len(src)
    fish = [NAN] * m
    trig = [NAN] * m
    v = 0.0
    f = 0.0
    started = False
    for i in range(m):
        h, lo = hh[i], ll[i]
        if h != h or lo != lo:
            continue
        rng = h - lo
        pos = (src[i] - lo) / rng - 0.5 if rng > 0 else 0.0
        v = 0.66 * pos + 0.67 * v
        v = max(-0.999, min(0.999, v))
        prev_f = f
        f = 0.5 * math.log((1.0 + v) / (1.0 - v)) + 0.5 * f
        fish[i] = f
        trig[i] = prev_f if started else f
        started = True
    return np.array(fish), np.array(trig)


def schaff(x: np.ndarray, fast: int = 23, slow: int = 50, cycle: int = 10, factor: float = 0.5) -> np.ndarray:
    m_line = ema(x, fast) - ema(x, slow)
    lo1 = rolling_min(m_line, cycle).tolist()
    hi1 = rolling_max(m_line, cycle).tolist()
    ml = m_line.tolist()
    m = len(ml)
    out = [NAN] * m
    f1 = pf = f2 = pff = NAN
    for i in range(m):
        if hi1[i] != hi1[i] or lo1[i] != lo1[i] or ml[i] != ml[i]:
            continue
        rng = hi1[i] - lo1[i]
        f1 = (ml[i] - lo1[i]) / rng * 100.0 if rng > 0 else (f1 if f1 == f1 else 50.0)
        pf = f1 if pf != pf else pf + factor * (f1 - pf)
        out[i] = pf
    pf_arr = np.array(out)
    lo2 = rolling_min(pf_arr, cycle).tolist()
    hi2 = rolling_max(pf_arr, cycle).tolist()
    pfl = pf_arr.tolist()
    res = [NAN] * m
    for i in range(m):
        if hi2[i] != hi2[i] or lo2[i] != lo2[i] or pfl[i] != pfl[i]:
            continue
        rng = hi2[i] - lo2[i]
        f2 = (pfl[i] - lo2[i]) / rng * 100.0 if rng > 0 else (f2 if f2 == f2 else 50.0)
        pff = f2 if pff != pff else pff + factor * (f2 - pff)
        res[i] = pff
    return np.array(res)


# ============================================================================= volume


def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    d = np.sign(nz(diff(close, 1)))
    return np.cumsum(d * nz(volume))


def money_flow_multiplier(high, low, close) -> np.ndarray:
    high, low, close = f64(high), f64(low), f64(close)
    rng = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        mfm = np.where(rng > 0, ((close - low) - (high - close)) / rng, 0.0)
    return mfm


def ad_line(high, low, close, volume) -> np.ndarray:
    return np.cumsum(money_flow_multiplier(high, low, close) * nz(volume))


def cmf(high, low, close, volume, n: int = 20) -> np.ndarray:
    v = nz(volume)
    num = rolling_sum(money_flow_multiplier(high, low, close) * v, n)
    den = rolling_sum(v, n)
    return np.where(np.isfinite(num) & np.isfinite(den), safe_div(num, den, fill=0.0), NAN)


def mfi(high, low, close, volume, n: int = 14) -> np.ndarray:
    tp = (f64(high) + f64(low) + f64(close)) / 3.0
    rmf = tp * nz(volume)
    dtp = diff(tp, 1)
    pos = np.where(dtp > 0, rmf, 0.0)
    neg = np.where(dtp < 0, rmf, 0.0)
    pos[0] = NAN
    neg[0] = NAN
    ps = rolling_sum(pos, n)
    ns = rolling_sum(neg, n)
    out = 100.0 - 100.0 / (1.0 + safe_div(ps, ns, fill=np.inf))
    out = np.where((ns == 0) & (ps == 0), 50.0, out)
    return np.where(np.isfinite(ps) & np.isfinite(ns), out, NAN)


def force_index(close, volume, n: int = 13) -> np.ndarray:
    return ema(diff(close, 1) * nz(volume), n)


def ease_of_movement(high, low, volume, n: int = 14, divisor: float = 10000.0) -> np.ndarray:
    high, low = f64(high), f64(low)
    hl2 = (high + low) / 2.0
    dist = diff(hl2, 1)
    box = safe_div(nz(volume) / max(divisor, 1e-12), high - low, fill=NAN)
    return sma(safe_div(dist, box, fill=0.0) * np.where(np.isfinite(dist), 1.0, NAN), n)


def pvt(close, volume) -> np.ndarray:
    c = f64(close)
    r = nz(safe_div(c - shift(c, 1), shift(c, 1)))
    return np.cumsum(r * nz(volume))


def nvi_pvi(close, volume) -> tuple[np.ndarray, np.ndarray]:
    cs = f64(close).tolist()
    vs = nz(volume).tolist()
    m = len(cs)
    nvi = [NAN] * m
    pvi = [NAN] * m
    if m == 0:
        return np.array(nvi), np.array(pvi)
    n_val = p_val = 1000.0
    nvi[0] = pvi[0] = 1000.0
    for i in range(1, m):
        prev = cs[i - 1]
        r = (cs[i] - prev) / prev if prev else 0.0
        if vs[i] < vs[i - 1]:
            n_val *= 1.0 + r
        elif vs[i] > vs[i - 1]:
            p_val *= 1.0 + r
        nvi[i] = n_val
        pvi[i] = p_val
    return np.array(nvi), np.array(pvi)


def klinger(high, low, close, volume, fast: int = 34, slow: int = 55, signal: int = 13) -> tuple[np.ndarray, np.ndarray]:
    hs, ls, cs = f64(high), f64(low), f64(close)
    hlc = ((hs + ls + cs) / 3.0).tolist()
    dm = (hs - ls).tolist()
    vs = nz(volume).tolist()
    m = len(hlc)
    vf = [NAN] * m
    trend_prev = 0.0
    cm = 0.0
    for i in range(1, m):
        trend = 1.0 if hlc[i] > hlc[i - 1] else -1.0
        cm = cm + dm[i] if trend == trend_prev else dm[i - 1] + dm[i]
        trend_prev = trend
        ratio = 2.0 * (dm[i] / cm) - 1.0 if cm > 0 else -1.0
        vf[i] = vs[i] * abs(ratio) * trend * 100.0
    vf_arr = np.array(vf)
    ko = ema(vf_arr, fast) - ema(vf_arr, slow)
    return ko, ema(ko, signal)


# ============================================================================= statistics


def hurst(x: np.ndarray, n: int = 100, max_lag: int = 20) -> np.ndarray:
    """Rolling Hurst exponent from the scaling of lagged differences of log prices (std ~ lag^H)."""
    x = f64(x)
    m = len(x)
    out = full_nan(m)
    with np.errstate(divide="ignore", invalid="ignore"):
        lp = np.log(x)
    n = max(20, int(n))
    lags = sorted({lag for lag in (2, 3, 4, 6, 8, 12, 16, 20, 24, 32) if lag <= max(2, int(max_lag)) and lag < n // 2})
    if len(lags) < 3 or m < n:
        return out
    logs = []
    for lag in lags:
        d = lp - shift(lp, lag)
        logs.append(np.log(rolling_std(d, n - lag) + 1e-300))
    y = np.ascontiguousarray(np.vstack(logs).T)  # (m, L)
    lx = np.log(np.array(lags, dtype=float))
    lxm = lx - lx.mean()
    slope = _wdot(y - y.mean(axis=1, keepdims=True), lxm) / float((lxm * lxm).sum())
    valid = np.all(np.isfinite(y), axis=1)
    out[valid] = np.clip(slope[valid], 0.0, 1.0)
    return out


def hist_volatility(close: np.ndarray, n: int = 20, periods_per_year: float = 365.0) -> np.ndarray:
    c = f64(close)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(c / shift(c, 1))
    return rolling_std(lr, n, ddof=1) * math.sqrt(max(periods_per_year, 1.0)) * 100.0


def ulcer_index(close: np.ndarray, n: int = 14) -> np.ndarray:
    c = f64(close)
    mx = rolling_max(c, n)
    dd = 100.0 * safe_div(c - mx, mx)
    return np.sqrt(sma(dd * dd, n))


def chaikin_volatility(high, low, n: int = 10, roc_n: int = 10) -> np.ndarray:
    e = ema(f64(high) - f64(low), n)
    return roc(e, roc_n)


# ============================================================================= structure


def pivots(high: np.ndarray, low: np.ndarray, left: int = 5, right: int = 5) -> dict[str, np.ndarray]:
    """Swing pivots known causally.

    A pivot high at bar j (high[j] is the maximum of [j-left, j+right] and strictly above the left side) is only
    confirmed at bar c = j + right. Returns arrays of the confirmation index ``c`` (sorted), the pivot bar ``j``
    and the price, for highs and lows.
    """
    high, low = f64(high), f64(low)
    m = len(high)
    left = max(1, int(left))
    right = max(1, int(right))
    span = left + right + 1
    out = {k: np.zeros(0, dtype=np.int64) for k in ("hi_conf", "hi_bar", "lo_conf", "lo_bar")}
    out["hi_price"] = np.zeros(0)
    out["lo_price"] = np.zeros(0)
    if m < span:
        return out
    wh = sliding_window_view(high, span)  # wh[s] covers s .. s+span-1, centre j = s + left
    wl = sliding_window_view(low, span)
    centre_h = wh[:, left]
    centre_l = wl[:, left]
    left_h = wh[:, :left].max(axis=1)
    right_h = wh[:, left + 1 :].max(axis=1)
    left_l = wl[:, :left].min(axis=1)
    right_l = wl[:, left + 1 :].min(axis=1)
    is_ph = (centre_h > left_h) & (centre_h >= right_h)
    is_pl = (centre_l < left_l) & (centre_l <= right_l)
    s_h = np.flatnonzero(is_ph)
    s_l = np.flatnonzero(is_pl)
    out["hi_bar"] = (s_h + left).astype(np.int64)
    out["hi_conf"] = (s_h + left + right).astype(np.int64)
    out["hi_price"] = centre_h[s_h].astype(float)
    out["lo_bar"] = (s_l + left).astype(np.int64)
    out["lo_conf"] = (s_l + left + right).astype(np.int64)
    out["lo_price"] = centre_l[s_l].astype(float)
    return out


def decay_events(events: np.ndarray, decay: float) -> np.ndarray:
    """Causal memory of +-1 events: value = event when it fires, else previous * decay."""
    ev = nz(events).tolist()
    m = len(ev)
    out = [0.0] * m
    prev = 0.0
    for i in range(m):
        e = ev[i]
        prev = e if e != 0.0 else prev * decay
        out[i] = prev
    return np.array(out)


def crossover(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a, b = f64(a), f64(b)
    pa, pb = shift(a, 1), shift(b, 1)
    return (a > b) & (pa <= pb)


def crossunder(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a, b = f64(a), f64(b)
    pa, pb = shift(a, 1), shift(b, 1)
    return (a < b) & (pa >= pb)
