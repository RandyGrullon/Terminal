"""Practice-broker mirror API (app.broker, Alpaca PAPER only): GET /api/broker/status -> BrokerStatus ·
POST /api/broker/sync -> BrokerStatus after a reconcile pass (400 {detail} in Spanish when it is off, has no keys
or runs offline). Neither ever returns the keys."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.runtime import rt
from app.util import clean_json

router = APIRouter()


def _service():
    svc = getattr(rt, "broker", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="El espejo del bróker de práctica no está cargado")
    return svc


@router.get("/api/broker/status")
async def broker_status(refresh: bool = Query(False, description="Vuelve a pedir la cuenta a Alpaca")) -> dict:
    """BrokerStatus: settings (never the keys), the practice account, the mirrored positions and the stats."""
    return clean_json(await _service().overview(refresh=refresh))


@router.post("/api/broker/sync")
async def broker_sync() -> dict:
    """Reconcile now with NEXUS automática's positions and answer the fresh BrokerStatus."""
    from app.broker.mirror import MirrorError

    try:
        return clean_json(await _service().sync_now())
    except MirrorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
