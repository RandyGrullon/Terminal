"""Shared helpers: timeframes, time, JSON hygiene, HTTP client, rate limiting, caching."""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import ssl
import time
from collections import OrderedDict
from typing import Any

import httpx
import numpy as np

from app.config import USER_AGENT

# ----------------------------------------------------------------------------- timeframes

TF_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}
TIMEFRAMES: list[str] = list(TF_SECONDS)

_MINUTES_TO_TF = {str(v // 60): k for k, v in TF_SECONDS.items() if v < 86400}
_TF_ALIASES = {
    "d": "1d", "1d": "1d", "day": "1d", "1day": "1d", "daily": "1d", "diario": "1d",
    "w": "1w", "1w": "1w", "week": "1w", "1week": "1w", "weekly": "1w", "semanal": "1w",
    "h1": "1h", "h2": "2h", "h4": "4h", "h6": "6h", "h12": "12h",
    "m1": "1m", "m3": "3m", "m5": "5m", "m15": "15m", "m30": "30m",
    "1min": "1m", "3min": "3m", "5min": "5m", "15min": "15m", "30min": "30m",
    "1hour": "1h", "2hour": "2h", "4hour": "4h", "6hour": "6h", "12hour": "12h",
}
_WEEK_ANCHOR = 345600  # 1970-01-05 00:00 UTC (a Monday)


def normalize_tf(text: Any) -> str:
    """Accepts '1h', '1H', '60', 'h1', 'D', '1D', 'W', '240'... and returns a key of TF_SECONDS."""
    if text is None:
        raise ValueError("Temporalidad vacía")
    s = str(text).strip()
    if s in TF_SECONDS:
        return s
    if s in ("1M", "M"):
        raise ValueError("La temporalidad mensual no está soportada")
    low = s.lower()
    if low in TF_SECONDS:
        return low
    if low in _TF_ALIASES:
        return _TF_ALIASES[low]
    if low.isdigit() and low in _MINUTES_TO_TF:
        return _MINUTES_TO_TF[low]
    raise ValueError(f"Temporalidad no válida: {text!r} (usa {', '.join(TIMEFRAMES)})")


def tf_seconds(tf: str) -> int:
    return TF_SECONDS[normalize_tf(tf)]


def floor_time(ts: int | float, tf: str) -> int:
    """Open time of the bar of timeframe `tf` containing unix second `ts` (weeks start Monday 00:00 UTC)."""
    sec = tf_seconds(tf)
    ts = int(ts)
    if sec == TF_SECONDS["1w"]:
        return ((ts - _WEEK_ANCHOR) // sec) * sec + _WEEK_ANCHOR
    return ts - (ts % sec)


def floor_times(arr: np.ndarray, tf: str) -> np.ndarray:
    """Vectorized floor_time for an int64 array."""
    sec = tf_seconds(tf)
    a = np.asarray(arr, dtype=np.int64)
    if sec == TF_SECONDS["1w"]:
        return ((a - _WEEK_ANCHOR) // sec) * sec + _WEEK_ANCHOR
    return a - (a % sec)


def now_s() -> int:
    return int(time.time())


def now_ms() -> int:
    return int(time.time() * 1000)


# ----------------------------------------------------------------------------- JSON hygiene


def clean_json(obj: Any) -> Any:
    """Recursively make `obj` JSON-safe: numpy -> python, NaN/inf -> None, tuples/sets -> lists."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, np.generic):
        return clean_json(obj.item())
    if isinstance(obj, np.ndarray):
        return [clean_json(x) for x in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean_json(x) for x in obj]
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return clean_json(obj.to_dict())
    return str(obj)


def to_list(arr: Any, decimals: int | None = None) -> list:
    """numpy array -> list of floats with None for NaN/inf."""
    a = np.asarray(arr, dtype=float)
    if decimals is not None:
        a = np.round(a, decimals)
    return [x if math.isfinite(x) else None for x in a.tolist()]


def safe_float(x: Any, default: float | None = None) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# ----------------------------------------------------------------------------- symbols


def parse_symbol(symbol: str) -> tuple[str, str]:
    """'BINANCE:BTCUSDT' -> ('BINANCE', 'BTCUSDT'); 'BTCUSDT' -> ('', 'BTCUSDT')."""
    s = (symbol or "").strip().upper()
    if ":" in s:
        exchange, ticker = s.split(":", 1)
        return exchange, ticker
    return "", s


# ----------------------------------------------------------------------------- HTTP

_SSL_CTX: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """SSL context backed by the OS trust store (works behind corporate TLS-inspecting proxies)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            import truststore

            _SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:  # pragma: no cover - truststore is a hard dependency
            _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def http_client(timeout: float = 15.0, **kw: Any) -> httpx.AsyncClient:
    """The only sanctioned way to make outbound HTTP calls (honest UA + system certificates)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    headers.update(kw.pop("headers", None) or {})
    return httpx.AsyncClient(
        timeout=timeout, verify=ssl_context(), headers=headers, follow_redirects=True, **kw
    )


class RateLimiter:
    """`async with limiter:` enforces a minimum interval between entries and a concurrency cap."""

    def __init__(self, min_interval_s: float = 0.0, max_concurrency: int = 1):
        self._min = float(min_interval_s)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> "RateLimiter":
        await self._sem.acquire()
        try:
            async with self._lock:
                wait = self._last + self._min - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
        except BaseException:
            self._sem.release()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._sem.release()


class TTLCache:
    """Tiny LRU cache with per-entry expiry (monotonic clock)."""

    def __init__(self, ttl_s: float, maxsize: int = 512):
        self.ttl = float(ttl_s)
        self.maxsize = maxsize
        self._d: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any, default: Any = None) -> Any:
        item = self._d.get(key)
        if item is None:
            return default
        expires, value = item
        if expires < time.monotonic():
            self._d.pop(key, None)
            return default
        self._d.move_to_end(key)
        return value

    def set(self, key: Any, value: Any, ttl: float | None = None) -> None:
        self._d[key] = (time.monotonic() + (self.ttl if ttl is None else ttl), value)
        self._d.move_to_end(key)
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)

    def pop(self, key: Any) -> None:
        self._d.pop(key, None)

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


class RateLimitedLog:
    """Avoid spamming the same error: `if log.should_log(key, 60): rt.bus.emit(...)`."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def should_log(self, key: str, every_s: float = 60.0) -> bool:
        now = time.monotonic()
        if now - self._last.get(key, -1e18) >= every_s:
            self._last[key] = now
            return True
        return False
