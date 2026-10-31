"""REST endpoints of the alerts feature (alerts outside the app: Telegram, email).

GET  /api/alerts/status  -> AlertsStatus (channels configured / active, filters, counters, recent alerts, last test;
                            never a secret)
POST /api/alerts/test    {channel?: "telegram" | "email"} -> AlertsTestResponse (per-channel result or Spanish error).
                         JSON only (Content-Type: application/json, `{}` is enough): a page of another site cannot send
                         that without a CORS preflight, so it cannot make this server message the user (CSRF); a
                         bodyless or form POST gets 415.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from app.runtime import rt
from app.util import clean_json

router = APIRouter()


def _json_only(request: Request) -> None:
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json" and not (ctype.startswith("application/") and ctype.endswith("+json")):
        raise HTTPException(status_code=415, detail="Envía la petición en JSON (Content-Type: application/json), por "
                                                    "ejemplo {} o {\"channel\": \"telegram\"}.")


def _service():
    if rt.alerts is None or not hasattr(rt.alerts, "full_status"):
        raise HTTPException(status_code=503, detail="Las alertas externas no están disponibles en este momento")
    return rt.alerts


@router.get("/api/alerts/status")
async def get_alerts_status() -> dict:
    return clean_json(_service().full_status())


@router.post("/api/alerts/test")
async def post_alerts_test(request: Request, body: Any = Body(None)) -> dict:
    from app.alerts.service import AlertBusy

    svc = _service()
    _json_only(request)
    channel = None
    if body is not None:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Cuerpo no válido: usa {} o {\"channel\": \"telegram\"}")
        raw = body.get("channel")
        if raw is not None and not isinstance(raw, str):
            raise HTTPException(status_code=400, detail="'channel' debe ser texto (telegram o email)")
        channel = (raw or "").strip().lower() or None
    try:
        result = await svc.send_test(channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except AlertBusy as exc:
        raise HTTPException(status_code=getattr(exc, "status", 429), detail=str(exc)) from None
    return clean_json(result)
