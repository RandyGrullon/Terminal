"""System endpoints: health, settings, events, terminal, database."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.config import VERSION
from app.runtime import rt
from app.terminal import execute, list_commands
from app.util import clean_json, now_ms

router = APIRouter()


class TerminalRequest(BaseModel):
    text: str


def _service_status(svc: Any) -> dict:
    try:
        return clean_json(svc.status())
    except Exception as exc:
        return {"running": False, "error": str(exc)}


@router.get("/api/health")
async def health() -> dict:
    services = {attr: _service_status(svc) for attr, svc in rt.services}
    if rt.ws is not None:
        services["ws"] = rt.ws.status()
    return clean_json(
        {
            "ok": True,
            "version": VERSION,
            "uptime_s": int(time.time() - rt.started_at),
            "server_time": now_ms(),
            "db": {"path": str(rt.db.path), "size_bytes": rt.db.size_bytes()},
            "services": services,
            "load_errors": rt.load_errors,
        }
    )


@router.get("/api/settings")
async def get_settings() -> dict:
    return {"settings": clean_json(rt.settings.all())}


@router.put("/api/settings")
async def put_settings(values: dict[str, Any] = Body(...)) -> dict:
    try:
        await rt.settings.update(values)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc).strip("'\"")) from None
    changed = ", ".join(values)
    rt.bus.emit("info", "system", f"Ajustes actualizados: {changed}")
    return {"settings": clean_json(rt.settings.all())}


@router.get("/api/events")
async def get_events(limit: int = 300, level: str | None = None, source: str | None = None) -> dict:
    return {"events": rt.bus.recent(min(max(limit, 1), 3000), level=level, source=source)}


@router.get("/api/terminal/commands")
async def terminal_commands() -> dict:
    return {"commands": list_commands()}


@router.post("/api/terminal")
async def terminal_exec(req: TerminalRequest) -> dict:
    return await execute(req.text)


@router.get("/api/db/stats")
async def db_stats() -> dict:
    return {"path": str(rt.db.path), "size_bytes": rt.db.size_bytes(), "tables": await rt.db.table_counts()}


@router.post("/api/db/vacuum")
async def db_vacuum() -> dict:
    await rt.db.vacuum()
    rt.bus.emit("info", "system", "Base de datos compactada")
    return {"ok": True, "size_bytes": rt.db.size_bytes()}
