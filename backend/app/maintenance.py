"""Data retention. Keeps the database lean (essential on Supabase's free 500 MB tier, nice to have locally):
fine-grained candles, old events and resolved learning records are pruned periodically.

The scanner writes one prediction per scanned asset and run (~16 k rows a day with the default background scan), so
its resolved rows are kept for a month only: totals, hit rates, calibration and the daily charts come from the
`prediction_daily` aggregates (app.sim.evaluator), which are kept much longer. Scan results are kept for the latest
runs only, and NEXUS memories of symbols nobody has analysed for months are dropped (re-learned from history if the
symbol is opened again), together with the semáforo vetoes and bar-close gate decisions of that symbol/timeframe.
Finished demo orders (filled, cancelled, expired) are kept for a year; pending ones are never touched."""
from __future__ import annotations

import asyncio
from typing import Any

from app import config
from app.util import now_ms

DAY = 86400
# Candle retention per timeframe (days). Higher timeframes are kept forever.
CANDLE_RETENTION_DAYS = {"1m": 10, "3m": 30, "5m": 45, "15m": 120, "30m": 180, "1h": 730, "2h": 1095}
SIM_CANDLE_DAYS = 7  # SIM:* bars are regenerated on demand
EVENT_DAYS = 60
RESOLVED_DAYS = 365
SCANNER_RESOLVED_DAYS = 30  # resolved scanner predictions (their daily aggregates stay)
DAILY_AGG_DAYS = 730  # prediction_daily rows
SCAN_DAYS = 45  # scan_runs (one small row per run)
SCAN_RESULT_RUNS = 100  # scan_results are kept for the newest runs only (≈ 1 day of background scans)
NEXUS_MODEL_DAYS = 90  # model_state kind='nexus' not updated for this long
# model_state kinds kept per NEXUS memory (key "SYMBOL|tf", app.engine.service VETO_KIND / GATE_KIND): the entries the
# semáforo vetoed and the bar-close gate decisions that every later run replays at their bars, so the history shown
# does not repaint. Their own updated_at only moves when a new one is recorded, so a symbol watched for months can
# have an old row that is still in use: a row goes only when it is NEXUS_MODEL_DAYS old AND no 'nexus' memory of the
# same key is left (nobody analysed that symbol/timeframe for that long, or its memory was reset and never rebuilt).
# The next analysis then starts cold from history anyway.
NEXUS_DECISION_KINDS = ("nexus_veto", "nexus_gate")
PAPER_ORDER_DAYS = 365  # demo orders no longer pending (filled / cancelled / expired), by their last update


class Maintenance:
    def __init__(self, rt: Any):
        self.rt = rt
        self._task: asyncio.Task | None = None
        self.last_run: int | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        if config.BACKGROUND:
            self._task = asyncio.create_task(self._loop(), name="maintenance")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def status(self) -> dict:
        return {"running": self._task is not None, "last_run": self.last_run, "error": self.last_error}

    async def _loop(self) -> None:
        await asyncio.sleep(600)
        while True:
            try:
                await self.run_once()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                if self.rt.bus is not None:
                    self.rt.bus.emit("warn", "system", f"Mantenimiento de la base de datos falló: {exc}")
            await asyncio.sleep(6 * 3600)

    async def run_once(self, now: int | None = None) -> None:
        db = self.rt.db
        ms = int(now) if now is not None else now_ms()
        s = ms // 1000
        for tf, days in CANDLE_RETENTION_DAYS.items():
            await db.execute("DELETE FROM candles WHERE tf = ? AND time < ?", (tf, s - days * DAY))
        await db.execute("DELETE FROM candles WHERE symbol LIKE 'SIM:%' AND time < ?", (s - SIM_CANDLE_DAYS * DAY,))
        await db.execute("DELETE FROM events WHERE ts < ?", (ms - EVENT_DAYS * DAY * 1000,))
        await self._prune_predictions(ms)
        await db.execute("DELETE FROM gate_log WHERE resolved = 1 AND created_at < ?", (ms - RESOLVED_DAYS * DAY * 1000,))
        old_runs = "SELECT id FROM scan_runs WHERE started_at < ?"
        cutoff = ms - SCAN_DAYS * DAY * 1000
        await db.execute(f"DELETE FROM scan_results WHERE run_id IN ({old_runs})", (cutoff,))
        await db.execute("DELETE FROM scan_runs WHERE started_at < ?", (cutoff,))
        keep_from = await db.fetchval(
            "SELECT id FROM scan_runs ORDER BY id DESC LIMIT 1 OFFSET ?", (SCAN_RESULT_RUNS - 1,)
        )
        if keep_from is not None:  # results of older runs (the run rows themselves stay, with their summary)
            await db.execute("DELETE FROM scan_results WHERE run_id < ?", (int(keep_from),))
        model_cutoff = ms - NEXUS_MODEL_DAYS * DAY * 1000
        await db.execute("DELETE FROM model_state WHERE kind = 'nexus' AND updated_at < ?", (model_cutoff,))
        kinds = ", ".join(f"'{kind}'" for kind in NEXUS_DECISION_KINDS)
        await db.execute(  # after the memories above: the decisions of a memory dropped right now go with it
            f"DELETE FROM model_state WHERE kind IN ({kinds}) AND updated_at < ? AND NOT EXISTS "
            "(SELECT 1 FROM model_state m WHERE m.kind = 'nexus' AND m.key = model_state.key)",
            (model_cutoff,),
        )
        await db.execute("DELETE FROM paper_orders WHERE status <> 'pending' AND updated_at < ?",
                         (ms - PAPER_ORDER_DAYS * DAY * 1000,))
        self.last_run = ms
        if self.rt.bus is not None:
            self.rt.bus.emit("debug", "system", "Mantenimiento de datos completado (retención aplicada)")

    async def _prune_predictions(self, ms: int) -> None:
        """Resolved predictions: a year in general, a month for the scanner's high-volume rows. The month rule only
        runs once the daily aggregates exist (they keep the history of totals, hit rates and charts)."""
        db = self.rt.db
        try:
            from app.sim.evaluator import ensure_daily_aggregates

            await ensure_daily_aggregates(db)
            aggregated = True
        except Exception as exc:  # evaluator module missing or failing: keep the rows (only the yearly rule applies)
            aggregated = False
            if self.rt.bus is not None:
                self.rt.bus.emit("warn", "system", f"Retención de predicciones del escáner aplazada: {exc}")
        await db.execute("DELETE FROM predictions WHERE resolved = 1 AND created_at < ?",
                         (ms - RESOLVED_DAYS * DAY * 1000,))
        if not aggregated:
            return
        await db.execute("DELETE FROM predictions WHERE source = 'scanner' AND resolved = 1 AND created_at < ?",
                         (ms - SCANNER_RESOLVED_DAYS * DAY * 1000,))
        await db.execute("DELETE FROM prediction_daily WHERE day < ?", (ms // 1000 - DAILY_AGG_DAYS * DAY,))
