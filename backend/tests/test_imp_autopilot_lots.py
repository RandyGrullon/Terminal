"""NEXUS automática in MT5 lots (autopilot.lots, app.engine.lots): its size rounded down to steps of 0.01 lot (gold
100 oz, crypto 1 coin, stocks 1 share, forex 100 000), the minimum lot only when it risks at most
autopilot.lot_max_risk_pct of its equity at the stop, and every position showing its lots."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.engine import lots as L
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

GOLD, SPOT = "BINANCE:PAXGUSDT.P", "BINANCE:ETHUSDT"


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_lot_specs_steps_and_rounding():
    assert L.lot_spec(GOLD) == {"size": 100.0, "unit": "oz"}  # PAX Gold: 1 token = 1 oz
    assert L.lot_spec("OANDA:XAUUSD") == {"size": 100.0, "unit": "oz"}
    assert L.lot_spec("OANDA:XAGUSD") == {"size": 5000.0, "unit": "oz"}
    assert L.lot_spec("OANDA:EURUSD") == {"size": 100000.0, "unit": "EUR"}
    assert L.lot_spec("BINANCE:BTCUSDT.P") == {"size": 1.0, "unit": "BTC"}
    assert L.lot_spec("NASDAQ:AAPL") == {"size": 1.0, "unit": "acción"}
    # index futures and options trade whole contracts: no lot
    assert L.lot_spec("CME_MINI:MES1!") is None
    assert L.lot_spec("OPT:NASDAQ:AAPL:C:230:2026-12-18", {"kind": "option"}) is None
    assert L.lot_spec("NASDAQ:AAPL", {"kind": "option"}) is None
    assert L.lot_info(GOLD, 3.0) == {"lots": 0.03, "size": 100.0, "unit": "oz"}
    assert L.lot_info("BINANCE:BTCUSDT", "x") is None
    # 0.01 lot of gold = 1 oz = 1 000 contracts of 0.001; of DOGE (1 coin per contract) a whole contract
    assert L.step_units(100.0, 0.001) == pytest.approx(1.0)
    assert L.step_units(1.0, 0.001) == pytest.approx(0.01)
    assert L.step_units(1.0, 1.0) == pytest.approx(1.0)
    # rounded down to whole steps, without binary noise
    assert L.fit_steps(413.4567, 0.01, 0.002, 5.0) == 413.45
    assert L.fit_steps(3.339, 0.01, 1.0, 5.0) == 3.33
    # under one step: the minimum lot only when it risks at most the cap
    assert L.fit_steps(0.4, 1.0, 2.0, 2.0) == 1.0
    assert L.fit_steps(0.4, 1.0, 2.0, 1.99) is None
    assert L.fit_steps(0.0, 1.0, 2.0, 50.0) is None
    assert L.unit_word("acción", 2.0) == "acciones" and L.unit_word("acción", 1.0) == "acción"
    assert L.unit_word("oz", 3.0) == "oz"
    assert pp.fmt_lots(0.01) == "0,01 lotes" and pp.fmt_lots(1) == "1,00 lote" and pp.fmt_lots(0.0164) == "0,0164 lotes"


def test_autopilot_in_lots_gold_perpetual_and_spot(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0})
            rt.hub = FakeHub(prices={GOLD: 100.0, SPOT: 100.0})
            rt.nexus = BarNexus(default=_signal("BUY", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            # 1 % of 1 000 = 10 $ at a 2-point stop → 5 oz = 0,05 lot (5 000 contracts of 0,001 PAXG)
            snap = await engine.set_autopilot(enabled=True, source="list", symbols=[GOLD], lots=True)
            assert snap["autopilot"]["lots"] is True and snap["autopilot"]["lot_max_risk_pct"] == 5.0
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["qty"] == pytest.approx(5.0) and pos["instrument"]["contracts"] == 5000
            assert pos["lot"] == {"lots": 0.05, "size": 100.0, "unit": "oz"}
            text = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "0,05 lotes (5,000 PAXG) con 10x" in text
            await engine.close_autopilot(all=True)
            # a spot pair: 10 $ / 3 points = 3,333 ETH → 3,33 lots (1 lot = 1 ETH)
            rt.nexus = BarNexus(default=_signal("BUY", 3.0))
            await engine.set_autopilot(symbols=[SPOT])
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["qty"] == pytest.approx(3.33) and pos["lot"] == {"lots": 3.33, "size": 1.0, "unit": "ETH"}
            text = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "3,33 lotes (3,3300 ETH)" in text
            await engine.stop()

    asyncio.run(scenario())


def test_minimum_lot_and_its_cap(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0})
            rt.hub = FakeHub(prices={GOLD: 100.0})
            engine = pp.PaperEngine(rt)
            await engine.start()

            async def gold(cap: float) -> dict:
                # 0,1 % of 1 000 = 1 $ at an 8-point stop → 0,125 oz, under the minimum lot (1 oz, 8 $ = 0,8 %)
                res = await engine.open_position(3, GOLD, "long", "1h", sl=92.0, risk_pct=0.1, check_gate=False,
                                                 contracts=True, margin_pct=50.0, lots_max_risk_pct=cap)
                return res["position"]

            with pytest.raises(pp.PaperRiskBlocked) as blk:
                await gold(0.5)
            assert blk.value.rule == "lots"
            assert ("El lote mínimo de PAXGUSDT.P (0,01 lotes = 1,0000 oz) arriesga 8,00 con este stop, el 0,8 % de "
                    "la cuenta: más que su tope de 0,5 % por operación en lotes") in blk.value.message
            pos = await gold(1.0)  # the cap allows it: the minimum lot, never a fraction of it
            assert pos["qty"] == pytest.approx(1.0) and pos["lot"]["lots"] == pytest.approx(0.01)
            # without lots: the plain contracts its risk asks for (~124 contracts ≈ 0,124 oz, the equity less the fees)
            res = await engine.open_position(3, GOLD, "long", "1h", sl=92.0, risk_pct=0.1, check_gate=False,
                                             contracts=True, margin_pct=50.0)
            qty = res["position"]["qty"]
            assert 0.12 <= qty <= 0.125 and res["position"]["lot"]["lots"] == pytest.approx(qty / 100.0)
            await engine.stop()

    asyncio.run(scenario())


def test_lot_settings_are_validated(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = pp.PaperEngine(rt)
            await engine.start()
            with pytest.raises(pp.PaperError, match="El tope del lote mínimo"):
                await engine.set_autopilot(lot_max_risk_pct=30)
            snap = await engine.set_autopilot(lot_max_risk_pct=10, lots=False)
            assert (snap["autopilot"]["lots"], snap["autopilot"]["lot_max_risk_pct"]) == (False, 10.0)
            await engine.stop()

    asyncio.run(scenario())
