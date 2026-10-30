"""Test doubles for the DATA-HUB tests (no tests in this module): a controllable clock, fake exchange / keyed
providers, a fake TradingView client, a WS recorder and a `make_hub` helper. Everything is offline."""
from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from typing import Any

from app import config as nexus_config
from app.data import hub as hub_mod
from app.util import floor_time, tf_seconds

T0 = 1_760_000_400  # aligned to the hour (same "now" as conftest.FIXED_END)


class Clock:
    """Replaces app.data.hub.now_s so cache freshness and synthetic data are deterministic."""

    def __init__(self, t: int = T0 + 1800):
        self.t = int(t)

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds)


def bar(t: int, scale: float = 1.0) -> dict:
    """Deterministic candle whose prices depend only on its time."""
    base = (100.0 + 10.0 * math.sin(t / 50_000.0) + (t % 7919) / 7919.0) * scale
    o, c = base, base * 1.001
    return {"time": t, "open": o, "high": max(o, c) * 1.002, "low": min(o, c) * 0.998, "close": c,
            "volume": 10.0 + (t // 60) % 13}


class FakeProviders:
    """Mimics app.data.providers: bars up to the one forming at clock(); optional listing start / failures."""

    def __init__(self, clock: Clock, listing_start: int | None = None, fail: dict[str, Exception] | None = None,
                 delay: float = 0.0):
        self.clock = clock
        self.listing_start = listing_start
        self.fail = dict(fail or {})
        self.delay = delay
        self.calls: list[dict] = []

    async def fetch_candles(self, provider: str, native: str, tf: str, limit: int, *, client: Any,
                            end_time: int | None = None) -> list[dict]:
        self.calls.append({"provider": provider, "native": native, "tf": tf, "limit": int(limit), "end_time": end_time})
        if self.delay:
            await asyncio.sleep(self.delay)
        if provider in self.fail:
            raise self.fail[provider]
        tf_s = tf_seconds(tf)
        end = floor_time(self.clock() if end_time is None else end_time, tf)
        start = end - (int(limit) - 1) * tf_s
        if self.listing_start is not None:
            start = max(start, floor_time(self.listing_start, tf))
        return [bar(t) for t in range(start, end + 1, tf_s)]

    async def fetch_latest(self, provider: str, native: str, tf: str, *, client: Any) -> list[dict]:
        return (await self.fetch_candles(provider, native, tf, 2, client=client))[-2:]


class FakeKeyed:
    """Mimics app.data.keyed.fetch_keyed_candles."""

    def __init__(self, clock: Clock, fail: Exception | None = None):
        self.clock = clock
        self.fail = fail
        self.calls: list[dict] = []

    async def fetch_keyed_candles(self, provider: str, symbol: str, asset_class: str, tf: str, limit: int, *,
                                  api_key: str, client: Any) -> list[dict]:
        self.calls.append({"provider": provider, "symbol": symbol, "asset_class": asset_class, "tf": tf,
                           "limit": int(limit), "api_key": api_key})
        if self.fail is not None:
            raise self.fail
        tf_s = tf_seconds(tf)
        end = floor_time(self.clock(), tf)
        return [bar(t, 2.0) for t in range(end - (int(limit) - 1) * tf_s, end + 1, tf_s)]


class FakeTV:
    """Mimics app.data.tradingview.TradingViewClient (only what the hub calls)."""

    def __init__(self, search: dict[str, list[dict]] | None = None, universe_rows: list[dict] | None = None,
                 bars: dict[str, dict[str, dict]] | None = None, quotes: dict[str, dict] | None = None,
                 details: dict[str, dict] | None = None, counts: dict[str, int] | None = None):
        self.search_results = {k.lower(): v for k, v in (search or {}).items()}
        self.universe_rows = list(universe_rows or [])
        self.bars = dict(bars or {})
        self.quote_map = dict(quotes or {})
        self.details = dict(details or {})
        self.counts = dict(counts or {})
        self.calls: list[tuple] = []

    async def search(self, q: str, market: str | None = None, limit: int = 30) -> list[dict]:
        self.calls.append(("search", q, market, limit))
        return [dict(r) for r in self.search_results.get(q.lower(), [])][:limit]

    async def scan(self, market: str, columns: list[str], *, filters: Any = None, sort_by: str | None = None,
                   order: str = "desc", limit: int = 300, offset: int = 0, tickers: list[str] | None = None) -> dict:
        self.calls.append(("scan", market, tuple(columns), sort_by, limit, tuple(tickers or ())))
        rows = [{"symbol": r["symbol"], **{c: r.get(c) for c in columns}} for r in self.universe_rows]
        if tickers:
            rows = [r for r in rows if r["symbol"] in tickers]
        return {"total": len(rows), "rows": rows[offset: offset + limit]}

    async def quotes(self, symbols: list[str]) -> dict[str, dict]:
        self.calls.append(("quotes", tuple(symbols)))
        return {s: dict(self.quote_map[s]) for s in symbols if s in self.quote_map}

    async def universe(self, market: str, offset: int = 0, limit: int = 100, sort: str = "volume",
                       order: str = "desc", q: str | None = None, type_: str | None = None) -> dict:
        self.calls.append(("universe", market, offset, limit, sort, order, q, type_))
        return {"total": len(self.universe_rows) + 1000, "rows": [dict(r) for r in self.universe_rows[offset: offset + limit]]}

    async def market_counts(self) -> dict[str, int]:
        return dict(self.counts)

    async def symbol_details(self, symbol: str) -> dict | None:
        self.calls.append(("details", symbol))
        d = self.details.get(symbol)
        return dict(d) if d else None

    async def current_bars(self, symbols: list[str], tfs: list[str]) -> dict[str, dict[str, dict]]:
        self.calls.append(("current_bars", tuple(symbols), tuple(tfs)))
        return {s: {tf: dict(c) for tf, c in self.bars.get(s, {}).items() if tf in tfs} for s in symbols if s in self.bars}

    def status(self) -> dict:
        return {"enabled": True, "fake": True}

    async def close(self) -> None:
        return None


class WSStub:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    def publish(self, topic: str, message: dict) -> None:
        self.messages.append((topic, message))


@asynccontextmanager
async def make_hub(runtime_factory, monkeypatch, *, offline: bool = False, providers: Any = None,
                   keyed: Any = None, tv: Any = None, clock: Clock | None = None):
    """Runtime (temp DB) + a started MarketDataHub wired to fakes; no background loops."""
    monkeypatch.setattr(nexus_config, "OFFLINE", offline)
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    clock = clock or Clock()
    monkeypatch.setattr(hub_mod, "now_s", clock)
    async with runtime_factory() as rt:
        hub = hub_mod.MarketDataHub(rt)
        rt.hub = hub
        rt.ws = WSStub()
        hub._http = lambda: None
        if providers is not None:
            hub._providers = lambda: providers
        if keyed is not None:
            hub._keyed_module = lambda: keyed
        if tv is not None:
            hub._tv = tv
        else:
            hub._tv_error, hub._tv_error_at = "sin cliente en las pruebas", float("inf")
        await hub.start()
        try:
            yield rt, hub
        finally:
            await hub.stop()
