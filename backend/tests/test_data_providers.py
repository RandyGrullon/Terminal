"""Crypto exchange candle providers: real payload samples, pagination, resampling, errors, OFFLINE (no network)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import config
from app.data import providers as P
from app.data import providers_http as H
from app.util import floor_time, http_client

# Real answers captured once from this network (trimmed to 3 bars; 2026-10-30 13:00..15:00 UTC, BTC 1h)
BINANCE_1H = [
    [1793372400000, "77251.95", "78110.00", "77238.00", "77622.00", "1127.68014", 1793375999999, "87555993.32", 265927,
     "599.78", "46562221.32", "0"],
    [1793376000000, "77622.01", "77692.31", "77150.00", "77352.67", "561.30457", 1793379599999, "43484290.38", 222547,
     "261.88", "20290001.98", "0"],
    [1793379600000, "77352.68", "77373.68", "77352.67", "77361.68", "0.85236", 1793383199999, "65937.43", 463,
     "0.36", "27905.61", "0"],
]
OKX_1H = {"code": "0", "msg": "", "data": [
    ["1793379600000", "77316.6", "77338.6", "77312.6", "77312.6", "2.8406539", "219636.24", "219636.24", "0"],
    ["1793376000000", "77598.1", "77659.7", "77110.1", "77316.7", "501.93867051", "38862290.12", "38862290.12", "1"],
    ["1793372400000", "77212", "78088.4", "77197.8", "77593.7", "1141.35910178", "88574604.60", "88574604.60", "1"],
]}
COINBASE_1H = [  # [time, low, high, open, close, volume], newest first
    [1793379600, 77184.16, 77229.59, 77201.52, 77198.45, 1.53364987],
    [1793376000, 77001, 77574.61, 77459.47, 77202.35, 562.00321552],
    [1793372400, 77049.31, 77957.75, 77060.91, 77459.46, 777.34444109],
]
KRAKEN_1H = {"error": [], "result": {"XXBTZUSD": [
    [1793372400, "77089.7", "77946.8", "77073.2", "77479.4", "77431.2", "211.36170239", 13051],
    [1793376000, "77479.3", "77553.9", "77039.9", "77217.0", "77325.3", "181.20724316", 11970],
    [1793379600, "77217.0", "77219.4", "77210.7", "77214.9", "77211.8", "0.15005839", 56],
], "last": 1793376000}}
KUCOIN_1H = {"code": "200000", "data": [  # [time, open, CLOSE, HIGH, LOW, volume, turnover]
    ["1793379600", "77326.8", "77323.5", "77328.2", "77323.5", "0.97976815", "75762.40"],
    ["1793376000", "77578", "77326.9", "77653.3", "77118.1", "85.34965685", "6605454.01"],
    ["1793372400", "77217.6", "77589.4", "78054.9", "77193", "181.8171502", "14115745.69"],
]}
BAD = {
    "binance": (400, {"code": -1121, "msg": "Invalid symbol."}),
    "okx": (200, {"code": "51001", "data": [], "msg": "Instrument ID doesn't exist."}),
    "coinbase": (404, {"message": "NotFound"}),
    "kraken": (200, {"error": ["EQuery:Unknown asset pair"]}),
    "kucoin": (200, {"msg": "Unsupported trading pair.", "code": "400100"}),
}
T0 = 1793372400  # 13:00 UTC


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(H, "LIMITS", {k: (0.0, 4) for k in H.LIMITS})
    monkeypatch.setattr(config, "OFFLINE", False)


def run(coro):
    return asyncio.run(coro)


def mock(handler):
    return http_client(transport=httpx.MockTransport(handler))


async def _fetch(provider, native, tf, limit, handler, **kw):
    async with mock(handler) as c:
        return await P.fetch_candles(provider, native, tf, limit, client=c, **kw)


@pytest.mark.parametrize(
    "provider, native, payload, first, last",
    [
        ("binance", "BTCUSDT", BINANCE_1H, (77251.95, 78110.0, 77238.0, 77622.0, 1127.68014), 77361.68),
        ("okx", "BTC-USDT", OKX_1H, (77212.0, 78088.4, 77197.8, 77593.7, 1141.35910178), 77312.6),
        ("coinbase", "BTC-USD", COINBASE_1H, (77060.91, 77957.75, 77049.31, 77459.46, 777.34444109), 77198.45),
        ("kraken", "XBTUSD", KRAKEN_1H, (77089.7, 77946.8, 77073.2, 77479.4, 211.36170239), 77214.9),
        ("kucoin", "BTC-USDT", KUCOIN_1H, (77217.6, 78054.9, 77193.0, 77589.4, 181.8171502), 77323.5),
    ],
)
def test_real_samples_are_parsed(provider, native, payload, first, last):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=payload)

    rows = run(_fetch(provider, native, "1h", 3, handler))
    assert [r["time"] for r in rows] == [T0, T0 + 3600, T0 + 7200]
    c = rows[0]
    assert (c["open"], c["high"], c["low"], c["close"], c["volume"]) == pytest.approx(first)
    assert rows[-1]["close"] == pytest.approx(last)
    assert seen[0].headers["user-agent"].startswith("NexusTerminal/")
    assert "origin" not in seen[0].headers and "referer" not in seen[0].headers and "cookie" not in seen[0].headers


@pytest.mark.parametrize("provider", list(BAD))
def test_unknown_pair_is_not_found(provider):
    status, body = BAD[provider]
    with pytest.raises(P.DataProviderError) as info:
        run(_fetch(provider, "NOPEUSDT", "1h", 10, lambda req: httpx.Response(status, json=body)))
    assert info.value.kind == "not_found" and "par no encontrado" in str(info.value)
    assert info.value.provider == provider


def _binance_generator(calls: list, first_bar: int):
    def handler(req):
        q = dict(req.url.params)
        calls.append(q)
        n = int(q["limit"])
        end = int(q.get("endTime", (T0 + 7200) * 1000)) // 1000
        end -= end % 3600
        times = [t for t in range(end - (n - 1) * 3600, end + 1, 3600) if t >= first_bar]
        return httpx.Response(200, json=[[t * 1000, "1", "2", "0.5", "1.5", "3", 0, "0", 0, "0", "0", "0"]
                                         for t in times])
    return handler


def test_binance_pages_backwards_until_listing():
    calls: list = []
    first_bar = T0 + 7200 - 2499 * 3600  # exactly 2500 bars of history
    rows = run(_fetch("binance", "BTCUSDT", "1h", 2400, _binance_generator(calls, first_bar)))
    times = [r["time"] for r in rows]
    assert len(rows) == 2400 and times[-1] == T0 + 7200
    assert all(b - a == 3600 for a, b in zip(times, times[1:]))
    assert [int(c["limit"]) for c in calls] == [1000, 1000, 400]
    assert int(calls[1]["endTime"]) == (times[-1000] - 1) * 1000  # strictly older than the first page
    calls.clear()
    rows = run(_fetch("binance", "BTCUSDT", "1h", 5000, _binance_generator(calls, first_bar)))
    assert len(rows) == 2500 and rows[0]["time"] == first_bar  # stops at the listing date


def test_end_time_is_inclusive():
    calls: list = []
    rows = run(_fetch("binance", "BTCUSDT", "1h", 5, _binance_generator(calls, 0), end_time=T0))
    assert rows[-1]["time"] == T0 and len(rows) == 5
    assert int(calls[0]["endTime"]) == T0 * 1000


def test_okx_recent_then_history_pages():
    urls: list[str] = []

    def handler(req):
        urls.append(req.url.path)
        q = dict(req.url.params)
        after = int(q.get("after", (T0 + 7201) * 1000)) // 1000  # strictly older than `after`
        n = int(q["limit"])
        times = [t for t in range(after - 1 - (after - 1) % 3600, 0, -3600)][:n]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": [
            [str(t * 1000), "1", "2", "0.5", "1.5", "4", "0", "0", "1"] for t in times]})

    rows = run(_fetch("okx", "BTCUSDT", "1h", 450, handler))
    assert len(rows) == 450 and rows[-1]["time"] == T0 + 7200
    assert all(b["time"] - a["time"] == 3600 for a, b in zip(rows, rows[1:]))
    assert urls == ["/api/v5/market/candles", "/api/v5/market/history-candles", "/api/v5/market/history-candles"]


def _window_handler(calls: list, provider: str, step: int):
    """Coinbase (start/end, newest first) or KuCoin (startAt/endAt) windows over an endless 1h/1d history."""
    def handler(req):
        q = dict(req.url.params)
        calls.append(q)
        if provider == "coinbase":
            start, end = int(q["start"]), int(q["end"])
            times = list(range(end - end % step, start - 1, -step))
            return httpx.Response(200, json=[[t, 0.5, 2.0 + (t // step) % 5, 1.0, 1.5, 2.0] for t in times])
        start, end = int(q["startAt"]), int(q["endAt"])
        times = [t for t in range(end - end % step, start - 1, -step) if t < end and t <= T0 + 7260]
        return httpx.Response(200, json={"code": "200000", "data": [
            [str(t), "1", "1.5", "2", "0.5", "2", "3"] for t in times]})
    return handler


def test_coinbase_builds_4h_from_1h(monkeypatch):
    monkeypatch.setattr("app.data.providers_cex.now_s", lambda: T0 + 7200 + 600)
    calls: list = []
    rows = run(_fetch("coinbase", "BTC-USD", "4h", 10, _window_handler(calls, "coinbase", 3600)))
    assert len(rows) == 10 and all(floor_time(r["time"], "4h") == r["time"] for r in rows)
    assert all(c["granularity"] == "3600" for c in calls)
    full = rows[-2]  # a complete 4h bucket = 4 hourly bars
    assert full["volume"] == pytest.approx(8.0) and full["open"] == 1.0 and full["close"] == 1.5
    assert P.plan("coinbase", "4h") == ("1h", 4) and P.plan("coinbase", "1h") == ("1h", 1)


def test_kucoin_window_paging_and_column_order(monkeypatch):
    monkeypatch.setattr("app.data.providers_cex.now_s", lambda: T0 + 7200 + 60)
    calls: list = []
    rows = run(_fetch("kucoin", "BTC-USDT", "1h", 2000, _window_handler(calls, "kucoin", 3600)))
    assert len(rows) == 2000 and rows[-1]["time"] == T0 + 7200
    assert all(b["time"] - a["time"] == 3600 for a, b in zip(rows, rows[1:]))
    assert len(calls) == 2 and calls[0]["type"] == "1hour"
    assert (rows[0]["high"], rows[0]["low"], rows[0]["close"]) == (2.0, 0.5, 1.5)


def test_kraken_weekly_bars_start_on_monday():
    def handler(req):
        assert req.url.params["interval"] == "1440"
        times = [T0 - T0 % 86400 - i * 86400 for i in range(719, -1, -1)]
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": [
            [t, "1", "2", "0.5", "1.5", "1.2", "10", 5] for t in times], "last": times[-1]}})

    rows = run(_fetch("kraken", "BTCUSD", "1w", 500, handler))
    assert 100 <= len(rows) <= P.history_limit("kraken", "1w") == 103
    assert all(floor_time(r["time"], "1w") == r["time"] for r in rows)
    assert rows[0]["volume"] == pytest.approx(70.0)  # first (incomplete) week dropped: 7 days x 10


def test_fetch_latest_returns_two_bars(monkeypatch):
    monkeypatch.setattr("app.data.providers_cex.now_s", lambda: T0 + 7200 + 600)
    calls: list = []
    rows = run(_fetch_latest("binance", "BTCUSDT", "1h", _binance_generator(calls, 0)))
    assert [r["time"] for r in rows] == [T0 + 3600, T0 + 7200] and calls[0]["limit"] == "2"
    calls.clear()
    rows = run(_fetch_latest("coinbase", "BTC-USD", "4h", _window_handler(calls, "coinbase", 3600)))
    assert len(rows) == 2 and all(floor_time(r["time"], "4h") == r["time"] for r in rows)


async def _fetch_latest(provider, native, tf, handler):
    async with mock(handler) as c:
        return await P.fetch_latest(provider, native, tf, client=c)


def test_rate_limit_is_retried_then_reported():
    hits = {"n": 0}

    def flaky(req):
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={"code": -1003})
        return httpx.Response(200, json=BINANCE_1H)

    assert len(run(_fetch("binance", "BTCUSDT", "1h", 3, flaky))) == 3 and hits["n"] == 2
    with pytest.raises(P.DataProviderError) as info:
        run(_fetch("binance", "BTCUSDT", "1h", 3,
                   lambda req: httpx.Response(429, headers={"Retry-After": "0.01"}, json={})))
    assert info.value.kind == "rate_limit" and "límite de peticiones" in str(info.value)


def test_server_geo_block_and_network_errors():
    with pytest.raises(P.DataProviderError) as info:
        run(_fetch("binance", "BTCUSDT", "1h", 3, lambda req: httpx.Response(451, json={})))
    assert info.value.kind == "unsupported" and "451" in str(info.value)
    calls = {"n": 0}

    def down(req):
        calls["n"] += 1
        return httpx.Response(503, text="busy")

    with pytest.raises(P.DataProviderError) as info:
        run(_fetch("okx", "BTC-USDT", "1h", 3, down))
    assert info.value.kind == "server" and calls["n"] == 2

    def broken(req):
        raise httpx.ConnectError("boom", request=req)

    with pytest.raises(P.DataProviderError) as info:
        run(_fetch("kucoin", "BTC-USDT", "1h", 3, broken))
    assert info.value.kind == "network" and "error de red" in str(info.value)


def test_offline_never_touches_the_network(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", True)
    touched = []

    def handler(req):
        touched.append(req)
        return httpx.Response(200, json=BINANCE_1H)

    for coro in (_fetch("binance", "BTCUSDT", "1h", 3, handler), _fetch_latest("okx", "BTC-USDT", "1h", handler)):
        with pytest.raises(P.DataProviderError, match="Modo sin conexión") as info:
            run(coro)
        assert info.value.kind == "offline"
    assert touched == []


def test_fetch_venues_falls_back_to_next_exchange():
    from app.data.symbols import route_symbol

    def handler(req):
        if "binance" in req.url.host:
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        return httpx.Response(200, json=OKX_1H)

    async def go():
        async with mock(handler) as c:
            return await P.fetch_venues(route_symbol("BYBIT:BTCUSDT").venues, "1h", 3, client=c)

    rows, venue = run(go())
    assert venue.provider == "okx" and len(rows) == 3


def test_inputs_validation_and_helpers():
    assert P.supports("okx", "12h") and P.supports("kraken", "3m") and not P.supports("bybit", "1h")
    assert not P.supports("binance", "7m")
    assert P.plan("kraken", "6h") == ("1h", 6) and P.plan("kucoin", "1w") == ("1d", 7)
    assert P.history_limit("binance", "1h") is None and P.history_limit("kraken", "1h") == 720
    assert P.history_limit("kraken", "6h") == 120
    assert P.to_native("okx", "BINANCE:ETHUSDT") == "ETH-USDT"
    assert P.to_native("kraken", "BTC/USD") == "XBTUSD" and P.to_native("binance", "btc-usdt") == "BTCUSDT"
    assert P.native_symbol("coinbase", "BTC", "USD") == "BTC-USD"
    with pytest.raises(P.DataProviderError) as info:
        run(_fetch("bybit", "BTCUSDT", "1h", 3, lambda req: httpx.Response(200, json=[])))
    assert info.value.kind == "unsupported"
    with pytest.raises(P.DataProviderError, match="Temporalidad no válida"):
        run(_fetch("binance", "BTCUSDT", "7m", 3, lambda req: httpx.Response(200, json=[])))


@pytest.mark.network
def test_live_binance_and_okx():
    async def go():
        async with http_client() as c:
            a = await P.fetch_candles("binance", "BTCUSDT", "1h", 5, client=c)
            b = await P.fetch_latest("okx", "BTC-USDT", "4h", client=c)
            return a, b

    a, b = run(go())
    assert len(a) == 5 and len(b) == 2 and a[-1]["close"] > 0
