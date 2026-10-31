"""HTTP plumbing shared by the market-data providers (exchanges, keyed APIs, TradingView scanner).

- `DataProviderError`: a `ProviderError` with a machine-readable `kind` so callers can decide whether to try another
  venue (`not_found`), back off (`rate_limit`) or tell the user to fix a key (`auth`). Messages are Spanish.
- `loop_limiter()`: one `RateLimiter` per (event loop, key) — asyncio primitives must not cross event loops.
- `request_json()`: one call through the shared client with retries on network errors, HTTP 429/418 and 5xx.
"""
from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from typing import Any

import httpx

from app import config
from app.data.base import ProviderError
from app.util import RateLimiter

OFFLINE_MESSAGE = "Modo sin conexión"

# kinds: offline | not_found | rate_limit | auth | premium | network | server | invalid | unsupported | error
ERROR_KINDS = (
    "offline", "not_found", "rate_limit", "auth", "premium", "network", "server", "invalid", "unsupported", "error",
)


class DataProviderError(ProviderError):
    """ProviderError carrying the provider id and an error kind (see ERROR_KINDS)."""

    def __init__(self, message: str, *, provider: str = "", kind: str = "error"):
        super().__init__(message)
        self.provider = provider
        self.kind = kind if kind in ERROR_KINDS else "error"


def offline_error(provider: str = "") -> DataProviderError:
    return DataProviderError(OFFLINE_MESSAGE, provider=provider, kind="offline")


def ensure_online(provider: str = "") -> None:
    """Raise the Spanish offline error when config.OFFLINE is set (read at call time)."""
    if config.OFFLINE:
        raise offline_error(provider)


# ----------------------------------------------------------------------------- rate limiters

_LIMITERS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, RateLimiter]]" = weakref.WeakKeyDictionary()

# provider -> (min seconds between requests, max concurrent requests); conservative vs. the public limits
LIMITS: dict[str, tuple[float, int]] = {
    "binance": (0.12, 3),
    "okx": (0.12, 2),
    "coinbase": (0.15, 2),
    "kraken": (1.05, 1),
    "kucoin": (0.2, 2),
    "twelvedata": (0.5, 1),
    "alphavantage": (1.2, 1),
    "polygon": (0.5, 1),
}


def loop_limiter(key: str, min_interval_s: float | None = None, max_concurrency: int | None = None) -> RateLimiter:
    """RateLimiter shared by every request with the same key inside the running event loop."""
    loop = asyncio.get_running_loop()
    per_loop = _LIMITERS.get(loop)
    if per_loop is None:
        per_loop = {}
        _LIMITERS[loop] = per_loop
    limiter = per_loop.get(key)
    if limiter is None:
        default_interval, default_conc = LIMITS.get(key, (0.25, 2))
        limiter = RateLimiter(
            default_interval if min_interval_s is None else min_interval_s,
            default_conc if max_concurrency is None else max_concurrency,
        )
        per_loop[key] = limiter
    return limiter


# ----------------------------------------------------------------------------- requests


@dataclass
class HttpResult:
    status: int
    data: Any  # parsed JSON (dict/list) or None when the body is not JSON
    text: str
    headers: httpx.Headers


def _retry_after(headers: httpx.Headers, attempt: int) -> float:
    raw = headers.get("Retry-After")
    try:
        wait = float(raw) if raw is not None else 0.0
    except ValueError:
        wait = 0.0
    if wait <= 0:
        wait = 1.0 * (attempt + 1)
    return min(wait, 5.0)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    label: str,
    params: dict | None = None,
    json_body: Any = None,
    headers: dict | None = None,
    limiter: RateLimiter | None = None,
    retries: int = 2,
    timeout: float | None = None,
) -> HttpResult:
    """Perform one HTTP call. Network errors, 429/418 and 5xx are retried (bounded); any other status is returned
    to the caller, which interprets provider-specific error payloads. Raises DataProviderError (Spanish).
    `timeout` (seconds) overrides the client's timeout for this call (live polls use a short one, retries=0)."""
    ensure_online(provider)
    lim = limiter if limiter is not None else loop_limiter(provider)
    extra: dict[str, Any] = {"timeout": float(timeout)} if timeout is not None else {}
    attempt = 0
    while True:
        try:
            async with lim:
                resp = await client.request(method, url, params=params, json=json_body, headers=headers, **extra)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < retries:
                attempt += 1
                await asyncio.sleep(0.5 * attempt)
                continue
            raise DataProviderError(
                f"{label}: error de red ({type(exc).__name__})", provider=provider, kind="network"
            ) from exc
        status = resp.status_code
        if status in (429, 418):
            if attempt < retries:
                await asyncio.sleep(_retry_after(resp.headers, attempt))
                attempt += 1
                continue
            raise DataProviderError(
                f"{label}: límite de peticiones alcanzado (HTTP {status}); inténtalo en unos segundos",
                provider=provider, kind="rate_limit",
            )
        if status >= 500:
            if attempt < min(retries, 1):
                attempt += 1
                await asyncio.sleep(1.0)
                continue
            raise DataProviderError(f"{label}: error del servidor (HTTP {status})", provider=provider, kind="server")
        text = resp.text
        try:
            data = resp.json() if text.strip() else None
        except ValueError:
            data = None
        return HttpResult(status, data, text, resp.headers)


def unexpected(label: str, provider: str, result: HttpResult) -> DataProviderError:
    """Generic error for an HTTP status / payload the caller does not recognise."""
    snippet = " ".join(result.text.split())[:160]
    detail = f"HTTP {result.status}" + (f": {snippet}" if snippet else "")
    kind = "auth" if result.status in (401, 403) else "not_found" if result.status == 404 else "error"
    return DataProviderError(f"{label}: respuesta inesperada ({detail})", provider=provider, kind=kind)
