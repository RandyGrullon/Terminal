"""Integration round 2 (core): server start-up and the built UI.

- uvicorn always runs on the asyncio loop (start.sh's uvicorn[standard] would pick uvloop on Linux);
- the start-up events report what ran before the event bus existed: the database migrations (rt.db.schema_info:
  applied now, or versions newer than this code) and, on Linux, the server's own protection (app.hardening);
- /api/health `db.schema_version`;
- the built UI: /assets/* (hashed names) cached for a year as immutable, index.html revalidated (no-cache).
"""
from __future__ import annotations

import asyncio

import pytest

from app import config, hardening
from app import migrations
from tests.conftest import PG_URL, _drop_pg_schema, _test_schema


def test_main_runs_uvicorn_on_the_asyncio_loop(monkeypatch):
    import uvicorn

    from app import main

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    main.main()
    assert len(calls) == 1 and calls[0]["loop"] == "asyncio"


# ----------------------------------------------------------------------------- start-up events


@pytest.fixture
def boot(tmp_path, monkeypatch):
    """boot() -> (events, health) of one start/stop of the full app over the same database (SQLite file, or one
    PostgreSQL schema with NEXUS_TEST_PG_URL)."""
    from fastapi.testclient import TestClient

    from app.runtime import rt

    schema = _test_schema() if PG_URL else None
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "boot.db")
    monkeypatch.setattr(config, "DATABASE_URL", PG_URL)
    monkeypatch.setattr(config, "DB_SCHEMA", schema or "nexus")
    monkeypatch.setattr(config, "AUTH_MODE", "none")
    monkeypatch.setattr(config, "CORS_ORIGINS", [])
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)
    saved = hardening.server_protections()

    def run() -> tuple[list[dict], dict]:
        from app.main import create_app

        rt.reset()
        with TestClient(create_app()) as client:
            health = client.get("/api/health").json()
            events = rt.bus.recent(300)
        rt.reset()
        # the bus reloads the previous starts' events from the database: keep this start's only
        first = max(i for i, e in enumerate(events) if e["message"].startswith("Iniciando "))
        return events[first:], health

    try:
        yield run
    finally:
        rt.reset()
        with hardening._lock:
            hardening._status.clear()
            hardening._status.update(saved)
        if schema:
            asyncio.run(_drop_pg_schema(PG_URL, schema))


def _starting(events: list[dict]) -> str:
    [line] = [e["message"] for e in events if e["message"].startswith("Iniciando ")]
    return line


async def _add_future_version(version: int) -> None:
    from app.db import open_database

    db = await open_database()
    try:
        await db.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                         (version, "de_una_version_futura", 0))
    finally:
        await db.close()


def test_startup_reports_the_database_migrations(boot):
    latest = migrations.latest_version()
    labels = [m.label for m in migrations.discover()]
    events, health = boot()  # a new database: base schema + every migration
    assert f"(esquema v{latest})" in _starting(events)
    [prepared] = [e for e in events if e["message"].startswith("Base de datos preparada")]
    assert prepared["level"] == "system" and f"(esquema v{latest})" in prepared["message"]
    assert all(label in prepared["message"] for label in labels)
    assert prepared["data"]["schema"]["applied"] == labels
    assert health["db"]["schema_version"] == latest

    events, health = boot()  # the same database again: nothing to apply, nothing to report beyond the version
    assert f"(esquema v{latest})" in _starting(events)
    assert not [e for e in events if e["message"].startswith(("Base de datos preparada", "Base de datos actualizada"))]
    assert not [e for e in events if "migraciones más nuevas" in e["message"]]
    assert health["db"]["schema_version"] == latest

    asyncio.run(_add_future_version(latest + 5))  # as if a newer NEXUS had migrated this database
    events, health = boot()
    [newer] = [e for e in events if "migraciones más nuevas" in e["message"]]
    assert newer["level"] == "warn" and f"(v{latest + 5})" in newer["message"]
    assert "sigue funcionando" in newer["message"]
    assert health["db"]["schema_version"] == latest + 5


class _FakeLibc:
    def __init__(self, dumpable_after: int = 0) -> None:
        self.dumpable_after = dumpable_after


def _fake_prctl(libc: _FakeLibc, option: int, arg2: int = 0) -> int:
    return libc.dumpable_after if option == hardening.PR_GET_DUMPABLE else 0


def test_startup_reports_the_server_protection_on_linux(boot, monkeypatch):
    monkeypatch.setattr(hardening, "_platform", lambda: "linux")
    monkeypatch.setattr(hardening, "_libc", lambda: _FakeLibc())
    monkeypatch.setattr(hardening, "_prctl", _fake_prctl)
    events, _ = boot()
    assert _starting(events).endswith(" · servidor no volcable: sí")
    assert not [e for e in events if "no volcable (" in e["message"]]

    def refused(*args, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(hardening, "_prctl", refused)
    events, _ = boot()
    assert _starting(events).endswith(" · servidor no volcable: no")
    [warn] = [e for e in events if "no volcable (" in e["message"]]
    assert warn["level"] == "warn" and "(EPERM)" in warn["message"] and "/proc/<pid>/environ" in warn["message"]


def test_startup_says_nothing_about_a_protection_that_does_not_exist_here(boot, monkeypatch):
    monkeypatch.setattr(hardening, "_platform", lambda: "win32")
    events, _ = boot()
    assert "no volcable" not in _starting(events)
    assert not [e for e in events if "no volcable" in e["message"]]


# ----------------------------------------------------------------------------- the built UI


@pytest.fixture
def ui_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.runtime import rt

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><script src="/assets/index-1a2b3c4d.js"></script>',
                                     encoding="utf-8")
    (dist / "assets" / "index-1a2b3c4d.js").write_text("console.log('nexus')", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setattr(config, "FRONTEND_DIST", dist)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "AUTH_MODE", "none")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)
    from app.main import create_app

    rt.reset()
    with TestClient(create_app()) as client:
        yield client
    rt.reset()


IMMUTABLE = "public, max-age=31536000, immutable"


def test_hashed_assets_are_cached_for_a_year_and_index_is_revalidated(ui_client):
    r = ui_client.get("/assets/index-1a2b3c4d.js")
    assert r.status_code == 200 and r.text == "console.log('nexus')"
    assert r.headers["cache-control"] == IMMUTABLE
    again = ui_client.get("/assets/index-1a2b3c4d.js", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.headers["cache-control"] == IMMUTABLE
    missing = ui_client.get("/assets/index-00000000.js")
    assert missing.status_code == 404 and missing.headers.get("cache-control") != IMMUTABLE
    for path in ("/", "/index.html", "/simulador"):  # the SPA entry, also for client-side routes
        page = ui_client.get(path)
        assert page.status_code == 200 and "index-1a2b3c4d.js" in page.text, path
        assert page.headers["cache-control"] == "no-cache", path
    icon = ui_client.get("/favicon.svg")
    assert icon.status_code == 200 and icon.headers.get("cache-control") is None  # not hashed: no year-long cache
