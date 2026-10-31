"""Integration round 2 (core): retention of the demo orders and of the NEXUS decision records (app.maintenance).

- paper_orders: rows no longer pending (filled / cancelled / expired) go after PAPER_ORDER_DAYS of their last
  update; a pending order is never touched, however old;
- model_state kinds 'nexus_veto' / 'nexus_gate' (semáforo vetoes and bar-close gate decisions every later run replays):
  a row goes only when it is NEXUS_MODEL_DAYS old AND no 'nexus' memory of the same key is left. A symbol still in
  use keeps an old row (its updated_at only moves when a new veto / decision is recorded); a memory dropped in the
  same pass takes its rows with it; every other model_state kind is left alone.
Runs on SQLite, and on PostgreSQL with NEXUS_TEST_PG_URL.
"""
from __future__ import annotations

import asyncio

from app import config as nexus_config
from app.maintenance import NEXUS_MODEL_DAYS, PAPER_ORDER_DAYS, Maintenance

DAY_MS = 86_400_000
NOW = 1_790_000_000_000


async def _order(db, status: str, days_ago: float, symbol: str) -> None:
    at = int(NOW - days_ago * DAY_MS)
    await db.execute(
        "INSERT INTO paper_orders(account_id, symbol, tf, side, kind, price, status, created_at, updated_at) "
        "VALUES (1, ?, '1h', 'long', 'limit', 100.0, ?, ?, ?)",
        (symbol, status, at - DAY_MS, at),
    )


async def _state(db, kind: str, key: str, days_ago: float) -> None:
    await db.execute(
        "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES (?, ?, '{}', 1, NULL, ?)",
        (kind, key, int(NOW - days_ago * DAY_MS)),
    )


def test_finished_demo_orders_are_kept_a_year_and_pending_ones_forever(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            await _order(db, "pending", PAPER_ORDER_DAYS + 200, "SIM:PENDIENTE")  # GTC, placed long ago
            await _order(db, "filled", PAPER_ORDER_DAYS + 5, "SIM:LLENA_VIEJA")
            await _order(db, "cancelled", PAPER_ORDER_DAYS + 1, "SIM:CANCELADA_VIEJA")
            await _order(db, "expired", PAPER_ORDER_DAYS + 30, "SIM:CADUCADA_VIEJA")
            await _order(db, "filled", PAPER_ORDER_DAYS - 1, "SIM:LLENA_RECIENTE")
            await _order(db, "cancelled", 3, "SIM:CANCELADA_RECIENTE")
            await Maintenance(rt).run_once(now=NOW)
            left = {r["symbol"]: r["status"] for r in await db.fetchall("SELECT symbol, status FROM paper_orders")}
            assert left == {"SIM:PENDIENTE": "pending", "SIM:LLENA_RECIENTE": "filled",
                            "SIM:CANCELADA_RECIENTE": "cancelled"}

    asyncio.run(scenario())


def test_vetoes_and_gate_decisions_go_only_with_their_memory(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    old, fresh = NEXUS_MODEL_DAYS + 30, 10

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            # A: watched every day; its decision rows were last written long ago but are still replayed
            await _state(db, "nexus", "SIM:A|1h", 1)
            await _state(db, "nexus_veto", "SIM:A|1h", NEXUS_MODEL_DAYS + 200)
            await _state(db, "nexus_gate", "SIM:A|1h", old)
            # B: nobody analysed it for months: memory, vetoes and gate decisions go together in this pass
            await _state(db, "nexus", "SIM:B|4h", NEXUS_MODEL_DAYS + 5)
            await _state(db, "nexus_veto", "SIM:B|4h", NEXUS_MODEL_DAYS + 6)
            await _state(db, "nexus_gate", "SIM:B|4h", NEXUS_MODEL_DAYS + 7)
            # C: memory reset recently (no 'nexus' row), its records are recent: kept for the re-learned history
            await _state(db, "nexus_veto", "SIM:C|1h", fresh)
            await _state(db, "nexus_gate", "SIM:C|1h", fresh)
            # D: no memory for a long time and old records: dropped
            await _state(db, "nexus_veto", "SIM:D|1d", old)
            await _state(db, "nexus_gate", "SIM:D|15m", old)
            # other kinds are never touched by this rule, however old
            for kind, key in (("scanner", "crypto|1h|14400"), ("gate", "global"), ("gate", "check_stats"),
                              ("macro", "calendar"), ("alerts", "state")):
                await _state(db, kind, key, NEXUS_MODEL_DAYS + 400)
            await Maintenance(rt).run_once(now=NOW)
            rows = await db.fetchall("SELECT kind, key FROM model_state")
            left = sorted((r["kind"], r["key"]) for r in rows)
            assert left == sorted([
                ("nexus", "SIM:A|1h"), ("nexus_veto", "SIM:A|1h"), ("nexus_gate", "SIM:A|1h"),
                ("nexus_veto", "SIM:C|1h"), ("nexus_gate", "SIM:C|1h"),
                ("scanner", "crypto|1h|14400"), ("gate", "global"), ("gate", "check_stats"),
                ("macro", "calendar"), ("alerts", "state"),
            ])
            await Maintenance(rt).run_once(now=NOW)  # idempotent
            assert len(await db.fetchall("SELECT kind FROM model_state")) == len(left)

    asyncio.run(scenario())
