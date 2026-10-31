"""Vectorized, causal feature precomputation for the NEXUS engine.

Every array is aligned with the candles and the value at bar ``i`` only uses bars ``<= i`` (higher-timeframe values
become visible only once their bucket has closed). NaN marks the warm-up of each indicator.

Resumable chain: the value of a bar never depends on where the window starts once its recursive filters (EMA,
Wilder averages, Supertrend bands, OBV, the higher-timeframe EMA/ATR) are given: `feature_state(f, i)` reads them
after bar i and `compute_features(cs, resume)` continues from them, so a run that starts at that bar reproduces the
bits of the run that stored them. Finite windows are reduced window by window (`exact` sums), never with running
sums whose rounding depends on the first bar.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.engine import ta
from app.util import TF_SECONDS, tf_seconds

HTF_FACTOR = 4
WARMUP_BARS = 60
SR_MAX_AGE = 250
PIVOT_LEFT = 5
PIVOT_RIGHT = 3
ATR_RANK_BARS = 200
SEED_BARS = 200  # longest recursive-filter warm-up (EMA 200): no restore point exists before it after a cold start
# bars between a stored restore point of the recursive filters and the first bar a resumed run processes: every
# finite window of a processed bar (support/resistance pivots up to SR_MAX_AGE bars back, the ATR percentile over
# ATR_RANK_BARS) then lies inside the bars recomputed from that state
RESUME_LOOKBACK = max(SR_MAX_AGE + PIVOT_LEFT + 1, ATR_RANK_BARS + 1)
REC_KEYS = ("ema_fast", "ema_slow", "ema_mid", "ema_trend", "macd_fast", "macd_slow", "macd_signal", "rsi_up",
            "rsi_dn", "atr", "adx_tr", "adx_pdm", "adx_mdm", "adx", "st_atr", "st_up", "st_dn", "st_dir",
            "htf_ema", "htf_atr")


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
    # internal arrays of the recursive filters (feature_state) and the first bar whose higher-timeframe bucket is
    # complete in this window ("htf_from")
    rec: dict = field(default_factory=dict, repr=False)

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


def _htf_trend(time: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, tf_s: int,
               init: tuple[float, float] | None = None) -> tuple[np.ndarray, tuple | None]:
    """Higher-timeframe trend in [-1, 1] aligned causally: a bucket is visible only after it closes. Also returns
    (bucket ids, HTF EMA, HTF ATR) for feature_state. `init` = (EMA, ATR) after the first bucket of the window
    (a resumed window starts exactly at the first bar of a bucket)."""
    n = len(close)
    out = np.full(n, np.nan)
    if n < HTF_FACTOR * 3:
        return out, None
    k = HTF_FACTOR * tf_s
    bucket = time // k
    uniq, first = np.unique(bucket, return_index=True)
    last = np.append(first[1:], n) - 1
    h_close = close[last]
    h_high = np.maximum.reduceat(high, first)
    h_low = np.minimum.reduceat(low, first)
    h_ema = ta.ema(h_close, 20, init=None if init is None else init[0])
    h_atr = ta.atr(h_high, h_low, h_close, 10, init=None if init is None else init[1])
    dist = ta.safe_div(h_close - h_ema, h_atr)
    slope = ta.safe_div(h_ema - ta.shift(h_ema, 3), h_atr)
    trend = np.clip(0.5 * np.tanh(dist / 1.5) + 0.5 * np.tanh(slope / 1.0), -1.0, 1.0)
    pos = np.searchsorted(uniq, bucket)
    complete = (time + tf_s) >= (bucket + 1) * k
    hidx = np.where(complete, pos, pos - 1)
    ok = hidx >= 0
    out[ok] = trend[hidx[ok]]
    return out, (uniq, h_ema, h_atr)


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


def _carry_swing(levels: np.ndarray, conf: np.ndarray, k: int, value: float | None) -> None:
    """From bar k until the first pivot confirmed after it, the swing level in effect is the stored one (a pivot
    of any age, possibly older than this window)."""
    if not 0 <= k < len(levels):
        return
    nxt = int(np.searchsorted(conf, k, side="right"))
    end = int(conf[nxt]) if nxt < len(conf) else len(levels)
    levels[k:end] = np.nan if value is None else float(value)


def compute_features(cs, resume: dict | None = None) -> Features:
    """All indicator arrays needed by the engine (vectorized, causal).

    `resume` continues a stored chain instead of starting cold: "rec" = feature_state() taken at the window's first
    bar (its recursive filters continue from there), "swing" = (k, low, high) = the swing levels in effect after
    bar k of this window."""
    n = len(cs)
    tf = str(cs.tf)
    try:
        tf_s = tf_seconds(tf)
    except Exception:
        tf_s = int(np.median(np.diff(cs.time))) if n > 1 else 3600
    t = np.asarray(cs.time, dtype=np.int64)
    o, h, lo, c = (np.asarray(a, dtype=np.float64) for a in (cs.open, cs.high, cs.low, cs.close))
    v = np.nan_to_num(np.asarray(cs.volume, dtype=np.float64), nan=0.0)
    rec = (resume or {}).get("rec") if n else None

    def init(*keys: str):
        if rec is None:
            return None
        return rec[keys[0]] if len(keys) == 1 else tuple(rec[k] for k in keys)

    atr = ta.atr(h, lo, c, 14, init=init("atr"))
    macd_line, macd_sig, macd_hist, macd_fast, macd_slow = ta.macd_parts(
        c, 12, 26, 9, init=init("macd_fast", "macd_slow", "macd_signal"))
    pdi, mdi, adx, adx_tr, adx_pdm, adx_mdm = ta.adx_dmi_parts(h, lo, c, 14,
                                                              init=init("adx_tr", "adx_pdm", "adx_mdm", "adx"))
    bb_mid, bb_up, bb_lo, bb_sd = ta.bollinger(c, 20, 2.0, exact=True)
    st_line, st_dir, st_atr, st_up, st_dn = ta.supertrend_parts(h, lo, c, 10, 3.0,
                                                                init=init("st_atr", "st_up", "st_dn", "st_dir"))
    lr = ta.linreg_stats(c, 20)
    rsi, rsi_up, rsi_dn = ta.rsi_parts(c, 14, init=init("rsi_up", "rsi_dn"))
    srsi_k, srsi_d = ta.stoch_of(rsi, 14, 3, 3, exact=True)
    atr_pct = ta.safe_div(atr * 100.0, c)
    dc = ta.diff(c, 1)
    sigma = ta.rolling_std(dc, 50)
    sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, atr * 0.8)

    has_volume = bool(np.nanmax(v) > 0) if n else False
    obv = None
    if has_volume:
        vol_z = ta.zscore(np.log1p(v), 50, exact=True)
        obv = ta.obv(c, v, init=None if rec is None else rec.get("obv"))
        vol_mean = ta.sma(v, 20, exact=True)
        obv_slope = ta.safe_div(obv - ta.shift(obv, 10), vol_mean * 10.0)
    else:
        vol_z = np.full(n, np.nan)
        obv_slope = np.full(n, np.nan)

    piv = ta.pivots(h, lo, PIVOT_LEFT, PIVOT_RIGHT)
    div_bear, div_bull = _divergences(h, lo, rsi)
    warmup = min(max(WARMUP_BARS, 0), max(n - 1, 0))
    htf_trend, htf_parts = _htf_trend(t, h, lo, c, int(tf_s), init=init("htf_ema", "htf_atr"))
    swing_low = _last_known(piv["lo_conf"], piv["lo_price"], n)
    swing_high = _last_known(piv["hi_conf"], piv["hi_price"], n)
    swing = (resume or {}).get("swing")
    if swing is not None:
        _carry_swing(swing_low, piv["lo_conf"], int(swing[0]), swing[1])
        _carry_swing(swing_high, piv["hi_conf"], int(swing[0]), swing[2])
    if rec is not None:
        htf_from = 0  # the window starts at the first bar of a bucket and its HTF state was given
    else:
        k = HTF_FACTOR * int(tf_s)
        change = np.flatnonzero((t[1:] // k) != (t[:-1] // k)) if n > 1 else np.zeros(0, dtype=np.int64)
        htf_from = int(change[0]) + 1 if change.size else n  # the first bucket may be partial in a cold window

    return Features(
        n=n, tf=tf, tf_s=int(tf_s), time=t, open=o, high=h, low=lo, close=c, volume=v,
        ema_fast=ta.ema(c, 9, init=init("ema_fast")), ema_slow=ta.ema(c, 21, init=init("ema_slow")),
        ema_mid=ta.ema(c, 55, init=init("ema_mid")), ema_trend=ta.ema(c, 200, init=init("ema_trend")),
        rsi=rsi, macd=macd_line, macd_signal=macd_sig, macd_hist=macd_hist,
        atr=atr, atr_pct=atr_pct, atr_rank=ta.rolling_rank_pct(atr_pct, ATR_RANK_BARS, min_periods=30),
        sigma=sigma, adx=adx, pdi=pdi, mdi=mdi,
        bb_mid=bb_mid, bb_z=ta.safe_div(c - bb_mid, bb_sd), bb_width=ta.safe_div(bb_up - bb_lo, bb_mid),
        st_line=st_line, st_dir=st_dir,
        dc_hi=ta.shift(ta.rolling_max(h, 20), 1), dc_lo=ta.shift(ta.rolling_min(lo, 20), 1),
        srsi_k=srsi_k, srsi_d=srsi_d, obv_slope=obv_slope, vol_z=vol_z,
        er=ta.efficiency_ratio(c, 20, exact=True), lr_t=lr["tstat"], lr_slope=lr["slope"],
        range_atr=ta.safe_div(h - lo, ta.shift(atr, 1)),
        htf_trend=htf_trend, htf_label=_htf_label(tf, int(tf_s)),
        div_bear=div_bear, div_bull=div_bull, swing_low=swing_low, swing_high=swing_high,
        piv_hi_conf=piv["hi_conf"], piv_hi_bar=piv["hi_bar"], piv_hi_price=piv["hi_price"],
        piv_lo_conf=piv["lo_conf"], piv_lo_bar=piv["lo_bar"], piv_lo_price=piv["lo_price"],
        warmup=warmup,
        rec={"macd_fast": macd_fast, "macd_slow": macd_slow, "rsi_up": rsi_up, "rsi_dn": rsi_dn, "adx_tr": adx_tr,
             "adx_pdm": adx_pdm, "adx_mdm": adx_mdm, "st_atr": st_atr, "st_up": st_up, "st_dn": st_dn, "obv": obv,
             "htf": htf_parts, "htf_from": htf_from},
    )


def feature_state(f: Features, i: int) -> dict | None:
    """Recursive filter states after bar i (JSON-ready floats), from which `compute_features(window starting at
    bar i, resume={"rec": ...})` reproduces every later value bit for bit. None when bar i cannot be a restore
    point: a filter has not started yet, or bar i is not the first bar of a higher-timeframe bucket that is
    complete in this window."""
    i = int(i)
    if not 0 <= i < f.n or i < int(f.rec.get("htf_from", f.n)) or f.rec.get("htf") is None:
        return None
    k = HTF_FACTOR * int(f.tf_s)
    b = int(f.time[i]) // k
    if i > 0 and int(f.time[i - 1]) // k == b:
        return None
    uniq, h_ema, h_atr = f.rec["htf"]
    pos = int(np.searchsorted(uniq, b))
    if pos >= len(uniq) or int(uniq[pos]) != b:
        return None
    r = f.rec
    vals = {
        "ema_fast": f.ema_fast[i], "ema_slow": f.ema_slow[i], "ema_mid": f.ema_mid[i], "ema_trend": f.ema_trend[i],
        "macd_fast": r["macd_fast"][i], "macd_slow": r["macd_slow"][i], "macd_signal": f.macd_signal[i],
        "rsi_up": r["rsi_up"][i], "rsi_dn": r["rsi_dn"][i], "atr": f.atr[i], "adx_tr": r["adx_tr"][i],
        "adx_pdm": r["adx_pdm"][i], "adx_mdm": r["adx_mdm"][i], "adx": f.adx[i], "st_atr": r["st_atr"][i],
        "st_up": r["st_up"][i], "st_dn": r["st_dn"][i], "st_dir": f.st_dir[i], "htf_ema": h_ema[pos],
        "htf_atr": h_atr[pos],
    }
    out = {key: float(vals[key]) for key in REC_KEYS}
    if not all(np.isfinite(list(out.values()))) or out["st_dir"] not in (1.0, -1.0):
        return None
    obv = r.get("obv")
    out["obv"] = float(obv[i]) if obv is not None and np.isfinite(obv[i]) else None
    out["time"] = int(f.time[i])
    return out


def restore_point(f: Features, anchor: int, lookback: int = RESUME_LOOKBACK) -> tuple[int, dict] | None:
    """(bar, feature_state) of the latest restore point at least `lookback` bars before the bar after `anchor`
    (None when there is none in this window: a resumed run then restarts cold at this window's start)."""
    hi = int(anchor) + 1 - int(lookback)
    for i in range(hi, max(hi - 8 * HTF_FACTOR, -1), -1):
        state = feature_state(f, i)
        if state is not None:
            return i, state
    return None


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
