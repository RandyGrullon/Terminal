"""DATA (data-rt): real-time quotes of the API-key providers (app.data.realtime) and how the hub uses them: quote
source priority (exchange > keyed provider > TradingView with its delay > local cache), the live feed of charts and
demo positions, budget-aware cadence inside the free plans, plan / limit detection. Fakes only (httpx MockTransport):
no network, no real keys."""
from __future__ import annotations

import asyncio
import json

import httpx

from app.data import realtime as R
from app.util import floor_time, http_client
from tests.test_data_hub_fakes import T0, Clock, make_hub

AAPL = "NASDAQ:AAPL"
EUR = "FX:EURUSD"
KEY = "fake-test-key"
DAY = floor_time(T0, "1d")  # a Thursday
OPEN_NOW = DAY + 14 * 3600 + 600  # 14:10 UTC = 10:10 New York: US session open


class Wall:
    def __init__(self, t: float):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


def td_quote(symbol: str, close: float, at: int, open_: bool = True) -> dict:
    return {"symbol": symbol, "name": symbol, "exchange": "NASDAQ", "datetime": "2025-10-09", "timestamp": DAY,
            "last_quote_at": at, "open": "100.0", "high": "102.0", "low": "99.5", "close": str(close),
            "volume": "12000", "previous_close": "99.0", "change": str(round(close - 99.0, 4)),
            "percent_change": str(round((close - 99.0) / 99.0 * 100, 4)), "is_market_open": open_}


def client_for(handler, seen: list | None = None) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)
    return http_client(transport=httpx.MockTransport(wrapped))


async def keyed_hub(rt, hub, now: int, handler=None, seen=None, provider: str = "twelvedata"):
    await rt.settings.set(f"providers.{provider}_key", KEY)
    hub.realtime.clock = Wall(now)
    if handler is not None:
        client = client_for(handler, seen)
        hub._http = lambda: client
    return hub


# ----------------------------------------------------------------------------- pure parts


def test_freshness_judged_by_the_providers_own_timestamp():
    now = OPEN_NOW
    live = R.td_quote(AAPL, td_quote("AAPL", 101.5, now - 40), now)
    assert live["realtime"] is True and live["ts"] == (now - 40) * 1000 and "delay_s" not in live
    assert live["price"] == 101.5 and live["change_pct"] == round(2.5 / 99 * 100, 4) and live["source"] == "twelvedata"
    late = R.td_quote(AAPL, td_quote("AAPL", 101.5, now - 900), now)
    assert late["realtime"] is False and late["delay_s"] == 900  # measured, never called real time
    closed = R.td_quote(AAPL, td_quote("AAPL", 101.5, now - 7200, open_=False), now)
    assert closed["market_open"] is False and "realtime" not in closed and "delay_s" not in closed
    assert R.td_error({"code": 429, "message": "You have run out of API credits for the day.", "status": "error"})[0] == "day"
    assert R.td_error({"code": 429, "message": "You have run out of API credits for the current minute.",
                       "status": "error"})[0] == "rate_limit"
    assert R.td_error({"code": 401, "message": "**apikey** parameter is incorrect", "status": "error"})[0] == "auth"
    assert R.td_error({"code": 404, "message": "symbol not found", "status": "error"})[0] == "not_found"
    assert R.td_error(td_quote("AAPL", 1.0, now)) is None
    assert R.session_open("stocks", OPEN_NOW) is True and R.session_open("stocks", DAY + 21 * 3600) is False
    assert R.session_open("forex", DAY + 2 * 3600) is True and R.session_open("forex", DAY + 2 * 86400 + 3600) is False
    assert R.freshness_label({"source": "tradingview", "delay_s": 900, "ts": 0}) == "Retraso 15 min · TradingView"
    assert R.freshness_label({"source": "twelvedata", "realtime": True, "ts": 0}) == "Tiempo real · Twelve Data"


def test_cadence_spreads_the_free_daily_credits_over_the_session(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, clock=Clock(OPEN_NOW)) as (rt, hub):
            await keyed_hub(rt, hub, OPEN_NOW)
            rq = hub.realtime
            one = rq.interval_for("twelvedata", [AAPL])
            # Basic plan: 8 credits/min (2 kept for candles), 800/day (25 % kept): the 600 left are spread over the
            # 5 h 50 min of session still ahead -> one quote every ~35 s, never below realtime.min_interval_s
            left = 800 * 0.75
            assert abs(one - R.open_seconds_left("stocks", OPEN_NOW) / left) < 0.5 and 30 < one < 40
            assert abs(rq.interval_for("twelvedata", [AAPL, "NASDAQ:NVDA"]) - 2 * one) < 1.0
            rq.budget("twelvedata").spend(600, quotes=True)  # the quote share of the day is gone
            assert rq.interval_for("twelvedata", [AAPL]) is None
            await rt.settings.set("realtime.twelvedata_credits_day", 0)  # paid plan without a daily limit
            assert rq.interval_for("twelvedata", [AAPL]) == 15.0  # realtime.min_interval_s
            await rt.settings.set("realtime.twelvedata_credits_min", 3)
            assert rq.interval_for("twelvedata", [AAPL, "NASDAQ:NVDA", EUR]) is None  # 3 credits, 1 reserved

    asyncio.run(scenario())


def test_only_charts_and_demo_positions_spend_realtime_credits(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, clock=Clock(OPEN_NOW)) as (rt, hub):
            hub.subscribe(AAPL, "1h", "nexus")  # background watcher: TradingView is enough
            hub.subscribe("BINANCE:BTCUSDT", "1m", "ws:1")  # crypto: the exchange feed
            assert hub.realtime.select() == {}  # no key yet
            await keyed_hub(rt, hub, OPEN_NOW)
            assert hub.realtime.select() == {}
            hub.subscribe(AAPL, "1h", "ws:2")
            hub.subscribe(EUR, "1m", "paper")
            hub.subscribe("NASDAQ:NVDA", "15m", "ws:3")
            hub.subscribe("LSE:VOD", "1h", "ws:4")  # outside the free plan's real-time markets
            await rt.settings.set("realtime.max_symbols", 2)
            assert hub.realtime.select() == {EUR: "twelvedata", AAPL: "twelvedata"}  # demo positions first
            await rt.settings.set("realtime.enabled", False)
            assert hub.realtime.select() == {}

    asyncio.run(scenario())


# Real /quote answer captured once with Twelve Data's documented public demo key (Saturday: market closed), trimmed.
TD_AAPL_QUOTE = {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "mic_code": "XNGS", "currency": "USD",
                 "datetime": "2026-10-30", "timestamp": 1793367000, "last_quote_at": 1793390340, "open": "346.72000",
                 "high": "349.19000", "low": "334.35001", "close": "344.63000", "volume": "67797500",
                 "previous_close": "348.91000", "change": "-4.28000", "percent_change": "-1.22668",
                 "average_volume": "42129920", "is_market_open": False}


def test_real_twelvedata_quote_sample_single_and_batch(runtime_factory, monkeypatch):
    saturday = 1793390340 + 2 * 86400
    seen: list = []

    def handler(request):
        wanted = request.url.params["symbol"].split(",")
        if len(wanted) == 1:
            return httpx.Response(200, json=TD_AAPL_QUOTE)
        return httpx.Response(200, json={"AAPL": TD_AAPL_QUOTE,
                                         "EUR/USD": {"code": 404, "message": "symbol not found", "status": "error"}})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, clock=Clock(saturday)) as (rt, hub):
            await keyed_hub(rt, hub, saturday, handler, seen)
            got = await hub.realtime.poll("twelvedata", [AAPL])
            q = got[AAPL]
            assert (q["price"], q["change"], q["change_pct"], q["volume"]) == (344.63, -4.28, -1.22668, 67797500.0)
            assert q["ts"] == 1793390340 * 1000 and q["market_open"] is False and "realtime" not in q
            got = await hub.realtime.poll("twelvedata", [AAPL, EUR])
            assert set(got) == {AAPL}  # the failing symbol is set aside, the other one still counts
            assert hub.realtime.provider_for(EUR, "forex") is None and "no disponible" in str(
                hub.realtime._blocked.get(("twelvedata", EUR)))
            assert hub.realtime.provider_for(AAPL, "stock") == "twelvedata"
            assert json.loads(json.dumps(hub.realtime.status()))["polls"] == 2

    asyncio.run(scenario())


def test_rejected_key_backs_off_and_repeated_limits_wait_longer(runtime_factory, monkeypatch):
    calls = {"n": 0}

    def rejected(request):
        calls["n"] += 1
        return httpx.Response(401, json={"code": 401, "status": "error", "message": "**apikey** parameter is incorrect"})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, clock=Clock(OPEN_NOW)) as (rt, hub):
            await keyed_hub(rt, hub, OPEN_NOW, rejected)
            assert await hub.realtime.poll("twelvedata", [AAPL]) == {}
            state, detail = hub.realtime.provider_state("twelvedata")
            assert state == "error" and "clave" in detail.lower()  # backs off 10 min instead of retrying every poll
            hub.realtime.clock.t += 60
            assert hub.realtime._down_reason("twelvedata") is not None
            hub.realtime.clock.t += R.AUTH_BACKOFF_S
            assert hub.realtime._down_reason("twelvedata") is None
            # two "limit reached" answers in a row: the second one pauses 15 min (HTTP 429 hides minute vs day)
            rq = hub.realtime
            rq._fail("twelvedata", "rate_limit", "Twelve Data: límite", [AAPL])
            first = rq.budget("twelvedata").blocked_until - rq.clock()
            rq.clock.t += first + 1
            rq._fail("twelvedata", "rate_limit", "Twelve Data: límite", [AAPL])
            assert first <= 62 and rq.budget("twelvedata").blocked_until - rq.clock() >= R.RATE_LIMIT_LONG_S - 1
            assert calls["n"] == 1

    asyncio.run(scenario())
