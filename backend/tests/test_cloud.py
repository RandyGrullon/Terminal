"""Cloud mode (Supabase Postgres + auth): SQL translation, parameter coercion, auth middleware, Postgres integration.

The Postgres integration tests run only with NEXUS_TEST_PG_URL (e.g. an embedded server or a Supabase project).
"""
from __future__ import annotations

import asyncio
import math
import uuid

import numpy as np
import pytest

from app.db_pg import coerce, redact_dsn, translate
from tests.conftest import PG_URL

# ----------------------------------------------------------------------------- translation


def test_placeholders_skip_string_literals():
    sql, rid = translate("SELECT * FROM events WHERE message = '¿qué?' AND level = ? AND ts > ?")
    assert sql == "SELECT * FROM events WHERE message = '¿qué?' AND level = $1 AND ts > $2"
    assert rid is False


def test_insert_or_ignore_and_returning_id():
    sql, rid = translate("INSERT OR IGNORE INTO signals(symbol, tf, time, action, created_at) VALUES (?, ?, ?, ?, ?)")
    assert sql.startswith("INSERT INTO signals(symbol, tf, time, action, created_at) VALUES ($1, $2, $3, $4, $5)")
    assert "ON CONFLICT DO NOTHING" in sql and sql.endswith("RETURNING id") and rid


def test_insert_or_replace_uses_primary_key_upsert():
    sql, rid = translate("INSERT OR REPLACE INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES (?,?,?,?,?,?)")
    assert "ON CONFLICT (kind, key) DO UPDATE SET state = EXCLUDED.state" in sql
    assert "updated_at = EXCLUDED.updated_at" in sql and rid is False


def test_replace_into_unique_key_for_identity_table():
    sql, _ = translate("REPLACE INTO scripts (name, code, description, is_example, created_at, updated_at) VALUES (?,?,?,?,?,?)")
    assert sql.startswith("INSERT INTO scripts (name")
    assert "ON CONFLICT (name) DO UPDATE SET code = EXCLUDED.code" in sql


def test_existing_upsert_is_untouched():
    src = "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    sql, rid = translate(src)
    assert sql == src.replace("?", "$1", 1).replace("?", "$2", 1).replace("?", "$3", 1) and rid is False


def test_function_rewrites():
    sql, _ = translate("SELECT ROUND(AVG(correct) * 100, 1) AS hit, MAX(a, b) m, MIN(x, 3) n, MAX(price) p, IFNULL(z, 0) FROM t")
    assert "ROUND(CAST(AVG(correct) * 100 AS NUMERIC), 1)" in sql
    assert "GREATEST(a, b)" in sql and "LEAST(x, 3)" in sql and "MAX(price)" in sql and "COALESCE(z, 0)" in sql


def test_strftime_group_concat_like_and_json():
    sql, _ = translate(
        "SELECT strftime('%Y-%m-%d', created_at / 1000, 'unixepoch') AS day, GROUP_CONCAT(symbol), "
        "json_extract(data, '$.verdict') FROM predictions WHERE symbol LIKE ? GROUP BY day LIMIT -1"
    )
    assert "to_char(to_timestamp(created_at / 1000) AT TIME ZONE 'UTC', 'YYYY-MM-DD')" in sql
    assert "STRING_AGG(CAST(symbol AS TEXT), ',')" in sql
    assert "(CAST(data AS JSON) #>> '{verdict}')" in sql
    assert "symbol ILIKE $1" in sql and "LIMIT ALL" in sql


def test_nested_round_is_idempotent():
    sql, _ = translate("SELECT ROUND(ROUND(x, 2) * 2, 1) FROM t")
    assert sql.count("CAST(") == 2


def test_coerce_types():
    assert coerce(np.int64(5), "int8") == 5 and isinstance(coerce(np.int64(5), "int8"), int)
    assert coerce(1_700_000_000.0, "int8") == 1_700_000_000
    assert coerce(3, "float8") == 3.0 and isinstance(coerce(3, "float8"), float)
    assert coerce(float("nan"), "float8") is None
    assert coerce(True, "int8") == 1
    assert coerce(12.5, "text") == "12.5"
    assert coerce(None, "int8") is None
    assert redact_dsn("postgresql://user:secret@db.example.com:5432/postgres") == "postgresql://user:•••@db.example.com:5432/postgres"


# ----------------------------------------------------------------------------- auth middleware


@pytest.fixture
def secured_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.auth import SupabaseVerifier
    from app.runtime import rt

    async def fake_verify(self, token):
        return {"id": "u1", "email": "randy@example.com"} if token == "good-token" else None

    monkeypatch.setattr(SupabaseVerifier, "verify", fake_verify)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "AUTH_MODE", "supabase")
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setattr(config, "CORS_ORIGINS", ["https://nexus.example.app"])
    monkeypatch.setattr(config, "OFFLINE", True)
    monkeypatch.setattr(config, "BACKGROUND", False)
    from app.main import create_app

    rt.reset()
    with TestClient(create_app()) as client:
        yield client
    rt.reset()


def test_auth_blocks_api_without_token(secured_client):
    r = secured_client.get("/api/settings")
    assert r.status_code == 401 and "No autorizado" in r.json()["detail"]
    r = secured_client.get("/api/settings", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401
    r = secured_client.get("/api/settings", headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 200


def test_auth_public_endpoints(secured_client):
    r = secured_client.get("/api/health")
    assert r.status_code == 200 and r.json() == {**r.json(), "auth": "required"} and "services" not in r.json()
    r = secured_client.get("/api/health", headers={"Authorization": "Bearer good-token"})
    assert "services" in r.json() and r.json()["auth"] == "supabase"
    cfg = secured_client.get("/api/auth/config").json()
    assert cfg == {"mode": "supabase", "supabase_url": "https://example.supabase.co", "supabase_anon_key": "anon-key"}


def test_auth_cors_preflight_and_401_carry_cors_headers(secured_client):
    origin = "https://nexus.example.app"
    r = secured_client.options(
        "/api/settings", headers={"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    )
    assert r.status_code == 200 and r.headers.get("access-control-allow-origin") == origin
    r = secured_client.get("/api/settings", headers={"Origin": origin})
    assert r.status_code == 401 and r.headers.get("access-control-allow-origin") == origin


def test_auth_websocket(secured_client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with secured_client.websocket_connect("/ws") as ws:
            ws.receive_json()
    with secured_client.websocket_connect("/ws?token=good-token", headers={"origin": "https://nexus.example.app"}) as ws:
        assert ws.receive_json()["type"] == "hello"
    with pytest.raises(WebSocketDisconnect):
        with secured_client.websocket_connect("/ws?token=good-token", headers={"origin": "https://evil.example.com"}) as ws:
            ws.receive_json()


def test_public_bind_without_auth_refuses_to_start(monkeypatch):
    from app import config, main

    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    monkeypatch.setattr(config, "AUTH_MODE", "none")
    monkeypatch.delenv("NEXUS_ALLOW_INSECURE", raising=False)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2


# ----------------------------------------------------------------------------- PostgreSQL integration

pg = pytest.mark.skipif(not PG_URL, reason="set NEXUS_TEST_PG_URL to run PostgreSQL integration tests")


@pg
def test_pg_roundtrip_with_sqlite_dialect():
    from app.db_pg import PostgresDatabase

    async def scenario():
        db = await PostgresDatabase.open(PG_URL, schema=f"t_{uuid.uuid4().hex[:12]}")
        try:
            await db.init_schema()
            # identity insert returns the id; INSERT OR IGNORE on the unique key does not duplicate
            sid = await db.execute(
                "INSERT OR IGNORE INTO signals(symbol, tf, time, action, score, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("BINANCE:BTCUSDT", "1h", np.int64(1_700_000_000), "BUY", np.float64(0.52), 1_700_000_000_000),
            )
            assert sid >= 1
            await db.execute(
                "INSERT OR IGNORE INTO signals(symbol, tf, time, action, score, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("BINANCE:BTCUSDT", "1h", 1_700_000_000.0, "BUY", 0.9, 1),
            )
            assert await db.fetchval("SELECT COUNT(*) FROM signals") == 1
            # upsert via INSERT OR REPLACE
            for v in ("a", "b"):
                await db.execute(
                    "INSERT OR REPLACE INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES (?,?,?,?,?,?)",
                    ("nexus", "X|1h", v, 1, None, 1),
                )
            assert await db.fetchval("SELECT state FROM model_state WHERE kind = ? AND key = ?", ("nexus", "X|1h")) == "b"
            # NaN -> NULL, numeric aggregates -> float, ROUND / scalar MAX / strftime / LIKE
            await db.executemany(
                "INSERT INTO predictions(source, symbol, tf, created_at, target_at, price, prob_up, correct, resolved, outcome_price) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                [
                    ("scanner", "BINANCE:ETHUSDT", "1h", 1_760_000_000_000 + i, 1, 10.0, 0.6, i % 2, float("nan") if i == 0 else 11.0)
                    for i in range(4)
                ],
            )
            row = await db.fetchone(
                "SELECT ROUND(AVG(correct) * 100, 1) AS hit, MAX(COUNT(*), 2) AS n, "
                "strftime('%Y-%m-%d', MIN(created_at) / 1000, 'unixepoch') AS day FROM predictions WHERE symbol LIKE ?",
                ("binance:eth%",),
            )
            assert row["hit"] == 50.0 and isinstance(row["hit"], float) and row["n"] == 4 and row["day"] == "2025-10-09"
            assert await db.fetchval("SELECT COUNT(*) FROM predictions WHERE outcome_price IS NULL") == 1
            async with db.transaction() as tx:
                pid = await tx.execute(
                    "INSERT INTO paper_positions(account_id, symbol, side, qty, qty_open, entry_price, entry_time) VALUES (?,?,?,?,?,?,?)",
                    (1, "BINANCE:BTCUSDT", "long", 0.5, 0.5, 77000, 1_700_000_000),
                )
                assert pid >= 1
            counts = await db.table_counts()
            assert counts["signals"] == 1 and counts["paper_positions"] == 1 and db.size_bytes() > 0
        finally:
            await db.drop_schema()
            await db.close()

    asyncio.run(scenario())


@pg
def test_pg_transaction_rollback():
    from app.db_pg import PostgresDatabase

    async def scenario():
        db = await PostgresDatabase.open(PG_URL, schema=f"t_{uuid.uuid4().hex[:12]}")
        try:
            await db.init_schema()
            with pytest.raises(RuntimeError):
                async with db.transaction() as tx:
                    await tx.execute("INSERT INTO watchlist(symbol, position, added_at) VALUES (?, ?, ?)", ("A:B", 0, 0))
                    raise RuntimeError("boom")
            assert await db.fetchval("SELECT COUNT(*) FROM watchlist") == 0
        finally:
            await db.drop_schema()
            await db.close()

    asyncio.run(scenario())
