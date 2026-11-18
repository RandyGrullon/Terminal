"""NEXUS automática's New York session mode: the aggressive mode applies on its own while the New York stock session is
open (Monday to Friday 9:30–16:00 New York time, NYSE holidays and early closes) and its saved settings outside it; it
notes each open and close in its decision log. Also the market hours the page shows (crypto 24/7, CME futures)."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app import config as nexus_config
from app.sim import market_session as MS
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

NY = MS.NEW_YORK


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=NY).timestamp())


def test_new_york_session_holidays_early_closes_and_daylight_saving():
    tue_open, tue_close = ts(2026, 11, 17, 9, 30), ts(2026, 11, 17, 16, 0)
    assert MS.ny_session(ts(2026, 11, 17, 9, 0)) == {"open": False, "opens_at": tue_open, "closes_at": tue_close,
                                                      "early_close": False, "known": True}
    assert MS.ny_session(ts(2026, 11, 17, 12, 0))["open"] is True
    after = MS.ny_session(ts(2026, 11, 17, 16, 0))  # the close itself: next session
    assert (after["open"], after["opens_at"]) == (False, ts(2026, 11, 18, 9, 30))
    # Thanksgiving (Thursday 26) closed, Friday 27 closes at 13:00
    thanks = MS.ny_session(ts(2026, 11, 25, 18, 0))
    assert (thanks["opens_at"], thanks["closes_at"], thanks["early_close"]) == (
        ts(2026, 11, 27, 9, 30), ts(2026, 11, 27, 13, 0), True)
    assert MS.ny_session(ts(2026, 11, 27, 13, 30))["opens_at"] == ts(2026, 11, 30, 9, 30)  # the weekend skipped
    # daylight saving time (14 March 2027): 9:30 in New York is 13:30 UTC instead of 14:30
    spring = MS.ny_session(ts(2027, 3, 12, 17, 0))
    assert spring["opens_at"] - ts(2027, 3, 15, 0, 0) == 9 * 3600 + 1800
    assert spring["opens_at"] == int(datetime(2027, 3, 15, 13, 30, tzinfo=MS.ZoneInfo("UTC")).timestamp())
    assert MS.ny_session(ts(2028, 1, 3, 8, 0))["known"] is False  # past the holiday lists


def test_cme_futures_week_and_markets_view():
    assert MS.cme_session(ts(2026, 11, 17, 12, 0)) == {"open": True, "opens_at": None,
                                                        "closes_at": ts(2026, 11, 17, 17, 0)}
    assert MS.cme_session(ts(2026, 11, 17, 17, 30)) == {"open": False, "opens_at": ts(2026, 11, 17, 18, 0),
                                                         "closes_at": None}
    assert MS.cme_session(ts(2026, 11, 17, 18, 30))["closes_at"] == ts(2026, 11, 18, 17, 0)
    assert MS.cme_session(ts(2026, 11, 20, 17, 30))["opens_at"] == ts(2026, 11, 22, 18, 0)  # Friday → Sunday
    assert MS.cme_session(ts(2026, 11, 22, 19, 0))["closes_at"] == ts(2026, 11, 23, 17, 0)
    view = {m["key"]: m for m in MS.markets_view(ts(2026, 11, 27, 10, 0))}
    assert view["crypto"]["open"] and view["crypto"]["note"] == "24 horas, 7 días"
    assert view["ny"]["open"] and view["ny"]["closes_at"] == ts(2026, 11, 27, 13, 0)
    assert "hoy cierra a las 13:00" in view["ny"]["note"]
    assert view["cme"]["open"] and "sin festivos" in view["cme"]["note"]


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_aggressive_only_while_new_york_is_open(runtime_factory, no_background, monkeypatch):
    state = {"open": False}

    def fake_session(now_s: int) -> dict:
        return {"open": state["open"], "opens_at": 1_900_000_000, "closes_at": 1_900_023_400, "early_close": False,
                "known": True}

    monkeypatch.setattr(pp.MS, "ny_session", fake_session)

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={"BINANCE:BTCUSDT.P": 100.0})
            rt.nexus = BarNexus(default=_signal("BUY", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()

            def flip(is_open: bool) -> None:
                state["open"] = is_open
                engine._ny_cache = None

            snap = await engine.set_autopilot(enabled=True, source="list", symbols=["SIM:NEXUS"],
                                              session_aggressive=True)
            st = snap["autopilot"]
            assert st["session_aggressive"] is True and rt.settings.get("autopilot.session_aggressive") is True
            assert st["session"]["enabled"] and not st["session"]["active"] and not st["session"]["open"]
            assert [m["key"] for m in st["markets"]] == ["crypto", "ny", "cme"]
            # closed: its saved settings
            assert (engine._autopilot_cfg()["tfs"], engine._autopilot_cfg()["risk_pct"]) == (["1h"], 1.0)
            assert (await engine.risk_state(3))["daily_loss_limit_pct"] == 3.0
            await engine._session_tick()  # first look: no note
            # the session opens: aggressive mode, noted once
            flip(True)
            await engine._session_tick()
            cfg = engine._autopilot_cfg()
            assert (cfg["tfs"], cfg["risk_pct"], cfg["lots"]) == (["5m", "15m", "1h"], 4.0, True)
            assert (await engine.risk_state(3))["daily_loss_limit_pct"] == 20.0
            assert (await engine.risk_state(1))["daily_loss_limit_pct"] == 3.0  # the user's demo account never
            assert engine.autopilot_status()["session"]["active"] is True
            assert engine.autopilot_status()["log"][0]["text"].startswith("Abre la sesión de Nueva York: modo agresivo")
            await engine._session_tick()
            assert sum("Abre la sesión" in e["text"] for e in engine.autopilot_status()["log"]) == 1
            # it closes: back to its saved settings
            flip(False)
            await engine._session_tick()
            assert engine._autopilot_cfg()["risk_pct"] == 1.0
            assert engine.autopilot_status()["log"][0]["text"].startswith("Cierra la sesión de Nueva York: vuelve a")
            # switched off while open: saved settings even in the session
            flip(True)
            await engine._session_tick()
            await engine.set_autopilot(session_aggressive=False)
            await engine._session_tick()
            assert engine._autopilot_cfg()["risk_pct"] == 1.0
            assert "desactivado" in engine.autopilot_status()["log"][0]["text"]
            await engine.stop()

    asyncio.run(scenario())
