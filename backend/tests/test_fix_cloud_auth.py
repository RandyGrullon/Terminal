"""Cloud auth regressions (findings transient-auth-failure-logout, ws-auth-4401-and-no-revalidation, backend side):
a Supabase outage is HTTP 503 / WebSocket 1013 (never 401, never cached as a bad token); a rejected token is 401 /
WebSocket 4401 sent after accepting. Supabase is faked with httpx.MockTransport (no network)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.auth import (
    UNAVAILABLE_DETAIL,
    WS_TRY_AGAIN,
    WS_UNAUTHORIZED,
    AuthMiddleware,
    SupabaseVerifier,
    VerifierUnavailable,
)

OWNER = {"id": "u-0", "email": "owner@example.com"}


class FakeSupabase:
    """GET /auth/v1/user: mode 'up' (known tokens -> 200, others -> 401), 'down' (network error) or an HTTP status."""

    def __init__(self) -> None:
        self.mode: str | int = "up"
        self.users = {"tok-owner": OWNER, "tok-stranger": {"id": "u-9", "email": "stranger@example.org"}}
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "down":
            raise httpx.ConnectError("simulated egress failure", request=request)
        if isinstance(self.mode, int):
            return httpx.Response(self.mode, json={"msg": "upstream problem"})
        user = self.users.get(request.headers.get("authorization", "")[7:])
        return httpx.Response(200, json=user) if user else httpx.Response(403, json={"msg": "invalid JWT"})

    def verifier(self, allowed=("owner@example.com",)) -> SupabaseVerifier:
        v = SupabaseVerifier("https://fake.supabase.co", "anon", list(allowed))
        v._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return v


def test_verify_outage_raises_and_is_not_cached():
    fake = FakeSupabase()

    async def scenario():
        v = fake.verifier()
        for mode in ("down", 503, 502, 429):
            fake.mode = mode
            with pytest.raises(VerifierUnavailable):
                await v.verify("tok-owner")
        fake.mode = "up"  # recovered: the same token works at once (the old code cached 5xx/429 as bad for 15 s)
        assert await v.verify("tok-owner") == OWNER
        await v.close()

    asyncio.run(scenario())


def test_verify_definitive_rejections_are_cached():
    fake = FakeSupabase()

    async def scenario():
        v = fake.verifier()
        assert await v.verify("tok-unknown") is None  # 403 from Supabase
        assert await v.verify("tok-stranger") is None  # valid account, not in the allowlist
        calls = fake.calls
        assert await v.verify("tok-unknown") is None and await v.verify("tok-stranger") is None
        assert fake.calls == calls  # answered from the bad-token cache
        fake.mode = 401
        assert await v.verify("tok-other") is None
        await v.close()

    asyncio.run(scenario())


def test_verify_fresh_bypasses_positive_cache():
    fake = FakeSupabase()

    async def scenario():
        v = fake.verifier()
        assert await v.verify("tok-owner") == OWNER
        fake.users.pop("tok-owner")  # session revoked at Supabase
        assert await v.verify("tok-owner") == OWNER  # REST keeps the short positive cache
        assert await v.verify("tok-owner", fresh=True) is None  # WebSocket re-checks ask Supabase
        assert await v.verify("tok-owner") is None  # and the rejection is now cached for REST too
        await v.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- ASGI level


class _StubVerifier:
    def __init__(self, outcome):
        self.outcome = outcome

    async def verify(self, token, **_kw):
        if self.outcome == "down":
            raise VerifierUnavailable("ConnectError")
        return OWNER if self.outcome == "ok" else None


def _run_ws(outcome: str) -> tuple[list[dict], bool]:
    reached: list[bool] = []

    async def inner(scope, receive, send):
        reached.append(True)

    async def scenario():
        sent: list[dict] = []
        queue = [{"type": "websocket.connect"}]

        async def receive():
            return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            sent.append(message)

        mw = AuthMiddleware(inner, verifier=_StubVerifier(outcome))
        await mw({"type": "websocket", "path": "/ws", "query_string": b"token=abc", "headers": []}, receive, send)
        return sent

    return asyncio.run(scenario()), bool(reached)


def test_websocket_rejections_are_sent_after_accept():
    sent, reached = _run_ws("bad")
    # close before accept would become an HTTP 403 handshake failure (browsers only see 1006)
    assert [m["type"] for m in sent] == ["websocket.accept", "websocket.close"] and not reached
    assert sent[1]["code"] == WS_UNAUTHORIZED == 4401
    sent, reached = _run_ws("down")
    assert [m["type"] for m in sent] == ["websocket.accept", "websocket.close"] and not reached
    assert sent[1]["code"] == WS_TRY_AGAIN == 1013 and len(sent[1]["reason"].encode()) <= 123
    sent, reached = _run_ws("ok")
    assert sent == [] and reached  # authenticated: handed to the app untouched


# ----------------------------------------------------------------------------- whole app


@pytest.fixture
def cloud_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.runtime import rt

    fake = FakeSupabase()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "AUTH_MODE", "supabase")
    monkeypatch.setattr(config, "SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(config, "ALLOWED_EMAILS", ["owner@example.com"])
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)
    from app.main import create_app

    rt.reset()
    app = create_app()
    rt.auth._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        yield client, fake
    rt.reset()


def test_supabase_outage_is_503_not_401(cloud_app):
    client, fake = cloud_app
    owner = {"Authorization": "Bearer tok-owner"}
    fake.mode = "down"
    r = client.get("/api/settings", headers=owner)
    assert r.status_code == 503 and r.json()["detail"] == UNAVAILABLE_DETAIL and r.headers.get("retry-after")
    fake.mode = 503
    assert client.get("/api/settings", headers=owner).status_code == 503
    # public endpoints keep answering (anonymous view) during the outage
    r = client.get("/api/health", headers=owner)
    assert r.status_code == 200 and r.json()["auth"] == "required"
    fake.mode = "up"
    assert client.get("/api/settings", headers=owner).status_code == 200
    r = client.get("/api/settings", headers={"Authorization": "Bearer tok-unknown"})
    assert r.status_code == 401 and "No autorizado" in r.json()["detail"]
    r = client.get("/api/settings", headers={"Authorization": "Bearer tok-stranger"})
    assert r.status_code == 401  # valid Supabase account outside NEXUS_ALLOWED_EMAILS


def test_supabase_outage_closes_websocket_with_1013(cloud_app):
    from starlette.websockets import WebSocketDisconnect

    client, fake = cloud_app
    fake.mode = "down"
    with client.websocket_connect("/ws?token=tok-owner") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1013
    fake.mode = "up"
    with client.websocket_connect("/ws?token=tok-unknown") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401
    with client.websocket_connect("/ws?token=tok-owner") as ws:
        assert ws.receive_json()["type"] == "hello"
