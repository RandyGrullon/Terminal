"""Regression tests (engine fixes): an entry vetoed by the semáforo never leaves a phantom NEXUS position, neither
on the vetoed bar nor on later bars, and the veto survives a restart (macro-merge-position, contract I3)."""
from __future__ import annotations

import asyncio
import json

from app.config import DEFAULT_SETTINGS
from app.engine import service as service_mod
from app.engine.macro_merge import merge_current, veto_gate, vetoed_side
from app.engine.nexus import NexusParams, run_nexus
from app.engine.service import NexusService
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_fix_engine_watch import SeriesHub, WSRecorder, entry_bar, with_forming

SYM = "SIM:VETO"
FED = "Decisión de tipos de la Fed en 12 min: no abras posiciones."


class FakeMacro:
    def __init__(self, level: str | None = "red") -> None:
        self.level = level

    async def gate_check(self, symbol: str, direction: str = "long") -> dict | None:
        if self.level is None:
            return None
        return {"id": "macro", "label": "Calendario y volatilidad", "passed": self.level == "green",
                "critical": self.level == "red", "weight": 1.0, "value": self.level,
                "detail": FED if self.level == "red" else "Sin eventos ni volatilidad extrema."}


def series_and_entry():
    series = generate_candleset(n=700, seed=11, tf="1h", end_time=FIXED_END, symbol=SYM)
    k, action = entry_bar(series, 440, 690)
    return series, k, action, ("long" if action == "BUY" else "short")


def filled_from(resp: dict, t_fill: int) -> list[dict]:
    return [t for t in resp["trades"] if t["entry_time"] == t_fill]


def test_engine_rejects_a_vetoed_entry_and_keeps_learning_unchanged():
    series, k, action, side = series_and_entry()
    params = NexusParams.from_settings(DEFAULT_SETTINGS)
    t_k, t_next = int(series.time[k]), int(series.time[k + 1])
    veto = [{"time": t_k, "direction": side, "level": "red", "detail": FED}]
    window = series.upto(k)
    plain = run_nexus(window, params)
    vetoed = run_nexus(window, params, vetoed_entries=veto)
    assert plain.current["action"] == action and plain.current["position"]["entry_time"] == t_k
    cur = vetoed.current
    assert cur["action"] == "WAIT" and cur["position"] is None and cur["advice"].startswith("No operar")
    assert "Fed" in cur["advice"]
    assert vetoed.bars["pos_entry"][-1] is None and vetoed.bars["pos_sl"][-1] is None
    rejected = [m for m in vetoed.markers if m["time"] == t_k]
    assert [m["kind"] for m in rejected] == ["REJECTED_" + side.upper()] and "Fed" in rejected[0]["tooltip"]
    assert not [t for t in vetoed.trades if t["entry_time"] == t_k]
    # the probability model learns from prices only: the veto changes trades, not what was learned
    assert vetoed.learning["updates"] == plain.learning["updates"]
    assert vetoed.learning["weights"] == plain.learning["weights"]
    # later bars: the full run fills the entry decided at k on bar k+1; with the veto that trade never exists
    later = series.upto(k + 3)
    assert [t for t in run_nexus(later, params).trades if t["entry_time"] == t_next]
    assert not [t for t in run_nexus(later, params, vetoed_entries=veto).trades if t["entry_time"] == t_next]
    # deterministic, and a veto of a bar outside the window changes nothing
    again = run_nexus(window, params, vetoed_entries=veto)
    assert again.markers == vetoed.markers and again.trades == vetoed.trades
    far = run_nexus(window, params, vetoed_entries=[{"time": 1, "direction": side, "level": "red", "detail": "x"}])
    assert far.trades == plain.trades and far.markers == plain.markers


def test_closed_bar_veto_is_stored_and_applies_on_later_bars_and_after_restart(runtime_factory):
    series, k, action, side = series_and_entry()
    t_k, t_next = int(series.time[k]), int(series.time[k + 1])

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub, rt.ws, rt.macro = SeriesHub(series.upto(k)), WSRecorder(), FakeMacro("red")
            svc = rt.nexus = NexusService(rt)
            resp = await svc.process_closed_bar(SYM, "1h", upto=t_k)
            cur = resp["current"]
            assert cur["time"] == t_k and cur["action"] == "WAIT" and cur["position"] is None
            assert cur["warnings"][0]["code"] == "MACRO" and "Fed" in cur["advice"]
            assert resp["bars"]["pos_entry"][-1] is None and not filled_from(resp, t_k)
            assert [m["kind"] for m in resp["markers"] if m["time"] == t_k] == ["REJECTED_" + side.upper()]
            row = await rt.db.fetchone("SELECT state FROM model_state WHERE kind='nexus_veto' AND key=?",
                                       (f"{SYM}|1h",))
            stored = json.loads(row["state"])["entries"]
            assert [(e["time"], e["direction"], e["level"]) for e in stored] == [(t_k, side, "red")]
            log = await rt.db.fetchall("SELECT direction, taken, verdict FROM gate_log WHERE symbol=? AND time=?",
                                       (SYM, t_k))
            assert [(r["direction"], int(r["taken"]), r["verdict"]) for r in log] == [(side, 0, "AVOID")]
            assert not await rt.db.fetchall("SELECT id FROM signals WHERE symbol=?", (SYM,))

            # three bars later, semáforo green: no MANTENER/CERRAR advice for a position that was never opened
            rt.hub, rt.macro = SeriesHub(series.upto(k + 3)), FakeMacro("green")
            later = await svc.analyze(SYM, "1h")
            assert not filled_from(later, t_next)
            pos = later["current"]["position"]
            assert pos is None or pos["entry_time"] != t_next

            # a restart reads the stored veto back
            fresh = NexusService(rt)
            after = await fresh.analyze(SYM, "1h")
            assert not filled_from(after, t_next) and after["trades"] == later["trades"]

    asyncio.run(scenario())


def test_bar_close_veto_reaches_the_live_chart(runtime_factory):
    series, k, action, side = series_and_entry()
    t_k, t_forming = int(series.time[k]), int(series.time[k + 1])

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub, rt.ws, rt.macro = SeriesHub(with_forming(series, k)), WSRecorder(listeners=1), FakeMacro("red")
            svc = rt.nexus = NexusService(rt)
            resp = await svc.process_closed_bar(SYM, "1h", upto=t_k)
            assert resp["current"]["time"] == t_k and resp["current"]["action"] == "WAIT"
            msgs = [m for t, m in rt.ws.messages if t == f"market:{SYM}:1h" and m.get("type") == "nexus"]
            live = msgs[-1]["current"]
            # the live view (forming bar) does not hold the short/long the vetoed close would have opened
            assert live["time"] == t_forming and live["position"] is None
            assert not [m for m in msgs[-1]["markers"] if m["time"] == t_k and m["kind"] in ("BUY", "SELL")]
            chart = await svc.analyze(SYM, "1h")
            assert not filled_from(chart, t_forming) and chart["bars"]["pos_entry"][-1] is None

    asyncio.run(scenario())


def test_forming_bar_veto_is_only_for_that_view(runtime_factory, monkeypatch):
    series, k, action, side = series_and_entry()
    t_k = int(series.time[k])
    clock = {"now": t_k + 600}  # bar k opened 10 min ago: still forming
    monkeypatch.setattr(service_mod, "now_s", lambda: clock["now"])

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub, rt.ws, rt.macro = SeriesHub(series.upto(k)), WSRecorder(), FakeMacro("red")
            svc = rt.nexus = NexusService(rt)
            red = await svc.analyze(SYM, "1h")
            assert red["current"]["action"] == "WAIT" and red["current"]["position"] is None
            assert red["bars"]["pos_entry"][-1] is None
            assert not await rt.db.fetchone("SELECT state FROM model_state WHERE kind='nexus_veto'")
            rt.macro = FakeMacro("green")  # the window passed while the bar was still open: the entry stands
            green = await svc.analyze(SYM, "1h")
            assert green["current"]["action"] == action and green["current"]["position"]["entry_time"] == t_k
            # at the close the decision is taken again with the semáforo of that moment (red: stored veto)
            clock["now"] = t_k + 3600 + 5
            rt.macro = FakeMacro("red")
            closed = await svc.current(SYM, "1h", upto=t_k)
            assert closed["action"] == "WAIT" and closed["position"] is None
            assert await rt.db.fetchone("SELECT state FROM model_state WHERE kind='nexus_veto'")

    asyncio.run(scenario())


def _gate(verdict: str = "TAKE", score: float = 80.0) -> dict:
    labels = {"TAKE": "TOMAR", "WAIT": "ESPERAR", "AVOID": "NO TOMAR"}
    return {"direction": "long", "verdict": verdict, "verdict_label": labels[verdict], "score": score,
            "entry": 100.0, "sl": 98.0, "tp1": 102.0, "tp2": 104.0, "tp3": 106.0, "checks": [],
            "reasons_for": [], "reasons_against": [], "summary": labels[verdict], "time": 7}


def _check(level: str) -> dict:
    return {"id": "macro", "label": "Calendario y volatilidad", "passed": level == "green",
            "critical": level == "red", "weight": 1.0, "value": level, "detail": f"Detalle {level}."}


def test_veto_helpers_and_display_safety_net():
    cur = {"time": 7, "price": 100.0, "action": "BUY", "action_label": "COMPRAR", "advice": "Comprar.",
           "warnings": [], "gate": {"long": _gate(), "short": _gate("AVOID", 20.0)},
           "position": {"side": "long", "entry_time": 7, "entry_price": 100.0}}
    assert vetoed_side(cur, {"long": _check("red"), "short": _check("red")}, 65.0)[0] == "long"
    assert vetoed_side(cur, {"long": _check("amber"), "short": _check("amber")}, 65.0) is None  # 80 * 0.85 = 68
    weak = dict(cur, gate={"long": _gate(score=70.0), "short": _gate("AVOID", 20.0)})
    assert vetoed_side(weak, {"long": _check("amber"), "short": _check("amber")}, 65.0)[0] == "long"
    assert vetoed_side(cur, {"long": _check("green"), "short": _check("green")}, 65.0) is None
    assert vetoed_side(dict(cur, action="HOLD_LONG"), {"long": _check("red"), "short": None}, 65.0) is None
    merged = merge_current(cur, {"long": _check("red"), "short": _check("red")}, 65.0)
    assert merged["action"] == "WAIT" and merged["position"] is None and cur["position"] is not None
    stored = veto_gate(_gate(score=90.0), {"level": "amber", "detail": "VIX alto."}, 65.0)
    assert stored["verdict"] == "WAIT" and stored["checks"][0]["id"] == "macro"  # never TAKE once vetoed
    assert veto_gate(_gate(), {"level": "red", "detail": "FOMC."}, 65.0)["verdict"] == "AVOID"
