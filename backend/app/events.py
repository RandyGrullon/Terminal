"""Event bus: every notable thing that happens is an event. Events are kept in memory, persisted to SQLite,
printed to the server console and pushed to the browser terminal (WS topic "events")."""
from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from datetime import datetime
from typing import Any, Callable

from app.runtime import rt
from app.util import clean_json, now_ms

LEVELS = ("debug", "info", "success", "warn", "error", "signal", "trade", "learn", "scan", "system")

_ANSI = {
    "debug": "\033[90m",
    "info": "\033[37m",
    "success": "\033[92m",
    "warn": "\033[93m",
    "error": "\033[91m",
    "signal": "\033[96m",
    "trade": "\033[95m",
    "learn": "\033[94m",
    "scan": "\033[36m",
    "system": "\033[97m",
}
_RESET = "\033[0m"
_KEEP_EVENTS = 50_000


class EventBus:
    def __init__(self, maxlen: int = 3000, console: bool = True, console_min_level: str = "info"):
        self._buffer: deque[dict] = deque(maxlen=maxlen)
        self._pending: list[dict] = []
        self._next_id = 1
        self._db: Any = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self._flushes = 0
        self.console = console
        self.console_debug = console_min_level == "debug"

    async def start(self, db: Any) -> None:
        self._db = db
        self._loop = asyncio.get_running_loop()
        max_id = await db.fetchval("SELECT MAX(id) FROM events", default=0)
        self._next_id = int(max_id) + 1
        rows = await db.fetchall("SELECT id, ts, level, source, message, data FROM events ORDER BY id DESC LIMIT 300")
        for row in reversed(rows):
            data = None
            if row.get("data"):
                try:
                    data = json.loads(row["data"])
                except ValueError:
                    data = None
            self._buffer.append({**row, "data": data})
        self._task = asyncio.create_task(self._persist_loop(), name="eventbus-persist")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self.flush()

    # ------------------------------------------------------------------ emit

    def emit(self, level: str, source: str, message: str, data: Any = None) -> dict:
        if level not in LEVELS:
            level = "info"
        ev = {
            "id": self._next_id,
            "ts": now_ms(),
            "level": level,
            "source": str(source),
            "message": str(message),
            "data": clean_json(data) if data is not None else None,
        }
        self._next_id += 1
        self._buffer.append(ev)
        self._pending.append(ev)
        if self.console and (level != "debug" or self.console_debug):
            self._print(ev)
        if rt.ws is not None:
            try:
                rt.ws.publish("events", {"type": "event", "event": ev})
            except Exception:
                pass
        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:
                pass
        return ev

    def emit_threadsafe(self, level: str, source: str, message: str, data: Any = None) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.emit, level, source, message, data)

    def add_listener(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def recent(self, limit: int = 200, level: str | None = None, source: str | None = None) -> list[dict]:
        items = list(self._buffer)
        if level:
            wanted = set(level.split(","))
            items = [e for e in items if e["level"] in wanted]
        if source:
            wanted_src = set(source.split(","))
            items = [e for e in items if e["source"] in wanted_src]
        return items[-max(1, int(limit)) :]

    # ------------------------------------------------------------------ persistence

    async def flush(self) -> None:
        if not self._pending or self._db is None or self._db.conn is None:
            return
        batch, self._pending = self._pending, []
        try:
            await self._db.executemany(
                "INSERT OR IGNORE INTO events(id, ts, level, source, message, data) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        e["id"],
                        e["ts"],
                        e["level"],
                        e["source"],
                        e["message"],
                        json.dumps(e["data"], ensure_ascii=False) if e["data"] is not None else None,
                    )
                    for e in batch
                ],
            )
        except Exception as exc:  # never lose the app because of logging
            self._print({"ts": now_ms(), "level": "error", "source": "events", "message": f"No se pudieron guardar eventos: {exc}"})

    async def _persist_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self.flush()
            self._flushes += 1
            if self._flushes % 600 == 0 and self._db is not None:
                try:
                    await self._db.execute("DELETE FROM events WHERE id < ?", (self._next_id - _KEEP_EVENTS,))
                except Exception:
                    pass

    # ------------------------------------------------------------------ console

    @staticmethod
    def _print(ev: dict) -> None:
        stamp = datetime.fromtimestamp(ev["ts"] / 1000).strftime("%H:%M:%S")
        color = _ANSI.get(ev["level"], "")
        line = f"{color}{stamp} {ev['level'].upper():<7} [{ev['source']}] {ev['message']}{_RESET}"
        try:
            print(line, flush=True)
        except Exception:
            try:
                sys.stdout.write(line.encode("ascii", "replace").decode("ascii") + "\n")
                sys.stdout.flush()
            except Exception:
                pass
