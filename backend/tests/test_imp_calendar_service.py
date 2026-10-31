"""The calendar beyond the current week through the service, the REST API and the terminal (fakes, no network):
next week's official releases drive the semáforo's warn / block windows and alerts once the weekly feed ends, the
live official calendars refresh, fail and persist on their own, and every event says where it comes from."""
from __future__ import annotations

import asyncio

import pytest

from app import config
from app.macro import calendar as cal
from app.macro import fomc
from app.macro import official as off
from app.macro.metrics import MetricsCollector
from app.macro.service import MacroService
from tests.test_imp_calendar_fakes import BEA_ICS, official_state
from tests.test_macro_fakes import FF_SAMPLE, Clock, FakeHTTP

FRIDAY_NOON = fomc.ny_time("2026-10-30", 12, 0)  # the weekly feed (25-31 Oct) knows nothing about next week
NFP_MINUS_20 = fomc.ny_time("2026-11-06", 8, 10)  # official Employment Situation at 08:30 New York
THURSDAY_NOON = fomc.ny_time("2026-10-29", 12, 0)  # the feed still has events ahead (ECI, BoJ...)
EVENT_KEYS = set(cal.EVENT_FIELDS) | {"countdown_s"}
SOURCE_IDS = ["forexfactory", "federalreserve", "bls", "bea", "ecb", "boe", "boj"]


async def start_service(rt, now: int, official=None):
    """Started service on fakes: the stored weekly feed is FF_SAMPLE (week of 25 Oct), `official` = what
    fetch_official returns (callable or value; default: every live source fails), metrics never use the network."""
    await cal.save_cached(rt.db, cal.make_cache(cal.parse_feed(FF_SAMPLE)))
    calls = {"official": 0}

    async def fetch_feed():
        return cal.parse_feed(FF_SAMPLE)

    async def fetch_official():
        calls["official"] += 1
        result = official() if callable(official) else official
        if result is None:
            return {source: cal.CalendarError("sin conexión (prueba)") for source in off.SOURCES}
        if isinstance(result, Exception):
            raise result
        return result

    svc = MacroService(rt, clock=Clock(now), monotonic=Clock(1000.0), fetch_feed=fetch_feed,
                       fetch_official=fetch_official)
    svc.metrics = MetricsCollector(rt, client_factory=FakeHTTP({}), clock=svc._mono)
    await svc.start()
    return svc, calls


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "BACKGROUND", False)


def test_friday_sees_next_week_and_the_semaforo_blocks_on_it(offline, runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.set("ui.timezone", "America/New_York")
            svc, calls = await start_service(rt, FRIDAY_NOON)
            week = await svc.calendar(days=14, impacts=("high",), currencies=["USD"])
            nxt = [e for e in week if e["time"] > fomc.ny_time("2026-11-01", 0, 0)]
            assert [(e["source"], e["title_es"]) for e in nxt[:2]] == [
                ("bls", "Informe de empleo de EE. UU. (nóminas NFP y desempleo)"),
                ("bls", "IPC de EE. UU. (inflación)")]
            assert all(set(e) == EVENT_KEYS for e in week) and calls["official"] == 0  # offline: no fetch
            everyone = await svc.calendar(days=14, impacts=("high",), currencies=["USD", "GBP"])
            assert any(e["source"] == "boe" and e["currency"] == "GBP" for e in everyone)
            cond = await svc.conditions()
            assert cond["next_event"]["source"] == "bls" and cond["next_event"]["time"] == NFP_MINUS_20 + 1200
            assert any("Informe de empleo de EE. UU." in r for r in cond["reasons"])

            svc._time.t = NFP_MINUS_20  # the weekly copy is stale now: only the official dates remain
            svc._cond.clear()
            cond = await svc.conditions()
            assert cond["level"] == "red" and cond["blackout"]["active"] and cond["stale"] is True
            assert cond["headline"] == "Informe de empleo de EE. UU. en 20 min: espera a que pase la volatilidad."
            assert cond["advice"] == "No abras posiciones nuevas hasta las 9:00 (30 min después del dato)."
            check = await svc.gate_check("BINANCE:BTCUSDT", "long")
            assert check["critical"] is True and check["passed"] is False
            eur = await svc.conditions("FX:EURUSD")
            assert eur["level"] == "red"  # EUR/USD also follows USD events
            svc._time.t = fomc.ny_time("2026-11-06", 6, 45)  # 1 h 45 min before: inside the warn window
            svc._cond.clear()
            soon = await svc.conditions()
            assert soon["level"] == "amber" and soon["headline"].startswith("Informe de empleo de EE. UU. en 1 h 45")
            assert soon["advice"].endswith("no abras nada nuevo desde las 8:00.")  # the official date sets it
            assert "Sin calendario semanal ni cotizaciones recientes: solo fechas oficiales" in soon["reasons"]
            assert (await svc.gate_check("NASDAQ:AAPL", "long"))["passed"] is False  # a known release: not "no data"

    asyncio.run(scenario())


def test_alerts_count_down_to_an_official_release(offline, runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc, _ = await start_service(rt, fomc.ny_time("2026-11-10", 7, 31))  # CPI at 08:30
            await svc.refresh()
            svc._time.t = fomc.ny_time("2026-11-10", 8, 15)
            await svc.refresh()
            svc._time.t = fomc.ny_time("2026-11-10", 8, 30)
            await svc.refresh()
            texts = [e["message"] for e in rt.bus.recent(100, source="macro")]
            assert any("IPC de EE. UU. en 59 min" in t or "IPC de EE. UU. en 1 h" in t for t in texts)
            assert any(t.startswith("🔴 IPC de EE. UU.: se publica ahora") for t in texts)

    asyncio.run(scenario())


def test_refresh_official_partial_failure_retry_and_persistence(online, runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            answers = {"value": {"bls": official_state()["sources"]["bls"], "bea": cal.CalendarError("HTTP 403")}}
            svc, calls = await start_service(rt, FRIDAY_NOON, official=lambda: answers["value"])
            assert await svc.refresh_official() is True and calls["official"] == 1
            assert set(svc.official["sources"]) == {"bls"} and svc._off_errors == {"bea": "HTTP 403"}
            warns = [e["message"] for e in rt.bus.recent(50, source="macro")]
            assert warns == ["Semáforo: calendario oficial del BEA no disponible (HTTP 403); uso las fechas oficiales "
                             "verificadas el 31/10/2026."]
            assert await svc.refresh_official() is False and calls["official"] == 1  # not due yet
            stored = await off.load_official(rt.db)
            assert set(stored["sources"]) == {"bls"}
            status = svc.status()
            assert status["official_errors"] == {"bea": "HTTP 403"} and status["official_events"] > 0
            cov = svc.coverage()
            assert [s["id"] for s in cov["sources"]] == SOURCE_IDS
            rows = {s["id"]: s for s in cov["sources"]}
            assert rows["bls"]["ok"] is True and rows["bls"]["updated_at"] == official_state()["sources"]["bls"][
                "fetched_at"]
            assert rows["bea"]["ok"] is False and rows["bea"]["error"] == "HTTP 403" and rows["bea"]["live"] is True
            assert rows["ecb"]["live"] is False and rows["ecb"]["ok"] is True
            assert cov["weekly_until"] == svc.cache["week_end"] and cov["weekly_stale"] is False
            assert cov["verified"] == "2026-10-31"

            svc._mono.advance(off.RETRY_S + 1)
            answers["value"] = {"bls": cal.CalendarError("HTTP 503"),
                                "bea": off.build_copy("bea", off.parse_ics(BEA_ICS), 5, FRIDAY_NOON)}
            assert await svc.refresh_official() is True and calls["official"] == 2
            assert set(svc.official["sources"]) == {"bls", "bea"}  # the failing BLS keeps its stored copy
            assert svc._off_errors == {"bls": "HTTP 503"}
            warns = [e["message"] for e in rt.bus.recent(50, source="macro")]
            assert any("calendario oficial del BLS no disponible (HTTP 503); uso la copia guardada." in w for w in warns)

            svc._mono.advance(off.RETRY_S + 1)
            answers["value"] = RuntimeError("boom")
            assert await svc.refresh_official() is False and set(svc.official["sources"]) == {"bls", "bea"}
            assert svc._off_errors["bea"] == "RuntimeError: boom"

            fresh, _ = await start_service(rt, FRIDAY_NOON)
            assert set(fresh.official["sources"]) == {"bls", "bea"}

    asyncio.run(scenario())


def test_offline_service_never_fetches_official(offline, runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc, calls = await start_service(rt, FRIDAY_NOON, official=official_state()["sources"])
            assert await svc.refresh_official(force=True) is False
            await svc.refresh(force=True)
            assert calls["official"] == 0 and svc.official is None

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- REST API and terminal


def _prepare(now: int):
    from app.runtime import rt

    assert rt.macro is not None, rt.load_errors
    rt.macro._time = Clock(now)
    rt.macro.cache = cal.make_cache(cal.parse_feed(FF_SAMPLE))
    rt.macro._cond.clear()
    return rt


def test_calendar_endpoint_next_week_sources_and_coverage(api_client):
    rt = _prepare(THURSDAY_NOON)
    r = api_client.get("/api/macro/calendar", params={"days": 14, "impact": "high"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"events", "coverage"}
    events = body["events"]
    assert all(set(e) == EVENT_KEYS for e in events)
    nfp = [e for e in events if e["source"] == "bls" and e["title"] == "Employment Situation"]
    assert len(nfp) == 1 and nfp[0]["time"] == NFP_MINUS_20 + 1200 and nfp[0]["impact"] == "high"
    assert {e["source"] for e in events} >= {"forexfactory", "bls"}
    cov = body["coverage"]
    assert [s["id"] for s in cov["sources"]] == SOURCE_IDS
    assert cov["weekly_until"] == rt.macro.cache["week_end"] and cov["weekly_stale"] is False
    assert {k for s in cov["sources"] for k in s} == {"id", "label", "live", "ok", "updated_at", "until", "error"}
    eur = api_client.get("/api/macro/calendar", params={"days": 60, "impact": "high", "currency": "EUR"}).json()
    ecb = [e for e in eur["events"] if e["source"] == "ecb"]
    assert [e["title_es"] for e in ecb] == ["Decisión de tipos del BCE", "Rueda de prensa del BCE"]


def test_terminal_calendar_shows_sources_and_coverage(api_client):
    _prepare(THURSDAY_NOON)
    res = api_client.post("/api/terminal", json={"text": "calendario 10"}).json()
    assert res["ok"] is True
    text = "\n".join(line["text"] for line in res["lines"])
    assert "Fuente" in text and "BLS" in text and "Informe de empleo de EE. UU." in text
    assert "Los datos semanales (FF) llegan hasta el" in text and "fechas oficiales (Fed, BLS, BEA, BCE" in text
    assert {e["source"] for e in res["data"]["events"]} >= {"forexfactory", "bls"}
    assert [s["id"] for s in res["data"]["coverage"]["sources"]] == SOURCE_IDS
