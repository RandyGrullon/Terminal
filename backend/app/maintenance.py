"""Data retention. Keeps the database lean (essential on Supabase's free 500 MB tier, nice to have locally):
fine-grained candles, old events and resolved learning records are pruned periodically."""
from __future__ import annotations

import asyncio
from typing import Any

from app import config
from app.util import now_ms, now_s

DAY = 86400
# Candle retention per timeframe (days). Higher timeframes are kept forever.
CANDLE_RETENTION_DAYS = {"1m": 10, "3m": 30, "5m": 45, "15m": 120, "30m": 180, "1h": 730, "2h": 1095}
SIM_CANDLE_DAYS = 7  # SIM:* bars are regenerated on demand
EVENT_DAYS = 60
RESOLVED_DAYS = 365
SCAN_DAYS = 45


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

    async def run_once(self) -> None:
        db = self.rt.db
        s, ms = now_s(), now_ms()
        for tf, days in CANDLE_RETENTION_DAYS.items():
            await db.execute("DELETE FROM candles WHERE tf = ? AND time < ?", (tf, s - days * DAY))
        await db.execute("DELETE FROM candles WHERE symbol LIKE 'SIM:%' AND time < ?", (s - SIM_CANDLE_DAYS * DAY,))
        await db.execute("DELETE FROM events WHERE ts < ?", (ms - EVENT_DAYS * DAY * 1000,))
        await db.execute("DELETE FROM predictions WHERE resolved = 1 AND created_at < ?", (ms - RESOLVED_DAYS * DAY * 1000,))
        await db.execute("DELETE FROM gate_log WHERE resolved = 1 AND created_at < ?", (ms - RESOLVED_DAYS * DAY * 1000,))
        old_runs = "SELECT id FROM scan_runs WHERE started_at < ?"
        cutoff = ms - SCAN_DAYS * DAY * 1000
        await db.execute(f"DELETE FROM scan_results WHERE run_id IN ({old_runs})", (cutoff,))
        await db.execute("DELETE FROM scan_runs WHERE started_at < ?", (cutoff,))
        self.last_run = ms
        if self.rt.bus is not None:
            self.rt.bus.emit("debug", "system", "Mantenimiento de datos completado (retención aplicada)")
