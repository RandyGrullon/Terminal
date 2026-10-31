"""Integration round (int-be-infra): schema documentation and the idempotent, lock-light RLS block.

- Both schema files document model_state kind='nexus_veto' and the prediction_daily table (item 1, deterministic).
- On PostgreSQL, init_schema enables RLS on every table and, run again (it runs at every boot), re-enables nothing:
  the DO block only alters tables where pg_class.relrowsecurity is still false, so it takes no ACCESS EXCLUSIVE lock
  on a redeploy next to an active instance (item 2).
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.conftest import PG_URL

BACKEND = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------- schema documentation (item 1)


@pytest.mark.parametrize("name", ["schema.sql", "schema_pg.sql"])
def test_schema_documents_nexus_veto_state_and_prediction_daily(name):
    text = (BACKEND / "app" / name).read_text(encoding="utf-8")
    # model_state kind='nexus_veto' with its key and state shape
    assert "nexus_veto" in text, name
    assert "{version, entries:" in text, name
    # the prediction_daily table exists and is documented, not just declared
    assert "CREATE TABLE IF NOT EXISTS prediction_daily" in text, name
    assert "Daily aggregates of resolved predictions" in text, name


# ----------------------------------------------------------------------------- RLS block on PostgreSQL (item 2)

pg = pytest.mark.skipif(not PG_URL, reason="set NEXUS_TEST_PG_URL to run PostgreSQL integration tests")


@pg
def test_rls_enabled_on_all_tables_and_init_is_idempotent():
    from app.db_pg import PostgresDatabase

    async def tables_with_rls_off(db: "PostgresDatabase") -> set[str]:
        rows = await db.fetchall(
            "SELECT c.relname AS relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ? AND c.relkind = 'r' AND NOT c.relrowsecurity",
            (db.schema,),
        )
        return {r["relname"] for r in rows}

    async def all_tables(db: "PostgresDatabase") -> set[str]:
        rows = await db.fetchall(
            "SELECT c.relname AS relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ? AND c.relkind = 'r'",
            (db.schema,),
        )
        return {r["relname"] for r in rows}

    async def scenario():
        db = await PostgresDatabase.open(PG_URL, schema=f"t_{uuid.uuid4().hex[:12]}")
        try:
            await db.init_schema()
            tables = await all_tables(db)
            assert {"model_state", "prediction_daily"} <= tables
            # every table has RLS enabled after the first init
            assert await tables_with_rls_off(db) == set()
            # a second init (every boot runs it) re-enables nothing: the DO block's loop body runs zero ALTERs, so it
            # takes no ACCESS EXCLUSIVE lock while a standby initialises the schema next to the active instance
            await db.init_schema()
            assert await tables_with_rls_off(db) == set()
            assert await all_tables(db) == tables
        finally:
            await db.drop_schema()
            await db.close()

    asyncio.run(scenario())
