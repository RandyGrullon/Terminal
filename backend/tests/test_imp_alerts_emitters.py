"""Contract with the real emitters: the events of the semáforo (MacroService.refresh), the NEXUS watcher
(WatchMixin._events) and the demo account (PaperEngine open + stop) reach a fake channel through the AlertService."""
from __future__ import annotations

import asyncio

import pytest

from app import config
from app.alerts.service import AlertService
from app.engine.service import NexusService
from app.macro import fomc
from app.macro.service import MacroService
from app.sim import paper as pp
from tests.test_imp_alerts_fakes import Clock, FakeChannel
from tests.test_sim_fakes import FakeHub as PaperHub
from tests.test_sim_fakes import FakeNexus

SYM = "BINANCE:BTCUSDT"


async def until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("timeout waiting for the alert worker")
        await asyncio.sleep(0.01)


async def alert_service(rt, channel: FakeChannel, now: float) -> AlertService:
    svc = AlertService(rt, clock=Clock(now), channels=[channel])
    svc.batch_window_s = 0.05
    await svc.start()
    return svc


def test_semaforo_alerts_are_forwarded(runtime_factory, monkeypatch):
    async def scenario():
        # offline: the semáforo evaluates the built-in official FOMC schedule, nothing is fetched
        monkeypatch.setattr(config, "OFFLINE", True)
        monkeypatch.setattr(config, "BACKGROUND", False)
        async with runtime_factory() as rt:
            await rt.settings.set("ui.timezone", "America/New_York")
            now = fomc.ny_time("2026-10-28", 13, 40)  # 20 min before the FOMC decision: no-trade window
            tg = FakeChannel()
            alerts = await alert_service(rt, tg, now)
            macro = MacroService(rt, clock=Clock(now), monotonic=Clock(1000.0))
            await macro.start()
            cond = await macro.refresh()
            assert cond["level"] == "red"
            await until(lambda: tg.sent and not alerts._busy and alerts._queue.empty())
            text = tg.sent[0][0]
            lines = text.splitlines()
            assert any(line.startswith("🔴 No operar ahora") for line in lines)  # the level alert of the refresh
            assert lines[-1] == "— NEXUS · semáforo de mercado" and len(tg.sent) == 1  # its alerts: one message
            assert {r["category"] for r in alerts.recent} == {"macro"}
            assert any(r["priority"] == "critical" for r in alerts.recent)
            await macro.stop()
            await alerts.stop()

    asyncio.run(scenario())


def test_nexus_watcher_signals_are_forwarded(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            nexus = NexusService(rt)
            nexus._watch = {(SYM, "1h")}  # the watchlist at watch.tf, as sync_watch leaves it
            rt.nexus = nexus
            tg = FakeChannel()
            alerts = await alert_service(rt, tg, 1_790_000_000)
            cur = {"action": "BUY", "action_label": "COMPRAR", "score": 0.52, "prob_up": 0.64, "warnings": [],
                   "position": None, "advice": "Comprar con stop en 64.000"}
            gates = {"long": {"verdict_label": "TOMAR", "score": 78.0}}
            nexus._events(SYM, "1h", cur, gates, [], True)
            nexus._events("BINANCE:SOLUSDT", "1h", cur, gates, [], True)  # not watched: filtered
            early = {**cur, "action": "EXIT_LONG", "action_label": "CERRAR LARGO", "advice": "Cerrar ahora: EV −0,3 R",
                     "warnings": [{"code": "EARLY_EXIT", "severity": "critical", "message": "x"}]}
            nexus._events(SYM, "1h", early, gates, [], True)
            await until(lambda: alerts.counters["received"] == 4 and not alerts._busy and alerts._queue.empty())
            lines = [line for text, _ in tg.sent for line in text.splitlines()]
            assert any(line.startswith(f"📈 {SYM} 1h → COMPRAR · score +0,52 · p↑ 64 %") for line in lines)
            assert any(line.startswith(f"⚠ {SYM} 1h: Cerrar ahora") for line in lines)
            assert not any("SOLUSDT" in line for line in lines)
            assert not any("CERRAR LARGO" in line for line in lines)  # collapsed into the critical early exit
            assert alerts.counters["filtered"] == 1
            await alerts.stop()

    asyncio.run(scenario())


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(config, "BACKGROUND", False)
    monkeypatch.setattr(config, "OFFLINE", True)


def test_demo_account_open_and_stop_are_forwarded_as_simulated(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            monkeypatch.setattr(rt.ws, "publish", lambda topic, msg: None)
            await rt.settings.update({"risk.fee_pct": 0.1, "risk.slippage_pct": 0.0, "alerts.min_level": "info"})
            rt.hub = PaperHub(prices={SYM: 100.0})
            rt.nexus = FakeNexus("TAKE")
            engine = pp.PaperEngine(rt)
            rt.paper = engine
            await engine.start()
            tg = FakeChannel()
            alerts = await alert_service(rt, tg, 1_790_000_000)
            res = await engine.open_position(1, SYM, "long", "1h")
            pos = res["position"]
            t = int(pos["entry_time"]) + 60
            candle = {"time": t, "open": 99.0, "high": 99.5, "low": 97.0, "close": 97.5, "volume": 1.0}
            await engine._on_candle(SYM, "1m", candle, True)  # through the stop (98)
            assert engine.open_positions(1) == []
            await until(lambda: alerts.counters["sent"] == 2)
            lines = [line for text, _ in tg.sent for line in text.splitlines()]
            assert any(line.startswith(f"🧪 Cuenta demo (simulado): Abierto {SYM} largo") for line in lines)
            stop = [line for line in lines if line.startswith("🛑 Cuenta demo (simulado): Cerrado")]
            assert stop and "Stop loss" in stop[0]
            kinds = {r["kind"]: r["priority"] for r in alerts.recent}
            assert kinds == {"opened": "warn", "stop": "critical"}
            assert any("operación simulada de la cuenta demo" in text or "operaciones simuladas" in text
                       for text, _ in tg.sent)
            await alerts.stop()
            await engine.stop()

    asyncio.run(scenario())
