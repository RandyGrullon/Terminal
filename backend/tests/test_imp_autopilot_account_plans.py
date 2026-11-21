"""Plans by account size (autopilot.account_plans, app.sim.account_plan) and the focus symbols (autopilot.focus_symbols
/ autopilot.wide_tfs) of NEXUS automática."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import account_plan as AP
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

GOLD, BTC = "BINANCE:XAUUSDT.P", "BINANCE:BTCUSDT.P"


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_tiers_by_initial_balance():
    assert [AP.plan_for(x)["tier"] for x in (10000, 5000, 1000, 500, 300, 100, 0)] == [
        "grande", "grande", "media", "pequeña", "pequeña", "micro", "micro"]
    p = AP.plan_for(10000)
    assert (p["risk_pct"], p["day_target_pct"], p["day_loss_pct"]) == (0.75, 1.5, 2.25)
    # smaller accounts: more risk per trade, a larger daily goal in %, the loss limit = 3 × the risk
    for x in (10000, 1000, 500, 100):
        q = AP.plan_for(x)
        assert q["day_target_pct"] == 2 * q["risk_pct"] and q["day_loss_pct"] == 3 * q["risk_pct"]
    v = AP.view(3, p, {"start_balance": 10000.0, "realized": 75.0})
    assert (v["day_target"], v["day_loss_limit"], v["risk_usd"], v["day_progress_pct"]) == (150.0, 225.0, 75.0, 50.0)


def test_plans_drive_risk_daily_goal_and_loss(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0})
            rt.hub = FakeHub(prices={GOLD: 100.0})
            rt.nexus = BarNexus(default=_signal("BUY", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.reset(10000.0, account_id=3)
            a100 = (await engine.create_twin(100))["twin"]["id"]
            # off: the saved settings
            assert engine._plan(3) is None and (await engine.autopilot_snapshot())["plans"] == []
            await rt.settings.update({"autopilot.account_plans": True})
            assert engine._autopilot_cfg(3)["risk_pct"] == 0.75 and engine._autopilot_cfg(a100)["risk_pct"] == 2.5
            assert engine._risk_limits(3)["daily_loss_limit_pct"] == 2.25
            assert engine._risk_limits(a100)["daily_loss_limit_pct"] == 7.5
            snap = await engine.autopilot_snapshot()
            assert [(p["account_id"], p["tier"], p["day_target"]) for p in snap["plans"]] == [(3, "grande", 150.0),
                                                                                             (a100, "micro", 5.0)]
            # the daily goal reached -> no new entries until 00:00 UTC (latched), with its reason
            day = pp.utc_day(pp.now_s())
            engine._day_target_hit[a100] = day
            with pytest.raises(pp.PaperRiskBlocked, match="Meta del día alcanzada"):
                await engine._check_daily(a100)
            await engine._check_daily(3)  # the 10k account is not blocked
            await engine.stop()

    asyncio.run(scenario())


def test_focus_symbols_trade_every_timeframe_the_rest_only_the_wide_ones(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "autopilot.focus_symbols": [GOLD],
                                      "autopilot.wide_tfs": ["1h"]})
            rt.hub = FakeHub(prices={GOLD: 100.0, BTC: 100.0})
            rt.nexus = BarNexus(default=_signal("BUY", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.set_autopilot(enabled=True, source="list", symbols=[GOLD, BTC], tfs=["5m", "15m", "1h"])
            prof = {p.tf: set(p.symbols) for p in engine._autopilot_entry_profiles() if p.account_id == 3}
            assert prof == {"5m": {GOLD}, "15m": {GOLD}, "1h": {GOLD, BTC}}
            await rt.settings.update({"autopilot.focus_symbols": []})  # off: every symbol on every timeframe
            prof = {p.tf: set(p.symbols) for p in engine._autopilot_entry_profiles() if p.account_id == 3}
            assert all(v == {GOLD, BTC} for v in prof.values())
            await engine.stop()

    asyncio.run(scenario())


def test_metal_hours_follow_the_real_gold_market(runtime_factory, no_background):
    """autopilot.metal_hours: the gold perpetual (24/7 on Binance) opens nothing while real gold is closed (Friday
    17:00 → Sunday 18:00 New York, daily 17-18 h); crypto is not affected; off = the old 24/7 behaviour."""
    from app.sim import market_session as MS
    sat = int(MS.datetime(2026, 11, 21, 12, 0, tzinfo=MS.NEW_YORK).timestamp())
    wed = int(MS.datetime(2026, 11, 18, 12, 0, tzinfo=MS.NEW_YORK).timestamp())
    pause = int(MS.datetime(2026, 11, 18, 17, 30, tzinfo=MS.NEW_YORK).timestamp())

    async def scenario():
        async with runtime_factory() as rt:
            engine = pp.PaperEngine(rt)
            await engine.start()
            assert engine._metal_closed(GOLD, sat) is False  # off by default
            await rt.settings.update({"autopilot.metal_hours": True})
            assert engine._metal_closed(GOLD, sat) is True and engine._metal_closed(GOLD, pause) is True
            assert engine._metal_closed(GOLD, wed) is False
            assert engine._metal_closed(BTC, sat) is False
            await engine.stop()

    asyncio.run(scenario())
