"""Raw market metrics for the semáforo (conditions.py interprets them):

- Volatility / futures / dollar / rates quotes through the hub's TradingView scanner (one request per minute).
- Crypto Fear & Greed (alternative.me, 60 min cache) and OKX perpetual funding for BTC / ETH (10 min cache).

Every source is optional: failures are logged (rate-limited, source "macro") and the last good value is kept.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from app import config
from app.util import RateLimitedLog, http_client, now_ms, safe_float

QUOTE_TICKERS: dict[str, str] = {
    "vix": "TVC:VIX",
    "es": "CME_MINI:ES1!",
    "nq": "CME_MINI:NQ1!",
    "dxy": "TVC:DXY",
    "us10y": "TVC:US10Y",
    "us02y": "TVC:US02Y",
    "move": "TVC:MOVE",
    "oil": "NYMEX:CL1!",
    "gold": "COMEX:GC1!",
    "btc_d": "CRYPTOCAP:BTC.D",
}
COLUMNS = ["close", "change", "change_abs", "high", "low"]
FNG_URL = "https://api.alternative.me/fng/?limit=1"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
FUNDING_INST: dict[str, str] = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP"}
QUOTES_TTL_S = 60
FNG_TTL_S = 3600
FUNDING_TTL_S = 600
RETRY_S = 300  # after a failed crypto request
QUOTES_STALE_S = 900  # quotes older than this (failed refreshes) mark the conditions as stale


# ----------------------------------------------------------------------------- parsing (pure)


def parse_quotes(data: Any) -> dict[str, dict]:
    """tv_scan result -> {metric id: {value, change_pct, change_abs, high, low}} (unknown/empty rows skipped)."""
    by_symbol = {v: k for k, v in QUOTE_TICKERS.items()}
    out: dict[str, dict] = {}
    rows = data.get("rows") if isinstance(data, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mid = by_symbol.get(str(row.get("symbol") or "").upper())
        value = safe_float(row.get("close"))
        if mid is None or value is None:
            continue
        out[mid] = {
            "value": value,
            "change_pct": safe_float(row.get("change")),
            "change_abs": safe_float(row.get("change_abs")),
            "high": safe_float(row.get("high")),
            "low": safe_float(row.get("low")),
        }
    return out


def parse_fng(data: Any) -> dict | None:
    """alternative.me response -> {value 0..100, classification, time (unix s)}."""
    items = data.get("data") if isinstance(data, dict) else None
    if not items or not isinstance(items[0], dict):
        return None
    value = safe_float(items[0].get("value"))
    if value is None or not 0 <= value <= 100:
        return None
    ts = safe_float(items[0].get("timestamp"))
    return {
        "value": int(round(value)),
        "classification": str(items[0].get("value_classification") or ""),
        "time": int(ts) if ts is not None else None,
    }


def parse_funding(data: Any) -> dict | None:
    """OKX public funding-rate response -> {rate (fraction per period), interval_h, time (ms)}."""
    if not isinstance(data, dict) or str(data.get("code", "0")) != "0":
        return None
    items = data.get("data")
    if not items or not isinstance(items[0], dict):
        return None
    item = items[0]
    rate = safe_float(item.get("fundingRate"))
    if rate is None:
        return None
    t = safe_float(item.get("fundingTime"))
    prev = safe_float(item.get("prevFundingTime"))
    nxt = safe_float(item.get("nextFundingTime"))
    interval = None
    if t is not None and prev is not None and t > prev:
        interval = (t - prev) / 3_600_000.0
    elif t is not None and nxt is not None and nxt > t:
        interval = (nxt - t) / 3_600_000.0
    return {"rate": rate, "interval_h": round(interval, 2) if interval else None, "time": int(t) if t else None}


# ----------------------------------------------------------------------------- collector


class MetricsCollector:
    """Keeps the latest good value of every source and refreshes each one on its own cadence."""

    def __init__(self, rt: Any, client_factory: Callable[..., Any] | None = None,
                 clock: Callable[[], float] | None = None):
        self.rt = rt
        self._client_factory = client_factory or http_client
        self._clock = clock or time.monotonic
        self._next: dict[str, float] = {}
        self._log = RateLimitedLog()
        self.quotes: dict[str, dict] = {}
        self.quotes_at: int | None = None
        self.fng: dict | None = None
        self.fng_at: int | None = None
        self.funding: dict[str, dict] = {}
        self.funding_at: int | None = None
        self.errors: dict[str, str] = {}

    def _due(self, key: str) -> bool:
        return self._clock() >= self._next.get(key, float("-inf"))

    def _schedule(self, key: str, seconds: float) -> None:
        self._next[key] = self._clock() + seconds

    def _fail(self, key: str, label: str, exc: BaseException) -> None:
        self.errors[key] = f"{type(exc).__name__}: {exc}"[:200]
        if self.rt is not None and self.rt.bus is not None and self._log.should_log(key, 1800):
            self.rt.bus.emit("warn", "macro", f"Semáforo: no se pudo leer {label} ({self.errors[key]})")

    async def refresh(self, crypto: bool = True, force: bool = False) -> list[str]:
        """Refresh what is due (never touches the network when config.OFFLINE). Returns the updated sources."""
        if config.OFFLINE:
            return []
        updated: list[str] = []
        if force or self._due("quotes"):
            if await self._refresh_quotes():
                updated.append("quotes")
        if crypto and (force or self._due("fng")):
            if await self._refresh_fng():
                updated.append("fng")
        if crypto and (force or self._due("funding")):
            if await self._refresh_funding():
                updated.append("funding")
        return updated

    async def _refresh_quotes(self) -> bool:
        hub = getattr(self.rt, "hub", None)
        self._schedule("quotes", QUOTES_TTL_S)
        if hub is None or not hasattr(hub, "tv_scan"):
            return False
        try:
            data = await hub.tv_scan("global", list(COLUMNS), tickers=list(QUOTE_TICKERS.values()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail("quotes", "las cotizaciones (VIX, futuros, DXY)", exc)
            return False
        quotes = parse_quotes(data)
        if not quotes:
            return False
        self.quotes.update(quotes)
        self.quotes_at = now_ms()
        self.errors.pop("quotes", None)
        return True

    async def _get_json(self, url: str, params: dict | None = None) -> Any:
        async with self._client_factory(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            return resp.json()

    async def _refresh_fng(self) -> bool:
        try:
            parsed = parse_fng(await self._get_json(FNG_URL))
            if parsed is None:
                raise ValueError("respuesta sin datos")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._schedule("fng", RETRY_S)
            self._fail("fng", "el índice de miedo/codicia cripto", exc)
            return False
        self._schedule("fng", FNG_TTL_S)
        self.fng, self.fng_at = parsed, now_ms()
        self.errors.pop("fng", None)
        return True

    async def _refresh_funding(self) -> bool:
        got: dict[str, dict] = {}
        for coin, inst in FUNDING_INST.items():
            try:
                parsed = parse_funding(await self._get_json(OKX_FUNDING_URL, {"instId": inst}))
                if parsed is None:
                    raise ValueError("respuesta sin datos")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail("funding", f"el funding de {coin} (OKX)", exc)
                continue
            got[coin] = parsed
        if not got:
            self._schedule("funding", RETRY_S)
            return False
        self._schedule("funding", FUNDING_TTL_S)
        self.funding.update(got)
        self.funding_at = now_ms()
        if len(got) == len(FUNDING_INST):
            self.errors.pop("funding", None)
        return True

    def snapshot(self) -> dict:
        """Raw metrics for conditions.assess (copies, JSON-safe)."""
        return {
            "quotes": {k: dict(v) for k, v in self.quotes.items()},
            "quotes_at": self.quotes_at,
            "fng": dict(self.fng) if self.fng else None,
            "fng_at": self.fng_at,
            "funding": {k: dict(v) for k, v in self.funding.items()},
            "funding_at": self.funding_at,
        }

    def status(self) -> dict:
        return {"quotes": len(self.quotes), "quotes_at": self.quotes_at, "fng": bool(self.fng),
                "funding": sorted(self.funding), "errors": dict(self.errors)}
