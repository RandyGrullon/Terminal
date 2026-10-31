"""Semáforo rules with official dates only (weekly feed stale, no quotes): a known release still leads the headline
and sets the window; with nothing known the generic "Sin datos macro" stays. Plus the validation of fetched copies."""
from __future__ import annotations

import asyncio

from app import config
from app.config import DEFAULT_SETTINGS
from app.macro import calendar as cal
from app.macro import conditions as C
from app.macro import fomc
from app.macro import official as off
from app.macro.metrics import MetricsCollector
from app.macro.service import MacroService
from tests.test_imp_calendar_fakes import official_state
from tests.test_macro_fakes import Clock, FakeHTTP

SETTINGS = {**DEFAULT_SETTINGS, "ui.timezone": "America/New_York"}
CPI_DAY = "2026-11-10"  # official CPI at 08:30 New York


def official(now: int) -> list[dict]:
    return cal.merge_extra([], off.official_events(None, now - 86400, now + 30 * 86400))


def test_no_data_but_a_known_official_release_leads():
    now = fomc.ny_time(CPI_DAY, 7, 0)  # 90 min before
    cond = C.assess(official(now), {}, now, SETTINGS, C.global_scope(), calendar_stale=True)
    assert cond["level"] == "amber" and cond["stale"] is True
    assert cond["headline"] == "IPC de EE. UU. en 1 h 30 min: prepárate y no entres justo antes del dato."
    assert cond["advice"] == ("Reduce el tamaño a la mitad y usa stops más amplios; no abras nada nuevo desde las "
                              "8:00.")
    assert cond["reasons"][-1] == "Sin calendario semanal ni cotizaciones recientes: solo fechas oficiales"
    assert cond["next_event"]["source"] == "bls"
    after = fomc.ny_time(CPI_DAY, 9, 45)  # 75 min after: residual volatility
    late = C.assess(official(after), {}, after, SETTINGS, C.global_scope(), calendar_stale=True)
    assert late["level"] == "amber" and late["headline"].startswith("IPC de EE. UU. hace 1 h 15 min: volatilidad")
    quiet = fomc.ny_time("2026-11-11", 12, 0)
    nothing = C.assess(official(quiet), {}, quiet, SETTINGS, C.global_scope(), calendar_stale=True)
    assert nothing["level"] == "amber" and nothing["headline"] == C.NO_DATA_HEADLINE
    assert "Sin datos macro" in nothing["advice"]


def test_stale_week_with_quotes_names_the_official_dates():
    now = fomc.ny_time("2026-11-11", 12, 0)
    from tests.test_macro_fakes import metrics_with

    cond = C.assess(official(now), metrics_with(now), now, SETTINGS, C.global_scope(), calendar_stale=True)
    assert cond["level"] == "green"
    assert "Calendario semanal sin actualizar: solo se evalúan las fechas oficiales" in cond["reasons"]


def test_invalid_fetched_copies_are_rejected(monkeypatch, runtime_factory):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "BACKGROUND", False)
    good_bea = official_state()["sources"]["bea"]

    async def fetch_official():
        return {"bls": {"events": "nope"}, "bea": good_bea, "nasa": good_bea}

    async def scenario():
        async with runtime_factory() as rt:
            svc = MacroService(rt, clock=Clock(fomc.ny_time("2026-10-30", 12, 0)), monotonic=Clock(10.0),
                               fetch_official=fetch_official)
            svc.metrics = MetricsCollector(rt, client_factory=FakeHTTP({}), clock=svc._mono)
            assert await svc.refresh_official() is True
            assert set(svc.official["sources"]) == {"bea"} and svc._off_errors == {"bls": "copia no válida"}
            assert off.clean_copy("bls", {"events": [], "kinds": "x", "until": "y", "fetched_at": "z"}) == {
                "events": [], "kinds": [], "until": None, "fetched_at": 0}

    asyncio.run(scenario())
