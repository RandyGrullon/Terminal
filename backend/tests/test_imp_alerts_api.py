"""Alerts API and terminal on the full app (offline, fakes): GET /api/alerts/status never returns a secret,
POST /api/alerts/test reports each channel (Spanish errors, 400 / 409 / 429 / 503), `alertas` and `alertas test`."""
from __future__ import annotations

from app import config, util
from app.runtime import rt
from tests.test_imp_alerts_fakes import CHAT, TOKEN, FakeHTTP, tg_error, tg_ok

SECRET_PW = "pw-super-secreta-123"


def configure(client, **extra) -> None:
    body = {"alerts.telegram_enabled": True, "alerts.telegram_token": TOKEN, "alerts.telegram_chat_id": CHAT,
            "alerts.smtp_password": SECRET_PW, **extra}
    assert client.put("/api/settings", json=body).status_code == 200


def test_status_shape_and_no_secrets(api_client):
    r = api_client.get("/api/alerts/status")
    assert r.status_code == 200
    st = r.json()
    for key in ("running", "offline", "channels", "active_channels", "categories", "min_level", "max_per_hour",
                "sent_last_hour", "quiet", "counters", "since", "pending", "recent", "last_test", "warnings"):
        assert key in st
    assert st["running"] is True and st["offline"] is True and st["active_channels"] == 0
    assert [c["id"] for c in st["channels"]] == ["telegram", "email"]
    assert st["categories"] == {"macro": True, "signals": True, "paper": True} and st["min_level"] == "warn"
    assert st["warnings"][0].startswith("Modo sin conexión (NEXUS_OFFLINE=1)")
    configure(api_client)
    st = api_client.get("/api/alerts/status").json()
    tg = st["channels"][0]
    assert tg["active"] and tg["configured"] and tg["target"] == f"chat {CHAT}" and st["active_channels"] == 1
    raw = api_client.get("/api/alerts/status").text + api_client.get("/api/health").text
    assert TOKEN not in raw and SECRET_PW not in raw
    health = api_client.get("/api/health").json()
    assert health["services"]["alerts"]["running"] is True and health["services"]["alerts"]["channels"] == "Telegram"


def test_test_endpoint_with_nothing_configured_and_offline(api_client):
    r = api_client.post("/api/alerts/test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "No hay ningún canal configurado" in body["message"]
    assert all(x["skipped"] for x in body["results"])
    configure(api_client)
    rt.alerts._last_test_mono = float("-inf")
    body = api_client.post("/api/alerts/test", json={}).json()  # NEXUS_OFFLINE=1: nothing leaves the machine
    tg = body["results"][0]
    assert body["ok"] is False and not tg["skipped"] and "NEXUS_OFFLINE=1" in tg["error"]


def test_test_endpoint_sends_through_the_honest_client(api_client, monkeypatch):
    configure(api_client)
    http = FakeHTTP(tg_ok(), tg_error(400, "Bad Request: chat not found"))
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(util, "http_client", http)
    rt.alerts._last_test_mono = float("-inf")
    r = api_client.post("/api/alerts/test", json={"channel": "telegram"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["message"] == "Telegram: enviado"
    assert [x["channel"] for x in body["results"]] == ["telegram"]
    assert http.calls[0][0] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert http.calls[0][1]["text"].startswith("✅ Prueba de NEXUS Terminal")
    again = api_client.post("/api/alerts/test", json={"channel": "telegram"})
    assert again.status_code == 429 and "antes de otra prueba" in again.json()["detail"]
    rt.alerts._last_test_mono = float("-inf")
    body = api_client.post("/api/alerts/test", json={}).json()
    assert body["ok"] is False and "no encuentra el chat" in body["results"][0]["error"]
    assert body["results"][1]["skipped"] and "servidor SMTP" in body["results"][1]["error"]
    st = api_client.get("/api/alerts/status").json()
    assert st["last_test"]["ok"] is False and st["counters"]["sent"] == 0
    assert TOKEN not in api_client.get("/api/alerts/status").text


def test_test_endpoint_rejects_bad_input_and_missing_service(api_client):
    assert api_client.post("/api/alerts/test", json={"channel": "sms"}).status_code == 400
    r = api_client.post("/api/alerts/test", json={"channel": 5})
    assert r.status_code == 400 and "texto" in r.json()["detail"]
    assert api_client.post("/api/alerts/test", json=["telegram"]).status_code == 400
    saved = rt.alerts
    rt.alerts = None
    try:
        r = api_client.get("/api/alerts/status")
        assert r.status_code == 503 and r.json()["detail"].startswith("Las alertas externas no están disponibles")
        assert api_client.post("/api/alerts/test").status_code == 503
        out = api_client.post("/api/terminal", json={"text": "alertas"}).json()
        assert out["ok"] is False and "no están disponibles" in out["lines"][-1]["text"]
    finally:
        rt.alerts = saved


def test_terminal_commands(api_client, monkeypatch):
    out = api_client.post("/api/terminal", json={"text": "alertas"}).json()
    texts = [line["text"] for line in out["lines"]]
    assert out["ok"] is True and texts[0] == "Alertas fuera de la app (Telegram y correo)"
    assert any(t.startswith("  ○ Telegram · desactivado") for t in texts)
    assert any("Ningún canal activo" in t for t in texts) and out["data"]["active_channels"] == 0
    configure(api_client, **{"alerts.quiet_enabled": True})
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(util, "http_client", FakeHTTP(tg_ok()))
    rt.alerts._last_test_mono = float("-inf")
    out = api_client.post("/api/terminal", json={"text": "alertas test"}).json()
    texts = [line["text"] for line in out["lines"]]
    assert out["ok"] is True and texts[0].startswith("✓ Telegram: mensaje de prueba enviado")
    assert any(t.startswith("○ Correo: no se probó") for t in texts)
    out = api_client.post("/api/terminal", json={"text": "avisos"}).json()
    texts = [line["text"] for line in out["lines"]]
    assert any(t.startswith("  ✓ Telegram · activo · chat") for t in texts)
    assert any(t.startswith("Horas de silencio: de 23:00 a 07:00") for t in texts)
    assert TOKEN not in str(out)
    bad = api_client.post("/api/terminal", json={"text": "alertas borrar"}).json()
    assert bad["ok"] is False and "Subcomando desconocido" in bad["lines"][-1]["text"]
    out = api_client.post("/api/terminal", json={"text": "alertas prueba"}).json()
    assert out["ok"] is False and "antes de otra prueba" in out["lines"][-1]["text"]


def _ts_interfaces() -> dict:
    """Interfaces of frontend/src/api/types-alerts.ts parsed like tools/contract_check.py does."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("contract_check_for_alerts", root / "tools" / "contract_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_interfaces((root / "frontend" / "src" / "api" / "types-alerts.ts").read_text(encoding="utf-8"))


def _matches(ifaces: dict, name: str, obj: dict) -> None:
    fields = ifaces[name]
    missing = [k for k, optional in fields.items() if not optional and k not in obj]
    extra = [k for k in obj if k not in fields]
    assert not missing and not extra, (name, missing, extra)


def test_json_matches_the_typescript_contract(api_client, monkeypatch):
    ifaces = _ts_interfaces()
    configure(api_client)
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(util, "http_client", FakeHTTP(tg_ok(), tg_ok()))
    rt.alerts._last_test_mono = float("-inf")
    test = api_client.post("/api/alerts/test", json={}).json()
    _matches(ifaces, "AlertsTestResponse", test)
    for r in test["results"]:
        _matches(ifaces, "AlertChannelResult", r)
    from app.alerts.classify import classify
    from tests.test_imp_alerts_fakes import macro_event

    api_client.portal.call(rt.alerts.process, [classify(macro_event("red"))])  # one real delivery (fake client)
    st = api_client.get("/api/alerts/status").json()
    _matches(ifaces, "AlertsStatus", st)
    _matches(ifaces, "AlertsCounters", st["counters"])
    _matches(ifaces, "AlertsTestResult", st["last_test"])
    for ch in st["channels"]:
        _matches(ifaces, "AlertChannelStatus", ch)
    assert st["channels"][0]["last"]["ok"] is True
    _matches(ifaces, "AlertChannelResult", st["channels"][0]["last"])
    assert st["recent"] and st["recent"][0]["status"] == "sent"
    _matches(ifaces, "AlertRecord", st["recent"][0])
