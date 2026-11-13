"""Health of NEXUS automática: its heartbeat (a stop of the server or the PC while it was on is noted, alerted and shown
for a day), the 24/7 markets it watches without recent decisions (their candles stopped arriving: alerted once per
incident) and its weekly report (Mondays, of the week that just ended; the first pass only takes note of the week).
Its alerts are opt-in: category autopilot, sent only with alerts.autopilot on."""
from __future__ import annotations

import asyncio

from app.alerts.classify import classify
from app.sim import paper as pp
from tests.test_imp_autopilot import SYM, BarNexus, _engine, _settle, no_background  # noqa: F401 (fixture)
from tests.test_sim_fakes import make_current

H = 3600
WEDNESDAY = 1_762_300_800  # Wed 2025-11-05 00:00 UTC (Mon 2025-11-03 00:00 UTC = 1762128000)


def _notes(engine: pp.PaperEngine) -> list[str]:
    return [e["text"] for e in engine.autopilot_status()["log"]]


def _alerts(rt, kind: str) -> list:
    out = []
    for ev in rt.bus.recent():
        alert = classify(ev)
        if alert is not None and alert.category == "autopilot" and alert.kind == kind:
            out.append(alert)
    return out


def test_markets_without_recent_decisions_and_a_stop_of_the_pc(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("WAIT")))
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            await engine._autopilot_watchdog()  # its monitor passes every 5 s while it is on
            cfg = engine._autopilot_cfg()
            health = engine.autopilot_status()["health"]
            assert health["ok"] and health["stale"] == [] and health["downtime"] is None
            assert health["text"] == "Funciona: decide al cierre de cada vela de 1h." and health["last_decision"]
            # four hours later with no new bar: the PC slept (a gap between two passes) and ETH has no recent decision
            later = pp.now_s() + 4 * H
            await engine._autopilot_watchdog(now=later)
            health = engine._autopilot_health(cfg, later)
            assert not health["ok"] and health["stale"] == [SYM]
            assert health["downtime"]["to"] == later * 1000
            notes = _notes(engine)
            assert any(t.startswith("Estuvo detenida 4 h") and "el PC sin suspenderse" in t for t in notes), notes
            assert any(t.startswith("Sin decisiones recientes en ETHUSDT") for t in notes), notes
            assert len(_alerts(rt, "downtime")) == 1 and len(_alerts(rt, "stale")) == 1
            await engine._autopilot_watchdog(now=later + 5)  # the same incident: no second alert
            assert len(_alerts(rt, "stale")) == 1
            # a new closed bar is decided: it decides again
            engine._autopilot_read(SYM, "1h", later - H, make_current("WAIT"))
            await engine._autopilot_watchdog(now=later + 10)
            assert engine._autopilot_health(cfg, later + 10)["ok"] and "Vuelve a decidir con normalidad" in _notes(engine)
            # paused: nothing to watch over
            await engine.set_autopilot(enabled=False)
            assert engine.autopilot_status()["health"]["text"] == "Pausada: no decide."
            await engine.stop()

    asyncio.run(scenario())


def test_a_server_that_was_off_is_noticed_at_its_start(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"autopilot.enabled": True, "autopilot.source": "list", "autopilot.symbols": [SYM]})
            await rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                (pp.HEARTBEAT_KEY, str(pp.now_s() - 5 * H)))
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("WAIT")))
            down = engine.autopilot_status()["health"]["downtime"]
            assert down is not None and down["to"] - down["from"] >= 5 * H * 1000 - 5000
            assert any(t.startswith("Estuvo detenida 5 h") for t in _notes(engine))
            await engine.stop()
            # a heartbeat a few minutes old (it is saved every 5 min) is no stop
            await rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                (pp.HEARTBEAT_KEY, str(pp.now_s() - 300)))
            again = pp.PaperEngine(rt)
            await again.start()
            assert again.autopilot_status()["health"]["downtime"] is None
            await again.stop()

    asyncio.run(scenario())


def _closed(rt, exit_time: int, r: float, cause: str):
    meta = pp.json_dumps({"journal": {"cause": cause, "mae_r": 1.0, "mfe_r": 0.1}})
    return rt.db.execute(
        "INSERT INTO paper_positions(account_id, symbol, tf, side, qty, qty_open, entry_price, entry_time, sl, tp_hits, "
        "status, exit_price, exit_time, pnl, pnl_pct, r_multiple, exit_reason, origin, meta) VALUES "
        "(3, ?, '1h', 'long', 1, 0, 100, ?, 98, 0, 'closed', ?, ?, ?, 0, ?, 'SL', 'autopilot', ?)",
        (SYM, exit_time - H, 100 + 2 * r, exit_time, 2 * r, r, meta))


def test_the_weekly_report_of_the_week_that_ended(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("WAIT")))
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            await engine._autopilot_watchdog(now=WEDNESDAY)  # the first pass only takes note of the week
            assert engine.autopilot_status()["weekly"] is None
            await _closed(rt, WEDNESDAY + 3600, 1.5, "objetivo")
            await _closed(rt, WEDNESDAY + 7200, -1.0, "en_contra")
            await _closed(rt, WEDNESDAY + 9000, -1.0, "en_contra")
            await _closed(rt, WEDNESDAY - 3 * 86400, 2.0, "objetivo")  # the week before: not in this report
            await engine._autopilot_watchdog(now=WEDNESDAY + 7 * 86400)
            weekly = engine.autopilot_status()["weekly"]
            assert (weekly["trades"], weekly["wins"], weekly["total_r"], weekly["pnl"]) == (3, 1, -0.5, -1.0)
            assert weekly["week_start"] == 1_762_128_000_000 and weekly["week_end"] == 1_762_732_800_000
            assert weekly["text"] == ("Semana del 03/11 al 09/11: 3 operaciones cerradas, 1 de 3 ganadoras, R total "
                                      "−0,50R (−1,00 $). Lo más repetido en sus pérdidas: «En contra desde el principio» "
                                      "(2 de 2). Operaciones simuladas; no es asesoría financiera.")
            assert len(_alerts(rt, "weekly")) == 1
            await engine._autopilot_watchdog(now=WEDNESDAY + 7 * 86400 + 60)  # once a week
            assert len(_alerts(rt, "weekly")) == 1
            saved = await rt.db.fetchval("SELECT value FROM meta WHERE key=?", (pp.WEEKLY_KEY,))
            assert pp.json_loads(saved, {})["text"] == weekly["text"]
            # a restart keeps it
            await engine.stop()
            again = pp.PaperEngine(rt)
            await again.start()
            assert again.autopilot_status()["weekly"]["text"] == weekly["text"]
            await again.stop()

    asyncio.run(scenario())


def test_terminal_shows_its_health_and_weekly_report(api_client):
    res = api_client.post("/api/terminal", json={"text": "autopilot"}).json()
    assert res["ok"] and "Pausada: no decide." in "\n".join(line["text"] for line in res["lines"])
    res = api_client.post("/api/terminal", json={"text": "autopilot semana"}).json()
    assert res["ok"] and "Todavía no hay informe semanal" in "\n".join(line["text"] for line in res["lines"])
    st = api_client.get("/api/paper/autopilot").json()["autopilot"]
    assert set(st["health"]) == {"ok", "last_decision", "stale", "downtime", "text"} and st["weekly"] is None
