"""Practice-broker mirror API (app.broker: Alpaca PAPER and Binance's TESTNETS, never real money):
GET /api/broker/status -> BrokerStatus (Alpaca) · POST /api/broker/sync -> BrokerStatus after a reconcile pass ·
GET /api/broker/binance/status and POST /api/broker/binance/sync -> the same for the Binance testnets (provider
"binance_testnet") · GET /api/broker/mt5/status and POST /api/broker/mt5/sync -> the MetaTrader 5 mirror (provider
"mt5": the account open in MT5, demo or a real one the user authorized by its number). A sync answers 400 {detail} in Spanish when the mirror is off, has no keys or runs offline. None
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


@router.get("/api/broker/mt5/status")
async def broker_mt5_status(refresh: bool = Query(False, description="Vuelve a leer la cuenta de MT5")) -> dict:
    """BrokerStatus of the MetaTrader 5 mirror (the account open in the MT5 terminal: demo, or an authorized real one)."""
    return clean_json(await _service("broker_mt5").overview(refresh=refresh))


@router.post("/api/broker/mt5/sync")
async def broker_mt5_sync() -> dict:
    """Reconcile the MetaTrader 5 mirror now and answer the fresh BrokerStatus."""
    return await _sync("broker_mt5")


@router.post("/api/broker/binance/sync")
async def broker_binance_sync() -> dict:
    """Reconcile the Binance testnets mirror now and answer the fresh BrokerStatus."""
    return await _sync("broker_binance")
