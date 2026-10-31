"""Single active instance on a shared database (finding multi-instance-background-services).

Only the holder of the schema's advisory lock starts the services; a second instance (deploy overlap, a second Fly
machine) stays in standby: /api/health answers with role "standby", the rest of the API gets 503, WebSockets 1013,
and it takes over when the active instance stops. The lifespan logic runs on SQLite with a fake lock; the real
PostgreSQL lock (dedicated connection) runs with NEXUS_TEST_PG_URL.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest

import app.main  # noqa: F401  (module-level create_app() must run with the default local config)
from app import config
from app.db_pg import AdvisoryLock, advisory_lock_key
from tests.conftest import PG_URL


class FakeService:
    """Stands in for every real service: records construction, start (with the BACKGROUND flag) and stop."""

    instances: list["FakeService"] = []

    def __init__(self, rt) -> None:
        self.started = self.stopped = False
        self.background: bool | None = None
        FakeService.instances.append(self)

    async def start(self) -> None:
        self.started, self.background = True, config.BACKGROUND

    async def stop(self) -> None:
        self.stopped = True

    def status(self) -> dict:
        return {"running": self.started and not self.stopped}


class FakeLock:
    """In-process stand-in for app.db_pg.AdvisoryLock (one holder at a time, like the database)."""

    holder: "FakeLock | None" = None

    def __init__(self) -> None:
        self.held = False

    async def try_acquire(self) -> bool:
        if FakeLock.holder is None or FakeLock.holder is self:
            FakeLock.holder, self.held = self, True
        return self.held

    async def alive(self) -> bool:
        self.held = self.held and FakeLock.holder is self
        return self.held

    async def release(self) -> None:
        if FakeLock.holder is self:
            FakeLock.holder = None
        self.held = False


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _election_env(monkeypatch, tmp_path, *, database_url: str = "", schema: str = "nexus") -> None:
    from app import main

    FakeService.instances = []
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "leader.db")
    monkeypatch.setattr(config, "DATABASE_URL", database_url)
    monkeypatch.setattr(config, "DB_SCHEMA", schema)
    monkeypatch.setattr(config, "AUTH_MODE", "none")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", True)  # the election only runs when background work is enabled
    monkeypatch.setattr(main, "SERVICES", [("maintenance", __name__, "FakeService")])
    monkeypatch.setattr(main, "LEADER_RETRY_S", 0.05)
    monkeypatch.setattr(main, "LEADER_CHECK_S", 0.05)


def _assert_standby(client, *, services_running: bool = False) -> None:
    from starlette.websockets import WebSocketDisconnect

    health = client.get("/api/health").json()
    assert health["role"] == "standby"
    assert services_running or "maintenance" not in health["services"]
    r = client.get("/api/settings")
    assert r.status_code == 503 and r.json()["detail"].startswith("Instancia en espera") and r.headers["retry-after"]
    assert client.get("/api/auth/config").status_code == 200
    with client.websocket_connect("/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1013


# ----------------------------------------------------------------------------- lifespan logic (fake lock, SQLite)


@pytest.fixture
def fake_lock_db(monkeypatch, tmp_path):
    from app.db import Database
    from app.runtime import rt

    _election_env(monkeypatch, tmp_path)
    FakeLock.holder = None
    monkeypatch.setattr(Database, "advisory_lock", lambda self, name="leader": FakeLock(), raising=False)
    rt.reset()
    yield
    rt.reset()
    FakeLock.holder = None


def test_standby_until_the_active_instance_stops(fake_lock_db):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.runtime import rt

    other = FakeLock()
    asyncio.run(other.try_acquire())  # another instance is active
    with TestClient(create_app()) as client:
        _assert_standby(client)
        assert FakeService.instances == []  # never constructed on a standby (their start() would hit the other one)
        asyncio.run(other.release())  # the active instance stops
        assert _wait_for(lambda: client.get("/api/health").json()["role"] == "leader")
        assert client.get("/api/settings").status_code == 200
        [svc] = FakeService.instances
        assert svc.started and svc.background is True and rt.leader.takeovers == 1
        assert any("toma el relevo" in e["message"] for e in rt.bus.recent(100))
    assert svc.stopped and FakeLock.holder is None  # released on shutdown


def test_lone_instance_is_leader_and_steps_down_when_the_lock_is_lost(fake_lock_db):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.runtime import rt

    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["role"] == "leader"
        [first] = FakeService.instances
        assert first.started and rt.maintenance is first
        thief = FakeLock()
        FakeLock.holder = thief  # our session died and another instance took the lock
        thief.held = True
        assert _wait_for(lambda: first.stopped)
        assert _wait_for(lambda: client.get("/api/health").json()["role"] == "standby")
        assert rt.maintenance is None and len(FakeService.instances) == 1
        _assert_standby(client)
        asyncio.run(thief.release())
        assert _wait_for(lambda: client.get("/api/health").json()["role"] == "leader")
        assert len(FakeService.instances) == 2 and FakeService.instances[1].started  # fresh services, fresh state


def test_no_election_without_background_work(fake_lock_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(config, "BACKGROUND", False)
    asyncio.run(FakeLock().try_acquire())  # even with the lock taken elsewhere
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["role"] == "single"
        assert client.get("/api/settings").status_code == 200


def test_standby_gate_only_acts_in_standby(api_client):
    from app.runtime import rt

    assert api_client.get("/api/health").json()["role"] == "single"
    rt.role = "standby"
    try:
        _assert_standby(api_client, services_running=True)
        assert api_client.options("/api/settings").status_code != 503  # CORS preflights are not refused
    finally:
        rt.role = "single"
    assert api_client.get("/api/settings").status_code == 200


def test_lock_key_is_stable_and_distinct_per_schema():
    a, b = advisory_lock_key("nexus"), advisory_lock_key("t_0123456789ab")
    assert a == advisory_lock_key("nexus") and a != b
    assert all(-(2**63) <= k < 2**63 for k in (a, b))


# ----------------------------------------------------------------------------- PostgreSQL (real advisory lock)

pg = pytest.mark.skipif(not PG_URL, reason="set NEXUS_TEST_PG_URL to run PostgreSQL integration tests")


class LoopThread:
    """An event loop in a thread for the 'other instance' (asyncpg connections are bound to their loop)."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float = 30.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


async def _terminate_backend(pid: int) -> None:
    import asyncpg

    conn = await asyncpg.connect(PG_URL)
    try:
        await conn.fetchval("SELECT pg_terminate_backend($1)", pid)
    finally:
        await conn.close()


async def _drop_schema(schema: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(PG_URL)
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await conn.close()


@pg
def test_pg_advisory_lock_is_exclusive_per_schema_and_dies_with_its_session():
    async def scenario():
        key = advisory_lock_key(f"t_{uuid.uuid4().hex[:12]}")
        a, b = AdvisoryLock(PG_URL, key), AdvisoryLock(PG_URL, key)
        other_schema = AdvisoryLock(PG_URL, advisory_lock_key(f"t_{uuid.uuid4().hex[:12]}"))
        try:
            assert await a.try_acquire() and await a.alive()
            assert not await b.try_acquire()  # same schema: exclusive
            assert await other_schema.try_acquire()  # another schema never contends
            await a.release()
            assert await b.try_acquire()  # released on stop
            await _terminate_backend(b._conn.get_server_pid())  # the session dies (network cut, crash...)
            assert not await b.alive()
            assert await a.try_acquire()  # ...and the lock went with it
        finally:
            for lock in (a, b, other_schema):
                await lock.release()

    asyncio.run(scenario())


@pg
def test_pg_second_instance_waits_and_takes_over(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.runtime import rt

    schema = f"t_{uuid.uuid4().hex[:12]}"
    _election_env(monkeypatch, tmp_path, database_url=PG_URL, schema=schema)
    other_loop = LoopThread()
    other = AdvisoryLock(PG_URL, advisory_lock_key(schema))
    rt.reset()
    try:
        assert other_loop.run(other.try_acquire())  # the active instance (e.g. the previous deployment)
        with TestClient(create_app()) as client:
            _assert_standby(client)
            assert FakeService.instances == []
            other_loop.run(other.release())  # it stops
            assert _wait_for(lambda: client.get("/api/health").json()["role"] == "leader")
            [svc] = FakeService.instances
            assert svc.started and client.get("/api/settings").status_code == 200
            # the active instance loses its lock session: it steps down, then takes the free lock again
            other_loop.run(_terminate_backend(rt.leader.lock._conn.get_server_pid()))
            assert _wait_for(lambda: svc.stopped)
            assert _wait_for(lambda: len(FakeService.instances) == 2 and FakeService.instances[1].started)
            assert _wait_for(lambda: client.get("/api/health").json()["role"] == "leader")
        assert other_loop.run(other.try_acquire())  # released on shutdown: the next instance can start
    finally:
        other_loop.run(other.release())
        other_loop.close()
        rt.reset()
        asyncio.run(_drop_schema(schema))


@pg
def test_pg_instances_starting_together_initialise_the_schema_once():
    from app.db_pg import PostgresDatabase

    async def scenario():
        schema = f"t_{uuid.uuid4().hex[:12]}"
        dbs = [await PostgresDatabase.open(PG_URL, schema=schema) for _ in range(3)]
        try:
            # fresh database, simultaneous start (e.g. two Fly machines): CREATE ... IF NOT EXISTS must not race
            await asyncio.gather(*(db.init_schema() for db in dbs))
            assert await dbs[0].fetchval("SELECT value FROM meta WHERE key = 'schema_version'") is not None
        finally:
            await dbs[0].drop_schema()
            for db in dbs:
                await db.close()

    asyncio.run(scenario())
