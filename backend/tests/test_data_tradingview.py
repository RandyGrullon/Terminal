"""TradingView scanner client: request building, parsing, search ranking, quotes, universe, details, current bars,
market counts, OFFLINE / disabled behaviour — against a fake scanner that interprets the request body (offline)."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app import config
from app.data import tradingview_parse as P
from app.data.providers_http import DataProviderError
from app.data.tradingview import TradingViewClient
from app.util import floor_time, http_client

# Real scanner rows captured once (columns: name, description, type, exchange, currency, close, volume, 24h_vol|5)
REAL_BTC = [
    ("CRYPTOCAP:BTC", ["BTC", "Market Cap BTC, $", "index", "CRYPTOCAP", "USD", 1548475001570, 58155627963.86, None]),
    ("OKX:BTCUSDT.P", ["BTCUSDT.P", "BTCUSDT Perpetual Swap Contract", "swap", "OKX", "USDT", 77145.8,
                       8274164832.31, 12786133679.2]),
    ("COINBASE:BTCUSD.P", ["BTCUSD.P", "BTC / USD PERPETUAL CONTRACT", "swap", "COINBASE", "USD", 77078, 2097049160,
                           228303390255950]),
    ("SET:BTC", ["BTC", "Brooker Group Public Co. Ltd.", "stock", "SET", "THB", 0.23, 50489220, None]),
    ("AMEX:BTC", ["BTC", "Grayscale Bitcoin Mini Trust ETF", "fund", "AMEX", "USD", 34.035, 2344354, None]),
    ("LSE:BTC", ["BTC", "London BTC Company Limited", "stock", "LSE", "GBX", 1.4, 242317, None]),
    ("BINANCE:BTCUSDT.P", ["BTCUSDT.P", "Bitcoin / TetherUS PERPETUAL CONTRACT", "swap", "BINANCE", "USDT", 77150,
                           207623.572, 24968854219.24]),
    ("BINANCE:BTCUSDT", ["BTCUSDT", "Bitcoin / TetherUS", "spot", "BINANCE", "USDT", 77361.68, 21034.71, 1.6e9]),
    ("COINBASE:BTCUSD", ["BTCUSD", "Bitcoin / U.S. Dollar", "spot", "COINBASE", "USD", 77198.45, 9010.2, 7.0e8]),
    ("RAYDIUMCLMM:WBTCUSDC_8MEJ5V.USD", ["WBTCUSDC_8MEJ5V.USD", "Wrapped BTC / USD Coin on Solana (8meJ5VWW) in USD",
                                        "spot", "RAYDIUMCLMM", "USD", 76937.98, 2624.88, 9.9e12]),
]
BTC_COLS = ["name", "description", "type", "exchange", "currency", "close", "volume", "24h_vol|5"]
# Real answer for name/description ~ "apple" on the global market sorted by volume (DEX pools first)
REAL_APPLE = [
    ("UNISWAP3POLYGON:APPLEUSDT_8F6463", ["APPLEUSDT_8F6463", "Apple / USDT0 on Polygon (0x8F6463FBd1e599)", "spot",
                                         "UNISWAP3POLYGON", 83635520275.01]),
    ("PANCAKESWAP:CZAAPLB_8BA5A4", ["CZAAPLB_8BA5A4", "CZ / Apple on BSC (0x8bA5a4410FDF519)", "spot", "PANCAKESWAP",
                                    44293684.48]),
    ("NASDAQ:AAPL", ["AAPL", "Apple Inc.", "stock", "NASDAQ", 22201240]),
    ("FINRA:AAPL6070268", ["AAPL6070268", "Apple Inc. 4.0% 12-MAY-2028", "bond", "FINRA", 11764000]),
    ("SSE:603020", ["603020", "Apple Flavor & Fragrance Group Co. Ltd. Class A", "stock", "SSE", 10676800]),
]
APPLE_COLS = ["name", "description", "type", "exchange", "volume"]


def table(rows, cols, **extra):
    return {sym: {**dict(zip(cols, vals)), **extra.get(sym, {})} for sym, vals in rows}


class FakeScanner:
    """Minimal TradingView screener: markets, tickers, range, sort, filter (in_range/equal/match), filter2."""

    def __init__(self, rows: dict[str, dict], only: dict[str, set[str]] | None = None, fail_sort: str | None = None,
                 status: int = 200):
        self.rows, self.only, self.fail_sort, self.status = rows, only or {}, fail_sort, status
        self.requests: list[tuple[str, dict, httpx.Request]] = []

    @staticmethod
    def _ok(row: dict, f: dict) -> bool:
        v, op, right = row.get(f["left"]), f["operation"], f["right"]
        if op == "in_range":
            return v in right
        if op == "equal":
            return v == right
        if op == "match":
            return str(right).lower() in str(v or "").lower()
        raise AssertionError(op)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        market = req.url.path.strip("/").split("/")[0]
        body = json.loads(req.content)
        self.requests.append((market, body, req))
        if self.status != 200:
            return httpx.Response(self.status, json={"totalCount": 0, "error": "boom", "data": None})
        if self.fail_sort and (body.get("sort") or {}).get("sortBy") == self.fail_sort:
            return httpx.Response(400, json={"totalCount": 0, "error": f"Unknown field \"{self.fail_sort}\""})
        syms = [s for s in self.rows if market in self.only.get(s, {"global", self.rows[s].get("_market", "crypto")})]
        if "symbols" in body:
            syms = [s for s in body["symbols"]["tickers"] if s in syms]
        for f in body.get("filter") or []:
            syms = [s for s in syms if self._ok(self.rows[s], f)]
        if body.get("filter2"):
            ops = [o["expression"] for o in body["filter2"]["operands"]]
            syms = [s for s in syms if any(self._ok(self.rows[s], e) for e in ops)]
        if body.get("sort"):
            col, desc = body["sort"]["sortBy"], body["sort"]["sortOrder"] == "desc"
            have = [s for s in syms if self.rows[s].get(col) is not None]
            have.sort(key=lambda s: self.rows[s][col], reverse=desc)
            syms = have + [s for s in syms if self.rows[s].get(col) is None]
        total = len(syms)
        if "range" in body:
            a, b = body["range"]
            syms = syms[a:b]
        data = [{"s": s, "d": [self.rows[s].get(c) for c in body["columns"]]} for s in syms]
        return httpx.Response(200, json={"totalCount": total, "data": data})


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)


def client(fake, **kw) -> TradingViewClient:
    kw.setdefault("min_interval_s", 0.0)
    return TradingViewClient(client_factory=lambda: http_client(transport=httpx.MockTransport(fake)), **kw)


def run(tv: TradingViewClient, fn):
    async def go():
        try:
            return await fn(tv)
        finally:
            await tv.close()
    return asyncio.run(go())


# ----------------------------------------------------------------------------- bodies & parsing


def test_build_body_and_parse_real_payload():
    body = P.build_body(["name", "close"], filters=[{"left": "type", "operation": "equal", "right": "spot"},
                                                    P.text_filter2("btc")], sort_by="close", order="asc",
                        offset=10, limit=5)
    assert body["range"] == [10, 15] and body["sort"] == {"sortBy": "close", "sortOrder": "asc"}
    assert body["filter"] == [{"left": "type", "operation": "equal", "right": "spot"}]
    assert body["filter2"]["operator"] == "or" and "symbols" not in body
    body = P.build_body(["name"], tickers=["BINANCE:BTCUSDT"])
    assert body == {"columns": ["name"], "symbols": {"tickers": ["BINANCE:BTCUSDT"]}}
    payload = {"totalCount": 187, "data": [{"s": s, "d": d} for s, d in REAL_BTC[:2]]}
    parsed = P.parse_response(payload, BTC_COLS)
    assert parsed["total"] == 187 and parsed["rows"][1]["symbol"] == "OKX:BTCUSDT.P"
    assert parsed["rows"][1]["24h_vol|5"] == 12786133679.2 and parsed["rows"][0]["24h_vol|5"] is None
    with pytest.raises(ValueError, match="Unknown field"):
        P.parse_response({"totalCount": 0, "error": "Unknown field \"x\"", "data": None}, ["x"])
    with pytest.raises(ValueError):
        P.unique_columns(["close", "bad column"])
    assert P.delay_seconds("delayed_streaming_900") == 900 and P.delay_seconds("streaming") == 0
    assert P.delay_seconds(None) is None


def test_scan_request_is_plain_and_mapped():
    fake = FakeScanner(table(REAL_BTC, BTC_COLS))
    tv = client(fake)
    out = run(tv, lambda tv: tv.scan("crypto", ["name", "close", "close"], filters=[
        {"left": "type", "operation": "equal", "right": "spot"}], sort_by="24h_vol|5", limit=2, offset=0))
    market, body, req = fake.requests[0]
    assert market == "crypto" and req.url.path == "/crypto/scan" and req.method == "POST"
    assert body["columns"] == ["name", "close"] and body["range"] == [0, 2]
    assert req.headers["user-agent"].startswith("NexusTerminal/")
    assert all(h not in req.headers for h in ("origin", "referer", "cookie"))
    assert out["total"] == 3 and [r["symbol"] for r in out["rows"]] == ["RAYDIUMCLMM:WBTCUSDC_8MEJ5V.USD",
                                                                        "BINANCE:BTCUSDT"]
    assert tv.status()["requests"] == 1 and tv.status()["running"] is True


def test_scan_rejects_bad_input_and_maps_server_errors():
    fake = FakeScanner(table(REAL_BTC, BTC_COLS), status=400)
    tv = client(fake)

    async def bad(tv):
        for args in (("../etc", ["name"]), ("crypto", ["name; drop"])):
            with pytest.raises(DataProviderError) as info:
                await tv.scan(*args)
            assert info.value.kind == "invalid"
        assert fake.requests == []
        with pytest.raises(DataProviderError, match="petición rechazada") as info:
            await tv.scan("crypto", ["name"])
        return info.value

    err = run(tv, bad)
    assert err.kind == "invalid" and tv.status()["errors"] == 1 and "boom" in tv.status()["last_error"]


def test_disabled_or_offline_returns_empty_without_network(monkeypatch):
    fake = FakeScanner(table(REAL_BTC, BTC_COLS))
    flag = {"on": False}
    tv = client(fake, enabled=lambda: flag["on"])

    async def everything(tv):
        return [
            await tv.scan("crypto", ["name"]), await tv.search("btc"), await tv.quotes(["BINANCE:BTCUSDT"]),
            await tv.universe("crypto"), await tv.market_counts(), await tv.symbol_details("BINANCE:BTCUSDT"),
            await tv.current_bars(["BINANCE:BTCUSDT"], ["1m"]),
        ]

    empty = [{"total": 0, "rows": []}, [], {}, {"total": 0, "rows": []}, {}, None, {}]
    assert run(tv, everything) == empty and fake.requests == []
    flag["on"] = True  # the callable is checked on every call
    monkeypatch.setattr(config, "OFFLINE", True)
    assert run(client(fake, enabled=lambda: flag["on"]), everything) == empty and fake.requests == []
    assert client(fake, enabled=lambda: True).status()["enabled"] is False


# ----------------------------------------------------------------------------- search


def test_search_btc_spot_first_no_dex_and_cached():
    fake = FakeScanner(table(REAL_BTC, BTC_COLS, **{"BINANCE:BTCUSDT": {"pricescale": 100, "logoid": "crypto/BTC"}}))
    tv = client(fake)

    async def go(tv):
        first = await tv.search("btc")
        n = len(fake.requests)
        again = await tv.search("BTC ")
        return first, n, again

    results, n_requests, again = run(tv, go)
    symbols = [r["symbol"] for r in results]
    assert symbols[:2] == ["BINANCE:BTCUSDT", "COINBASE:BTCUSD"]
    assert not any("RAYDIUM" in s for s in symbols)
    perps = [i for i, s in enumerate(symbols) if s.endswith(".P")]
    spots = [i for i, s in enumerate(symbols) if not s.endswith(".P")]
    assert perps and min(perps) > max(i for i in spots if results[i]["type"] in ("spot", "stock", "index"))
    top = results[0]
    assert set(top) == {"symbol", "ticker", "exchange", "description", "type", "market", "currency", "pricescale",
                        "logoid", "provider", "history", "price", "change_pct"}
    assert (top["market"], top["provider"], top["history"], top["pricescale"]) == ("crypto", "binance", "full", 100)
    assert top["price"] == 77361.68
    bodies = [b for _, b, _ in fake.requests]
    assert n_requests == 2 and {"BTC", "BTCUSDT"} <= set(bodies[0]["filter"][0]["right"])
    assert bodies[1]["filter2"]["operator"] == "or" and "BINANCE" in bodies[1]["filter"][0]["right"]
    assert "RAYDIUMCLMM" not in bodies[1]["filter"][0]["right"]
    assert again == results and len(fake.requests) == n_requests  # served from the 300 s cache


def test_search_apple_falls_back_to_unfiltered_scan_and_drops_dex():
    fake = FakeScanner(table(REAL_APPLE, APPLE_COLS))
    results = run(client(fake), lambda tv: tv.search("apple", limit=10))
    symbols = [r["symbol"] for r in results]
    assert symbols[0] == "NASDAQ:AAPL" and "FINRA:AAPL6070268" in symbols
    assert not any("SWAP" in s for s in symbols)
    assert len(fake.requests) == 3  # names, filtered text search, then the unfiltered fallback


def test_search_exact_symbol_goes_first():
    rows = table(REAL_BTC, BTC_COLS)
    rows["BINANCE:ETHUSDT"] = {"name": "ETHUSDT", "description": "Ethereum / TetherUS", "type": "spot",
                               "exchange": "BINANCE", "currency": "USDT", "close": 3000.0}
    results = run(client(FakeScanner(rows)), lambda tv: tv.search("binance:ethusdt"))
    assert results and results[0]["symbol"] == "BINANCE:ETHUSDT"


# ----------------------------------------------------------------------------- quotes & details

QUOTE_ROWS = {
    "BINANCE:BTCUSDT": {"name": "BTCUSDT", "description": "Bitcoin / TetherUS", "type": "spot", "exchange": "BINANCE",
                        "currency": "USDT", "close": 77449.06, "change": -0.672, "change_abs": -524.1,
                        "volume": 21034.7, "high": 78804.01, "low": 76500.11, "open": 77991.89,
                        "update_mode": "streaming", "Recommend.All": 0.31, "Recommend.MA": 0.5, "RSI": 48.2,
                        "ADX": 22.0, "pricescale": 100},
    "NASDAQ:AAPL": {"name": "AAPL", "description": "Apple Inc.", "type": "stock", "exchange": "NASDAQ",
                    "currency": "USD", "close": 347.28, "change": -0.467, "update_mode": "delayed_streaming_900",
                    "_market": "america"},
    "TVC:DXY": {"name": "DXY", "description": "U.S. Dollar Currency Index", "type": "index", "exchange": "TVC",
                "currency": "USD", "close": 101.978},
}


def test_quotes_lookup_fallback_and_caches():
    fake = FakeScanner(QUOTE_ROWS, only={"TVC:DXY": {"cfd"}})
    tv = client(fake)

    async def go(tv):
        first = await tv.quotes(["binance:btcusdt", "TVC:DXY", "NOPE:XYZ", "SIM:NEXUS", "BINANCE:BTCUSDT"])
        n1 = len(fake.requests)
        second = await tv.quotes(["BINANCE:BTCUSDT", "TVC:DXY", "NOPE:XYZ"])
        return first, n1, second, len(fake.requests)

    first, n1, second, n2 = run(tv, go)
    assert set(first) == {"BINANCE:BTCUSDT", "TVC:DXY"}
    q = first["BINANCE:BTCUSDT"]
    # streaming data (update_mode) is announced with the optional `realtime` key (types-realtime.ts)
    assert set(q) == {"symbol", "price", "change_pct", "change", "volume", "high", "low", "open", "ts", "realtime"}
    assert q["realtime"] is True and "realtime" not in first["TVC:DXY"] and "delay_s" not in first["TVC:DXY"]
    assert (q["price"], q["change_pct"], q["change"], q["open"]) == (77449.06, -0.672, -524.1, 77991.89)
    assert first["TVC:DXY"]["price"] == 101.978 and first["TVC:DXY"]["high"] is None
    assert [m for m, _, _ in fake.requests] == ["global", "cfd"] and n1 == 2
    tickers = fake.requests[0][1]["symbols"]["tickers"]
    assert tickers == ["BINANCE:BTCUSDT", "TVC:DXY", "NOPE:XYZ"]  # SIM:* never sent, duplicates removed
    assert second == {k: v for k, v in first.items()} and n2 == n1  # 5 s cache + 'missing' memory


def test_symbol_details_with_technicals():
    fake = FakeScanner(QUOTE_ROWS)
    tv = client(fake)

    async def go(tv):
        return await tv.symbol_details("nasdaq:aapl"), await tv.symbol_details("BINANCE:BTCUSDT"), \
            await tv.symbol_details("SIM:NEXUS"), await tv.symbol_details("not a symbol")

    aapl, btc, sim, bad = run(tv, go)
    assert sim is None and bad is None
    assert aapl["symbol"] == "NASDAQ:AAPL" and aapl["market"] == "america" and aapl["history"] == "recorder"
    assert aapl["technicals"]["delay_s"] == 900.0 and aapl["quote"]["price"] == 347.28
    tech = btc["technicals"]
    assert (tech["rating"], tech["rating_ma"], tech["rsi"], tech["adx"]) == (0.31, 0.5, 48.2, 22.0)
    assert tech["rating_osc"] is None and tech["delay_s"] == 0.0
    assert btc["provider"] == "binance" and btc["pricescale"] == 100
    assert all(isinstance(v, float) or v is None for v in tech.values())


# ----------------------------------------------------------------------------- universe


def test_universe_crypto_filters_sort_and_rows():
    rows = table(REAL_BTC, BTC_COLS, **{"BINANCE:BTCUSDT": {"RSI|60": 55.5, "Recommend.All": 0.2, "ATR": 900.0}})
    fake = FakeScanner(rows)
    out = run(client(fake), lambda tv: tv.universe("crypto", offset=0, limit=50, sort="volume"))
    _, body, _ = fake.requests[0]
    assert body["sort"] == {"sortBy": "24h_vol|5", "sortOrder": "desc"} and body["range"] == [0, 50]
    venues = body["filter"][0]["right"]
    assert body["filter"][0]["left"] == "exchange" and "BINANCE" in venues and "RAYDIUMCLMM" not in venues
    symbols = [r["symbol"] for r in out["rows"]]
    # TradingView's order is kept; the inverse perpetual with an impossible 24h value (2e14 USD) is dropped
    assert symbols == ["BINANCE:BTCUSDT.P", "OKX:BTCUSDT.P", "BINANCE:BTCUSDT", "COINBASE:BTCUSD"]
    assert out["total"] == 5
    btc = out["rows"][symbols.index("BINANCE:BTCUSDT")]
    assert (btc["close"], btc["rating"], btc["atr"], btc["market"]) == (77361.68, 0.2, 900.0, "crypto")
    assert btc["RSI|60"] == 55.5 and "quick_score" not in btc  # raw scanner columns for the quick model
    public = P.public_universe_row(btc)
    assert list(public) == list(P.UNIVERSE_ROW_KEYS) and public["quick_score"] is None and "RSI|60" not in public


def test_universe_global_drops_dex_and_falls_back_on_rejected_sort():
    fake = FakeScanner(table(REAL_BTC, BTC_COLS), fail_sort="Recommend.All")
    tv = client(fake)

    async def go(tv):
        return await tv.universe("global", limit=20, sort="Recommend.All", order="asc", q="btc", type_="spot")

    out = run(tv, go)
    sorts = [b["sort"]["sortBy"] for _, b, _ in fake.requests]
    assert sorts == ["Recommend.All", "volume"]
    body = fake.requests[-1][1]
    assert body["filter"] == [{"left": "type", "operation": "equal", "right": "spot"}] and body["filter2"]
    assert [r["symbol"] for r in out["rows"]] == ["COINBASE:BTCUSD", "BINANCE:BTCUSDT"]  # asc; DEX pool removed
    with pytest.raises(DataProviderError):
        run(client(fake), lambda tv: tv.universe("bad market!"))


# ----------------------------------------------------------------------------- current bars (recorder)

# Real snapshot of BINANCE:BTCUSDT (per-timeframe current-bar columns); |60 high/low deliberately absent
FRESH = {"close": 77449.06, "close|1": 77384.06, "open|1": 77454.71, "high|1": 77475.48, "low|1": 77384.06,
         "volume|1": 4.00486, "close|5": 77384.06, "open|5": 77400.0, "high|5": 77480.0, "low|5": 77380.0,
         "volume|5": 20.1, "close|60": 77384.06, "open|60": 77352.68, "volume|60": 16.80727, "open": 77991.89,
         "high": 78804.01, "low": 76500.11, "volume": 21034.70525, "update_mode": "streaming"}


def test_current_bars_from_snapshot_columns():
    fake = FakeScanner({"BINANCE:BTCUSDT": dict(FRESH)})
    before = int(time.time())
    out = run(client(fake), lambda tv: tv.current_bars(["BINANCE:BTCUSDT", "SIM:NEXUS"], ["1m", "5m", "1h", "1d",
                                                                                          "2h", "bad"]))
    bars = out["BINANCE:BTCUSDT"]
    assert set(bars) == {"1m", "5m", "1d"}  # 1h lacks high/low; 2h has no current-bar columns
    m1 = bars["1m"]
    assert m1["time"] in (floor_time(before, "1m"), floor_time(before, "1m") + 60)
    assert (m1["open"], m1["high"], m1["low"], m1["close"], m1["volume"]) == (77454.71, 77475.48, 77384.06,
                                                                               77449.06, 4.00486)
    d1 = bars["1d"]
    assert d1["time"] == floor_time(before, "1d") or d1["time"] == floor_time(before + 5, "1d")
    assert (d1["open"], d1["high"], d1["low"], d1["close"]) == (77991.89, 78804.01, 76500.11, 77449.06)
    cols = fake.requests[0][1]["columns"]
    assert "open|1" in cols and "volume|60" in cols and not any(c.endswith("|120") for c in cols)
    assert fake.requests[0][1]["symbols"]["tickers"] == ["BINANCE:BTCUSDT"]
    assert P.CURRENT_BAR_TFS == ("1m", "5m", "15m", "1h", "4h", "1d", "1w")


# ----------------------------------------------------------------------------- market counts & limits


def test_market_counts_cached_and_crypto_counts_exclude_dex():
    rows = table(REAL_BTC, BTC_COLS)
    fake = FakeScanner(rows)
    tv = client(fake)

    async def go(tv):
        first = await tv.market_counts(wait_s=10)
        n = len(fake.requests)
        second = await tv.market_counts()
        return first, n, second

    first, n, second = run(tv, go)
    assert set(first) == {m for m, _ in __import__("app.data.symbols", fromlist=["MARKETS"]).MARKETS}
    cex = sum(1 for r in rows.values() if r["exchange"] in P.CEX_EXCHANGES)
    assert first["global"] == len(rows) and first["crypto"] == cex == 5  # DEX pools / non-CEX not counted
    assert n == len(first) and second == first and len(fake.requests) == n
    crypto = next(b for m, b, _ in fake.requests if m == "crypto")
    assert crypto["range"] == [0, 1] and crypto["filter"][0]["left"] == "exchange"


def test_market_counts_return_partial_results_while_refreshing():
    fake = FakeScanner(table(REAL_BTC, BTC_COLS))

    async def slow(req):
        await asyncio.sleep(0.03)
        return fake(req)

    tv = client(slow)

    async def go(tv):
        partial = await tv.market_counts(wait_s=0.0)
        for _ in range(100):
            if all(v is not None for v in tv.market_counts_cached().values()):
                break
            await asyncio.sleep(0.05)
        return partial, tv.market_counts_cached()

    partial, later = run(tv, go)
    assert set(partial) == set(later) and sum(v is None for v in partial.values()) >= len(partial) - 2
    assert all(v is not None for v in later.values())  # the refresh kept running in the background


def test_rate_limiter_spacing_and_429_retry():
    hits: list[float] = []
    fake = FakeScanner(table(REAL_BTC, BTC_COLS))

    def handler(req):
        hits.append(time.monotonic())
        if len(hits) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, text="slow down")
        return fake(req)

    tv = client(handler, min_interval_s=0.2)

    async def go(tv):
        await asyncio.gather(tv.scan("crypto", ["name"]), tv.scan("america", ["name"]))

    run(tv, go)
    assert len(hits) == 3
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    assert min(gaps) >= 0.15  # >= min interval between consecutive requests (timer tolerance)


@pytest.mark.network
def test_live_tradingview_search_and_quotes():
    async def go(tv):
        found = await tv.search("btc", limit=10)
        quotes = await tv.quotes(["BINANCE:BTCUSDT", "NASDAQ:AAPL"])
        return found, quotes

    found, quotes = run(TradingViewClient(), go)
    assert found and found[0]["symbol"].endswith("BTCUSDT") and not any("SWAP" in r["symbol"] for r in found)
    assert quotes["BINANCE:BTCUSDT"]["price"] > 0
