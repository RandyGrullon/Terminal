"""NEXUS automática's challenge («reto»): the server itself closes its trades at the target equity or at the deadline,
then goes back to its normal settings. The aggressive mode is an overlay of the settings while it lasts (the saved
settings never change), and for account 3 only its daily loss limit becomes its loss brake."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import config as nexus_config
from app.sim import autopilot_goal as G
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

PERP = "BINANCE:BTCUSDT.P"
NOW = 1_800_000_000


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_parse_view_and_result():
    goal = G.parse_goal(1200, (NOW + 3600) * 1000, True, None, equity=1000.0, now=NOW)  # milliseconds are accepted
    assert goal == {"target": 1200.0, "until": NOW + 3600, "started_at": NOW, "start_equity": 1000.0,
                    "aggressive": True, "then": "normal"}
    for bad, msg in (((900, None), "mayor que el valor actual"), ((20_000, None), "como mucho 10 veces"),
                     ((1200, NOW + 30), "al menos 1 minuto"), ((1200, NOW + 8 * 86400), "7 días"),
                     ((0, None), "mayor que 0")):
        with pytest.raises(ValueError, match=msg):
            G.parse_goal(*bad, False, "normal", equity=1000.0, now=NOW)
    with pytest.raises(ValueError, match="Al terminar"):
        G.parse_goal(1200, None, False, "sell", equity=1000.0, now=NOW)
    assert G.load_goal(goal) == goal and G.load_goal({"target": "x"}) is None and G.load_goal(None) is None

    view = G.goal_view(goal, 1050.0, NOW + 600)
    assert (view["progress_pct"], view["remaining"], view["pnl"], view["seconds_left"]) == (25.0, 150.0, 50.0, 3000)
    assert view["mode_text"] == G.AGGRESSIVE_TEXT and "no crea ventaja" in view["note"]
    assert G.goal_view(goal, 900.0, NOW)["progress_pct"] == 0.0  # below the start: nothing done yet, never negative

    trades = [{"pnl": 30.0}, {"pnl": -10.0}, {"pnl": 25.0}]
    res = G.goal_result(goal, "time", equity=1045.5, now=NOW + 3600, closed=1, trades=trades)
    assert (res["wins"], res["losses"], res["pnl"], res["pnl_pct"]) == (2, 1, 45.5, 4.55)
    assert res["text"].startswith("Se acabó el tiempo: meta 1.200,00, de 1.000,00 a 1.045,50 +45,50 (+4,55 %)")
    assert "2 de 3 operaciones ganadoras" in res["text"] and "cerró 1 operación abierta" in res["text"]
    late = G.goal_result(goal, "time", equity=990.0, now=NOW, closed=0, trades=[], late=True)
    assert "sin operaciones cerradas" in late["text"] and "−10,00" in late["text"] and "servidor estaba apagado" in late["text"]
    # the aggressive daily loss limit is the loss brake (20 % when the brake is off)
    assert G.aggressive_risk(15) == {"daily_loss_limit_pct": 15.0, "max_open_risk_pct": 20.0}
    assert G.aggressive_risk(0)["daily_loss_limit_pct"] == 20.0


async def _engine(rt) -> pp.PaperEngine:
    await rt.settings.update({"risk.slippage_pct": 0.0})
    rt.hub = FakeHub(prices={PERP: 100.0})
    rt.nexus = BarNexus(default=_signal("BUY", 2.0))
    engine = pp.PaperEngine(rt)
    await engine.start()
    return engine


def test_aggressive_mode_is_an_overlay_that_ends_with_the_challenge(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            snap = await engine.start_goal(1200, int(time.time()) + 3600, True)
            goal = snap["autopilot"]["goal"]
            assert (goal["target"], goal["start_equity"], goal["aggressive"], goal["remaining"]) == (
                1200.0, 1000.0, True, 200.0)
            cfg = engine._autopilot_cfg()
            assert (cfg["tfs"], cfg["risk_pct"], cfg["lots"], cfg["scanner_top"], cfg["script_gate"]) == (
                ["5m", "15m", "1h"], 4.0, True, 30, False)
            assert engine._perp_leverage() == 20.0
            # the saved settings never change
            assert rt.settings.get("autopilot.risk_pct") == 1.0 and rt.settings.get("autopilot.tfs") == []
            # account 3 only: its daily loss limit is its brake (20 %), the user's demo account keeps 3 %
            assert (await engine.risk_state(3))["daily_loss_limit_pct"] == 20.0
            assert (await engine.risk_state(1))["daily_loss_limit_pct"] == 3.0
            with pytest.raises(pp.PaperError, match="Ya hay un reto"):
                await engine.start_goal(1300)
            # it trades with 4 % of its equity at the stop: 40 $ at a 2-point stop
            await engine.set_autopilot(enabled=True, source="list", symbols=[PERP])
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["qty"] * 2.0 == pytest.approx(40.0)
            # ended by hand without closing: the overlay ends, the trade stays with its stop and targets
            snap = await engine.stop_goal()
            assert snap["autopilot"]["goal"] is None and snap["autopilot"]["goal_last"]["outcome"] == "cancelled"
            assert len(engine.open_positions(3)) == 1
            cfg = engine._autopilot_cfg()
            assert (cfg["tfs"], cfg["risk_pct"]) == (["1h"], 1.0)
            assert (await engine.risk_state(3))["daily_loss_limit_pct"] == 3.0
            with pytest.raises(pp.PaperError, match="No hay ningún reto"):
                await engine.stop_goal()
            await engine.stop()

    asyncio.run(scenario())


def test_reaching_the_target_closes_its_trades_by_itself(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.set_autopilot(enabled=True, source="list", symbols=[PERP])
            await _settle(engine)
            [pos] = engine.open_positions(3)  # 1 % of 1 000 at a 2-point stop: 5 BTC
            await engine.start_goal(1020)
            await engine._goal_tick()
            assert engine._goal is not None  # not there yet
            rt.hub.prices[PERP] = 106.0
            engine._prices[PERP] = (106.0, time.monotonic())  # +6 × 5 = +30 $ open
            await engine._goal_tick()
            assert engine._goal is None and engine.open_positions(3) == []
            [closed] = [h for h in await engine.history(3) if h["id"] == pos["id"]]
            assert closed["exit_label"] == "Cierre del reto" and closed["pnl"] > 0
            last = engine.autopilot_status()["goal_last"]
            assert (last["outcome"], last["closed"], last["trades"], last["wins"]) == ("reached", 1, 1, 1)
            assert last["text"].startswith("Meta cumplida: meta 1.020,00")
            log = " | ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "Cerrada al terminar el reto a" in log and "Meta cumplida" in log
            assert engine._autopilot_cfg()["enabled"]  # «normal»: it keeps trading with its saved settings
            await engine.stop()

    asyncio.run(scenario())


def test_the_deadline_ends_it_even_after_a_restart_and_can_pause_it(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.set_autopilot(enabled=True, source="list", symbols=[PERP])
            await _settle(engine)
            until = int(time.time()) + 3600
            await engine.start_goal(5000, until, False, "pause")
            await engine.stop()
            # the server restarts: the challenge is still there, and the deadline passed while it was off
            again = pp.PaperEngine(rt)
            await again.start()
            assert again._goal is not None and again._goal["until"] == until
            await again._goal_tick(now=until + 3600)
            last = again.autopilot_status()["goal_last"]
            assert (last["outcome"], last["late"], last["closed"]) == ("time", True, 1)
            assert again.open_positions(3) == [] and not again._autopilot_cfg()["enabled"]  # «pause»
            # a reset cancels a running challenge (measured from the old balance)
            await again.start_goal(5000)
            await again.reset(2000, account_id=3)
            assert again._goal is None and "Reto cancelado" in again.autopilot_status()["log"][0]["text"]
            await again.stop()

    asyncio.run(scenario())
