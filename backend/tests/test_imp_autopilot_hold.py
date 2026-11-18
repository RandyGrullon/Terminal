"""NEXUS automática's buy-and-hold portfolio (autopilot.hold_*): hold_pct % of its equity spread over its basket, each
asset bought once at market (a stock only with its market open) with a far emergency stop and no targets, held (no
trader closes it on NEXUS's signals), outside the trading limits (open risk, per group, maximum of positions), not
bought again for 30 days after it is sold, and bought again after a reset of the account."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_sim_fakes import FakeHub, make_current

SPY, BTC, ETH = "AMEX:SPY", "BINANCE:BTCUSDT", "BINANCE:ETHUSDT"


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_portfolio_buys_once_holds_and_stays_outside_the_trading_limits(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({
                "risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 2.0, "paper.max_positions_per_group": 1,
                "autopilot.hold_pct": 50, "autopilot.hold_stop_pct": 50,
                "autopilot.hold_basket": [{"symbol": SPY, "weight": 60}, {"symbol": BTC, "weight": 20},
                                          {"symbol": ETH, "weight": 20}, {"symbol": "malo", "weight": 5}]})
            rt.hub = FakeHub(prices={SPY: 500.0, BTC: 50_000.0, ETH: 2_000.0})
            rt.nexus = BarNexus(default=make_current("WAIT"))
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.reset(1000.0, account_id=3)
            open_markets = {BTC, ETH}  # the US market is closed: SPY waits

            async def live(symbol: str) -> bool:
                return symbol in open_markets

            monkeypatch.setattr(engine, "_market_live", live)
            await engine.set_autopilot(enabled=True, source="list", symbols=[], tfs=["1h", "4h"], max_positions=1)
            await engine._refresh_autopilot(force=True)
            await _settle(engine)
            st = engine.autopilot_status()
            assert (st["hold_pct"], st["hold_stop_pct"]) == (50.0, 50.0)
            assert [x["symbol"] for x in st["hold_basket"]] == [SPY, BTC, ETH]  # the invalid one is left out
            held = {p["symbol"]: p for p in engine.open_positions(3)}
            assert set(held) == {BTC, ETH}
            btc = held[BTC]
            # 50 % of 1 000 = 500 $; BTC 20 % → 100 $ = 0.002 BTC, held on daily bars, no targets, stop −50 %
            assert btc["hold"] is True and btc["tf"] == "1d" and btc["qty"] == pytest.approx(0.002)
            assert btc["sl"] == pytest.approx(25_000.0) and (btc["tp1"], btc["tp2"], btc["tp3"]) == (None, None, None)
            # outside the limits: 2 holdings with a 1 % open-risk cap and one position per group were not refused,
            # and they do not count against the trading positions (max_positions 1 still leaves room)
            assert engine._open_risk(3) == 0.0
            engine._check_max_positions(3, 1)
            engine._check_group(3, "BINANCE:SOLUSDT")
            # no trader follows a holding: NEXUS's exit signals never close it
            assert not any(engine._follows(prof, engine._open[btc["id"]]) for prof in engine._autopilot_profiles())
            notes = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "Compra para la cartera" in notes and "espera a que abra su mercado" in notes
            # the market opens: SPY is bought (60 % → 300 $ = 0.6 shares), a retry waits HOLD_RETRY_S
            open_markets.add(SPY)
            engine._hold_retry.clear()
            await engine._refresh_autopilot(force=True)
            spy = next(p for p in engine.open_positions(3) if p["symbol"] == SPY)
            assert spy["qty"] == pytest.approx(0.6, rel=1e-3) and spy["hold"]  # of the equity after the fees paid
            # «close all» closes its trades, never the portfolio
            await engine.close_autopilot(all=True)
            assert {p["symbol"] for p in engine.open_positions(3)} == {SPY, BTC, ETH}
            # sold by the user: not bought again (30 days)
            await engine.close_autopilot(position_id=btc["id"])
            engine._hold_retry.clear()
            await engine._refresh_autopilot(force=True)
            assert BTC not in {p["symbol"] for p in engine.open_positions(3)}
            # a reset of the account buys the portfolio again
            await engine.reset(1000.0, account_id=3)
            await engine._refresh_autopilot(force=True)
            assert {p["symbol"] for p in engine.open_positions(3)} == {SPY, BTC, ETH}
            # off (hold_pct 0): nothing is bought
            await engine.reset(1000.0, account_id=3)
            await rt.settings.update({"autopilot.hold_pct": 0})
            await engine._refresh_autopilot(force=True)
            assert engine.open_positions(3) == []
            await engine.stop()

    asyncio.run(scenario())
