"""NEXUS REST API, NexusService persistence (memory snapshots, watcher side effects) through the full app."""
from __future__ import annotations

import copy
import json

import pytest

from app.data.base import CandleSet
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

SYM = "SIM:NEXUS"


class MiniHub:
    """Fallback market-data hub, only used when app.data.hub could not be loaded."""

    def __init__(self) -> None:
        self.listeners: list = []
        self.subs: set = set()
        self.sets: dict = {}

    async def get_candles(self, symbol: str, tf: str = "1h", limit: int = 500) -> CandleSet:
        sym = symbol.upper()
        if sym.startswith("SIM:"):
            cs = self.sets.get((sym, tf))
            if cs is None:
                cs = self.sets[(sym, tf)] = generate_candleset(n=1500, seed=4, tf=tf, end_time=FIXED_END, symbol=sym)
            return cs.tail(limit)
        return CandleSet.empty(sym, tf, note="sin datos")

    async def resolve(self, text: str) -> str | None:
        return text.upper() if ":" in text else None

    async def watchlist(self) -> list[str]:
        return [SYM]

    def subscribe(self, symbol: str, tf: str, owner: str) -> None:
        self.subs.add((symbol, tf, owner))

    def unsubscribe(self, symbol: str, tf: str, owner: str) -> None:
        self.subs.discard((symbol, tf, owner))

    def add_listener(self, cb) -> None:
        self.listeners.append(cb)


@pytest.fixture
def client(api_client):
    from app.runtime import rt

    if rt.hub is None:
        rt.hub = MiniHub()
    assert rt.nexus is not None, rt.load_errors
    return api_client


def _db(client, method: str, sql: str, params: tuple = ()):
    from app.runtime import rt

    return client.portal.call(getattr(rt.db, method), sql, params)


def _term(client, text: str) -> dict:
    r = client.post("/api/terminal", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_nexus_endpoint_persists_memory(client):
    r = client.get("/api/nexus", params={"symbol": SYM, "tf": "1h", "limit": 600})
    assert r.status_code == 200, r.text
    data = r.json()
    json.dumps(data, allow_nan=False)
    assert data["ok"] is True and data["symbol"] == SYM and data["tf"] == "1h" and data["quick"] is None
    assert {"provider", "note", "history", "params", "bars", "markers", "trades", "current", "prediction",
            "learning", "stats"} <= set(data)
    assert len(data["bars"]["time"]) == 600 and set(data["current"]["gate"]) == {"long", "short"}
    row = _db(client, "fetchone", "SELECT state, updates, trained_until FROM model_state WHERE kind='nexus' AND key=?",
              (f"{SYM}|1h",))
    assert row is not None and int(row["updates"]) > 0
    state = json.loads(row["state"])
    times = [s["time"] for s in state["snapshots"]]
    assert 0 < len(times) <= 60 and times == sorted(times) and times[-1] == data["current"]["time"]
    again = client.get("/api/nexus", params={"symbol": SYM, "tf": "1h", "limit": 600}).json()
    assert again["current"] == data["current"]
    model = client.get("/api/nexus/model", params={"symbol": SYM, "tf": "1h"}).json()
    assert model["learning"]["updates"] > 0 and len(model["experts"]) >= 10
    assert isinstance(model["calibration"], list) and isinstance(model["rolling"], list)
    health = client.get("/api/health").json()
    assert health["services"]["nexus"]["running"] is True
    assert not [e for e in health["load_errors"] if "app.engine" in e or "app.api.nexus" in e]


def test_gate_check_endpoint(client):
    data = client.get("/api/nexus", params={"symbol": SYM, "tf": "1h"}).json()
    r = client.get("/api/gate/check", params={"symbol": SYM, "tf": "1h", "direction": "long"})
    assert r.status_code == 200, r.text
    assert r.json() == data["current"]["gate"]["long"]
    price = data["current"]["price"]
    custom = client.get("/api/gate/check", params={"symbol": SYM, "tf": "1h", "direction": "short",
                                                   "entry": price, "sl": price * 1.02, "tp": price * 0.95}).json()
    assert custom["entry"] == pytest.approx(price) and custom["sl"] == pytest.approx(price * 1.02)
    assert custom["tp1"] == custom["tp3"] == pytest.approx(price * 0.95) and custom["verdict"] in ("TAKE", "WAIT", "AVOID")
    bad = client.get("/api/gate/check", params={"symbol": SYM, "tf": "1h", "direction": "long", "sl": price * 1.1})
    assert bad.status_code == 400 and "stop" in bad.json()["detail"]
    wrong = client.get("/api/gate/check", params={"symbol": SYM, "tf": "1h", "direction": "arriba"})
    assert wrong.status_code == 400 and "dirección" in wrong.json()["detail"]
    tf_bad = client.get("/api/nexus", params={"symbol": SYM, "tf": "7x"})
    assert tf_bad.status_code == 400


def test_insufficient_history_falls_back(client):
    r = client.get("/api/nexus", params={"symbol": "SIM:NEXUS", "tf": "1h", "limit": 120})
    assert r.status_code == 200
    data = client.get("/api/nexus", params={"symbol": "BINANCE:NOEXISTEUSDT", "tf": "1h"}).json()
    assert data["ok"] is False and "insuficiente" in data["message"] and "quick" in data
    assert data["current"] is None and data["stats"]["trades"] == 0
    json.dumps(data, allow_nan=False)
    g = client.get("/api/gate/check", params={"symbol": "BINANCE:NOEXISTEUSDT", "tf": "1h", "direction": "long"})
    assert g.status_code == 422 and "insuficiente" in g.json()["detail"]


def test_reset_model(client):
    client.get("/api/nexus", params={"symbol": SYM, "tf": "1h", "limit": 400})
    assert _db(client, "fetchval", "SELECT COUNT(*) FROM model_state WHERE kind='nexus'") >= 1
    r = client.post("/api/nexus/reset", json={"symbol": SYM, "tf": "1h"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _db(client, "fetchval", "SELECT COUNT(*) FROM model_state WHERE kind='nexus' AND key=?", (f"{SYM}|1h",)) == 0


def test_indicator_endpoints(client):
    cat = client.get("/api/indicators/catalog")
    if cat.status_code == 503:
        assert cat.json()["detail"] == "Biblioteca de indicadores no disponible"
        return
    assert cat.status_code == 200 and len(cat.json()["indicators"]) >= 70
    body = {"symbol": SYM, "tf": "1h", "limit": 300,
            "items": [{"uid": "a", "id": "rsi", "params": {}}, {"uid": "b", "id": "no_existe", "params": {}}]}
    r = client.post("/api/indicators/compute", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    json.dumps(out, allow_nan=False)
    assert len(out["time"]) == 300
    first, second = out["results"]
    assert first["uid"] == "a" and first["error"] is None and first["outputs"]
    series = next(iter(first["outputs"].values()))
    assert len(series) == 300 and series[0] is None and isinstance(series[-1], float)
    assert second["uid"] == "b" and second["error"] and second["outputs"] == {}


def test_watcher_publishes_and_records(client):
    from app.runtime import rt

    published: list = []
    rt.ws.publish = lambda topic, msg: published.append((topic, msg))
    resp = client.portal.call(rt.nexus.process_closed_bar, SYM, "1h")
    assert resp is not None and resp["ok"]
    topic, msg = published[-1]
    assert topic == f"market:{SYM}:1h" and msg["type"] == "nexus" and msg["symbol"] == SYM
    assert msg["current"]["time"] == resp["current"]["time"] and isinstance(msg["markers"], list)
    n_pred = _db(client, "fetchval", "SELECT COUNT(*) FROM predictions WHERE source='nexus' AND symbol=?", (SYM,))
    assert n_pred == 1
    client.portal.call(rt.nexus.process_closed_bar, SYM, "1h")  # same bar again: no duplicate prediction
    assert _db(client, "fetchval", "SELECT COUNT(*) FROM predictions WHERE source='nexus'") == 1

    fake = copy.deepcopy(resp)
    fake["current"]["action"] = "BUY"
    fake["current"]["action_label"] = "COMPRAR"
    t = fake["current"]["time"]
    rt.nexus._last_action.pop((SYM, "1h"), None)
    marks = [{"time": t, "kind": "BUY"}, {"time": t, "kind": "REJECTED_SHORT"}]
    client.portal.call(rt.nexus._record, SYM, "1h", fake, marks)
    sig = _db(client, "fetchone", "SELECT action, verdict, gate_score, reason FROM signals WHERE symbol=?", (SYM,))
    assert sig["action"] == "BUY" and sig["verdict"] == fake["current"]["gate"]["long"]["verdict"]
    rows = _db(client, "fetchall", "SELECT direction, taken, source, checks FROM gate_log WHERE symbol=? ORDER BY direction",
               (SYM,))
    assert [(r["direction"], int(r["taken"]), r["source"]) for r in rows] == [("long", 1, "watch"), ("short", 0, "watch")]
    assert set(json.loads(rows[0]["checks"])) >= {"risk_reward", "expected_value", "trend_alignment"}
    events = client.get("/api/events", params={"source": "nexus", "limit": 50}).json()["events"]
    assert any(e["level"] == "signal" and "COMPRAR" in e["message"] for e in events)
    assert any(e["level"] == "info" and "rechazada" in e["message"] for e in events)


def test_watcher_ignores_unwatched_and_open_bars(client):
    from app.runtime import rt

    svc = rt.nexus
    client.portal.call(svc._on_candle, SYM, "1h", {"time": 123}, False)
    client.portal.call(svc._on_candle, "SIM:OTRO", "1h", {"time": 123}, True)
    assert not svc._processing and not svc._bg_tasks
    assert svc.watching() == [] and svc.status()["watching"] == 0


def test_terminal_commands(client):
    res = _term(client, f"signal {SYM}")
    assert res["ok"] and any("Consejo:" in line["text"] for line in res["lines"])
    assert res["actions"] and res["actions"][0]["action"] == "open_chart"
    chk = _term(client, f"check {SYM} long")
    text = "\n".join(line["text"] for line in chk["lines"])
    assert chk["ok"] and "¿Tomo esta operación?" in text and "Veredicto:" in text and ("✓" in text or "✗" in text)
    price = chk["data"]["entry"]
    custom = _term(client, f"check {SYM} short 1h --sl {price * 1.03:.4f} --tp {price * 0.9:.4f}")
    assert custom["ok"] and custom["data"]["direction"] == "short" and custom["data"]["sl"] == pytest.approx(price * 1.03, rel=1e-3)
    for cmd in (f"predict {SYM}", f"explain {SYM}", f"model {SYM}", "indicators rsi", f"señal {SYM} 1h"):
        out = _term(client, cmd)
        assert out["ok"], (cmd, out["lines"])
    warn = _term(client, f"reset-model {SYM}")
    assert warn["ok"] and "--yes" in warn["lines"][0]["text"]
    done = _term(client, f"reset-model {SYM} --yes")
    assert done["ok"] and "reiniciada" in done["lines"][-1]["text"]
    bad = _term(client, f"check {SYM} long --sl 999999999")
    assert bad["ok"] is False
    wl = _term(client, "watchlist-signals")
    assert wl["ok"] and any("Señales NEXUS" in line["text"] for line in wl["lines"])
    cmds = {c["name"]: c for c in client.get("/api/terminal/commands").json()["commands"]}
    for name in ("signal", "check", "predict", "explain", "model", "reset-model", "indicators", "watchlist-signals"):
        assert cmds[name]["group"] == "NEXUS"


def test_snapshot_retention_and_prior_selection():
    from app.engine.service import merge_snapshots, pick_prior

    tf_s = 3600
    old = [{"time": t * tf_s, "state": {"v": t}} for t in range(0, 4000, 10)]
    new = [{"time": 4000 * tf_s, "state": {"v": "new"}}, {"time": 0, "state": {"v": "replaced"}}]
    kept = merge_snapshots(old, new, tf_s)
    times = [s["time"] for s in kept]
    assert len(kept) <= 60 and times == sorted(times) and times[-1] == 4000 * tf_s
    assert all(b - a >= 50 * tf_s for a, b in zip(times, times[1:]))
    assert pick_prior(kept, 3000 * tf_s)["v"] <= 3000
    assert pick_prior(kept, -1) is None
    assert pick_prior([{"time": 5, "state": {"x": 1}}, {"time": 9, "state": {"x": 2}}], 7) == {"x": 1}


def test_watcher_live_flow(client, monkeypatch):
    """With background work enabled: watchlist subscriptions (owner 'nexus') and a closed bar -> recompute."""
    import asyncio

    from app import config
    from app.runtime import rt

    svc = rt.nexus
    monkeypatch.setattr(config, "BACKGROUND", True)
    subs: list = []
    monkeypatch.setattr(rt.hub, "subscribe", lambda symbol, tf, owner: subs.append((symbol, tf, owner)), raising=False)
    monkeypatch.setattr(rt.hub, "unsubscribe", lambda symbol, tf, owner: subs.remove((symbol, tf, owner)), raising=False)

    async def watchlist() -> list[str]:
        return [SYM]

    monkeypatch.setattr(rt.hub, "watchlist", watchlist, raising=False)
    client.portal.call(svc.sync_watch)
    assert subs == [(SYM, "1h", "nexus")] and svc.watching() == [{"symbol": SYM, "tf": "1h"}]
    published: list = []
    rt.ws.publish = lambda topic, msg: published.append((topic, msg))

    async def feed_and_wait() -> None:
        await svc._on_candle(SYM, "1h", {"time": 1_900_000_000}, True)
        await svc._on_candle(SYM, "1h", {"time": 1_900_000_000}, True)  # same bar twice: processed once
        await asyncio.gather(*list(svc._bg_tasks))

    client.portal.call(feed_and_wait)
    assert len(published) == 1 and published[0][1]["type"] == "nexus"
    assert _db(client, "fetchval", "SELECT COUNT(*) FROM predictions WHERE source='nexus' AND symbol=?", (SYM,)) == 1
    client.portal.call(svc.stop_watch)
    assert subs == [] and svc.watching() == []
