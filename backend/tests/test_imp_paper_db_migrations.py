"""paper-db · versioned database migrations (improvement 11), SQLite and PostgreSQL.

schema.sql / schema_pg.sql are the frozen baseline (version 1); numbered migrations in app/migrations run in order at
start, recorded in schema_migrations. Fresh databases and upgrades of existing ones end up identical; an existing
database is marked as baseline without re-running its DDL (unless it lacks a baseline table, then it is healed); a
second start runs no DDL; a failing migration rolls everything back; instances starting together apply each migration
once. PostgreSQL tests need NEXUS_TEST_PG_URL.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
from pathlib import Path

import pytest

from app import migrations as mig
from app.db import SCHEMA_FILE, Database
from tests.conftest import PG_URL

BASELINE = SCHEMA_FILE.read_text(encoding="utf-8")
EXPECTED = ["0002_paper_orders", "0003_paper_fills_time"]


def _norm(sql: str | None) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


def _schema_rows(path: Path) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                           "ORDER BY type, name").fetchall()
    finally:
        con.close()
    return [(t, n, tb, _norm(sql)) for t, n, tb, sql in rows]


def _legacy_sqlite(path: Path, script: str = BASELINE) -> None:
    """A database created by NEXUS before versioning: the schema script run with executescript, some data."""
    con = sqlite3.connect(path)
    try:
        con.executescript(script)
        con.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
        con.execute("INSERT INTO settings(key, value, updated_at) VALUES ('risk.per_trade_pct', '2.0', 1)")
        con.execute("INSERT INTO paper_positions(account_id, symbol, side, qty, qty_open, entry_price, entry_time) "
                    "VALUES (1, 'BINANCE:BTCUSDT', 'long', 1, 1, 100, 1700000000)")
        con.commit()
    finally:
        con.close()


async def _open(path: Path) -> Database:
    return await Database.open(path)


# ----------------------------------------------------------------------------- the migration files


def test_migration_files_are_complete_and_numbered():
    found = mig.discover()
    assert [m.label for m in found] == EXPECTED and mig.latest_version(found) == 3
    for m in found:
        assert mig.split_sqlite(m.sqlite), m.label  # parses into statements
        assert m.postgres.strip(), m.label
    tables, indexes = mig.baseline_objects(BASELINE)
    assert {"meta", "settings", "paper_positions", "paper_fills", "prediction_daily", "backtests"} <= set(tables)
    assert {"idx_pos_status", "idx_pred_model", "idx_events_ts"} <= set(indexes) and "schema_migrations" not in tables
    pg_tables, pg_indexes = mig.baseline_objects(Path(mig.MIGRATIONS_DIR.parent / "schema_pg.sql").read_text("utf-8"))
    assert pg_tables == tables and pg_indexes == indexes  # the two baselines declare the same objects
    from app.db_pg import IDENTITY_TABLES, TABLE_PKS

    for m in found:  # every table a migration creates is known to the PostgreSQL translator (upserts, RETURNING id)
        for name in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", m.postgres):
            assert name in TABLE_PKS, (m.label, name)
    assert "paper_orders" in IDENTITY_TABLES and TABLE_PKS["schema_migrations"] == ("version",)


@pytest.mark.parametrize("files, needle", [
    ({"0002_a.sqlite.sql": "SELECT 1;"}, "no tiene el archivo .pg.sql"),
    ({"0002_a.sqlite.sql": "SELECT 1;", "0002_a.pg.sql": "SELECT 1;", "0004_b.sqlite.sql": "SELECT 1;",
      "0004_b.pg.sql": "SELECT 1;"}, "Falta la migración 0003"),
    ({"0002_a.sqlite.sql": "SELECT 1;", "0002_b.pg.sql": "SELECT 1;"}, "dos nombres"),
    ({"0001_x.sqlite.sql": "SELECT 1;", "0001_x.pg.sql": "SELECT 1;"}, "reservada para el esquema base"),
    ({"2_bad.sqlite.sql": "SELECT 1;"}, "Nombre de migración no válido"),
])
def test_broken_migration_sets_are_refused(tmp_path, files, needle):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    with pytest.raises(mig.MigrationError, match=needle):
        mig.discover(tmp_path)


def test_sqlite_splitter_handles_comments_strings_and_unfinished_statements():
    stmts = mig.split_sqlite("-- a; comment\nCREATE TABLE t (a TEXT DEFAULT 'x;y'); -- tail\n\nINSERT INTO t VALUES (';');\n")
    assert stmts == ["-- a; comment\nCREATE TABLE t (a TEXT DEFAULT 'x;y'); -- tail", "INSERT INTO t VALUES (';');"]
    with pytest.raises(mig.MigrationError, match="incompleta"):
        mig.split_sqlite("CREATE TABLE t (a INTEGER)")
    assert mig.split_sqlite("-- only a comment\n") == []


# ----------------------------------------------------------------------------- SQLite


def test_fresh_sqlite_database_gets_baseline_and_every_migration(tmp_path):
    async def scenario():
        db = await _open(tmp_path / "fresh.db")
        try:
            info = await db.init_schema()
            assert info == {"version": 3, "latest": 3, "baseline": "created", "applied": EXPECTED, "unknown": []}
            rows = await db.fetchall("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
            assert [(r["version"], r["name"]) for r in rows] == [(1, "baseline"), (2, "paper_orders"),
                                                                  (3, "paper_fills_time")]
            assert all(r["applied_at"] > 1_600_000_000_000 for r in rows)  # unix ms
            assert await db.fetchval("SELECT value FROM meta WHERE key='schema_version'") == "3"
            assert "paper_orders" in await db.table_counts() and db.schema_info == info
        finally:
            await db.close()

    asyncio.run(scenario())


def test_existing_database_is_marked_as_baseline_without_rerunning_its_ddl(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    _legacy_sqlite(path)
    ran: list[str] = []
    original = mig._sqlite_run

    async def recording(conn, script):
        ran.append(script)
        await original(conn, script)

    monkeypatch.setattr(mig, "_sqlite_run", recording)

    async def scenario():
        db = await _open(path)
        try:
            info = await db.init_schema()
            assert info["baseline"] == "marked" and info["applied"] == EXPECTED and info["version"] == 3
            assert BASELINE not in ran  # the baseline DDL did not run again
            assert len(ran) == 3  # schema_migrations itself + the two migrations
            assert await db.fetchval("SELECT value FROM settings WHERE key='risk.per_trade_pct'") == "2.0"
            assert await db.fetchval("SELECT COUNT(*) FROM paper_positions") == 1  # data kept
            ran.clear()
            again = await db.init_schema()  # every later start: nothing pending, no DDL at all
            assert again["baseline"] == "present" and again["applied"] == [] and ran == []
        finally:
            await db.close()

    asyncio.run(scenario())
    fresh = tmp_path / "fresh.db"

    async def make_fresh():
        db = await _open(fresh)
        await db.init_schema()
        await db.close()

    asyncio.run(make_fresh())
    assert _schema_rows(path) == _schema_rows(fresh)  # upgraded == fresh, object by object


def test_an_older_database_missing_a_baseline_table_is_healed(tmp_path):
    start = BASELINE.index("CREATE TABLE IF NOT EXISTS prediction_daily")
    end = BASELINE.index(");", start) + 2
    older = BASELINE[:start] + BASELINE[end:]
    older = older.replace("CREATE INDEX IF NOT EXISTS idx_pred_model ON predictions (model_key, resolved, target_at);", "")
    path = tmp_path / "older.db"
    _legacy_sqlite(path, older)

    async def scenario():
        db = await _open(path)
        try:
            info = await db.init_schema()
            assert info["baseline"] == "created" and info["applied"] == EXPECTED
            names = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
            assert {"prediction_daily", "idx_pred_model", "paper_orders"} <= names
            assert await db.fetchval("SELECT COUNT(*) FROM paper_positions") == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_a_failing_migration_rolls_everything_back(tmp_path):
    good = mig.Migration(2, "good", "CREATE TABLE t_good (a INTEGER);", "SELECT 1;")
    bad = mig.Migration(3, "broken", "CREATE TABLE t_bad (a INTEGER);\nCREATE TABLE t_bad (a INTEGER);", "SELECT 1;")

    async def scenario():
        db = await _open(tmp_path / "r.db")
        try:
            with pytest.raises(mig.MigrationError, match="0003_broken"):
                await db.init_schema([good, bad])
            names = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
            assert names == set()  # baseline, schema_migrations, t_good and the half of 0003: all rolled back
            info = await db.init_schema()  # the real set still applies afterwards
            assert info["applied"] == EXPECTED
        finally:
            await db.close()

    asyncio.run(scenario())


def test_versions_from_a_newer_nexus_are_reported_not_undone(tmp_path):
    async def scenario():
        db = await _open(tmp_path / "n.db")
        try:
            await db.init_schema()
            await db.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (99, 'future', 1)")
            info = await db.init_schema()
            assert info["unknown"] == [99] and info["version"] == 99 and info["applied"] == []
            assert await db.fetchval("SELECT COUNT(*) FROM schema_migrations") == 4
        finally:
            await db.close()

    asyncio.run(scenario())


def test_two_processes_starting_on_one_file_apply_each_migration_once(tmp_path):
    async def scenario():
        a, b = await _open(tmp_path / "shared.db"), await _open(tmp_path / "shared.db")
        try:
            infos = await asyncio.gather(a.init_schema(), b.init_schema())
            assert sorted(len(i["applied"]) for i in infos) == [0, 2]
            rows = await a.fetchall("SELECT version, COUNT(*) AS n FROM schema_migrations GROUP BY version")
            assert {r["version"]: r["n"] for r in rows} == {1: 1, 2: 1, 3: 1}
        finally:
            await a.close()
            await b.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- PostgreSQL

pg = pytest.mark.skipif(not PG_URL, reason="set NEXUS_TEST_PG_URL to run PostgreSQL integration tests")


def _schema() -> str:
    return f"t_{uuid.uuid4().hex[:12]}"


async def _pg_objects(db) -> set[tuple[str, str]]:
    rows = await db.fetchall(
        "SELECT c.relname AS name, CAST(c.relkind AS TEXT) AS kind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ? AND c.relkind IN ('r', 'i')",
        (db.schema,),
    )
    return {(r["name"], r["kind"]) for r in rows}


async def _legacy_pg(schema: str, drop: tuple[str, ...] = ()) -> None:
    """A schema created by NEXUS before versioning: the old schema_pg.sql (baseline + RLS block), some data."""
    import asyncpg

    from app.db_pg import SCHEMA_FILE as PG_SCHEMA

    script = PG_SCHEMA.read_text(encoding="utf-8").replace("{schema}", schema) + "\n" + mig.pg_rls_sql(schema)
    conn = await asyncpg.connect(PG_URL)
    try:
        await conn.execute(script)
        for name in drop:
            await conn.execute(f'DROP TABLE "{schema}"."{name}"')
        await conn.execute(f'INSERT INTO "{schema}".paper_positions(account_id, symbol, side, qty, qty_open, entry_price, '
                           "entry_time) VALUES (1, 'BINANCE:BTCUSDT', 'long', 1, 1, 100, 1700000000)")
    finally:
        await conn.close()


@pg
def test_pg_fresh_upgrade_and_idempotent_starts():
    from app.db_pg import PostgresDatabase

    async def scenario():
        fresh = await PostgresDatabase.open(PG_URL, schema=_schema())
        old_schema = _schema()
        await _legacy_pg(old_schema)
        old = await PostgresDatabase.open(PG_URL, schema=old_schema)
        try:
            info = await fresh.init_schema()
            assert info["baseline"] == "created" and info["applied"] == EXPECTED and info["version"] == 3
            up = await old.init_schema()
            assert up["baseline"] == "marked" and up["applied"] == EXPECTED  # baseline DDL not re-run
            assert await old.fetchval("SELECT COUNT(*) FROM paper_positions") == 1
            assert await _pg_objects(fresh) == await _pg_objects(old)
            for db in (fresh, old):
                assert await db.fetchval("SELECT value FROM meta WHERE key='schema_version'") == "3"
                no_rls = await db.fetchall(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = ? AND c.relkind = 'r' AND NOT c.relrowsecurity", (db.schema,))
                assert no_rls == []  # including paper_orders and schema_migrations
                again = await db.init_schema()
                assert again["baseline"] == "present" and again["applied"] == []
            oid = await old.execute("INSERT INTO paper_orders(account_id, symbol, side, kind, price, created_at, updated_at) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (1, "BINANCE:BTCUSDT", "long", "limit", 95.0, 1, 1))
            assert oid >= 1  # identity table: execute() returns the id like SQLite's lastrowid
        finally:
            for db in (fresh, old):
                await db.drop_schema()
                await db.close()

    asyncio.run(scenario())


@pg
def test_pg_older_schema_is_healed_and_a_failing_migration_rolls_back():
    from app.db_pg import PostgresDatabase

    async def scenario():
        schema = _schema()
        await _legacy_pg(schema, drop=("prediction_daily",))
        db = await PostgresDatabase.open(PG_URL, schema=schema)
        broken_schema = _schema()
        broken = await PostgresDatabase.open(PG_URL, schema=broken_schema)
        try:
            info = await db.init_schema()
            assert info["baseline"] == "created" and ("prediction_daily", "r") in await _pg_objects(db)
            bad = mig.Migration(2, "broken", "SELECT 1;", "CREATE TABLE half (a BIGINT); SELECT * FROM missing_table;")
            with pytest.raises(mig.MigrationError, match="0002_broken"):
                await broken.init_schema([bad])
            assert await _pg_objects(broken) == set()  # baseline and the half-applied migration rolled back
        finally:
            for d in (db, broken):
                await d.drop_schema()
                await d.close()

    asyncio.run(scenario())


@pg
def test_pg_instances_starting_together_apply_each_migration_once():
    from app.db_pg import PostgresDatabase

    async def scenario():
        schema = _schema()
        dbs = [await PostgresDatabase.open(PG_URL, schema=schema) for _ in range(3)]
        try:
            infos = await asyncio.gather(*(db.init_schema() for db in dbs))
            assert sorted(i["baseline"] for i in infos) == ["created", "present", "present"]
            assert sorted(len(i["applied"]) for i in infos) == [0, 0, 2]
            rows = await dbs[0].fetchall("SELECT version, COUNT(*) AS n FROM schema_migrations GROUP BY version")
            assert {int(r["version"]): r["n"] for r in rows} == {1: 1, 2: 1, 3: 1}
        finally:
            await dbs[0].drop_schema()
            for db in dbs:
                await db.close()

    asyncio.run(scenario())
