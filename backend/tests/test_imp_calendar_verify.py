"""Adversarial checks of the calendar beyond the current week (verifier): stale quotes must not lead a "no data"
semáforo, the approximate Bank of Japan time is never presented as exact, and the curated official dates are pinned
to what the official pages said when they were checked (a second, independent reading on 2026-10-31)."""
from __future__ import annotations

import asyncio
import datetime as dt

from app import config
from app.config import DEFAULT_SETTINGS
from app.macro import calendar as cal
from app.macro import conditions as C
from app.macro import fomc
from app.macro import official as off
from app.macro import schedules
from app.macro.alerts import AlertTracker
from app.macro.metrics import MetricsCollector
from app.macro.service import MacroService
from tests.test_macro_fakes import Clock, FakeHTTP, calm_quotes, metrics_with

SETTINGS = {**DEFAULT_SETTINGS, "ui.timezone": "America/New_York"}
QUIET = fomc.ny_time("2026-11-11", 12, 0)  # Wednesday: no official release within hours


def official(now: int) -> list[dict]:
    return cal.merge_extra([], off.official_events(None, now - 86400, now + 30 * 86400))


def stale_metrics(now: int, vix: float) -> dict:
    """Quotes that stopped refreshing an hour ago (MetricsCollector keeps the last good values in memory)."""
    quotes = calm_quotes()
    quotes["vix"] = dict(quotes["vix"], value=vix)
    return metrics_with(now - 3600, quotes=quotes)


def test_stale_amber_quotes_do_not_lead_a_no_data_semaforo():
    cond = C.assess(official(QUIET), stale_metrics(QUIET, 24.0), QUIET, SETTINGS, C.global_scope(),
                    calendar_stale=True)
    assert cond["level"] == "amber" and cond["stale"] is True
    assert cond["headline"] == C.NO_DATA_HEADLINE  # not "VIX 24,0: volatilidad alta" from hour-old quotes
    assert cond["advice"].startswith("Sin datos macro")
    red = C.assess(official(QUIET), stale_metrics(QUIET, 35.0), QUIET, SETTINGS, C.global_scope(),
                   calendar_stale=True)
    assert red["level"] == "red" and red["headline"].startswith("VIX 35,0")  # red still leads, as before
    cpi = fomc.ny_time("2026-11-10", 7, 0)  # official CPI in 90 min: the calendar finding leads
    soon = C.assess(official(cpi), stale_metrics(cpi, 24.0), cpi, SETTINGS, C.global_scope(), calendar_stale=True)
    assert soon["headline"].startswith("IPC de EE. UU. en 1 h 30 min")
    assert soon["advice"].endswith("no abras nada nuevo desde las 8:00.")


def test_gate_check_keeps_no_data_with_stale_amber_quotes(monkeypatch, runtime_factory):
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            svc = MacroService(rt, clock=Clock(QUIET), monotonic=Clock(10.0))
            svc.metrics = MetricsCollector(rt, client_factory=FakeHTTP({}), clock=svc._mono)
            snap = stale_metrics(QUIET, 24.0)
            svc.metrics.quotes, svc.metrics.quotes_at = snap["quotes"], snap["quotes_at"]
            await svc.start()
            check = await svc.gate_check("NASDAQ:AAPL", "long")
            assert check["passed"] is None and check["critical"] is False  # no data, not a failed check
            assert check["detail"] == C.NO_DATA_HEADLINE

    asyncio.run(scenario())


def test_bank_of_japan_time_is_said_to_be_approximate():
    boj = schedules.local_time("2026-12-18", 12, 0, schedules.TOKYO)  # 12:00 Tokyo is only a reference
    now = boj - 20 * 60
    settings = {**DEFAULT_SETTINGS, "ui.timezone": "Asia/Tokyo"}
    events = official(now)
    cond = C.assess(events, metrics_with(now), now, settings, C.scope_for_symbol("FX:USDJPY"), calendar_stale=True)
    assert cond["level"] == "red" and cond["blackout"]["active"]
    assert cond["headline"] == ("Decisión de tipos del Banco de Japón (hora aproximada) en 20 min: espera a que pase "
                                "la volatilidad.")
    assert cond["advice"] == ("No abras posiciones nuevas hasta las 12:30 (30 min después del dato; la hora del dato "
                              "es aproximada).")
    tracker = AlertTracker()
    tracker.check(cond, events, now - 60, C.settings_view(settings), {"JPY"})  # level alert first
    marks = tracker.check(cond, events, boj - 14 * 60, C.settings_view(settings), {"JPY"})
    assert [a["message"] for a in marks] == [
        "🔴 Decisión de tipos del Banco de Japón (hora aproximada) en 14 min: no abras posiciones nuevas."]
    cpi = fomc.ny_time("2026-11-10", 8, 10)  # an exact official time keeps the plain wording
    plain = C.assess(official(cpi), metrics_with(cpi), cpi, SETTINGS, C.global_scope(), calendar_stale=True)
    assert plain["advice"] == "No abras posiciones nuevas hasta las 9:00 (30 min después del dato)."
    assert "aproximada" not in plain["headline"]


# Dates read again on 2026-10-31 from the official pages named in schedules.py (BLS cpi.htm / empsit.htm, BEA
# schedule/full + next-year, ECB mgcgc calendar, BoE upcoming-mpc-dates, BoJ mpmsche_minu), with the app's client.
VERIFIED = {
    ("bls", "us_cpi"): ["2026-11-10", "2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13",
                        "2027-05-12", "2027-06-10", "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14",
                        "2027-11-10", "2027-12-10"],
    ("bls", "us_nfp"): ["2026-11-06", "2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02",
                        "2027-05-07", "2027-06-04", "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08",
                        "2027-11-05", "2027-12-03"],
    ("bea", "us_gdp"): ["2026-11-25", "2026-12-23", "2027-01-28", "2027-02-25", "2027-03-31", "2027-04-29",
                        "2027-05-27", "2027-06-25", "2027-07-29", "2027-08-25", "2027-09-30", "2027-10-28",
                        "2027-11-24", "2027-12-22"],
    ("ecb", "ecb_decision"): ["2026-12-17", "2027-02-04", "2027-03-18", "2027-04-29", "2027-06-10", "2027-07-22",
                              "2027-09-09", "2027-10-28", "2027-12-16"],
    ("boe", "boe_decision"): ["2026-11-05", "2026-12-17", "2027-02-04", "2027-03-18", "2027-04-29", "2027-06-17",
                              "2027-07-29", "2027-09-16", "2027-11-04", "2027-12-16"],
    ("boj", "boj_decision"): ["2026-12-18", "2027-01-22", "2027-03-18", "2027-04-28", "2027-06-11", "2027-07-22",
                              "2027-09-22", "2027-10-29", "2027-12-17"],
}
ZONES = {"bls": schedules.NEW_YORK, "bea": schedules.NEW_YORK, "ecb": schedules.FRANKFURT, "boe": schedules.LONDON,
         "boj": schedules.TOKYO}


def test_curated_dates_match_the_official_pages():
    events = schedules.static_events()
    for (source, kind), days in VERIFIED.items():
        got = [dt.datetime.fromtimestamp(e["time"], ZONES[source]).date().isoformat()
               for e in events if e["source"] == source and e["kind"] == kind]
        assert got == days, (source, kind)
    pce = [e for e in events if e["kind"] == "us_pce"]
    assert [dt.datetime.fromtimestamp(e["time"], schedules.NEW_YORK).date().isoformat() for e in pce] == VERIFIED[
        ("bea", "us_gdp")]  # BEA publishes PCE with every GDP estimate
    press = [e["time"] - 30 * 60 for e in events if e["kind"] == "ecb_press"]
    assert press == [e["time"] for e in events if e["kind"] == "ecb_decision"]  # 14:15 decision, 14:45 press
