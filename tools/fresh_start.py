"""Fresh start of NEXUS automática in the database (run it with NEXUS STOPPED): first a full backup of what it removes
to a JSON file, then removes every position / fill / pending order of its accounts (3 and the running small /
challenge accounts), puts each account back at its initial balance, empties the vault, the decision log, the
challenge results, the weekly summary and the daily blocks. Accounts 1 and 2, the settings, the scanner and the learned
models are not touched.

    python tools/fresh_start.py --backup-dir backend/.tmp/backups [--dry-run]

Uses NEXUS_DATABASE_URL (PostgreSQL) and NEXUS_DB_SCHEMA. Restore = the backup file (rows as they were).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time

KEEP_ACCOUNTS = (1, 2)
META_RESET = {"autopilot.vault": json.dumps({"total": 0.0, "by_account": {}, "entries": []}),
              "autopilot.state": None, "autopilot.goal": "null", "autopilot.goal_last": None,
              "autopilot.weekly": None, "autopilot.weekly_week": None, "autopilot.hold": "{}"}


def _jsonable(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


async def main(args) -> int:
    import asyncpg

    schema = os.environ.get("NEXUS_DB_SCHEMA", "public").strip() or "public"
    conn = await asyncpg.connect(os.environ["NEXUS_DATABASE_URL"], statement_cache_size=0, timeout=30)
    try:
        await conn.execute(f'set search_path to "{schema}"')
        raw = await conn.fetchval("select value from meta where key = 'autopilot.challenges'")
        chs = json.loads(raw) if raw else {}
        ids = [3, *sorted(int(k) for k, v in chs.items() if v.get("status") == "running")]
        dead = sorted(int(k) for k, v in chs.items() if v.get("status") != "running")
        backup: dict = {"taken_at": int(time.time()), "schema": schema, "accounts": ids, "tables": {}, "meta": {}}
        for t in ("paper_positions", "paper_fills", "paper_orders"):
            rows = await conn.fetch(f"select * from {t} where account_id = any($1::bigint[])", ids + dead)
            backup["tables"][t] = [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows]
        rows = await conn.fetch("select * from paper_accounts")
        backup["tables"]["paper_accounts"] = [dict(r) for r in rows]
        for r in await conn.fetch("select key, value from meta where key like 'autopilot.%' or key like 'paper.day_low.%'"):
            backup["meta"][r["key"]] = r["value"]
        os.makedirs(args.backup_dir, exist_ok=True)
        path = os.path.join(args.backup_dir, f"nexus-backup-{dt.datetime.now():%Y%m%d-%H%M%S}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(backup, f, ensure_ascii=False)
        n = {t: len(v) for t, v in backup["tables"].items()}
        print(f"copia de seguridad: {path} · {n}")
        if args.dry_run:
            print("--dry-run: no se borró nada")
            return 0
        async with conn.transaction():
            allids = ids + dead
            for t in ("paper_fills", "paper_orders", "paper_positions"):
                await conn.execute(f"delete from {t} where account_id = any($1::bigint[])", allids)
            for aid in ids:
                await conn.execute("update paper_accounts set balance = initial_balance, updated_at = $2 where id = $1",
                                   aid, int(time.time() * 1000))
            if dead:  # ended challenges: gone (their accounts too)
                await conn.execute("delete from paper_accounts where id = any($1::bigint[])", dead)
            now = int(time.time())
            keep = {}
            for k, v in chs.items():
                if int(k) in ids:
                    v.update({"started_at": now, "reached_at": None, "ended_at": None, "result": None, "status": "running"})
                    keep[k] = v
            await conn.execute("update meta set value = $1 where key = 'autopilot.challenges'", json.dumps(keep))
            for k, v in META_RESET.items():
                if v is None:
                    await conn.execute("delete from meta where key = $1", k)
                else:
                    await conn.execute("insert into meta(key, value) values ($1, $2) on conflict (key) do update set "
                                       "value = excluded.value", k, v)
            await conn.execute("delete from meta where key like 'paper.day_low.%' and key not in ('paper.day_low.1', "
                               "'paper.day_low.2')")
        for r in await conn.fetch("select id, name, balance from paper_accounts where id = any($1::bigint[]) order by id", ids):
            print(f"cuenta {r['id']} «{r['name']}»: {r['balance']:,.2f} $")
        left = await conn.fetchval("select count(*) from paper_positions where account_id = any($1::bigint[])", ids)
        print(f"posiciones que quedan en esas cuentas: {left}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args())))
