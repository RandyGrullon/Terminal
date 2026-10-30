"""Official public candle endpoints of the centralized exchanges (pure request building + payload parsing).

Each spec knows its native intervals, the page size, how to page backwards and how to turn the exchange payload
into ascending Candle dicts {time (bar open, unix s), open, high, low, close, volume (base asset)}.
Verified from this network (see tests for the embedded real samples):
- BINANCE  data-api.binance.vision/api/v3/klines (api.binance.com answers 451 here) · 1000/request · Monday weeks
- OKX      /api/v5/market/candles (300, latest bars) + /history-candles (100) · `after` = older than ts (ms)
- COINBASE /products/{BASE-QUOTE}/candles · granularity 60,300,900,3600,21600,86400 only · 300/request
- KRAKEN   /0/public/OHLC · only the latest 720 bars · weekly bars start on Thursday (we resample 1d -> 1w)
- KUCOIN   /api/v1/market/candles · 1500/request · rows [t, open, CLOSE, HIGH, LOW, vol, turnover] · Thursday weeks
"""
from __future__ import annotations

import math
from typing import Any

from app.data.providers_http import DataProviderError, HttpResult, unexpected
from app.data.symbols import native_symbol, normalize_base, split_pair
from app.util import TIMEFRAMES, now_s


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def candle(t: Any, o: Any, h: Any, lo: Any, c: Any, v: Any, *, ms: bool = False) -> dict | None:
    """Validated Candle dict (None when a price is missing / not finite)."""
    tv = _num(t)
    vals = [_num(o), _num(h), _num(lo), _num(c)]
    if tv is None or any(x is None for x in vals):
        return None
    ts = int(tv // 1000) if ms else int(tv)
    op, hi, low, cl = vals  # type: ignore[misc]
    vol = _num(v)
    return {
        "time": ts,
        "open": op,
        "high": max(hi, op, cl),
        "low": min(low, op, cl),
        "close": cl,
        "volume": vol if vol is not None and vol >= 0 else 0.0,
    }


class ExchangeSpec:
    provider = ""
    label = ""
    intervals: dict[str, Any] = {}
    page_size = 500
    max_bars: int | None = None  # deepest history the API can serve (Kraken: 720)
    single_page = False  # the API cannot page backwards
    stop_on_short_page = True  # a page shorter than requested means "no older data"
    separator = ""  # native instrument separator: "" (BTCUSDT) or "-" (BTC-USDT)

    def to_native(self, text: str) -> str:
        """Accepts 'BTCUSDT', 'BTC-USDT', 'BTC/USDT' or 'EXCHANGE:BTCUSDT' and returns the API instrument id."""
        s = str(text or "").strip().upper()
        if ":" in s:
            s = s.split(":", 1)[1]
        if not s:
            raise DataProviderError(f"{self.label}: falta el instrumento", provider=self.provider, kind="invalid")
        for sep in ("-", "/", "_"):
            if sep in s:
                base, quote = s.split(sep, 1)
                return native_symbol(self.provider, normalize_base(base), normalize_base(quote))
        pair = split_pair(s)
        if pair is None:
            return s
        return native_symbol(self.provider, pair[0], pair[1])

    def request(self, native: str, code: Any, tf_sec: int, count: int, cursor: int | None, page: int
                ) -> tuple[str, dict, int]:
        """(url, params, requested_count) for one page of bars with open time <= cursor (None = latest)."""
        raise NotImplementedError

    def parse(self, result: HttpResult, native: str) -> list[dict]:
        raise NotImplementedError

    def not_found(self, native: str) -> DataProviderError:
        return DataProviderError(f"{self.label}: par no encontrado ({native})", provider=self.provider,
                                 kind="not_found")

    def api_error(self, message: str, kind: str = "error") -> DataProviderError:
        return DataProviderError(f"{self.label}: {message}", provider=self.provider, kind=kind)


def _rows(raw: Any, build) -> list[dict]:
    out = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, (list, tuple)):
            c = build(r)
            if c is not None:
                out.append(c)
    out.sort(key=lambda c: c["time"])
    return out


# ----------------------------------------------------------------------------- BINANCE


class BinanceSpec(ExchangeSpec):
    provider = "binance"
    label = "BINANCE"
    URL = "https://data-api.binance.vision/api/v3/klines"
    intervals = {tf: tf for tf in TIMEFRAMES}
    page_size = 1000

    def request(self, native, code, tf_sec, count, cursor, page):
        n = int(min(count, self.page_size))
        params: dict[str, Any] = {"symbol": native, "interval": code, "limit": n}
        if cursor is not None:
            params["endTime"] = int(cursor) * 1000
        return self.URL, params, n

    def parse(self, result, native):
        if result.status == 200 and isinstance(result.data, list):
            return _rows(result.data, lambda r: candle(r[0], r[1], r[2], r[3], r[4], r[5], ms=True)
                         if len(r) >= 6 else None)
        data = result.data if isinstance(result.data, dict) else {}
        if result.status == 400 and data.get("code") in (-1121, -1100, -1120):
            if data.get("code") == -1121:
                raise self.not_found(native)
            raise self.api_error(f"parámetro no válido ({data.get('msg') or result.status})", "invalid")
        if result.status == 451:
            raise self.api_error("servicio no disponible desde esta región (HTTP 451)", "unsupported")
        raise unexpected(self.label, self.provider, result)


# ----------------------------------------------------------------------------- OKX


class OkxSpec(ExchangeSpec):
    provider = "okx"
    label = "OKX"
    URL_RECENT = "https://www.okx.com/api/v5/market/candles"
    URL_HISTORY = "https://www.okx.com/api/v5/market/history-candles"
    intervals = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "2h": "2H", "4h": "4H",
        "6h": "6Hutc", "12h": "12Hutc", "1d": "1Dutc", "1w": "1Wutc",
    }
    page_size = 300
    HISTORY_PAGE = 100
    separator = "-"

    def request(self, native, code, tf_sec, count, cursor, page):
        recent = page == 0 and cursor is None
        n = int(min(count, self.page_size if recent else self.HISTORY_PAGE))
        params: dict[str, Any] = {"instId": native, "bar": code, "limit": n}
        if cursor is not None:
            params["after"] = str((int(cursor) + 1) * 1000)  # strictly older than `after`
        return (self.URL_RECENT if recent else self.URL_HISTORY), params, n

    def parse(self, result, native):
        data = result.data if isinstance(result.data, dict) else None
        if data is None:
            raise unexpected(self.label, self.provider, result)
        code = str(data.get("code", ""))
        if code == "0":
            return _rows(data.get("data"), lambda r: candle(r[0], r[1], r[2], r[3], r[4], r[5], ms=True)
                         if len(r) >= 6 else None)
        msg = str(data.get("msg") or code)
        if code in ("51001", "51000"):
            raise self.not_found(native)
        if code in ("50011", "50061"):
            raise self.api_error("límite de peticiones alcanzado; inténtalo en unos segundos", "rate_limit")
        raise self.api_error(f"error {code}: {msg}")


# ----------------------------------------------------------------------------- COINBASE


class CoinbaseSpec(ExchangeSpec):
    provider = "coinbase"
    label = "COINBASE"
    URL = "https://api.exchange.coinbase.com/products/{}/candles"
    intervals = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
    page_size = 300
    stop_on_short_page = False  # windows without trades come back short
    separator = "-"

    def request(self, native, code, tf_sec, count, cursor, page):
        g = int(code)
        n = int(min(count, self.page_size))
        end = int(cursor) if cursor is not None else now_s()
        end -= end % g
        start = end - (n - 1) * g
        return self.URL.format(native), {"granularity": g, "start": str(start), "end": str(end)}, n

    def parse(self, result, native):
        if result.status == 200 and isinstance(result.data, list):
            # [time, low, high, open, close, volume], newest first
            return _rows(result.data, lambda r: candle(r[0], r[3], r[2], r[1], r[4], r[5]) if len(r) >= 6 else None)
        data = result.data if isinstance(result.data, dict) else {}
        if result.status == 404:
            raise self.not_found(native)
        if result.status == 400:
            raise self.api_error(f"petición no válida ({data.get('message') or 'HTTP 400'})", "invalid")
        raise unexpected(self.label, self.provider, result)


# ----------------------------------------------------------------------------- KRAKEN


class KrakenSpec(ExchangeSpec):
    provider = "kraken"
    label = "KRAKEN"
    URL = "https://api.kraken.com/0/public/OHLC"
    # no "1w": Kraken weeks start on Thursday, so weekly bars are rebuilt from daily ones (Monday weeks)
    intervals = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    page_size = 720
    max_bars = 720
    single_page = True

    def request(self, native, code, tf_sec, count, cursor, page):
        n = int(min(count, self.page_size))
        params: dict[str, Any] = {"pair": native, "interval": int(code)}
        if cursor is None and n < self.page_size:
            params["since"] = now_s() - (n + 1) * int(tf_sec)  # smaller payload for live polling
        return self.URL, params, n

    def parse(self, result, native):
        data = result.data if isinstance(result.data, dict) else None
        if data is None:
            raise unexpected(self.label, self.provider, result)
        errors = [str(e) for e in (data.get("error") or [])]
        if errors:
            text = "; ".join(errors)
            if any("Unknown asset pair" in e or "Invalid arguments" in e for e in errors):
                raise self.not_found(native)
            if any("Too many requests" in e or "Rate limit" in e for e in errors):
                raise self.api_error("límite de peticiones alcanzado; inténtalo en unos segundos", "rate_limit")
            if any(e.startswith("EService") for e in errors):
                raise self.api_error(f"servicio no disponible ({text})", "server")
            raise self.api_error(f"error: {text}")
        result_obj = data.get("result") if isinstance(data.get("result"), dict) else {}
        series = next((v for k, v in result_obj.items() if k != "last" and isinstance(v, list)), None)
        if series is None:
            raise self.not_found(native)
        # [time, open, high, low, close, vwap, volume, count]
        return _rows(series, lambda r: candle(r[0], r[1], r[2], r[3], r[4], r[6]) if len(r) >= 7 else None)


# ----------------------------------------------------------------------------- KUCOIN


class KucoinSpec(ExchangeSpec):
    provider = "kucoin"
    label = "KUCOIN"
    URL = "https://api.kucoin.com/api/v1/market/candles"
    # no "1w": KuCoin weeks start on Thursday, so weekly bars are rebuilt from daily ones (Monday weeks)
    intervals = {
        "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1hour", "2h": "2hour",
        "4h": "4hour", "6h": "6hour", "12h": "12hour", "1d": "1day",
    }
    page_size = 1500
    stop_on_short_page = False
    separator = "-"

    def request(self, native, code, tf_sec, count, cursor, page):
        n = int(min(count, self.page_size))
        end = int(cursor) + 1 if cursor is not None else now_s() + int(tf_sec)
        start = end - n * int(tf_sec)
        return self.URL, {"symbol": native, "type": code, "startAt": start, "endAt": end}, n

    def parse(self, result, native):
        data = result.data if isinstance(result.data, dict) else None
        if data is None:
            raise unexpected(self.label, self.provider, result)
        code = str(data.get("code", ""))
        if code == "200000":
            # [time, open, close, high, low, volume, turnover], newest first
            return _rows(data.get("data"), lambda r: candle(r[0], r[1], r[3], r[4], r[2], r[5])
                         if len(r) >= 6 else None)
        msg = str(data.get("msg") or code)
        if code == "400100" or "Unsupported trading pair" in msg:
            raise self.not_found(native)
        if code == "429000":
            raise self.api_error("límite de peticiones alcanzado; inténtalo en unos segundos", "rate_limit")
        raise self.api_error(f"error {code}: {msg}")


SPECS: dict[str, ExchangeSpec] = {
    spec.provider: spec for spec in (BinanceSpec(), OkxSpec(), CoinbaseSpec(), KrakenSpec(), KucoinSpec())
}
