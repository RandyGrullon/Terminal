"""Versioned database migrations for SQLite and PostgreSQL, applied by `init_schema` at every start.

Version 1 is the **baseline**: the schema files `app/schema.sql` (SQLite) and `app/schema_pg.sql` (PostgreSQL) as
they were when versioning started. They are frozen: every later change is a numbered migration in this folder, one
file per dialect with the same number and name::

    0002_paper_orders.sqlite.sql        0002_paper_orders.pg.sql

The runner keeps the table ``schema_migrations(version, name, applied_at)`` (applied_at = unix ms) and, on start:

- **Baseline**: a database without a baseline row gets one. One that already has every baseline table and index
  (created by an earlier NEXUS) is only *marked* as baseline, without re-running its DDL; a fresh database, or an old
  one missing some baseline table or index (the schema files only use ``IF NOT EXISTS``), runs the baseline script.
- **Pending migrations** run in version order, each followed by its schema_migrations row, all in ONE transaction
  (DDL is transactional in both engines): a failing migration rolls everything back and the start fails with the
  migration's name. The transaction is serialized across processes by ``BEGIN IMMEDIATE`` (SQLite) or by the
  ``pg_advisory_xact_lock`` that ``PostgresDatabase.init_schema`` takes first, so instances starting together never
  apply a migration twice.
- With nothing pending no DDL runs at all (no table locks next to a live instance during a deploy overlap). On
  PostgreSQL the row-level-security pass (``pg_rls.sql``) runs every time: it only alters tables whose RLS is still
  off, so it is lock-free once every table has it, and it covers the tables created by migrations.
- A database migrated by a newer NEXUS (versions this code does not know) keeps working: they are reported in
  ``schema_info["unknown"]`` and nothing is undone. So migrations must stay backward compatible (new tables, nullable
  columns, indexes): during a deploy the previous version may still be running against the migrated database.

Module SQL elsewhere stays in the SQLite dialect (``app.db_pg`` translates it); migration files are written natively
for each engine. PostgreSQL files may use ``{schema}`` (replaced by the NEXUS schema name) and run with the
search_path set to it. SQLite files are split into statements with ``sqlite3.complete_statement``: one statement per
line group, ``--`` comments allowed.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS_DIR = Path(__file__).resolve().parent
PG_RLS_FILE = MIGRATIONS_DIR / "pg_rls.sql"
BASELINE_VERSION = 1
BASELINE_NAME = "baseline"
TABLE = "schema_migrations"

_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9][a-z0-9_]*)\.(sqlite|pg)\.sql$")
_OBJECT_RE = re.compile(r"\bCREATE\s+(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_]\w*)", re.I)
_COMMENT_RE = re.compile(r"--[^\n]*")

SQLITE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations (\n"
    "  version    INTEGER PRIMARY KEY,\n"
    "  name       TEXT NOT NULL,\n"
    "  applied_at INTEGER NOT NULL\n"
    ")"
)
PG_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (\n"
    "  version    BIGINT PRIMARY KEY,\n"
    "  name       TEXT NOT NULL,\n"
    "  applied_at BIGINT NOT NULL\n"
    ")"
)


class MigrationError(RuntimeError):
    """A migration file set is inconsistent or a migration failed (Spanish message, the start is aborted)."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite: str  # SQLite script
    postgres: str  # PostgreSQL script ({schema} placeholder allowed)

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


# ----------------------------------------------------------------------------- discovery & parsing


def discover(directory: Path | str | None = None) -> list[Migration]:
    """Numbered migrations of `directory` (default: this package), in version order.

    Rules (a broken set raises MigrationError, so a bad file never half-applies): versions start at 2 (1 is the
    baseline) and have no gaps; each version has exactly one SQLite and one PostgreSQL file with the same name."""
    folder = Path(directory) if directory is not None else MIGRATIONS_DIR
    found: dict[int, dict[str, Any]] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file() or not path.name.endswith(".sql") or path.name == PG_RLS_FILE.name:
            continue
        m = _FILE_RE.match(path.name)
        if not m:
            raise MigrationError(f"Nombre de migración no válido: {path.name} (usa NNNN_nombre.sqlite.sql / .pg.sql)")
        version, name, dialect = int(m.group(1)), m.group(2), m.group(3)
        if version <= BASELINE_VERSION:
            raise MigrationError(f"La versión {version} está reservada para el esquema base: {path.name}")
        entry = found.setdefault(version, {"name": name})
        if entry["name"] != name:
            raise MigrationError(f"La migración {version} tiene dos nombres: {entry['name']} y {name}")
        if dialect in entry:
            raise MigrationError(f"La migración {version} está duplicada para {dialect}")
        entry[dialect] = path.read_text(encoding="utf-8")
    out: list[Migration] = []
    for expected, version in enumerate(sorted(found), start=BASELINE_VERSION + 1):
        if version != expected:
            raise MigrationError(f"Falta la migración {expected:04d} (hay un hueco antes de la {version:04d})")
        entry = found[version]
        for dialect in ("sqlite", "pg"):
            if dialect not in entry:
                raise MigrationError(f"La migración {version:04d}_{entry['name']} no tiene el archivo .{dialect}.sql")
        out.append(Migration(version, entry["name"], entry["sqlite"], entry["pg"]))
    return out


def latest_version(migrations: Iterable[Migration] | None = None) -> int:
    items = list(discover() if migrations is None else migrations)
    return max([BASELINE_VERSION] + [m.version for m in items])


def baseline_objects(script: str) -> tuple[list[str], list[str]]:
    """(tables, indexes) created by a schema script (`CREATE TABLE|INDEX IF NOT EXISTS name`)."""
    tables: list[str] = []
    indexes: list[str] = []
    for kind, name in _OBJECT_RE.findall(_COMMENT_RE.sub("", script)):
        (tables if kind.upper() == "TABLE" else indexes).append(name.lower())
    return tables, indexes


def _has_sql(text: str) -> bool:
    return bool(_COMMENT_RE.sub("", text).strip().strip(";").strip())


def split_sqlite(script: str) -> list[str]:
    """SQLite script -> statements (sqlite3.complete_statement understands strings, comments and triggers)."""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if _has_sql(buf):
                out.append(buf.strip())
            buf = ""
    if _has_sql(buf):
        raise MigrationError("Sentencia SQL incompleta al final del script (falta ';')")
    return out


def _now_ms() -> int:
    return int(time.time() * 1000)


def _info(applied: dict[int, str], ran: list[Migration], baseline: str, migrations: list[Migration]) -> dict:
    known = {BASELINE_VERSION} | {m.version for m in migrations}
    return {
        "version": max(applied) if applied else BASELINE_VERSION,
        "latest": latest_version(migrations),
        "baseline": baseline,  # created | marked | present
        "applied": [m.label for m in ran],
        "unknown": sorted(v for v in applied if v not in known),
    }


# ----------------------------------------------------------------------------- SQLite


async def _sqlite_rows(conn: Any, sql: str, params: tuple = ()) -> list[tuple]:
    cur = await conn.execute(sql, params)
    try:
        return [tuple(r) for r in await cur.fetchall()]
    finally:
        await cur.close()


async def _sqlite_exec(conn: Any, sql: str, params: tuple = ()) -> None:
    cur = await conn.execute(sql, params)
    await cur.close()


async def _sqlite_run(conn: Any, script: str) -> None:
    for stmt in split_sqlite(script):
        await _sqlite_exec(conn, stmt)


async def apply_sqlite(conn: Any, baseline_sql: str, migrations: list[Migration] | None = None) -> dict:
    """Bring a SQLite database (aiosqlite connection, caller holds the Database lock) to the latest version.

    Everything happens in one ``BEGIN IMMEDIATE`` transaction: the write lock is taken before reading the applied
    versions, so two processes opening the same file cannot both apply a migration; any error rolls back."""
    migrations = discover() if migrations is None else list(migrations)
    if conn.in_transaction:
        await conn.commit()
    await _sqlite_exec(conn, "BEGIN IMMEDIATE")
    try:
        info = await _sqlite_apply(conn, baseline_sql, migrations)
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    return info


async def _sqlite_apply(conn: Any, baseline_sql: str, migrations: list[Migration]) -> dict:
    names = {str(n).lower() for (n,) in await _sqlite_rows(
        conn, "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    applied: dict[int, str] = {}
    if TABLE in names:
        applied = {int(v): str(n) for v, n in await _sqlite_rows(conn, "SELECT version, name FROM schema_migrations")}
    baseline = "present"
    now = _now_ms()
    if BASELINE_VERSION not in applied:
        tables, indexes = baseline_objects(baseline_sql)
        missing = [o for o in tables + indexes if o not in names]
        baseline = "marked"
        if missing:
            baseline = "created"
            await _sqlite_run(conn, baseline_sql)
        if TABLE not in names:
            await _sqlite_run(conn, SQLITE_TABLE_SQL + ";")
        await _sqlite_exec(conn, "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                           (BASELINE_VERSION, BASELINE_NAME, now))
        applied[BASELINE_VERSION] = BASELINE_NAME
    ran: list[Migration] = []
    for m in migrations:
        if m.version in applied:
            continue
        try:
            await _sqlite_run(conn, m.sqlite)
        except sqlite3.Error as exc:
            raise MigrationError(f"La migración {m.label} falló en SQLite: {exc}") from exc
        await _sqlite_exec(conn, "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                           (m.version, m.name, now))
        applied[m.version] = m.name
        ran.append(m)
    info = _info(applied, ran, baseline, migrations)
    await _sqlite_exec(
        conn,
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(info["version"]),),
    )
    return info


# ----------------------------------------------------------------------------- PostgreSQL


def pg_rls_sql(schema: str) -> str:
    return PG_RLS_FILE.read_text(encoding="utf-8").replace("{schema}", schema)


async def apply_postgres(conn: Any, schema: str, baseline_sql: str, migrations: list[Migration] | None = None) -> dict:
    """Bring a PostgreSQL schema to the latest version. The caller runs this inside a transaction that already holds
    ``pg_advisory_xact_lock`` for the schema (``PostgresDatabase.init_schema``); errors abort that transaction."""
    import asyncpg  # cloud mode only

    migrations = discover() if migrations is None else list(migrations)
    schema = schema.lower()  # the schema files create it unquoted, so PostgreSQL folds the name to lower case
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relkind IN ('r', 'p', 'i')",
        schema,
    )
    names = {str(r["relname"]).lower() for r in rows}
    applied: dict[int, str] = {}
    if TABLE in names:
        applied = {int(r["version"]): str(r["name"])
                   for r in await conn.fetch(f'SELECT version, name FROM "{schema}".schema_migrations')}
    baseline = "present"
    now = _now_ms()
    if BASELINE_VERSION not in applied:
        tables, indexes = baseline_objects(baseline_sql)
        missing = [o for o in tables + indexes if o not in names]
        baseline = "marked"
        if missing:
            baseline = "created"
            await conn.execute(baseline_sql.replace("{schema}", schema))
        if TABLE not in names:
            await conn.execute(PG_TABLE_SQL.replace("{schema}", schema))
        await conn.execute(f'INSERT INTO "{schema}".schema_migrations(version, name, applied_at) VALUES ($1, $2, $3)',
                           BASELINE_VERSION, BASELINE_NAME, now)
        applied[BASELINE_VERSION] = BASELINE_NAME
    ran: list[Migration] = []
    for m in migrations:
        if m.version in applied:
            continue
        await conn.execute(f'SET LOCAL search_path TO "{schema}"')
        try:
            await conn.execute(m.postgres.replace("{schema}", schema))
        except asyncpg.PostgresError as exc:
            raise MigrationError(f"La migración {m.label} falló en PostgreSQL: {exc}") from exc
        await conn.execute(f'INSERT INTO "{schema}".schema_migrations(version, name, applied_at) VALUES ($1, $2, $3)',
                           m.version, m.name, now)
        applied[m.version] = m.name
        ran.append(m)
    await conn.execute(pg_rls_sql(schema))
    info = _info(applied, ran, baseline, migrations)
    await conn.execute(
        f'INSERT INTO "{schema}".meta(key, value) VALUES (\'schema_version\', $1) '
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        str(info["version"]),
    )
    return info
