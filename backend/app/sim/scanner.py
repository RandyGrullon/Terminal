"""Market scanner & market-wide learning (ScannerService, `rt.scanner`).

Quick mode (every asset of a market): one TradingView scan pulling technical columns at several timeframes →
normalised features + market-wide context of that scan (BTC/ETH trend and momentum, breadth over 1h/4h/1d,
dispersion, market regime) → scanner model v2 per "market|tf|horizon_s" (`ScannerModel`, persisted in `model_state`
kind='scanner'):
- direction: P(up) from a scan-level part (bias + context + regime intercepts) and a cross-sectional part (the
  asset's features minus the scan's mean, so a market-wide shift of every indicator is never read as 150 separate
  signals); magnitude: P(|move| > neutral band) from a volatility prior (Volatility.D scaled to the horizon) plus
  learned corrections. The prediction is the outcome with the highest expected hit rate: up (P(up)), down
  (1 − P(up)) or neutral (P(move inside the band)) — neutral when the expected move is small versus the band;
- learning: one scan = one lesson for the scan-level part (its mean error), one lesson per row (capped) for the
  cross-sectional part, every lesson weighted by Δt / horizon (overlapping 4 h windows are mostly the same market
  move); plain SGD with forgetting of the scan-level weights (adapts to regime changes, no vanishing step);
- causal Platt recalibration per model key, applied only after a minimum sample (rows and independent lessons);
- while the cross-sectional part is cold it is blended with a rule-based prior (ratings + trend alignment +
  momentum) centred on the scan's median, and the reasons say so; the prior's market-wide tilt is never used (the
  market view comes only from the learned scan-level part, which starts at zero).
Every scanned asset gets a `predictions` row that the PredictionEvaluator resolves after `scanner.horizon_s`,
feeding the model (feedback loop → honest out-of-sample hit rate, always compared with the baseline: the most
frequent outcome of the period, see app.sim.evaluator.skill_report). The legacy v1 model (`OnlineLogit`, one SGD
step per row) is kept for the offline replay that compares both designs on the same stored features.

Deep mode: the top candidates with real history run the full NEXUS engine + Trade Gate (`rt.nexus.analyze`) →
verdict TOMAR / ESPERAR / NO TOMAR, SL/TP, regime, expected move; every gate decision is logged in `gate_log`.
A gate TOMAR is an "oportunidad TOMAR" only when NEXUS itself signals that entry (BUY for long, SELL for short);
otherwise the row reads "Filtro favorable · sin señal NEXUS". Outdated series (a venue that stopped trading) are
skipped: no verdict and no gate_log row.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app import config
from app.sim.common import (
    ACTION_LABEL,
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
from app.engine.service_watch import gate_target_ms
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
# Signature of the feature list stored with every compact vector: a later change of FEATURES can never map old
# values to the wrong names (such rows are simply not used for training).
FEATURE_SIG = hashlib.sha1(",".join(FEATURE_NAMES).encode("utf-8")).hexdigest()[:8]

COLD_UPDATES = 200
ACTION_THRESHOLD = 0.15
QUICK_TTL_S = 60.0
FIRST_RUN_DELAY_S = 20.0
COLUMN_MODE_TTL_S = 6 * 3600.0  # how long a column/filter fallback is remembered per market
STALE_BARS = 3  # a 24/7 (crypto) series whose newest bar is further behind is outdated: no deep verdict...
STALE_QUIET_S = 3600  # ...once it also closed more than this ago (same rule as app.data.hub.stale_note)
# ScanRow fields stored in their own scan_results columns (not repeated inside `data`)
RESULT_COLUMNS = ("rank", "symbol", "score", "prob_up", "action", "confidence", "price", "change_pct", "volume",
                  "verdict", "gate_score")
RESULT_DEFAULTS: dict[str, Any] = {
    "type": "", "rating": None, "deep": False, "direction": None, "regime": None, "sl": None, "tp1": None,
    "tp2": None, "expected_move_pct": None, "reasons": [], "nexus_action": None, "opportunity": None,
}


# Row values of the v2 model that are not base features (volatility scale and liquidity for the magnitude model).
EXTRA_FEATURES: list[tuple[str, str]] = [
    ("vol_d_log", "Volatilidad diaria (log, sin recortar)"),
    ("atr_log", "ATR diario / precio (log)"),
    ("bbw_log", "Ancho de las Bandas de Bollinger (log)"),
    ("liq_log", "Volumen negociado (log)"),
]
EXTRA_NAMES = [name for name, _ in EXTRA_FEATURES]
EXTRA_SIG = hashlib.sha1(",".join(EXTRA_NAMES).encode("utf-8")).hexdigest()[:8]
# Market-wide context of one scan (the same for every row of that scan).
REGIMES = ("trend_up", "trend_down", "range", "volatile")
REGIME_TEXT = {"trend_up": "alcista", "trend_down": "bajista", "range": "lateral", "volatile": "volátil"}
CONTEXT_FEATURES: list[tuple[str, str]] = [
    ("ctx_breadth_1h", "Amplitud del mercado 1h (activos al alza)"),
    ("ctx_breadth_4h", "Amplitud del mercado 4h"),
    ("ctx_breadth_1d", "Amplitud del mercado diaria"),
    ("ctx_mean_4h", "Movimiento medio del mercado 4h"),
    ("ctx_dispersion", "Dispersión entre activos (4h, log)"),
    ("ctx_majors_trend", "Tendencia de los principales (BTC/ETH)"),
    ("ctx_majors_mom", "Momento de los principales (BTC/ETH)"),
    ("ctx_majors_vol", "Volatilidad de los principales (log)"),
] + [(f"reg_{r}", f"Régimen de mercado {REGIME_TEXT[r]}") for r in REGIMES]
CONTEXT_NAMES = [name for name, _ in CONTEXT_FEATURES]
CONTEXT_SIG = hashlib.sha1(",".join(CONTEXT_NAMES).encode("utf-8")).hexdigest()[:8]


def _r4(v: Any) -> float | None:
    f = safe_float(v)
    return None if f is None or not math.isfinite(f) else round(f, 4)


PACK_SCALE = 10_000  # fixed point with 4 decimals, the precision of the JSON lists
PACK_NULL = -32768  # a missing value
PACK_MAX = 32767 / PACK_SCALE  # ±3.2767: every stored value is clipped to ±3 already


def _pack(values: list[float | None]) -> str:
    """Values as little-endian int16 fixed point (4 decimals, PACK_NULL when missing), base64."""
    q = [PACK_NULL if v is None else int(round(clamp(v, -PACK_MAX, PACK_MAX) * PACK_SCALE)) for v in values]
    return base64.b64encode(np.asarray(q, dtype="<i2").tobytes()).decode("ascii")


def _unpack(text: Any) -> list[float | None] | None:
    if not isinstance(text, str):
        return None
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeError):  # binascii.Error is a ValueError
        return None
    if not raw or len(raw) % 2:
        return None
    return [None if v == PACK_NULL else v / PACK_SCALE for v in np.frombuffer(raw, dtype="<i2").tolist()]


def encode_features(feats: dict[str, float], context: dict[str, float] | None = None) -> str:
    """Compact `predictions.features`.

    v2 (base features only) = JSON list in FEATURE_NAMES order (4 decimals, null when missing). v4 (what a scan
    stores: base features + EXTRA_NAMES + the scan's context in CONTEXT_NAMES order) = the same values packed as
    little-endian int16 fixed point in base64 ("q", see `_pack`), with the signature of each list ("f", "g" and, with
    a context, "s"): ≈ 0,2 KB per row instead of ≈ 0,45 KB as JSON lists (v3), so a week of scans can be kept for
    the offline replay, which rebuilds exactly what the model saw (same 4 decimals)."""
    values = [_r4(feats.get(n)) for n in FEATURE_NAMES]
    extras = [_r4(feats.get(n)) for n in EXTRA_NAMES]
    if context is None and all(v is None for v in extras):
        return json_dumps({"v": 2, "f": FEATURE_SIG, "x": values})
    out: dict[str, Any] = {"v": 4, "f": FEATURE_SIG, "g": EXTRA_SIG}
    packed = values + extras
    if context:
        out["s"] = CONTEXT_SIG
        packed += [_r4(context.get(n)) for n in CONTEXT_NAMES]
    out["q"] = _pack(packed)
    return json_dumps(out)


def _decode_packed(data: dict) -> dict[str, float] | None:
    """v4: base features, then extras and context when their signatures match the current lists."""
    vals = _unpack(data.get("q"))
    if vals is None or data.get("f") != FEATURE_SIG or len(vals) < len(FEATURE_NAMES):
        return None
    out = {name: v for name, v in zip(FEATURE_NAMES, vals) if v is not None}
    rest = vals[len(FEATURE_NAMES):]
    if data.get("g") == EXTRA_SIG and len(rest) >= len(EXTRA_NAMES):
        out.update({name: v for name, v in zip(EXTRA_NAMES, rest) if v is not None})
        ctx = rest[len(EXTRA_NAMES):]
        if data.get("s") == CONTEXT_SIG and len(ctx) == len(CONTEXT_NAMES):
            out.update({name: v for name, v in zip(CONTEXT_NAMES, ctx) if v is not None})
    return out or None


def decode_features(raw: Any) -> dict[str, float] | None:
    """Feature dict from `predictions.features` (packed v4, compact v2/v3 lists or legacy {"x": {name: value}}); None
    if unusable. v3/v4 rows also return their extras and context values (names never collide with FEATURE_NAMES); a
    part whose signature does not match the current lists is left out."""
    data = json_loads(raw, None)
    if not isinstance(data, dict):
        return None
    if "q" in data:
        return _decode_packed(data)
    x = data.get("x")
    if isinstance(x, list):
        if data.get("f") != FEATURE_SIG or len(x) != len(FEATURE_NAMES):
            return None
        out = {name: float(v) for name, v in zip(FEATURE_NAMES, x) if safe_float(v) is not None}
        for key, sig_key, sig, names in (("e", "g", EXTRA_SIG, EXTRA_NAMES), ("c", "s", CONTEXT_SIG, CONTEXT_NAMES)):
            part = data.get(key)
            if isinstance(part, list) and data.get(sig_key) == sig and len(part) == len(names):
                out.update({name: float(v) for name, v in zip(names, part) if safe_float(v) is not None})
        return out or None
    if isinstance(x, dict):  # legacy dicts: only their finite numbers (a corrupt value never reaches the models)
        return _finite_dict(x) or None
    if "x" in data or "v" in data:
        return None  # a versioned payload without a usable vector
    return _finite_dict(data) or None  # oldest format: the feature dict itself


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
    # absurd (subnormal) scales count as missing: their scaled forms below would underflow to 0 and divide by zero
    if atr is not None and atr <= 1e-300:
        atr = None
    if vol_d is not None and vol_d <= 1e-12:
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


def extract_extras(row: dict) -> dict[str, float]:
    """EXTRA_NAMES values of a scan row: the unclipped daily volatility, ATR / price, Bollinger width and traded value
    (log scales; missing columns → value absent). They feed the magnitude model (how far the price may move)."""
    out: dict[str, float] = {}

    def put_log(name: str, ratio: float) -> None:
        # a ratio that underflows to 0 (absurd or subnormal quotes) is left out: one odd row never stops a whole scan
        if ratio > 0:
            out[name] = _clip(math.log(ratio))

    close = _f(row, "close")
    vol_d = _f(row, "Volatility.D")
    if vol_d is not None and vol_d > 0:
        put_log("vol_d_log", vol_d / 5.0)
    atr = _f(row, "ATR")
    if atr is not None and atr > 0 and close is not None and close > 0:
        put_log("atr_log", atr / close * 100.0 / 5.0)
    up, lo = _f(row, "BB.upper"), _f(row, "BB.lower")
    if up is not None and lo is not None and up > lo and close is not None and close > 0:
        put_log("bbw_log", (up - lo) / close * 100.0 / 10.0)
    value = _f(row, CRYPTO_VALUE_COL)
    if value is None:
        value = _f(row, STOCK_VALUE_COL)
    if value is None and close is not None and _f(row, "volume") is not None:
        value = close * float(_f(row, "volume") or 0.0)
    if value is not None and value > 0:
        out["liq_log"] = _clip((math.log10(value) - 7.0) / 2.0)
    return out


def flat_band_pct(horizon_s: float) -> float:
    """|return| (%) below which a neutral prediction (direction 0) counts as correct (≈ 0.5 % at 4 h)."""
    hours = max(float(horizon_s), 60.0) / 3600.0
    return float(clamp(0.25 * math.sqrt(hours), 0.1, 1.5))


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


# ----------------------------------------------------------------------------- legacy v1 model (offline replay only)


def _sigmoid(z: float) -> float:
    z = clamp(z, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-z))


class OnlineLogit:
    """Legacy v1 scanner model: online logistic regression (AdaGrad + L2), one SGD step per resolved row on the raw
    features. Replaced by `ScannerModel` (v2) for live predictions; kept so the offline replay can measure both designs
    on the same stored features. JSON-serialisable."""

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
            v = feats.get(name)
            if v is None:
                continue
            v = v if v.__class__ is float and math.isfinite(v) else safe_float(v)
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
        parsed: list[tuple[float, int]] = []
        for item in state.get("recent") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                p, y = safe_float(item[0]), safe_float(item[1])
                if p is not None and y is not None:
                    parsed.append((p, 1 if y > 0 else 0))
        model.recent = parsed[-cls.RECENT:]
        return model


def model_key(market: str, tf: str, horizon_s: int) -> str:
    return f"{market}|{tf}|{int(horizon_s)}"


def parse_model_key(key: str) -> tuple[str, str, int]:
    """(market, tf, horizon_s) of a scanner model key ("crypto|1h|14400"); defaults for malformed keys."""
    parts = str(key or "").split("|")
    market = parts[0] if parts and parts[0] else "crypto"
    tf = parts[1] if len(parts) > 1 and parts[1] else "1h"
    try:
        horizon = int(parts[2]) if len(parts) > 2 else 14400
    except ValueError:
        horizon = 14400
    return market, tf, max(60, horizon)


async def load_scanner_model(db: Any, key: str) -> "ScannerModel":
    row = await db.fetchone("SELECT state FROM model_state WHERE kind='scanner' AND key=?", (key,))
    return ScannerModel.from_state(json_loads(row["state"]) if row else None, key=key)


async def save_scanner_model(db: Any, key: str, model: "ScannerModel | OnlineLogit") -> None:
    await db.execute(
        "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES ('scanner', ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, key) DO UPDATE SET state=excluded.state, updates=excluded.updates, "
        "trained_until=excluded.trained_until, updated_at=excluded.updated_at",
        (key, json_dumps(model.to_state()), model.updates, model.trained_until, now_ms()),
    )


# ----------------------------------------------------------------------------- market context of one scan

MIN_CONTEXT_ROWS = 5
MAJOR_BASES = ("BTC", "ETH")
MAJORS_FALLBACK_N = 5  # other markets: the most liquid assets of the scan stand for "the majors"
REGIME_VOL_LOG = math.log(2.0)  # majors' daily volatility ≥ 6 % (3 % × 2): volatile market
REGIME_SHOCK_4H = 1.5  # the universe moved ≥ 1.5 volatility units on average over 4 h: volatile market
REGIME_TREND = 0.3


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _vol_d(feats: dict[str, float]) -> float | None:
    """Daily volatility (%) of a row: the unclipped extra, else the base feature (capped at 20 %), else ATR / price.
    The logs are read within the ±3 every stored vector holds (a legacy or corrupt row cannot overflow exp())."""
    if "vol_d_log" in feats:
        return 5.0 * math.exp(_clip(feats["vol_d_log"]))
    if "volatility" in feats:
        return max(5.0 * (feats["volatility"] + 1.0), 0.05)
    if "atr_log" in feats:
        return 5.0 * math.exp(_clip(feats["atr_log"]))
    return None


def _trend_score(f: dict[str, float]) -> float | None:
    parts = [math.tanh(f[k]) for k in ("ema_60", "ema_240", "px_ema50", "sma_cross") if k in f]
    if "rec_240" in f:
        parts.append(f["rec_240"])
    return _mean(parts)


def _mom_score(f: dict[str, float]) -> float | None:
    m = _mean([f[k] for k in ("chg_60", "chg_240", "macd_60") if k in f])
    return math.tanh(m / 1.5) if m is not None else None


def major_indices(symbols: list[str], market: str) -> list[int]:
    """Rows that stand for the market: BTC and ETH in crypto, else the most liquid assets (the scan is sorted)."""
    if market == "crypto":
        idx = [i for i, s in enumerate(symbols) if crypto_base({}, str(s or "")) in MAJOR_BASES]
        if idx:
            return idx
    return list(range(min(MAJORS_FALLBACK_N, len(symbols))))


def classify_regime(ctx: dict[str, float]) -> str:
    """trend_up | trend_down | range | volatile (the engine's vocabulary) from the scan context."""
    if ctx.get("ctx_majors_vol", 0.0) >= REGIME_VOL_LOG or abs(ctx.get("ctx_mean_4h", 0.0)) >= REGIME_SHOCK_4H:
        return "volatile"
    trend = 0.5 * ctx.get("ctx_majors_trend", 0.0) + 0.5 * ctx.get("ctx_breadth_1d", 0.0)
    if trend >= REGIME_TREND:
        return "trend_up"
    if trend <= -REGIME_TREND:
        return "trend_down"
    return "range"


def market_context(feats_list: list[dict[str, float]], symbols: list[str], market: str) -> dict[str, float]:
    """Context of one scan from its rows' features: breadth (share of assets up over 1h / 4h / 1d mapped to −1..1),
    mean and dispersion of the 4 h move, trend / momentum / volatility of the majors and the regime one-hot. Built
    from the features only, so the offline replay rebuilds the same values."""
    ctx: dict[str, float] = {}

    def breadth(key: str) -> float | None:
        vals = [f[key] for f in feats_list if key in f]
        if len(vals) < MIN_CONTEXT_ROWS:
            return None
        return 2.0 * sum(1 for v in vals if v > 0) / len(vals) - 1.0

    for name, key in (("ctx_breadth_1h", "chg_60"), ("ctx_breadth_4h", "chg_240"), ("ctx_breadth_1d", "chg_d")):
        v = breadth(key)
        if v is not None:
            ctx[name] = round(v, 4)
    chg = [f["chg_240"] for f in feats_list if "chg_240" in f]
    if len(chg) >= MIN_CONTEXT_ROWS:
        arr = np.asarray(chg, dtype=float)
        ctx["ctx_mean_4h"] = round(float(arr.mean()), 4)
        ctx["ctx_dispersion"] = round(_clip(math.log(max(float(arr.std()), 0.05))), 4)
    majors = [feats_list[i] for i in major_indices(symbols, market) if i < len(feats_list)]
    for name, fn in (("ctx_majors_trend", _trend_score), ("ctx_majors_mom", _mom_score)):
        m = _mean([v for v in (fn(f) for f in majors) if v is not None])
        if m is not None:
            ctx[name] = round(float(clamp(m, -1.0, 1.0)), 4)
    vols = [v for v in (_vol_d(f) for f in majors) if v is not None and v > 0]
    if vols:
        ctx["ctx_majors_vol"] = round(_clip(math.log(float(np.median(vols)) / 3.0)), 4)
    regime = classify_regime(ctx)
    for r in REGIMES:
        ctx[f"reg_{r}"] = 1.0 if r == regime else 0.0
    return ctx


def context_regime(ctx: dict[str, float] | None) -> str | None:
    for r in REGIMES:
        if (ctx or {}).get(f"reg_{r}", 0.0) >= 0.5:
            return r
    return None


def context_text(ctx: dict[str, float] | None, market: str) -> str | None:
    """'Mercado: 62 % de los activos al alza en 4 h · BTC/ETH en tendencia alcista · régimen alcista'."""
    if not ctx:
        return None
    parts: list[str] = []
    b4 = ctx.get("ctx_breadth_4h")
    if b4 is not None:
        parts.append(f"{fmt_prob((b4 + 1.0) / 2.0)} de los activos al alza en 4 h")
    trend = ctx.get("ctx_majors_trend")
    if trend is not None:
        who = "BTC/ETH" if market == "crypto" else "los más líquidos"
        word = "alcista" if trend >= 0.25 else "bajista" if trend <= -0.25 else "sin tendencia clara"
        parts.append(f"{who} {'en tendencia ' + word if word != 'sin tendencia clara' else word}")
    regime = context_regime(ctx)
    if regime:
        parts.append(f"régimen {REGIME_TEXT[regime]}")
    return ("Mercado: " + " · ".join(parts)) if parts else None


# ----------------------------------------------------------------------------- scanner model v2: building blocks

MODEL_VERSION = 2
DIR_COMMON = list(CONTEXT_NAMES)  # scan-level inputs of the direction model (+ its bias)
DIR_ROW = [n for n in FEATURE_NAMES if n != "volatility"] + ["beta_x_mkt"]  # cross-sectional (demeaned) inputs
MAG_COMMON = ["ctx_majors_vol", "ctx_dispersion", "ctx_abs_mean_4h"] + [f"reg_{r}" for r in REGIMES]
MAG_ROW = ["mag_prior", "atr_log", "bbw_log", "liq_log", "rel_vol", "abs_chg_60", "abs_chg_240", "abs_chg_d",
           "abs_bb_pos"]
MAG_RAW = ("mag_prior",)  # absolute input (not demeaned): each asset's own volatility prior
# the row inputs whose scan mean the model uses (beta_x_mkt is built from vol_rel)
MEAN_INPUTS = tuple(dict.fromkeys(["vol_rel" if n == "beta_x_mkt" else n for n in DIR_ROW] + MAG_ROW))
DERIVED_LABELS = {
    "beta_x_mkt": "Volatilidad relativa × movimiento del mercado 4h",
    "ctx_abs_mean_4h": "Tamaño del movimiento del mercado 4h",
    "mag_prior": "Movimiento esperado por la volatilidad",
    "abs_chg_60": "Tamaño del cambio 1h",
    "abs_chg_240": "Tamaño del cambio 4h",
    "abs_chg_d": "Tamaño del cambio diario",
    "abs_bb_pos": "Distancia al centro de Bollinger",
}
MAG_KAPPA = 0.5  # σ of the move over the horizon ≈ 0.5 × Volatility.D × √(horizon / 1 día) (measured on crypto scans)
ROW_CAP = 50  # cross-sectional lessons per scan at most: assets of one sector still move together
MIN_TIME_WEIGHT = 1.0 / 64.0
COLD_LESSONS = 30.0  # independent market lessons (Σ Δt / horizon) before the scan-level part is fully trusted
COLD_ROW_LESSONS = 1500.0  # cross-sectional lessons before the row part is fully trusted (≈ 30 windows × 50)
CAL_MIN_ROWS = 300  # Platt recalibration applies only after this many resolved rows ...
CAL_MIN_LESSONS = 3.0  # ... and this many independent horizon windows
# While the row part is cold, the rule prior fills in for it, centred on the scan's median: only its cross-sectional
# part (this asset vs. the typical asset of the scan) is used. Its market-wide tilt (every asset looks bearish after a
# drop) is unvalidated market timing and is ignored: the market view comes only from the learned scan-level part,
# which starts at zero.
PRIOR_REL_K = 1.0
RECENT_SCANS = 96  # per-scan outcome summaries kept for the "acierto reciente" line (≈ 1 day at 15 min)
CONTEXT_MAX_AGE_MS = 6 * 3600 * 1000  # quick reads use the last scan's context while it is this recent
STRAGGLER_MIN_ROWS = 10  # rows of an already learned scan that resolve later: learned only in groups this large
W_MAX = 4.0


def _sigmoid_arr(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def mag_prior_logit(vol_d: float | None, horizon_s: float) -> float | None:
    """logit P(|move| > neutral band) for a Gaussian move with σ = MAG_KAPPA × vol_d × √(horizon / 1 día)."""
    if vol_d is None or vol_d <= 0:
        return None
    sigma = MAG_KAPPA * vol_d * math.sqrt(max(float(horizon_s), 60.0) / 86400.0)
    p = math.erfc(flat_band_pct(horizon_s) / (sigma * math.sqrt(2.0)))
    p = clamp(p, 0.02, 0.98)
    return math.log(p / (1.0 - p))


def decide_direction(p_up: float, p_move: float) -> int:
    """The outcome with the highest expected hit rate under the judge rule: up is right when the return is > 0
    (P = p_up), down when < 0 (1 − p_up), neutral when |return| ≤ band (1 − p_move)."""
    if 1.0 - p_move > max(p_up, 1.0 - p_up):
        return 0
    return 1 if p_up >= 0.5 else -1


class StructuredLogit:
    """Online logistic model split into a scan-level part (bias + context, identical for every row of one scan) and a
    row-level part (the asset's inputs; demeaned ones are relative to the scan's mean). One scan update:

    - the scan-level weights learn from the scan's mean error: one market move is ONE lesson, not one per asset;
    - the row weights learn from the cross-sectional deviations (e_i − ē)(x_i − x̄): one lesson per row, at most
      ROW_CAP per scan;
    - every lesson is weighted by the time it adds (Δt / horizon): scans 15 min apart judged over 4 h windows are
      mostly the same move;
    - plain SGD (no step that decays to zero), L2 towards an anchor and forgetting of the active scan-level weights
      (half-life in lessons): a stale market bias fades and adaptation to a new regime does not stall.
    """

    def __init__(self, common: list[str], row: list[str], *, raw: tuple[str, ...] = (), anchor: dict | None = None,
                 lr_common: float = 0.2, lr_row: float = 0.004, l2_common: float = 0.1, l2_row: float = 0.2,
                 half_life: float = 15.0):
        self.common = list(common)
        self.row = list(row)
        self.raw = tuple(raw)
        self.anchor_r = np.array([float((anchor or {}).get(n, 0.0)) for n in self.row])
        self.wc = np.zeros(len(self.common))
        self.wr = self.anchor_r.copy()
        self.b = 0.0
        self.lr_c, self.lr_r = float(lr_common), float(lr_row)
        self.l2_c, self.l2_r = float(l2_common), float(l2_row)
        self.half_life = float(half_life)
        self.lessons = 0.0
        self.row_lessons = 0.0

    def parts(self, c: np.ndarray, r: np.ndarray) -> tuple[float, np.ndarray]:
        """(scan-level logit, row-level logits)."""
        return self.b + float(self.wc @ c), r @ self.wr

    def step(self, c: np.ndarray, r: np.ndarray, y: np.ndarray, lessons_c: float, lessons_r: float) -> np.ndarray:
        zc, zr = self.parts(c, r)
        p = _sigmoid_arr(zc + zr)
        e = p - y
        eb = float(e.mean())
        rb = r.mean(axis=0)
        g_cross = ((e - eb)[:, None] * (r - rb)).mean(axis=0)
        # Scan-level inputs that are 0 in this scan (another regime's intercept) are neither regularised nor forgotten:
        # each regime keeps what it learned until it comes back.
        active = c != 0
        self.b -= self.lr_c * lessons_c * (eb + self.l2_c * self.b)
        self.wc -= self.lr_c * lessons_c * (eb * c + self.l2_c * self.wc * active)
        self.wr -= self.lr_r * (lessons_c * eb * rb + lessons_r * (g_cross + self.l2_r * (self.wr - self.anchor_r)))
        keep = 0.5 ** (lessons_c / self.half_life)
        self.b *= keep
        self.wc[active] *= keep
        np.clip(self.wc, -W_MAX, W_MAX, out=self.wc)
        np.clip(self.wr, -W_MAX, W_MAX, out=self.wr)
        self.b = float(clamp(self.b, -W_MAX, W_MAX))
        self.lessons += lessons_c
        self.row_lessons += lessons_r
        return p

    def weights(self) -> dict[str, float]:
        out = {n: float(w) for n, w in zip(self.common, self.wc)}
        out.update({n: float(w) for n, w in zip(self.row, self.wr)})
        return out

    def to_state(self) -> dict:
        return {"common": self.common, "row": self.row, "wc": [round(float(v), 6) for v in self.wc],
                "wr": [round(float(v), 6) for v in self.wr], "b": round(self.b, 6),
                "lessons": round(self.lessons, 5), "row_lessons": round(self.row_lessons, 4)}

    def load(self, state: Any) -> None:
        """Weights by name (a later change of the input lists keeps what still matches)."""
        if not isinstance(state, dict):
            return
        for names, key, target in ((state.get("common") or [], "wc", self.wc), (state.get("row") or [], "wr", self.wr)):
            values = state.get(key) or []
            index = {n: i for i, n in enumerate(self.common if key == "wc" else self.row)}
            for j, name in enumerate(names):
                i = index.get(name)
                if i is not None and j < len(values) and safe_float(values[j]) is not None:
                    target[i] = float(clamp(float(values[j]), -W_MAX, W_MAX))
        self.b = float(clamp(safe_float(state.get("b"), 0.0) or 0.0, -W_MAX, W_MAX))
        self.lessons = max(float(safe_float(state.get("lessons"), 0.0) or 0.0), 0.0)
        self.row_lessons = max(float(safe_float(state.get("row_lessons"), 0.0) or 0.0), 0.0)


class Platt:
    """Causal Platt recalibration p = σ(a·z + b) of one model key. Learned prequentially (each resolved scan is scored
    with the current a, b before they learn from it) with the model's lesson weights (b: one lesson per scan; a: also
    the cross-sectional part). Applied only after CAL_MIN_ROWS rows and CAL_MIN_LESSONS independent windows; until
    then p = σ(z)."""

    LR = 0.05
    L2 = 0.05

    def __init__(self) -> None:
        self.a = 1.0
        self.b = 0.0
        self.rows = 0
        self.lessons = 0.0

    def active(self) -> bool:
        return self.rows >= CAL_MIN_ROWS and self.lessons >= CAL_MIN_LESSONS

    def prob(self, z: np.ndarray) -> np.ndarray:
        return _sigmoid_arr(self.a * z + self.b if self.active() else z)

    def update(self, z: np.ndarray, y: np.ndarray, lessons_c: float, lessons_r: float) -> None:
        if not len(z):
            return
        p = _sigmoid_arr(self.a * z + self.b)
        e = p - y
        eb, zb = float(e.mean()), float(z.mean())
        ga_cross = float(((e - eb) * (z - zb)).mean())
        self.b -= self.LR * lessons_c * (eb + self.L2 * self.b)
        self.a -= self.LR * (lessons_c * eb * zb + lessons_r * (ga_cross + self.L2 * (self.a - 1.0)))
        self.a = float(clamp(self.a, 0.0, 2.0))
        self.b = float(clamp(self.b, -2.0, 2.0))
        self.rows += int(len(z))
        self.lessons += float(lessons_c)

    def to_state(self) -> dict:
        return {"a": round(self.a, 6), "b": round(self.b, 6), "rows": self.rows, "lessons": round(self.lessons, 5)}

    @classmethod
    def from_state(cls, state: Any) -> "Platt":
        cal = cls()
        if isinstance(state, dict):
            cal.a = float(clamp(safe_float(state.get("a"), 1.0) or 0.0, 0.0, 2.0))
            cal.b = float(clamp(safe_float(state.get("b"), 0.0) or 0.0, -2.0, 2.0))
            cal.rows = max(int(safe_float(state.get("rows"), 0) or 0), 0)
            cal.lessons = max(float(safe_float(state.get("lessons"), 0.0) or 0.0), 0.0)
        return cal


@dataclass
class LearnSample:
    """One resolved scanner prediction handed to the model by the evaluator."""

    key: str
    feats: dict
    up: int  # 1 when the return was > 0
    label_time: int | None = None  # unix s of the target
    created_ms: int | None = None  # the scan it belongs to
    ret_pct: float | None = None
    symbol: str = ""
    direction: int | None = None  # the prediction that was recorded
    correct: int | None = None  # its verdict (1 / 0)


def as_sample(item: Any) -> LearnSample | None:
    """LearnSample from a LearnSample, a dict or the legacy tuple (model_key, features, label_up, label_time_s)."""
    if isinstance(item, LearnSample):
        return item
    if isinstance(item, dict) and item.get("key"):
        names = LearnSample.__dataclass_fields__
        return LearnSample(**{k: v for k, v in item.items() if k in names})
    if isinstance(item, (tuple, list)) and len(item) >= 4:
        key, feats, label, label_time = item[:4]
        lt = safe_float(label_time)
        return LearnSample(key=str(key), feats=dict(feats or {}), up=1 if label else 0,
                           label_time=int(lt) if lt is not None else None)
    return None


@dataclass
class Batch:
    """Inputs of one scan (or of single rows read with a remembered scan context)."""

    inputs: list[dict]
    ctx: dict
    c_dir: np.ndarray
    r_dir: np.ndarray
    c_mag: np.ndarray
    r_mag: np.ndarray
    prior: np.ndarray
    prior_center: float  # median rule prior of the scan (0 for an asset read on its own)
    means: dict[str, float]


def column_means(inputs: list[dict], names: tuple[str, ...] | None = None) -> dict[str, float]:
    """Mean of every input over the rows that have it (`names`: only these inputs; their values are finite floats,
    as `ScannerModel.row_inputs` builds them)."""
    if names is not None:
        out: dict[str, float] = {}
        for k in names:
            total, count = 0.0, 0
            for x in inputs:
                v = x.get(k)
                if v is not None:
                    total += v
                    count += 1
            if count:
                out[k] = total / count
        return out
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for x in inputs:
        for k, v in x.items():
            if isinstance(v, (int, float)) and math.isfinite(v):
                sums[k] = sums.get(k, 0.0) + float(v)
                counts[k] = counts.get(k, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}


def _finite_dict(feats: dict | None) -> dict[str, float]:
    """The finite numeric values of a feature dict as floats (fast path for floats, safe_float for the rest)."""
    out: dict[str, float] = {}
    for k, v in (feats or {}).items():
        if v.__class__ is float:
            if math.isfinite(v):
                out[k] = v
        else:
            f = safe_float(v)
            if f is not None:
                out[k] = f
    return out


def _horizon_text(horizon_s: float) -> str:
    h = float(horizon_s)
    if h >= 86400 and h % 86400 == 0:
        n = int(h // 86400)
        return f"{n} {_plural(n, 'día', 'días')}"
    if h >= 3600:
        return f"{fmt_num(h / 3600.0, 0 if h % 3600 == 0 else 1)} h"
    return f"{int(h // 60)} min"


class ScannerModel:
    """Scanner model v2 of one key "market|tf|horizon_s": direction (StructuredLogit + Platt), magnitude
    (StructuredLogit with a volatility prior + Platt), the context of the last scan (for single-symbol reads) and a
    per-scan summary of the recent verified outcomes. JSON-serialisable (model_state kind='scanner')."""

    def __init__(self, key: str = "crypto|1h|14400"):
        self.key = str(key)
        self.market, self.tf, self.horizon_s = parse_model_key(self.key)
        self.dir = StructuredLogit(DIR_COMMON, DIR_ROW, lr_common=0.3, half_life=12.0)
        self.mag = StructuredLogit(MAG_COMMON, MAG_ROW, raw=MAG_RAW, anchor={"mag_prior": 1.0}, lr_common=0.3,
                                   half_life=12.0)
        self.cal_dir = Platt()
        self.cal_mag = Platt()
        self.updates = 0
        self.scans = 0
        self.skipped = 0
        self.trained_until: int | None = None
        self.last_learned_ms: int | None = None
        self.memory: dict | None = None
        self.recent: list[list[float]] = []

    @property
    def band_pct(self) -> float:
        return flat_band_pct(self.horizon_s)

    def alpha_common(self) -> float:
        return min(self.dir.lessons / COLD_LESSONS, 1.0)

    def alpha_row(self) -> float:
        return min(self.dir.row_lessons / COLD_ROW_LESSONS, 1.0)

    def blend_alpha(self) -> float:
        return min(self.alpha_common(), self.alpha_row())

    # ------------------------------------------------------------------ inputs and prediction

    def row_inputs(self, feats: dict) -> dict:
        x = _finite_dict(feats)
        vd = _vol_d(x)
        if vd is not None:
            x["vol_rel"] = math.log(vd / 5.0)
            prior = mag_prior_logit(vd, self.horizon_s)
            if prior is not None:
                x["mag_prior"] = prior
        for k in ("chg_60", "chg_240", "chg_d", "bb_pos"):
            if k in x:
                x[f"abs_{k}"] = abs(x[k])
        x["prior"] = rule_prior(x, self.tf)
        return x

    def batch(self, feats_list: list[dict], ctx: dict | None, means: dict | None = None, *,
              absolute: bool = False) -> Batch:
        """Model inputs of rows read together. Row inputs are relative to `means` (default: the rows' own column
        means, i.e. the scan); `absolute` reads them as they are (a single asset with no scan to compare with)."""
        ctx = _finite_dict(ctx)
        inputs = [self.row_inputs(f) for f in feats_list]
        if absolute:
            means = {}
        elif not means:
            means = column_means(inputs, MEAN_INPUTS)
        else:
            means = dict(means)
        base = 0.0 if absolute else None  # the reference of an input without a mean
        n = len(inputs)
        mom = math.tanh(ctx.get("ctx_mean_4h", 0.0))
        r_dir = np.zeros((n, len(DIR_ROW)))
        r_mag = np.zeros((n, len(MAG_ROW)))
        default_prior = means.get("mag_prior", 0.0)
        for i, x in enumerate(inputs):
            for j, name in enumerate(DIR_ROW):
                src = "vol_rel" if name == "beta_x_mkt" else name
                v, mu = x.get(src), means.get(src, base)
                if v is not None and mu is not None:
                    r_dir[i, j] = (v - mu) * (mom if name == "beta_x_mkt" else 1.0)
            for j, name in enumerate(MAG_ROW):
                v = x.get(name)
                mu = means.get(name, base)
                if name in MAG_RAW:
                    r_mag[i, j] = v if v is not None else default_prior
                elif v is not None and mu is not None:
                    r_mag[i, j] = v - mu
        c_dir = np.array([ctx.get(name, 0.0) for name in DIR_COMMON])
        ctx_mag = {**ctx, "ctx_abs_mean_4h": abs(ctx.get("ctx_mean_4h", 0.0))}
        c_mag = np.array([ctx_mag.get(name, 0.0) for name in MAG_COMMON])
        prior = np.array([x["prior"] for x in inputs]) if n else np.zeros(0)
        if absolute:
            center = 0.0
        elif means.get("prior_center") is not None:
            center = float(means["prior_center"])
        else:
            center = float(np.median(prior)) if n else 0.0
        means["prior_center"] = center
        return Batch(inputs, ctx, c_dir, r_dir, c_mag, r_mag, prior, center, means)

    def predict(self, b: Batch) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(served direction logit, P(up), P(|move| > band)) per row of the batch: the learned scan-level part (the
        market view, zero until it has learned) + the row part, blended with the cross-sectional rule prior while
        the row part is cold; then the Platt recalibration once it has its minimum sample."""
        zc, zr = self.dir.parts(b.c_dir, b.r_dir)
        a_r = self.alpha_row()
        z = zc + a_r * zr + (1.0 - a_r) * PRIOR_REL_K * (b.prior - b.prior_center)
        p_up = np.clip(self.cal_dir.prob(z), 0.01, 0.99)
        mc, mr = self.mag.parts(b.c_mag, b.r_mag)
        p_move = np.clip(self.cal_mag.prob(mc + mr), 0.01, 0.99)
        return z, p_up, p_move

    # ------------------------------------------------------------------ learning

    def _time_weight(self, created_ms: int | None) -> float:
        if self.last_learned_ms is None or created_ms is None:
            return 1.0
        dt = (created_ms - self.last_learned_ms) / 1000.0
        return float(clamp(dt / self.horizon_s, MIN_TIME_WEIGHT, 1.0))

    def learn_scan(self, samples: list[LearnSample], batch: Batch | None = None) -> int:
        """One resolved scan: prequential scoring (calibrators, recent summary), then the structured updates of both
        models. Rows of an already learned scan that resolve later are learned only in groups of at least
        STRAGGLER_MIN_ROWS (with the minimum weight). `batch` = the inputs of exactly these rows, in this order, when
        the caller already built them (the offline replay); else they are rebuilt. Returns the rows learned."""
        if not samples:
            return 0
        created = samples[0].created_ms
        if created is None and samples[0].label_time is not None:
            created = (int(samples[0].label_time) - self.horizon_s) * 1000
        n = len(samples)
        straggler = self.last_learned_ms is not None and created is not None and created <= self.last_learned_ms
        if straggler and n < STRAGGLER_MIN_ROWS:
            self.skipped += n
            return 0
        w = MIN_TIME_WEIGHT if straggler else self._time_weight(created)
        if batch is not None and len(batch.inputs) == n:
            b = batch
        else:
            feats_list = [s.feats or {} for s in samples]
            first = feats_list[0]
            ctx = {k: first[k] for k in CONTEXT_NAMES if k in first}
            if context_regime(ctx) is None:  # rows stored without their context (v2 encoding): rebuilt from the scan
                ctx = market_context(feats_list, [s.symbol for s in samples], self.market)
            b = self.batch(feats_list, ctx)
        y = np.array([1.0 if s.up else 0.0 for s in samples])
        lessons_r = w * min(n, ROW_CAP)
        z, _, _ = self.predict(b)
        self.cal_dir.update(z, y, w, lessons_r)
        self.dir.step(b.c_dir, b.r_dir, y, w, lessons_r)
        idx = [i for i, s in enumerate(samples) if s.ret_pct is not None]
        flat = -1
        if idx:
            band = self.band_pct
            yb = np.array([1.0 if abs(float(samples[i].ret_pct)) > band else 0.0 for i in idx])
            r_mag = b.r_mag[idx]
            mc, mr = self.mag.parts(b.c_mag, r_mag)
            lessons_m = w * min(len(idx), ROW_CAP)
            self.cal_mag.update(mc + mr, yb, w, lessons_m)
            self.mag.step(b.c_mag, r_mag, yb, w, lessons_m)
            flat = int(len(idx) - yb.sum()) if len(idx) == n else -1
        judged = [s.correct for s in samples if s.correct is not None]
        hits = int(sum(1 for c in judged if c)) if len(judged) == n else -1
        entry = [int(created or 0), n, hits, int(y.sum()), flat, round(w, 5)]
        if straggler:  # part of a scan already counted: merged into its summary while it is still there
            for i in range(len(self.recent) - 1, -1, -1):
                prev = self.recent[i]
                if prev[0] == entry[0]:
                    self.recent[i] = [prev[0], prev[1] + n, prev[2] + hits if prev[2] >= 0 and hits >= 0 else -1,
                                      prev[3] + entry[3], prev[4] + flat if prev[4] >= 0 and flat >= 0 else -1, prev[5]]
                    break
        else:
            self.recent.append(entry)
            del self.recent[:-RECENT_SCANS]
            self.scans += 1
        self.updates += n
        if created is not None:
            self.last_learned_ms = max(self.last_learned_ms or 0, int(created))
        times = [int(s.label_time) for s in samples if s.label_time is not None]
        if times:
            self.trained_until = max(self.trained_until or 0, max(times))
        return n

    def remember(self, b: Batch, created_ms: int) -> None:
        """Context and column means of the latest scan: single-symbol reads are placed in that market."""
        keep = set(DIR_ROW) | set(MAG_ROW) | {"vol_rel", "prior_center"}
        self.memory = {"t": int(created_ms), "ctx": {k: round(v, 4) for k, v in b.ctx.items()},
                       "means": {k: round(v, 5) for k, v in b.means.items() if k in keep}}

    def remembered(self, now: int) -> tuple[dict | None, dict | None, int | None]:
        """(context if still recent, column means, age in ms) of the last scan."""
        mem = self.memory if isinstance(self.memory, dict) else None
        if not mem:
            return None, None, None
        age = max(0, int(now) - int(safe_float(mem.get("t"), 0) or 0))
        ctx = mem.get("ctx") if age <= CONTEXT_MAX_AGE_MS else None
        return ctx, mem.get("means") or None, age

    # ------------------------------------------------------------------ honest status texts

    def recent_summary(self) -> dict | None:
        """Recorded hits of the recent resolved scans vs. the reference (always the most frequent outcome)."""
        rows = [r for r in self.recent if r[2] >= 0 and r[4] >= 0 and r[1] > 0]
        if not rows:
            return None
        n = int(sum(r[1] for r in rows))
        up = int(sum(r[3] for r in rows))
        options = [(up, "siempre «alcista»"), (n - up, "siempre «bajista»"), (int(sum(r[4] for r in rows)),
                                                                           "siempre «neutral»")]
        ref_hits, ref_label = max(options, key=lambda o: o[0])
        return {"n": n, "hits": int(sum(r[2] for r in rows)), "ref_hits": int(ref_hits), "ref_label": ref_label,
                "windows": float(sum(r[5] for r in rows))}

    def status_text(self) -> str:
        lessons = fmt_num(self.dir.lessons, 1)
        if self.blend_alpha() < 1.0:
            rules = ": se combina con reglas técnicas" if self.alpha_row() < 1.0 else ""
            return (f"Modelo en aprendizaje ({lessons}/{COLD_LESSONS:.0f} ventanas de mercado independientes · "
                    f"{fmt_num(self.updates, 0)} resultados){rules}")
        cal = " · recalibrado (Platt)" if self.cal_dir.active() else ""
        return (f"Modelo entrenado con {fmt_num(self.updates, 0)} resultados ({lessons} ventanas de mercado "
                f"independientes){cal}")

    def recent_text(self) -> str | None:
        s = self.recent_summary()
        if not s:
            return None
        n = s["n"]
        text = (f"Acierto reciente {fmt_num(s['hits'], 0)} de {fmt_num(n, 0)} ({fmt_prob(s['hits'] / n)}) · "
                f"referencia {fmt_prob(s['ref_hits'] / n)} ({s['ref_label']})")
        if s["windows"] < MIN_INDEPENDENT_WINDOWS:
            text += f" · solo {fmt_num(s['windows'], 1)} ventanas independientes: sin conclusión todavía"
        return text

    def weight_table(self) -> list[dict]:
        """Learned direction weights (the asset's features, its volatility × market move and the market context)."""
        w = self.dir.weights()
        names = list(FEATURE_NAMES) + ["beta_x_mkt"] + list(CONTEXT_NAMES)
        labels = {**FEATURE_LABELS, **dict(CONTEXT_FEATURES), **DERIVED_LABELS}
        return sorted(({"name": n, "label": labels.get(n, n), "weight": round(w.get(n, 0.0), 6)} for n in names),
                      key=lambda f: -abs(f["weight"]))

    # ------------------------------------------------------------------ persistence

    def to_state(self) -> dict:
        return {
            "version": MODEL_VERSION, "key": self.key, "dir": self.dir.to_state(), "mag": self.mag.to_state(),
            "cal_dir": self.cal_dir.to_state(), "cal_mag": self.cal_mag.to_state(), "updates": self.updates,
            "scans": self.scans, "skipped": self.skipped, "trained_until": self.trained_until,
            "last_learned_ms": self.last_learned_ms, "memory": self.memory, "recent": self.recent,
        }

    @classmethod
    def from_state(cls, state: Any, key: str | None = None) -> "ScannerModel":
        """A v1 (OnlineLogit) state starts a fresh v2 model: its weights were learned from the raw, correlated rows.
        The history of its predictions stays in the statistics."""
        st = state if isinstance(state, dict) else {}
        model = cls(key or st.get("key") or "crypto|1h|14400")
        if int(safe_float(st.get("version"), 1) or 1) < MODEL_VERSION:
            return model
        model.dir.load(st.get("dir"))
        model.mag.load(st.get("mag"))
        model.cal_dir = Platt.from_state(st.get("cal_dir"))
        model.cal_mag = Platt.from_state(st.get("cal_mag"))
        model.updates = int(safe_float(st.get("updates"), 0) or 0)
        model.scans = int(safe_float(st.get("scans"), 0) or 0)
        model.skipped = int(safe_float(st.get("skipped"), 0) or 0)
        for attr in ("trained_until", "last_learned_ms"):
            v = safe_float(st.get(attr))
            setattr(model, attr, int(v) if v is not None else None)
        model.memory = st.get("memory") if isinstance(st.get("memory"), dict) else None
        recent = []
        for item in st.get("recent") or []:
            if isinstance(item, (list, tuple)) and len(item) == 6 and all(safe_float(v) is not None for v in item):
                recent.append([int(item[0]), int(item[1]), int(item[2]), int(item[3]), int(item[4]), float(item[5])])
        model.recent = recent[-RECENT_SCANS:]
        return model


MIN_INDEPENDENT_WINDOWS = 30  # no conclusion about skill with fewer independent horizon windows (n ≥ 30 rule)


async def train_scanner_models(db: Any, samples: list[Any], models: dict[str, ScannerModel] | None = None
                               ) -> dict[str, ScannerModel]:
    """Group resolved samples by model key and scan (oldest first), learn each scan once and save the touched
    models. `models` is the caller's cache (ScannerService) or None (loaded from the database)."""
    cache = models if models is not None else {}
    groups: dict[str, dict[int, list[LearnSample]]] = {}
    for item in samples:
        s = as_sample(item)
        if s is None:
            continue
        created = s.created_ms
        if created is None:
            _, _, horizon = parse_model_key(s.key)
            created = (int(s.label_time) - horizon) * 1000 if s.label_time is not None else 0
        groups.setdefault(s.key, {}).setdefault(int(created), []).append(s)
    touched: dict[str, ScannerModel] = {}
    for key, by_scan in groups.items():
        model = cache.get(key)
        if model is None:
            model = cache[key] = await load_scanner_model(db, key)
        for created in sorted(by_scan):
            for s in by_scan[created]:
                if s.created_ms is None:
                    s.created_ms = created
            model.learn_scan(by_scan[created])
        touched[key] = model
    for key, model in touched.items():
        await save_scanner_model(db, key, model)
    return touched


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
    prob_move: float | None = None  # v2: P(|move| > neutral band)
    direction: int = 0  # the recorded prediction: 1 up, -1 down, 0 neutral


def _directional_signals(feats: dict[str, float]) -> list[float]:
    keys = ("rec_15", "rec_60", "rec_240", "rec_all", "rec_1w", "macd_60", "macd_240", "ema_60", "ema_240", "di_trend")
    return [_sign(feats[k]) for k in keys if k in feats and feats[k] != 0]


def evaluate_features(
    feats: dict[str, float],
    completeness: float,
    tf: str,
    model: OnlineLogit | None,
    x: np.ndarray | None = None,
    prior: float | None = None,
) -> tuple[float, float, float, float]:
    """(prob_up, score, confidence, blend_alpha) from features, the learned model and the rule prior (legacy v1
    reading). `x` = model.vector(feats) and `prior` = rule_prior(feats, tf) when the caller already has them."""
    prior = rule_prior(feats, tf) if prior is None else prior
    p_prior = 0.5 + 0.25 * prior
    alpha = model.blend_alpha() if model is not None else 0.0
    p_model = model.predict(model.vector(feats) if x is None else x) if model is not None else 0.5
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


def evaluate_row(row: dict, tf: str, model: "ScannerModel | OnlineLogit | None",
                 now: int | None = None) -> QuickEval | None:
    """One row: the v2 model (placed in the market with the context of its last scan) or the legacy v1 reading."""
    if isinstance(model, ScannerModel):
        return evaluate_single(row, model, now if now is not None else now_ms())
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


# ----------------------------------------------------------------------------- evaluation with the v2 model

DIRECTION_ACTION = {1: "BUY", -1: "SELL", 0: "WAIT"}
MAX_SHALLOW_REASONS = 9


def _prepare_rows(rows: list[dict]) -> list[tuple[dict, dict, float]]:
    """(row, base features + extras, completeness) of the rows with a price and at least one feature."""
    out = []
    for row in rows:
        price = _f(row, "close")
        if price is None or price <= 0:
            continue
        feats, completeness = extract_features(row)
        if completeness <= 0:
            continue
        feats.update(extract_extras(row))
        out.append((row, feats, completeness))
    return out


def _confidence_v2(direction: int, p_up: float, p_move: float, feats: dict, completeness: float,
                   model: ScannerModel) -> float:
    """Strength of the chosen outcome (its expected hit rate above 50 %) × agreement of the technical signals ×
    completeness × maturity of the model."""
    q = (1.0 - p_move) if direction == 0 else (p_up if direction > 0 else 1.0 - p_up)
    strength = clamp((q - 0.5) * 5.0, 0.0, 1.0)
    if direction:
        signs = _directional_signals(feats)
        agreement = sum(1 for s in signs if s == direction) / len(signs) if signs else 0.5
        agree = 0.4 + 0.6 * agreement
    else:
        agree = 0.7
    maturity = 0.6 + 0.4 * model.alpha_common()
    return float(clamp(strength * agree * math.sqrt(max(completeness, 0.0)) * maturity, 0.0, 1.0))


def build_reasons_v2(row: dict, feats: dict, direction: int, p_up: float, p_move: float, model: ScannerModel,
                     ctx: dict | None, *, ctx_note: str | None = None, max_for: int = 4,
                     max_against: int = 2) -> list[str]:
    cands = _reason_candidates(row, feats, model.tf)
    band = f"±{fmt_num(model.band_pct, 2)} %"
    horizon = _horizon_text(model.horizon_s)
    reasons: list[str] = []
    if direction == 0:
        reasons.append(f"Movimiento esperado pequeño: P(moverse más de {band} en {horizon}) {fmt_prob(p_move)} → "
                       "predicción neutral (acierta si el precio queda dentro de esa banda)")
        reasons += [c[2] for c in sorted(cands, key=lambda c: -c[1])[:3]]
    else:
        favor = sorted((c for c in cands if c[0] * direction > 0), key=lambda c: -c[1])
        against = sorted((c for c in cands if c[0] * direction < 0), key=lambda c: -c[1])
        reasons += [c[2] for c in favor[:max_for]]
        reasons += [f"En contra: {c[2]}" for c in against[:max_against]]
        reasons.append(f"P↑ {fmt_prob(p_up)} en {horizon} · P(moverse más de {band}) {fmt_prob(p_move)}")
    tail: list[str] = []
    text = context_text(ctx, model.market)
    if text:
        tail.append(text + (f" ({ctx_note})" if ctx_note else ""))
    elif ctx_note:
        tail.append(f"Mercado: {ctx_note}")
    tail.append(model.status_text())
    recent = model.recent_text()
    if recent:
        tail.append(recent)
    return reasons[: max(MAX_SHALLOW_REASONS - len(tail), 1)] + tail


def _quick_eval(row: dict, feats: dict, completeness: float, model: ScannerModel, p_up: float, p_move: float,
                ctx: dict | None, ctx_note: str | None) -> QuickEval:
    direction = decide_direction(p_up, p_move)
    reasons = build_reasons_v2(row, feats, direction, p_up, p_move, model, ctx, ctx_note=ctx_note)
    return QuickEval(
        score=round(2.0 * p_up - 1.0, 4),
        prob_up=round(p_up, 4),
        action=DIRECTION_ACTION[direction],
        confidence=round(_confidence_v2(direction, p_up, p_move, feats, completeness, model), 4),
        rating=primary_rating(row, model.tf),
        price=_f(row, "close"),
        reasons=reasons,
        features={k: round(v, 5) for k, v in feats.items()},
        completeness=completeness,
        model_updates=model.updates,
        blend=model.blend_alpha(),
        prob_move=round(p_move, 4),
        direction=direction,
    )


def evaluate_scan(rows: list[dict], model: ScannerModel) -> tuple[list[tuple[dict, QuickEval]], dict, Batch | None]:
    """Every valid row of one scan read together: the scan's context and column means place each asset in its
    market. Returns ([(row, QuickEval)], context, batch)."""
    prepared = _prepare_rows(rows)
    if not prepared:
        return [], {}, None
    symbols = [str(r.get("symbol") or "").upper() for r, _, _ in prepared]
    feats_list = [f for _, f, _ in prepared]
    ctx = market_context(feats_list, symbols, model.market)
    b = model.batch(feats_list, ctx)
    _, p_up, p_move = model.predict(b)
    out = [(row, _quick_eval(row, f, comp, model, float(p_up[i]), float(p_move[i]), ctx, None))
           for i, (row, f, comp) in enumerate(prepared)]
    return out, ctx, b


def evaluate_single(row: dict, model: ScannerModel, now: int) -> QuickEval | None:
    """One symbol read with the context and column means of the market's last scan; without any scan yet the asset
    is read on its own (absolute values) and the reasons say there is no market context."""
    prepared = _prepare_rows([row])
    if not prepared:
        return None
    row, feats, comp = prepared[0]
    ctx, means, age = model.remembered(now)
    if ctx is not None:
        note = f"contexto del último escaneo, {_age_text(age / 1000.0)}"
    elif age is not None:
        note = f"sin escaneo reciente del mercado en {model.tf} (el último, {_age_text(age / 1000.0)})"
    else:
        # the context is kept per model key (market, timeframe and horizon): a 4 h read has none from the 1 h scans
        note = f"sin escaneos de este mercado en {model.tf} todavía: se lee el activo sin contexto"
    b = model.batch([feats], ctx or {}, means, absolute=means is None)
    _, p_up, p_move = model.predict(b)
    return _quick_eval(row, feats, comp, model, float(p_up[0]), float(p_move[0]), ctx or {}, note)


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


def tv_available(hub: Any) -> bool:
    """False only when the hub says TradingView is disabled/unreachable (`hub.tv_online()`); unknown → True."""
    fn = getattr(hub, "tv_online", None)
    if not callable(fn):
        return True
    try:
        return bool(fn())
    except Exception:
        return True


# ----------------------------------------------------------------------------- service


class ScannerService:
    def __init__(self, rt: Any):
        self.rt = rt
        self._started = False
        self._loop_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._scan_lock = asyncio.Lock()
        self._model_lock = asyncio.Lock()
        self._models: dict[str, ScannerModel] = {}
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
                self._models[row["key"]] = ScannerModel.from_state(json_loads(row["state"]), key=row["key"])
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

    def get_model_sync(self, key: str) -> ScannerModel | None:
        return self._models.get(key)

    async def get_model(self, key: str) -> ScannerModel:
        model = self._models.get(key)
        if model is None:
            model = await load_scanner_model(self.rt.db, key)
            self._models[key] = model
        return model

    async def learn_batch(self, samples: list[Any]) -> dict[str, ScannerModel]:
        """Learn resolved predictions: LearnSample items (or legacy (model_key, features, label_up, label_time_s)
        tuples). Grouped by model key and scan, oldest scan first: each scan is one structured update."""
        async with self._model_lock:
            return await train_scanner_models(self.rt.db, samples, self._models)

    async def _save_model(self, key: str, model: ScannerModel) -> None:
        async with self._model_lock:
            await save_scanner_model(self.rt.db, key, model)

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
        if not tv_available(hub):
            raise ScanError("TradingView no está disponible (desactivado en Ajustes o sin conexión): "
                            "el escáner necesita sus datos")
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
        evals, ctx, batch = evaluate_scan(rows, model)
        for row, ev in evals:
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
                    "nexus_action": None,
                    "opportunity": False,
                }
            )
            pred_rows.append(
                ("scanner", symbol, tf, key, created, target, ev.price, ev.direction, ev.prob_up, ev.score,
                 encode_features(ev.features, ctx))
            )
        if pred_rows:
            await self.rt.db.executemany(
                "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, "
                "prob_up, score, features) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                pred_rows,
            )
        if batch is not None:  # context of this scan: single-symbol reads of this market are placed in it
            model.remember(batch, created)
            try:
                await self._save_model(key, model)
            except Exception as exc:
                if self._log.should_log("model-save", 300):
                    self._emit("warn", f"No se pudo guardar el contexto del escáner: {exc}")
        self._publish({"event": "progress", "run_id": run_id, "progress": 0.5 if deep else 0.9,
                       "message": f"{len(results)} activos evaluados"})
        deep_done = 0
        if deep and results:
            deep_done = await self._deep(run_id, results, tf, deep_top, market)
        rank_rows(results)
        await self._persist_results(run_id, results)
        summary = self._summary(market, tf, results, deep and deep_done > 0, context_regime(ctx))
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

    async def _deep(self, run_id: int, results: list[dict], tf: str, deep_top: int, market: str = "") -> int:
        nexus, hub = self.rt.nexus, self.rt.hub
        if nexus is None or hub is None:
            self._emit("warn", "Modo profundo no disponible: el motor NEXUS no está cargado")
            return 0
        # Directional reads first; neutral ones (a move smaller than the band is expected) only fill the remaining
        # slots, so a quiet market still gets its strongest candidates analysed (NEXUS then picks the direction).
        candidates = sorted(
            results,
            key=lambda r: (r["action"] == "WAIT", -abs(r["score"]) * (0.5 + 0.5 * r["confidence"])),
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
            outdated = stale_reason(resp, tf, market, now_s())
            if outdated:  # no verdict, SL/TP or gate_log decision from weeks-old candles
                row["reasons"] = [f"NEXUS: {outdated} (sin análisis profundo)"] + list(row.get("reasons") or [])
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
            # horizon from the decision bar (close of the horizon_bars-th bar after it), not from the wall clock: the
            # convention of every gate_log writer (watcher, paper, scanner)
            target = gate_target_ms(int(t), tf, horizon_bars)
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
            # `data` keeps only what has no column of its own (and no empty values): results() rebuilds the row
            data = {k: v for k, v in r.items() if k not in RESULT_COLUMNS and v is not None}
            if data.get("description") == r["symbol"]:
                data.pop("description")
            rows.append(
                (run_id, r["symbol"], r["rank"], r["score"], r["prob_up"], r["action"], r["confidence"],
                 r["price"], r["change_pct"], r["volume"], r["verdict"], r["gate_score"], json_dumps(data))
            )
        await self.rt.db.executemany(
            "INSERT INTO scan_results(run_id, symbol, rank, score, prob_up, action, confidence, price, "
            "change_pct, volume, verdict, gate_score, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    @staticmethod
    def _summary(market: str, tf: str, results: list[dict], deep: bool, regime: str | None = None) -> str:
        n = len(results)
        head = f"Escaneo {market} {tf}: {n} {_plural(n, 'activo', 'activos')}"
        if deep:
            takes = [r for r in results if r.get("opportunity")]  # ranked: the best one first
            favorable = [r for r in results if r["deep"] and r["verdict"] == "TAKE" and not r.get("opportunity")]
            fav_txt = f"{len(favorable)} con filtro favorable sin señal NEXUS" if favorable else ""
            if takes:
                best = takes[0]
                return (f"{head} · {len(takes)} {_plural(len(takes), 'oportunidad', 'oportunidades')} TOMAR · mejor: "
                        f"{best['symbol']} (TOMAR {best['gate_score'] or 0:.0f}/100)"
                        + (f" · {fav_txt}" if fav_txt else ""))
            if favorable:
                return f"{head} · ninguna oportunidad TOMAR con señal NEXUS ({fav_txt})"
            return f"{head} · ninguna oportunidad TOMAR: el filtro recomienda esperar"
        buys = sum(1 for r in results if r["action"] == "BUY")
        sells = sum(1 for r in results if r["action"] == "SELL")
        flats = n - buys - sells
        best = next((r for r in results if r["action"] != "WAIT"), None)  # ranked: the strongest directional read
        best_txt = f" · mejor: {best['symbol']} (p↑ {fmt_prob(best['prob_up'])})" if best else ""
        regime_txt = f" · régimen {REGIME_TEXT[regime]}" if regime in REGIME_TEXT else ""
        return (f"{head} · {buys} {_plural(buys, 'alcista', 'alcistas')} · {sells} {_plural(sells, 'bajista', 'bajistas')}"
                f" · {flats} {_plural(flats, 'neutral', 'neutrales')}"
                f"{regime_txt}{best_txt}")

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
        data = await self.rt.db.fetchall(
            f"SELECT {', '.join(RESULT_COLUMNS)}, data FROM scan_results WHERE run_id=? ORDER BY rank", (row["id"],)
        )
        return _run_dict(row), [result_row(r) for r in data]

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
        """QuickSignal for ANY TradingView symbol (single-ticker scan); local candles as fallback. A fallback read from
        a series the hub flags as outdated carries `stale: True` and says so in its first reason (contract I2), so it
        is never shown as the current market."""
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return None
        tf = normalize_tf(tf or "1h")
        cached = self._quick_cache.get((symbol, tf))
        if cached is not None:
            return cached
        result: dict | None = None
        hub = self.rt.hub
        if (not symbol.startswith("SIM:") and not config.OFFLINE and hub is not None and hasattr(hub, "tv_scan")
                and tv_available(hub)):
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
                    ev = evaluate_single(rows[0], model, now_ms())
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
                if getattr(cs, "stale", False):  # series the hub flags as outdated: never read as the current market
                    from app.engine.service import stale_head

                    result["stale"] = True
                    result["reasons"] = ([f"{stale_head(cs)}: esta lectura usa esas velas antiguas"]
                                         + list(result.get("reasons") or []))
        if result is not None:
            result = clean_json(result)
            self._quick_cache.set((symbol, tf), result)
        return result

    def quick_score_rows(self, rows: list[dict], market: str | None = None, tf: str | None = None) -> list[tuple[float, float] | None]:
        """(score, prob_up) per TradingView row (used by /api/universe). Sync, in-memory model only: the rows are read
        with the context and column means of the market's last scan (or among themselves when there is none)."""
        try:
            tf_n = normalize_tf(tf or self._settings("scanner.tf", "1h"))
        except ValueError:
            tf_n = "1h"
        key = model_key((market or "crypto").lower(), tf_n, self.horizon_s())
        model = self._models.get(key) or ScannerModel(key)
        out: list[tuple[float, float] | None] = [None] * len(rows or [])
        valid: list[int] = []
        feats_list: list[dict] = []
        for i, row in enumerate(rows or []):
            try:
                if not isinstance(row, dict) or (_f(row, "close") or 0) <= 0:
                    continue
                feats, completeness = extract_features(row)
                if completeness <= 0:
                    continue
                feats.update(extract_extras(row))
                valid.append(i)
                feats_list.append(feats)
            except Exception:
                continue
        if not valid:
            return out
        try:
            ctx, means, _ = model.remembered(now_ms())
            if ctx is None:
                symbols = [str((rows[i] or {}).get("symbol") or "").upper() for i in valid]
                ctx = market_context(feats_list, symbols, model.market)
            _, p_up, _ = model.predict(model.batch(feats_list, ctx, means))
            for j, i in enumerate(valid):
                p = float(p_up[j])
                out[i] = (round(2.0 * p - 1.0, 4), round(p, 4))
        except Exception:
            pass
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


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _age_text(seconds: float) -> str:
    """Spanish age: 'hace 3 días' / 'hace 5 horas' / 'hace 40 minutos'."""
    if seconds >= 2 * 86400:
        n = int(seconds // 86400)
        return f"hace {n} {_plural(n, 'día', 'días')}"
    if seconds >= 2 * 3600:
        n = int(seconds // 3600)
        return f"hace {n} {_plural(n, 'hora', 'horas')}"
    n = max(int(seconds // 60), 1)
    return f"hace {n} {_plural(n, 'minuto', 'minutos')}"


def stale_reason(resp: dict | None, tf: str, market: str, now: int) -> str | None:
    """Why a NexusResponse must not be used as a live verdict (outdated series), else None.

    The data hub flags outdated candle sets (`stale`) and NEXUS answers ok=false for them (apply_deep reports that
    message); on top of that a 24/7 crypto series whose newest bar is more than STALE_BARS bars old and closed more
    than STALE_QUIET_S ago is refused here (the hub's own rule: quiet minutes of an illiquid pair are not old data)."""
    if not isinstance(resp, dict) or not resp.get("ok", True):
        return None
    current = resp.get("current") or {}
    if resp.get("stale") or current.get("stale"):
        note = str(resp.get("note") or resp.get("message") or "").strip()
        return note if note.lower().startswith("datos desactualizados") else "Datos desactualizados"
    t = safe_float(current.get("time"))
    if market != "crypto" or t is None:
        return None
    try:
        tf_s = tf_seconds(normalize_tf(tf))
    except ValueError:
        return None
    age = now - int(t)  # since the newest bar opened (< tf_s while it is the forming bar)
    if age // tf_s > STALE_BARS and age - tf_s > STALE_QUIET_S:
        return f"Datos desactualizados: última vela {_age_text(age)}"
    return None


def apply_deep(row: dict, resp: dict | None) -> dict | None:
    """Fill a ScanRow with the NEXUS + gate verdict. Returns the GateResult used (or None).

    `opportunity` = the gate says TOMAR *and* NEXUS signals that same entry now (BUY for long, SELL for short). A gate
    TOMAR without that signal is presented as "Filtro favorable · sin señal NEXUS" (its verdict stays the gate's)."""
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
    action = str(current.get("action") or "") or None
    take = gate.get("verdict") == "TAKE"
    signal = action == ("BUY" if direction == "long" else "SELL")
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
            "nexus_action": action,
            "opportunity": bool(take and signal),
        }
    )
    if take and not signal:
        label = ACTION_LABEL.get(action or "", action or "sin acción")
        reasons = [
            f"Filtro favorable · sin señal NEXUS ({gate_line(gate)}; NEXUS: {label})",
            "El filtro aprueba la idea, pero NEXUS no da una señal de entrada en esa dirección: no cuenta como "
            "oportunidad TOMAR",
        ]
        reasons += list(gate.get("reasons_for") or [])[:1]
    else:
        head = f"Filtro {gate_line(gate)}"
        summary = gate.get("summary")
        reasons = [f"{head}: {summary}" if summary else head]
        if not take:
            reasons += [f"En contra: {r}" for r in (gate.get("reasons_against") or [])[:2]]
        else:
            reasons += list(gate.get("reasons_for") or [])[:2]
    row["reasons"] = (reasons + list(row.get("reasons") or []))[:8]
    return gate


def is_opportunity(row: dict) -> bool:
    """A real 'oportunidad TOMAR' (gate TOMAR + NEXUS signal). Rows stored before this flag existed never count."""
    return bool(row.get("opportunity"))


def result_row(rec: dict) -> dict:
    """ScanRow from a scan_results row: its columns + the compact `data` JSON (older rows stored the whole row)."""
    data = json_loads(rec.get("data"), {}) or {}
    out: dict[str, Any] = {**RESULT_DEFAULTS, **(data if isinstance(data, dict) else {})}
    out.update({k: rec.get(k) for k in RESULT_COLUMNS})
    out.setdefault("description", out["symbol"])
    out["deep"] = bool(out.get("deep"))
    return out


def rank_rows(rows: list[dict]) -> None:
    """Oportunidades TOMAR first (by gate score), then the directional reads and last the neutral ones (ESPERAR: a
    move smaller than the neutral band is expected), each by |score| × confidence. A confident neutral call can have
    a large |score| (P↑ 70 % with P(move inside the band) 80 %), but it is not a candidate: it never heads the table
    or the run summary's "mejor"."""

    def key(r: dict) -> tuple:
        take = is_opportunity(r)
        neutral = not take and r.get("action") == "WAIT"
        return (0 if take else 1, -(r.get("gate_score") or 0.0) if take else 0.0, 1 if neutral else 0,
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
