"""Cloud start-up guards: Supabase auth without NEXUS_ALLOWED_EMAILS fails closed (finding allowlist-fail-open), a
public bind without NEXUS_DATABASE_URL refuses to start (cloud-sqlite-ephemeral) and NEXUS_ALLOWED_HOSTS never blocks
the platform health check on /api/health (allowed-hosts-breaks-railway-healthcheck)."""
from __future__ import annotations

import pytest

import app.main  # noqa: F401  (imported with the default local config: its module-level create_app() must succeed)
from app import config


@pytest.fixture
def supabase_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "startup.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "AUTH_MODE", "supabase")
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setattr(config, "ALLOWED_EMAILS", [])
    monkeypatch.setattr(config, "ALLOW_ANY_SUPABASE_USER", False)
    monkeypatch.setattr(config, "ALLOW_LOCAL_DB", False)
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)
    from app.runtime import rt

    rt.reset()
    yield monkeypatch
    rt.reset()


def _boot_events() -> list[dict]:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.runtime import rt

    with TestClient(create_app()):
        return rt.bus.recent(200)


def test_supabase_without_allowlist_refuses_to_start(supabase_env):
    from app.main import create_app

    with pytest.raises(RuntimeError) as exc:
        create_app()
    assert "NEXUS_ALLOWED_EMAILS" in str(exc.value) and "NEXUS_ALLOW_ANY_SUPABASE_USER" in str(exc.value)


def test_any_supabase_user_is_an_explicit_logged_opt_in(supabase_env):
    supabase_env.setattr(config, "ALLOW_ANY_SUPABASE_USER", True)
    messages = [e["message"] for e in _boot_events()]
    assert any("cualquier usuario del proyecto" in m for m in messages)
    assert any("NEXUS_ALLOW_ANY_SUPABASE_USER=1" in m for m in messages)


def test_startup_logs_allowlist_size_and_warns_about_sqlite(supabase_env):
    supabase_env.setattr(config, "ALLOWED_EMAILS", ["a@example.com"])
    events = _boot_events()
    messages = [e["message"] for e in events]
    assert any("login Supabase (1 correo permitido)" in m for m in messages)
    assert any(e["level"] == "warn" and "Modo nube con SQLite local" in e["message"] for e in events)


def test_public_bind_without_database_url_refuses_to_start(supabase_env, capsys):
    import uvicorn

    from app import main

    calls: list[dict] = []
    supabase_env.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    supabase_env.setattr(config, "HOST", "0.0.0.0")
    supabase_env.delenv("NEXUS_ALLOW_INSECURE", raising=False)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2 and not calls
    out = capsys.readouterr().out
    assert "NEXUS_DATABASE_URL" in out and "docs/DEPLOY.md" in out and "NEXUS_ALLOW_LOCAL_DB=1" in out
    # explicit opt-in (persistent volume) or a real database URL: it starts
    supabase_env.setattr(config, "ALLOW_LOCAL_DB", True)
    main.main()
    supabase_env.setattr(config, "ALLOW_LOCAL_DB", False)
    supabase_env.setattr(config, "DATABASE_URL", "postgresql://u:p@db.example.com:5432/postgres")
    main.main()
    assert len(calls) == 2 and calls[0]["host"] == "0.0.0.0"


@pytest.fixture
def public_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.runtime import rt

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hosts.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "AUTH_MODE", "none")
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)
    monkeypatch.setenv("NEXUS_ALLOWED_HOSTS", "nexus-production.up.railway.app")
    from app.main import create_app

    rt.reset()
    with TestClient(create_app()) as client:
        yield client
    rt.reset()


def test_allowed_hosts_do_not_block_platform_health_checks(public_client):
    r = public_client.get("/api/health", headers={"host": "healthcheck.railway.app"})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = public_client.get("/api/settings", headers={"host": "healthcheck.railway.app"})
    assert r.status_code == 400  # every other path keeps the Host check
    r = public_client.get("/api/settings", headers={"host": "nexus-production.up.railway.app"})
    assert r.status_code == 200


def test_local_bind_keeps_checking_health_host(api_client):
    # DNS-rebinding protection stays on for every path when the server only listens locally
    assert api_client.get("/api/health", headers={"host": "healthcheck.railway.app"}).status_code == 400
