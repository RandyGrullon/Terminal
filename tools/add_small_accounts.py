"""Adds small accounts of NEXUS automática (kind "twin") straight in the database, for a server that has no session to
call POST /api/paper/autopilot/twin with (e.g. the phone). Run it with NEXUS STOPPED: the running engine keeps the
challenge records in memory and would write its own copy back.

    python tools/add_small_accounts.py 500:5000 100:1000      # initial:target ...

Uses NEXUS_DATABASE_URL (PostgreSQL) and NEXUS_DB_SCHEMA (default public). Skips an account whose name exists.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.sim import autopilot_challenges as CH  # noqa: E402
from app.sim.common import fmt_num  # noqa: E402


def default_name(ini: float) -> str:
    return f"NEXUS {fmt_num(ini / 1000.0, 0)}k" if ini >= 1000 and ini % 1000 == 0 else f"NEXUS {fmt_num(ini, 0)}"


async def main(pairs: list[tuple[float, float]]) -> int:
    import asyncpg

    schema = os.environ.get("NEXUS_DB_SCHEMA", "public").strip() or "public"
    conn = await asyncpg.connect(os.environ["NEXUS_DATABASE_URL"], statement_cache_size=0, timeout=20)
    try:
        await conn.execute(f'set search_path to "{schema}"')
        async with conn.transaction():
            raw = await conn.fetchval("select value from meta where key = 'autopilot.challenges'")
            chs = json.loads(raw) if raw else {}
            top = await conn.fetchval("select coalesce(max(id), 0) from paper_accounts") or 0
            for ini, tgt in pairs:
                name = default_name(ini)
                if name.lower() in {c.get("name", "").lower() for c in chs.values()}:
                    print(f"ya existe «{name}»: se omite")
                    continue
                now = int(time.time())
                rec = CH.parse_twin(ini, name, now=now, taken={c["name"] for c in chs.values()}, target=tgt)
                aid = max(CH.FIRST_ID, int(top) + 1, *(int(k) + 1 for k in chs))
                ts = now * 1000
                await conn.execute(
                    "insert into paper_accounts(id, name, balance, initial_balance, currency, created_at, updated_at) "
                    "values ($1, $2, $3, $3, 'USD', $4, $4)", aid, rec["name"], rec["initial"], ts)
                chs[str(aid)] = {"id": aid, **rec}
                top = aid
                print(f"creada «{rec['name']}» (cuenta {aid}): {CH.money(rec['initial'])} → {CH.money(rec['target'])}")
            await conn.execute(
                "insert into meta(key, value) values ('autopilot.challenges', $1) "
                "on conflict (key) do update set value = excluded.value", json.dumps(chs))
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    try:
        args = [(float(a.split(":")[0]), float(a.split(":")[1])) for a in sys.argv[1:]]
    except (IndexError, ValueError):
        args = []
    if not args:
        print("uso: add_small_accounts.py 500:5000 100:1000", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(args)))
