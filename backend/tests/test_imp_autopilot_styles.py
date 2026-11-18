"""NEXUS automática on several timeframes at once (autopilot.tfs: quick, measured and long-term trades): one trader per
timeframe on the same candidates, the fastest as its main one (reads, health), positions tagged with the timeframe
that opened them, still one position per market, `tf` alone back to one timeframe, at most three, and the style of each
timeframe (trade_style)."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_sim_fakes import FakeHub, make_current

BTC, ETH = "BINANCE:BTCUSDT.P", "BINANCE:ETHUSDT.P"


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def _buy() -> dict:
    cur = make_current("BUY")
    for g in cur["gate"].values():
        g["sl"] = 98.0 if g["direction"] == "long" else 102.0
    return cur


def test_styles_of_the_timeframes():
    assert [pp.trade_style(tf) for tf in ("1m", "5m", "15m", "1h", "2h", "4h", "1d")] == [
        "rápida", "rápida", "medida", "medida", "medida", "largo plazo", "largo plazo"]


def test_three_timeframes_one_trader_each_and_one_position_per_market(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0,
                                      "paper.max_positions_per_group": 0})
            rt.hub = FakeHub(prices={BTC: 100.0, ETH: 100.0})
            rt.nexus = BarNexus(default=make_current("WAIT"))
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.set_autopilot(enabled=True, source="list", symbols=[BTC, ETH], tfs=["4h", "5m", "1h"])
            await _settle(engine)
            st = engine.autopilot_status()
            assert (st["tf"], st["tfs"]) == ("5m", ["5m", "1h", "4h"])  # fastest first: the main one
            assert st["health"]["text"] == "Funciona: decide al cierre de cada vela de 5m · 1h · 4h."
            assert [p.tf for p in engine._autopilot_entry_profiles()] == ["5m", "1h", "4h"]
            assert rt.settings.get("autopilot.tf") == "5m"
            # every timeframe decided the last closed bar of each candidate once
            assert {(s, tf) for (_, s, tf) in engine._auto_bar} == {(sym, tf) for sym in (BTC, ETH)
                                                                   for tf in ("5m", "1h", "4h")}
            # a 1h entry on BTC: the position carries 1h and the log says which style opened it
            rt.nexus.default = _buy()
            t = engine._auto_bar[(3, BTC, "1h")]
            await engine._on_candle(BTC, "1h", {"time": t + 3600, "open": 100, "high": 101, "low": 99, "close": 100,
                                                "volume": 1}, True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert (pos["symbol"], pos["tf"]) == (BTC, "1h")
            assert any(e["text"].startswith("Abre largo en 1h (medida) a ") for e in engine.autopilot_status()["log"])
            # a 5m entry on the same market: still one position per market
            t5 = engine._auto_bar[(3, BTC, "5m")]
            await engine._on_candle(BTC, "5m", {"time": t5 + 300, "open": 100, "high": 101, "low": 99, "close": 100,
                                                "volume": 1}, True)
            await _settle(engine)
            assert len(engine.open_positions(3)) == 1
            # a 4h entry on ETH opens there
            t4 = engine._auto_bar[(3, ETH, "4h")]
            await engine._on_candle(ETH, "4h", {"time": t4 + 14400, "open": 100, "high": 101, "low": 99, "close": 100,
                                                "volume": 1}, True)
            await _settle(engine)
            assert sorted((p["symbol"], p["tf"]) for p in engine.open_positions(3)) == [(BTC, "1h"), (ETH, "4h")]
            # health: a market held on another timeframe gets no reads on the main one, and is not "stale" for that
            old = {"bar_time": 1_000_000, "tf": "5m", "action": "WAIT", "label": "ESPERAR"}
            engine._autopilot_last[BTC] = dict(old)
            engine._autopilot_last[ETH] = dict(old)
            engine._autopilot_last["BINANCE:SOLUSDT.P"] = dict(old)
            engine._autopilot_watch["BINANCE:SOLUSDT.P"] = {"why": "lista", "direction": None}
            health = engine._autopilot_health(engine._autopilot_cfg(), now=10_000_000)
            assert health["stale"] == ["BINANCE:SOLUSDT.P"], health
            # back to one timeframe: the other positions keep their exits on their own bars (exits-only traders)
            await engine.set_autopilot(tf="15m")
            await _settle(engine)
            st = engine.autopilot_status()
            assert (st["tf"], st["tfs"]) == ("15m", ["15m"]) and rt.settings.get("autopilot.tfs") == []
            profiles = {(p.tf, p.entries) for p in engine._autopilot_profiles()}
            assert profiles == {("15m", True), ("1h", False), ("4h", False)}
            await engine.stop()

    asyncio.run(scenario())


def test_bad_timeframe_lists_are_refused(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = pp.PaperEngine(rt)
            await engine.start()
            with pytest.raises(pp.PaperError, match="Como mucho 3 temporalidades"):
                await engine.set_autopilot(tfs=["1m", "5m", "1h", "4h"])
            with pytest.raises(pp.PaperError):
                await engine.set_autopilot(tfs=["7x"])
            with pytest.raises(pp.PaperError, match="usa una lista"):
                await engine.set_autopilot(tfs="5m")
            # one in the list is one timeframe, not a list
            await engine.set_autopilot(tfs=["4h"])
            assert (engine.autopilot_status()["tfs"], rt.settings.get("autopilot.tfs")) == (["4h"], [])
            await engine.stop()

    asyncio.run(scenario())


def test_api_takes_the_timeframes(api_client):
    r = api_client.post("/api/paper/autopilot", json={"tfs": ["1h", "5m", "4h"]})
    assert r.status_code == 200 and r.json()["autopilot"]["tfs"] == ["5m", "1h", "4h"]
    r = api_client.post("/api/paper/autopilot", json={"tfs": ["1m", "5m", "1h", "4h"]})
    assert r.status_code == 400 and "Como mucho 3" in r.json()["detail"]


def test_terminal_takes_several_timeframes(api_client):
    res = api_client.post("/api/terminal", json={"text": "autopilot --tf 4h,5m,1h"}).json()
    assert res["ok"], res
    assert api_client.get("/api/paper/autopilot").json()["autopilot"]["tfs"] == ["5m", "1h", "4h"]
    res = api_client.post("/api/terminal", json={"text": "autopilot --tf 15m"}).json()
    assert res["ok"] and api_client.get("/api/paper/autopilot").json()["autopilot"]["tfs"] == ["15m"]
