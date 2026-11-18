"""NEXUS automática with one position per market AND timeframe (autopilot.per_tf_positions): several XAUUSD trades at once,
one per timeframe, in account 3 and in its challenge accounts; and closing a challenge account's trades by hand while
it keeps running."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from app.util import floor_time, now_s, tf_seconds
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

XAU = "BINANCE:XAUUSDT.P"


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


async def _next_bar(engine: pp.PaperEngine, prof: pp.AutoProfile) -> None:
    """The trader decides a newer closed bar of its timeframe (as the feed hands it over)."""
    upto = floor_time(now_s(), prof.tf) + tf_seconds(prof.tf)
    engine._auto_bar[(prof.account_id, XAU, prof.tf)] = upto
    await engine._auto_check(XAU, prof.tf, upto, prof)


async def _engine(rt) -> pp.PaperEngine:
    await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0})
    rt.hub = FakeHub(prices={XAU: 100.0})
    rt.nexus = BarNexus(default=_signal("BUY", 2.0))
    engine = pp.PaperEngine(rt)
    await engine.start()
    return engine


def test_one_market_one_position_unless_per_timeframe(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            snap = await engine.set_autopilot(enabled=True, source="list", symbols=[XAU], tfs=["5m", "1h"], lots=True)
            await _settle(engine)
            assert snap["autopilot"]["per_tf_positions"] is False
            assert len(engine.open_positions(3)) == 1  # one per market: the second timeframe waits
            await engine.close_autopilot(all=True)
            snap = await engine.set_autopilot(per_tf_positions=True, tfs=["5m", "15m", "1h"])
            assert snap["autopilot"]["per_tf_positions"] is True
            assert rt.settings.get("autopilot.per_tf_positions") is True
            await _settle(engine)
            for prof in engine._autopilot_entry_profiles():  # the next bar of each timeframe (5m and 1h were decided)
                await _next_bar(engine, prof)
            open_ = engine.open_positions(3)
            assert sorted(p["tf"] for p in open_) == ["15m", "1h", "5m"]  # one gold trade per timeframe
            assert all(p["symbol"] == XAU and p["lot"]["unit"] == "oz" for p in open_)
            # a bar of a timeframe that already holds its trade opens no second one there
            await _next_bar(engine, next(p for p in engine._autopilot_entry_profiles() if p.tf == "5m"))
            assert len(engine.open_positions(3)) == 3
            await engine.stop()

    asyncio.run(scenario())


def test_challenge_accounts_stack_too_and_close_by_hand(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.set_autopilot(enabled=True, source="list", symbols=[XAU], tfs=["1h"], lots=True,
                                       per_tf_positions=True)
            await _settle(engine)
            await engine.create_challenge(5000, 10000)
            await _settle(engine)
            # the challenge account is aggressive: 5m, 15m and 1h, a gold trade on each
            assert sorted(p["tf"] for p in engine.open_positions(101)) == ["15m", "1h", "5m"]
            closed = await engine.close_autopilot(all=True, account_id=101)
            assert len(closed) == 3 and engine.open_positions(101) == []
            assert engine._challenges[101]["status"] == "running"  # it keeps going
            assert any("a mano" in e["text"] for e in engine._ch_logs[101])
            assert len(engine.open_positions(3)) == 1  # account 3 untouched
            with pytest.raises(pp.PaperError, match="no es de NEXUS automática"):
                await engine.close_autopilot(all=True, account_id=1)
            await engine.stop()

    asyncio.run(scenario())
