"""Market-conditions check (semáforo) merged into live NEXUS decisions."""
from __future__ import annotations

import copy

from app.engine.macro_merge import merge_current, merge_gate


def gate(verdict="TAKE", score=80.0):
    return {
        "direction": "long", "verdict": verdict, "verdict_label": {"TAKE": "TOMAR", "WAIT": "ESPERAR", "AVOID": "NO TOMAR"}[verdict],
        "score": score, "win_prob": 0.6, "rr": 2.0, "ev_r": 0.3, "entry": 100.0, "sl": 98.0, "tp1": 102.0, "tp2": 104.0,
        "tp3": 106.0, "checks": [{"id": "regime", "label": "Régimen", "passed": True, "critical": False, "weight": 1.0,
                                   "detail": "Tendencia"}],
        "reasons_for": ["Tendencia a favor"], "reasons_against": [], "summary": "TOMAR", "time": 1,
    }


def check(level):
    passed = {"green": True, "amber": False, "red": False, None: None}[level]
    return {"id": "macro", "label": "Calendario y volatilidad", "passed": passed, "critical": level == "red",
            "weight": 1.0, "value": level, "detail": "FOMC en 18 min: no abras posiciones." if level == "red" else
            "VIX 23 (+14 %): volatilidad alta." if level == "amber" else "Sin eventos ni volatilidad extrema."}


def current(action="BUY"):
    return {"time": 1, "price": 100.0, "action": action, "action_label": "COMPRAR", "advice": "Comprar: filtro TOMAR.",
            "warnings": [], "gate": {"long": gate(), "short": gate("AVOID", 30.0)}}


def test_red_forces_avoid_and_wait():
    g = merge_gate(gate(), check("red"), 65.0)
    assert g["verdict"] == "AVOID" and g["verdict_label"] == "NO TOMAR" and g["score"] <= 25
    assert g["checks"][0]["id"] == "macro" and g["summary"].startswith("NO TOMAR: FOMC")
    cur = merge_current(current(), {"long": check("red"), "short": check("red")}, 65.0)
    assert cur["action"] == "WAIT" and cur["action_label"] == "ESPERAR" and "No abras" in cur["advice"]
    assert cur["warnings"][0] == {"code": "MACRO", "severity": "critical", "message": "Mercado: FOMC en 18 min: no abras posiciones."}


def test_amber_penalises_and_may_downgrade():
    strong = merge_gate(gate(score=90.0), check("amber"), 65.0)
    assert strong["verdict"] == "TAKE" and strong["score"] == 76.5
    weak = merge_gate(gate(score=70.0), check("amber"), 65.0)
    assert weak["verdict"] == "WAIT" and weak["score"] == 59.5 and weak["reasons_against"][0].startswith("VIX")
    cur = merge_current(current(), {"long": check("amber"), "short": check("amber")}, 65.0)
    assert cur["action"] == "BUY"  # 80 * 0.85 = 68 stays TAKE
    assert cur["warnings"][0]["severity"] == "warn"


def test_green_and_missing_data_do_not_change_decisions():
    g = merge_gate(gate(), check("green"), 65.0)
    assert g["verdict"] == "TAKE" and g["score"] == 80.0 and g["checks"][0]["passed"] is True
    assert merge_gate(gate(), check(None), 65.0)["score"] == 80.0
    cur = current()
    assert merge_current(cur, {"long": None, "short": None}, 65.0) is cur


def test_open_position_gets_warning_not_forced_exit():
    cur = merge_current(current("HOLD_LONG"), {"long": check("red"), "short": check("red")}, 65.0)
    assert cur["action"] == "HOLD_LONG" and "Protege la posición" in cur["advice"]


def test_inputs_are_not_mutated():
    cur = current()
    snapshot = copy.deepcopy(cur)
    merge_current(cur, {"long": check("red"), "short": check("red")}, 65.0)
    assert cur == snapshot
