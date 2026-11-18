"""Overnight swap of positions in MT5 lots (app.engine.costs, setting risk.swap_by_symbol): XM's GOLD swap (longs pay,
shorts earn a little, Wednesday triple) charged at the rollover of 17:00 New York (00:00 of XM's server) and counted in
each trade's result and R like the commission."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config as nexus_config
from app.config import DEFAULT_SETTINGS
from app.engine import costs as CS
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

NY = ZoneInfo("America/New_York")
XAU = "BINANCE:XAUUSDT.P"


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%a %H:%M")


def test_rollover_calendar_is_xm_midnight_with_a_triple_wednesday():
    week = CS.rollovers(datetime(2026, 11, 16, 12, tzinfo=NY).timestamp(), datetime(2026, 11, 23, 12, tzinfo=NY).timestamp())
    assert [(_utc(ts), n) for ts, n in week] == [("Mon 22:00", 1), ("Tue 22:00", 1), ("Wed 22:00", 3), ("Thu 22:00", 1),
                                                 ("Fri 22:00", 1)]  # winter: 22:00 UTC; nothing on the weekend
    assert sum(n for _, n in week) == 7  # the triple Wednesday covers Saturday and Sunday
    summer = CS.rollovers(datetime(2026, 7, 15, 12, tzinfo=NY).timestamp(), datetime(2026, 7, 16, 12, tzinfo=NY).timestamp())
    assert [(_utc(ts), n) for ts, n in summer] == [("Wed 21:00", 3)]  # US daylight saving time: 21:00 UTC
    at = int(datetime(2026, 11, 17, 17, tzinfo=NY).timestamp())
    assert CS.rollovers(at, at + 60) == [] and CS.rollovers(at - 1, at) == [(at, 1)]  # (after, until]
    assert [CS.swap_asset(s) for s in (XAU, "OANDA:XAUUSD", "XM:GOLD", "BINANCE:BTCUSDT", "FX:EURUSD", "SIM:NEXUS",
                                       "OPT:AAPL")] == ["XAU", "XAU", "XAU", "BTC", "EUR", None, None]
    rules = DEFAULT_SETTINGS["risk.swap_by_symbol"]
    assert CS.swap_rule(XAU, rules) == {"long": -75.0, "short": 16.0}
    assert CS.swap_rule("BINANCE:BTCUSDT", rules) is None and CS.swap_rule(XAU, {"XAU": {"long": 0}}) is None
    assert CS.swap_for({"long": -75.0, "short": 16.0}, "long", 0.05, 3) == pytest.approx(-11.25)


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


async def _open(rt, action: str) -> tuple[pp.PaperEngine, dict]:
    await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0})
    rt.hub = FakeHub(prices={XAU: 100.0})
    rt.nexus = BarNexus(default=_signal(action, 2.0))
    engine = pp.PaperEngine(rt)
    await engine.start()
    await engine.set_autopilot(enabled=True, source="list", symbols=[XAU], lots=True)
    await _settle(engine)
    [pos] = engine.open_positions(3)
    return engine, pos


def test_a_gold_long_pays_the_swap_into_its_result_and_r(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine, pos = await _open(rt, "BUY")
            lots = pos["lot"]["lots"]
            assert lots == pytest.approx(0.05) and pos["swap"] == 0.0
            before = (await engine.account(3))["balance"]
            until = pos["entry_time"] + 4 * 86400
            nights = sum(n for _, n in CS.rollovers(pos["entry_time"], until))
            await engine._swap_tick(now=until)
            want = -75.0 * lots * nights
            [now] = engine.open_positions(3)
            assert now["swap"] == pytest.approx(want) and nights >= 1 and want < 0
            assert (await engine.account(3))["balance"] == pytest.approx(before + want, abs=0.01)
            assert now["pnl"] == pytest.approx(pos["pnl"] + want, abs=0.01)  # in its result, like the commission
            fills = await rt.db.fetchall("SELECT qty, fee, reason FROM paper_fills WHERE position_id=? AND reason LIKE "
                                         "'Swap%'", (pos["id"],))
            assert len(fills) == 1 and fills[0]["qty"] == 0 and fills[0]["fee"] == pytest.approx(-want)
            assert fills[0]["reason"] == f"Swap ({nights} {'noche' if nights == 1 else 'noches'})"
            await engine._swap_tick(now=until)  # the same rollovers are never charged twice
            assert engine.open_positions(3)[0]["swap"] == pytest.approx(want)
            params = {x["label"]: x["value"] for x in engine.open_positions(3)[0]["plan"]["params"]}
            assert params["Swap acumulado"].startswith("−") and "17:00 de Nueva York" in params["Swap acumulado"]
            log = " | ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "Swap del rollover:" in log
            # closed: its R is net of the swap too
            [closed] = await engine.close_position(pos["id"], account_id=3)
            risk = pos["qty"] * 2.0
            assert closed["r_multiple"] == pytest.approx(closed["pnl"] / risk, abs=1e-3)
            assert closed["pnl"] < pos["pnl"] + want + 0.01  # entry and exit fees plus the swap
            await engine.stop()

    asyncio.run(scenario())


def test_a_gold_short_earns_a_little_and_other_markets_pay_nothing(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine, pos = await _open(rt, "SELL")
            until = pos["entry_time"] + 2 * 86400
            nights = sum(n for _, n in CS.rollovers(pos["entry_time"], until))
            await engine._swap_tick(now=until)
            assert engine.open_positions(3)[0]["swap"] == pytest.approx(16.0 * pos["lot"]["lots"] * nights)
            # the setting decides (it changes with rates): zero means no swap
            await rt.settings.update({"risk.swap_by_symbol": {"XAU": {"long": 0, "short": 0}}})
            await engine._swap_tick(now=until + 3 * 86400)
            assert engine.open_positions(3)[0]["swap"] == pytest.approx(16.0 * pos["lot"]["lots"] * nights)
            btc = {"symbol": "BINANCE:BTCUSDT", "origin": "autopilot", "meta": {}}
            assert engine._swap_rule(btc) is None and engine._swap_rule({**btc, "symbol": XAU, "origin": "sim"}) is None
            await engine.stop()

    asyncio.run(scenario())
