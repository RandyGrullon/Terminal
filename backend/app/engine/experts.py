"""NEXUS experts: each one is a causal signal in [-1, 1] computed vectorized for every bar."""
from __future__ import annotations

import numpy as np

from app.engine import ta
from app.engine.features import Features
from app.engine.regime import RANGE, TREND_DOWN, TREND_UP, VOLATILE

EXPERTS: list[dict[str, str]] = [
    {"id": "trend_ribbon", "label": "Cinta de EMAs",
     "description": "Alineación de las EMA 9/21/55/200 y posición del precio respecto a ellas."},
    {"id": "macd", "label": "Momento MACD",
     "description": "Histograma MACD 12/26/9 normalizado por ATR y lado de la línea MACD."},
    {"id": "rsi", "label": "RSI adaptativo",
     "description": "RSI 14: momento a favor en tendencia, reversión a la media en mercado lateral."},
    {"id": "bollinger", "label": "Bandas de Bollinger",
     "description": "Distancia z a la media de 20: continuación en tendencia, reversión en lateral."},
    {"id": "donchian", "label": "Ruptura Donchian",
     "description": "Cierre por encima/debajo del canal de 20 velas (memoria que decae)."},
    {"id": "supertrend", "label": "Supertrend",
     "description": "Dirección del Supertrend 10/3 ponderada por la distancia a la línea."},
    {"id": "adx_dmi", "label": "ADX / DMI",
     "description": "Diferencia +DI/−DI ponderada por la fuerza de la tendencia (ADX)."},
    {"id": "volume_obv", "label": "Volumen / OBV",
     "description": "Pendiente del OBV normalizada: el volumen confirma la dirección del precio."},
    {"id": "regression", "label": "Pendiente de regresión",
     "description": "Estadístico t de la pendiente de la regresión lineal de 20 velas."},
    {"id": "htf_trend", "label": "Tendencia mayor",
     "description": "Tendencia en la temporalidad ×4 (solo velas mayores ya cerradas)."},
    {"id": "stochrsi", "label": "Cruce StochRSI",
     "description": "Cruces de %K/%D en sobreventa (compra) o sobrecompra (venta)."},
    {"id": "price_action", "label": "Acción del precio",
     "description": "Velas envolventes y de rechazo cerca de soportes/resistencias."},
]
EXPERT_IDS: list[str] = [e["id"] for e in EXPERTS]
EXPERT_LABEL: dict[str, str] = {e["id"]: e["label"] for e in EXPERTS}
N_EXPERTS = len(EXPERTS)


def _nz(x: np.ndarray) -> np.ndarray:
    return np.where(np.isfinite(x), x, 0.0)


def _sgn(x: np.ndarray) -> np.ndarray:
    return np.sign(_nz(x))


def _trend_ribbon(f: Features) -> np.ndarray:
    parts = [_sgn(f.ema_fast - f.ema_slow), _sgn(f.ema_slow - f.ema_mid), _sgn(f.close - f.ema_slow)]
    has200 = np.isfinite(f.ema_trend)
    long_term = np.where(has200, _sgn(f.ema_mid - f.ema_trend), 0.0)
    count = 3.0 + has200.astype(float)
    align = (parts[0] + parts[1] + parts[2] + long_term) / count
    strength = np.tanh(_nz(ta.safe_div(np.abs(f.close - f.ema_mid), f.atr)) / 2.0)
    return align * (0.5 + 0.5 * strength)


def _macd(f: Features) -> np.ndarray:
    hist = np.tanh(_nz(ta.safe_div(f.macd_hist, 0.3 * f.atr)))
    return np.clip(0.6 * hist + 0.4 * _sgn(f.macd), -1.0, 1.0)


def _rsi(f: Features, regime: np.ndarray) -> np.ndarray:
    r = _nz(f.rsi - 50.0)
    mom = np.clip(r / 25.0, -1.0, 1.0)
    mr = np.clip(-r / 20.0, -1.0, 1.0) * (np.abs(r) > 15.0)
    trend = (regime == TREND_UP) | (regime == TREND_DOWN)
    out = np.where(trend, mom, np.where(regime == RANGE, mr, 0.5 * mr))
    return np.where(np.isfinite(f.rsi), out, 0.0)


def _bollinger(f: Features, regime: np.ndarray) -> np.ndarray:
    z = _nz(f.bb_z)
    trend = (regime == TREND_UP) | (regime == TREND_DOWN)
    return np.where(trend, np.clip(z / 2.5, -1.0, 1.0), -np.clip(z / 2.0, -1.0, 1.0))


def _donchian(f: Features) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        ev = np.where(f.close > f.dc_hi, 1.0, np.where(f.close < f.dc_lo, -1.0, 0.0))
    return ta.decay_events(ev, 0.9)


def _supertrend(f: Features) -> np.ndarray:
    dist = _nz(ta.safe_div(np.abs(f.close - f.st_line), 2.0 * f.atr))
    return _nz(f.st_dir) * np.clip(0.5 + dist, 0.0, 1.0)


def _adx_dmi(f: Features) -> np.ndarray:
    di = _nz(ta.safe_div(f.pdi - f.mdi, f.pdi + f.mdi))
    return np.clip(di * np.clip(_nz(f.adx) / 25.0, 0.0, 1.0), -1.0, 1.0)


def _volume_obv(f: Features) -> np.ndarray:
    damp = np.where(_nz(f.vol_z) > -1.0, 1.0, 0.5)
    return np.clip(_nz(f.obv_slope) * 2.0, -1.0, 1.0) * damp


def _regression(f: Features) -> np.ndarray:
    return np.tanh(_nz(f.lr_t) / 3.0)


def _stochrsi(f: Features) -> np.ndarray:
    k, d = f.srsi_k, f.srsi_d
    up = ta.crossover(k, d) & (_nz(k) < 25.0)
    dn = ta.crossunder(k, d) & (_nz(k) > 75.0)
    return ta.decay_events(np.where(up, 1.0, np.where(dn, -1.0, 0.0)), 0.85)


def _price_action(f: Features) -> np.ndarray:
    o, h, lo, c = f.open, f.high, f.low, f.close
    po, pc = ta.shift(o, 1), ta.shift(c, 1)
    body = np.abs(c - o)
    rng = np.maximum(h - lo, 1e-12)
    lower_wick = np.minimum(o, c) - lo
    upper_wick = h - np.maximum(o, c)
    atr = f.atr
    with np.errstate(invalid="ignore"):
        near_sup = np.abs(lo - f.swing_low) <= 1.0 * atr
        near_res = np.abs(h - f.swing_high) <= 1.0 * atr
        bull_engulf = (c > o) & (pc < po) & (c >= po) & (o <= pc) & (body > 0.5 * atr)
        bear_engulf = (c < o) & (pc > po) & (c <= po) & (o >= pc) & (body > 0.5 * atr)
        bull_pin = (lower_wick >= 2.0 * body) & ((c - lo) / rng >= 0.66) & (rng > 0.8 * atr)
        bear_pin = (upper_wick >= 2.0 * body) & ((h - c) / rng >= 0.66) & (rng > 0.8 * atr)
    bull = np.where(bull_engulf, 1.0, np.where(bull_pin, 0.7, 0.0)) * np.where(near_sup, 1.0, 0.5)
    bear = np.where(bear_engulf, 1.0, np.where(bear_pin, 0.7, 0.0)) * np.where(near_res, 1.0, 0.5)
    return ta.decay_events(np.clip(bull - bear, -1.0, 1.0), 0.75)


def expert_signals(f: Features, regime: np.ndarray) -> np.ndarray:
    """Matrix (n, N_EXPERTS) of expert signals in [-1, 1]; zero during each expert's warm-up."""
    cols = [
        _trend_ribbon(f),
        _macd(f),
        _rsi(f, regime),
        _bollinger(f, regime),
        _donchian(f),
        _supertrend(f),
        _adx_dmi(f),
        _volume_obv(f),
        _regression(f),
        _nz(f.htf_trend),
        _stochrsi(f),
        _price_action(f),
    ]
    out = np.column_stack(cols) if f.n else np.zeros((0, N_EXPERTS))
    out = np.clip(np.where(np.isfinite(out), out, 0.0), -1.0, 1.0)
    out[regime < 0] = 0.0  # nothing is trusted before the regime (core indicators) is known
    return out


def volatile_mask(regime: np.ndarray) -> np.ndarray:
    return regime == VOLATILE
