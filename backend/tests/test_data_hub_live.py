"""DATA-HUB against the real public endpoints (skipped unless NEXUS_NET_TESTS=1). Keeps requests to a minimum."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.data.hub import MarketDataHub


@pytest.mark.network
def test_live_hub_candles_search_quotes(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "OFFLINE", False)
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            hub = MarketDataHub(rt)
            rt.hub = hub
            await hub.start()
            try:
                cs = await hub.get_candles("BINANCE:BTCUSDT", "1h", 50)
                assert len(cs) == 50 and cs.history == "full" and cs.last_close > 0, cs.note
                cached = await hub.get_candles("BINANCE:BTCUSDT", "1h", 50)  # served from the DB cache
                assert cached.last_time == cs.last_time and hub.cache_hits >= 1
                results = await hub.search("apple", limit=10)
                assert any(r["symbol"] == "NASDAQ:AAPL" for r in results)
                quotes = await hub.quotes(["BINANCE:BTCUSDT", "NASDAQ:AAPL"])
                assert quotes["BINANCE:BTCUSDT"]["price"] > 0
                info = await hub.symbol_info("NASDAQ:AAPL")
                assert info["history"] in ("recorder", "keyed") and info["description"]
            finally:
                await hub.stop()

    asyncio.run(scenario())
