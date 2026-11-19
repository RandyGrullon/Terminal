"""Copy the local SQLite database (data/nexus.db) to PostgreSQL / Supabase, so the cloud starts with your accounts.

    cd backend
    $env:NEXUS_DATABASE_URL = "postgresql://..."      # the Supabase Session pooler URI (never passed as an argument)
    .venv/Scripts/python -m app.dbcopy --dry-run       # what it would copy, writes nothing
    .venv/Scripts/python -m app.dbcopy                 # the copy

What it copies: every table of the app (accounts, positions, fills, orders, settings and API keys, meta: goals,
challenge accounts, lab trials…, NEXUS's memory, scanner and predictions history, scripts, backtests). Candles are a
cache the cloud downloads again by itself: by default only the recorded ones (source 'recorder', live quotes with no
downloadable history, e.g. OANDA:XAUUSD) are copied; `--candles all` copies every one (watch Supabase's 500 MB),
`--candles none` none.

Safety:
- the source is opened read-only (one consistent snapshot, also with the local server running);
- it refuses while the local server answers on 127.0.0.1:8420 (what happens after the copy would stay only on this PC)
  unless --allow-running;
- it refuses when another NEXUS instance holds the leader lock of that database (the cloud backend running: stop its
  service first), and when the target already has data, unless --replace (which empties the copied tables first);
- everything goes in ONE transaction: an error leaves the target as it was;
- at the end it compares the row counts and every account's balance with the source.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from app.db_pg import IDENTITY_TABLES, PostgresDatabase, coerce, redact_dsn

CANDLE_MODES = ("recorded", "all", "none")
SKIP_TABLES = {"schema_migrations"}  # the target keeps its own
SKIP_META_KEYS = {"schema_version"}
BATCH = 5000
# Tables that a fresh target may already hold without that meaning someone used it (init_schema / first start).
SEED_OK = {"meta", "settings", "symbols", "schema_migrations"}


class CopyError(Exception):
    """A reason not to copy, in Spanish."""


def source_tables(src: sqlite3.Connection) -> list[str]:
    return [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def where_of(table: str, candles: str) -> str | None:
    """None = skip the table; '' = every row; otherwise a WHERE clause for the source."""
    if table == "candles":
        return None if candles == "none" else ("" if candles == "all" else "WHERE source = 'recorder'")
    if table == "meta":
        return "WHERE key NOT IN (" + ",".join(f"'{k}'" for k in sorted(SKIP_META_KEYS)) + ")"
    return ""


def open_source(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise CopyError(f"No existe la base de datos local: {path}")
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    src.execute("BEGIN")  # one read transaction = one consistent snapshot while the copy runs
    return src


def plan_copy(src: sqlite3.Connection, candles: str, target_tables: Iterable[str]) -> list[dict[str, Any]]:
    target = set(target_tables)
    rows: list[dict[str, Any]] = []
    for t in source_tables(src):
        if t in SKIP_TABLES or t not in target:
            continue
        where = where_of(t, candles)
        if where is None:
            rows.append({"table": t, "rows": 0, "skipped": True, "where": None})
            continue
        n = int(src.execute(f'SELECT COUNT(*) FROM "{t}" {where}').fetchone()[0])
        rows.append({"table": t, "rows": n, "skipped": False, "where": where})
    return rows


def local_server_running(port: int = 8420) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
            return True
    except Exception:
        return False


async def _column_types(conn: Any, schema: str, table: str) -> dict[str, str]:
    recs = await conn.fetch(
        "SELECT column_name, udt_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2",
        schema, table,
    )
    return {r["column_name"]: r["udt_name"] for r in recs}


def _clean(value: Any, typ: str) -> Any:
    v = coerce(value, typ)
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


async def copy_database(src_path: Path, dsn: str, schema: str = "nexus", *, candles: str = "recorded",
                        replace: bool = False, dry_run: bool = False,
                        say: Callable[[str], None] = print) -> dict[str, Any]:
    if candles not in CANDLE_MODES:
        raise CopyError(f"--candles debe ser uno de {', '.join(CANDLE_MODES)}")
    src = open_source(src_path)
    db = await PostgresDatabase.open(dsn, schema=schema)
    lock = None
    try:
        say(f"Destino: {redact_dsn(dsn)} (esquema «{schema}»)")
        await db.init_schema()  # creates the schema and applies the migrations, like the backend at start
        lock = db.advisory_lock()
        if not await lock.try_acquire():
            lock = None
            raise CopyError("Otra instancia de NEXUS está usando esa base de datos (tiene el bloqueo de líder). "
                            "Detén el servicio en Railway y vuelve a intentarlo.")
        async with db.conn.acquire() as conn:
            target_tables = [r["table_name"] for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = $1 AND table_type = 'BASE TABLE'",
                schema)]
            plan = plan_copy(src, candles, target_tables)
            busy = []
            for p in plan:
                n = int(await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}"."{p["table"]}"'))
                if n and p["table"] not in SEED_OK:
                    busy.append(f'{p["table"]} ({n})')
            for p in plan:
                say(f'  {p["table"]:<18} {"omitida (las velas se vuelven a descargar)" if p["skipped"] else format(p["rows"], ",d").replace(",", ".") + " filas"}')
            if busy and not replace:
                raise CopyError("El destino ya tiene datos: " + ", ".join(busy) + ". Usa --replace para vaciar esas "
                                "tablas y copiar encima (se pierde lo que haya allí).")
            if dry_run:
                say("Prueba (--dry-run): no se escribió nada.")
                return {"plan": plan, "written": False}
            async with conn.transaction():
                tables = [p for p in plan if not p["skipped"]]
                if replace or busy:
                    names = ", ".join(f'"{schema}"."{p["table"]}"' for p in plan)
                    await conn.execute(f"TRUNCATE {names}")
                else:  # meta / settings / symbols a first start may have filled: the local values win
                    names = ", ".join(f'"{schema}"."{p["table"]}"' for p in tables if p["table"] in SEED_OK)
                    if names:
                        await conn.execute(f"TRUNCATE {names}")
                for p in tables:
                    t = p["table"]
                    types = await _column_types(conn, schema, t)
                    cur = src.execute(f'SELECT * FROM "{t}" {p["where"]}')
                    cols = [d[0] for d in cur.description]
                    keep = [i for i, c in enumerate(cols) if c in types]
                    names_ = [cols[i] for i in keep]
                    tps = [types[c] for c in names_]
                    done = 0
                    while True:
                        chunk = cur.fetchmany(BATCH)
                        if not chunk:
                            break
                        recs = [tuple(_clean(r[i], tp) for i, tp in zip(keep, tps)) for r in chunk]
                        await conn.copy_records_to_table(t, records=recs, columns=names_, schema_name=schema)
                        done += len(recs)
                    if t in IDENTITY_TABLES:
                        await conn.execute(
                            f"SELECT setval(pg_get_serial_sequence('\"{schema}\".\"{t}\"', 'id'), "
                            f'COALESCE((SELECT MAX(id) FROM "{schema}"."{t}"), 1), '
                            f'(SELECT MAX(id) FROM "{schema}"."{t}") IS NOT NULL)')
                    say(f"  copiada {t}: {done:,d} filas".replace(",", "."))
            # ---- check against the source snapshot
            problems = []
            for p in plan:
                if p["skipped"]:
                    continue
                n = int(await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}"."{p["table"]}" '
                                            + ("WHERE source = 'recorder'" if p["table"] == "candles" and candles == "recorded" else "")
                                            + ("WHERE key NOT IN ('schema_version')" if p["table"] == "meta" else "")))
                if n != p["rows"]:
                    problems.append(f'{p["table"]}: {n} de {p["rows"]} filas')
            accounts = []
            for aid, name, bal in src.execute("SELECT id, name, balance FROM paper_accounts ORDER BY id"):
                got = await conn.fetchval(f'SELECT balance FROM "{schema}".paper_accounts WHERE id = $1', aid)
                ok = got is not None and abs(float(got) - float(bal)) < 1e-6
                accounts.append({"id": aid, "name": name, "balance": bal, "ok": ok})
                if not ok:
                    problems.append(f"cuenta {aid} ({name}): saldo {got} en vez de {bal}")
            for a in accounts:
                say(f'  cuenta {a["id"]:>3} {a["name"]:<28} {a["balance"]:>12,.2f} $  {"igual" if a["ok"] else "DISTINTA"}')
            if problems:
                raise CopyError("La comprobación encontró diferencias: " + "; ".join(problems))
            say("Copia terminada y comprobada: mismas filas y mismos saldos que la base local.")
            return {"plan": plan, "written": True, "accounts": accounts}
    finally:
        if lock is not None:
            await lock.release()
        await db.close()
        src.close()


def main(argv: list[str] | None = None) -> int:
    from app import config

    ap = argparse.ArgumentParser(prog="python -m app.dbcopy", description="Copia la base local de NEXUS a PostgreSQL / Supabase.")
    ap.add_argument("--from", dest="src", default=str(config.DB_PATH), help="base SQLite local (por defecto data/nexus.db)")
    ap.add_argument("--candles", choices=CANDLE_MODES, default="recorded",
                    help="velas a copiar: recorded = solo las grabadas en vivo (por defecto), all = todas, none = ninguna")
    ap.add_argument("--replace", action="store_true", help="vacía en el destino las tablas copiadas antes de copiar")
    ap.add_argument("--dry-run", action="store_true", help="muestra lo que copiaría sin escribir nada")
    ap.add_argument("--allow-running", action="store_true", help="copia aunque el servidor local esté encendido")
    args = ap.parse_args(argv)
    dsn = os.environ.get("NEXUS_DATABASE_URL", "").strip()
    if not dsn:
        print("Falta NEXUS_DATABASE_URL (la URI del Session pooler de Supabase) en esta ventana.", file=sys.stderr)
        return 2
    if not args.dry_run and not args.allow_running and local_server_running():
        print("El servidor local de NEXUS está encendido: ciérralo antes de copiar (lo que pase después de la copia "
              "quedaría solo en esta PC), o usa --allow-running.", file=sys.stderr)
        return 2
    try:
        asyncio.run(copy_database(Path(args.src), dsn, os.environ.get("NEXUS_DB_SCHEMA", config.DB_SCHEMA),
                                  candles=args.candles, replace=args.replace, dry_run=args.dry_run))
    except CopyError as exc:
        print(f"No se copió: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
