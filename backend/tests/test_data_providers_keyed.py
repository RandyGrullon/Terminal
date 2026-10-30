"""Keyed providers (Twelve Data, Alpha Vantage, Polygon): real samples, time zones, paging, errors (offline)."""
from __future__ import annotations

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import config
from app.data import keyed as K
from app.data import providers_http as H
from app.util import floor_time, http_client

KEY = "secret-key-123"

# Real answers captured once (trimmed). Twelve Data with timezone=UTC; Alpha Vantage intraday in US/Eastern.
TD_AAPL_1H = {"meta": {"symbol": "AAPL", "interval": "1h", "currency": "USD", "exchange": "NASDAQ"}, "status": "ok",
              "values": [
                  {"datetime": "2026-10-30 16:30:00", "open": "347.495", "high": "347.955", "low": "346.84",
                   "close": "346.96", "volume": "143294"},
                  {"datetime": "2026-10-30 15:30:00", "open": "347.685", "high": "347.86", "low": "345.69",
                   "close": "347.53", "volume": "211727"},
                  {"datetime": "2026-10-30 14:30:00", "open": "346.3", "high": "349.19", "low": "346.21",
                   "close": "347.69", "volume": "233554"}]}
TD_EURUSD_1D = {"meta": {"symbol": "EUR/USD", "interval": "1day"}, "status": "ok", "values": [
    {"datetime": "2026-10-30", "open": "1.11622", "high": "1.12149", "low": "1.11366", "close": "1.1157"},
    {"datetime": "2026-10-29", "open": "1.11556", "high": "1.1174", "low": "1.11068", "close": "1.11621"}]}
AV_IBM_5M = {
    "Meta Data": {"1. Information": "Intraday (5min) open, high, low, close prices and volume", "2. Symbol": "IBM",
                  "4. Interval": "5min", "6. Time Zone": "US/Eastern"},
    "Time Series (5min)": {
        "2026-10-29 19:55:00": {"1. open": "255.0000", "2. high": "256.4900", "3. low": "254.4000",
                                "4. close": "255.0100", "5. volume": "621"},
        "2026-10-29 19:50:00": {"1. open": "255.0000", "2. high": "256.5886", "3. low": "254.9569",
                                "4. close": "255.0100", "5. volume": "787"}}}
AV_EURUSD_1D = {"Meta Data": {"2. From Symbol": "EUR", "6. Time Zone": "UTC"}, "Time Series FX (Daily)": {
    "2026-10-29": {"1. open": "1.11520", "2. high": "1.11850", "3. low": "1.11030", "4. close": "1.11780"},
    "2026-10-28": {"1. open": "1.11260", "2. high": "1.11690", "3. low": "1.11110", "4. close": "1.11520"}}}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(H, "LIMITS", {k: (0.0, 4) for k in H.LIMITS})
    monkeypatch.setattr(config, "OFFLINE", False)


def fetch(provider, symbol, aclass, tf, limit, handler, key=KEY):
    async def go():
        async with http_client(transport=httpx.MockTransport(handler)) as c:
            return await K.fetch_keyed_candles(provider, symbol, aclass, tf, limit, api_key=key, client=c)
    return asyncio.run(go())


def utc(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp())


# ----------------------------------------------------------------------------- Twelve Data


def test_twelvedata_sample_and_key_in_header():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=TD_AAPL_1H)

    rows = fetch("twelvedata", "NASDAQ:AAPL", "stock", "1h", 3, handler)
    assert [r["time"] for r in rows] == [utc("2026-10-30 14:30"), utc("2026-10-30 15:30"), utc("2026-10-30 16:30")]
    assert rows[0]["open"] == 346.3 and rows[-1]["volume"] == 143294.0
    q = dict(seen[0].url.params)
    assert q["symbol"] == "AAPL" and q["interval"] == "1h" and q["timezone"] == "UTC" and q["outputsize"] == "3"
    assert seen[0].headers["authorization"] == f"apikey {KEY}" and KEY not in str(seen[0].url)
    fx = fetch("twelvedata", "FX:EURUSD", "forex", "1d", 5, lambda req: httpx.Response(200, json=TD_EURUSD_1D))
    assert [r["time"] for r in fx] == [utc("2026-10-29"), utc("2026-10-30")] and fx[0]["volume"] == 0.0


@pytest.mark.parametrize(
    "payload, kind, text",
    [
        ({"code": 401, "message": "**apikey** parameter is incorrect or not specified.", "status": "error"},
         "auth", "clave API inválida"),
        ({"code": 429, "message": "You have run out of API credits for the current minute.", "status": "error"},
         "rate_limit", "límite de créditos"),
        ({"code": 400, "message": "**symbol** not found: ZZZZ.", "status": "error"}, "not_found", "no disponible"),
        ({"code": 403, "message": "This symbol is available starting with the Grow plan.", "status": "error"},
         "premium", "plan de pago"),
    ],
)
def test_twelvedata_errors(payload, kind, text):
    with pytest.raises(K.DataProviderError) as info:
        fetch("twelvedata", "NASDAQ:ZZZZ", "stock", "1h", 10, lambda req: httpx.Response(200, json=payload))
    assert info.value.kind == kind and text in str(info.value) and KEY not in str(info.value)
    assert str(info.value).startswith("Twelve Data:")


def test_twelvedata_pages_and_resamples_6h():
    calls: list[dict] = []
    end = utc("2026-10-30 22:00")

    def handler(req):
        q = dict(req.url.params)
        calls.append(q)
        top = utc(q["end_date"]) if "end_date" in q else end
        top -= top % 7200
        n = int(q["outputsize"])
        stamps = [dt.datetime.fromtimestamp(top - i * 7200, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                  for i in range(n)]
        return httpx.Response(200, json={"status": "ok", "values": [
            {"datetime": s, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"} for s in stamps]})

    rows = fetch("twelvedata", "NASDAQ:AAPL", "stock", "6h", 2000, handler)
    assert len(rows) == 2000 and all(floor_time(r["time"], "6h") == r["time"] for r in rows)
    assert len(calls) == 2 and calls[0]["interval"] == "2h" and calls[0]["outputsize"] == "5000"
    assert "end_date" in calls[1] and rows[-2]["volume"] == 30.0


# ----------------------------------------------------------------------------- Alpha Vantage


def test_alphavantage_intraday_is_converted_from_new_york_time():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=AV_IBM_5M)

    rows = fetch("alphavantage", "NYSE:IBM", "stock", "5m", 2, handler)
    assert [r["time"] for r in rows] == [utc("2026-10-29 23:50"), utc("2026-10-29 23:55")]  # EDT = UTC-4
    q = dict(seen[0].url.params)
    assert q["function"] == "TIME_SERIES_INTRADAY" and q["interval"] == "5min" and q["outputsize"] == "compact"
    fx = fetch("alphavantage", "FX:EURUSD", "forex", "1d", 2, lambda req: httpx.Response(200, json=AV_EURUSD_1D))
    assert [r["time"] for r in fx] == [utc("2026-10-28"), utc("2026-10-29")]


def test_alphavantage_weekly_bars_are_dated_on_monday():
    weekly = {"Meta Data": {"6. Time Zone": "US/Eastern"}, "Weekly Time Series": {
        "2026-10-30": {"1. open": "250", "2. high": "260", "3. low": "245", "4. close": "255", "5. volume": "1000"},
        "2026-10-23": {"1. open": "240", "2. high": "251", "3. low": "238", "4. close": "250", "5. volume": "900"}}}
    rows = fetch("alphavantage", "NYSE:IBM", "stock", "1w", 2, lambda req: httpx.Response(200, json=weekly))
    assert [r["time"] for r in rows] == [utc("2026-10-19"), utc("2026-10-26")]
    assert all(floor_time(r["time"], "1w") == r["time"] for r in rows)


def test_alphavantage_premium_full_falls_back_to_compact():
    sizes: list[str] = []

    def handler(req):
        sizes.append(req.url.params["outputsize"])
        if req.url.params["outputsize"] == "full":
            return httpx.Response(200, json={"Information": "Thank you for using Alpha Vantage! This is a premium "
                                                            "endpoint and there are multiple ways to unlock it."})
        return httpx.Response(200, json=AV_EURUSD_1D)

    rows = fetch("alphavantage", "FX:EURUSD", "forex", "1d", 500, handler)
    assert sizes == ["full", "compact"] and len(rows) == 2


@pytest.mark.parametrize(
    "payload, kind",
    [
        ({"Information": "Our standard API rate limit is 25 requests per day."}, "rate_limit"),
        ({"Note": "Thank you! Our standard API call frequency is 5 calls per minute."}, "rate_limit"),
        ({"Error Message": "the parameter apikey is invalid or missing."}, "auth"),
        ({"Error Message": "Invalid API call. Please retry or visit the documentation."}, "not_found"),
    ],
)
def test_alphavantage_errors(payload, kind):
    with pytest.raises(K.DataProviderError) as info:
        fetch("alphavantage", "NYSE:IBM", "stock", "1d", 10, lambda req: httpx.Response(200, json=payload))
    assert info.value.kind == kind and KEY not in str(info.value)


def test_us_eastern_fallback_matches_tz_database():
    ny = ZoneInfo("America/New_York")
    for text in ("2026-03-08 01:59", "2026-03-08 03:00", "2026-07-01 12:00", "2026-11-01 00:30",
                 "2026-11-01 02:00", "2026-12-24 18:00"):
        naive = dt.datetime.fromisoformat(text)
        expected = int(naive.replace(tzinfo=ny).utcoffset().total_seconds())
        assert K._us_eastern_offset(naive) == expected, text
    assert K.parse_time("2026-10-29 19:55:00", "US/Eastern") == utc("2026-10-29 23:55")
    assert K.parse_time("bad date") is None


# ----------------------------------------------------------------------------- Polygon


def test_polygon_follows_next_url_with_bearer_key():
    seen: list[httpx.Request] = []
    t1 = utc("2026-10-30 15:00") * 1000

    def handler(req):
        seen.append(req)
        if "cursor" in req.url.params:
            res = [{"t": t1 - 3 * 3600_000, "o": 1, "h": 2, "l": 0.5, "c": 1.1, "v": 7},
                   {"t": t1 - 2 * 3600_000, "o": 1, "h": 2, "l": 0.5, "c": 1.2, "v": 8}]
            return httpx.Response(200, json={"status": "OK", "results": res[::-1], "resultsCount": 2})
        res = [{"t": t1 - 3600_000, "o": 1, "h": 2, "l": 0.5, "c": 1.3, "v": 9},
               {"t": t1, "o": 1, "h": 2, "l": 0.5, "c": 1.4, "v": 10}]
        nxt = "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/hour/1/2?cursor=abc"
        return httpx.Response(200, json={"status": "DELAYED", "results": res[::-1], "resultsCount": 2,
                                         "next_url": nxt})

    rows = fetch("polygon", "NASDAQ:AAPL", "stock", "1h", 4, handler)
    assert [r["close"] for r in rows] == [1.1, 1.2, 1.3, 1.4] and rows[-1]["time"] == t1 // 1000
    assert all(r.headers["authorization"] == f"Bearer {KEY}" and KEY not in str(r.url) for r in seen)
    assert "/v2/aggs/ticker/AAPL/range/1/hour/" in seen[0].url.path and seen[0].url.params["sort"] == "desc"


@pytest.mark.parametrize(
    "status, payload, kind",
    [
        (401, {"status": "ERROR", "error": "Unknown API Key"}, "auth"),
        (403, {"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."}, "premium"),
        (200, {"status": "ERROR", "error": "You've exceeded the maximum requests per minute."}, "rate_limit"),
    ],
)
def test_polygon_errors(status, payload, kind):
    with pytest.raises(K.DataProviderError) as info:
        fetch("polygon", "FX:EURUSD", "forex", "1h", 10, lambda req: httpx.Response(status, json=payload))
    assert info.value.kind == kind and KEY not in str(info.value)


def test_polygon_empty_result_is_empty_list():
    rows = fetch("polygon", "NASDAQ:ZZZZ", "stock", "1d", 10,
                 lambda req: httpx.Response(200, json={"status": "OK", "resultsCount": 0}))
    assert rows == []


# ----------------------------------------------------------------------------- dispatcher


def test_dispatcher_guards_without_network(monkeypatch):
    touched: list = []

    def handler(req):
        touched.append(req)
        return httpx.Response(200, json=TD_AAPL_1H)

    cases = [
        (("twelvedata", "NASDAQ:AAPL", "stock", "1h", 10, handler, ""), "auth"),
        (("twelvedata", "BINANCE:BTCUSDT", "crypto", "1h", 10, handler), "unsupported"),
        (("polygon", "BMV:AMXL", "stock", "1h", 10, handler), "unsupported"),
        (("bloomberg", "NASDAQ:AAPL", "stock", "1h", 10, handler), "unsupported"),
        (("twelvedata", "NASDAQ:AAPL", "stock", "7m", 10, handler), "invalid"),
    ]
    for args, kind in cases:
        with pytest.raises(K.DataProviderError) as info:
            fetch(*args)
        assert info.value.kind == kind, args
    monkeypatch.setattr(config, "OFFLINE", True)
    with pytest.raises(K.DataProviderError, match="Modo sin conexión"):
        fetch("twelvedata", "NASDAQ:AAPL", "stock", "1h", 10, handler)
    assert touched == []
    assert K.keyed_supports("twelvedata", "NASDAQ:AAPL") and K.keyed_supports("polygon", "FX:EURUSD", "forex")
    assert not K.keyed_supports("alphavantage", "BINANCE:BTCUSDT") and not K.keyed_supports("nope", "NASDAQ:AAPL")
    assert K.KEYED_PROVIDERS == ("twelvedata", "alphavantage", "polygon")
