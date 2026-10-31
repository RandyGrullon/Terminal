"""MacroService and MetricsCollector with fakes (no network): refresh cycle, alerts on bus + WS, persistence of
the calendar, offline restart, gate_check mapping, scoped conditions and the calendar API of the service."""
from __future__ import annotations

import asyncio

import pytest

from app import config
from app.macro import calendar as cal
from app.macro import fomc
from app.macro.metrics import MetricsCollector, parse_fng, parse_funding, parse_quotes
from app.macro.service import MacroService
from tests.test_macro_fakes import (FF_SAMPLE, FNG_SAMPLE, OKX_SAMPLE, TV_ROWS, Clock, FakeHTTP, FakeHub,
                                    FakeResponse)

FOMC_MINUS_20 = fomc.ny_time("2026-10-28", 13, 40)


class WSRecorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    def publish(self, topic: str, message: dict) -> None:
        self.messages.append((topic, message))

    def macro(self) -> list[dict]:
        return [m for t, m in self.messages if t == "macro"]


def crypto_http() -> FakeHTTP:
    return FakeHTTP({"alternative.me": FakeResponse(200, FNG_SAMPLE), "okx.com": FakeResponse(200, OKX_SAMPLE)})


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(config, "BACKGROUND", False)


def make_service(rt, now: int, feed=None, http: FakeHTTP | None = None, official=None):
    """Service with fake sources only (never the network): `official` = what the live official calendars return
    (default: every source fails, so the curated dates of schedules.py apply)."""
    calls = {"feed": 0, "official": 0}

    async def fetch():
        calls["feed"] += 1
        if isinstance(feed, Exception):
            raise feed
        return cal.parse_feed(FF_SAMPLE if feed is None else feed)

    async def fetch_official():
        calls["official"] += 1
        if official is None:
            return {source: cal.CalendarError("sin conexión (prueba)") for source in ("bls", "bea")}
        return official

    svc = MacroService(rt, clock=Clock(now), monotonic=Clock(1000.0), fetch_feed=fetch, fetch_official=fetch_official)
    svc.metrics = MetricsCollector(rt, client_factory=http or crypto_http(), clock=svc._mono)
    return svc, calls


def test_parsers_on_real_samples():
    q = parse_quotes({"rows": TV_ROWS})
    assert set(q) == {"vix", "es", "nq", "dxy", "us10y", "us02y", "move", "oil", "gold", "btc_d"}
    assert q["vix"] == {"value": 22.29, "change_pct": 29.668411867364732, "change_abs": 5.1, "high": 23.93,
                        "low": 18.03}
    assert parse_fng(FNG_SAMPLE) == {"value": 30, "classification": "Fear", "time": 1793318400}
    assert parse_funding(OKX_SAMPLE) == {"rate": 0.0001, "interval_h": 8.0, "time": 1793404800000}
    assert parse_quotes({"rows": [{"symbol": "TVC:VIX", "close": None}]}) == {}
    assert parse_fng({"data": []}) is None and parse_funding({"code": "50011", "data": []}) is None


def test_metrics_collector_cadence_and_offline(online, monkeypatch):
    async def scenario():
        hub, http, mono = FakeHub(), crypto_http(), Clock(0.0)
        rt = type("RT", (), {"hub": hub, "bus": None})()
        mc = MetricsCollector(rt, client_factory=http, clock=mono)
        assert await mc.refresh() == ["quotes", "fng", "funding"]
        assert hub.scans[0][0] == "global" and "TVC:VIX" in hub.scans[0][2]
        assert mc.snapshot()["funding"]["BTC"]["rate"] == 0.0001 and mc.fng["value"] == 30
        assert len(http.calls) == 3  # F&G + BTC + ETH funding
        assert await mc.refresh() == []
        mono.advance(61)
        assert await mc.refresh() == ["quotes"]
        mono.advance(600)
        assert await mc.refresh() == ["quotes", "funding"]
        mono.advance(3600)
        monkeypatch.setattr(config, "OFFLINE", True)
        assert await mc.refresh() == [] and len(hub.scans) == 3

    asyncio.run(scenario())


def test_refresh_cycle_alerts_persistence_and_status(online, runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.set("ui.timezone", "America/New_York")
            rt.hub, rt.ws = FakeHub(), WSRecorder()
            svc, calls = make_service(rt, FOMC_MINUS_20)
            await svc.start()
            assert svc.cache is None and svc.status()["level"] is None
            cond = await svc.refresh()
            assert calls["feed"] == 1 and cond["level"] == "red" and cond["blackout"]["active"]
            assert cond["advice"] == "No abras posiciones nuevas hasta las 15:00 (30 min después del dato)."
            assert {t for t, _ in rt.ws.messages} == {"macro", "events"}  # alerts also reach the event log
            alerts = [m["alert"] for m in rt.ws.macro() if m["alert"]]
            assert alerts and alerts[0]["kind"] == "level" and alerts[0]["level"] == "red"
            assert rt.ws.macro()[-1]["alert"] is None and rt.ws.macro()[-1]["type"] == "macro"
            events = rt.bus.recent(50, source="macro")
            assert any(e["level"] == "warn" and "No operar ahora" in e["message"] for e in events)
            stored = await cal.load_cached(rt.db)
            assert stored is not None and len(stored["events"]) == len(FF_SAMPLE)
            st = svc.status()
            assert st["running"] and st["level"] == "red" and st["calendar_events"] == len(FF_SAMPLE)
            assert st["stale"] is False and st["next_event"] == "Decisión de tipos de la Fed en 20 min"
            await svc.refresh()
            assert calls["feed"] == 1  # the feed is refreshed at most once per hour
            rt.ws.messages.clear()
            svc._time.t = fomc.ny_time("2026-10-28", 15, 1)
            await svc.refresh()
            kinds = [m["alert"]["kind"] for m in rt.ws.macro() if m["alert"]]
            assert "blackout_end" in kinds
            await rt.settings.set("macro.alerts", False)
            n_events = len(rt.bus.recent(500, source="macro"))
            svc._time.t = fomc.ny_time("2026-10-29", 8, 15)  # GDP + PCE in 15 min
            cond = await svc.refresh()
            assert cond["level"] == "red" and len(rt.bus.recent(500, source="macro")) == n_events
            assert rt.ws.macro()[-1]["alert"] is None
            await svc.stop()
            assert svc.status()["running"] is False

    asyncio.run(scenario())


def test_offline_restart_uses_stored_calendar(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            await cal.save_cached(rt.db, cal.make_cache(cal.parse_feed(FF_SAMPLE)))
            monkeypatch.setattr(config, "OFFLINE", True)
            svc, calls = make_service(rt, fomc.ny_time("2026-10-29", 8, 10), feed=RuntimeError("no network"))
            await svc.start()
            cond = await svc.refresh()
            assert calls["feed"] == 0 and svc.metrics.quotes == {}
            assert cond["level"] == "red" and cond["stale"] is True  # no quotes offline
            assert cond["headline"].startswith("Inflación PCE subyacente de EE. UU. y 1 dato más en 20 min")
            svc._time.t = fomc.ny_time("2026-11-02", 9, 0)  # next week: the stored copy is stale
            svc._cond.clear()
            later = await svc.conditions()
            assert later["stale"] is True and later["level"] == "amber"
            assert later["headline"] == "Sin datos macro: opera con cautela."
            check = await svc.gate_check("NASDAQ:AAPL", "long")
            assert check["passed"] is None and check["critical"] is False and check["value"] == "amber"

    asyncio.run(scenario())


def test_calendar_failure_keeps_cache_and_retries_later(online, runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            await cal.save_cached(rt.db, cal.make_cache(cal.parse_feed(FF_SAMPLE)))
            rt.hub = FakeHub()
            svc, calls = make_service(rt, FOMC_MINUS_20, feed=cal.CalendarError("HTTP 429"))
            await svc.start()
            await svc.refresh()
            await svc.refresh()
            assert calls["feed"] == 1 and len(svc.cache["events"]) == len(FF_SAMPLE)
            warns = [e for e in rt.bus.recent(50, source="macro") if "calendario económico no disponible" in e["message"]]
            assert len(warns) == 1 and "uso la copia guardada" in warns[0]["message"]
            svc._mono.advance(cal.RETRY_S + 1)
            await svc.refresh()
            assert calls["feed"] == 2

    asyncio.run(scenario())


def test_gate_check_scopes_and_calendar(online, runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(infos={"XETR:SAP": {"type": "stock", "currency": "EUR", "market": "germany"}})
            svc, _ = make_service(rt, FOMC_MINUS_20)
            await svc.start()
            await svc.refresh()
            check = await svc.gate_check("BINANCE:BTCUSDT", "long")
            assert check == {"id": "macro", "label": "Calendario y volatilidad", "passed": False, "critical": True,
                             "weight": 1.0, "value": "red", "detail": check["detail"]}
            assert check["detail"].startswith("Decisión de tipos de la Fed en 20 min")
            assert await svc.gate_check("SIM:NEXUS", "short") is None
            fx = await svc.conditions("FX:EURUSD")
            assert fx["scope"] == "forex:EUR/USD" and fx["symbol"] == "FX:EURUSD"
            sap = await svc.conditions("XETR:SAP")
            assert sap["scope"] == "stocks:EUR" and rt.hub.info_calls == ["XETR:SAP"]
            assert (await svc.conditions("BINANCE:ETHUSDT"))["symbol"] == "BINANCE:ETHUSDT"
            assert (await svc.conditions("BINANCE:BTCUSDT"))["scope"] == "crypto"
            week = await svc.calendar(days=7)
            assert week and all(e["impact"] in ("high", "medium") for e in week)
            assert {e["title"] for e in week[:2]} == {"FOMC Statement", "Federal Funds Rate"}
            assert week[0]["countdown_s"] == 1200 and week[0]["source"] == "forexfactory"
            assert any(e["source"] == "federalreserve" for e in await svc.calendar(days=45, impacts=("high",)))
            eur = await svc.calendar(days=7, impacts=None, currencies=["EUR"])
            assert {e["currency"] for e in eur} == {"EUR"}
            await rt.settings.set("macro.enabled", False)
            assert await svc.gate_check("BINANCE:BTCUSDT", "long") is None

    asyncio.run(scenario())
