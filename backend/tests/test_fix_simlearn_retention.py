"""Fix round (simlearn): db-growth-scanner-retention.

Resolved scanner predictions are pruned after ~30 days while `prediction_daily` keeps the totals, hit rates,
calibration and daily charts; scan_results are kept for the newest runs only; NEXUS memories untouched for 90 days
are dropped; resolved rows lose their feature vector; the stats no longer scan the whole predictions table.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config as nexus_config
from app.maintenance import Maintenance, NEXUS_MODEL_DAYS, SCAN_RESULT_RUNS, SCANNER_RESOLVED_DAYS
from app.sim import evaluator as ev
from app.sim.scanner import encode_features

DAY_MS = 86_400_000
NOW = 1_760_000_400_000


async def _resolved(db, *, source="scanner", symbol="BINANCE:BTCUSDT", days_ago: float, prob=0.7, ret=1.0,
                    correct=1, key="crypto|1h|14400"):
    created = int(NOW - days_ago * DAY_MS)
    await db.execute(
        "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, prob_up, score, "
        "features, resolved, outcome_price, outcome_return_pct, correct, resolved_at) "
        "VALUES (?, ?, '1h', ?, ?, ?, 100, 1, ?, 0.4, NULL, 1, ?, ?, ?, ?)",
        (source, symbol, key if source == "scanner" else f"{symbol}|1h", created, created + 4 * 3600 * 1000, prob,
         100 + ret, ret, correct, created + 5 * 3600 * 1000),
    )


async def _snapshot(db, settings) -> dict:
    stats = await ev.prediction_stats(db)
    summary = await ev.learning_summary(db, settings)
    return {"stats": stats, "timeline": summary["timeline"], "scanner": summary["scanner_model"],
            "calibration": await ev.calibration_bins(db, model_key_value="crypto|1h|14400")}


def test_old_scanner_rows_are_pruned_and_the_statistics_stay(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            # history from before the aggregates existed (an upgraded database): folded in once
            for i, (prob, ret, ok) in enumerate([(0.8, 1.0, 1), (0.7, -0.5, 0), (0.3, -1.0, 1), (0.6, 2.0, 1)]):
                await _resolved(db, days_ago=40 + i, prob=prob, ret=ret, correct=ok)
            await _resolved(db, days_ago=45, ret=0.0, correct=None)  # resolved without verdict
            await _resolved(db, source="nexus", days_ago=50, prob=0.55, ret=0.3, correct=1)
            await _resolved(db, days_ago=2, prob=0.65, ret=0.8, correct=1)
            await db.execute(
                "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                "VALUES ('scanner', 'crypto|1h|14400', '{}', 5, NULL, ?)", (NOW,))
            before = await _snapshot(db, rt.settings.all())
            assert before["stats"]["resolved"] == 6 and before["stats"]["total"] == 7
            assert await db.fetchval("SELECT value FROM meta WHERE key='prediction_daily'") == "1"

            await Maintenance(rt).run_once(now=NOW)
            left = await db.fetchall("SELECT source, created_at FROM predictions ORDER BY created_at")
            assert [r["source"] for r in left] == ["nexus", "scanner"]  # scanner rows > 30 days gone
            assert all(NOW - r["created_at"] < SCANNER_RESOLVED_DAYS * DAY_MS for r in left if r["source"] == "scanner")
            after = await _snapshot(db, rt.settings.all())
            assert after == before  # totals, hit rate, Brier, daily charts and calibration unchanged
            assert len(after["timeline"]) == 6

            # a new process: the flag prevents folding the rows in twice
            ev._DAILY_READY.discard(db)
            assert (await ev.prediction_stats(db))["resolved"] == 6

    asyncio.run(scenario())


def test_resolution_updates_the_aggregates_and_drops_the_features(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    class Hub:
        async def quotes(self, symbols):
            return {s: {"symbol": s, "price": 103.0} for s in symbols}

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            await ev.ensure_daily_aggregates(db)  # aggregates already in place (the normal running state)
            pid = await db.execute(
                "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, "
                "prob_up, score, features) VALUES ('scanner', 'BINANCE:BTCUSDT', '1h', 'crypto|1h|14400', ?, ?, 100, "
                "1, 0.72, 0.44, ?)",
                (NOW - 4 * 3600 * 1000, NOW - 60_000, encode_features({"rec_60": 0.5})),
            )
            rt.hub = Hub()
            out = await ev.PredictionEvaluator(rt).run_once(now=NOW)
            assert out["predictions"] == 1 and out["scanner_updates"] == 1
            row = await db.fetchone("SELECT features, correct FROM predictions WHERE id=?", (pid,))
            assert row["features"] is None and row["correct"] == 1
            agg = await db.fetchall("SELECT * FROM prediction_daily")
            assert len(agg) == 1
            a = agg[0]
            assert (a["source"], a["model_key"], a["bin"], a["n"], a["scored"], a["correct"], a["up"]) == \
                ("scanner", "crypto|1h|14400", 7, 1, 1, 1, 1)
            assert a["prob_sum"] == pytest.approx(0.72) and a["brier_sum"] == pytest.approx(0.28 ** 2)
            assert a["day"] == (NOW - 60_000) // DAY_MS * 86400

    asyncio.run(scenario())


def test_scan_results_kept_for_the_newest_runs_only(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            ids = []
            for i in range(SCAN_RESULT_RUNS + 5):
                rid = await db.execute(
                    "INSERT INTO scan_runs(market, tf, started_at, finished_at, n_symbols, status, deep, message, params) "
                    "VALUES ('crypto', '1h', ?, ?, 2, 'done', 0, 'ok', '{}')", (NOW - (200 - i) * 60_000, NOW))
                ids.append(rid)
                for sym in ("A:B", "C:D"):
                    await db.execute(
                        "INSERT INTO scan_results(run_id, symbol, rank, score, prob_up, action, confidence, price, "
                        "change_pct, volume, verdict, gate_score, data) VALUES (?, ?, 1, 0.1, 0.55, 'BUY', 0.5, 1, 0, "
                        "0, NULL, NULL, '{}')", (rid, sym))
            await Maintenance(rt).run_once(now=NOW)
            kept = {r["run_id"] for r in await db.fetchall("SELECT DISTINCT run_id FROM scan_results")}
            assert kept == set(ids[-SCAN_RESULT_RUNS:])
            assert await db.fetchval("SELECT COUNT(*) FROM scan_runs") == len(ids)  # the run summaries stay

    asyncio.run(scenario())


def test_untouched_nexus_memories_are_dropped(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            rows = [("nexus", "OLD:ONE|1h", NEXUS_MODEL_DAYS + 5), ("nexus", "NEW:ONE|1h", 10),
                    ("scanner", "crypto|1h|14400", 400), ("gate", "global", 400)]
            for kind, key, days in rows:
                await db.execute(
                    "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES (?, ?, '{}', 1, "
                    "NULL, ?)", (kind, key, NOW - days * DAY_MS))
            await Maintenance(rt).run_once(now=NOW)
            left = {(r["kind"], r["key"]) for r in await db.fetchall("SELECT kind, key FROM model_state")}
            assert left == {("nexus", "NEW:ONE|1h"), ("scanner", "crypto|1h|14400"), ("gate", "global")}

    asyncio.run(scenario())


def test_prediction_indexes_exist(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            if db.dialect == "postgres":
                rows = await db.fetchall("SELECT indexname AS name FROM pg_indexes WHERE tablename = 'predictions' "
                                         "AND schemaname = current_schema()")
            else:
                rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='predictions'")
            names = {r["name"] for r in rows}
            assert {"idx_pred_source", "idx_pred_model"} <= names
            # the schema scripts are idempotent (run again at every start)
            await db.init_schema()
            assert json.loads(json.dumps(await ev.learning_summary(db)))["gate"]["decisions"] == 0

    asyncio.run(scenario())
