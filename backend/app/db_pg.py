"""PostgreSQL / Supabase backend with the same interface as app.db.Database (cloud mode).

The application SQL is written in the SQLite dialect; `translate()` rewrites it for PostgreSQL:

    ?                                   -> $1, $2, ...
    INSERT OR IGNORE INTO ...           -> INSERT INTO ... ON CONFLICT DO NOTHING
    INSERT OR REPLACE / REPLACE INTO    -> INSERT INTO ... ON CONFLICT (<key>) DO UPDATE SET ...
    IFNULL / INSTR / GROUP_CONCAT       -> COALESCE / STRPOS / STRING_AGG
    ROUND(x, n)                         -> ROUND(CAST(x AS NUMERIC), n)
    MAX(a, b) / MIN(a, b) (scalar)      -> GREATEST / LEAST
    strftime(fmt, x, 'unixepoch')       -> to_char(to_timestamp(x) AT TIME ZONE 'UTC', pg_fmt)
    json_extract(col, '$.a.b')          -> (CAST(col AS JSON) #>> '{a,b}')
    LIKE -> ILIKE (SQLite LIKE is case-insensitive) · == -> = · LIMIT -1 -> LIMIT ALL

INSERTs into identity tables get `RETURNING id` so execute() returns the new id like sqlite's lastrowid.
Parameters are coerced to the types the server reports (SQLite is lenient, PostgreSQL is not), NaN is stored as
NULL (like sqlite3) and NUMERIC results come back as float.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import numpy as np

SCHEMA_VERSION = "1"
SCHEMA_FILE = Path(__file__).with_name("schema_pg.sql")

# Keep in sync with schema_pg.sql
TABLE_PKS: dict[str, tuple[str, ...]] = {
    "meta": ("key",),
    "settings": ("key",),
    "symbols": ("symbol",),
    "candles": ("symbol", "tf", "time"),
    "watchlist": ("symbol",),
    "model_state": ("kind", "key"),
    "signals": ("id",),
    "predictions": ("id",),
    "gate_log": ("id",),
    "scan_runs": ("id",),
    "scan_results": ("run_id", "symbol"),
    "paper_accounts": ("id",),
    "paper_positions": ("id",),
    "paper_fills": ("id",),
    "events": ("id",),
    "scripts": ("id",),
    "backtests": ("id",),
}
IDENTITY_TABLES = {"signals", "predictions", "gate_log", "scan_runs", "paper_positions", "paper_fills", "scripts", "backtests"}
UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "signals": ("symbol", "tf", "time", "action"),
    "gate_log": ("symbol", "tf", "time", "direction", "source"),
    "scripts": ("name",),
}

_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_TOKEN = re.compile(r"\x00(\d+)\x00")
_FUNC = re.compile(r"(ROUND|MAX|MIN|GROUP_CONCAT|STRFTIME|IFNULL|INSTR|JSON_EXTRACT|DATETIME|DATE)\s*\(", re.I)
_INSERT_OR = re.compile(
    r"^\s*(?:INSERT\s+OR\s+(IGNORE|REPLACE)|(REPLACE))\s+INTO\s+([A-Za-z_]\w*)(?:\s*\(([^)]*)\))?", re.I | re.S
)
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_]\w*)", re.I)
_STRFTIME_MAP = [("%Y", "YYYY"), ("%m", "MM"), ("%d", "DD"), ("%H", "HH24"), ("%M", "MI"), ("%S", "SS"), ("%j", "DDD"), ("%W", "IW")]


# ============================================================================= SQL translation


def _match_paren(s: str, open_idx: int) -> int | None:
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _split_args(inner: str) -> list[str]:
    args, depth, cur = [], 0, []
    for ch in inner:
        if ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        cur.append(ch)
    args.append("".join(cur).strip())
    return args


def _transform_calls(sql: str, saved: list[str]) -> str:
    """Rewrite SQLite-only function calls (recursively, inner calls first). `sql` has its strings masked."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        m = _FUNC.match(sql, i)
        if m and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_.$")):
            open_idx = m.end() - 1
            close_idx = _match_paren(sql, open_idx)
            if close_idx is None:
                out.append(sql[i:])
                break
            inner = _transform_calls(sql[open_idx + 1 : close_idx], saved)
            out.append(_rewrite(m.group(1).upper(), m.group(1), inner, saved))
            i = close_idx + 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _literal(arg: str, saved: list[str]) -> str | None:
    """Return the python value of a masked string literal argument, else None."""
    m = _TOKEN.fullmatch(arg.strip())
    if not m:
        return None
    raw = saved[int(m.group(1))]
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    return None


def _rewrite(name: str, original: str, inner: str, saved: list[str]) -> str:
    args = _split_args(inner)
    if name == "ROUND" and len(args) == 2:
        if re.fullmatch(r"CAST\(.*\s+AS\s+NUMERIC\)", args[0], re.I | re.S):
            return f"ROUND({inner})"
        return f"ROUND(CAST({args[0]} AS NUMERIC), {args[1]})"
    if name in ("MAX", "MIN") and len(args) >= 2:
        return f"{'GREATEST' if name == 'MAX' else 'LEAST'}({inner})"
    if name == "IFNULL":
        return f"COALESCE({inner})"
    if name == "INSTR":
        return f"STRPOS({inner})"
    if name == "GROUP_CONCAT":
        sep = args[1] if len(args) > 1 else "','"
        return f"STRING_AGG(CAST({args[0]} AS TEXT), {sep})"
    if name == "JSON_EXTRACT" and len(args) == 2:
        path = _literal(args[1], saved)
        if path and path.startswith("$."):
            keys = ",".join(path[2:].split("."))
            return f"(CAST({args[0]} AS JSON) #>> '{{{keys}}}')"
    if name in ("STRFTIME", "DATETIME", "DATE"):
        if name == "STRFTIME":
            fmt = _literal(args[0], saved) if args else None
            value, mods = (args[1] if len(args) > 1 else None), args[2:]
        else:
            fmt = "%Y-%m-%d %H:%M:%S" if name == "DATETIME" else "%Y-%m-%d"
            value, mods = (args[0] if args else None), args[1:]
        if fmt is not None and value is not None:
            mod_values = [(_literal(a, saved) or "").lower() for a in mods]
            if (_literal(value, saved) or "").lower() == "now":
                ts = "now()"
            elif "unixepoch" in mod_values:
                ts = f"to_timestamp({value})"
            else:
                ts = f"CAST({value} AS TIMESTAMPTZ)"
            if fmt == "%s":
                return f"CAST(EXTRACT(EPOCH FROM {ts}) AS BIGINT)"
            pg_fmt = fmt
            for a, b in _STRFTIME_MAP:
                pg_fmt = pg_fmt.replace(a, b)
            return f"to_char({ts} AT TIME ZONE 'UTC', '{pg_fmt}')"
    return f"{original}({inner})"


def _conflict_clause(table: str, cols: list[str] | None, mode: str) -> str:
    if mode == "IGNORE":
        return " ON CONFLICT DO NOTHING"
    pk = TABLE_PKS.get(table.lower(), ())
    target: tuple[str, ...] = ()
    if cols is not None:
        lowered = [c.lower() for c in cols]
        if pk and all(k in lowered for k in pk):
            target = pk
        elif table.lower() in UNIQUE_KEYS and all(k in lowered for k in UNIQUE_KEYS[table.lower()]):
            target = UNIQUE_KEYS[table.lower()]
    else:
        target = pk
    if not target:
        return " ON CONFLICT DO NOTHING"
    updates = [c for c in (cols or []) if c.lower() not in target]
    if not updates:
        return f" ON CONFLICT ({', '.join(target)}) DO NOTHING"
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
    return f" ON CONFLICT ({', '.join(target)}) DO UPDATE SET {sets}"


def translate(sql: str, *, returning: bool = True) -> tuple[str, bool]:
    """SQLite-dialect SQL -> (PostgreSQL SQL, returns_id)."""
    s = sql.strip().rstrip(";").strip()
    saved: list[str] = []

    def mask(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"

    s = _QUOTED.sub(mask, s)

    m = _INSERT_OR.match(s)
    if m:
        mode = (m.group(1) or m.group(2) or "").upper()
        table = m.group(3)
        cols = [c.strip() for c in m.group(4).split(",")] if m.group(4) is not None else None
        s = re.sub(r"^\s*(?:INSERT\s+OR\s+(?:IGNORE|REPLACE)|REPLACE)\s+INTO", "INSERT INTO", s, count=1, flags=re.I)
        clause = _conflict_clause(table, cols, "IGNORE" if mode == "IGNORE" else "REPLACE")
        ret = re.search(r"\bRETURNING\b", s, re.I)
        s = s[: ret.start()].rstrip() + clause + " " + s[ret.start() :] if ret else s + clause

    s = _transform_calls(s, saved)
    s = s.replace("==", "=")
    s = re.sub(r"\bLIKE\b", "ILIKE", s, flags=re.I)
    s = re.sub(r"\bLIMIT\s+-1\b", "LIMIT ALL", s, flags=re.I)

    counter = 0

    def placeholder(_: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    s = re.sub(r"\?", placeholder, s)

    returns_id = False
    im = _INSERT.match(s)
    if returning and im and im.group(1).lower() in IDENTITY_TABLES and not re.search(r"\bRETURNING\b", s, re.I):
        s += " RETURNING id"
        returns_id = True

    s = _TOKEN.sub(lambda t: saved[int(t.group(1))], s)
    return s, returns_id


# ============================================================================= parameter coercion

_INT_TYPES = {"int2", "int4", "int8", "oid"}
_FLOAT_TYPES = {"float4", "float8", "numeric"}
_TEXT_TYPES = {"text", "varchar", "bpchar", "name", "json", "jsonb"}


def coerce(value: Any, typ: str | None) -> Any:
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None  # sqlite3 stores NaN as NULL
    if typ in _INT_TYPES:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(round(value))
        if isinstance(value, (str, Decimal)):
            try:
                return int(float(value))
            except ValueError:
                return value
        return value
    if typ in _FLOAT_TYPES:
        if isinstance(value, (bool, int, float, Decimal)):
            return float(value) if typ != "numeric" else Decimal(str(value))
        if isinstance(value, str):
            try:
                return float(value) if typ != "numeric" else Decimal(value)
            except ValueError:
                return value
        return value
    if typ in _TEXT_TYPES:
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", "replace")
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)
    if typ == "bool":
        return bool(value)
    return value


def _row(rec: asyncpg.Record) -> dict:
    d = dict(rec)
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = float(v)
    return d


def redact_dsn(dsn: str) -> str:
    try:
        parts = urlsplit(dsn)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        user = parts.username or ""
        return urlunsplit((parts.scheme, f"{user}:•••@{host}{port}", parts.path, "", ""))
    except ValueError:
        return "postgresql://•••"


def _ssl_option(dsn: str) -> Any:
    if "sslmode=" in dsn:
        return None  # asyncpg reads it from the DSN
    host = (urlsplit(dsn).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return False
    return "require"


# ============================================================================= database


class _Executor:
    """Shared translate + coerce + execute logic (used by the pool and by transactions)."""

    def __init__(self, owner: "PostgresDatabase"):
        self._owner = owner

    async def _prepare(self, conn: asyncpg.Connection, sql: str, returning: bool = True) -> tuple[str, bool, list[str]]:
        pg_sql, returns_id = translate(sql, returning=returning)
        types = self._owner._types.get(pg_sql)
        if types is None:
            try:
                stmt = await conn.prepare(pg_sql)
            except asyncpg.PostgresError as exc:
                exc.add_note(f"SQL (PostgreSQL): {pg_sql}")
                raise
            types = [t.name for t in stmt.get_parameters()]
            if len(self._owner._types) > 4000:
                self._owner._types.clear()
            self._owner._types[pg_sql] = types
        return pg_sql, returns_id, types

    @staticmethod
    def _args(params: Sequence[Any], types: list[str]) -> list[Any]:
        params = list(params or ())
        return [coerce(v, types[i] if i < len(types) else None) for i, v in enumerate(params)]

    async def _execute(self, conn: asyncpg.Connection, sql: str, params: Sequence[Any]) -> int:
        pg_sql, returns_id, types = await self._prepare(conn, sql)
        args = self._args(params, types)
        if returns_id:
            value = await conn.fetchval(pg_sql, *args)
            return int(value) if value is not None else 0
        await conn.execute(pg_sql, *args)
        return 0

    async def _executemany(self, conn: asyncpg.Connection, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        rows = list(seq)
        if not rows:
            return
        pg_sql, _, types = await self._prepare(conn, sql, returning=False)
        await conn.executemany(pg_sql, [self._args(r, types) for r in rows])

    async def _fetchall(self, conn: asyncpg.Connection, sql: str, params: Sequence[Any]) -> list[dict]:
        pg_sql, _, types = await self._prepare(conn, sql, returning=False)
        return [_row(r) for r in await conn.fetch(pg_sql, *self._args(params, types))]

    async def _fetchone(self, conn: asyncpg.Connection, sql: str, params: Sequence[Any]) -> dict | None:
        pg_sql, _, types = await self._prepare(conn, sql, returning=False)
        rec = await conn.fetchrow(pg_sql, *self._args(params, types))
        return _row(rec) if rec is not None else None


class PgTransaction(_Executor):
    def __init__(self, owner: "PostgresDatabase", conn: asyncpg.Connection):
        super().__init__(owner)
        self._conn = conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await self._execute(self._conn, sql, params)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        await self._executemany(self._conn, sql, seq)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return await self._fetchall(self._conn, sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        return await self._fetchone(self._conn, sql, params)


class PostgresDatabase(_Executor):
    """Drop-in replacement for app.db.Database backed by an asyncpg pool (Supabase Postgres)."""

    dialect = "postgres"

    def __init__(self, dsn: str, schema: str):
        super().__init__(self)
        self.dsn = dsn
        self.schema = schema
        self.path = redact_dsn(dsn)  # shown in /api/health and the terminal
        self.conn: asyncpg.Pool | None = None  # non-None while open (EventBus checks it)
        self._types: dict[str, list[str]] = {}
        self._size = 0
        self._size_task: asyncio.Task | None = None

    @classmethod
    async def open(cls, dsn: str, schema: str = "nexus") -> "PostgresDatabase":
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
            raise ValueError(f"Nombre de esquema no válido: {schema!r}")
        db = cls(dsn, schema)
        db.conn = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=int(os.environ.get("NEXUS_PG_POOL_MAX", "5")),
            command_timeout=60,
            ssl=_ssl_option(dsn),
            statement_cache_size=int(os.environ.get("NEXUS_PG_STATEMENT_CACHE", "100")),
            server_settings={"search_path": f"{schema},public", "application_name": "nexus-terminal"},
        )
        return db

    async def init_schema(self) -> None:
        script = SCHEMA_FILE.read_text(encoding="utf-8").replace("{schema}", self.schema)
        async with self.conn.acquire() as conn:
            await conn.execute(script)
        self._types.clear()
        await self.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        await self.refresh_size()
        self._size_task = asyncio.create_task(self._size_loop(), name="pg-size")

    # ---------------------------------------------------------------- public API (same as Database)
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self.conn.acquire() as conn:
            return await self._execute(conn, sql, params)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        rows = list(seq)
        if not rows:
            return
        async with self.conn.acquire() as conn:
            async with conn.transaction():
                await self._executemany(conn, sql, rows)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        async with self.conn.acquire() as conn:
            return await self._fetchall(conn, sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        async with self.conn.acquire() as conn:
            return await self._fetchone(conn, sql, params)

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PgTransaction]:
        async with self.conn.acquire() as conn:
            async with conn.transaction():
                yield PgTransaction(self, conn)

    async def table_counts(self) -> dict[str, int]:
        async with self.conn.acquire() as conn:
            names = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = $1 AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                self.schema,
            )
            out: dict[str, int] = {}
            for rec in names:
                name = rec["table_name"]
                out[name] = int(await conn.fetchval(f'SELECT COUNT(*) FROM "{self.schema}"."{name}"'))
        await self.refresh_size()
        return out

    def size_bytes(self) -> int:
        return self._size

    async def refresh_size(self) -> None:
        try:
            async with self.conn.acquire() as conn:
                self._size = int(
                    await conn.fetchval(
                        "SELECT COALESCE(SUM(pg_total_relation_size(format('%I.%I', schemaname, tablename))), 0) "
                        "FROM pg_tables WHERE schemaname = $1",
                        self.schema,
                    )
                )
        except Exception:
            pass

    async def _size_loop(self) -> None:
        while True:
            await asyncio.sleep(120)
            await self.refresh_size()

    async def vacuum(self) -> None:
        async with self.conn.acquire() as conn:
            names = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = $1", self.schema)
            for rec in names:
                await conn.execute(f'VACUUM (ANALYZE) "{self.schema}"."{rec["tablename"]}"')
        await self.refresh_size()

    async def close(self) -> None:
        if self._size_task:
            self._size_task.cancel()
            self._size_task = None
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def drop_schema(self) -> None:
        """Tests only: remove the whole schema."""
        async with self.conn.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
