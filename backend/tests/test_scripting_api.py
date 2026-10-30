"""BE-SCRIPT: REST API (§6.5) and PyChart terminal commands through the full app (OFFLINE, no background loops).
Runs on SQLite and (with NEXUS_TEST_PG_URL) on PostgreSQL."""
from __future__ import annotations

import importlib.util

import pytest

from app.data.base import CandleSet
from app.scripting import examples as examples_mod
from app.scripting.service import MSG_EXAMPLE_PROTECTED, MSG_UNAVAILABLE
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

CODE = 'indicator("Media API", overlay=True)\nn = input.int(20, "Longitud")\nplot(ta.sma(close, n), "SMA")\n'
HAS_VALIDATOR = importlib.util.find_spec("app.scripting.validator") is not None
HAS_SANDBOX = importlib.util.find_spec("app.scripting.sandbox") is not None


class FakeHub:
    def __init__(self) -> None:
        self.cs = generate_candleset(n=800, seed=21, end_time=FIXED_END, symbol="SIM:NEXUS")

    async def get_candles(self, symbol, tf, limit=500):
        if symbol != "SIM:NEXUS":
            return CandleSet.empty(symbol, tf, note="símbolo de prueba desconocido")
        return self.cs.tail(int(limit))

    async def resolve(self, text):
        return "SIM:NEXUS" if str(text).lower() in ("sim", "sim:nexus") else None


def _term(client, text: str) -> dict:
    r = client.post("/api/terminal", json={"text": text})
    assert r.status_code == 200
    return r.json()


def _text(res: dict) -> str:
    return "\n".join(line["text"] for line in res["lines"])


def test_service_is_registered(api_client):
    health = api_client.get("/api/health").json()
    assert not [e for e in health["load_errors"] if "scripting" in e or "api.scripts" in e]
    st = health["services"]["scripts"]
    assert st["running"] is True and st["examples"] == len(examples_mod.EXAMPLES)


def test_crud_endpoints(api_client):
    listed = api_client.get("/api/scripts").json()["scripts"]
    assert len(listed) == len(examples_mod.EXAMPLES) and all(s["is_example"] for s in listed)
    assert set(listed[0]) == {"id", "name", "description", "is_example", "updated_at"}

    r = api_client.post("/api/scripts", json={"name": "Mi media", "code": CODE, "description": "Prueba"})
    assert r.status_code == 200
    created = r.json()
    assert created["name"] == "Mi media" and created["code"] == CODE and created["is_example"] is False
    assert api_client.get(f"/api/scripts/{created['id']}").json() == created

    dup = api_client.post("/api/scripts", json={"name": "MI MEDIA", "code": CODE})
    assert dup.status_code == 409 and "Ya existe" in dup.json()["detail"]
    missing = api_client.post("/api/scripts", json={"code": CODE})
    assert missing.status_code == 400 and "nombre" in missing.json()["detail"]

    r = api_client.put(f"/api/scripts/{created['id']}", json={"name": "Mi media 2", "description": "Nueva"})
    assert r.status_code == 200 and r.json()["name"] == "Mi media 2" and r.json()["code"] == CODE
    names = [s["name"] for s in api_client.get("/api/scripts").json()["scripts"]]
    assert names[0] == "Mi media 2"  # user scripts first

    assert api_client.delete(f"/api/scripts/{created['id']}").json() == {"ok": True}
    gone = api_client.get(f"/api/scripts/{created['id']}")
    assert gone.status_code == 404 and gone.json()["detail"] == "Script no encontrado"
    assert api_client.get("/api/scripts/no-es-un-id").status_code == 404
    assert api_client.put("/api/scripts/999999", json={"code": CODE}).status_code == 404


def test_examples_are_protected(api_client):
    example = api_client.get("/api/scripts").json()["scripts"][0]
    r = api_client.put(f"/api/scripts/{example['id']}", json={"code": "# cambiado"})
    assert r.status_code == 403 and r.json()["detail"] == MSG_EXAMPLE_PROTECTED
    r = api_client.delete(f"/api/scripts/{example['id']}")
    assert r.status_code == 403 and r.json()["detail"] == MSG_EXAMPLE_PROTECTED
    assert api_client.get(f"/api/scripts/{example['id']}").json()["code"] != "# cambiado"


def test_reference_endpoint(api_client):
    ref = api_client.get("/api/scripts/reference").json()
    assert ref["sections"] and all(s["title"] and s["items"] for s in ref["sections"])
    names = {item["name"] for s in ref["sections"] for item in s["items"]}
    assert {"indicator", "strategy", "plot", "ta.ema", "strategy.entry", "input.int", "color.red"} <= names
    item = ref["sections"][0]["items"][0]
    assert set(item) == {"name", "signature", "doc"} and item["doc"]


def test_compile_endpoint(api_client):
    r = api_client.post("/api/scripts/compile", json={"code": CODE})
    if not HAS_VALIDATOR:
        assert r.status_code == 503 and r.json()["detail"] == MSG_UNAVAILABLE
        pytest.skip("Compilador PyChart (app.scripting.validator) no disponible todavía")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["meta"]["title"] == "Media API"
    assert body["inputs"][0]["name"] == "n" and body["inputs"][0]["default"] == 20
    bad = api_client.post("/api/scripts/compile", json={"code": "import subprocess\n"}).json()
    assert bad["ok"] is False and bad["errors"][0]["kind"] == "security" and bad["errors"][0]["line"] == 1


def test_run_endpoint_validation(api_client, monkeypatch):
    from app.runtime import rt

    monkeypatch.setattr(rt, "hub", FakeHub())
    r = api_client.post("/api/scripts/run", json={"code": CODE, "symbol": "SIM:NEXUS", "tf": "13x"})
    assert r.status_code in (400, 503)
    if HAS_SANDBOX:
        assert r.status_code == 400 and "Temporalidad" in r.json()["detail"]
        r = api_client.post("/api/scripts/run", json={"code": CODE, "symbol": "SIM:DESCONOCIDO", "tf": "1h"})
        assert r.status_code == 400 and "Sin velas" in r.json()["detail"]
    else:
        assert r.json()["detail"]


def test_run_endpoint_with_fake_hub(api_client, monkeypatch):
    pytest.importorskip("app.scripting.sandbox")
    from app.runtime import rt

    monkeypatch.setattr(rt, "hub", FakeHub())
    r = api_client.post("/api/scripts/run", json={"code": CODE, "symbol": "SIM:NEXUS", "tf": "1h", "limit": 300,
                                                  "inputs": {"n": 10}})
    assert r.status_code == 200
    res = r.json()
    assert res["ok"] is True, res["errors"]
    assert len(res["time"]) == 300 and len(res["candles"]) == 300 and res["plots"]
    assert len(res["plots"][0]["data"]) == 300 and res["meta"]["title"] == "Media API"
    assert api_client.get("/api/health").json()["services"]["scripts"]["runs"] >= 1


def test_run_endpoint_with_real_hub(api_client):
    pytest.importorskip("app.scripting.sandbox")
    from app.runtime import rt

    if rt.hub is None:
        pytest.skip("El módulo de mercado (rt.hub) no está disponible todavía: no hay velas SIM:NEXUS")
    r = api_client.post("/api/scripts/run", json={"code": CODE, "symbol": "SIM:NEXUS", "tf": "1h", "limit": 400})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is True and len(res["time"]) > 0 and len(res["candles"]) == len(res["time"])


def test_terminal_commands(api_client, monkeypatch):
    from app.runtime import rt

    commands = {c["name"]: c for c in api_client.get("/api/terminal/commands").json()["commands"]}
    for name in ("scripts", "script", "run", "compile"):
        assert commands[name]["group"] == "PyChart"

    res = _term(api_client, "scripts")
    assert res["ok"] and "Ichimoku completo" in _text(res) and len(res["data"]) == len(examples_mod.EXAMPLES)
    res = _term(api_client, "script ichimoku")
    assert res["ok"] and "Tenkan" in _text(res) and res["data"]["name"] == "Ichimoku completo"
    assert not _term(api_client, "script")["ok"]
    assert "no hay ningún script" in _text(_term(api_client, "script zzzz")).lower()

    res = _term(api_client, 'compile "Cruce de EMAs (estrategia)"')
    if HAS_VALIDATOR:
        assert res["ok"] and "compila sin errores" in _text(res) and "rapida" in _text(res)
    else:
        assert not res["ok"] and MSG_UNAVAILABLE in _text(res)

    if not HAS_SANDBOX:
        pytest.skip("Sandbox PyChart (app.scripting.sandbox) no disponible todavía")
    monkeypatch.setattr(rt, "hub", FakeHub())
    res = _term(api_client, "run cruce de emas sim:nexus 1h --limit 500 --rapida=12")
    assert res["ok"], _text(res)
    text = _text(res)
    assert "Cruce de EMAs (estrategia)" in text and "SIM:NEXUS 1h" in text and "EMA rápida" in text
    assert res["data"]["inputs"] == {"rapida": 12} and res["data"]["bars"] == 500
    assert "Operaciones" in text  # strategy metrics table
    res = _term(api_client, "run cruce de emas sim:nexus --noexiste=3")
    assert not res["ok"] and "desconocida" in _text(res)
    assert not _term(api_client, "run ichimoku")["ok"]  # missing symbol
