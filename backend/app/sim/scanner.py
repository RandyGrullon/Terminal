"""Market scanner & market-wide learning (ScannerService, `rt.scanner`).

Quick mode (every asset of a market): one TradingView scan pulling technical columns at several timeframes →
normalised features → online logistic model per "market|tf|horizon_s" (AdaGrad + L2, persisted in
`model_state` kind='scanner') → P(up) and score = 2p−1. While the model is cold (< 200 resolved outcomes) its
probability is blended with a rule-based prior (ratings + trend alignment + momentum) and the reasons say so.
Every scanned asset gets a `predictions` row that the PredictionEvaluator resolves after `scanner.horizon_s`,
feeding the model (feedback loop → honest out-of-sample hit rate).

Deep mode: the top candidates with real history run the full NEXUS engine + Trade Gate (`rt.nexus.analyze`) →
verdict TOMAR / ESPERAR / NO TOMAR, SL/TP, regime, expected move; every gate decision is logged in `gate_log`.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app import config
from app.sim.common import (
    VERDICT_LABEL,
    fmt_num,
    fmt_pct,
    fmt_prob,
    gate_checks_vector,
    gate_line,
    json_dumps,
    json_loads,
    rating_label,
)
from app.util import RateLimitedLog, TTLCache, clamp, clean_json, normalize_tf, now_ms, now_s, parse_symbol, safe_float, tf_seconds

# ----------------------------------------------------------------------------- TradingView columns

BASE_COLUMNS = [
    "name", "description", "type", "exchange", "close", "change", "volume",
    "Recommend.All", "Recommend.MA", "Recommend.Other", "RSI", "RSI[1]", "Stoch.K", "Stoch.D", "CCI20",
    "ADX", "ADX+DI", "ADX-DI", "AO", "Mom", "MACD.macd", "MACD.signal", "BB.upper", "BB.lower",
    "EMA20", "EMA50", "EMA200", "SMA50", "SMA200", "ATR", "Volatility.D", "Perf.W", "Perf.1M",
    "average_volume_10d_calc", "relative_volume_10d_calc",
]
MTF_COLUMNS = [
    "RSI|15", "RSI|60", "RSI|240", "Recommend.All|15", "Recommend.All|60", "Recommend.All|240",
    "Recommend.All|1W", "MACD.macd|60", "MACD.signal|60", "MACD.macd|240", "MACD.signal|240",
    "EMA20|60", "EMA50|60", "EMA20|240", "EMA50|240", "ADX|60", "change|60", "change|240", "close|60",
    "close|240",
]
CRYPTO_VALUE_COL = "24h_vol|5"  # 24h traded value in USD for crypto pairs (verified)
STOCK_VALUE_COL = "Value.Traded"  # price × volume for stock markets (verified)
MAJOR_CRYPTO_EXCHANGES = [
    "BINANCE", "COINBASE", "KRAKEN", "BYBIT", "OKX", "BITGET", "KUCOIN", "BITSTAMP", "GEMINI", "BITFINEX",
    "MEXC", "GATEIO", "CRYPTOCOM", "HTX",
]
NO_VOLUME_MARKETS = {"forex", "cfd", "futures", "bond", "bonds", "index", "indices", "economics"}
STABLE_BASES = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "BUSD", "USDE", "PYUSD", "USDP", "USD1", "USDD", "GUSD", "EURC",
    "EURT", "UST", "USTC", "RLUSD", "USDS", "FRAX", "LUSD", "SUSD", "EUR", "GBP", "USD", "TRY", "BRL", "AEUR",
}
CRYPTO_EXCHANGE_HINTS = set(MAJOR_CRYPTO_EXCHANGES) | {"BINANCEUS", "UPBIT", "BITHUMB", "POLONIEX", "BITMEX",
                                                      "DERIBIT", "PHEMEX", "BLOFIN", "COINEX", "CRYPTO"}
STOCK_EXCHANGE_HINTS = {"NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "OTC", "CBOE", "BATS"}
FOREX_EXCHANGE_HINTS = {"FX", "OANDA", "FX_IDC", "FOREXCOM", "FXCM", "SAXO", "PEPPERSTONE", "ICMARKETS", "EASYMARKETS"}
FUTURES_EXCHANGE_HINTS = {"CME", "CME_MINI", "COMEX", "NYMEX", "CBOT", "ICEUS", "ICEEUR", "EUREX"}
_JUNK_TICKER = re.compile(r"_[0-9A-Z]{4,}(\.[A-Z]+)?$")

TF_SUFFIX = {
    "1m": "|15", "3m": "|15", "5m": "|15", "15m": "|15",
    "30m": "|60", "1h": "|60", "2h": "|60",
    "4h": "|240", "6h": "|240", "12h": "|240",
    "1d": "", "1w": "|1W",
}
TF_LABEL = {"|15": "15m", "|60": "1h", "|240": "4h", "": "diario", "|1W": "semanal"}

# ----------------------------------------------------------------------------- features

FEATURES: list[tuple[str, str]] = [
    ("rec_all", "Rating técnico diario"),
    ("rec_ma", "Rating de medias móviles (diario)"),
    ("rec_osc", "Rating de osciladores (diario)"),
    ("rec_15", "Rating técnico 15m"),
    ("rec_60", "Rating técnico 1h"),
    ("rec_240", "Rating técnico 4h"),
    ("rec_1w", "Rating técnico semanal"),
    ("rsi_d", "RSI diario"),
    ("rsi_slope", "Pendiente del RSI diario"),
    ("rsi_15", "RSI 15m"),
    ("rsi_60", "RSI 1h"),
    ("rsi_240", "RSI 4h"),
    ("stoch", "Estocástico %K"),
    ("stoch_cross", "Cruce estocástico (K−D)"),
    ("cci", "CCI 20"),
    ("di_trend", "Fuerza direccional (ADX/DMI)"),
    ("adx_60_dir", "ADX 1h con dirección"),
    ("ao", "Awesome Oscillator / ATR"),
    ("mom", "Momentum / ATR"),
    ("macd_d", "MACD diario vs. señal"),
    ("macd_60", "MACD 1h vs. señal"),
    ("macd_240", "MACD 4h vs. señal"),
    ("ema_60", "EMA20 vs. EMA50 (1h)"),
    ("ema_240", "EMA20 vs. EMA50 (4h)"),
    ("px_ema50", "Precio vs. EMA50 diaria"),
    ("px_ema200", "Precio vs. EMA200 diaria"),
    ("sma_cross", "SMA50 vs. SMA200"),
    ("px_60", "Precio vs. EMA20 (1h)"),
    ("bb_pos", "Posición en Bandas de Bollinger"),
    ("chg_d", "Cambio diario / volatilidad"),
    ("chg_60", "Cambio 1h / volatilidad"),
    ("chg_240", "Cambio 4h / volatilidad"),
    ("perf_w", "Rendimiento semanal / volatilidad"),
    ("perf_1m", "Rendimiento mensual / volatilidad"),
    ("rel_vol", "Volumen relativo (log)"),
    ("rel_vol_dir", "Volumen relativo a favor del movimiento"),
    ("volatility", "Volatilidad diaria"),
]
FEATURE_NAMES = [name for name, _ in FEATURES]
FEATURE_LABELS = dict(FEATURES)

COLD_UPDATES = 200
ACTION_THRESHOLD = 0.15
QUICK_TTL_S = 60.0
FIRST_RUN_DELAY_S = 20.0
COLUMN_MODE_TTL_S = 6 * 3600.0  # how long a column/filter fallback is remembered per market


def _f(row: dict, key: str) -> float | None:
    return safe_float(row.get(key))


def _clip(x: float, lim: float = 3.0) -> float:
    return float(clamp(x, -lim, lim))


def _sign(x: float | None) -> float:
    if x is None or x == 0:
        return 0.0
    return 1.0 if x > 0 else -1.0


def extract_features(row: dict) -> tuple[dict[str, float], float]:
    """Normalised features from a TradingView scan row (missing values → feature absent).

    Returns (features, completeness) where completeness is the share of features that could be computed.
    """
    close = _f(row, "close")
    atr = _f(row, "ATR")
    vol_d = _f(row, "Volatility.D")
    if (atr is None or atr <= 0) and close and vol_d:
        atr = close * vol_d / 100.0
    if atr is not None and atr <= 0:
        atr = None
    if vol_d is not None and vol_d <= 0:
        vol_d = None
    atr_h = atr * math.sqrt(60 / 1440) if atr else None
    atr_4h = atr * math.sqrt(240 / 1440) if atr else None
    feats: dict[str, float] = {}

    def put(name: str, value: float | None, lim: float = 3.0) -> None:
        if value is not None and math.isfinite(value):
            feats[name] = _clip(value, lim)

    for name, key in (("rec_all", "Recommend.All"), ("rec_ma", "Recommend.MA"), ("rec_osc", "Recommend.Other"),
                      ("rec_15", "Recommend.All|15"), ("rec_60", "Recommend.All|60"),
                      ("rec_240", "Recommend.All|240"), ("rec_1w", "Recommend.All|1W")):
        put(name, _f(row, key), 1.0)
    for name, key in (("rsi_d", "RSI"), ("rsi_15", "RSI|15"), ("rsi_60", "RSI|60"), ("rsi_240", "RSI|240")):
        v = _f(row, key)
        put(name, (v - 50.0) / 50.0 if v is not None else None, 1.0)
    rsi, rsi1 = _f(row, "RSI"), _f(row, "RSI[1]")
    put("rsi_slope", (rsi - rsi1) / 10.0 if rsi is not None and rsi1 is not None else None, 2.0)
    k, d = _f(row, "Stoch.K"), _f(row, "Stoch.D")
    put("stoch", (k - 50.0) / 50.0 if k is not None else None, 1.0)
    put("stoch_cross", (k - d) / 20.0 if k is not None and d is not None else None, 2.0)
    cci = _f(row, "CCI20")
    put("cci", cci / 200.0 if cci is not None else None, 2.0)
    adx, dip, dim = _f(row, "ADX"), _f(row, "ADX+DI"), _f(row, "ADX-DI")
    if adx is not None and dip is not None and dim is not None and dip + dim > 0:
        put("di_trend", (dip - dim) / (dip + dim) * min(adx / 25.0, 2.0), 2.0)
    adx60 = _f(row, "ADX|60")
    e20h, e50h = _f(row, "EMA20|60"), _f(row, "EMA50|60")
    if adx60 is not None and e20h is not None and e50h is not None:
        put("adx_60_dir", min(adx60 / 25.0, 2.0) * _sign(e20h - e50h), 2.0)
    if atr:
        ao, mom = _f(row, "AO"), _f(row, "Mom")
        put("ao", ao / atr if ao is not None else None)
        put("mom", mom / atr if mom is not None else None)
        m, s = _f(row, "MACD.macd"), _f(row, "MACD.signal")
        put("macd_d", (m - s) / (0.25 * atr) if m is not None and s is not None else None)
        e50, e200 = _f(row, "EMA50"), _f(row, "EMA200")
        if close is not None:
            put("px_ema50", (close - e50) / atr if e50 is not None else None)
            put("px_ema200", (close - e200) / (3.0 * atr) if e200 is not None else None)
        s50, s200 = _f(row, "SMA50"), _f(row, "SMA200")
        put("sma_cross", (s50 - s200) / (3.0 * atr) if s50 is not None and s200 is not None else None)
    if atr_h:
        m, s = _f(row, "MACD.macd|60"), _f(row, "MACD.signal|60")
        put("macd_60", (m - s) / (0.25 * atr_h) if m is not None and s is not None else None)
        put("ema_60", (e20h - e50h) / atr_h if e20h is not None and e50h is not None else None)
        c60 = _f(row, "close|60")
        put("px_60", (c60 - e20h) / atr_h if c60 is not None and e20h is not None else None)
    if atr_4h:
        m, s = _f(row, "MACD.macd|240"), _f(row, "MACD.signal|240")
        put("macd_240", (m - s) / (0.25 * atr_4h) if m is not None and s is not None else None)
        e20q, e50q = _f(row, "EMA20|240"), _f(row, "EMA50|240")
        put("ema_240", (e20q - e50q) / atr_4h if e20q is not None and e50q is not None else None)
    up, lo = _f(row, "BB.upper"), _f(row, "BB.lower")
    if close is not None and up is not None and lo is not None and up > lo:
        put("bb_pos", (close - lo) / (up - lo) * 2.0 - 1.0, 1.5)
    if vol_d:
        chg = _f(row, "change")
        put("chg_d", chg / vol_d if chg is not None else None)
        c1 = _f(row, "change|60")
        put("chg_60", c1 / (vol_d * math.sqrt(1 / 24)) if c1 is not None else None)
        c4 = _f(row, "change|240")
        put("chg_240", c4 / (vol_d * math.sqrt(4 / 24)) if c4 is not None else None)
        pw, pm = _f(row, "Perf.W"), _f(row, "Perf.1M")
        put("perf_w", pw / (vol_d * math.sqrt(7)) if pw is not None else None)
        put("perf_1m", pm / (vol_d * math.sqrt(30)) if pm is not None else None)
        put("volatility", vol_d / 5.0 - 1.0)
    rv = _f(row, "relative_volume_10d_calc")
    if rv is not None and rv > 0:
        lrv = math.log(rv)
        put("rel_vol", lrv, 2.0)
        put("rel_vol_dir", lrv * _sign(_f(row, "change")), 2.0)
    completeness = len(feats) / len(FEATURE_NAMES)
    return feats, completeness


def primary_suffix(tf: str) -> str:
    return TF_SUFFIX.get(tf, "|60")


def primary_rating(row: dict, tf: str) -> float | None:
    v = _f(row, "Recommend.All" + primary_suffix(tf))
    return v if v is not None else _f(row, "Recommend.All")


def rule_prior(feats: dict[str, float], tf: str) -> float:
    """Rule-based directional prior in [-1, 1] from ratings, trend alignment and momentum."""
    suffix = primary_suffix(tf)
    rating_key = {"|15": "rec_15", "|60": "rec_60", "|240": "rec_240", "": "rec_all", "|1W": "rec_1w"}[suffix]
    comps: list[tuple[float, float]] = []
    rating_tf = feats.get(rating_key, feats.get("rec_all"))
    if rating_tf is not None:
        comps.append((0.30, rating_tf))
    if "rec_all" in feats:
        comps.append((0.15, feats["rec_all"]))
    htf = feats.get("rec_240", feats.get("rec_1w"))
    if htf is not None:
        comps.append((0.10, htf))
    trend_parts = [_sign(feats[k]) for k in ("ema_60", "ema_240", "px_ema50", "sma_cross") if k in feats]
    if trend_parts:
        comps.append((0.20, sum(trend_parts) / len(trend_parts)))
    mom_parts = [feats[k] for k in ("macd_60", "macd_240", "macd_d") if k in feats]
    if mom_parts:
        comps.append((0.15, clamp(sum(mom_parts) / (2.0 * len(mom_parts)), -1.0, 1.0)))
    rsi = feats.get("rsi_60", feats.get("rsi_d"))
    if rsi is not None:  # exhaustion: RSI > 75 / < 25 leans against the move
        exhaustion = -(rsi - 0.5) * 2.0 if rsi > 0.5 else (-(rsi + 0.5) * 2.0 if rsi < -0.5 else 0.0)
        comps.append((0.10, clamp(exhaustion, -1.0, 1.0)))
    if not comps:
        return 0.0
    total_w = sum(w for w, _ in comps)
    return float(clamp(sum(w * c for w, c in comps) / total_w, -1.0, 1.0))


# ----------------------------------------------------------------------------- online logistic model


def _sigmoid(z: float) -> float:
    z = clamp(z, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-z))


class OnlineLogit:
    """Online logistic regression (AdaGrad + L2) on the scanner features. JSON-serialisable."""

    RECENT = 200

    def __init__(self, names: list[str] | None = None, lr: float = 0.05, l2: float = 1e-3):
        self.names = list(names or FEATURE_NAMES)
        n = len(self.names)
        self.w = np.zeros(n)
        self.b = 0.0
        self.g2 = np.zeros(n)
        self.gb2 = 0.0
        self.lr = float(lr)
        self.l2 = float(l2)
        self.updates = 0
        self.trained_until: int | None = None
        self.recent: list[tuple[float, int]] = []

    def vector(self, feats: dict[str, Any]) -> np.ndarray:
        out = np.zeros(len(self.names))
        for i, name in enumerate(self.names):
            v = safe_float(feats.get(name))
            if v is not None:
                out[i] = clamp(v, -3.0, 3.0)
        return out

    def predict(self, x: np.ndarray) -> float:
        return _sigmoid(float(self.w @ x) + self.b)

    def update(self, x: np.ndarray, y: int, label_time: int | None = None) -> float:
        p = self.predict(x)
        err = p - float(y)
        g = err * x + self.l2 * self.w
        self.g2 += g * g
        self.gb2 += err * err
        self.w -= self.lr * g / (np.sqrt(self.g2) + 1e-8)
        self.b -= self.lr * err / (math.sqrt(self.gb2) + 1e-8)
        self.updates += 1
        self.recent.append((round(p, 5), int(y)))
        if len(self.recent) > self.RECENT:
            del self.recent[: len(self.recent) - self.RECENT]
        if label_time is not None:
            self.trained_until = max(self.trained_until or 0, int(label_time))
        return p

    def contributions(self, x: np.ndarray) -> np.ndarray:
        return self.w * x

    def hit_rate(self) -> float | None:
        if not self.recent:
            return None
        return sum(1 for p, y in self.recent if (p >= 0.5) == bool(y)) / len(self.recent)

    def brier(self) -> float | None:
        if not self.recent:
            return None
        return sum((p - y) ** 2 for p, y in self.recent) / len(self.recent)

    def blend_alpha(self) -> float:
        return min(self.updates / COLD_UPDATES, 1.0)

    def to_state(self) -> dict:
        return {
            "version": 1,
            "names": self.names,
            "w": [round(float(v), 8) for v in self.w],
            "b": round(self.b, 8),
            "g2": [round(float(v), 8) for v in self.g2],
            "gb2": round(self.gb2, 8),
            "lr": self.lr,
            "l2": self.l2,
            "updates": self.updates,
            "trained_until": self.trained_until,
            "recent": self.recent,
        }

    @classmethod
    def from_state(cls, state: dict | None) -> "OnlineLogit":
        model = cls()
        if not isinstance(state, dict):
            return model
        model.lr = float(safe_float(state.get("lr"), model.lr))
        model.l2 = float(safe_float(state.get("l2"), model.l2))
        names = state.get("names") or []
        w = state.get("w") or []
        g2 = state.get("g2") or []
        index = {name: i for i, name in enumerate(model.names)}
        for j, name in enumerate(names):  # map by name: survives feature-list changes
            i = index.get(name)
            if i is None:
                continue
            if j < len(w):
                model.w[i] = safe_float(w[j], 0.0)
            if j < len(g2):
                model.g2[i] = max(safe_float(g2[j], 0.0), 0.0)
        model.b = float(safe_float(state.get("b"), 0.0))
        model.gb2 = max(float(safe_float(state.get("gb2"), 0.0)), 0.0)
        model.updates = int(safe_float(state.get("updates"), 0) or 0)
        tu = safe_float(state.get("trained_until"))
        model.trained_until = int(tu) if tu is not None else None
        recent = state.get("recent") or []
        model.recent = [(float(p), int(y)) for p, y in recent if safe_float(p) is not None][-cls.RECENT:]
        return model


def model_key(market: str, tf: str, horizon_s: int) -> str:
    return f"{market}|{tf}|{int(horizon_s)}"


async def load_scanner_model(db: Any, key: str) -> OnlineLogit:
    row = await db.fetchone("SELECT state FROM model_state WHERE kind='scanner' AND key=?", (key,))
    return OnlineLogit.from_state(json_loads(row["state"]) if row else None)


async def save_scanner_model(db: Any, key: str, model: OnlineLogit) -> None:
    await db.execute(
        "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES ('scanner', ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, key) DO UPDATE SET state=excluded.state, updates=excluded.updates, "
        "trained_until=excluded.trained_until, updated_at=excluded.updated_at",
        (key, json_dumps(model.to_state()), model.updates, model.trained_until, now_ms()),
    )


# ----------------------------------------------------------------------------- evaluation of one row


@dataclass
class QuickEval:
    score: float
    prob_up: float
    action: str
    confidence: float
    rating: float | None
    price: float | None
    reasons: list[str]
    features: dict[str, float]
    completeness: float
    model_updates: int
    blend: float
    reasons_meta: dict[str, Any] = field(default_factory=dict)


def _directional_signals(feats: dict[str, float]) -> list[float]:
    keys = ("rec_15", "rec_60", "rec_240", "rec_all", "rec_1w", "macd_60", "macd_240", "ema_60", "ema_240", "di_trend")
    return [_sign(feats[k]) for k in keys if k in feats and feats[k] != 0]


def evaluate_features(
    feats: dict[str, float],
    completeness: float,
    tf: str,
    model: OnlineLogit | None,
) -> tuple[float, float, float, float]:
    """(prob_up, score, confidence, blend_alpha) from features, the learned model and the rule prior."""
    prior = rule_prior(feats, tf)
    p_prior = 0.5 + 0.25 * prior
    alpha = model.blend_alpha() if model is not None else 0.0
    p_model = model.predict(model.vector(feats)) if model is not None else 0.5
    p = alpha * p_model + (1.0 - alpha) * p_prior
    p = float(clamp(p, 0.01, 0.99))
    score = 2.0 * p - 1.0
    signs = _directional_signals(feats)
    direction = _sign(score)
    agreement = (sum(1 for s in signs if s == direction) / len(signs)) if signs and direction else 0.5
    maturity = 0.6 + 0.4 * alpha
    confidence = clamp(abs(score) * 2.5, 0.0, 1.0) * (0.4 + 0.6 * agreement) * math.sqrt(max(completeness, 0.0)) * maturity
    return p, score, float(clamp(confidence, 0.0, 1.0)), alpha


def action_for(score: float) -> str:
    if score >= ACTION_THRESHOLD:
        return "BUY"
    if score <= -ACTION_THRESHOLD:
        return "SELL"
    return "WAIT"


def _reason_candidates(row: dict, feats: dict[str, float], tf: str) -> list[tuple[float, float, str]]:
    """(direction, strength, text) candidates with concrete numbers."""
    out: list[tuple[float, float, str]] = []
    suffix = primary_suffix(tf)
    tfl = TF_LABEL.get(suffix, tf)
    r = primary_rating(row, tf)
    if r is not None:
        lbl = tfl if _f(row, "Recommend.All" + suffix) is not None else "diario"
        out.append((_sign(r), abs(r) * 1.2, f"Rating técnico {lbl}: {rating_label(r)} ({fmt_num(r, 2, True)})"))
    for key, label in (("ema_60", "1h"), ("ema_240", "4h")):
        v = feats.get(key)
        if v is not None and v != 0:
            up = v > 0
            out.append((_sign(v), min(abs(v), 2.0) * 0.45,
                        f"EMA20 {'>' if up else '<'} EMA50 en {label} (tendencia {'alcista' if up else 'bajista'})"))
    for key, label in (("macd_60", "1h"), ("macd_240", "4h"), ("macd_d", "diario")):
        v = feats.get(key)
        if v is not None and v != 0:
            out.append((_sign(v), min(abs(v), 2.0) * 0.35,
                        f"MACD {label} {'por encima' if v > 0 else 'por debajo'} de su señal"))
    for key, col, label in (("rsi_60", "RSI|60", "1h"), ("rsi_d", "RSI", "diario")):
        raw = _f(row, col)
        if raw is None:
            continue
        if raw >= 70:
            out.append((-1.0, (raw - 70) / 15 + 0.4, f"RSI {label} {raw:.0f} (sobrecompra: riesgo de giro)"))
        elif raw <= 30:
            out.append((1.0, (30 - raw) / 15 + 0.4, f"RSI {label} {raw:.0f} (sobreventa: posible rebote)"))
        else:
            out.append((_sign(raw - 50), abs(raw - 50) / 50, f"RSI {label} {raw:.0f}"))
        break
    v = feats.get("di_trend")
    adx = _f(row, "ADX")
    if v is not None and adx is not None:
        strength = "fuerte" if adx >= 25 else "débil"
        out.append((_sign(v), min(abs(v), 2.0) * 0.3,
                    f"ADX {adx:.0f} con DI{'+' if v > 0 else '−'} dominante (tendencia {strength})"))
    rv = _f(row, "relative_volume_10d_calc")
    if rv is not None and rv > 0:
        chg = _f(row, "change") or 0.0
        if rv >= 1.5:
            out.append((_sign(chg), min(rv / 3.0, 1.0),
                        f"Volumen relativo {fmt_num(rv, 1)}× (interés inusual {'comprador' if chg >= 0 else 'vendedor'})"))
        elif rv <= 0.6:
            out.append((0.0, 0.2, f"Volumen relativo {fmt_num(rv, 1)}× (poco interés: señales menos fiables)"))
    bb = feats.get("bb_pos")
    if bb is not None and abs(bb) >= 0.8:
        out.append((-_sign(bb) * 0.5, abs(bb) * 0.3,
                    f"Precio en la banda {'superior' if bb > 0 else 'inferior'} de Bollinger"))
    pw, pm = _f(row, "Perf.W"), _f(row, "Perf.1M")
    if pw is not None:
        txt = f"Rendimiento semana {fmt_pct(pw)}" + (f" · mes {fmt_pct(pm)}" if pm is not None else "")
        out.append((_sign(pw), min(abs(feats.get("perf_w", 0.0)), 2.0) * 0.3, txt))
    return out


def build_reasons(row: dict, feats: dict[str, float], tf: str, score: float, model: OnlineLogit | None,
                  alpha: float, *, max_for: int = 4, max_against: int = 2) -> list[str]:
    cands = _reason_candidates(row, feats, tf)
    direction = _sign(score)
    favor = sorted([c for c in cands if direction and c[0] * direction > 0], key=lambda c: -c[1])
    against = sorted([c for c in cands if direction and c[0] * direction < 0], key=lambda c: -c[1])
    neutral = [c for c in cands if not direction or c[0] == 0]
    reasons = [c[2] for c in favor[:max_for]]
    reasons += [f"En contra: {c[2]}" for c in against[:max_against]]
    if not direction:
        reasons += [c[2] for c in sorted(neutral, key=lambda c: -c[1])[:max_for]]
    updates = model.updates if model is not None else 0
    if alpha < 1.0:
        reasons.append(
            f"Modelo en aprendizaje ({updates}/{COLD_UPDATES} resultados): se combina con reglas técnicas"
        )
    else:
        hit = model.hit_rate() if model is not None else None
        reasons.append(
            f"Modelo entrenado con {updates} resultados reales"
            + (f" · acierto reciente {fmt_prob(hit)}" if hit is not None else "")
        )
    return reasons


def evaluate_row(row: dict, tf: str, model: OnlineLogit | None) -> QuickEval | None:
    price = _f(row, "close")
    if price is None or price <= 0:
        return None
    feats, completeness = extract_features(row)
    if completeness <= 0:
        return None
    p, score, confidence, alpha = evaluate_features(feats, completeness, tf, model)
    reasons = build_reasons(row, feats, tf, score, model, alpha)
    return QuickEval(
        score=round(score, 4),
        prob_up=round(p, 4),
        action=action_for(score),
        confidence=round(confidence, 4),
        rating=primary_rating(row, tf),
        price=price,
        reasons=reasons,
        features={k: round(v, 5) for k, v in feats.items()},
        completeness=completeness,
        model_updates=model.updates if model is not None else 0,
        blend=alpha,
    )


# ----------------------------------------------------------------------------- local fallback (candles)


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x, dtype=float)
    a = 2.0 / (n + 1.0)
    acc = float(x[0])
    for i, v in enumerate(x):
        acc = a * float(v) + (1 - a) * acc if i else float(v)
        out[i] = acc
    return out


def _rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gain = np.maximum(delta, 0.0)
    loss = np.maximum(-delta, 0.0)
    ag = np.empty_like(close, dtype=float)
    al = np.empty_like(close, dtype=float)
    g = l = 0.0
    for i in range(len(close)):
        if i == 0:
            g, l = gain[0], loss[0]
        else:
            g = (g * (n - 1) + gain[i]) / n
            l = (l * (n - 1) + loss[i]) / n
        ag[i], al[i] = g, l
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(al > 0, ag / al, np.inf)
    return np.where(np.isinf(rs), 100.0, 100.0 - 100.0 / (1.0 + rs))


def local_quick_from_candles(cs: Any, tf: str) -> dict | None:
    """QuickSignal-like read from local candles (no TradingView data): trend, MACD, RSI and momentum."""
    n = len(cs)
    if n < 30:
        return None
    close = np.asarray(cs.close, dtype=float)
    high, low = np.asarray(cs.high, dtype=float), np.asarray(cs.low, dtype=float)
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])
    atr = float(_ema(tr, 14)[-1])
    if atr <= 0:
        return None
    ema20, ema50 = _ema(close, 20), _ema(close, min(50, n - 1))
    macd = _ema(close, 12) - _ema(close, 26)
    signal = _ema(macd, 9)
    rsi = float(_rsi(close)[-1])
    last = float(close[-1])
    lookback = min(10, n - 1)
    mom = (last / close[-1 - lookback] - 1.0) / max(atr / last * math.sqrt(lookback), 1e-9)
    trend = (_sign(last - ema50[-1]) + _sign(ema20[-1] - ema50[-1])) / 2.0
    macd_c = clamp((macd[-1] - signal[-1]) / (0.25 * atr), -1.0, 1.0)
    rsi_c = (rsi - 50.0) / 50.0
    if rsi > 75:
        rsi_c = -0.5
    elif rsi < 25:
        rsi_c = 0.5
    prior = 0.35 * trend + 0.25 * macd_c + 0.20 * rsi_c + 0.20 * clamp(mom, -1.0, 1.0)
    p = clamp(0.5 + 0.25 * prior, 0.01, 0.99)
    score = 2.0 * p - 1.0
    reasons = [
        f"Lectura local con {n} velas {tf} (sin datos técnicos de TradingView)",
        f"EMA20 {'>' if ema20[-1] > ema50[-1] else '<'} EMA50 (tendencia {'alcista' if ema20[-1] > ema50[-1] else 'bajista'})",
        f"MACD {'por encima' if macd[-1] > signal[-1] else 'por debajo'} de su señal",
        f"RSI 14: {rsi:.0f}" + (" (sobrecompra)" if rsi >= 70 else " (sobreventa)" if rsi <= 30 else ""),
    ]
    agreement = sum(1 for s in (trend, macd_c, rsi_c, mom) if _sign(s) == _sign(score)) / 4.0
    confidence = clamp(abs(score) * 2.5, 0.0, 1.0) * (0.4 + 0.6 * agreement) * 0.6
    return {
        "score": round(score, 4),
        "prob_up": round(p, 4),
        "action": action_for(score),
        "confidence": round(confidence, 4),
        "rating": None,
        "price": last,
        "reasons": reasons,
        "model_updates": 0,
    }


# ----------------------------------------------------------------------------- filtering


def market_profile(market: str) -> dict[str, Any]:
    if market == "crypto":
        return {
            "value_col": CRYPTO_VALUE_COL,
            "sort": CRYPTO_VALUE_COL,
            "filters": [
                {"left": "exchange", "operation": "in_range", "right": MAJOR_CRYPTO_EXCHANGES},
                {"left": "type", "operation": "in_range", "right": ["spot"]},
            ],
            "min_value": 500_000.0,
            "dedupe_base": True,
        }
    if market in NO_VOLUME_MARKETS:
        return {"value_col": None, "sort": "volume", "filters": None, "min_value": 0.0, "dedupe_base": False}
    return {"value_col": STOCK_VALUE_COL, "sort": STOCK_VALUE_COL, "filters": None, "min_value": 1_000_000.0,
            "dedupe_base": False}


def is_junk_symbol(symbol: str) -> bool:
    exchange, ticker = parse_symbol(symbol)
    if "SWAP" in exchange or "DEX" in exchange:
        return True
    return bool(_JUNK_TICKER.search(ticker))


def crypto_base(row: dict, symbol: str) -> str:
    _, ticker = parse_symbol(symbol)
    ticker = ticker.split(".")[0]
    quote = str(row.get("currency") or "").upper()
    if quote and ticker.endswith(quote) and len(ticker) > len(quote):
        return ticker[: -len(quote)]
    for q in ("USDT", "USDC", "FDUSD", "BUSD", "USD", "EUR", "BTC", "ETH"):
        if ticker.endswith(q) and len(ticker) > len(q):
            return ticker[: -len(q)]
    return ticker


def traded_value(row: dict, profile: dict) -> float | None:
    col = profile.get("value_col")
    v = _f(row, col) if col else None
    if v is None:
        c, vol = _f(row, "close"), _f(row, "volume")
        if c is not None and vol is not None:
            v = c * vol
    return v


def filter_rows(rows: list[dict], market: str, limit: int) -> list[dict]:
    """Drop DEX / illiquid junk, stablecoin pairs and duplicated venues; keep at most `limit` rows."""
    profile = market_profile(market)
    values = [traded_value(r, profile) for r in rows]
    has_value = sum(1 for v in values if v is not None and v > 0)
    use_value = profile["min_value"] > 0 and has_value >= max(1, len(rows) // 2)
    out: list[dict] = []
    seen_symbols: set[str] = set()
    seen_bases: set[str] = set()
    for row, value in zip(rows, values):
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or symbol in seen_symbols or is_junk_symbol(symbol):
            continue
        close = _f(row, "close")
        if close is None or close <= 0:
            continue
        if use_value and (value is None or value < profile["min_value"]):
            continue
        if profile["dedupe_base"]:
            base = crypto_base(row, symbol)
            if base in STABLE_BASES or base in seen_bases:
                continue
            seen_bases.add(base)
        seen_symbols.add(symbol)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def market_for_exchange(exchange: str) -> str | None:
    ex = exchange.upper()
    if ex in CRYPTO_EXCHANGE_HINTS:
        return "crypto"
    if ex in STOCK_EXCHANGE_HINTS:
        return "america"
    if ex in FOREX_EXCHANGE_HINTS:
        return "forex"
    if ex in FUTURES_EXCHANGE_HINTS:
        return "futures"
    if ex in ("TVC", "SP", "DJ", "CAPITALCOM", "CURRENCYCOM"):
        return "cfd"
    return None


class ScanError(RuntimeError):
    """Scan failure with a Spanish message."""


# ----------------------------------------------------------------------------- service


class ScannerService:
    def __init__(self, rt: Any):
        self.rt = rt
        self._started = False
        self._loop_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._scan_lock = asyncio.Lock()
        self._model_lock = asyncio.Lock()
        self._models: dict[str, OnlineLogit] = {}
        self._quick_cache = TTLCache(QUICK_TTL_S, maxsize=1024)
        self._column_mode: dict[str, tuple[int, float]] = {}
        self._running_runs: dict[tuple[str, str], int] = {}
        self._last_run_ms: int | None = None
        self._next_run_at: float | None = None
        self._log = RateLimitedLog()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._started = True
        try:
            rows = await self.rt.db.fetchall("SELECT key, state FROM model_state WHERE kind='scanner'")
            for row in rows:
                self._models[row["key"]] = OnlineLogit.from_state(json_loads(row["state"]))
            last = await self.rt.db.fetchval("SELECT MAX(started_at) FROM scan_runs WHERE status='done'")
            self._last_run_ms = int(last) if last else None
            # runs left 'running' by a previous process can never finish
            await self.rt.db.execute(
                "UPDATE scan_runs SET status='error', message='Interrumpido (reinicio del servidor)', finished_at=? "
                "WHERE status='running'",
                (now_ms(),),
            )
        except Exception as exc:
            self._emit("warn", f"No se pudo cargar el estado del escáner: {exc}")
        if config.BACKGROUND:
            self._loop_task = asyncio.create_task(self._loop(), name="scanner-loop")

    async def stop(self) -> None:
        self._started = False
        tasks = [t for t in [self._loop_task, *self._tasks] if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._loop_task = None
        self._tasks.clear()

    def status(self) -> dict:
        next_in = None
        if self._next_run_at is not None and self._loop_task is not None and not self._loop_task.done():
            next_in = max(0, int(self._next_run_at - time.monotonic()))
        return {
            "running": self._started,
            "last_run": self._last_run_ms,
            "next_run_in_s": next_in,
            "model_updates": int(sum(m.updates for m in self._models.values())),
        }

    # ------------------------------------------------------------------ helpers

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        if self.rt.bus is not None:
            self.rt.bus.emit(level, "scanner", message, data)

    def _settings(self, key: str, default: Any = None) -> Any:
        if self.rt.settings is None:
            from app.config import DEFAULT_SETTINGS

            return DEFAULT_SETTINGS.get(key, default)
        return self.rt.settings.get(key, default)

    def _publish(self, message: dict) -> None:
        if self.rt.ws is not None:
            try:
                self.rt.ws.publish("scanner", {"type": "scanner", **message})
            except Exception:
                pass

    def horizon_s(self) -> int:
        return int(max(60, int(self._settings("scanner.horizon_s", 14400) or 14400)))

    def get_model_sync(self, key: str) -> OnlineLogit | None:
        return self._models.get(key)

    async def get_model(self, key: str) -> OnlineLogit:
        model = self._models.get(key)
        if model is None:
            model = await load_scanner_model(self.rt.db, key)
            self._models[key] = model
        return model

    async def learn_batch(self, samples: list[tuple[str, dict, int, int | None]]) -> dict[str, OnlineLogit]:
        """SGD steps on the scanner models: samples = [(model_key, features, label_up, label_time_s)]."""
        touched: dict[str, OnlineLogit] = {}
        async with self._model_lock:
            for key, feats, label, label_time in samples:
                model = await self.get_model(key)
                model.update(model.vector(feats or {}), 1 if label else 0, label_time)
                touched[key] = model
            for key, model in touched.items():
                await save_scanner_model(self.rt.db, key, model)
        return touched

    # ------------------------------------------------------------------ scanning

    async def run_scan(
        self,
        market: str | None = None,
        tf: str | None = None,
        limit: int | None = None,
        deep: bool = False,
        deep_top: int | None = None,
        *,
        wait: bool = False,
    ) -> int:
        """Create a scan run and execute it in the background (or wait). Returns the run id."""
        markets = self._settings("scanner.markets", ["crypto"]) or ["crypto"]
        market = str(market or markets[0]).strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{2,32}", market):
            raise ScanError(f"Mercado no válido: {market!r}")
        tf = normalize_tf(tf or self._settings("scanner.tf", "1h"))
        limit = int(clamp(int(limit or self._settings("scanner.limit", 300) or 300), 5, 1000))
        deep_top = int(clamp(int(deep_top or self._settings("scanner.deep_top", 12) or 12), 1, 50))
        existing = self._running_runs.get((market, tf))
        if existing is not None:
            return existing
        params = {"market": market, "tf": tf, "limit": limit, "deep": bool(deep), "deep_top": deep_top,
                  "horizon_s": self.horizon_s()}
        run_id = await self.rt.db.execute(
            "INSERT INTO scan_runs(market, tf, started_at, n_symbols, status, deep, message, params) "
            "VALUES (?, ?, ?, 0, 'running', ?, ?, ?)",
            (market, tf, now_ms(), 1 if deep else 0, "En curso…", json_dumps(params)),
        )
        self._running_runs[(market, tf)] = run_id
        task = asyncio.create_task(self._scan_task(run_id, market, tf, limit, bool(deep), deep_top),
                                   name=f"scan-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if wait:
            await task
        return run_id

    async def _scan_task(self, run_id: int, market: str, tf: str, limit: int, deep: bool, deep_top: int) -> None:
        try:
            async with self._scan_lock:
                self._publish({"event": "started", "run_id": run_id, "progress": 0.0,
                               "message": f"Escaneando {market} {tf}…"})
                await self._scan(run_id, market, tf, limit, deep, deep_top)
        except asyncio.CancelledError:
            await self._finish_run(run_id, "error", "Cancelado", 0)
            raise
        except Exception as exc:
            msg = str(exc) if isinstance(exc, ScanError) else f"Error en el escaneo: {type(exc).__name__}: {exc}"
            await self._finish_run(run_id, "error", msg, 0)
            self._publish({"event": "error", "run_id": run_id, "message": msg})
            if self._log.should_log(f"scan-error:{market}", 300):
                self._emit("error", f"Escaneo {market} {tf} falló: {msg}")
        finally:
            self._running_runs.pop((market, tf), None)

    async def _finish_run(self, run_id: int, status: str, message: str, n_symbols: int) -> None:
        try:
            await self.rt.db.execute(
                "UPDATE scan_runs SET status=?, message=?, finished_at=?, n_symbols=? WHERE id=?",
                (status, message, now_ms(), n_symbols, run_id),
            )
        except Exception:
            pass

    async def fetch_rows(self, market: str, limit: int) -> list[dict]:
        """TradingView scan with progressive fallbacks (filters → no filters → core columns)."""
        hub = self.rt.hub
        if config.OFFLINE:
            raise ScanError("Modo sin conexión: el escáner necesita datos de TradingView")
        if hub is None or not hasattr(hub, "tv_scan"):
            raise ScanError("Servicio de datos no disponible para escanear")
        profile = market_profile(market)
        extra = [c for c in (profile["value_col"], "currency") if c]
        fetch_n = int(min(limit * 2 if profile["dedupe_base"] else limit + 50, 1000))
        attempts = [
            (BASE_COLUMNS + MTF_COLUMNS + extra, profile["sort"], profile["filters"]),
            (BASE_COLUMNS + MTF_COLUMNS + extra, profile["sort"], None),
            (BASE_COLUMNS + ["currency"], "volume", None),
            (BASE_COLUMNS, "volume", None),
        ]
        mode = self._column_mode.get(market)
        start = mode[0] if mode is not None and time.monotonic() - mode[1] < COLUMN_MODE_TTL_S else 0
        last_exc: Exception | None = None
        for i in range(start, len(attempts)):
            columns, sort_by, filters = attempts[i]
            kwargs: dict[str, Any] = {"sort_by": sort_by, "limit": fetch_n}
            if filters:
                kwargs["filters"] = filters
            try:
                data = await asyncio.wait_for(hub.tv_scan(market, columns, **kwargs), timeout=45)
            except asyncio.TimeoutError:
                raise ScanError("TradingView no respondió a tiempo") from None
            except Exception as exc:
                last_exc = exc
                continue
            rows = (data or {}).get("rows") or []
            if rows or i == len(attempts) - 1:
                if i != start or mode is None:
                    self._column_mode[market] = (i, time.monotonic())
                return [r for r in rows if isinstance(r, dict)]
        raise ScanError(f"No se pudo consultar TradingView para '{market}': {last_exc}")

    async def _scan(self, run_id: int, market: str, tf: str, limit: int, deep: bool, deep_top: int) -> None:
        raw_rows = await self.fetch_rows(market, limit)
        rows = filter_rows(raw_rows, market, limit)
        if not rows:
            raise ScanError(f"El mercado '{market}' no devolvió activos válidos")
        horizon = self.horizon_s()
        key = model_key(market, tf, horizon)
        model = await self.get_model(key)
        created = now_ms()
        target = created + horizon * 1000
        results: list[dict] = []
        pred_rows: list[tuple] = []
        for row in rows:
            ev = evaluate_row(row, tf, model)
            if ev is None:
                continue
            symbol = str(row.get("symbol")).upper()
            results.append(
                {
                    "rank": 0,
                    "symbol": symbol,
                    "description": str(row.get("description") or row.get("name") or symbol),
                    "type": str(row.get("type") or ""),
                    "price": ev.price,
                    "change_pct": _f(row, "change"),
                    "volume": _f(row, "volume"),
                    "score": ev.score,
                    "prob_up": ev.prob_up,
                    "action": ev.action,
                    "confidence": ev.confidence,
                    "rating": ev.rating,
                    "deep": False,
                    "verdict": None,
                    "gate_score": None,
                    "direction": None,
                    "regime": None,
                    "sl": None,
                    "tp1": None,
                    "tp2": None,
                    "expected_move_pct": None,
                    "reasons": ev.reasons,
                }
            )
            direction = 1 if ev.action == "BUY" else -1 if ev.action == "SELL" else 0
            pred_rows.append(
                ("scanner", symbol, tf, key, created, target, ev.price, direction, ev.prob_up, ev.score,
                 json_dumps({"v": 1, "x": ev.features}))
            )
        if pred_rows:
            await self.rt.db.executemany(
                "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, "
                "prob_up, score, features) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                pred_rows,
            )
        self._publish({"event": "progress", "run_id": run_id, "progress": 0.5 if deep else 0.9,
                       "message": f"{len(results)} activos evaluados"})
        deep_done = 0
        if deep and results:
            deep_done = await self._deep(run_id, results, tf, deep_top)
        rank_rows(results)
        await self._persist_results(run_id, results)
        summary = self._summary(market, tf, results, deep and deep_done > 0)
        await self._finish_run(run_id, "done", summary, len(results))
        self._last_run_ms = created
        self._publish({"event": "done", "run_id": run_id, "progress": 1.0, "message": summary})
        self._emit("scan", summary, {"run_id": run_id, "market": market, "tf": tf})
        if deep and deep_done:
            rejected = [r for r in results if r["deep"] and r["verdict"] in ("AVOID", "WAIT")]
            if rejected:
                parts = [f"{r['symbol']} ({VERDICT_LABEL.get(r['verdict'], r['verdict'])} {r['gate_score'] or 0:.0f}/100)"
                         for r in rejected[:5]]
                self._emit("scan", "Rechazadas por el filtro: " + ", ".join(parts)
                           + (f" y {len(rejected) - 5} más" if len(rejected) > 5 else ""),
                           {"run_id": run_id})

    async def _deep(self, run_id: int, results: list[dict], tf: str, deep_top: int) -> int:
        nexus, hub = self.rt.nexus, self.rt.hub
        if nexus is None or hub is None:
            self._emit("warn", "Modo profundo no disponible: el motor NEXUS no está cargado")
            return 0
        candidates = sorted(
            [r for r in results if r["action"] != "WAIT"],
            key=lambda r: abs(r["score"]) * (0.5 + 0.5 * r["confidence"]),
            reverse=True,
        )
        chosen: list[dict] = []
        for row in candidates[: deep_top * 4]:
            if len(chosen) >= deep_top:
                break
            try:
                info = await asyncio.wait_for(hub.symbol_info(row["symbol"]), timeout=15)
            except Exception:
                continue
            if str((info or {}).get("history") or "") in ("full", "keyed"):
                chosen.append(row)
        done = 0
        horizon_bars = int(self._settings("nexus.time_stop_bars", 48) or 48)
        for i, row in enumerate(chosen):
            self._publish({
                "event": "progress",
                "run_id": run_id,
                "progress": round(0.5 + 0.45 * i / max(len(chosen), 1), 3),
                "message": f"Análisis profundo {row['symbol']} ({i + 1}/{len(chosen)})",
            })
            try:
                resp = await asyncio.wait_for(nexus.analyze(row["symbol"], tf), timeout=90)
            except Exception as exc:
                row["reasons"] = [f"Análisis profundo no disponible: {type(exc).__name__}"] + row["reasons"]
                continue
            gate = apply_deep(row, resp)
            if gate is not None:
                done += 1
                await self._log_gate(row["symbol"], tf, gate, resp, horizon_bars)
        return done

    async def _log_gate(self, symbol: str, tf: str, gate: dict, resp: dict, horizon_bars: int) -> None:
        try:
            current = resp.get("current") or {}
            t = safe_float(gate.get("time")) or safe_float(current.get("time")) or now_s()
            created = now_ms()
            target = created + horizon_bars * tf_seconds(tf) * 1000
            await self.rt.db.execute(
                "INSERT OR IGNORE INTO gate_log(symbol, tf, time, direction, verdict, score, win_prob, entry, sl, tp1, "
                "tp2, checks, taken, source, created_at, target_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'scanner', ?, ?)",
                (
                    symbol, tf, int(t), gate.get("direction"), gate.get("verdict"),
                    safe_float(gate.get("score"), 0.0), safe_float(gate.get("win_prob")),
                    safe_float(gate.get("entry")), safe_float(gate.get("sl")), safe_float(gate.get("tp1")),
                    safe_float(gate.get("tp2")), json_dumps(gate_checks_vector(gate)), created, target,
                ),
            )
        except Exception as exc:
            if self._log.should_log("gate-log", 120):
                self._emit("warn", f"No se pudo registrar la decisión del filtro: {exc}")

    async def _persist_results(self, run_id: int, results: list[dict]) -> None:
        rows = []
        seen: set[str] = set()
        for r in results:
            if r["symbol"] in seen:
                continue
            seen.add(r["symbol"])
            rows.append(
                (run_id, r["symbol"], r["rank"], r["score"], r["prob_up"], r["action"], r["confidence"],
                 r["price"], r["change_pct"], r["volume"], r["verdict"], r["gate_score"], json_dumps(r))
            )
        await self.rt.db.executemany(
            "INSERT INTO scan_results(run_id, symbol, rank, score, prob_up, action, confidence, price, "
            "change_pct, volume, verdict, gate_score, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    @staticmethod
    def _summary(market: str, tf: str, results: list[dict], deep: bool) -> str:
        n = len(results)
        if deep:
            takes = [r for r in results if r["deep"] and r["verdict"] == "TAKE"]
            if takes:
                best = takes[0]
                return (f"Escaneo {market} {tf}: {n} activos · {len(takes)} oportunidades TOMAR · mejor: "
                        f"{best['symbol']} (TOMAR {best['gate_score'] or 0:.0f}/100)")
            return (f"Escaneo {market} {tf}: {n} activos · ninguna oportunidad TOMAR: el filtro recomienda "
                    f"esperar")
        buys = sum(1 for r in results if r["action"] == "BUY")
        sells = sum(1 for r in results if r["action"] == "SELL")
        best = results[0] if results else None
        best_txt = f" · mejor: {best['symbol']} (p↑ {fmt_prob(best['prob_up'])})" if best else ""
        return f"Escaneo {market} {tf}: {n} activos · {buys} alcistas · {sells} bajistas{best_txt}"

    # ------------------------------------------------------------------ queries

    async def runs(self, limit: int = 20) -> list[dict]:
        rows = await self.rt.db.fetchall(
            "SELECT id, market, tf, started_at, finished_at, n_symbols, status, deep, message FROM scan_runs "
            "ORDER BY id DESC LIMIT ?",
            (int(clamp(limit, 1, 500)),),
        )
        return [_run_dict(r) for r in rows]

    async def results(self, run_id: int | None = None) -> tuple[dict | None, list[dict]]:
        if run_id is None:
            row = await self.rt.db.fetchone(
                "SELECT id, market, tf, started_at, finished_at, n_symbols, status, deep, message FROM scan_runs "
                "WHERE status='done' ORDER BY id DESC LIMIT 1"
            ) or await self.rt.db.fetchone(
                "SELECT id, market, tf, started_at, finished_at, n_symbols, status, deep, message FROM scan_runs "
                "ORDER BY id DESC LIMIT 1"
            )
        else:
            row = await self.rt.db.fetchone(
                "SELECT id, market, tf, started_at, finished_at, n_symbols, status, deep, message FROM scan_runs "
                "WHERE id=?",
                (int(run_id),),
            )
        if row is None:
            return None, []
        data = await self.rt.db.fetchall("SELECT data FROM scan_results WHERE run_id=? ORDER BY rank", (row["id"],))
        results = [d for d in (json_loads(r["data"]) for r in data) if isinstance(d, dict)]
        return _run_dict(row), results

    # ------------------------------------------------------------------ quick signals (any symbol)

    async def _market_for(self, symbol: str) -> str:
        exchange, _ = parse_symbol(symbol)
        hub = self.rt.hub
        if hub is not None and hasattr(hub, "symbol_info"):
            try:
                info = await asyncio.wait_for(hub.symbol_info(symbol), timeout=10)
                market = str((info or {}).get("market") or "").strip().lower()
                if market:
                    return market
            except Exception:
                pass
        return market_for_exchange(exchange) or "america"

    async def quick_signal(self, symbol: str, tf: str = "1h") -> dict | None:
        """QuickSignal for ANY TradingView symbol (single-ticker scan); local candles as fallback."""
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return None
        tf = normalize_tf(tf or "1h")
        cached = self._quick_cache.get((symbol, tf))
        if cached is not None:
            return cached
        result: dict | None = None
        hub = self.rt.hub
        if not symbol.startswith("SIM:") and not config.OFFLINE and hub is not None and hasattr(hub, "tv_scan"):
            market = await self._market_for(symbol)
            for columns in (BASE_COLUMNS + MTF_COLUMNS, BASE_COLUMNS):
                try:
                    data = await asyncio.wait_for(hub.tv_scan(market, columns, tickers=[symbol], limit=1), timeout=20)
                except Exception:
                    continue
                rows = [r for r in (data or {}).get("rows") or [] if isinstance(r, dict)]
                if rows:
                    horizon = self.horizon_s()
                    model = await self.get_model(model_key(market, tf, horizon))
                    ev = evaluate_row(rows[0], tf, model)
                    if ev is not None:
                        result = {
                            "symbol": symbol, "tf": tf, "score": ev.score, "prob_up": ev.prob_up,
                            "action": ev.action, "confidence": ev.confidence, "rating": ev.rating,
                            "price": ev.price, "reasons": ev.reasons, "model_updates": ev.model_updates,
                            "time": now_s(),
                        }
                    break
        if result is None and hub is not None and hasattr(hub, "get_candles"):
            try:
                cs = await asyncio.wait_for(hub.get_candles(symbol, tf, 300), timeout=30)
                local = local_quick_from_candles(cs, tf) if cs is not None else None
            except Exception:
                local = None
            if local is not None:
                result = {"symbol": symbol, "tf": tf, **local, "time": now_s()}
        if result is not None:
            result = clean_json(result)
            self._quick_cache.set((symbol, tf), result)
        return result

    def quick_score_rows(self, rows: list[dict], market: str | None = None, tf: str | None = None) -> list[tuple[float, float] | None]:
        """(score, prob_up) per TradingView row (used by /api/universe). Sync, in-memory model only."""
        try:
            tf_n = normalize_tf(tf or self._settings("scanner.tf", "1h"))
        except ValueError:
            tf_n = "1h"
        key = model_key((market or "crypto").lower(), tf_n, self.horizon_s())
        model = self._models.get(key)
        out: list[tuple[float, float] | None] = []
        for row in rows or []:
            try:
                if not isinstance(row, dict) or (_f(row, "close") or 0) <= 0:
                    out.append(None)
                    continue
                feats, completeness = extract_features(row)
                if completeness <= 0:
                    out.append(None)
                    continue
                p, score, _, _ = evaluate_features(feats, completeness, tf_n, model)
                out.append((round(score, 4), round(p, 4)))
            except Exception:
                out.append(None)
        return out

    # ------------------------------------------------------------------ background loop

    async def _loop(self) -> None:
        self._next_run_at = time.monotonic() + FIRST_RUN_DELAY_S
        while True:
            try:
                wait = self._next_run_at - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(min(wait, 5.0))
                    continue
                interval = max(60, int(self._settings("scanner.interval_s", 900) or 900))
                self._next_run_at = time.monotonic() + interval
                if not self._settings("scanner.enabled", True) or config.OFFLINE or not config.BACKGROUND:
                    continue
                deep_top = int(self._settings("scanner.deep_top", 12) or 12)
                deep = self.rt.nexus is not None
                for market in list(self._settings("scanner.markets", ["crypto"]) or []):
                    try:
                        await self.run_scan(market, None, None, deep=deep, deep_top=deep_top, wait=True)
                    except ScanError as exc:
                        if self._log.should_log(f"loop:{market}", 600):
                            self._emit("warn", f"Escaneo automático {market}: {exc}")
                self._next_run_at = time.monotonic() + interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._log.should_log("scanner-loop", 300):
                    self._emit("error", f"Bucle del escáner: {type(exc).__name__}: {exc}")
                await asyncio.sleep(30)


def apply_deep(row: dict, resp: dict | None) -> dict | None:
    """Fill a ScanRow with the NEXUS + gate verdict. Returns the GateResult used (or None)."""
    if not isinstance(resp, dict):
        return None
    current = resp.get("current") or {}
    if not resp.get("ok", True) or not current:
        msg = resp.get("message") or "historial insuficiente para NEXUS"
        row["reasons"] = [f"NEXUS: {msg}"] + list(row.get("reasons") or [])
        return None
    if row.get("action") == "BUY":
        direction = "long"
    elif row.get("action") == "SELL":
        direction = "short"
    else:
        direction = "long" if (safe_float(current.get("score")) or 0.0) >= 0 else "short"
    gate = ((current.get("gate") or {}).get(direction)) or None
    if not isinstance(gate, dict):
        return None
    prediction = resp.get("prediction") or {}
    row.update(
        {
            "deep": True,
            "verdict": gate.get("verdict"),
            "gate_score": safe_float(gate.get("score")),
            "direction": direction,
            "regime": current.get("regime"),
            "sl": safe_float(gate.get("sl")),
            "tp1": safe_float(gate.get("tp1")),
            "tp2": safe_float(gate.get("tp2")),
            "expected_move_pct": safe_float(prediction.get("expected_return_pct")),
        }
    )
    head = f"Filtro {gate_line(gate)}"
    summary = gate.get("summary")
    reasons = [f"{head}: {summary}" if summary else head]
    if gate.get("verdict") != "TAKE":
        reasons += [f"En contra: {r}" for r in (gate.get("reasons_against") or [])[:2]]
    else:
        reasons += list(gate.get("reasons_for") or [])[:2]
    row["reasons"] = (reasons + list(row.get("reasons") or []))[:8]
    return gate


def rank_rows(rows: list[dict]) -> None:
    """Deep TAKE first (by gate score), then |score| × confidence."""

    def key(r: dict) -> tuple:
        take = bool(r.get("deep")) and r.get("verdict") == "TAKE"
        return (0 if take else 1, -(r.get("gate_score") or 0.0) if take else 0.0,
                -abs(r.get("score") or 0.0) * (r.get("confidence") or 0.0))

    rows.sort(key=key)
    for i, r in enumerate(rows):
        r["rank"] = i + 1


def _run_dict(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "market": row["market"],
        "tf": row["tf"],
        "started_at": int(row["started_at"]),
        "finished_at": int(row["finished_at"]) if row.get("finished_at") is not None else None,
        "n_symbols": int(row.get("n_symbols") or 0),
        "status": row.get("status") or "done",
        "deep": bool(row.get("deep")),
        "message": row.get("message"),
    }
