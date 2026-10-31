"""Verifier regressions for the cloud fixes (ws-auth-4401-and-no-revalidation, multi-instance-background-services,
unpinned-backend-deps):

- the WebSocket session watchdog uses the token AuthMiddleware verified: with `?token=<valid>&token=<forged>`
  Starlette's query_params returns the forged one, whose unverified expiry kept the socket open past the real one;
- a take-over after a step-down starts from a fresh settings store, so the stopped services of the previous term
  are no longer called (nor kept in memory) on every settings change;
- the leader lock session gets idle_session_timeout, so a holder that vanished without closing its connection
  (host crash, network cut) frees the lock instead of keeping every instance in standby until TCP keepalive;
- requirements.lock renders the same where uvloop is installed (Linux/macOS venvs made by start.sh).
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest

import app.main  # noqa: F401  (module-level create_app() must run with the default local config)
from app import config
from app.auth import AuthMiddleware
from app.db_pg import LOCK_IDLE_TIMEOUT_S, AdvisoryLock, advisory_lock_key
from tests.conftest import PG_URL
from tests.test_fix_cloud_leader import FakeLock, _wait_for
from tests.test_fix_cloud_ws import OWNER, FakeSocket, FakeVerifier, _manager, make_jwt

# ----------------------------------------------------------------------------- WebSocket: the verified token


class _OkVerifier:
    async def verify(self, token, **_kw):
        return OWNER if token == "tok-valid" else None


def test_auth_middleware_records_the_token_it_verified():
    seen: list[dict] = []

    async def inner(scope, receive, send):
        seen.append(dict(scope["state"]))

    async def scenario():
        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            raise AssertionError(f"unexpected {message}")

        mw = AuthMiddleware(inner, verifier=_OkVerifier())
        scope = {"type": "websocket", "path": "/ws", "query_string": b"token=tok-valid&token=tok-forged", "headers": []}
        await mw(scope, receive, send)

    asyncio.run(scenario())
    assert seen and seen[0]["token"] == "tok-valid" and seen[0]["user"] == OWNER


def test_socket_watch_uses_the_verified_token_not_a_repeated_query_value():
    async def scenario():
        verified = make_jwt(time.time() + 0.3)
        forged = make_jwt(time.time() + 10**9)  # never accepted by Supabase
        sock = FakeSocket(forged, OWNER)  # query_params: what Starlette returns for ?token=<verified>&token=<forged>
        sock.scope["state"]["token"] = verified  # what AuthMiddleware checked
        verifier = FakeVerifier({verified: OWNER})
        verifier.down = True  # Supabase unreachable: only the expiry can end the session
        task = asyncio.create_task(_manager(verifier).endpoint(sock))
        await asyncio.wait_for(task, timeout=5)  # before the fix the forged expiry kept it open (timeout here)
        return sock

    sock = asyncio.run(scenario())
    assert sock.closed is not None and sock.closed[0] == 4401 and "caducada" in sock.closed[1]


# ----------------------------------------------------------------------------- take-over: fresh settings store


class ListeningService:
    """Registers a settings listener on start, like MarketDataHub, MacroService and PaperEngine."""

    instances: list["ListeningService"] = []

    def __init__(self, rt) -> None:
        self.rt = rt
        self.seen: list[str] = []
        self.stopped = False
        ListeningService.instances.append(self)

    async def start(self) -> None:
        self.rt.settings.add_listener(lambda key, _value: self.seen.append(key))

    async def stop(self) -> None:
        self.stopped = True

    def status(self) -> dict:
        return {"running": not self.stopped}


def test_take_over_after_step_down_drops_the_old_services_listeners(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import Database
    from app.main import create_app
    from app.runtime import rt

    ListeningService.instances = []
    FakeLock.holder = None
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "relay.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "AUTH_MODE", "none")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", True)
    monkeypatch.setattr(main, "SERVICES", [("maintenance", __name__, "ListeningService")])
    monkeypatch.setattr(main, "LEADER_RETRY_S", 0.05)
    monkeypatch.setattr(main, "LEADER_CHECK_S", 0.05)
    monkeypatch.setattr(Database, "advisory_lock", lambda self, name="leader": FakeLock(), raising=False)
    rt.reset()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/api/health").json()["role"] == "leader"
            [first] = ListeningService.instances
            thief = FakeLock()
            FakeLock.holder, thief.held = thief, True  # the lock session was lost: step down
            assert _wait_for(lambda: first.stopped)
            asyncio.run(thief.release())  # free again: take over with fresh services
            assert _wait_for(lambda: len(ListeningService.instances) == 2)
            assert _wait_for(lambda: client.get("/api/health").json()["role"] == "leader")
            second = ListeningService.instances[1]
            assert client.put("/api/settings", json={"risk.per_trade_pct": 1.5}).status_code == 200
            assert second.seen == ["risk.per_trade_pct"]
            assert first.seen == []  # the stopped service of the previous term is no longer called
    finally:
        rt.reset()
        FakeLock.holder = None


# ----------------------------------------------------------------------------- requirements.lock on Linux / macOS


def test_lock_render_is_the_same_where_uvloop_is_installed(monkeypatch):
    # bash start.sh installs uvicorn[standard], which brings uvloop on Linux/macOS: the lock rendered there must stay
    # the one generated on Windows (uvloop is left out of the image on purpose), or the drift test fails there
    from tests.test_fix_cloud_deps import _lock_tool

    tool = _lock_tool()
    expected = tool.render()
    real = tool.md.distribution

    class _Uvloop:
        version = "0.21.0"
        metadata = {"Name": "uvloop"}
        requires: list[str] = []

    monkeypatch.setattr(tool.md, "distribution", lambda name: _Uvloop() if name.lower() == "uvloop" else real(name))
    assert tool.render() == expected
    assert "uvloop==" not in expected and "uvloop" in expected  # named in the header, never pinned


# ----------------------------------------------------------------------------- PostgreSQL: abandoned lock session

pg = pytest.mark.skipif(not PG_URL, reason="set NEXUS_TEST_PG_URL to run PostgreSQL integration tests")


@pg
def test_pg_lock_session_has_an_idle_limit():
    async def scenario():
        lock = AdvisoryLock(PG_URL, advisory_lock_key(f"t_{uuid.uuid4().hex[:12]}"))
        try:
            assert await lock.try_acquire()
            ms = int(await lock._conn.fetchval("SELECT setting FROM pg_settings WHERE name = 'idle_session_timeout'"))
            assert ms == int(LOCK_IDLE_TIMEOUT_S * 1000)
        finally:
            await lock.release()

    asyncio.run(scenario())


@pg
def test_pg_lock_of_a_silent_holder_is_freed():
    async def scenario():
        key = advisory_lock_key(f"t_{uuid.uuid4().hex[:12]}")
        holder = AdvisoryLock(PG_URL, key, idle_timeout_s=1.0)
        other = AdvisoryLock(PG_URL, key)
        try:
            assert await holder.try_acquire()
            assert not await other.try_acquire()
            # the holder stops probing without closing its connection (host crash, network cut)
            deadline = time.monotonic() + 10
            while not await other.try_acquire():
                assert time.monotonic() < deadline, "the silent holder kept the lock"
                await asyncio.sleep(0.25)
            assert not await holder.alive()  # its session was ended by PostgreSQL
        finally:
            for lock in (holder, other):
                await lock.release()

    asyncio.run(scenario())
