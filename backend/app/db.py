"""SQLite access (aiosqlite, WAL). One shared connection guarded by an asyncio.Lock.

`init_schema` runs the versioned migrations (app.migrations): baseline schema.sql + numbered migrations, recorded in
schema_migrations; the result is kept in `Database.schema_info`."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

from app import migrations

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class Transaction:
    """Statements inside `async with db.transaction() as tx:` run atomically (commit on exit)."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = await self._conn.execute(sql, params)
        rowid = cur.lastrowid or 0
        await cur.close()
        return rowid

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        await self._conn.executemany(sql, list(seq))

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row is not None else None


class Database:
    dialect = "sqlite"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        # Result of the last init_schema: {version, latest, baseline, applied, unknown} (see app.migrations)
        self.schema_info: dict[str, Any] = {}

    @classmethod
    async def open(cls, path: Path | str) -> "Database":
        db = cls(Path(path))
        try:
            db.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:  # e.g. WinError 206 on long paths when the folder already exists
            if not db.path.parent.is_dir():
                raise
        db.conn = await aiosqlite.connect(str(db.path))
        db.conn.row_factory = aiosqlite.Row
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=5000",
            "PRAGMA temp_store=MEMORY",
            "PRAGMA foreign_keys=ON",
        ):
            await db.conn.execute(pragma)
        await db.conn.commit()
        return db

    async def init_schema(self, migration_set: list[migrations.Migration] | None = None) -> dict[str, Any]:
        """Baseline + pending migrations in one BEGIN IMMEDIATE transaction (`migration_set`: tests only)."""
        baseline = SCHEMA_FILE.read_text(encoding="utf-8")
        async with self._lock:
            self.schema_info = await migrations.apply_sqlite(self.conn, baseline, migration_set)
        return self.schema_info

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            rowid = cur.lastrowid or 0
            await cur.close()
            return rowid

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        rows = list(seq)
        if not rows:
            return
        async with self._lock:
            await self.conn.executemany(sql, rows)
            await self.conn.commit()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        return [dict(r) for r in rows]

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            row = await cur.fetchone()
            await cur.close()
        return dict(row) if row is not None else None

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        async with self._lock:
            tx = Transaction(self.conn)
            try:
                yield tx
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def table_counts(self) -> dict[str, int]:
        names = await self.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        out: dict[str, int] = {}
        for row in names:
            out[row["name"]] = int(await self.fetchval(f'SELECT COUNT(*) FROM "{row["name"]}"', default=0))
        return out

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    async def vacuum(self) -> None:
        async with self._lock:
            await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self.conn.execute("VACUUM")

    async def close(self) -> None:
        if self.conn is not None:
            async with self._lock:
                await self.conn.close()
            self.conn = None


async def open_database():
    """Local SQLite file (default) or PostgreSQL when NEXUS_DATABASE_URL is set (cloud / Supabase).
    Both expose the same API; module SQL is written for SQLite and translated by app.db_pg."""
    from app import config

    if config.DATABASE_URL:
        from app.db_pg import PostgresDatabase

        return await PostgresDatabase.open(config.DATABASE_URL, schema=config.DB_SCHEMA)
    return await Database.open(config.DB_PATH)
