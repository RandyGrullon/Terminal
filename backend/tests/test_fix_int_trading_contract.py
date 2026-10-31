"""Integration (trading): conventions shared by the engine, the paper account and the scanner.

- gate_log.target_at: one convention for every writer (watcher, paper, scanner): the close of the
  time_stop_bars-th bar after the decision bar, the window the evaluator judges every source over.
- A quick read the scanner takes from candles the hub flags as outdated carries `stale` and says so once, also
  inside NEXUS's stale response (contract I2).
- Paper "Cerrar ya" follows the engine's own exit rules (contract I7): EV of holding <= EARLY_EV with the score
  against, or NEXUS leaving the side; any other negative EV is the amber "Vigila la salida".
"""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.engine.levels import ev_hold
from app.engine.nexus_current import EARLY_EV
from app.engine.service import NexusService
from app.engine.service_watch import gate_target_ms
from app.sim import evaluator as ev
from app.sim import paper as pp
from app.sim import scanner as sc
from app.util import floor_time, tf_seconds
from tests.conftest import FIXED_END
from tests.test_fix_engine_stale import stale_set
from tests.test_fix_engine_watch import SeriesHub, WSRecorder
from tests.test_sim_fakes import FakeHub, make_current, make_gate

SYM = "BINANCE:ETHUSDT"
H = 3600


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_gate_target_is_the_evaluator_window():
    for tf in ("1m", "5m", "15m", "1h", "4h", "1d", "1w"):
        tf_s = tf_seconds(tf)
        bar = floor_time(FIXED_END - 500 * tf_s, tf)
        for decision in (bar, bar + tf_s // 3):  # the bar's open time, or a fill time inside it (paper)
            for horizon in (24, 48):
                want = (bar + (horizon + 1) * tf_s) * 1000
                assert gate_target_ms(decision, tf, horizon) == want
                assert ev.gate_target_s(decision, tf, horizon) * 1000 == want


def test_every_gate_log_writer_stores_the_same_target(runtime_factory, no_background):
    t = floor_time(FIXED_END - 10 * H, "1h")  # the decision bar

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={SYM: 100.0})
            cur = make_current("BUY", time=t)
            # the bar-close watcher (engine)
            resp = {"symbol": SYM, "tf": "1h", "params": {"time_stop_bars": 48, "horizon": 5}, "current": cur}
            await NexusService(rt)._record(SYM, "1h", resp, [{"time": t, "kind": "BUY"}])
            # the paper account (an order filled 25 min into the decision bar's successor keeps the gate's bar)
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.open_position(1, SYM, "long", "1h", gate=make_gate("long", "TAKE", 100.0, time=t),
                                       check_gate=False, time_s=t + H + 1500)
            await engine.stop()
            # the scanner's deep mode
            await sc.ScannerService(rt)._log_gate(SYM, "1h", make_gate("long", "TAKE", 100.0, time=t),
                                                  {"current": cur}, 48)
            rows = await rt.db.fetchall("SELECT source, time, target_at FROM gate_log ORDER BY source")
            assert [(r["source"], int(r["time"])) for r in rows] == [("paper", t), ("scanner", t), ("watch", t)]
            assert {int(r["target_at"]) for r in rows} == {(t + 49 * H) * 1000}  # close of the 48th bar after t

    asyncio.run(scenario())


def test_nexus_says_once_that_a_quick_read_uses_old_candles(runtime_factory, no_background):
    cs = stale_set("No se pudo actualizar (OKX: HTTP 404); se muestran los datos guardados. "
                   "Datos desactualizados: última vela hace 22 días.")

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub, rt.ws = SeriesHub(cs), WSRecorder()
            rt.scanner = sc.ScannerService(rt)  # the real local fallback (offline: no TradingView)
            resp = await NexusService(rt).analyze(cs.symbol, "1h")
            assert resp["ok"] is False and resp["message"].startswith("Datos desactualizados")
            quick = resp["quick"]
            assert quick["stale"] is True and quick["price"] == float(cs.close[-1])
            assert quick["reasons"][0] == ("Datos desactualizados: última vela hace 22 días: "
                                           "esta lectura usa esas velas antiguas")
            assert sum("desactualizad" in r.lower() for r in quick["reasons"]) == 1

    asyncio.run(scenario())


def test_paper_closes_only_on_the_engine_exit_rule():
    """Long @ 100, stop 98, next target 102, price 101: the paper verdict matches nexus_decide's EV rule (EV <=
    EARLY_EV with the score against) for every forecast; NEXUS leaving the side always closes."""
    kw = {"sigma": 0.8, "horizon": 5}
    seen = set()
    for p_up in (0.2, 0.3, 0.4, 0.44, 0.46, 0.48, 0.5, 0.6):
        for score in (-0.6, -0.2, -0.01, 0.0, 0.3):
            ev_r, _ = ev_hold(1, 101.0, 98.0, 102.0, 2.0, p_up, 0.8, 5)
            info = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0,
                                      make_current("HOLD_LONG", prob_up=p_up, score=score), **kw)
            rule = ev_r <= EARLY_EV and score < 0
            assert info["early_exit"] is rule and info["exit_by"] == ("ev" if rule else None)
            assert info["ev_r"] == pytest.approx(ev_r)
            code = info["warnings"][0]["code"] if info["warnings"] else None
            if rule:
                assert code == "EARLY_EXIT" and info["advice"].startswith("Cerrar ya")
            elif ev_r <= -0.005:
                assert code == "WATCH_EXIT" and info["advice"].startswith("Vigila la salida")
            else:
                assert code is None and info["advice"].startswith("Mantener")
            seen.add("exit" if rule else "watch" if ev_r <= -0.005 else "hold")
            out = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0,
                                     make_current("EXIT_LONG", prob_up=p_up, score=score), **kw)
            assert out["early_exit"] and out["exit_by"] == "signal"
            assert out["advice"] == "Cerrar ya: NEXUS recomienda cerrar el largo (CERRAR LARGO)"
    assert seen == {"exit", "watch", "hold"}  # the grid covers the three outcomes
