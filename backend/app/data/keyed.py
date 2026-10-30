"""Keyed market-data providers (used only when the user saved an API key in Ajustes → providers.*_key).

    KEYED_PROVIDERS = ("twelvedata", "alphavantage", "polygon")
    await fetch_keyed_candles(provider, symbol, asset_class, tf, limit, *, api_key, client) -> list[Candle dict]
    keyed_supports(provider, symbol, asset_class) -> bool

- Twelve Data `time_series` (outputsize <= 5000, timezone=UTC, key in the Authorization header) — best coverage.
- Alpha Vantage TIME_SERIES_* / FX_* (US/Eastern intraday timestamps converted to UTC; free plan 25 calls/day).
- Polygon v2 aggregates (key as Bearer header, newest first, `next_url` pagination).
Symbols are mapped with app.data.symbols.{twelvedata_params, alphavantage_spec, polygon_ticker}. Missing native
timeframes are rebuilt by resampling (weeks always start on Monday). Intraday stock bars keep the session alignment
of the provider (e.g. 13:30 UTC for US stocks 1h). Errors raise DataProviderError (Spanish: clave inválida, límite
alcanzado, plan de pago, símbolo no disponible...). API keys never appear in error messages.
"""
from __future__ import annotations

import datetime as dt
import functools
from typing import Any, Awaitable, Callable

import httpx

from app.data.providers import resample_rows
from app.data.providers_cex import candle
from app.data.providers_http import DataProviderError, HttpResult, ensure_online, request_json, unexpected
from app.data.symbols import alphavantage_spec, asset_class_of, polygon_ticker, twelvedata_params
from app.util import TF_SECONDS, floor_time, normalize_tf, now_s, parse_symbol

KEYED_PROVIDERS: tuple[str, ...] = ("twelvedata", "alphavantage", "polygon")
LABELS: dict[str, str] = {"twelvedata": "Twelve Data", "alphavantage": "Alpha Vantage", "polygon": "Polygon"}
MAX_LIMIT = 5000

# ----------------------------------------------------------------------------- time helpers


@functools.lru_cache(maxsize=16)
def _zone(name: str) -> dt.tzinfo | None:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def _us_eastern_offset(naive: dt.datetime) -> int:
    """UTC offset (s) of US/Eastern for a local wall time (fallback when the tz database is missing)."""
    march = dt.datetime(naive.year, 3, 8)
    dst_start = march + dt.timedelta(days=(6 - march.weekday()) % 7, hours=2)  # 2nd Sunday of March
    nov = dt.datetime(naive.year, 11, 1)
    dst_end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7, hours=2)  # 1st Sunday of November
    return -4 * 3600 if dst_start <= naive < dst_end else -5 * 3600


def _parse_naive(text: Any) -> dt.datetime | None:
    s = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_time(text: Any, tz_name: str | None = None) -> int | None:
    """'2026-10-30 16:30:00' in `tz_name` (None/UTC = already UTC) -> unix seconds."""
    naive = _parse_naive(text)
    if naive is None:
        return None
    name = (tz_name or "UTC").strip()
    if name.upper() in ("UTC", "GMT", "Z", ""):
        return int(naive.replace(tzinfo=dt.timezone.utc).timestamp())
    tz = _zone(name) or (_zone("America/New_York") if "eastern" in name.lower() or "new_york" in name.lower() else None)
    if tz is not None:
        return int(naive.replace(tzinfo=tz).timestamp())
    if "eastern" in name.lower() or "new_york" in name.lower():
        return int(naive.replace(tzinfo=dt.timezone.utc).timestamp()) - _us_eastern_offset(naive)
    return int(naive.replace(tzinfo=dt.timezone.utc).timestamp())


def _fmt_utc(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _plan(natives: tuple[str, ...], tf: str, provider: str) -> tuple[str, int]:
    if tf in natives:
        return tf, 1
    sec = TF_SECONDS[tf]
    best = None
    for base in natives:
        bsec = TF_SECONDS[base]
        if bsec < sec and sec % bsec == 0 and (best is None or bsec > TF_SECONDS[best]):
            best = base
    if best is None:
        raise DataProviderError(f"{LABELS[provider]}: temporalidad {tf} no disponible", provider=provider,
                                kind="unsupported")
    return best, sec // TF_SECONDS[best]


def _merge(rows: dict[int, dict], batch: list[dict]) -> int:
    added = 0
    for c in batch:
        if c["time"] not in rows:
            rows[c["time"]] = c
            added += 1
    return added


def _err(provider: str, message: str, kind: str) -> DataProviderError:
    return DataProviderError(f"{LABELS[provider]}: {message}", provider=provider, kind=kind)


# ----------------------------------------------------------------------------- Twelve Data

TD_URL = "https://api.twelvedata.com/time_series"
TD_INTERVALS: dict[str, str] = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1day",
}
TD_PAGE = 5000


def parse_twelvedata(result: HttpResult, symbol: str) -> list[dict]:
    """time_series payload -> ascending candles (datetimes requested in UTC)."""
    data = result.data
    if not isinstance(data, dict):
        raise unexpected(LABELS["twelvedata"], "twelvedata", result)
    if str(data.get("status", "ok")).lower() == "error" or result.status >= 400:
        try:
            code = int(data.get("code") or result.status)
        except (TypeError, ValueError):
            code = result.status
        msg = " ".join(str(data.get("message") or "").split())
        low = msg.lower()
        if code == 401 or "apikey" in low or "api key" in low:
            raise _err("twelvedata", "clave API inválida o no autorizada", "auth")
        if code == 429 or "credits" in low or "limit" in low:
            raise _err("twelvedata", "límite de créditos alcanzado (plan gratuito: 8 por minuto, 800 por día)",
                       "rate_limit")
        if code == 403 or "upgrade" in low or "plan" in low or "grow" in low or "pro " in low:
            raise _err("twelvedata", f"{symbol} requiere un plan de pago", "premium")
        if code in (400, 404) and ("symbol" in low or "not found" in low or "no data" in low or "invalid" in low):
            raise _err("twelvedata", f"símbolo no disponible o sin datos ({symbol})", "not_found")
        raise _err("twelvedata", f"error {code}: {msg[:160] or 'desconocido'}", "error")
    out = []
    for v in data.get("values") or []:
        if isinstance(v, dict):
            c = candle(parse_time(v.get("datetime")), v.get("open"), v.get("high"), v.get("low"), v.get("close"),
                       v.get("volume"))
            if c is not None:
                out.append(c)
    out.sort(key=lambda c: c["time"])
    return out


async def _fetch_twelvedata(symbol: str, asset_class: str, base_tf: str, need: int, api_key: str,
                            client: httpx.AsyncClient) -> list[dict]:
    ident = twelvedata_params(symbol, asset_class)
    if ident is None:
        raise _err("twelvedata", f"no cubre {symbol}", "unsupported")
    rows: dict[int, dict] = {}
    end_date: str | None = None
    for page in range(3):
        n = int(min(TD_PAGE, need - len(rows)))
        if n <= 0:
            break
        params: dict[str, Any] = {**ident, "interval": TD_INTERVALS[base_tf], "outputsize": n, "timezone": "UTC"}
        if end_date:
            params["end_date"] = end_date
        result = await request_json(client, "GET", TD_URL, provider="twelvedata", label=LABELS["twelvedata"],
                                    params=params, headers={"Authorization": f"apikey {api_key}"})
        try:
            batch = parse_twelvedata(result, symbol)
        except DataProviderError as exc:
            if page > 0 and exc.kind == "not_found":
                break  # no older data
            raise
        if not _merge(rows, batch) or len(batch) < n:
            break
        end_date = _fmt_utc(min(c["time"] for c in batch) - 1)
    return [rows[t] for t in sorted(rows)][-need:]


# ----------------------------------------------------------------------------- Alpha Vantage

AV_URL = "https://www.alphavantage.co/query"
AV_INTRADAY: dict[str, str] = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "60min"}
AV_NATIVE: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "1d", "1w")


def alphavantage_params(spec: dict, base_tf: str, outputsize: str) -> dict[str, Any]:
    fx = spec.get("kind") == "fx"
    ident = {"from_symbol": spec["from"], "to_symbol": spec["to"]} if fx else {"symbol": spec["symbol"]}
    if base_tf in AV_INTRADAY:
        fn = "FX_INTRADAY" if fx else "TIME_SERIES_INTRADAY"
        return {"function": fn, **ident, "interval": AV_INTRADAY[base_tf], "outputsize": outputsize}
    if base_tf == "1d":
        return {"function": "FX_DAILY" if fx else "TIME_SERIES_DAILY", **ident, "outputsize": outputsize}
    return {"function": "FX_WEEKLY" if fx else "TIME_SERIES_WEEKLY", **ident}


def parse_alphavantage(result: HttpResult, symbol: str, base_tf: str) -> list[dict]:
    data = result.data
    if not isinstance(data, dict):
        raise unexpected(LABELS["alphavantage"], "alphavantage", result)
    series_key = next((k for k in data if "Time Series" in k), None)
    if series_key is None:
        msg = " ".join(str(data.get("Error Message") or data.get("Information") or data.get("Note") or "").split())
        low = msg.lower()
        if "apikey" in low and ("invalid" in low or "missing" in low):
            raise _err("alphavantage", "clave API inválida", "auth")
        if "premium" in low:
            raise _err("alphavantage", "estos datos requieren un plan de pago", "premium")
        if "rate limit" in low or "call frequency" in low or "requests per day" in low or "per minute" in low:
            raise _err("alphavantage", "límite de peticiones alcanzado (plan gratuito: 25 al día)", "rate_limit")
        if data.get("Error Message"):
            raise _err("alphavantage", f"símbolo no válido o sin datos ({symbol})", "not_found")
        raise unexpected(LABELS["alphavantage"], "alphavantage", result)
    meta = data.get("Meta Data") if isinstance(data.get("Meta Data"), dict) else {}
    tz_name = next((str(v) for k, v in meta.items() if k.lower().endswith("time zone")), "UTC")
    series = data.get(series_key) if isinstance(data.get(series_key), dict) else {}
    out = []
    for stamp, v in series.items():
        if not isinstance(v, dict):
            continue
        if base_tf in AV_INTRADAY:
            t = parse_time(stamp, tz_name)
        else:  # daily / weekly bars are dated by session (weekly: last session of the week -> Monday)
            t = parse_time(stamp, "UTC")
            if t is not None and base_tf == "1w":
                t = floor_time(t, "1w")
        c = candle(t, v.get("1. open"), v.get("2. high"), v.get("3. low"), v.get("4. close"), v.get("5. volume"))
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: c["time"])
    return out


async def _fetch_alphavantage(symbol: str, asset_class: str, base_tf: str, need: int, api_key: str,
                              client: httpx.AsyncClient) -> list[dict]:
    spec = alphavantage_spec(symbol, asset_class)
    if spec is None:
        raise _err("alphavantage", f"no cubre {symbol}", "unsupported")
    outputsize = "compact" if need <= 100 else "full"
    while True:
        params = {**alphavantage_params(spec, base_tf, outputsize), "apikey": api_key}
        result = await request_json(client, "GET", AV_URL, provider="alphavantage", label=LABELS["alphavantage"],
                                    params=params)
        try:
            rows = parse_alphavantage(result, symbol, base_tf)
        except DataProviderError as exc:
            if exc.kind == "premium" and outputsize == "full" and base_tf not in ("1w",):
                outputsize = "compact"  # the free plan only serves the latest 100 points for some series
                continue
            raise
        return rows[-need:]


# ----------------------------------------------------------------------------- Polygon

PG_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{mult}/{span}/{start}/{end}"
PG_INTERVALS: dict[str, tuple[int, str]] = {
    "1m": (1, "minute"), "3m": (3, "minute"), "5m": (5, "minute"), "15m": (15, "minute"), "30m": (30, "minute"),
    "1h": (1, "hour"), "1d": (1, "day"),
}
PG_MAX_SPAN_S = 20 * 365 * 86400


def parse_polygon(result: HttpResult, ticker: str) -> tuple[list[dict], str | None]:
    """(ascending candles, next_url) from an aggregates payload."""
    data = result.data if isinstance(result.data, dict) else None
    status = str((data or {}).get("status") or "").upper()
    msg = " ".join(str((data or {}).get("error") or (data or {}).get("message") or "").split())
    if result.status == 401 or "API KEY" in msg.upper():
        raise _err("polygon", "clave API inválida", "auth")
    if result.status == 403 or status == "NOT_AUTHORIZED":
        raise _err("polygon", f"tu plan no incluye estos datos ({ticker})", "premium")
    if result.status == 404:
        raise _err("polygon", f"símbolo no disponible ({ticker})", "not_found")
    if data is None or result.status >= 400 or status == "ERROR":
        if "exceeded" in msg.lower() or "maximum requests" in msg.lower():
            raise _err("polygon", "límite de peticiones alcanzado (plan gratuito: 5 por minuto)", "rate_limit")
        if data is None or result.status >= 400:
            raise unexpected(LABELS["polygon"], "polygon", result)
        raise _err("polygon", f"error: {msg[:160] or status}", "error")
    out = []
    for r in data.get("results") or []:
        if isinstance(r, dict):
            c = candle(r.get("t"), r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v"), ms=True)
            if c is not None:
                out.append(c)
    out.sort(key=lambda c: c["time"])
    nxt = data.get("next_url")
    return out, (str(nxt) if isinstance(nxt, str) and nxt.startswith("https://") else None)


def _calendar_factor(asset_class: str) -> float:
    """Calendar seconds per bar-second: markets that close at night / on weekends need wider windows."""
    if asset_class in ("forex", "commodity", "cfd"):
        return 1.6
    if asset_class == "crypto":
        return 1.1
    return 4.5


async def _fetch_polygon(symbol: str, asset_class: str, base_tf: str, need: int, api_key: str,
                         client: httpx.AsyncClient) -> list[dict]:
    ticker = polygon_ticker(symbol, asset_class)
    if ticker is None:
        raise _err("polygon", f"no cubre {symbol}", "unsupported")
    mult, span = PG_INTERVALS[base_tf]
    headers = {"Authorization": f"Bearer {api_key}"}
    window = int(min(PG_MAX_SPAN_S, need * TF_SECONDS[base_tf] * _calendar_factor(asset_class) + 4 * 86400))
    end = (now_s() + 86400) * 1000
    rows: dict[int, dict] = {}
    url: str | None = PG_URL.format(ticker=ticker, mult=mult, span=span, start=end - window * 1000, end=end)
    params: dict | None = {"adjusted": "true", "sort": "desc", "limit": int(min(50000, max(need, 10)))}
    for _ in range(4):
        if url is None:
            break
        result = await request_json(client, "GET", url, provider="polygon", label=LABELS["polygon"],
                                    params=params, headers=headers)
        batch, nxt = parse_polygon(result, ticker)
        added = _merge(rows, batch)
        if len(rows) >= need or not batch or not added:
            break
        if nxt:
            url, params = nxt, None
            continue
        # window exhausted: look further back once more (older history than the first estimate)
        oldest = min(rows) * 1000
        url = PG_URL.format(ticker=ticker, mult=mult, span=span, start=oldest - window * 1000, end=oldest - 1)
        params = {"adjusted": "true", "sort": "desc", "limit": int(min(50000, max(need - len(rows), 10)))}
    return [rows[t] for t in sorted(rows)][-need:]


# ----------------------------------------------------------------------------- dispatcher

Fetcher = Callable[[str, str, str, int, str, httpx.AsyncClient], Awaitable[list[dict]]]
_FETCHERS: dict[str, Fetcher] = {
    "twelvedata": _fetch_twelvedata,
    "alphavantage": _fetch_alphavantage,
    "polygon": _fetch_polygon,
}
_NATIVE: dict[str, tuple[str, ...]] = {
    "twelvedata": tuple(TD_INTERVALS),
    "alphavantage": AV_NATIVE,
    "polygon": tuple(PG_INTERVALS),
}
_MAPPERS: dict[str, Callable[[str, str], Any]] = {
    "twelvedata": twelvedata_params,
    "alphavantage": alphavantage_spec,
    "polygon": polygon_ticker,
}


def _asset_class(symbol: str, asset_class: str | None) -> str:
    if asset_class:
        return str(asset_class).lower()
    exchange, ticker = parse_symbol(symbol)
    return asset_class_of(exchange, ticker)


def keyed_supports(provider: str, symbol: str, asset_class: str | None = None) -> bool:
    """True when `provider` can map `symbol` (the user still needs a key for it)."""
    mapper = _MAPPERS.get(str(provider or "").lower())
    return bool(mapper and mapper(str(symbol).upper(), _asset_class(symbol, asset_class)) is not None)


async def fetch_keyed_candles(provider: str, symbol: str, asset_class: str, tf: str, limit: int, *, api_key: str,
                              client: httpx.AsyncClient) -> list[dict]:
    """Up to `limit` most recent bars of `symbol` (TradingView 'EXCHANGE:TICKER') from a keyed provider."""
    prov = str(provider or "").lower()
    if prov not in _FETCHERS:
        raise DataProviderError(f"Proveedor con clave desconocido: {provider!r}", provider=prov, kind="unsupported")
    ensure_online(prov)
    key = str(api_key or "").strip()
    if not key:
        raise _err(prov, "falta la clave API (configúrala en Ajustes)", "auth")
    try:
        tf_n = normalize_tf(tf)
    except ValueError as exc:
        raise DataProviderError(str(exc), provider=prov, kind="invalid") from None
    try:
        n = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        n = 500
    sym = str(symbol or "").strip().upper()
    aclass = _asset_class(sym, asset_class)
    base_tf, factor = _plan(_NATIVE[prov], tf_n, prov)
    need = n if factor == 1 else n * factor + factor
    rows = await _FETCHERS[prov](sym, aclass, base_tf, need, key, client)
    if factor > 1:
        rows = resample_rows(rows, base_tf, tf_n)
    return rows[-n:]
