"""BE-SIM: REST API (§6.4) and terminal commands through the full app (OFFLINE, no background loops)."""
from __future__ import annotations

import time

import pytest

from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_sim_fakes import FakeHub, FakeNexus, engine_runs

BTC = "BINANCE:BTCUSDT"


@pytest.fixture
def client(api_client):
    from app.runtime import rt

    sim_cs = generate_candleset(n=400, seed=4, end_time=FIXED_END, symbol="SIM:NEXUS")
    rt.hub = FakeHub(prices={BTC: 100.0, "BINANCE:ETHUSDT": 200.0, "SIM:NEXUS": float(sim_cs.close[-1])},
                     candles={("SIM:NEXUS", "1h"): sim_cs})
    rt.nexus = FakeNexus("TAKE")
    yield api_client
    if rt.sim is not None:
        api_client.post("/api/sim/stop")


def _term(client, text: str) -> dict:
    r = client.post("/api/terminal", json={"text": text})
    assert r.status_code == 200
    return r.json()


def _text(res: dict) -> str:
    return "\n".join(line["text"] for line in res["lines"])


def test_services_are_registered(client):
    services = client.get("/api/health").json()["services"]
    assert services["scanner"]["running"] is True and "model_updates" in services["scanner"]
    assert services["paper"] == {"running": True, "open_positions": 0}
    assert set(services["evaluator"]) == {"running", "pending", "resolved_total"}
    assert services["sim"] == {"running": False, "mode": None, "bars_done": 0}
    assert not [e for e in client.get("/api/health").json()["load_errors"] if "app.sim" in e or "api.sim" in e]


def test_paper_endpoints(client):
    from app.runtime import rt

    acct = client.get("/api/paper/account").json()
    assert set(acct) == {"account", "positions", "history"} and acct["account"]["id"] == 1
    assert acct["account"]["name"] == "Principal" and acct["account"]["balance"] == 10000

    r = client.post("/api/paper/order", json={"symbol": "btc", "side": "long", "tf": "1h"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["position"]["symbol"] == BTC and body["gate"]["verdict"] == "TAKE"
    assert body["position"]["sl"] < body["position"]["entry_price"] < body["position"]["tp1"]

    rt.nexus.verdict = "AVOID"
    r = client.post("/api/paper/order", json={"symbol": BTC, "side": "short"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["gate"]["verdict"] == "AVOID" and "NO TOMAR" in detail["message"]
    r = client.post("/api/paper/order", json={"symbol": BTC, "side": "short", "force": True})
    assert r.status_code == 200 and r.json()["position"]["gate_verdict"] == "AVOID"

    assert client.post("/api/paper/order", json={"symbol": BTC}).status_code == 400
    assert client.post("/api/paper/order", json={"symbol": "zzz", "side": "long"}).status_code == 404
    assert client.post("/api/paper/order", json={"side": "long"}).status_code == 400
    r = client.post("/api/paper/order", json={"symbol": BTC, "side": "long", "sl": 150, "force": True})
    assert r.status_code == 400 and "stop" in r.json()["detail"]

    assert len(client.get("/api/paper/account").json()["positions"]) == 2
    r = client.post("/api/paper/close", json={"symbol": BTC})
    assert r.status_code == 200 and len(r.json()["closed"]) == 2
    assert client.post("/api/paper/close", json={"position_id": 999}).status_code == 404
    assert client.post("/api/paper/close", json={}).status_code == 400
    hist = client.get("/api/paper/account").json()["history"]
    assert len(hist) == 2 and all(p["status"] == "closed" for p in hist)

    r = client.post("/api/paper/reset", json={"initial_balance": 5000})
    assert r.status_code == 200 and r.json()["account"]["balance"] == 5000 and r.json()["history"] == []
    assert client.post("/api/paper/reset", json={}).json()["account"]["initial_balance"] == 10000
    assert client.post("/api/paper/reset", json={"initial_balance": -5}).status_code == 400

    r = client.post("/api/paper/auto", json={"enabled": True, "symbols": ["eth"], "tf": "4h"})
    assert r.status_code == 200 and r.json()["auto"] == {"enabled": True, "symbols": ["BINANCE:ETHUSDT"], "tf": "4h"}
    assert client.get("/api/settings").json()["settings"]["paper.auto_trade"] is True
    assert client.post("/api/paper/auto", json={"enabled": False, "tf": "7x"}).status_code == 400


def test_sim_endpoints(client):
    r = client.post("/api/sim/start", json={"mode": "synthetic", "speed": 20, "warmup": 60, "seed": 3})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["running"] is True
    deadline = time.time() + 5
    while time.time() < deadline and client.get("/api/sim/state").json()["state"]["bars_done"] < 2:
        time.sleep(0.05)
    snap = client.get("/api/sim/state").json()
    assert set(snap) == {"state", "candles", "nexus", "positions"} and len(snap["candles"]) >= 62
    r = client.post("/api/sim/config", json={"paused": True, "regime": "bear"})
    assert r.status_code == 200 and r.json()["state"]["paused"] is True and r.json()["state"]["regime"] == "bear"
    assert client.post("/api/sim/config", json={"regime": "moon"}).status_code == 400
    r = client.post("/api/sim/stop")
    assert r.status_code == 200 and r.json()["state"]["running"] is False
    assert client.post("/api/sim/config", json={"regime": "bull"}).status_code == 409
    assert client.post("/api/sim/start", json={"mode": "replay", "symbol": BTC}).status_code == 400
    assert client.post("/api/sim/start", json={"mode": "replay"}).status_code == 400
    assert client.post("/api/sim/start", json={"mode": "bogus"}).status_code == 400


def test_scanner_prediction_and_learning_endpoints(client):
    assert client.get("/api/scanner/runs").json() == {"runs": []}
    assert client.get("/api/scanner/results").json() == {"run": None, "results": []}
    assert client.get("/api/scanner/results", params={"run_id": 5}).status_code == 404
    r = client.post("/api/scanner/run", json={"market": "crypto", "tf": "1h"})
    assert r.status_code == 409 and "sin conexión" in r.json()["detail"].lower()
    q = client.get("/api/scanner/quick", params={"symbol": "SIM:NEXUS", "tf": "1h"})
    assert q.status_code == 200 and q.json()["symbol"] == "SIM:NEXUS" and q.json()["action"] in ("BUY", "SELL", "WAIT")
    assert client.get("/api/scanner/quick", params={"symbol": "SIM:NADA", "tf": "1h"}).status_code == 404
    assert client.get("/api/scanner/quick", params={"symbol": "SIM:NEXUS", "tf": "9x"}).status_code == 400
    p = client.get("/api/predictions").json()
    assert p["predictions"] == [] and set(p["stats"]) == {"total", "resolved", "pending", "hit_rate", "brier",
                                                          "by_source", "rolling"}
    assert client.get("/api/predictions", params={"resolved": "false", "limit": 5}).status_code == 200
    assert client.get("/api/predictions", params={"source": "otro"}).status_code == 400
    s = client.get("/api/learning/summary").json()
    assert set(s) == {"scanner_model", "nexus_models", "gate", "predictions", "timeline"}
    assert s["gate"]["decisions"] == 0


def test_backtest_and_montecarlo_endpoints(client, monkeypatch):
    assert client.post("/api/montecarlo", json={"mode": "nope"}).status_code == 400
    if not engine_runs():
        assert client.post("/api/backtest", json={"symbol": "SIM:NEXUS", "tf": "1h"}).status_code == 503
        assert client.post("/api/montecarlo", json={"mode": "synthetic"}).status_code == 503
        # with a stand-in engine the endpoint runs end to end
        from app.api import sim as api_sim
        from tests.test_sim_metrics import _fake_engine

        _fake_engine(monkeypatch)
        monkeypatch.setattr(api_sim, "load_engine", lambda: (None, None))
    r = client.post("/api/backtest", json={"symbol": "SIM:NEXUS", "tf": "1h", "limit": 400})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["symbol"] == "SIM:NEXUS" and res["bars"] == 400 and "metrics" in res and len(res["equity"]) == 400
    assert client.get("/api/db/stats").json()["tables"]["backtests"] == 1
    assert client.post("/api/backtest", json={"symbol": "SIM:VACIO", "tf": "1h"}).status_code == 404
    assert client.post("/api/backtest", json={"tf": "1h"}).status_code == 400
    assert client.post("/api/backtest", json={"symbol": "SIM:NEXUS", "risk_pct": 0}).status_code == 400


def test_terminal_commands(client):
    from app.runtime import rt

    for text in ("account", "positions", "predictions 5", "learning", "sim status", "results", "results 5 --take",
                 "scanner", "help backtest"):
        res = _term(client, text)
        assert res["ok"] is True, (text, res["lines"])
    res = _term(client, "buy btc --sl 98 --risk 0.5")
    assert res["ok"] and "COMPRA BINANCE:BTCUSDT" in _text(res) and res["actions"][0]["action"] == "open_chart"
    assert "hasta aquí" in _text(res)
    res = _term(client, "positions")
    assert res["ok"] and BTC in _text(res) and len(res["data"]) == 1
    rt.nexus.verdict = "AVOID"
    res = _term(client, "sell eth")
    assert res["ok"] is False and "NO TOMAR" in _text(res) and "--force" in _text(res)
    res = _term(client, "close all")
    assert res["ok"] and "Cerrado #" in _text(res)
    res = _term(client, "close all")
    assert res["ok"] and "No había posiciones abiertas" in _text(res)  # nothing left: informative
    assert _term(client, "close BINANCE:BTCUSDT")["ok"] is False  # explicit symbol without positions
    assert _term(client, "quick SIM:NEXUS")["ok"] is True
    res = _term(client, "scan crypto 1h --deep")
    assert res["ok"] is False and "sin conexión" in _text(res).lower()
    res = _term(client, "sim start --speed 20 --seed 4 --regime=bull")
    assert res["ok"] and res["actions"] == [{"action": "navigate", "page": "sim"}]
    assert _term(client, "sim pause")["ok"] and _term(client, "sim speed 5")["ok"]
    assert _term(client, "sim regime volatile")["ok"]
    assert _term(client, "sim resume")["ok"]
    assert _term(client, "sim bogus")["ok"] is False
    assert _term(client, "sim stop")["ok"]
    res = _term(client, "auto on btc eth --tf 4h")
    assert res["ok"] and res["data"]["symbols"] == [BTC, "BINANCE:ETHUSDT"] and res["data"]["tf"] == "4h"
    assert _term(client, "auto off")["ok"] and _term(client, "auto")["ok"]
    if not engine_runs():
        res = _term(client, "backtest SIM:NEXUS 1h")
        assert res["ok"] is False and "NEXUS" in _text(res)
        assert _term(client, "mc --synthetic")["ok"] is False
    names = {c["name"] for c in client.get("/api/terminal/commands").json()["commands"]}
    assert {"scan", "results", "scanner", "quick", "backtest", "mc", "sim", "buy", "sell", "close", "positions",
            "account", "auto", "predictions", "learning"} <= names
    groups = {c["group"] for c in client.get("/api/terminal/commands").json()["commands"] if c["name"] in names}
    assert {"Escáner", "Simulación", "Cuenta demo", "Aprendizaje"} <= groups
