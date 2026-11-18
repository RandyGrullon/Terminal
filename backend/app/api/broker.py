"""Practice-broker mirror API (app.broker: Alpaca PAPER and Binance's TESTNETS, never real money):
GET /api/broker/status -> BrokerStatus (Alpaca) · POST /api/broker/sync -> BrokerStatus after a reconcile pass ·
GET /api/broker/binance/status and POST /api/broker/binance/sync -> the same for the Binance testnets (provider
"binance_testnet"). A sync answers 400 {detail} in Spanish when the mirror is off, has no keys or runs offline. None
ever returns the keys."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.runtime import rt
from app.util import clean_json

router = APIRouter()


def _service(attr: str = "broker"):
    svc = getattr(rt, attr, None)
    if svc is None:
        raise HTTPException(status_code=503, detail="El espejo del bróker de práctica no está cargado")
    return svc


async def _sync(attr: str) -> dict:
    from app.broker.mirror import MirrorError

    try:
        return clean_json(await _service(attr).sync_now())
    except MirrorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/api/broker/status")
async def broker_status(refresh: bool = Query(False, description="Vuelve a pedir la cuenta a Alpaca")) -> dict:
    """BrokerStatus: settings (never the keys), the practice account, the mirrored positions and the stats."""
    return clean_json(await _service().overview(refresh=refresh))


@router.post("/api/broker/sync")
async def broker_sync() -> dict:
    """Reconcile now with NEXUS automática's positions and answer the fresh BrokerStatus."""
    return await _sync("broker")


@router.get("/api/broker/binance/status")
async def broker_binance_status(refresh: bool = Query(False, description="Vuelve a pedir la cuenta al testnet")) -> dict:
    """BrokerStatus of the Binance testnets mirror (spot crypto and perpetuals)."""
    return clean_json(await _service("broker_binance").overview(refresh=refresh))


@router.post("/api/broker/binance/sync")
async def broker_binance_sync() -> dict:
    """Reconcile the Binance testnets mirror now and answer the fresh BrokerStatus."""
    return await _sync("broker_binance")
