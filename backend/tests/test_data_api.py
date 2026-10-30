"""DATA-HUB REST API + terminal commands through the full app (api_client: OFFLINE, no background loops)."""
from __future__ import annotations

from app import config as nexus_config

INFO_KEYS = {"symbol", "ticker", "exchange", "description", "type", "market", "currency", "pricescale", "logoid",
             "provider", "history"}
QUOTE_KEYS = {"symbol", "price", "change_pct", "change", "volume", "high", "low", "open", "ts"}
UNIVERSE_KEYS = INFO_KEYS | {"close", "change_pct", "volume", "rating", "rsi", "adx", "atr", "perf_w", "perf_1m",
                             "volatility_d", "market_cap", "quick_score", "quick_prob"}


def test_candles_sim_shape_and_validation(api_client):
    r = api_client.get("/api/candles", params={"symbol": "SIM:NEXUS", "tf": "1h", "limit": 300})
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data) == {"symbol", "tf", "provider", "history", "note", "candles"}
    assert data["symbol"] == "SIM:NEXUS" and data["tf"] == "1h" and data["history"] == "synthetic"
    assert len(data["candles"]) == 300
    assert set(data["candles"][0]) == {"time", "open", "high", "low", "close", "volume"}
    times = [c["time"] for c in data["candles"]]
    assert times == sorted(times) and all(b - a == 3600 for a, b in zip(times, times[1:]))
    # aliases and timeframe spellings are accepted
    alias = api_client.get("/api/candles", params={"symbol": "sim", "tf": "60", "limit": 10}).json()
    assert alias["symbol"] == "SIM:NEXUS" and alias["tf"] == "1h" and len(alias["candles"]) == 10

    bad_tf = api_client.get("/api/candles", params={"symbol": "SIM:NEXUS", "tf": "7x"})
    assert bad_tf.status_code == 400 and "Temporalidad" in bad_tf.json()["detail"]
    bad_sym = api_client.get("/api/candles", params={"symbol": "NO VALIDO:$$", "tf": "1h"})
    assert bad_sym.status_code == 400 and "Símbolo" in bad_sym.json()["detail"]
    missing = api_client.get("/api/candles", params={"tf": "1h"})
    assert missing.status_code == 400 and "Falta el símbolo" in missing.json()["detail"]
    bad_limit = api_client.get("/api/candles", params={"symbol": "SIM:NEXUS", "limit": "muchas"})
    assert bad_limit.status_code == 400 and "limit" in bad_limit.json()["detail"]


def test_candles_offline_without_cache_is_empty_with_note(api_client):
    r = api_client.get("/api/candles", params={"symbol": "BINANCE:BTCUSDT", "tf": "1h", "limit": 50})
    assert r.status_code == 200
    data = r.json()
    assert data["candles"] == [] and data["note"] and "sin conexión" in data["note"].lower()
    stocks = api_client.get("/api/candles", params={"symbol": "NASDAQ:AAPL", "tf": "1d"}).json()
    assert stocks["history"] == "recorder" and stocks["provider"] == "recorder" and stocks["candles"] == []


def test_watchlist_seed_add_remove(api_client):
    items = api_client.get("/api/watchlist").json()["items"]
    assert [i["symbol"] for i in items] == nexus_config.DEFAULT_WATCHLIST
    assert all(INFO_KEYS | {"quote"} <= set(i) for i in items)
    sim = next(i for i in items if i["symbol"] == "SIM:NEXUS")
    assert sim["history"] == "synthetic" and sim["quote"] is not None and set(sim["quote"]) == QUOTE_KEYS

    added = api_client.post("/api/watchlist", json={"symbol": "BINANCE:ADAUSDT"})
    assert added.status_code == 200
    symbols = [i["symbol"] for i in added.json()["items"]]
    assert symbols[-1] == "BINANCE:ADAUSDT" and len(symbols) == len(nexus_config.DEFAULT_WATCHLIST) + 1
    again = api_client.post("/api/watchlist", json={"symbol": "ada"}).json()["items"]  # alias, already there
    assert [i["symbol"] for i in again] == symbols

    removed = api_client.delete("/api/watchlist", params={"symbol": "BINANCE:ADAUSDT"})
    assert removed.status_code == 200
    assert "BINANCE:ADAUSDT" not in [i["symbol"] for i in removed.json()["items"]]
    assert api_client.post("/api/watchlist", json={}).status_code == 400
    assert api_client.post("/api/watchlist", json={"symbol": "BAD SYMBOL:%%"}).status_code == 400


def test_symbol_search_offline(api_client):
    r = api_client.get("/api/symbols/search", params={"q": "btc"})
    assert r.status_code == 200
    results = r.json()["results"]
    assert results and results[0]["symbol"] == "BINANCE:BTCUSDT"
    assert all(INFO_KEYS | {"price", "change_pct"} <= set(x) for x in results)
    assert results[0]["history"] == "full" and results[0]["provider"] == "binance"
    gold = api_client.get("/api/symbols/search", params={"q": "oro"}).json()["results"]
    assert gold[0]["symbol"] == "OANDA:XAUUSD"
    assert api_client.get("/api/symbols/search", params={"q": ""}).json() == {"results": []}
    bad = api_client.get("/api/symbols/search", params={"q": "btc", "market": "Mal Mercado!"})
    assert bad.status_code == 400 and "Mercado" in bad.json()["detail"]
    crypto_only = api_client.get("/api/symbols/search", params={"q": "aapl", "market": "crypto"}).json()["results"]
    assert all(x["symbol"] != "NASDAQ:AAPL" for x in crypto_only)


def test_markets_universe_quotes_and_symbol(api_client):
    markets = api_client.get("/api/markets").json()["markets"]
    by_id = {m["id"]: m for m in markets}
    assert by_id["crypto"]["name"] == "Cripto" and by_id["america"]["name"] == "EE. UU."
    assert by_id["mexico"]["name"] == "México" and all(m["count"] is None for m in markets)  # offline

    api_client.get("/api/candles", params={"symbol": "SIM:NEXUS", "tf": "1h", "limit": 10})
    uni = api_client.get("/api/universe", params={"market": "global", "limit": 50})
    assert uni.status_code == 200
    data = uni.json()
    assert set(data) == {"total", "rows"} and data["total"] >= len(data["rows"]) >= 1
    assert all(set(row) == UNIVERSE_KEYS for row in data["rows"])
    assert api_client.get("/api/universe", params={"market": "crypto", "order": "up"}).status_code == 400
    assert api_client.get("/api/universe", params={"market": "??"}).status_code == 400
    assert api_client.get("/api/universe", params={"sort": "volume; drop"}).status_code == 400

    quotes = api_client.get("/api/quotes", params={"symbols": "SIM:NEXUS,BINANCE:BTCUSDT"}).json()["quotes"]
    assert set(quotes) == {"SIM:NEXUS"} and quotes["SIM:NEXUS"]["price"] > 0
    assert api_client.get("/api/quotes", params={"symbols": ""}).status_code == 400

    detail = api_client.get("/api/symbol", params={"symbol": "SIM:NEXUS"})
    assert detail.status_code == 200
    body = detail.json()
    assert INFO_KEYS | {"quote", "technicals"} <= set(body)
    assert body["description"] == "Mercado sintético NEXUS" and body["technicals"] is None
    assert body["quote"]["symbol"] == "SIM:NEXUS"


def _run(api_client, text: str) -> dict:
    r = api_client.post("/api/terminal", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_market_terminal_commands(api_client):
    res = _run(api_client, "candles sim 1h --limit 5")
    assert res["ok"], res["lines"]
    assert len(res["data"]["candles"]) == 5 and res["data"]["history"] == "synthetic"
    assert any("Apertura" in line["text"] for line in res["lines"])

    res = _run(api_client, "chart btc 4h")
    assert res["ok"] and res["actions"] == [{"action": "open_chart", "symbol": "BINANCE:BTCUSDT", "tf": "4h"}]
    assert _run(api_client, "chart btc")["actions"] == [{"action": "open_chart", "symbol": "BINANCE:BTCUSDT"}]
    assert not _run(api_client, "chart btc 7x")["ok"]

    res = _run(api_client, "watch add eth sol")
    assert res["ok"] and [i["symbol"] for i in res["data"]].count("BINANCE:ETHUSDT") == 1
    res = _run(api_client, "watch rm sim")
    assert res["ok"] and "SIM:NEXUS" not in [i["symbol"] for i in res["data"]]
    assert not _run(api_client, "watch nada")["ok"]

    res = _run(api_client, "search btc")
    assert res["ok"] and res["data"][0]["symbol"] == "BINANCE:BTCUSDT"
    res = _run(api_client, "quote sim")
    assert res["ok"] and "SIM:NEXUS" in res["data"]
    res = _run(api_client, "markets")
    assert res["ok"] and len(res["data"]) >= 20
    res = _run(api_client, "providers")
    assert res["ok"] and res["data"]["offline"] is True
    res = _run(api_client, "recorder")
    assert res["ok"] and res["data"]["running"] is False
    assert not _run(api_client, "recorder now")["ok"]  # offline
    res = _run(api_client, "recorder off")
    assert res["ok"] and res["data"]["enabled"] is False
    res = _run(api_client, "universe global --limit 5")
    assert res["ok"]
    commands = api_client.get("/api/terminal/commands").json()["commands"]
    market_cmds = {c["name"] for c in commands if c["group"] == "Mercado"}
    assert {"search", "quote", "candles", "markets", "universe", "watch", "chart", "providers", "recorder"} <= market_cmds
