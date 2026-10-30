"""Market regime detection: trend_up | trend_down | range | volatile (causal, with hysteresis)."""
from __future__ import annotations

import numpy as np

from app.engine.features import Features
from app.engine.fmt import REGIME_LABEL

REGIMES: tuple[str, ...] = ("trend_up", "trend_down", "range", "volatile")
TREND_UP, TREND_DOWN, RANGE, VOLATILE = 0, 1, 2, 3
UNKNOWN = -1
CONFIRM_BARS = 2


def regime_name(code: int) -> str:
    return REGIMES[code] if 0 <= code < len(REGIMES) else "unknown"


def regime_label(name: str) -> str:
    return REGIME_LABEL.get(name, REGIME_LABEL["unknown"])


def raw_regime(f: Features) -> np.ndarray:
    """Per-bar regime before hysteresis (UNKNOWN while the core indicators warm up)."""
    with np.errstate(invalid="ignore"):
        adx = np.nan_to_num(f.adx, nan=0.0)
        er = np.nan_to_num(f.er, nan=0.0)
        lr_t = np.nan_to_num(f.lr_t, nan=0.0)
        rank = np.nan_to_num(f.atr_rank, nan=0.5)
        up_bias = ((f.ema_fast > f.ema_slow) & (f.close > f.ema_slow)) | (lr_t > 2.5)
        down_bias = ((f.ema_fast < f.ema_slow) & (f.close < f.ema_slow)) | (lr_t < -2.5)
        strong = ((adx >= 20.0) & (er >= 0.20)) | (er >= 0.35) | (np.abs(lr_t) >= 3.5)
        clean_trend = (er >= 0.35) & (adx >= 25.0)
        volatile = (rank >= 0.90) & ~clean_trend
    out = np.full(f.n, RANGE, dtype=np.int64)
    out[strong & up_bias & ~down_bias] = TREND_UP
    out[strong & down_bias & ~up_bias] = TREND_DOWN
    out[volatile] = VOLATILE
    ready = np.isfinite(f.adx) & np.isfinite(f.er) & np.isfinite(f.ema_slow) & np.isfinite(f.atr)
    out[~ready] = UNKNOWN
    return out


def detect_regime(f: Features) -> np.ndarray:
    """Regime codes with hysteresis: a new trend/range regime must hold CONFIRM_BARS bars (volatile switches at once)."""
    raw = raw_regime(f).tolist()
    out = [UNKNOWN] * len(raw)
    cur = UNKNOWN
    pending = UNKNOWN
    count = 0
    for i, r in enumerate(raw):
        if r == UNKNOWN:
            cur, pending, count = UNKNOWN, UNKNOWN, 0
        elif cur == UNKNOWN or r == VOLATILE or r == cur:
            cur, pending, count = r, UNKNOWN, 0
        else:
            if r == pending:
                count += 1
            else:
                pending, count = r, 1
            if count >= CONFIRM_BARS:
                cur, pending, count = r, UNKNOWN, 0
        out[i] = cur
    return np.asarray(out, dtype=np.int64)


def regime_detail(f: Features, i: int) -> str:
    """'ADX 27 · eficiencia 0,41 · ATR p62' (Spanish, compact)."""
    from app.engine import fmt

    adx = f.value("adx", i)
    er = f.value("er", i)
    rank = f.value("atr_rank", i)
    parts = []
    if adx == adx:
        parts.append(f"ADX {fmt.num(adx, 0)}")
    if er == er:
        parts.append(f"eficiencia {fmt.num(er, 2)}")
    if rank == rank:
        parts.append(f"volatilidad p{fmt.num(rank * 100, 0)}")
    return " · ".join(parts)
