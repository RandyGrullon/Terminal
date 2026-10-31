"""DATA (data-rt): the hub with real-time keyed quotes: /api/quotes source priority, live bars of charts from the
provider (TradingView's delayed snapshots skipped), plan / limit errors falling back to TradingView, WS candle source
fields, the API endpoint and the terminal command. Fakes only (httpx MockTransport, fake TradingView)."""
from __future__ import annotations

import asyncio
import json

import httpx

from app.data import realtime as R
from tests.test_data_hub_fakes import Clock, FakeProviders, FakeTV, make_hub
from tests.test_data_realtime import AAPL, DAY, EUR, KEY, OPEN_NOW, keyed_hub, td_quote

TV_DELAYED = {AAPL: {"symbol": AAPL, "price": 100.2, "change_pct": 1.2, "change": 1.19, "ts": (OPEN_NOW - 900) * 1000,
                     "delay_s": 900},
              EUR: {"symbol": EUR, "price": 1.1, "change_pct": 0.1, "change": 0.0011, "ts": OPEN_NOW * 1000,
                    "realtime": True}}


def td_batch(prices: dict[str, float], at: int):
    def handler(request: httpx.Request) -> httpx.Response:
        wanted = request.url.params["symbol"].split(",")
        body = {w: td_quote(w, prices[w], at) for w in wanted if w in prices}
        return httpx.Response(200, json=body if len(wanted) > 1 else body.get(wanted[0], {}))
    return handler


def test_keyed_realtime_quote_beats_delayed_tradingview(runtime_factory, monkeypatch):
    seen: list = []
    tv = FakeTV(quotes=TV_DELAYED)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=Clock(OPEN_NOW)) as (rt, hub):
            # without a key: TradingView's quote keeps its announced delay (it used to be dropped by the hub)
            q = (await hub.quotes([AAPL]))[AAPL]
            assert q["delay_s"] == 900 and q["source"] == "tradingview" and "realtime" not in q
            assert (await hub.quotes([EUR]))[EUR]["realtime"] is True
            await keyed_hub(rt, hub, OPEN_NOW, td_batch({"AAPL": 101.5, "EUR/USD": 1.1012}, OPEN_NOW - 30), seen)
            hub.subscribe(AAPL, "1h", "ws:1")
            hub.subscribe(EUR, "1m", "ws:1")
            assert hub.realtime.select() == {AAPL: "twelvedata", EUR: "twelvedata"}
            [(provider, symbols)] = hub.realtime.due()
            got = await hub.realtime.poll(provider, symbols)
            assert set(got) == {AAPL, EUR}
            params = seen[-1].url.params
            assert sorted(params["symbol"].split(",")) == ["AAPL", "EUR/USD"] and params["interval"] == "1day"
            assert seen[-1].headers["authorization"] == f"apikey {KEY}" and KEY not in str(seen[-1].url)
            assert hub.realtime.budget("twelvedata").used_day(quotes_only=True) == 2  # 1 credit per symbol
            quotes = await hub.quotes([AAPL, EUR, "SIM:NEXUS"])
            q = quotes[AAPL]
            assert q["price"] == 101.5 and q["source"] == "twelvedata" and q["realtime"] is True
            assert q["ts"] == (OPEN_NOW - 30) * 1000 and "delay_s" not in q
            assert quotes[EUR]["source"] == "twelvedata"
            assert "source" not in quotes["SIM:NEXUS"]  # synthetic quotes keep their exact shape
            assert await hub.last_price(AAPL) == 101.5
            detail = await hub.symbol_detail(AAPL)
            assert detail["quote"]["source"] == "twelvedata"
            # the cached quote ages out (no poll for several intervals): TradingView again, with its delay
            hub.realtime.clock.t += 3600
            q = (await hub.quotes([AAPL]))[AAPL]
            assert q["source"] == "tradingview" and q["delay_s"] == 900

    asyncio.run(scenario())


def test_live_feed_of_a_chart_comes_from_the_provider_not_from_delayed_snapshots(runtime_factory, monkeypatch):
    clock = Clock(OPEN_NOW)
    bucket = floor_hour = (OPEN_NOW - 1800) // 3600 * 3600 + 1800  # provider's 1h bars at :30 (13:30 session)
    tv = FakeTV(quotes=TV_DELAYED, bars={AAPL: {"1h": {"time": OPEN_NOW // 3600 * 3600, "open": 99.0, "high": 99.5,
                                                       "low": 98.9, "close": 99.2, "volume": 5.0,
                                                       "asof": OPEN_NOW - 900}}})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=clock) as (rt, hub):
            await keyed_hub(rt, hub, OPEN_NOW, td_batch({"AAPL": 101.5}, OPEN_NOW - 20))
            await rt.db.execute(
                "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                "VALUES (?, '1h', ?, 100.0, 101.0, 99.8, 100.9, 3000.0, 'twelvedata')", (AAPL, bucket))
            hub.subscribe(AAPL, "1h", "ws:7")
            jobs = await hub._feed_tick()
            await asyncio.gather(*jobs)
            assert not [c for c in tv.calls if c[0] == "current_bars"]  # no delayed TradingView snapshot
            msgs = [m for topic, m in rt.ws.messages if topic == "market:NASDAQ:AAPL:1h"]
            assert msgs and msgs[-1]["source"] == "twelvedata" and msgs[-1]["realtime"] is True
            bar = msgs[-1]["candle"]
            assert bar["time"] == floor_hour and bar["open"] == 100.0 and bar["close"] == 101.5
            assert bar["high"] == 101.5 and bar["low"] == 99.8 and msgs[-1]["closed"] is False
            # the bucket ends before the next quote: the bar is emitted closed once (the provider's bar is refetched)
            clock.t = floor_hour + 3600 + 5
            hub.realtime.clock.t = clock.t
            hub.realtime._quotes[AAPL]["_at"] = clock.t - 10  # polled a moment ago (inside the old bucket)
            hub.realtime._next[AAPL] = clock.t + 30
            hub._keyed_fresh.set((AAPL, "1h", "twelvedata"), True)
            hub._next_poll.clear()
            jobs = await hub._feed_tick()
            await asyncio.gather(*jobs)
            closed = [m for topic, m in rt.ws.messages if topic == "market:NASDAQ:AAPL:1h" and m["closed"]]
            assert [m["candle"]["time"] for m in closed] == [floor_hour]
            assert hub._keyed_fresh.get((AAPL, "1h", "twelvedata")) is None

    asyncio.run(scenario())


def test_closed_market_quote_opens_no_live_bar(runtime_factory, monkeypatch):
    late = DAY + 22 * 3600  # 18:00 New York: closed

    def handler(request):
        return httpx.Response(200, json=td_quote("AAPL", 101.0, DAY + 20 * 3600 - 60, open_=False))

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=FakeTV(), clock=Clock(late)) as (rt, hub):
            await keyed_hub(rt, hub, late, handler)
            hub.subscribe(AAPL, "1m", "ws:1")
            await asyncio.gather(*(await hub._feed_tick()))
            assert not [m for t, m in rt.ws.messages if t.startswith("market:")]
            q = (await hub.quotes([AAPL]))[AAPL]
            assert q["market_open"] is False and q["source"] == "twelvedata" and "realtime" not in q
            assert R.freshness_label(q) == "Mercado cerrado · Twelve Data"
            nxt = hub.realtime._next[AAPL] - hub.realtime.clock()
            assert nxt >= R.CLOSED_POLL_S - 1  # a closed market is re-checked every 30 min only

    asyncio.run(scenario())


def test_limits_and_plans_fall_back_to_tradingview(runtime_factory, monkeypatch):
    tv = FakeTV(quotes=TV_DELAYED)
    answers = {"n": 0}

    def limited(request):
        answers["n"] += 1
        return httpx.Response(200, json={"code": 429, "status": "error",
                                         "message": "You have run out of API credits for the day."})

    def polygon_free(request):
        return httpx.Response(403, json={"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=Clock(OPEN_NOW)) as (rt, hub):
            await keyed_hub(rt, hub, OPEN_NOW, limited)
            hub.subscribe(AAPL, "1h", "ws:1")
            await asyncio.gather(*(await hub._feed_tick()))
            assert answers["n"] == 1 and hub.realtime.budget("twelvedata").blocked()
            assert hub.realtime.provider_state("twelvedata")[0] == "limite"
            assert not hub.realtime.serves(AAPL) and hub.realtime.due() == []
            q = (await hub.quotes([AAPL]))[AAPL]
            assert q["source"] == "tradingview" and q["delay_s"] == 900
            # Polygon's free plan has no snapshots: the market is marked and TradingView keeps serving
            await rt.settings.set("providers.twelvedata_key", "")
            await keyed_hub(rt, hub, OPEN_NOW, polygon_free, provider="polygon")
            assert hub.realtime.select() == {AAPL: "polygon"}
            [(provider, symbols)] = hub.realtime.due()
            assert await hub.realtime.poll(provider, symbols) == {}
            assert hub.realtime.select() == {}  # blocked for this market for 6 h
            state, detail = hub.realtime.provider_state("polygon")
            assert state in ("plan", "en_espera") and "no incluye" in str(hub.realtime._blocked.get(("polygon", "stocks")))
            assert (await hub.quotes([AAPL]))[AAPL]["source"] == "tradingview"

    asyncio.run(scenario())


def test_exchange_live_price_and_ws_source_fields(runtime_factory, monkeypatch):
    clock = Clock(OPEN_NOW)
    btc = "BINANCE:BTCUSDT"
    asof = OPEN_NOW - 900  # TradingView announces 15 min for NASDAQ: the snapshot describes the market back then
    tv = FakeTV(quotes={btc: {"symbol": btc, "price": 60000.0, "change": 600.0, "change_pct": 1.01, "ts": 1,
                              "realtime": True}},
                bars={AAPL: {"1m": {"time": asof // 60 * 60, "open": 99.0, "high": 99.5, "low": 98.9, "close": 99.2,
                                    "volume": 5.0, "asof": asof}}})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=FakeProviders(clock), tv=tv, clock=clock) as (rt, hub):
            hub.subscribe(btc, "1m", "ws:1")
            await hub._poll_key(btc, "1m")
            msg = [m for t, m in rt.ws.messages if t == "market:BINANCE:BTCUSDT:1m"][-1]
            assert msg["source"] == "binance" and msg["realtime"] is True and "delay_s" not in msg
            q = (await hub.quotes([btc]))[btc]
            assert q["source"] == "binance" and q["realtime"] is True and q["price"] == msg["candle"]["close"]
            assert abs(q["change"] - (q["price"] - 59400.0)) < 1e-6  # change kept against the previous close
            assert R.freshness_label(q) == "Tiempo real · Binance"
            # a recorder symbol fed by TradingView snapshots says how late its live bars are
            hub.subscribe(AAPL, "1m", "ws:1")
            await hub._poll_snapshots([(AAPL, "1m")])
            msg = [m for t, m in rt.ws.messages if t == "market:NASDAQ:AAPL:1m"][-1]
            assert msg["source"] == "tradingview" and msg["delay_s"] == 900 and msg["realtime"] is False
            assert msg["candle"]["time"] == asof // 60 * 60

    asyncio.run(scenario())


def test_realtime_endpoint_and_terminal_command(api_client):
    r = api_client.get("/api/realtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False and body["offline"] is True and body["symbols"] == []
    ids = [p["id"] for p in body["providers"]]
    assert ids == ["twelvedata", "polygon", "alphavantage"]
    td = body["providers"][0]
    assert td["configured"] is False and td["state"] == "sin_clave" and td["credits_min"] == 8 and td["credits_day"] == 800
    assert "Basic" in td["plan"] and "tiempo real" in td["plan"] and td["doc_url"].startswith("https://")
    assert body["providers"][2]["live"] is False and "25" in body["providers"][2]["plan"]
    json.dumps(body)
    res = api_client.post("/api/terminal", json={"text": "realtime"}).json()
    assert res["ok"] and any("Twelve Data" in line["text"] for line in res["lines"])
    res = api_client.post("/api/terminal", json={"text": "quote sim"}).json()
    assert res["ok"] and any("Sintético" in str(line) for line in res["lines"])
    settings = api_client.get("/api/settings").json()["settings"]
    assert settings["realtime.enabled"] is True and settings["realtime.twelvedata_credits_day"] == 800


def test_tradingview_quotes_outside_the_session_are_closed_market_prices(runtime_factory, monkeypatch):
    saturday = DAY + 2 * 86400 + 15 * 3600
    tv = FakeTV(quotes=TV_DELAYED)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=Clock(saturday)) as (_rt, hub):
            quotes = await hub.quotes([AAPL, EUR])
            assert quotes[AAPL]["market_open"] is False and quotes[EUR]["market_open"] is False
            assert R.freshness_label(quotes[AAPL]) == "Mercado cerrado · TradingView"
        async with make_hub(runtime_factory, monkeypatch, tv=tv, clock=Clock(OPEN_NOW)) as (_rt, hub):
            q = (await hub.quotes([AAPL]))[AAPL]
            assert "market_open" not in q and q["delay_s"] == 900  # in session: a guess never claims "open"

    asyncio.run(scenario())
