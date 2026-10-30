"""``ta.*`` namespace of PyChart: Pine-like technical analysis on bar-aligned numpy series.

Every function is causal (the value at bar i only uses bars <= i) and returns float64 arrays aligned with the
bars (NaN during warm-up); conditions return boolean arrays. Built on ``app.engine.ta``.

Pine conventions kept on purpose: ``bb``/``kc``/``donchian`` return ``(middle, upper, lower)``; ``macd`` returns
``(macd, signal, hist)``; ``dmi`` returns ``(plus_di, minus_di, adx)``; ``supertrend`` returns
``(line, direction)`` with direction -1 = alcista and +1 = bajista; ``highestbars``/``lowestbars`` return
non-positive offsets; ``tsi`` is in [-1, 1]; ``pivothigh``/``pivotlow`` report the pivot value on the bar where
it is confirmed (``right`` bars later).
"""
from __future__ import annotations

from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from app.engine import ta as T

from .runtime_base import Bars, ScriptRuntimeError, to_bool, to_float, to_int, to_series

NAN = float("nan")


def _windows(x: np.ndarray, n: int) -> np.ndarray | None:
    return sliding_window_view(x, n) if len(x) >= n else None


def _rolling(x: np.ndarray, n: int, fn) -> np.ndarray:
    """Applies a row-wise reducer over the trailing windows of length n (NaN-strict)."""
    out = np.full(len(x), NAN)
    w = _windows(x, n)
    if w is not None:
        with np.errstate(all="ignore"):
            out[n - 1 :] = fn(w)
    return out


def _pivot(src: np.ndarray, left: int, right: int, high: bool) -> np.ndarray:
    m = len(src)
    out = np.full(m, NAN)
    span = left + right + 1
    w = _windows(src, span)
    if w is None:
        return out
    centre = w[:, left]
    with np.errstate(invalid="ignore"):
        if high:
            lmax = w[:, :left].max(axis=1) if left else np.full(len(w), -np.inf)
            rmax = w[:, left + 1 :].max(axis=1) if right else np.full(len(w), -np.inf)
            hit = (centre > lmax) & (centre >= rmax)
        else:
            lmin = w[:, :left].min(axis=1) if left else np.full(len(w), np.inf)
            rmin = w[:, left + 1 :].min(axis=1) if right else np.full(len(w), np.inf)
            hit = (centre < lmin) & (centre <= rmin)
    hit &= np.all(np.isfinite(w), axis=1)
    idx = np.flatnonzero(hit)
    out[idx + left + right] = centre[idx]
    return out


class TA:
    """The ``ta`` object handed to scripts (read-only)."""

    __slots__ = ("_b",)

    def __init__(self, bars: Bars) -> None:
        object.__setattr__(self, "_b", bars)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ta es de solo lectura")

    def __repr__(self) -> str:
        return "<ta: análisis técnico de PyChart>"

    # -- helpers
    def _s(self, x: Any, what: str) -> np.ndarray:
        return to_series(x, self._b.n, what)

    @staticmethod
    def _n(x: Any, what: str) -> int:
        return to_int(x, what, minimum=1, maximum=100_000)

    def _src_len(self, fn: str, source: Any, length: Any, default_source: np.ndarray) -> tuple[np.ndarray, int]:
        """Pine overloads ``f(length)`` and ``f(source, length)``."""
        if length is None:
            if source is None or not np.isscalar(source):
                raise ScriptRuntimeError(f"ta.{fn}: indica la longitud, p. ej. ta.{fn}(20) o ta.{fn}(close, 20).")
            return default_source, self._n(source, f"ta.{fn}(length)")
        return self._s(source, f"ta.{fn}(source)"), self._n(length, f"ta.{fn}(length)")

    # ======================================================================== moving averages
    def sma(self, source: Any, length: Any) -> np.ndarray:
        return T.sma(self._s(source, "ta.sma(source)"), self._n(length, "ta.sma(length)"))

    def ema(self, source: Any, length: Any) -> np.ndarray:
        return T.ema(self._s(source, "ta.ema(source)"), self._n(length, "ta.ema(length)"))

    def wma(self, source: Any, length: Any) -> np.ndarray:
        return T.wma(self._s(source, "ta.wma(source)"), self._n(length, "ta.wma(length)"))

    def hma(self, source: Any, length: Any) -> np.ndarray:
        return T.hma(self._s(source, "ta.hma(source)"), self._n(length, "ta.hma(length)"))

    def rma(self, source: Any, length: Any) -> np.ndarray:
        return T.rma(self._s(source, "ta.rma(source)"), self._n(length, "ta.rma(length)"))

    def vwma(self, source: Any, length: Any) -> np.ndarray:
        return T.vwma(self._s(source, "ta.vwma(source)"), self._b.volume, self._n(length, "ta.vwma(length)"))

    def dema(self, source: Any, length: Any) -> np.ndarray:
        return T.dema(self._s(source, "ta.dema(source)"), self._n(length, "ta.dema(length)"))

    def tema(self, source: Any, length: Any) -> np.ndarray:
        return T.tema(self._s(source, "ta.tema(source)"), self._n(length, "ta.tema(length)"))

    def alma(self, source: Any, length: Any, offset: Any = 0.85, sigma: Any = 6.0) -> np.ndarray:
        return T.alma(
            self._s(source, "ta.alma(source)"),
            self._n(length, "ta.alma(length)"),
            to_float(offset, "ta.alma(offset)"),
            to_float(sigma, "ta.alma(sigma)"),
        )

    def swma(self, source: Any) -> np.ndarray:
        return T.swma(self._s(source, "ta.swma(source)"))

    def linreg(self, source: Any, length: Any, offset: Any = 0) -> np.ndarray:
        """Linear-regression value: intercept + slope * (length - 1 - offset) over the last ``length`` bars."""
        x = self._s(source, "ta.linreg(source)")
        n = self._n(length, "ta.linreg(length)")
        off = to_int(offset, "ta.linreg(offset)", minimum=None, maximum=None)
        out = np.full(len(x), NAN)
        w = _windows(x, n)
        if w is None:
            return out
        if n == 1:
            out[:] = x
            return out
        xk = np.arange(n, dtype=float)
        xm = xk.mean()
        sxx = float(((xk - xm) ** 2).sum())
        ym = w.mean(axis=1)
        slope = ((w - ym[:, None]) @ (xk - xm)) / sxx
        intercept = ym - slope * xm
        out[n - 1 :] = intercept + slope * (n - 1 - off)
        return out

    # ======================================================================== oscillators
    def rsi(self, source: Any, length: Any = 14) -> np.ndarray:
        return T.rsi(self._s(source, "ta.rsi(source)"), self._n(length, "ta.rsi(length)"))

    def macd(self, source: Any, fast: Any = 12, slow: Any = 26, signal: Any = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        line, sig, hist = T.macd(
            self._s(source, "ta.macd(source)"),
            self._n(fast, "ta.macd(fast)"),
            self._n(slow, "ta.macd(slow)"),
            self._n(signal, "ta.macd(signal)"),
        )
        return line, sig, hist

    def stoch(self, source: Any, high: Any, low: Any, length: Any = 14) -> np.ndarray:
        """Raw stochastic %K = 100 * (source - lowest(low)) / (highest(high) - lowest(low))."""
        src = self._s(source, "ta.stoch(source)")
        n = self._n(length, "ta.stoch(length)")
        hh = T.rolling_max(self._s(high, "ta.stoch(high)"), n)
        ll = T.rolling_min(self._s(low, "ta.stoch(low)"), n)
        out = 100.0 * T.safe_div(src - ll, hh - ll, fill=0.5)
        return np.where(np.isfinite(hh) & np.isfinite(ll) & np.isfinite(src), out, NAN)

    def cci(self, source: Any, length: Any = 20) -> np.ndarray:
        src = self._s(source, "ta.cci(source)")
        n = self._n(length, "ta.cci(length)")
        mean = T.sma(src, n)
        dev = self.dev(src, n)
        return np.where(np.isfinite(dev), T.safe_div(src - mean, 0.015 * dev, fill=0.0), NAN)

    def mfi(self, source: Any, length: Any = 14) -> np.ndarray:
        src = self._s(source, "ta.mfi(source)")
        n = self._n(length, "ta.mfi(length)")
        ch = T.diff(src, 1)
        vol = self._b.volume
        with np.errstate(invalid="ignore"):
            up = np.where(np.isfinite(ch), np.where(ch > 0, vol * src, 0.0), NAN)
            dn = np.where(np.isfinite(ch), np.where(ch < 0, vol * src, 0.0), NAN)
        su = T.rolling_sum(up, n)
        sd = T.rolling_sum(dn, n)
        out = 100.0 - 100.0 / (1.0 + T.safe_div(su, sd, fill=np.inf))
        out = np.where((sd == 0) & (su == 0), 50.0, out)
        return np.where(np.isfinite(su) & np.isfinite(sd), out, NAN)

    def roc(self, source: Any, length: Any) -> np.ndarray:
        src = self._s(source, "ta.roc(source)")
        prev = T.shift(src, self._n(length, "ta.roc(length)"))
        return 100.0 * T.safe_div(src - prev, prev)

    def mom(self, source: Any, length: Any) -> np.ndarray:
        return T.mom(self._s(source, "ta.mom(source)"), self._n(length, "ta.mom(length)"))

    def cmo(self, source: Any, length: Any) -> np.ndarray:
        return T.cmo(self._s(source, "ta.cmo(source)"), self._n(length, "ta.cmo(length)"))

    def tsi(self, source: Any, short_length: Any = 13, long_length: Any = 25) -> np.ndarray:
        src = self._s(source, "ta.tsi(source)")
        return T.tsi(src, self._n(long_length, "ta.tsi(long_length)"), self._n(short_length, "ta.tsi(short_length)")) / 100.0

    def wpr(self, length: Any = 14) -> np.ndarray:
        n = self._n(length, "ta.wpr(length)")
        hh = T.rolling_max(self._b.high, n)
        ll = T.rolling_min(self._b.low, n)
        out = -100.0 * T.safe_div(hh - self._b.close, hh - ll, fill=0.5)
        return np.where(np.isfinite(hh) & np.isfinite(ll), out, NAN)

    # ======================================================================== volatility & bands
    def tr(self, handle_na: Any = True) -> np.ndarray:
        """True range (``handle_na=False`` leaves the first bar as NaN, like Pine)."""
        out = T.true_range(self._b.high, self._b.low, self._b.close)
        if not handle_na and len(out):
            out[0] = NAN
        return out

    def atr(self, length: Any = 14) -> np.ndarray:
        return T.atr(self._b.high, self._b.low, self._b.close, self._n(length, "ta.atr(length)"))

    def stdev(self, source: Any, length: Any, biased: Any = True) -> np.ndarray:
        n = self._n(length, "ta.stdev(length)")
        return T.rolling_std(self._s(source, "ta.stdev(source)"), n, ddof=0 if biased else 1)

    def variance(self, source: Any, length: Any, biased: Any = True) -> np.ndarray:
        n = self._n(length, "ta.variance(length)")
        return T.rolling_var(self._s(source, "ta.variance(source)"), n, ddof=0 if biased else 1)

    def dev(self, source: Any, length: Any) -> np.ndarray:
        """Mean absolute deviation from the moving average."""
        x = self._s(source, "ta.dev(source)")
        n = self._n(length, "ta.dev(length)")
        return _rolling(x, n, lambda w: np.abs(w - w.mean(axis=1, keepdims=True)).mean(axis=1))

    def bb(self, source: Any, length: Any = 20, mult: Any = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        mid, up, lo, _ = T.bollinger(
            self._s(source, "ta.bb(source)"), self._n(length, "ta.bb(length)"), to_float(mult, "ta.bb(mult)")
        )
        return mid, up, lo

    def bbw(self, source: Any, length: Any = 20, mult: Any = 2.0) -> np.ndarray:
        mid, up, lo = self.bb(source, length, mult)
        return T.safe_div(up - lo, mid)

    def kc(self, source: Any, length: Any = 20, mult: Any = 1.5, use_true_range: Any = True) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = self._n(length, "ta.kc(length)")
        k = to_float(mult, "ta.kc(mult)")
        mid = T.ema(self._s(source, "ta.kc(source)"), n)
        span = self.tr(True) if use_true_range else self._b.high - self._b.low
        rng = T.ema(span, n)
        return mid, mid + k * rng, mid - k * rng

    def donchian(self, length: Any = 20) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        up, lo, mid = T.donchian(self._b.high, self._b.low, self._n(length, "ta.donchian(length)"))
        return mid, up, lo

    def supertrend(self, factor: Any = 3.0, atr_period: Any = 10) -> tuple[np.ndarray, np.ndarray]:
        line, direction = T.supertrend(
            self._b.high,
            self._b.low,
            self._b.close,
            self._n(atr_period, "ta.supertrend(atr_period)"),
            to_float(factor, "ta.supertrend(factor)"),
        )
        return line, -direction

    def sar(self, start: Any = 0.02, inc: Any = 0.02, maximum: Any = 0.2) -> np.ndarray:
        return T.psar(
            self._b.high,
            self._b.low,
            to_float(start, "ta.sar(start)"),
            to_float(inc, "ta.sar(inc)"),
            to_float(maximum, "ta.sar(maximum)"),
        )

    # ======================================================================== trend strength
    def dmi(self, di_length: Any = 14, adx_smoothing: Any = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = self._n(di_length, "ta.dmi(di_length)")
        s = self._n(adx_smoothing, "ta.dmi(adx_smoothing)")
        pdi, mdi, adx = T.adx_dmi(self._b.high, self._b.low, self._b.close, n)
        if s != n:
            dx = 100.0 * T.safe_div(np.abs(pdi - mdi), pdi + mdi, fill=0.0)
            adx = T.rma(np.where(np.isfinite(pdi), dx, NAN), s)
        return pdi, mdi, adx

    def adx(self, length: Any = 14) -> np.ndarray:
        return self.dmi(length, length)[2]

    # ======================================================================== volume
    def obv(self) -> np.ndarray:
        return T.obv(self._b.close, self._b.volume)

    def vwap(self, source: Any = None, anchor: Any = "D") -> np.ndarray:
        """Volume-weighted average price reset every UTC day ("D"), week ("W"), month ("M") or never (None)."""
        b = self._b
        src = (b.high + b.low + b.close) / 3.0 if source is None else self._s(source, "ta.vwap(source)")
        t = b.time
        key = str(anchor).upper() if anchor else ""
        if key == "D":
            group = t // 86400
        elif key == "W":
            group = (t // 86400 + 3) // 7
        elif key == "M":
            group = t.astype("datetime64[s]").astype("datetime64[M]").astype(np.int64)
        elif key == "":
            group = np.zeros(len(t), dtype=np.int64)
        else:
            raise ScriptRuntimeError("ta.vwap(anchor): usa 'D', 'W', 'M' o None.")
        ok = np.isfinite(src) & np.isfinite(b.volume)
        pv = np.cumsum(np.where(ok, src * b.volume, 0.0))
        vv = np.cumsum(np.where(ok, b.volume, 0.0))
        idx = np.arange(len(t))
        starts = np.ones(len(t), dtype=bool)
        starts[1:] = group[1:] != group[:-1]
        first = np.maximum.accumulate(np.where(starts, idx, 0))
        base_pv = np.where(first > 0, pv[np.maximum(first - 1, 0)], 0.0)
        base_vv = np.where(first > 0, vv[np.maximum(first - 1, 0)], 0.0)
        return T.safe_div(pv - base_pv, vv - base_vv)

    def cum(self, source: Any) -> np.ndarray:
        x = self._s(source, "ta.cum(source)")
        fin = np.isfinite(x)
        out = np.cumsum(np.where(fin, x, 0.0))
        out[np.cumsum(fin) == 0] = NAN
        return out

    # ======================================================================== extremes
    def highest(self, source: Any, length: Any = None) -> np.ndarray:
        src, n = self._src_len("highest", source, length, self._b.high)
        return T.rolling_max(src, n)

    def lowest(self, source: Any, length: Any = None) -> np.ndarray:
        src, n = self._src_len("lowest", source, length, self._b.low)
        return T.rolling_min(src, n)

    def highestbars(self, source: Any, length: Any = None) -> np.ndarray:
        src, n = self._src_len("highestbars", source, length, self._b.high)
        return -T.bars_since_max(src, n)

    def lowestbars(self, source: Any, length: Any = None) -> np.ndarray:
        src, n = self._src_len("lowestbars", source, length, self._b.low)
        return -T.bars_since_min(src, n)

    def pivothigh(self, source: Any, left: Any = None, right: Any = None) -> np.ndarray:
        if right is None:
            source, left, right = self._b.high, source, left
        return _pivot(
            self._s(source, "ta.pivothigh(source)"),
            to_int(left, "ta.pivothigh(leftbars)", minimum=0, maximum=10_000),
            to_int(right, "ta.pivothigh(rightbars)", minimum=0, maximum=10_000),
            True,
        )

    def pivotlow(self, source: Any, left: Any = None, right: Any = None) -> np.ndarray:
        if right is None:
            source, left, right = self._b.low, source, left
        return _pivot(
            self._s(source, "ta.pivotlow(source)"),
            to_int(left, "ta.pivotlow(leftbars)", minimum=0, maximum=10_000),
            to_int(right, "ta.pivotlow(rightbars)", minimum=0, maximum=10_000),
            False,
        )

    # ======================================================================== events
    def change(self, source: Any, length: Any = 1) -> np.ndarray:
        return T.diff(self._s(source, "ta.change(source)"), self._n(length, "ta.change(length)"))

    def crossover(self, a: Any, b: Any) -> np.ndarray:
        return T.crossover(self._s(a, "ta.crossover(a)"), self._s(b, "ta.crossover(b)"))

    def crossunder(self, a: Any, b: Any) -> np.ndarray:
        return T.crossunder(self._s(a, "ta.crossunder(a)"), self._s(b, "ta.crossunder(b)"))

    def cross(self, a: Any, b: Any) -> np.ndarray:
        x, y = self._s(a, "ta.cross(a)"), self._s(b, "ta.cross(b)")
        return T.crossover(x, y) | T.crossunder(x, y)

    def rising(self, source: Any, length: Any) -> np.ndarray:
        x = self._s(source, "ta.rising(source)")
        prev = T.shift(T.rolling_max(x, self._n(length, "ta.rising(length)")), 1)
        with np.errstate(invalid="ignore"):
            return np.isfinite(prev) & (x > prev)

    def falling(self, source: Any, length: Any) -> np.ndarray:
        x = self._s(source, "ta.falling(source)")
        prev = T.shift(T.rolling_min(x, self._n(length, "ta.falling(length)")), 1)
        with np.errstate(invalid="ignore"):
            return np.isfinite(prev) & (x < prev)

    def barssince(self, condition: Any) -> np.ndarray:
        cond = to_bool(condition, self._b.n, "ta.barssince(condition)")
        idx = np.arange(len(cond))
        last = np.maximum.accumulate(np.where(cond, idx, -1))
        return np.where(last >= 0, (idx - last).astype(float), NAN)

    def valuewhen(self, condition: Any, source: Any, occurrence: Any = 0) -> np.ndarray:
        cond = to_bool(condition, self._b.n, "ta.valuewhen(condition)")
        src = self._s(source, "ta.valuewhen(source)")
        k = to_int(occurrence, "ta.valuewhen(occurrence)", minimum=0, maximum=100_000)
        pos = np.flatnonzero(cond)
        target = np.cumsum(cond) - 1 - k
        out = np.full(len(cond), NAN)
        ok = target >= 0
        out[ok] = src[pos[target[ok]]]
        return out

    # ======================================================================== statistics
    def correlation(self, source1: Any, source2: Any, length: Any) -> np.ndarray:
        a = self._s(source1, "ta.correlation(source1)")
        b = self._s(source2, "ta.correlation(source2)")
        n = self._n(length, "ta.correlation(length)")
        out = np.full(len(a), NAN)
        wa, wb = _windows(a, n), _windows(b, n)
        if wa is None or wb is None or n < 2:
            return out
        da = wa - wa.mean(axis=1, keepdims=True)
        db = wb - wb.mean(axis=1, keepdims=True)
        with np.errstate(all="ignore"):
            res = (da * db).sum(axis=1) / np.sqrt((da * da).sum(axis=1) * (db * db).sum(axis=1))
        out[n - 1 :] = np.where(np.isfinite(res), np.clip(res, -1.0, 1.0), NAN)
        return out

    def percentrank(self, source: Any, length: Any) -> np.ndarray:
        return T.percent_rank(self._s(source, "ta.percentrank(source)"), self._n(length, "ta.percentrank(length)"))

    def median(self, source: Any, length: Any) -> np.ndarray:
        x = self._s(source, "ta.median(source)")
        return _rolling(x, self._n(length, "ta.median(length)"), lambda w: np.median(w, axis=1))


# --- end of section B: ta part 2 ---
