"""TradingView scanner: pure helpers (no I/O) — columns, request bodies, response parsing and row mapping to the
canonical shapes of frontend/src/api/types.ts (SymbolInfo, Quote, UniverseRow, technicals, Candle).

Endpoint: POST https://scanner.tradingview.com/{market}/scan with a plain JSON body
    {"columns": [...], "range": [offset, offset+limit], "sort": {"sortBy": col, "sortOrder": "desc"},
     "filter": [{"left": "name", "operation": "match", "right": "btc"}], "filter2": {...},
     "symbols": {"tickers": [...]}}
Response: {"totalCount": N, "data": [{"s": "EXCH:SYM", "d": [values in column order]}]}; unknown columns -> null.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

from app.data.base import make_symbol_info
from app.data.symbols import (
    CRYPTO_EXCHANGES, DIRECT_CEX, is_dex, liquidity_score, market_for, normalize_text, route_symbol,
)
from app.util import floor_time, parse_symbol

SCAN_URL = "https://scanner.tradingview.com/{market}/scan"

INFO_COLUMNS: tuple[str, ...] = ("name", "description", "type", "subtype", "exchange", "currency", "pricescale",
                                 "logoid")
QUOTE_COLUMNS: tuple[str, ...] = ("close", "change", "change_abs", "volume", "high", "low", "open", "update_mode")
# 24h traded value in USD for crypto pairs / price x volume for stocks (used to rank and to sort by liquidity)
CRYPTO_VALUE_COL = "24h_vol|5"
STOCK_VALUE_COL = "Value.Traded"
LIQUIDITY_COLUMNS: tuple[str, ...] = (CRYPTO_VALUE_COL, STOCK_VALUE_COL)

# UniverseRow numeric field -> TradingView column
UNIVERSE_FIELDS: dict[str, str] = {
    "close": "close", "change_pct": "change", "volume": "volume", "rating": "Recommend.All", "rsi": "RSI",
    "adx": "ADX", "atr": "ATR", "perf_w": "Perf.W", "perf_1m": "Perf.1M", "volatility_d": "Volatility.D",
    "market_cap": "market_cap_basic",
}
# Columns of the scanner quick model (app.sim.scanner BASE_COLUMNS + MTF_COLUMNS): universe rows carry them so the
# hub can call rt.scanner.quick_score_rows(rows) directly.
QUICK_COLUMNS: tuple[str, ...] = (
    "name", "description", "type", "exchange", "close", "change", "volume",
    "Recommend.All", "Recommend.MA", "Recommend.Other", "RSI", "RSI[1]", "Stoch.K", "Stoch.D", "CCI20",
    "ADX", "ADX+DI", "ADX-DI", "AO", "Mom", "MACD.macd", "MACD.signal", "BB.upper", "BB.lower",
    "EMA20", "EMA50", "EMA200", "SMA50", "SMA200", "ATR", "Volatility.D", "Perf.W", "Perf.1M",
    "average_volume_10d_calc", "relative_volume_10d_calc",
    "RSI|15", "RSI|60", "RSI|240", "Recommend.All|15", "Recommend.All|60", "Recommend.All|240",
    "Recommend.All|1W", "MACD.macd|60", "MACD.signal|60", "MACD.macd|240", "MACD.signal|240",
    "EMA20|60", "EMA50|60", "EMA20|240", "EMA50|240", "ADX|60", "change|60", "change|240", "close|60", "close|240",
)
# SymbolDetail.technicals key -> TradingView column
TECHNICALS: dict[str, str] = {
    "rating": "Recommend.All", "rating_ma": "Recommend.MA", "rating_osc": "Recommend.Other",
    "rating_15m": "Recommend.All|15", "rating_1h": "Recommend.All|60", "rating_4h": "Recommend.All|240",
    "rating_1w": "Recommend.All|1W", "rsi": "RSI", "rsi_prev": "RSI[1]", "rsi_15m": "RSI|15", "rsi_1h": "RSI|60",
    "rsi_4h": "RSI|240", "stoch_k": "Stoch.K", "stoch_d": "Stoch.D", "cci": "CCI20", "adx": "ADX",
    "plus_di": "ADX+DI", "minus_di": "ADX-DI", "adx_1h": "ADX|60", "ao": "AO", "momentum": "Mom",
    "macd": "MACD.macd", "macd_signal": "MACD.signal", "macd_1h": "MACD.macd|60", "macd_signal_1h": "MACD.signal|60",
    "bb_upper": "BB.upper", "bb_lower": "BB.lower", "ema20": "EMA20", "ema50": "EMA50", "ema200": "EMA200",
    "sma20": "SMA20", "sma50": "SMA50", "sma200": "SMA200", "vwap": "VWAP", "atr": "ATR",
    "volatility_d": "Volatility.D", "volatility_w": "Volatility.W", "perf_w": "Perf.W", "perf_1m": "Perf.1M",
    "perf_3m": "Perf.3M", "perf_y": "Perf.Y", "market_cap": "market_cap_basic",
    "avg_volume_10d": "average_volume_10d_calc", "rel_volume_10d": "relative_volume_10d_calc",
}
# Current (forming) bar per timeframe: open|1 high|1 low|1 close|1 volume|1 ... ; no suffix = daily bar.
CURRENT_BAR_SUFFIX: dict[str, str] = {"1m": "|1", "5m": "|5", "15m": "|15", "1h": "|60", "4h": "|240", "1d": "",
                                      "1w": "|1W"}
CURRENT_BAR_TFS: tuple[str, ...] = tuple(CURRENT_BAR_SUFFIX)

# Centralized crypto venues (the crypto universe excludes DEX pools, whose fake volumes swamp the listing).
CEX_EXCHANGES: tuple[str, ...] = tuple(sorted(set(DIRECT_CEX) | set(CRYPTO_EXCHANGES)))
NO_VOLUME_MARKETS = frozenset({"forex", "cfd", "futures", "bond", "coin"})
SORT_ALIASES: dict[str, str] = {
    "price": "close", "close": "close", "change": "change", "change_pct": "change", "volume": "volume",
    "rating": "Recommend.All", "recommend.all": "Recommend.All", "rsi": "RSI", "adx": "ADX", "atr": "ATR",
    "perf_w": "Perf.W", "perf.w": "Perf.W", "perf_1m": "Perf.1M", "perf.1m": "Perf.1M",
    "volatility_d": "Volatility.D", "volatility.d": "Volatility.D", "market_cap": "market_cap_basic",
    "market_cap_basic": "market_cap_basic", "name": "name", "description": "description",
    "value": STOCK_VALUE_COL, "value.traded": STOCK_VALUE_COL, "24h_vol|5": CRYPTO_VALUE_COL,
    "relative_volume_10d_calc": "relative_volume_10d_calc", "perf.3m": "Perf.3M", "perf.y": "Perf.Y",
}
_COLUMN_RE = re.compile(r"^[A-Za-z0-9_.+\-\[\]|]{1,48}$")
_TYPE_RE = re.compile(r"^[a-z_]{1,20}$")


def num(value: Any) -> float | None:
    """Finite float or None (TradingView sends null for unknown columns / missing data)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def unique_columns(columns: Iterable[str]) -> list[str]:
    """De-duplicated, validated column list (order kept). Raises ValueError (Spanish) on a malformed name."""
    out: list[str] = []
    for col in columns:
        c = str(col or "").strip()
        if not c:
            continue
        if not _COLUMN_RE.match(c):
            raise ValueError(f"Columna de TradingView no válida: {col!r}")
        if c not in out:
            out.append(c)
    if not out:
        out.append("name")
    return out


def build_body(columns: list[str], *, filters: Any = None, filter2: dict | None = None, sort_by: str | None = None,
               order: str = "desc", offset: int = 0, limit: int = 300, tickers: list[str] | None = None) -> dict:
    """JSON body of one scan. `filters` = list of TradingView filter dicts (or one dict; a dict with "operator"
    is treated as a filter2 expression tree)."""
    body: dict[str, Any] = {"columns": list(columns)}
    plain: list[dict] = []
    if isinstance(filters, dict):
        filters = [filters]
    for f in filters or []:
        if isinstance(f, dict) and "operator" in f and filter2 is None:
            filter2 = f
        elif isinstance(f, dict) and "left" in f:
            plain.append(f)
    if plain:
        body["filter"] = plain
    if filter2:
        body["filter2"] = filter2
    if tickers:
        body["symbols"] = {"tickers": list(tickers)}
    else:
        body["range"] = [int(offset), int(offset) + int(limit)]
    if sort_by:
        body["sort"] = {"sortBy": sort_by, "sortOrder": "asc" if str(order).lower() == "asc" else "desc"}
    return body


def text_filter2(q: str) -> dict:
    """name ~ q OR description ~ q (TradingView 'match' is a case-insensitive word/substring match)."""
    return {"operator": "or", "operands": [
        {"expression": {"left": "name", "operation": "match", "right": q}},
        {"expression": {"left": "description", "operation": "match", "right": q}},
    ]}


def parse_response(data: Any, columns: list[str]) -> dict:
    """{"total": int, "rows": [{"symbol": "EXCH:SYM", <column>: value}]}. Raises ValueError with the server error."""
    if not isinstance(data, dict):
        raise ValueError("respuesta no válida")
    err = data.get("error")
    if err:
        raise ValueError(str(err)[:200])
    rows: list[dict] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("s") or "").strip().upper()
        values = item.get("d")
        if not sym or ":" not in sym or not isinstance(values, list):
            continue
        row: dict[str, Any] = {"symbol": sym}
        for i, col in enumerate(columns):
            v = values[i] if i < len(values) else None
            row[col] = None if isinstance(v, float) and not math.isfinite(v) else v
        rows.append(row)
    try:
        total = int(data.get("totalCount") or 0)
    except (TypeError, ValueError):
        total = len(rows)
    return {"total": max(total, len(rows)), "rows": rows}


# ----------------------------------------------------------------------------- row mapping


def delay_seconds(update_mode: Any) -> int | None:
    """'streaming' -> 0, 'delayed_streaming_900' -> 900 (data delay announced by TradingView), else None."""
    mode = str(update_mode or "").strip().lower()
    if not mode:
        return None
    if mode == "streaming":
        return 0
    m = re.search(r"(\d+)$", mode)
    return int(m.group(1)) if m and "delayed" in mode else None


def row_info(row: dict, market: str | None = None) -> dict:
    """SymbolInfo from a scan row. provider/history come from routing alone (crypto -> exchange API, 'full';
    anything else -> 'recorder'); the hub overrides them when a keyed provider applies."""
    symbol = str(row.get("symbol") or "").upper()
    exchange, ticker = parse_symbol(symbol)
    tv_type = str(row.get("type") or "")
    mkt = market if market and market != "global" else market_for(exchange, ticker, tv_type)
    meta = {"type": tv_type, "currency": row.get("currency"), "description": row.get("description"), "market": mkt}
    route = route_symbol(symbol, meta)
    if route.kind == "crypto" and route.venues:
        provider, history = route.venues[0].provider, "full"
    else:
        provider, history = "recorder", "recorder"
    scale = num(row.get("pricescale"))
    logoid = str(row.get("logoid") or "").strip() or None
    return make_symbol_info(
        symbol,
        ticker=ticker,
        exchange=exchange,
        description=str(row.get("description") or row.get("name") or ticker),
        type=tv_type,
        market=mkt,
        currency=str(row.get("currency") or ""),
        pricescale=int(scale) if scale is not None and scale > 0 else None,
        logoid=logoid,
        provider=provider,
        history=history,
    )


def row_quote(row: dict, ts_ms: int) -> dict:
    """Quote (types.ts) from a scan row with QUOTE_COLUMNS."""
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "price": num(row.get("close")),
        "change_pct": num(row.get("change")),
        "change": num(row.get("change_abs")),
        "volume": num(row.get("volume")),
        "high": num(row.get("high")),
        "low": num(row.get("low")),
        "open": num(row.get("open")),
        "ts": int(ts_ms),
    }


def row_technicals(row: dict) -> dict[str, float | None]:
    tech: dict[str, float | None] = {key: num(row.get(col)) for key, col in TECHNICALS.items()}
    delay = delay_seconds(row.get("update_mode"))
    tech["delay_s"] = float(delay) if delay is not None else None
    return tech


def row_universe(row: dict, market: str) -> dict:
    """UniverseRow without quick_score/quick_prob, plus the raw scanner columns (QUICK_COLUMNS) so the scanner's
    quick model can score the row; strip them with public_universe_row() before sending to the browser."""
    out = row_info(row, market)
    for key, col in UNIVERSE_FIELDS.items():
        out[key] = num(row.get(col))
    for col in (*QUICK_COLUMNS, *LIQUIDITY_COLUMNS):
        if col not in out and col in row:
            out[col] = row[col]
    return out


UNIVERSE_ROW_KEYS: tuple[str, ...] = (
    "symbol", "ticker", "exchange", "description", "type", "market", "currency", "pricescale", "logoid", "provider",
    "history", *UNIVERSE_FIELDS, "quick_score", "quick_prob",
)


def public_universe_row(row: dict) -> dict:
    """Exactly the UniverseRow keys of types.ts (quick_score / quick_prob default to None)."""
    return {k: row.get(k) for k in UNIVERSE_ROW_KEYS}


def row_current_bars(row: dict, tfs: Iterable[str], now: int) -> dict[str, dict]:
    """{tf: Candle} for the bars forming at `now` (time = floor_time(now, tf)). The close of every timeframe is the
    freshest `close` (high/low widened to contain it). Timeframes without data are omitted."""
    latest = num(row.get("close"))
    out: dict[str, dict] = {}
    for tf in tfs:
        sfx = CURRENT_BAR_SUFFIX.get(tf)
        if sfx is None:
            continue
        o, h, lo, c = (num(row.get(f"{k}{sfx}")) for k in ("open", "high", "low", "close"))
        if o is None or h is None or lo is None or c is None:
            continue
        if latest is not None:
            c = latest
        v = num(row.get(f"volume{sfx}"))
        out[tf] = {
            "time": floor_time(now, tf),
            "open": o,
            "high": max(h, o, c),
            "low": min(lo, o, c),
            "close": c,
            "volume": v if v is not None and v >= 0 else 0.0,
        }
    return out


def current_bar_columns(tfs: Iterable[str]) -> list[str]:
    cols = ["close", "update_mode"]
    for tf in tfs:
        sfx = CURRENT_BAR_SUFFIX.get(tf)
        if sfx is not None:
            cols += [f"{k}{sfx}" for k in ("open", "high", "low", "close", "volume")]
    return unique_columns(cols)


# ----------------------------------------------------------------------------- search & universe helpers

SEARCH_COLUMNS: tuple[str, ...] = (*INFO_COLUMNS, "close", "change", "volume", *LIQUIDITY_COLUMNS)
_SEARCH_QUOTES = ("USDT", "USD", "USDC", "EUR", "FDUSD", "BTC")


def search_variants(query: str) -> list[str]:
    """Exact TradingView names worth looking up for a query: 'btc' -> BTC, BTCUSDT, BTCUSD, ... ([] for
    multi-word queries, which only make sense as description searches)."""
    q = str(query or "").strip().upper()
    if ":" in q:
        q = q.split(":", 1)[1].strip()
    if not q or any(ch.isspace() for ch in q):
        return []
    alnum = re.sub(r"[^A-Z0-9]", "", q)
    out = [v for v in dict.fromkeys((q, alnum))
           if v and len(v) <= 24 and re.match(r"^[A-Z0-9][A-Z0-9_.!&\-]*$", v)]
    if q.isalnum() and 2 <= len(q) <= 8:
        out += [q + quote for quote in _SEARCH_QUOTES if not q.endswith(quote)]
    return out[:12]


def search_item(row: dict, market: str | None) -> dict:
    """SearchResult-like dict (+ private _liquidity used by symbols.rank_results)."""
    item = row_info(row, market)
    item["price"] = num(row.get("close"))
    item["change_pct"] = num(row.get("change"))
    item["_liquidity"] = liquidity_score(row)
    return item


def drop_private(item: dict) -> dict:
    return {k: v for k, v in item.items() if not str(k).startswith("_")}


def not_dex(row: dict) -> bool:
    return not is_dex(str(row.get("symbol") or ""), str(row.get("description") or ""))


# No single instrument trades more than ~100 B USD a day; TradingView reports 2e14 for some inverse perpetuals
# (COINBASE:BTCUSD.P, DERIBIT:BTCUSD.P), which would otherwise top every liquidity-sorted listing.
ABSURD_VALUE = 1e11


def absurd_liquidity(row: dict, sort_col: str | None) -> bool:
    """True when the listing is sorted by a liquidity column and this row's value is impossible."""
    if sort_col not in LIQUIDITY_COLUMNS:
        return False
    v = num(row.get(sort_col))
    return v is not None and v > ABSURD_VALUE


def universe_sorts(market: str, sort: str | None) -> list[str]:
    """Sort columns to try in order. 'volume' means liquidity: USD traded value for crypto (base-unit volumes are
    not comparable across coins and DEX pools report absurd numbers), price x volume for stock markets."""
    raw = str(sort or "volume").strip()
    key = SORT_ALIASES.get(raw.lower()) or (raw if _COLUMN_RE.match(raw) else "volume")
    if key != "volume":
        return [key, "volume"]  # volume = fallback when TradingView rejects the column
    if market == "crypto":
        return [CRYPTO_VALUE_COL, "volume"]
    if market in NO_VOLUME_MARKETS:
        return ["volume"]
    return [STOCK_VALUE_COL, "volume"]


def universe_filters(market: str, type_: str | None) -> list[dict]:
    filters: list[dict] = []
    if market == "crypto":
        filters.append({"left": "exchange", "operation": "in_range", "right": list(CEX_EXCHANGES)})
    kind = str(type_ or "").strip().lower()
    if kind and _TYPE_RE.match(kind):
        filters.append({"left": "type", "operation": "equal", "right": kind})
    return filters


def universe_columns() -> list[str]:
    return unique_columns([*INFO_COLUMNS, *QUOTE_COLUMNS, *UNIVERSE_FIELDS.values(), *QUICK_COLUMNS,
                           *LIQUIDITY_COLUMNS])


def details_columns() -> list[str]:
    return unique_columns([*INFO_COLUMNS, *QUOTE_COLUMNS, *TECHNICALS.values(), *LIQUIDITY_COLUMNS])


def norm_query(query: Any) -> str:
    return normalize_text(query)[:64]
