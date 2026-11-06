"""NEXUS automática, phase 2: index futures (app.engine.contracts) in whole contracts sized by its risk per trade, an
approximate-margin budget checked atomically, partial targets only with a multiple of 3 contracts, the contract on the
position and P&L in dollars (points × multiplier); the user's account keeps sizing futures in plain units."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import config as nexus_config
from app.engine.contracts import FUTURES, futures_spec
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_sim_fakes import FakeHub, make_current

MES, M2K, MNQ = "CME_MINI:MES1!", "CME_MINI:M2K1!", "CME_MINI:MNQ1!"


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def _stop(points: float) -> dict:
    """NEXUS says BUY at 100 with the gate's stop `points` below."""
    cur = make_current("BUY")
    for g in cur["gate"].values():
        g["sl"] = 100.0 - points if g["direction"] == "long" else 100.0 + points
    return cur


def test_futures_specs():
    assert (futures_spec(MES)["root"], futures_spec(MES)["multiplier"]) == ("MES", 5.0)
    assert futures_spec("CME_MINI:ES1!")["root"] == "ES" and futures_spec(M2K)["root"] == "M2K"
    assert futures_spec("CME_MINI:MESZ2026")["root"] == "MES" and futures_spec("CBOT_MINI:MYMH26")["root"] == "MYM"
    for other in ("NASDAQ:AAPL", "BINANCE:BTCUSDT", "CME:6E1!", ""):
        assert futures_spec(other) is None
    assert all(v["margin"] > 0 and v["multiplier"] > 0 and v["tick"] > 0 for v in FUTURES.values())


def test_whole_contracts_by_risk_and_an_approximate_margin_budget(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0})
            rt.hub = FakeHub(prices={MES: 100.0, M2K: 100.0, MNQ: 100.0})
            rt.nexus = BarNexus(default=_stop(2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            # 1 % of 1 000 = 10 $; 1 MES contract at a 2-point stop risks 2 × 5 = 10 $: one fits the risk, but its
            # margin (~1 500 $) is over the 50 % of the equity it may tie up
            await engine.set_autopilot(enabled=True, source="list", symbols=[MES])
            await _settle(engine)
            assert engine.open_positions(3) == []
            text = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "El margen de 1 contrato MES (~1.500, aproximado) supera lo disponible" in text and "(50 % del" in text
            # with up to 100 % of the equity as margin, one M2K (~700 $) fits
            await engine.set_autopilot(symbols=[M2K], futures_margin_pct=100)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["instrument"] == {"kind": "future", "root": "M2K", "name": "Micro E-mini Russell 2000",
                                         "multiplier": 5.0, "contracts": 1, "margin": 700.0}
            assert pos["qty"] == 5.0 and engine._open[pos["id"]]["meta"]["partial"] is False  # 1 contract: no thirds
            engine._prices[M2K] = (110.0, time.monotonic())  # +10 points × 5 $ = +50 $
            assert (await engine.account(3))["unrealized_pnl"] == pytest.approx(50.0)
            await engine.close_autopilot(all=True)
            # a stop too wide for the budget: 1 MNQ contract at 10 points risks 20 $ > 10 $
            rt.nexus.default = _stop(10.0)
            await engine.set_autopilot(symbols=[MNQ])
            await _settle(engine)
            assert engine.open_positions(3) == []
            assert any("1 contrato MNQ (Micro E-mini Nasdaq-100) arriesga 20,00 con este stop" in e["text"]
                       for e in engine.autopilot_status()["log"])
            # the user's own account sizes a future in plain units, as before (no contract rules)
            res = await engine.open_position(1, MES, "long", "1h", check_gate=False, sl=98.0)
            assert res["position"]["instrument"] is None and res["position"]["qty"] == pytest.approx(50.0)
            await engine.stop()

    asyncio.run(scenario())


def test_thirds_of_contracts_and_a_margin_that_trims_the_order(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0})
            rt.hub = FakeHub(prices={M2K: 100.0})
            rt.nexus = BarNexus(default=_stop(2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.reset(100_000.0, account_id=3)
            # 0,3 % of 100 000 = 300 $ → 30 contracts of 10 $ each; margin 30 × 700 = 21 000 ≤ 100 % → thirds allowed
            await engine.set_autopilot(enabled=True, source="list", symbols=[M2K], risk_pct=0.3, futures_margin_pct=100)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["instrument"]["contracts"] == 30 and pos["qty"] == 150.0
            assert engine._open[pos["id"]]["meta"]["partial"] is True
            await engine.close_autopilot(all=True)
            # 10 % of the equity for margin: 10 000 $ / 700 → 14 contracts of the 30 wanted (no thirds with 14)
            await engine.set_autopilot(futures_margin_pct=10)
            t = engine._auto_bar[(3, M2K, "1h")]
            await engine._on_candle(M2K, "1h", {"time": t + 3600, "open": 100, "high": 101, "low": 99, "close": 100,
                                                "volume": 1}, True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["instrument"]["contracts"] == 14 and pos["qty"] == 70.0
            assert engine._open[pos["id"]]["meta"]["partial"] is False
            await engine.stop()

    asyncio.run(scenario())
