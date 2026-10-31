"""Validation feature: ValidationService jobs (one at a time, persisted in model_state kind 'validation'), the REST
endpoints and the terminal command `validar`. Offline: SIM:* symbols are served by the hub from synthetic data."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.validation import service as service_mod
from app.validation.service import STATE_KEY, STATE_KIND, ValidationBusy, ValidationService

QUICK = {"symbols": ["SIM:NEXUS"], "tfs": ["1h"], "bars": 1200}


def wait_done(client, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get("/api/validation/status").json()
        if not st["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("la validación no terminó a tiempo")


def test_status_and_latest_before_any_run(api_client):
    st = api_client.get("/api/validation/status").json()
    assert st["running"] is False and st["state"] == "idle" and st["job"] is None
    assert st["defaults"]["tfs"] == ["1h", "4h"] and st["defaults"]["bars"] == 3000
    assert "BINANCE:BTCUSDT" in st["defaults"]["symbols"]
    assert api_client.get("/api/validation/latest").json() == {"report": None}
    health = api_client.get("/api/health").json()
    assert health["services"]["validation"]["running"] is False


def test_run_validates_persists_and_reports_unavailable_series(api_client):
    r = api_client.post("/api/validation/run", json={**QUICK, "symbols": ["SIM:NEXUS", "BINANCE:NOPEUSDT"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["running"] is True and body["job"]["total"] == 2 and body["job"]["request"]["bars"] == 1200
    st = wait_done(api_client)
    assert st["state"] == "done" and st["job"]["done"] == 2 and st["job"]["summary"]["label"]
    rep = api_client.get("/api/validation/latest").json()["report"]
    assert [s["symbol"] for s in rep["series"]] == ["SIM:NEXUS"]
    assert rep["errors"] and rep["errors"][0]["symbol"] == "BINANCE:NOPEUSDT"  # offline, no cached candles
    s = rep["series"][0]
    assert s["bars"] <= 1200 and len(s["folds"]) == 5 and s["overall"]["random"]["runs"] == 1000
    assert rep["summary"]["code"] in {"insufficient", "negative", "no_edge", "edge"}
    assert rep["summary"]["text"].startswith(rep["summary"]["label"])
    assert not any(k.startswith("_") for k in s)
    events = api_client.get("/api/events", params={"source": "validation", "limit": 50}).json()["events"]
    msgs = " ".join(e["message"] for e in events)
    assert "Validación iniciada" in msgs and "Validación terminada" in msgs and "SIM:NEXUS 1h" in msgs


@pytest.mark.parametrize("body, needle", [
    ({"tfs": ["7x"]}, "Temporalidad"),
    ({"bars": 10}, "entre 1000 y 5000"),
    ({"symbols": [f"SIM:S{i}" for i in range(13)]}, "Demasiados símbolos"),
    ({"symbols": [f"SIM:S{i}" for i in range(9)], "tfs": ["15m", "1h", "4h"]}, "Demasiadas series"),
    ({"symbols": ["zzqqxxnoexiste"]}, "No encontré el símbolo"),
])
def test_run_rejects_invalid_requests(api_client, body, needle):
    r = api_client.post("/api/validation/run", json=body)
    assert r.status_code == 400 and needle in r.json()["detail"]
    assert api_client.get("/api/validation/status").json()["running"] is False


def test_one_job_at_a_time(api_client, monkeypatch):
    real = service_mod.load_from_hub

    async def slow(hub, symbol, tf, bars):
        await asyncio.sleep(1.0)
        return await real(hub, symbol, tf, bars)

    monkeypatch.setattr(service_mod, "load_from_hub", slow)
    assert api_client.post("/api/validation/run", json=QUICK).status_code == 200
    busy = api_client.post("/api/validation/run", json=QUICK)
    assert busy.status_code == 409 and "en curso" in busy.json()["detail"]
    assert wait_done(api_client)["state"] == "done"
    assert api_client.post("/api/validation/run", json=QUICK).status_code == 200  # free again
    wait_done(api_client)


def test_terminal_validar(api_client, monkeypatch):
    real = service_mod.load_from_hub

    async def slow(hub, symbol, tf, bars):
        await asyncio.sleep(1.0)
        return await real(hub, symbol, tf, bars)

    monkeypatch.setattr(service_mod, "load_from_hub", slow)

    def run(text: str) -> dict:
        return api_client.post("/api/terminal", json={"text": text}).json()

    empty = run("validar resultado")
    assert empty["ok"] and "Aún no hay ninguna validación" in empty["lines"][0]["text"]
    started = run("validar SIM:NEXUS --tf=1h --bars=1200")
    assert started["ok"], started
    assert "Validación iniciada: SIM:NEXUS · 1h · 1200 velas" in started["lines"][0]["text"]
    assert run("validar SIM:NEXUS --tf 1h")["ok"] is False  # busy -> Spanish error with the next step
    wait_done(api_client)
    st = run("validar estado")
    assert st["ok"] and "terminada" in st["lines"][0]["text"]
    res = run("validar resultado")
    text = "\n".join(line["text"] for line in res["lines"])
    assert res["ok"] and "SIM:NEXUS 1h" in text and "Veredicto" in text and "no garantiza" in text
    bad = run("validar --tf=9z")
    assert bad["ok"] is False and "Temporalidad" in bad["lines"][-1]["text"]
    assert run("validate estado")["ok"] and run("validación estado")["ok"]  # aliases


def test_latest_report_survives_a_restart(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = ValidationService(rt)
            assert await svc.latest() is None
            report = {"version": 1, "series": [{"symbol": "SIM:X", "tf": "1h"}], "generated_at": 123,
                      "summary": {"code": "no_edge", "label": "Sin evidencia de ventaja", "text": "…"}}
            await svc._save(report)
            await svc._save({**report, "generated_at": 456})  # upsert, one row
            fresh = ValidationService(rt)  # a restarted server reads it back from model_state
            got = await fresh.latest()
            assert got["generated_at"] == 456 and got["series"][0]["symbol"] == "SIM:X"
            n = await rt.db.fetchval("SELECT COUNT(*) FROM model_state WHERE kind=? AND key=?", (STATE_KIND, STATE_KEY))
            assert n == 1
            await rt.db.execute("UPDATE model_state SET state='not json' WHERE kind=? AND key=?",
                                (STATE_KIND, STATE_KEY))
            assert await ValidationService(rt).latest() is None  # a corrupt row is ignored, never a crash

    asyncio.run(scenario())


def test_service_without_hub_and_busy_guard(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = ValidationService(rt)
            with pytest.raises(RuntimeError):
                await svc.run(["SIM:NEXUS"], ["1h"], 1200)  # no market-data service
            gate = asyncio.Event()

            async def never(*_a):
                await gate.wait()

            svc._task = asyncio.create_task(never())
            rt.hub = object()
            with pytest.raises(ValidationBusy):
                await svc.run(["SIM:NEXUS"], ["1h"], 1200)
            gate.set()
            await svc.stop()
            assert svc.status()["running"] is False

    asyncio.run(scenario())
