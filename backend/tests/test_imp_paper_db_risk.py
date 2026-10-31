"""paper-db · portfolio risk of the demo account (improvement 10).

Total open risk cap, positions per correlated group (crypto as one group by default) and the daily loss limit of the
UTC day, which blocks new orders and pending fills (manual, auto-trading) until 00:00 UTC; closing is never blocked.
The daily P&L query runs on PostgreSQL too with NEXUS_TEST_PG_URL.
"""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from tests.test_sim_fakes import FakeHub, FakeNexus, make_current

BTC = "BINANCE:BTCUSDT"
ETH = "BINANCE:ETHUSDT"
SOL = "BINANCE:SOLUSDT"
BTC_USDC = "BINANCE:BTCUSDC"
AAPL = "NASDAQ:AAPL"
EUR = "FX:EURUSD"
PRICES = {BTC: 100.0, ETH: 200.0, SOL: 50.0, BTC_USDC: 100.0, AAPL: 100.0, EUR: 100.0}
DAY = 86400
T0 = 1_800_000_000 - 1_800_000_000 % DAY + 12 * 3600  # 12:00 UTC of a fixed day


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)
    monkeypatch.setattr(pp, "now_s", lambda: now[0])
    monkeypatch.setattr(pp, "now_ms", lambda: now[0] * 1000)
    return now


async def _engine(rt, **settings) -> pp.PaperEngine:
    await rt.settings.update({"risk.fee_pct": 0.1, "risk.slippage_pct": 0.0, **settings})
    rt.hub = FakeHub(prices=dict(PRICES))
    rt.nexus = FakeNexus("TAKE")
    engine = pp.PaperEngine(rt)
    rt.paper = engine
    await engine.start()
    return engine


async def _settle(engine: pp.PaperEngine) -> None:
    while engine._tasks:
        await asyncio.gather(*list(engine._tasks))


def test_correlated_groups():
    assert {pp.risk_group(s) for s in (BTC, ETH, "COINBASE:SOLUSD", BTC_USDC)} == {"crypto"}
    assert pp.risk_group(BTC, False) == pp.risk_group(BTC_USDC, False) == pp.risk_group("KRAKEN:XBTUSD", False) == "crypto:BTC"
    assert pp.risk_group(ETH, False) == "crypto:ETH"
    assert pp.risk_group(AAPL) == "stock" and pp.risk_group(EUR) == "forex"
    assert pp.risk_group("OANDA:XAUUSD") == "commodity" and pp.risk_group("SIM:NEXUS") == "sim"
    assert pp.risk_group_label("crypto") == "Cripto" and pp.risk_group_label("crypto:BTC") == "Cripto BTC"
    assert pp.risk_group_label("forex") == "Forex"


def test_open_risk_cap_blocks_new_orders_until_a_stop_protects_the_entry(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, **{"paper.max_open_risk_pct": 2.5})
            a = (await engine.open_position(1, BTC, "long", "1h"))["position"]  # 1 % of equity at risk
            await engine.open_position(1, AAPL, "long", "1h")
            state = await engine.risk_state(1)
            assert state["open_risk"] == pytest.approx(200.0, rel=1e-3) and state["max_open_risk_pct"] == 2.5
            assert state["open_risk_pct"] == pytest.approx(2.0, rel=1e-2)
            with pytest.raises(pp.PaperRiskBlocked) as blk:
                await engine.open_position(1, EUR, "long", "1h", force=True)  # force never bypasses a risk rule
            assert blk.value.rule == "open_risk"
            assert blk.value.message.startswith("Riesgo abierto máximo superado: con esta orden el riesgo total sería")
            assert "Como mucho puedes arriesgar 0,5" in blk.value.message
            assert len(engine.open_positions(1)) == 2
            res = await engine.open_position(1, EUR, "long", "1h", risk_pct=0.4)  # fits in the room left
            assert res["position"]["symbol"] == EUR
            await engine.close_position(res["position"]["id"])
            # TP1 moves the stop to break-even: that position no longer risks anything
            await engine._on_candle(BTC, "1m", {"time": T0 + 60, "open": 100, "high": 102.3, "low": 99.9,
                                                "close": 102.1, "volume": 1}, False)
            assert engine._open[a["id"]]["tp_hits"] == 1
            assert (await engine.risk_state(1))["open_risk"] == pytest.approx(100.0, rel=1e-3)
            assert (await engine.open_position(1, EUR, "long", "1h"))["position"]["symbol"] == EUR
            await engine.stop()

    asyncio.run(scenario())


def test_group_limit_counts_crypto_as_one_correlated_group(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, **{"paper.max_positions_per_group": 2})
            await engine.open_position(1, BTC, "long", "1h")
            await engine.open_position(1, ETH, "short", "1h")
            with pytest.raises(pp.PaperRiskBlocked) as blk:
                await engine.open_position(1, SOL, "long", "1h")
            assert blk.value.rule == "group"
            assert blk.value.message.startswith("Límite de posiciones por grupo correlacionado: ya hay 2 abiertas en "
                                                "Cripto (máximo 2): las criptomonedas suelen moverse juntas")
            assert rt.nexus.gate_calls[-1][0] == ETH  # refused before asking the gate
            assert (await engine.open_position(1, AAPL, "long", "1h"))["position"]["symbol"] == AAPL  # its own class
            groups = {g["group"]: g for g in (await engine.risk_state(1))["groups"]}
            assert groups["crypto"]["positions"] == 2 and groups["crypto"]["full"] and groups["crypto"]["max"] == 2
            assert groups["crypto"]["symbols"] == [BTC, ETH] and groups["stock"]["positions"] == 1
            # crypto per coin: SOL is its own group; a second BTC pair fills the BTC group
            await rt.settings.update({"paper.crypto_one_group": False})
            assert (await engine.open_position(1, SOL, "long", "1h"))["position"]["symbol"] == SOL
            await engine.open_position(1, BTC_USDC, "long", "1h")
            with pytest.raises(pp.PaperRiskBlocked, match="Cripto BTC"):
                await engine.open_position(1, BTC, "long", "1h")
            # pending orders: refused at placement when the group is full
            with pytest.raises(pp.PaperRiskBlocked, match="Cripto BTC"):
                await engine.place_order(1, BTC, "long", "limit", 95.0, "1h")
            # the simulator account is not subject to the demo account's limits
            await engine.open_position(2, BTC, "long", "1h", origin="sim", price=100.0, check_gate=False)
            await engine.stop()

    asyncio.run(scenario())


async def _lose(engine: pp.PaperEngine, symbol: str = BTC, exit_price: float = 96.5) -> dict:
    """A trade of 100 units at 100 closed at `exit_price`: a realized loss of about 3.5 % of 10 000."""
    pos = (await engine.open_position(1, symbol, "long", "1h", qty=100.0, sl=95.0))["position"]
    return (await engine.close_position(pos["id"], price=exit_price))[0]


def test_daily_loss_limit_blocks_until_the_next_utc_day(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, **{"paper.daily_loss_limit_pct": 3.0, "paper.max_open_risk_pct": 50.0})
            win = (await engine.open_position(1, ETH, "long", "1h", qty=10.0, sl=190.0))["position"]
            waiting = (await engine.place_order(1, SOL, "long", "limit", 48.0, "1h"))["order"]
            lost = await _lose(engine)
            assert lost["pnl"] < -300
            state = await engine.risk_state(1)
            assert state["blocked"] and state["blocked_until"] == T0 - T0 % DAY + DAY
            assert state["daily_pnl"] == pytest.approx(lost["pnl"] - 10 * 200 * 0.001, abs=0.01)  # + ETH's entry fee
            assert state["day_start_balance"] == pytest.approx(10000.0, abs=0.01)
            assert state["daily_loss_limit"] == pytest.approx(300.0, abs=0.01)
            assert "hasta las 00:00 UTC" in state["block_reason"].replace("Hasta", "hasta")
            warns = [e["message"] for e in rt.bus.recent(level="warn") if "pérdida diaria" in e["message"]]
            assert len(warns) == 1 and warns[0].startswith("Cuenta demo: límite de pérdida diaria alcanzado")
            # new market and pending orders are refused, even forced; closing is still possible
            with pytest.raises(pp.PaperRiskBlocked) as blk:
                await engine.open_position(1, AAPL, "long", "1h", force=True)
            assert blk.value.rule == "daily_loss" and "00:00 UTC" in blk.value.message
            with pytest.raises(pp.PaperRiskBlocked):
                await engine.place_order(1, AAPL, "long", "limit", 95.0, "1h")
            # a pending order that triggers while blocked is cancelled with the reason (never filled)
            clock[0] = T0 + 120
            await engine._on_candle(SOL, "1m", {"time": T0 + 60, "open": 49, "high": 49.2, "low": 47.9, "close": 48.1,
                                                "volume": 1}, False)
            await _settle(engine)
            row = await rt.db.fetchone("SELECT status, cancelled_by, reason FROM paper_orders WHERE id=?", (waiting["id"],))
            assert (row["status"], row["cancelled_by"]) == ("cancelled", "risk")
            assert row["reason"].startswith("No se ejecutó al llegar a 48,00: Límite de pérdida diaria alcanzado")
            # a later winner the same day does not lift the block (latched on the day's worst realized P&L)
            await engine.close_position(win["id"], price=215.0)
            state = await engine.risk_state(1)
            assert state["daily_pnl"] > -300 and state["daily_low"] < -300 and state["blocked"]
            # ... nor does a restart (the day's low is persisted)
            await engine.stop()
            engine = pp.PaperEngine(rt)
            rt.paper = engine
            await engine.start()
            assert (await engine.risk_state(1))["blocked"]
            # the auto-trader is blocked too, with one notice (not one per bar)
            await engine.set_auto(True, [AAPL], "1h")
            rt.nexus.current_value = make_current("BUY", long_verdict="TAKE")
            for k in range(2):
                await engine._on_candle(AAPL, "1h", {"time": T0 - 3600 + k * 3600, "open": 100, "high": 101,
                                                     "low": 99, "close": 100, "volume": 1}, True)
                await _settle(engine)
            assert engine.open_positions(1) == []
            notes = [e["message"] for e in rt.bus.recent(level="info") if e["message"].startswith("Auto:")]
            assert len(notes) == 1 and "pérdida diaria" in notes[0]
            # next UTC day: trading again
            clock[0] = T0 - T0 % DAY + DAY + 60
            state = await engine.risk_state(1)
            assert not state["blocked"] and state["daily_pnl"] == 0 and state["block_reason"] is None
            assert (await engine.open_position(1, AAPL, "long", "1h"))["position"]["symbol"] == AAPL
            await engine.stop()

    asyncio.run(scenario())


def test_raising_or_disabling_the_daily_limit_is_the_users_call(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, **{"paper.daily_loss_limit_pct": 3.0})
            await _lose(engine)
            assert (await engine.risk_state(1))["blocked"]
            await rt.settings.update({"paper.daily_loss_limit_pct": 5.0})  # the day's low is above −5 %
            assert not (await engine.risk_state(1))["blocked"]
            await rt.settings.update({"paper.daily_loss_limit_pct": 3.0})
            assert (await engine.risk_state(1))["blocked"]
            await rt.settings.update({"paper.daily_loss_limit_pct": 0, "paper.max_open_risk_pct": 0,
                                      "paper.max_positions_per_group": 0})
            state = await engine.risk_state(1)
            assert not state["blocked"] and state["daily_loss_limit_pct"] is None
            assert state["max_open_risk_pct"] is None and state["max_positions_per_group"] is None
            for sym in (BTC, ETH, SOL, BTC_USDC):
                await engine.open_position(1, sym, "long", "1h", risk_pct=3.0)  # 12 % risk, 4 crypto: no limits
            assert len(engine.open_positions(1)) == 4
            await engine.stop()

    asyncio.run(scenario())


def test_daily_realized_pnl_counts_the_utc_day_only(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            day = T0 - T0 % DAY
            # opened yesterday (entry fee yesterday), closed today: only the gross result and the exit fee count today
            old = (await engine.open_position(1, BTC, "long", "1h", qty=10.0, sl=95.0, time_s=day - 600))["position"]
            await engine.close_position(old["id"], price=101.0)
            assert await engine._day_realized(1, day) == pytest.approx(10 * 1.0 - 10 * 101.0 * 0.001)
            assert await engine._day_realized(1, day - DAY) == pytest.approx(-10 * 100.0 * 0.001)
            # a partial close today counts its part; the open remainder does not count until it closes
            pos = (await engine.open_position(1, ETH, "short", "1h", qty=3.0, sl=210.0))["position"]
            await engine._on_candle(ETH, "1m", {"time": T0, "open": 200, "high": 200.5, "low": 189.0, "close": 190.0,
                                                "volume": 1}, False)
            p = engine._open[pos["id"]]
            assert p["tp_hits"] == 1
            tp1 = p["tp1"]
            expected = (10 * 1.0 - 10 * 101.0 * 0.001) - 3 * 200 * 0.001 + (200 - tp1) * 1.0 - tp1 * 1.0 * 0.001
            assert await engine._day_realized(1, day) == pytest.approx(expected)
            state = await engine.risk_state(1)
            assert state["daily_pnl"] == pytest.approx(expected, abs=0.01)
            assert state["unrealized_pnl"] != 0  # shown, but not part of the daily limit
            await engine.stop()

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- REST API


@pytest.fixture
def client(api_client):
    from app.runtime import rt

    rt.hub = FakeHub(prices=dict(PRICES))
    rt.nexus = FakeNexus("TAKE")
    return api_client


def test_risk_endpoint_and_409_risk_limit(client):
    r = client.get("/api/paper/risk")
    assert r.status_code == 200
    risk = r.json()
    assert risk["blocked"] is False and risk["open_risk"] == 0 and risk["groups"] == []
    assert risk["max_open_risk_pct"] == 6.0 and risk["max_positions_per_group"] == 3
    assert risk["daily_loss_limit_pct"] == 3.0 and risk["crypto_one_group"] is True
    assert "Cuenta demo" in risk["note"]
    assert client.put("/api/settings", json={"paper.max_positions_per_group": 1}).status_code == 200
    assert client.post("/api/paper/order", json={"symbol": BTC, "side": "long"}).status_code == 200
    r = client.post("/api/paper/order", json={"symbol": ETH, "side": "long", "force": True})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "risk_limit" and detail["rule"] == "group" and "Cripto" in detail["message"]
    assert detail["risk"]["groups"][0]["full"] is True and "gate" not in detail
    r = client.post("/api/paper/orders", json={"symbol": ETH, "side": "long", "kind": "limit", "price": 190})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "risk_limit"
    assert client.get("/api/paper/risk").json()["open_positions"] == 1
