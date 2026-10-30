"""DATA-HUB: resolve / search / symbol_info, keyed + recorder history, quotes, universe scoring, tv_scan."""
from __future__ import annotations

import asyncio

import pytest

from app.data.base import ProviderError
from tests.test_data_hub_fakes import Clock, FakeKeyed, FakeTV, bar, make_hub

H = 3600


def test_resolve_order_alias_db_then_tradingview(runtime_factory, monkeypatch):
    tv = FakeTV(search={"zeta corp": [{"symbol": "NYSE:ZETA", "ticker": "ZETA", "description": "Zeta Corp",
                                       "type": "stock", "exchange": "NYSE"}]})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv) as (rt, hub):
            assert await hub.resolve("btc") == "BINANCE:BTCUSDT"  # alias
            assert await hub.resolve("oro") == "OANDA:XAUUSD"
            assert await hub.resolve("  nasdaq:aapl ") == "NASDAQ:AAPL"  # full symbols pass through
            assert await hub.resolve("bad:sym bol$") is None
            await rt.db.execute(
                "INSERT INTO symbols(symbol, ticker, exchange, description, type, market, updated_at) "
                "VALUES ('BME:ZZT', 'ZZT', 'BME', 'Zeta Zeta Telecom', 'stock', 'spain', 0)"
            )
            assert await hub.resolve("zzt") == "BME:ZZT"  # symbols table before TradingView
            assert not [c for c in tv.calls if c[0] == "search"]
            assert await hub.resolve("Zeta Corp") == "NYSE:ZETA"  # best TradingView hit
            assert await hub.resolve("nothing at all") is None
            await rt.settings.set("providers.crypto_default", "okx")
            hub._resolve_cache.clear()
            assert await hub.resolve("eth") == "OKX:ETHUSDT"
            assert await hub.resolve("") is None

    asyncio.run(scenario())


def test_search_alias_first_remote_then_local_and_meta_stored(runtime_factory, monkeypatch):
    tv = FakeTV(search={"apple": [
        {"symbol": "NASDAQ:AAPL", "ticker": "AAPL", "description": "Apple Inc.", "type": "stock", "exchange": "NASDAQ",
         "market": "america", "currency": "USD", "pricescale": 100, "logoid": "apple", "price": 230.5, "change_pct": 1.2},
        {"symbol": "UNISWAP:APPLEUSDT_ABC123", "description": "junk"},  # kept by the client, valid symbol
    ]})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv) as (rt, hub):
            res = await hub.search("apple", limit=10)
            assert res[0]["symbol"] == "NASDAQ:AAPL" and res[0]["price"] == 230.5 and res[0]["change_pct"] == 1.2
            assert res[0]["history"] == "recorder" and res[0]["provider"] == "recorder"  # no API key configured
            assert res[0]["logoid"] == "apple" and res[0]["pricescale"] == 100
            row = await rt.db.fetchone("SELECT description, type, market FROM symbols WHERE symbol = 'NASDAQ:AAPL'")
            assert row == {"description": "Apple Inc.", "type": "stock", "market": "america"}
            btc = await hub.search("btc", limit=5)
            assert btc[0]["symbol"] == "BINANCE:BTCUSDT"  # alias first even when TradingView returns nothing
            with pytest.raises(ProviderError):
                await hub.search("btc", market="NOT VALID")
            assert await hub.search("   ") == []

    asyncio.run(scenario())


def test_symbol_info_history_kinds_and_details(runtime_factory, monkeypatch):
    tv = FakeTV(details={"NASDAQ:NVDA": {"symbol": "NASDAQ:NVDA", "description": "NVIDIA Corporation", "type": "stock",
                                         "market": "america", "currency": "USD", "pricescale": 100,
                                         "quote": {"price": 140.0, "change_pct": 2.0},
                                         "technicals": {"rating": 0.4, "rsi": 61.5, "adx": "bad"}}})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv) as (rt, hub):
            info = await hub.symbol_info("NASDAQ:NVDA")
            assert info["description"] == "NVIDIA Corporation" and info["history"] == "recorder"
            assert set(info) == {"symbol", "ticker", "exchange", "description", "type", "market", "currency",
                                 "pricescale", "logoid", "provider", "history"}
            calls = len(tv.calls)
            await hub.symbol_info("NASDAQ:NVDA")  # described now: no new TradingView call
            assert len(tv.calls) == calls
            await rt.settings.set("providers.twelvedata_key", "demo-key")
            assert (await hub.symbol_info("NASDAQ:NVDA"))["history"] == "keyed"
            assert hub.history_kind("NASDAQ:NVDA") == ("twelvedata", "keyed")
            assert hub.history_kind("CME_MINI:ES1!") == ("recorder", "recorder")  # no keyed provider covers futures
            assert hub.history_kind("BINANCE:SOLUSDT") == ("binance", "full")
            assert hub.history_kind("SIM:NEXUS") == ("synthetic", "synthetic")
            detail = await hub.symbol_detail("NASDAQ:NVDA")
            assert detail["quote"]["price"] == 140.0 and detail["technicals"] == {"rating": 0.4, "rsi": 61.5, "adx": None}

    asyncio.run(scenario())


def test_keyed_provider_cache_and_incremental_refresh(runtime_factory, monkeypatch):
    clock = Clock()
    keyed = FakeKeyed(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, keyed=keyed, clock=clock) as (rt, hub):
            await rt.settings.set("providers.polygon_key", "pk")
            cs = await hub.get_candles("NASDAQ:AAPL", "1h", 60)
            assert len(cs) == 60 and cs.history == "keyed" and cs.provider == "polygon"
            assert keyed.calls[-1]["limit"] == 60 and keyed.calls[-1]["api_key"] == "pk"
            assert keyed.calls[-1]["asset_class"] == "stock"
            await hub.get_candles("NASDAQ:AAPL", "1h", 60)  # fresh (TTL) -> no request
            assert len(keyed.calls) == 1
            clock.advance(3 * H)
            hub._keyed_fresh.clear()
            cs2 = await hub.get_candles("NASDAQ:AAPL", "1h", 60)
            assert keyed.calls[-1]["limit"] == 5 and int(cs2.time[-1]) == int(cs.time[-1]) + 3 * H
            # provider failure: cached data with a note; without cache -> recorder history
            keyed.fail = ProviderError("clave inválida")
            hub._keyed_fresh.clear()
            stale = await hub.get_candles("NASDAQ:AAPL", "1h", 60)
            assert len(stale) == 60 and "clave inválida" in stale.note
            other = await hub.get_candles("NASDAQ:MSFT", "1h", 60)
            assert other.history == "recorder" and "clave inválida" in other.note

    asyncio.run(scenario())


def test_recorder_history_resamples_missing_timeframes(runtime_factory, monkeypatch):
    clock = Clock()

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, clock=clock) as (rt, hub):
            start = clock() // H * H - 10 * H + 900  # the first 1h bucket is incomplete (starts at :15)
            rows = [bar(start + i * 900) for i in range(37)]
            await rt.db.executemany(
                "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                "VALUES ('FX:EURUSD', '15m', ?, ?, ?, ?, ?, ?, 'recorder')",
                [(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows],
            )
            cs = await hub.get_candles("FX:EURUSD", "30m", 100)
            assert cs.history == "recorder" and cs.provider == "recorder"
            assert len(cs) == 18 and all(int(t) % 1800 == 0 for t in cs.time)  # 37 x 15m -> 18 full 30m bars
            assert "Historial grabado localmente: 18 velas" in cs.note and "clave" in cs.note
            empty = await hub.get_candles("FX:GBPUSD", "1h", 100)
            assert len(empty) == 0 and "Aún no hay historial local" in empty.note

    asyncio.run(scenario())


def test_quotes_tradingview_sim_and_local_fallback(runtime_factory, monkeypatch):
    clock = Clock()
    tv = FakeTV(quotes={"NASDAQ:AAPL": {"symbol": "NASDAQ:AAPL", "price": 231.0, "change_pct": 0.5, "ts": 123}})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            day = clock() // 86400 * 86400
            await rt.db.executemany(
                "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                "VALUES ('FX:EURUSD', '1d', ?, ?, ?, ?, ?, ?, 'recorder')",
                [(day - 86400, 1.0, 1.1, 0.9, 1.05, 0.0), (day, 1.05, 1.2, 1.0, 1.1, 0.0)],
            )
            quotes = await hub.quotes(["NASDAQ:AAPL", "SIM:NEXUS", "FX:EURUSD", "bad symbol", "NASDAQ:AAPL"])
            assert set(quotes) == {"NASDAQ:AAPL", "SIM:NEXUS", "FX:EURUSD"}
            assert quotes["NASDAQ:AAPL"]["price"] == 231.0 and quotes["NASDAQ:AAPL"]["ts"] == 123
            assert quotes["NASDAQ:AAPL"]["high"] is None  # every Quote key present
            eur = quotes["FX:EURUSD"]
            assert eur["price"] == 1.1 and eur["change_pct"] == pytest.approx((1.1 - 1.05) / 1.05 * 100)
            assert quotes["SIM:NEXUS"]["price"] > 0
            assert await hub.last_price("NASDAQ:AAPL") == 231.0
            assert await hub.last_price("FX:EURUSD") == 1.1

    asyncio.run(scenario())


class _Scanner:
    def __init__(self) -> None:
        self.seen: list[dict] = []

    def quick_score_rows(self, rows, market=None, tf=None):
        self.seen = rows
        return [(0.42, 0.71) if r.get("Recommend.All") is not None else None for r in rows]


def test_universe_rows_scores_and_scan_delegation(runtime_factory, monkeypatch):
    tv = FakeTV(universe_rows=[
        {"symbol": "BINANCE:BTCUSDT", "description": "Bitcoin", "type": "spot", "close": 65000.0, "change_pct": 1.5,
         "volume": 1e9, "rating": 0.3, "rsi": 58.0, "extra": "x"},
        {"symbol": "BINANCE:ETHUSDT", "description": "Ethereum", "type": "spot", "close": 3000.0},
    ], counts={"crypto": 1500, "america": 9000})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv) as (rt, hub):
            rt.scanner = _Scanner()
            data = await hub.universe("crypto", limit=10, sort="Recommend.All", order="ASC")
            assert data["total"] == 1002 and [r["symbol"] for r in data["rows"]] == ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"]
            btc, eth = data["rows"]
            assert btc["quick_score"] == 0.42 and btc["quick_prob"] == 0.71 and eth["quick_score"] is None
            assert btc["rating"] == 0.3 and btc["market_cap"] is None and "extra" not in btc
            assert rt.scanner.seen[0]["Recommend.All"] == 0.3 and rt.scanner.seen[0]["RSI"] == 58.0
            assert tv.calls[-1] == ("universe", "crypto", 0, 10, "Recommend.All", "asc", None, None)
            scan = await hub.tv_scan("crypto", ["close", "rating"], sort_by="volume", limit=1,
                                     tickers=["BINANCE:ETHUSDT"])
            assert scan == {"total": 1, "rows": [{"symbol": "BINANCE:ETHUSDT", "close": 3000.0, "rating": None}]}
            markets = {m["id"]: m for m in await hub.markets()}
            assert markets["crypto"]["count"] == 1500 and markets["spain"]["count"] is None
            with pytest.raises(ProviderError):
                await hub.universe("¿?")
            await rt.settings.set("providers.tradingview_enabled", False)  # disabled -> offline behaviour
            assert await hub.tv_scan("crypto", ["close"]) == {"total": 0, "rows": []}
            assert await hub.market_counts() == {}

    asyncio.run(scenario())
