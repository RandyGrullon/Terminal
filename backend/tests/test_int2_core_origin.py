"""Integration round 2 (core): CSRF protection (app.main.OriginCheckMiddleware).

A POST / PUT / PATCH / DELETE carrying an Origin header gets 403 (Spanish detail) before any endpoint runs unless the
Origin is the server's own (the Host the request was sent to), the Vite dev server or one of NEXUS_CORS_ORIGINS.
Requests without Origin (terminal client, curl, tests) and safe methods pass. Local mode and cloud mode (Supabase
auth, a public bind with NEXUS_ALLOWED_HOSTS, a separate frontend origin) are both covered, and the middleware order
keeps CORS preflights and the CORS headers of the auth errors working.
"""
from __future__ import annotations

import pytest

from app import config
from app.main import OriginCheckMiddleware, origin_key

EVIL = "https://evil.example.com"
FRONTEND = "https://nexus.example.app"  # NEXUS_CORS_ORIGINS in the cloud fixture
PUBLIC_HOST = "nexus.fly.dev"


def _detail(r) -> str:
    return r.json()["detail"]


def test_origin_key_accepts_only_browser_origins():
    assert origin_key("http://127.0.0.1:8420") == "http://127.0.0.1:8420"
    assert origin_key("HTTPS://Nexus.Example.APP") == "https://nexus.example.app"
    assert origin_key("http://[::1]:8420") == "http://[::1]:8420"
    for bad in ("null", "", "file://", "chrome-extension://abc", "https://a.example/ruta", "https://u:p@a.example",
                "https://a.example?x=1", "http://[::1"):
        assert origin_key(bad) is None, bad


def test_own_origin_dev_server_and_configured_origins_are_allowed():
    mw = OriginCheckMiddleware(app=None, allowed_origins=["http://localhost:5173", FRONTEND + "/"])
    assert mw.allows("http://localhost:5173", "127.0.0.1:8420")
    assert mw.allows(FRONTEND, PUBLIC_HOST)
    assert mw.allows("http://127.0.0.1:8420", "127.0.0.1:8420")  # a page this server served
    assert mw.allows("https://NEXUS.fly.dev", "nexus.fly.dev")  # host names are case-insensitive
    assert not mw.allows("http://127.0.0.1:3000", "127.0.0.1:8420")  # another local port is another site
    assert not mw.allows(EVIL, "127.0.0.1:8420")
    assert not mw.allows(EVIL, None)
    assert not mw.allows("null", "127.0.0.1:8420")


# ----------------------------------------------------------------------------- local mode


def test_local_cross_site_changes_are_refused_before_the_endpoint_runs(api_client):
    from app.runtime import rt

    before = api_client.get("/api/settings").json()["settings"]["risk.per_trade_pct"]
    r = api_client.put("/api/settings", json={"risk.per_trade_pct": before + 1}, headers={"Origin": EVIL})
    assert r.status_code == 403
    assert "Origen no permitido" in _detail(r) and EVIL in _detail(r) and "CSRF" in _detail(r)
    assert r.headers.get("access-control-allow-origin") is None  # the foreign page cannot even read the refusal
    # a "simple" request a browser sends cross-site without any preflight (an HTML form / text/plain)
    r = api_client.post("/api/terminal", content='{"text": "settings risk.per_trade_pct 4"}',
                        headers={"Origin": EVIL, "Content-Type": "text/plain"})
    assert r.status_code == 403
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert api_client.request(method, "/api/db/vacuum", headers={"Origin": EVIL}).status_code == 403, method
    assert api_client.post("/api/db/vacuum", headers={"Origin": "null"}).status_code == 403  # sandboxed frame
    assert api_client.get("/api/settings").json()["settings"]["risk.per_trade_pct"] == before  # nothing changed
    warnings = [e for e in rt.bus.recent(100, source="system") if "rechazada: origen no permitido" in e["message"]]
    assert len(warnings) == 1 and warnings[0]["level"] == "warn"  # rate limited: one warning, not one per request


def test_local_ui_dev_server_and_clients_without_origin_still_work(api_client):
    body = {"text": "echo hola"}
    for headers in ({}, {"Origin": "http://testserver"}, {"Origin": "http://localhost:5173"},
                    {"Origin": "http://127.0.0.1:5173"}):
        r = api_client.post("/api/terminal", json=body, headers=headers)
        assert r.status_code == 200 and r.json()["lines"][0]["text"] == "hola", headers
    r = api_client.put("/api/settings", json={"risk.per_trade_pct": 1.5}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and r.json()["settings"]["risk.per_trade_pct"] == 1.5
    # safe methods are never refused (they change nothing; CORS keeps the answer from a foreign page)
    assert api_client.get("/api/health", headers={"Origin": EVIL}).status_code == 200
    r = api_client.options("/api/settings", headers={"Origin": "http://localhost:5173",
                                                     "Access-Control-Request-Method": "PUT"})
    assert r.status_code == 200 and r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_another_port_of_this_machine_is_another_site(api_client):
    r = api_client.post("/api/terminal", json={"text": "echo hola"}, headers={"Origin": "http://testserver:3000"})
    assert r.status_code == 403


# ----------------------------------------------------------------------------- cloud mode


@pytest.fixture
def cloud_client(tmp_path, monkeypatch):
    """Supabase auth (fake verifier), a public bind with NEXUS_ALLOWED_HOSTS and a separate frontend origin."""
    from fastapi.testclient import TestClient

    from app.auth import SupabaseVerifier
    from app.runtime import rt

    calls: list[str] = []

    async def fake_verify(self, token, **_kw):
        calls.append(token)
        return {"id": "u1", "email": "randy@example.com"} if token == "good-token" else None

    monkeypatch.setattr(SupabaseVerifier, "verify", fake_verify)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "origin.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "AUTH_MODE", "supabase")
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setattr(config, "ALLOWED_EMAILS", ["randy@example.com"])
    monkeypatch.setattr(config, "CORS_ORIGINS", [FRONTEND])
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)
    monkeypatch.setenv("NEXUS_ALLOWED_HOSTS", f"{PUBLIC_HOST},testserver")
    from app.main import create_app

    rt.reset()
    with TestClient(create_app()) as client:
        client.verify_calls = calls
        yield client
    rt.reset()


AUTH = {"Authorization": "Bearer good-token"}


def test_cloud_frontend_origin_and_the_ui_served_by_the_backend_are_allowed(cloud_client):
    body = {"text": "echo hola"}
    r = cloud_client.post("/api/terminal", json=body, headers={**AUTH, "Origin": FRONTEND})
    assert r.status_code == 200 and r.headers.get("access-control-allow-origin") == FRONTEND
    # the UI served by the backend itself (Docker image): same origin as the public host
    r = cloud_client.post("/api/terminal", json=body,
                          headers={**AUTH, "Origin": f"https://{PUBLIC_HOST}", "Host": PUBLIC_HOST})
    assert r.status_code == 200
    r = cloud_client.post("/api/terminal", json=body, headers=AUTH)  # scripts / curl: no Origin
    assert r.status_code == 200


def test_cloud_cross_site_change_is_refused_even_with_a_valid_token(cloud_client):
    r = cloud_client.post("/api/terminal", json={"text": "echo hola"}, headers={**AUTH, "Origin": EVIL})
    assert r.status_code == 403 and "NEXUS_CORS_ORIGINS" in _detail(r)
    assert r.headers.get("access-control-allow-origin") is None
    cloud_client.verify_calls.clear()
    r = cloud_client.put("/api/settings", json={"risk.per_trade_pct": 2}, headers={"Origin": EVIL})
    assert r.status_code == 403
    assert cloud_client.verify_calls == []  # refused before Auth asked Supabase about any token


def test_cloud_middleware_order_keeps_cors_and_auth_answers(cloud_client):
    # preflight of the frontend: answered by CORS (outermost), never refused by the origin check or Auth
    r = cloud_client.options("/api/settings", headers={"Origin": FRONTEND, "Access-Control-Request-Method": "PUT",
                                                       "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 200 and r.headers.get("access-control-allow-origin") == FRONTEND
    # an allowed origin without a session: Auth's 401, still readable by the frontend (CORS headers)
    r = cloud_client.post("/api/terminal", json={"text": "echo hola"}, headers={"Origin": FRONTEND})
    assert r.status_code == 401 and r.headers.get("access-control-allow-origin") == FRONTEND
    # the host check still runs for an unknown Host, whatever the origin
    r = cloud_client.post("/api/terminal", json={"text": "echo hola"},
                          headers={**AUTH, "Origin": "https://otro.example.com", "Host": "otro.example.com"})
    assert r.status_code == 400
