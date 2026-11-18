"""Every trade of NEXUS automática carries a plan: why it opened, and its target or, when it has none, why not
(Position.plan, app.sim.trade_plan)."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from app.sim import trade_plan as TP
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

XAU = "BINANCE:XAUUSDT.P"


def _pos(**over) -> dict:
    base = {"origin": "autopilot", "side": "short", "tf": "15m", "sl": 3789.22, "entry_price": 3752.98,
            "tp1": None, "tp2": None, "tp3": None, "tp_hits": 0, "trail": None, "gate_score": None,
            "gate_verdict": None}
    return {**base, **over}


def test_the_plan_of_each_kind_of_trade():
    src = {"context": {"source": "script:12:Supertrend + filtro ADX (estrategia)"}}
    plan = TP.plan_of(_pos(), src, 36.24)
    assert plan["target"] is None and plan["target_r"] is None
    assert plan["why"] == "Señal de entrada de la estrategia «Supertrend + filtro ADX (estrategia)» al cierre de la vela de 15m"
    assert plan["no_target"].startswith("La estrategia «Supertrend + filtro ADX (estrategia)» no fija objetivo: la cierra "
                                        "su propia regla de salida")
    assert "3.789,22" in plan["no_target"]
    # NEXUS's own signal with its targets: the next pending one, in R
    nexus = TP.plan_of(_pos(side="long", entry_price=100.0, sl=98.0, tp1=102.0, tp2=104.0, tp3=106.0,
                            gate_verdict="TAKE", gate_score=78.0), {"context": {"source": "nexus"}}, 2.0)
    assert (nexus["target"], nexus["target_r"], nexus["no_target"]) == (102.0, 1.0, None)
    assert nexus["why"] == "NEXUS dijo COMPRAR al cierre de la vela de 15m y el filtro dijo TOMAR 78/100"
    after_tp1 = TP.plan_of(_pos(side="long", entry_price=100.0, sl=100.0, tp1=102.0, tp2=104.0, tp3=106.0, tp_hits=1),
                           {}, 2.0)
    assert (after_tp1["target"], after_tp1["target_r"]) == (104.0, 2.0)
    # the why written at the entry wins; a holding of its portfolio explains why it has no target
    written = TP.plan_of(_pos(tp1=3700.0), {"context": {"why": "Por esto"}}, 36.0)
    assert written["why"] == "Por esto" and written["target"] == 3700.0
    hold = TP.plan_of(_pos(side="long", sl=300.0), {"hold": True}, None)
    assert hold["why"] == "Compra de su cartera de comprar y mantener"
    assert hold["no_target"].startswith("Es de su cartera de comprar y mantener") and "stop de emergencia" in hold["no_target"]
    assert TP.plan_of({**_pos(), "origin": "manual"}, {}, 1.0) is None  # only NEXUS automática's trades
    assert TP.entry_why(side="short", tf="5m", script_name="Donchian", entry_id="Corto", gate="ESPERAR 55/100",
                        gate_required=False, nexus_read="ESPERAR") == (
        "Señal de entrada «Corto» de la estrategia «Donchian» al cierre de la vela de 5m; filtro no exigido "
        "(ESPERAR 55/100); NEXUS decía ESPERAR")
    assert TP.strategy_name("script:7:") == "Script 7" and TP.strategy_name("nexus") is None


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_a_trade_opens_with_its_why_and_target_in_the_log(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0})
            rt.hub = FakeHub(prices={XAU: 100.0})
            rt.nexus = BarNexus(default=_signal("SELL", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.set_autopilot(enabled=True, source="list", symbols=[XAU], lots=True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            plan = pos["plan"]
            assert plan["why"].startswith("NEXUS dijo VENDER al cierre de la vela de 1h y el filtro dijo TOMAR")
            assert (plan["target"] is not None) != (plan["no_target"] is not None)  # one or the other, always
            params = {x["label"]: x["value"] for x in plan["params"]}
            assert params["Cuenta"] == "NEXUS automática" and params["Fuente de la señal"] == "Señal de NEXUS"
            assert params["Temporalidad"] == "1h (medida)" and params["Lectura de NEXUS"]
            assert params["Filtro «¿Tomo esta operación?»"].startswith("TOMAR") and params["Filtro «¿Tomo esta operación?»"].endswith("(exigido)")
            assert params["Riesgo por operación"] == "1,0 % del capital" and params["Modo"] == "normal"
            assert params["Stop inicial"].startswith("102,00") and "% de la entrada" in params["Stop inicial"]
            assert params["Pérdida si toca el stop inicial"].endswith("$ (antes de comisiones)")
            assert params["Tamaño"].endswith("oz)") and params["Apalancamiento"].startswith("10x · margen ~")
            assert "Objetivos" in params and "Sesión al entrar" in params
            text = next(e["text"] for e in engine.autopilot_status()["log"] if e["kind"] == "open")
            assert (" · objetivo " in text) or (" · sin objetivo: " in text)
            assert engine.open_positions(1) == [] and all(p["plan"] is None for p in engine.open_positions(1))
            await engine.stop()

    asyncio.run(scenario())
