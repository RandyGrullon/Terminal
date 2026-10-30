"""Vectorized, causal feature precomputation for the NEXUS engine.

Every array is aligned with the candles and the value at bar ``i`` only uses bars ``<= i`` (higher-timeframe values
become visible only once their bucket has closed). NaN marks the warm-up of each indicator.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.engine import ta
from app.util import TF_SECONDS, tf_seconds

HTF_FACTOR = 4
WARMUP_BARS = 60
SR_MAX_AGE = 250
PIVOT_LEFT = 5
PIVOT_RIGHT = 3


@dataclass
class Features:
    n: int
    tf: str
    tf_s: int
    time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    ema_fast: np.ndarray
    ema_slow: np.ndarray
    ema_mid: np.ndarray
    ema_trend: np.ndarray
    rsi: np.ndarray
    macd: np.ndarray
    macd_signal: np.ndarray
    macd_hist: np.ndarray
    atr: np.ndarray
    atr_pct: np.ndarray
    atr_rank: np.ndarray
    sigma: np.ndarray
    adx: np.ndarray
    pdi: np.ndarray
    mdi: np.ndarray
    bb_mid: np.ndarray
    bb_z: np.ndarray
    bb_width: np.ndarray
    st_line: np.ndarray
    st_dir: np.ndarray
    dc_hi: np.ndarray
    dc_lo: np.ndarray
    srsi_k: np.ndarray
    srsi_d: np.ndarray
    obv_slope: np.ndarray
    vol_z: np.ndarray
    er: np.ndarray
    lr_t: np.ndarray
    lr_slope: np.ndarray
    range_atr: np.ndarray
    htf_trend: np.ndarray
    htf_label: str
    div_bear: np.ndarray
    div_bull: np.ndarray
    swing_low: np.ndarray
    swing_high: np.ndarray
    piv_hi_conf: np.ndarray
    piv_hi_bar: np.ndarray
    piv_hi_price: np.ndarray
    piv_lo_conf: np.ndarray
    piv_lo_bar: np.ndarray
    piv_lo_price: np.ndarray
    warmup: int

    def value(self, name: str, i: int, default: float = float("nan")) -> float:
        """Scalar of a feature array at bar i (default when NaN)."""
        v = float(getattr(self, name)[i])
        return v if v == v else default


def _htf_label(tf: str, tf_s: int) -> str:
    target = tf_s * HTF_FACTOR
    for name, secs in TF_SECONDS.items():
        if secs == target:
            return name
    return f"{tf}×{HTF_FACTOR}"


def _last_known(conf: np.ndarray, values: np.ndarray, n: int) -> np.ndarray:
    """Per bar: the value of the latest event confirmed at index <= i (NaN before the first one)."""
    out = np.full(n, np.nan)
    if len(conf) == 0 or n == 0:
        return out
    idx = np.searchsorted(conf, np.arange(n), side="right") - 1
    ok = idx >= 0
    out[ok] = values[idx[ok]]
    return out


def _htf_trend(time: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, tf_s: int) -> np.ndarray:
    """Higher-timeframe trend in [-1, 1] aligned causally: a bucket is visible only after it closes."""
    n = len(close)
    out = np.full(n, np.nan)
    if n < HTF_FACTOR * 3:
        return out
    k = HTF_FACTOR * tf_s
    bucket = time // k
    uniq, first = np.unique(bucket, return_index=True)
    last = np.append(first[1:], n) - 1
    h_close = close[last]
    h_high = np.maximum.reduceat(high, first)
    h_low = np.minimum.reduceat(low, first)
    h_ema = ta.ema(h_close, 20)
    h_atr = ta.atr(h_high, h_low, h_close, 10)
    dist = ta.safe_div(h_close - h_ema, h_atr)
    slope = ta.safe_div(h_ema - ta.shift(h_ema, 3), h_atr)
    trend = np.clip(0.5 * np.tanh(dist / 1.5) + 0.5 * np.tanh(slope / 1.0), -1.0, 1.0)
    pos = np.searchsorted(uniq, bucket)
    complete = (time + tf_s) >= (bucket + 1) * k
    hidx = np.where(complete, pos, pos - 1)
    ok = hidx >= 0
    out[ok] = trend[hidx[ok]]
    return out


def _divergences(high: np.ndarray, low: np.ndarray, rsi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bearish: price makes a higher high while RSI makes a lower high (mirror for bullish)."""
    px_hi_recent = ta.rolling_max(high, 10)
    px_hi_prev = ta.shift(ta.rolling_max(high, 20), 10)
    rsi_hi_recent = ta.rolling_max(rsi, 10)
    rsi_hi_prev = ta.shift(ta.rolling_max(rsi, 20), 10)
    px_lo_recent = ta.rolling_min(low, 10)
    px_lo_prev = ta.shift(ta.rolling_min(low, 20), 10)
    rsi_lo_recent = ta.rolling_min(rsi, 10)
    rsi_lo_prev = ta.shift(ta.rolling_min(rsi, 20), 10)
    with np.errstate(invalid="ignore"):
        bear = (px_hi_recent > px_hi_prev) & (rsi_hi_recent < rsi_hi_prev - 3.0) & (rsi_hi_prev > 60.0)
        bull = (px_lo_recent < px_lo_prev) & (rsi_lo_recent > rsi_lo_prev + 3.0) & (rsi_lo_prev < 40.0)
    return np.asarray(bear, dtype=bool), np.asarray(bull, dtype=bool)


def compute_features(cs) -> Features:
    """All indicator arrays needed by the engine (vectorized, causal)."""
    n = len(cs)
    tf = str(cs.tf)
    try:
        tf_s = tf_seconds(tf)
    except Exception:
        tf_s = int(np.median(np.diff(cs.time))) if n > 1 else 3600
    t = np.asarray(cs.time, dtype=np.int64)
    o, h, lo, c = (np.asarray(a, dtype=np.float64) for a in (cs.open, cs.high, cs.low, cs.close))
    v = np.nan_to_num(np.asarray(cs.volume, dtype=np.float64), nan=0.0)

    atr = ta.atr(h, lo, c, 14)
    macd_line, macd_sig, macd_hist = ta.macd(c, 12, 26, 9)
    pdi, mdi, adx = ta.adx_dmi(h, lo, c, 14)
    bb_mid, bb_up, bb_lo, bb_sd = ta.bollinger(c, 20, 2.0)
    st_line, st_dir = ta.supertrend(h, lo, c, 10, 3.0)
    srsi_k, srsi_d = ta.stochrsi(c, 14, 14, 3, 3)
    lr = ta.linreg_stats(c, 20)
    rsi = ta.rsi(c, 14)
    atr_pct = ta.safe_div(atr * 100.0, c)
    dc = ta.diff(c, 1)
    sigma = ta.rolling_std(dc, 50)
    sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, atr * 0.8)

    has_volume = bool(np.nanmax(v) > 0) if n else False
    if has_volume:
        vol_z = ta.zscore(np.log1p(v), 50)
        obv = ta.obv(c, v)
        vol_mean = ta.sma(v, 20)
        obv_slope = ta.safe_div(obv - ta.shift(obv, 10), vol_mean * 10.0)
    else:
        vol_z = np.full(n, np.nan)
        obv_slope = np.full(n, np.nan)

    piv = ta.pivots(h, lo, PIVOT_LEFT, PIVOT_RIGHT)
    div_bear, div_bull = _divergences(h, lo, rsi)
    warmup = min(max(WARMUP_BARS, 0), max(n - 1, 0))

    return Features(
        n=n, tf=tf, tf_s=int(tf_s), time=t, open=o, high=h, low=lo, close=c, volume=v,
        ema_fast=ta.ema(c, 9), ema_slow=ta.ema(c, 21), ema_mid=ta.ema(c, 55), ema_trend=ta.ema(c, 200),
        rsi=rsi, macd=macd_line, macd_signal=macd_sig, macd_hist=macd_hist,
        atr=atr, atr_pct=atr_pct, atr_rank=ta.rolling_rank_pct(atr_pct, 200, min_periods=30), sigma=sigma,
        adx=adx, pdi=pdi, mdi=mdi,
        bb_mid=bb_mid, bb_z=ta.safe_div(c - bb_mid, bb_sd), bb_width=ta.safe_div(bb_up - bb_lo, bb_mid),
        st_line=st_line, st_dir=st_dir,
        dc_hi=ta.shift(ta.rolling_max(h, 20), 1), dc_lo=ta.shift(ta.rolling_min(lo, 20), 1),
        srsi_k=srsi_k, srsi_d=srsi_d, obv_slope=obv_slope, vol_z=vol_z,
        er=ta.efficiency_ratio(c, 20), lr_t=lr["tstat"], lr_slope=lr["slope"],
        range_atr=ta.safe_div(h - lo, ta.shift(atr, 1)),
        htf_trend=_htf_trend(t, h, lo, c, int(tf_s)), htf_label=_htf_label(tf, int(tf_s)),
        div_bear=div_bear, div_bull=div_bull,
        swing_low=_last_known(piv["lo_conf"], piv["lo_price"], n),
        swing_high=_last_known(piv["hi_conf"], piv["hi_price"], n),
        piv_hi_conf=piv["hi_conf"], piv_hi_bar=piv["hi_bar"], piv_hi_price=piv["hi_price"],
        piv_lo_conf=piv["lo_conf"], piv_lo_bar=piv["lo_bar"], piv_lo_price=piv["lo_price"],
        warmup=warmup,
    )


def sr_levels(f: Features, i: int, price: float, atr: float, k: int = 3) -> tuple[list[float], list[float]]:
    """Nearest supports (below price, descending) and resistances (above, ascending) from confirmed pivots."""
    lo_bound = i - SR_MAX_AGE
    levels: list[np.ndarray] = []
    for conf, bar, prices in ((f.piv_hi_conf, f.piv_hi_bar, f.piv_hi_price), (f.piv_lo_conf, f.piv_lo_bar, f.piv_lo_price)):
        if len(conf) == 0:
            continue
        m = int(np.searchsorted(conf, i, side="right"))
        if m == 0:
            continue
        sel = bar[:m] >= lo_bound
        levels.append(prices[:m][sel])
    if not levels:
        return [], []
    allp = np.concatenate(levels)
    allp = allp[np.isfinite(allp)]
    tol = max(atr * 0.25, abs(price) * 1e-6) if atr == atr and atr > 0 else abs(price) * 0.002
    sup = _cluster(np.sort(allp[allp < price])[::-1], tol)[:k]
    res = _cluster(np.sort(allp[allp > price]), tol)[:k]
    return sup, res


def _cluster(sorted_levels: np.ndarray, tol: float) -> list[float]:
    """Merge levels closer than tol (keeps the first of each cluster, i.e. the nearest to price)."""
    out: list[float] = []
    for p in sorted_levels.tolist():
        if not out or abs(p - out[-1]) > tol:
            out.append(float(p))
    return out
