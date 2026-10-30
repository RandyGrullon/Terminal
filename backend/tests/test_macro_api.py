"""REST API and terminal commands of the semáforo through the full app (OFFLINE, no background loops)."""
from __future__ import annotations

from app.macro import calendar as cal
from app.macro import fomc
from tests.test_macro_fakes import FF_SAMPLE, Clock

CONDITIONS_KEYS = {"level", "label", "headline", "advice", "reasons", "scope", "symbol", "blackout", "next_event",
                   "events", "metrics", "updated_at", "stale"}
EVENT_KEYS = set(cal.EVENT_FIELDS) | {"countdown_s"}


def _prepare(now: int):
    """Fix the service clock and give it the stored weekly calendar (as after a previous online run)."""
    from app.runtime import rt

    assert rt.macro is not None, rt.load_errors
    rt.macro._time = Clock(now)
    rt.macro.cache = cal.make_cache(cal.parse_feed(FF_SAMPLE))
    rt.macro._cond.clear()
    return rt


def test_macro_conditions_endpoint(api_client):
    _prepare(fomc.ny_time("2026-10-28", 13, 40))
    r = api_client.get("/api/macro")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == CONDITIONS_KEYS
    assert body["level"] == "red" and body["label"] == "No operar ahora" and body["scope"] == "global"
    assert body["blackout"]["active"] is True and body["symbol"] is None
    assert body["stale"] is True  # offline: no volatility quotes
    assert set(body["next_event"]) == EVENT_KEYS and body["next_event"]["countdown_s"] == 1200
    assert all(set(e) == EVENT_KEYS for e in body["events"]) and len(body["events"]) <= 5
    assert len(body["reasons"]) <= 3 and body["metrics"] == []

    sim = api_client.get("/api/macro", params={"symbol": "SIM:NEXUS"}).json()
    assert sim["level"] == "green" and sim["scope"] == "sim" and sim["headline"] == "Mercado simulado: sin eventos macro"
    fx = api_client.get("/api/macro", params={"symbol": "fx:eurusd"}).json()
    assert fx["scope"] == "forex:EUR/USD" and fx["symbol"] == "FX:EURUSD"
    btc = api_client.get("/api/macro", params={"symbol": "btc"}).json()  # resolved by the hub aliases
    assert btc["scope"] == "crypto" and btc["symbol"] == "BINANCE:BTCUSDT"

    bad = api_client.get("/api/macro", params={"symbol": "NO VALE::"})
    assert bad.status_code == 400 and "no válido" in bad.json()["detail"]
    unknown = api_client.get("/api/macro", params={"symbol": "zzqqxx"})
    assert unknown.status_code == 400 and "Símbolo no válido" in unknown.json()["detail"]


def test_macro_calendar_endpoint(api_client):
    _prepare(fomc.ny_time("2026-10-27", 12, 0))
    r = api_client.get("/api/macro/calendar")
    assert r.status_code == 200
    events = r.json()["events"]
    assert events and all(set(e) == EVENT_KEYS for e in events)
    assert all(e["impact"] in ("high", "medium") for e in events)
    assert [e["time"] for e in events] == sorted(e["time"] for e in events)
    eur = api_client.get("/api/macro/calendar", params={"impact": "all", "currency": "eur"}).json()["events"]
    assert {e["currency"] for e in eur} == {"EUR"} and any(e["impact"] == "medium" for e in eur)
    long = api_client.get("/api/macro/calendar", params={"days": 60, "impact": "high"}).json()["events"]
    assert any(e["id"] == "fomc-2026-12-09-decision" for e in long)
    for params, fragment in [({"days": "0"}, "fuera de rango"), ({"days": "abc"}, "no válido"),
                             ({"impact": "extreme"}, "Impacto no válido"), ({"currency": "EURO"}, "Divisa no válida")]:
        bad = api_client.get("/api/macro/calendar", params=params)
        assert bad.status_code == 400 and fragment in bad.json()["detail"], params


def test_macro_unavailable_returns_503(api_client):
    from app.runtime import rt

    svc, rt.macro = rt.macro, None
    try:
        r = api_client.get("/api/macro")
        assert r.status_code == 503 and "no está disponible" in r.json()["detail"]
        assert api_client.get("/api/macro/calendar").status_code == 503
        res = api_client.post("/api/terminal", json={"text": "macro"}).json()
        assert res["ok"] is False and "no está disponible" in res["lines"][-1]["text"]
    finally:
        rt.macro = svc


def test_terminal_commands(api_client):
    _prepare(fomc.ny_time("2026-10-28", 13, 40))

    def run(text: str) -> dict:
        r = api_client.post("/api/terminal", json={"text": text})
        assert r.status_code == 200
        return r.json()

    res = run("macro")
    text = "\n".join(line["text"] for line in res["lines"])
    assert res["ok"] is True and "🔴 NO OPERAR AHORA · global" in text
    assert "Decisión de tipos de la Fed en 20 min" in text and "Ventana sin operar hasta las" in text
    assert res["data"]["level"] == "red"
    for alias in ("mercado btc", "semaforo SIM:NEXUS"):
        assert run(alias)["ok"] is True
    assert "🟢" in run("semaforo SIM:NEXUS")["lines"][0]["text"]
    cal_res = run("calendario 3")
    cal_text = "\n".join(line["text"] for line in cal_res["lines"])
    assert cal_res["ok"] is True and "Decisión de tipos de la Fed" in cal_text and "Previsión" in cal_text
    assert all(e["currency"] == "USD" for e in cal_res["data"]["events"])
    every = run("agenda 2 --all")
    assert every["ok"] is True and {e["currency"] for e in every["data"]["events"]} > {"USD"}
    eur = run("calendario 7 --divisa=EUR")
    assert eur["ok"] is True and {e["currency"] for e in eur["data"]["events"]} == {"EUR"}
    bad = run("calendario muchos")
    assert bad["ok"] is False and "Días no válidos" in bad["lines"][-1]["text"]
    help_res = run("help")
    assert any("[Mercado]" in line["text"] for line in help_res["lines"])
