"""NEXUS automática trades crypto perpetuals (app.engine.contracts.PERPETUALS, `BINANCE:BTCUSDT.P`) like its index
futures: whole contracts sized by its risk per trade, a margin = notional / autopilot.perp_leverage inside its margin
budget, outside the cash exposure, a taker fee of a futures exchange, and no entry when the approximate liquidation
would come before the stop. Spot pairs and the user's own account are unchanged, except that autopilot.spot_short = false
makes a spot pair buy-only (a wallet)."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import config as nexus_config
from app.engine.contracts import PERP_FEE_PCT, futures_spec, liquidation_price, perpetual_spec
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_sim_fakes import FakeHub, make_current

PERP, SPOT = "BINANCE:BTCUSDT.P", "BINANCE:BTCUSDT"


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def _signal(action: str, points: float) -> dict:
    """NEXUS says `action` at 100 with the gate's stop `points` away (TOMAR both ways)."""
    cur = make_current(action, long_verdict="TAKE", short_verdict="TAKE")
    for g in cur["gate"].values():
        g["sl"] = 100.0 - points if g["direction"] == "long" else 100.0 + points
    return cur


def test_perpetual_specs_and_liquidation():
    spec = perpetual_spec(PERP)
    assert (spec["root"], spec["multiplier"], spec["perpetual"]) == ("BTCUSDT.P", 0.001, True)
    assert perpetual_spec("BYBIT:ETHUSDT.P")["name"] == "Perpetuo ETH/USDT"
    assert (perpetual_spec("BINANCE:SOLUSDT.P")["multiplier"], perpetual_spec("OKX:XRPUSDC.P")["name"]) == (
        1.0, "Perpetuo XRP/USDC")
    assert perpetual_spec("BTCUSDT.P") is not None  # without an exchange
    # gold: PAX Gold (1 token = 1 troy ounce, follows XAU/USD) in 0,001 PAXG contracts
    assert (perpetual_spec("BINANCE:PAXGUSDT.P")["multiplier"], perpetual_spec("BINANCE:PAXGUSDT.P")["name"]) == (
        0.001, "Perpetuo PAXG/USDT")
    for other in (SPOT, "BINANCE:PEPEUSDT.P", "BINANCE:BTCEUR.P", "CME:BTC1!", "CME_MINI:MES1!", ""):
        assert perpetual_spec(other) is None
    assert futures_spec(PERP) is None
    # 10x: the margin is 10 % of the notional, less 0,4 % of maintenance → about 9,6 % away from the entry
    assert liquidation_price(100.0, 1, 10) == pytest.approx(90.4)
    assert liquidation_price(100.0, -1, 10) == pytest.approx(109.6)
    assert liquidation_price(100.0, 1, 0.5) == pytest.approx(0.4)  # under 1x counts as 1x


def test_perpetual_in_contracts_with_leverage_margin_and_no_cash_cap(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0})
            rt.hub = FakeHub(prices={PERP: 100.0, SPOT: 100.0})
            rt.nexus = BarNexus(default=_signal("BUY", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            # 1 % of 1 000 = 10 $; 1 contract (0,001) at a 2-point stop risks 0,002 $ → 5 000 contracts = 5 units,
            # 500 $ of notional: over its 10 % cash exposure, but a perpetual is on margin (5 000 × 0,01 = 50 $)
            await engine.set_autopilot(enabled=True, source="list", symbols=[PERP], max_exposure_pct=10)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["instrument"] == {"kind": "future", "root": "BTCUSDT.P", "name": "Perpetuo BTC/USDT",
                                         "multiplier": 0.001, "contracts": 5000, "margin": 0.01, "perpetual": True,
                                         "leverage": 10.0, "liquidation": 90.4}
            assert pos["qty"] == pytest.approx(5.0)
            assert engine._open[pos["id"]]["meta"]["fees"] == pytest.approx(5.0 * 100.0 * PERP_FEE_PCT / 100.0)
            text = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "5000 contratos (5,000 BTC) con 10x · margen ~50,00 · liquidación ~90,4" in text
            engine._prices[PERP] = (101.0, time.monotonic())  # +1 × 5 units = +5 $
            assert (await engine.account(3))["unrealized_pnl"] == pytest.approx(5.0)
            await engine.close_autopilot(all=True)
            # the margin budget trims it: 1 % of the equity (≈ 1 004 $ after that trade) / 0,01 per contract
            await engine.set_autopilot(futures_margin_pct=1)
            equity = await engine.equity(3)
            t = engine._auto_bar[(3, PERP, "1h")]
            await engine._on_candle(PERP, "1h", {"time": t + 3600, "open": 100, "high": 101, "low": 99, "close": 100,
                                                 "volume": 1}, True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["instrument"]["contracts"] == int(equity * 0.01 / 0.01 + 1e-9) < 5000
            assert pos["qty"] == pytest.approx(pos["instrument"]["contracts"] * 0.001)
            await engine.close_autopilot(all=True)
            # the spot pair stays a cash position: no instrument, capped by its cash exposure
            await engine.set_autopilot(symbols=[SPOT], futures_margin_pct=50)
            t = engine._auto_bar.get((3, SPOT, "1h"), t + 3600)
            await engine._on_candle(SPOT, "1h", {"time": t + 3600, "open": 100, "high": 101, "low": 99, "close": 100,
                                                 "volume": 1}, True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["instrument"] is None and pos["qty"] * 100.0 <= 0.10 * 1010.0  # 10 % of its equity
            await engine.stop()

    asyncio.run(scenario())


def test_perpetual_short_and_a_leverage_whose_liquidation_beats_the_stop(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "autopilot.perp_leverage": 50})
            rt.hub = FakeHub(prices={PERP: 100.0})
            rt.nexus = BarNexus(default=_signal("SELL", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            # 50x: the liquidation of a short at 100 is ~101,6, before its stop at 102 → no entry
            await engine.set_autopilot(enabled=True, source="list", symbols=[PERP])
            await _settle(engine)
            assert engine.open_positions(3) == []
            text = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "Con apalancamiento 50x la liquidación (~101,60) llega antes que el stop (102,00)" in text
            # 20x: ~104,6, past the stop → the short opens, its liquidation above the entry
            await rt.settings.update({"autopilot.perp_leverage": 20})
            t = engine._auto_bar[(3, PERP, "1h")]
            await engine._on_candle(PERP, "1h", {"time": t + 3600, "open": 100, "high": 101, "low": 99, "close": 100,
                                                 "volume": 1}, True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["side"] == "short" and pos["instrument"]["leverage"] == 20.0
            assert pos["instrument"]["liquidation"] == pytest.approx(104.6)
            assert pos["instrument"]["margin"] == pytest.approx(0.005)
            await engine.stop()

    asyncio.run(scenario())


def test_the_users_account_keeps_plain_units_on_a_perpetual(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0})
            rt.hub = FakeHub(prices={PERP: 100.0})
            rt.nexus = BarNexus(default=None)
            engine = pp.PaperEngine(rt)
            await engine.start()
            res = await engine.open_position(1, PERP, "long", "1h", check_gate=False, sl=98.0)
            assert res["position"]["instrument"] is None
            # its fee is still a perpetual's taker fee
            pos = engine._open[res["position"]["id"]]
            assert pos["meta"]["fees"] == pytest.approx(pos["qty"] * 100.0 * PERP_FEE_PCT / 100.0)
            await engine.stop()

    asyncio.run(scenario())


def test_spot_crypto_without_shorts_buys_and_its_perpetual_still_sells(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "autopilot.spot_short": False})
            rt.hub = FakeHub(prices={PERP: 100.0, SPOT: 100.0})
            rt.nexus = BarNexus(default=_signal("SELL", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            # NEXUS says SELL on both: the spot pair (a wallet) refuses the short, the perpetual opens it
            await engine.set_autopilot(enabled=True, source="list", symbols=[SPOT, PERP])
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert (pos["symbol"], pos["side"]) == (PERP, "short")
            text = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "Corto descartado: al contado solo compra y luego vende lo que tiene" in text
            await engine.close_autopilot(all=True)
            # a BUY on the spot pair opens as usual
            rt.nexus.default = _signal("BUY", 2.0)
            await engine.set_autopilot(symbols=[SPOT])
            t = engine._auto_bar[(3, SPOT, "1h")]
            await engine._on_candle(SPOT, "1h", {"time": t + 3600, "open": 100, "high": 101, "low": 99, "close": 100,
                                                 "volume": 1}, True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert (pos["symbol"], pos["side"], pos["instrument"]) == (SPOT, "long", None)
            await engine.stop()

    asyncio.run(scenario())
