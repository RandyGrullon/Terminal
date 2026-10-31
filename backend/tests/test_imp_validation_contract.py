"""Validation feature, second verifier: the JSON of /api/validation/* has exactly the fields of the interfaces in
frontend/src/api/types-validation.ts (parsed like tools/contract_check.py does), so the card never reads a field the
server does not send and the server never sends one the types do not describe."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _interfaces() -> dict[str, dict[str, bool]]:
    spec = importlib.util.spec_from_file_location("contract_check_for_validation", ROOT / "tools" / "contract_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_interfaces((ROOT / "frontend" / "src" / "api" / "types-validation.ts").read_text(encoding="utf-8"))


def _matches(ifaces: dict, name: str, obj: dict) -> None:
    fields = ifaces[name]
    assert isinstance(obj, dict), (name, obj)
    missing = [k for k, optional in fields.items() if not optional and k not in obj]
    extra = [k for k in obj if k not in fields]
    assert not missing and not extra, (name, missing, extra)


def _wait_done(client, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get("/api/validation/status").json()
        if not st["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("la validación no terminó a tiempo")


def test_status_job_and_report_match_the_typescript_contract(api_client):
    ifaces = _interfaces()
    idle = api_client.get("/api/validation/status").json()
    _matches(ifaces, "ValidationStatus", idle)
    _matches(ifaces, "ValidationRequestInfo", idle["defaults"])
    _matches(ifaces, "ValidationLatestResponse", api_client.get("/api/validation/latest").json())

    body = {"symbols": ["SIM:NEXUS", "SIM:OTRO", "BINANCE:NOPEUSDT"], "tfs": ["1h"], "bars": 1200}
    started = api_client.post("/api/validation/run", json=body).json()
    _matches(ifaces, "ValidationStatus", started)
    _matches(ifaces, "ValidationJob", started["job"])
    st = _wait_done(api_client)
    assert st["state"] == "done"
    _matches(ifaces, "ValidationJob", st["job"])
    _matches(ifaces, "ValidationSummary", st["job"]["summary"])
    for err in st["job"]["errors"]:
        assert set(err) == {"series", "message"}

    latest = api_client.get("/api/validation/latest").json()
    _matches(ifaces, "ValidationLatestResponse", latest)
    rep = latest["report"]
    _matches(ifaces, "ValidationReport", rep)
    _matches(ifaces, "ValidationRequestInfo", rep["request"])
    _matches(ifaces, "ValidationConfig", rep["config"])
    _matches(ifaces, "ValidationSummary", rep["summary"])
    assert rep["errors"]
    for err in rep["errors"]:
        _matches(ifaces, "ValidationSeriesError", err)
    assert len(rep["series"]) == 2 and rep["groups"]
    for g in rep["groups"]:
        _matches(ifaces, "ValidationGroup", g)
        _matches(ifaces, "ValidationVerdict", g["verdict"])
        _matches(ifaces, "ValidationGroupVariant", g["ungated"])
        _matches(ifaces, "ValidationVerdict", g["ungated"]["verdict"])
    for s in rep["series"]:
        _matches(ifaces, "ValidationSeries", s)
        assert isinstance(s["gaps"], int)  # SIM:* trades 24/7: missing bars are counted
        for v in ("verdict", "verdict_ungated"):
            _matches(ifaces, "ValidationVerdict", s[v])
        _matches(ifaces, "ValidationGateEffect", s["gate_effect"])
        for section in [s["overall"], *s["folds"]]:
            _matches(ifaces, "ValidationFold" if "index" in section else "ValidationSection", section)
            for k in ("nexus", "ungated", "mechanical", "ungated_mechanical"):
                _matches(ifaces, "ValidationRStats", section[k])
            _matches(ifaces, "ValidationEquityStats", section["buy_hold"])
            for k in ("random", "random_ungated"):
                _matches(ifaces, "ValidationRandom", section[k])
