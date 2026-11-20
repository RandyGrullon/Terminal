"""Publishes this backend's current public address in Supabase, so a fixed link (the frontend on Vercel built with
VITE_NEXUS_DISCOVERY_URL / VITE_NEXUS_DISCOVERY_KEY) finds it even when the address changes (a Cloudflare quick tunnel
gets a new https://….trycloudflare.com on every restart).

    python tools/publish_endpoint.py https://xxxx.trycloudflare.com

Uses NEXUS_DATABASE_URL (the same Postgres the app uses). Keeps one row in public.nexus_endpoint (created on first use,
outside the app's schema): row-level security on, read-only for the publishable key (the address is not a secret:
every call to it still needs a login), written only through the database connection. Prints nothing secret.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

DDL = """
create table if not exists public.nexus_endpoint (
  id int primary key check (id = 1),
  url text not null,
  updated_at timestamptz not null default now()
);
alter table public.nexus_endpoint enable row level security;
do $$ begin
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'nexus_endpoint'
                 and policyname = 'nexus_endpoint_read') then
    create policy nexus_endpoint_read on public.nexus_endpoint for select to anon, authenticated using (true);
  end if;
end $$;
revoke all on public.nexus_endpoint from anon, authenticated;
grant select on public.nexus_endpoint to anon, authenticated;
"""

URL_RE = re.compile(r"^https://[a-z0-9.-]+(:\d+)?$", re.I)


async def publish(db_url: str, url: str) -> None:
    import asyncpg  # the app's dependency

    conn = await asyncpg.connect(db_url, statement_cache_size=0, timeout=20)
    try:
        await conn.execute(DDL)
        await conn.execute(
            "insert into public.nexus_endpoint (id, url, updated_at) values (1, $1, now()) "
            "on conflict (id) do update set url = excluded.url, updated_at = now()", url)
        await conn.execute("notify pgrst, 'reload schema'")  # the REST API sees the table on first use
    finally:
        await conn.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not URL_RE.match(argv[1].strip().rstrip("/")):
        print("uso: publish_endpoint.py https://<host>", file=sys.stderr)
        return 2
    db_url = os.environ.get("NEXUS_DATABASE_URL", "").strip()
    if not db_url:
        print("falta NEXUS_DATABASE_URL", file=sys.stderr)
        return 2
    url = argv[1].strip().rstrip("/")
    for attempt in range(5):
        try:
            asyncio.run(publish(db_url, url))
            print(f"enlace publicado: {url}")
            return 0
        except Exception as exc:  # network hiccup: retry
            print(f"intento {attempt + 1}: no se pudo publicar ({type(exc).__name__})", file=sys.stderr)
            if attempt < 4:
                import time
                time.sleep(10 * (attempt + 1))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
