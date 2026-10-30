"""Semáforo alerts: T−60 / T−15 / T−0 marks, blackout end, level changes, dedupe — with a fake clock."""
from __future__ import annotations

from app.config import DEFAULT_SETTINGS
from app.macro import conditions as C
from app.macro.alerts import LEVEL_COOLDOWN_S, AlertTracker
from tests.test_macro_conditions import NOW, ev, quotes
from tests.test_macro_fakes import metrics_with

SETTINGS = {**DEFAULT_SETTINGS, "ui.timezone": "America/New_York"}
CFG = C.settings_view(SETTINGS)
T = NOW + 3 * 3600  # CPI at 15:00 New York


def step(tracker: AlertTracker, events: list[dict], now: int, q: dict | None = None, cfg=CFG) -> list[dict]:
    cond = C.assess(events, metrics_with(now, q), now, SETTINGS, C.global_scope())
    return tracker.check(cond, events, now, cfg)


def kinds(alerts: list[dict]) -> list[str]:
    return [a["kind"] for a in alerts]


def test_event_marks_fire_once_each():
    events = [ev("CPI m/m", "USD", 180)]
    tr = AlertTracker()
    assert step(tr, events, T - 2 * 3600 - 60) == []  # green, nothing to say
    a = step(tr, events, T - 90 * 60)
    assert kinds(a) == ["level"] and a[0]["level"] == "amber"  # 90 min before: precaución
    a = step(tr, events, T - 60 * 60)
    assert kinds(a) == ["event_soon"] and a[0]["level"] == "amber"
    assert a[0]["message"] == "🟡 IPC de EE. UU. en 1 h: prepárate, desde las 14:30 no abras posiciones nuevas."
    assert step(tr, events, T - 59 * 60) == []
    a = step(tr, events, T - 30 * 60)
    assert kinds(a) == ["level"] and a[0]["level"] == "red"  # blackout starts
    assert "No abras posiciones nuevas hasta las 15:30" in a[0]["message"]
    a = step(tr, events, T - 15 * 60)
    assert kinds(a) == ["event_soon"] and a[0]["level"] == "red"
    assert a[0]["message"] == "🔴 IPC de EE. UU. en 15 min: no abras posiciones nuevas."
    assert step(tr, events, T - 14 * 60) == []
    a = step(tr, events, T)
    assert kinds(a) == ["event_now"]
    assert a[0]["message"] == "🔴 IPC de EE. UU.: se publica ahora. No abras posiciones nuevas hasta las 15:30."
    assert step(tr, events, T + 60) == []
    a = step(tr, events, T + 31 * 60)
    assert kinds(a) == ["blackout_end"] and a[0]["level"] == "amber"  # residual volatility window
    assert "Terminó la ventana sin operar (IPC de EE. UU. (mensual))" in a[0]["message"]
    assert step(tr, events, T + 32 * 60) == []
    a = step(tr, events, T + 2 * 3600 + 60)
    assert kinds(a) == ["level"] and a[0]["level"] == "green"


def test_restart_mid_countdown_does_not_replay_old_marks():
    events = [ev("CPI m/m", "USD", 180)]
    tr = AlertTracker()
    a = step(tr, events, T - 10 * 60)
    assert kinds(a) == ["level"]  # the T−15 reminder is merged into the level alert (same event)
    assert "IPC de EE. UU. en 10 min" in a[0]["message"] and a[0]["level"] == "red"
    assert step(tr, events, T - 5 * 60) == []
    assert kinds(step(tr, events, T + 30)) == ["event_now"]
    late = AlertTracker()
    assert "event_now" not in kinds(step(late, events, T + 10 * 60))  # too late for T−0


def test_only_alert_currencies_and_one_alert_per_cluster():
    events = [ev("Main Refinancing Rate", "EUR", 180), ev("CPI m/m", "USD", 180), ev("Core CPI m/m", "USD", 180),
              ev("CPI y/y", "USD", 180)]
    tr = AlertTracker()
    assert kinds(step(tr, events, T - 61 * 60)) == ["level"]
    a = step(tr, events, T - 60 * 60)
    assert kinds(a) == ["event_soon"]
    assert a[0]["message"].startswith("🟡 IPC subyacente de EE. UU. y 2 datos más en 1 h")
    both = C.settings_view({**SETTINGS, "macro.currencies": ["USD", "EUR"]})
    tr = AlertTracker()
    step(tr, events, T - 61 * 60, cfg=both)
    assert kinds(step(tr, events, T - 60 * 60, cfg=both)) == ["event_soon", "event_soon"]


def test_level_alerts_have_cooldown_except_escalation_to_red():
    tr = AlertTracker()
    t0 = NOW
    assert step(tr, [], t0) == []
    a = step(tr, [], t0 + 60, quotes(vix={"value": 23.0}))
    assert kinds(a) == ["volatility"] and a[0]["level"] == "amber"
    assert a[0]["message"].startswith("🟡 Precaución: VIX 23,0")
    assert kinds(step(tr, [], t0 + 120)) == ["level"]  # back to green
    assert step(tr, [], t0 + 180, quotes(vix={"value": 23.0})) == []  # flapping: amber already announced
    a = step(tr, [], t0 + 240, quotes(vix={"value": 33.0}))
    assert kinds(a) == ["volatility"] and a[0]["level"] == "red"
    assert step(tr, [], t0 + 300) == []  # green again within the cooldown of the last green alert
    a = step(tr, [], t0 + 300 + LEVEL_COOLDOWN_S, quotes(vix={"value": 23.0}))
    assert kinds(a) == ["volatility"]


def test_first_observation_reports_only_non_green():
    assert step(AlertTracker(), [], NOW) == []
    a = step(AlertTracker(), [], NOW, quotes(es={"change_pct": -3.4}))
    assert a[0]["level"] == "red" and "Futuros S&P 500 -3,4 %" in a[0]["message"]
