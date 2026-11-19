"""app.dbcopy: the local SQLite database copied to PostgreSQL (needs NEXUS_TEST_PG_URL; skipped without it)."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from app import dbcopy

PG_URL = os.environ.get("NEXUS_TEST_PG_URL", "").strip()


def test_where_of_candles_and_meta():
    assert dbcopy.where_of("candles", "none") is None
    assert dbcopy.where_of("candles", "all") == ""
    assert "recorder" in dbcopy.where_of("candles", "recorded")
    assert "schema_version" in dbcopy.where_of("meta", "recorded")
    assert dbcopy.where_of("paper_accounts", "recorded") == ""


def test_missing_source_is_a_spanish_error(tmp_path: Path):
    with pytest.raises(dbcopy.CopyError, match="No existe la base de datos local"):
        dbcopy.open_source(tmp_path / "nada.db")


def test_cli_needs_the_url(monkeypatch, capsys):
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    assert dbcopy.main(["--dry-run"]) == 2
    assert "NEXUS_DATABASE_URL" in capsys.readouterr().err


def _source(path: Path) -> None:
    from app.db import Database

    async def make():
        db = await Database.open(path)
        await db.init_schema()
        await db.close()

    asyncio.run(make())
    c = sqlite3.connect(path)
    c.execute("INSERT OR REPLACE INTO paper_accounts(id, name, balance, initial_balance, created_at) VALUES (3, 'NEXUS automática', 10106.61, 10000, 1)")
    c.execute("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) VALUES ('OANDA:XAUUSD','5m',1,1,1,1,1,0,'recorder')")
    c.execute("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) VALUES ('BINANCE:BTCUSDT','5m',1,1,1,1,1,0,'binance')")
    c.execute("INSERT INTO meta(key, value) VALUES ('autopilot.challenges', '[1]')")
    c.commit()
    c.close()


@pytest.mark.skipif(not PG_URL, reason="needs NEXUS_TEST_PG_URL (PostgreSQL)")
def test_copy_check_and_refuse_a_second_copy(tmp_path: Path):
    src = tmp_path / "nexus.db"
    _source(src)
    schema = "t_dbcopy_" + uuid.uuid4().hex[:8]
    lines: list[str] = []
    try:
        out = asyncio.run(dbcopy.copy_database(src, PG_URL, schema, say=lines.append))
        assert out["written"] and all(a["ok"] for a in out["accounts"])
        assert any(a["id"] == 3 and abs(a["balance"] - 10106.61) < 1e-9 for a in out["accounts"])
        plan = {p["table"]: p for p in out["plan"]}
        assert plan["candles"]["rows"] == 1  # only the recorded candle

        with pytest.raises(dbcopy.CopyError, match="ya tiene datos"):
            asyncio.run(dbcopy.copy_database(src, PG_URL, schema, say=lines.append))
        again = asyncio.run(dbcopy.copy_database(src, PG_URL, schema, replace=True, say=lines.append))
        assert again["written"]

        import asyncpg

        async def check():
            conn = await asyncpg.connect(PG_URL)
            try:
                assert await conn.fetchval(f'SELECT value FROM "{schema}".meta WHERE key = $1', "autopilot.challenges") == "[1]"
                assert await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}".candles') == 1
            finally:
                await conn.close()

        asyncio.run(check())
    finally:
        import asyncpg

        async def drop():
            conn = await asyncpg.connect(PG_URL)
            try:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await conn.close()

        asyncio.run(drop())
