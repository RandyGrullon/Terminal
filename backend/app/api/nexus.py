"""NEXUS, Trade Gate and indicator endpoints (§6.3)."""
from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.runtime import rt
from app.util import clean_json, normalize_tf

router = APIRouter()

INDICATOR_LIMIT_MAX = 5000


class ResetRequest(BaseModel):
    symbol: str
    tf: str = "1h"


class IndicatorItem(BaseModel):
    uid: str = ""
    id: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class ComputeRequest(BaseModel):
    symbol: str
    tf: str = "1h"
    limit: int = 1000
    items: list[IndicatorItem] = Field(default_factory=list)


def _service():
    if rt.nexus is None:
        raise HTTPException(status_code=503, detail="El motor NEXUS no está disponible")
    return rt.nexus


def _data_error() -> type[Exception]:
    try:
        from app.engine.service import NexusDataError
    except Exception:  # pragma: no cover - service module missing
        return RuntimeError
    return NexusDataError


async def _call(coro_fn, *args: Any, **kwargs: Any) -> Any:
    data_error = _data_error()
    try:
        return await coro_fn(*args, **kwargs)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except data_error as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="El análisis tardó demasiado; inténtalo de nuevo") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error interno de NEXUS: {type(exc).__name__}: {exc}") from None


def _symbol(symbol: str | None) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        raise HTTPException(status_code=400, detail="Falta el símbolo")
    return s


def _tf(tf: str | None) -> str:
    try:
        return normalize_tf(tf or "1h")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/api/nexus")
async def nexus(symbol: str, tf: str = "1h", limit: int | None = None) -> dict:
    svc = _service()
    return await _call(svc.analyze, _symbol(symbol), _tf(tf), limit)


@router.get("/api/nexus/model")
async def nexus_model(symbol: str, tf: str = "1h") -> dict:
    svc = _service()
    return await _call(svc.model, _symbol(symbol), _tf(tf))


@router.post("/api/nexus/reset")
async def nexus_reset(body: ResetRequest) -> dict:
    svc = _service()
    await _call(svc.reset, _symbol(body.symbol), _tf(body.tf))
    return {"ok": True}


@router.get("/api/gate/check")
async def gate_check(symbol: str, tf: str = "1h", direction: str = "long", entry: float | None = None,
                     sl: float | None = None, tp: float | None = None) -> dict:
    svc = _service()
    return await _call(svc.gate, _symbol(symbol), _tf(tf), direction, entry=entry, sl=sl, tp=tp)


# ----------------------------------------------------------------------------- indicators


def _indicators():
    try:
        from app.engine import indicators as lib
    except Exception:
        raise HTTPException(status_code=503, detail="Biblioteca de indicadores no disponible") from None
    for name in ("CATALOG", "compute_indicator", "IndicatorError"):
        if not hasattr(lib, name):
            raise HTTPException(status_code=503, detail="Biblioteca de indicadores no disponible")
    return lib


@router.get("/api/indicators/catalog")
async def indicators_catalog() -> dict:
    lib = _indicators()
    return clean_json({"indicators": lib.CATALOG})


def _json_series(arr: Any) -> list[float | None]:
    a = np.asarray(arr, dtype=np.float64).reshape(-1)
    return [v if v == v and v not in (float("inf"), float("-inf")) else None for v in a.tolist()]


def _compute_all(lib: Any, cs: Any, items: list[IndicatorItem]) -> list[dict]:
    results: list[dict] = []
    for item in items:
        try:
            if not item.id:
                raise lib.IndicatorError("Petición de indicador inválida: falta el id")
            arrays = lib.compute_indicator(item.id, cs, dict(item.params or {}))
            outputs = {str(k): _json_series(v) for k, v in (arrays or {}).items()}
            results.append({"uid": item.uid, "id": item.id, "outputs": outputs, "error": None})
        except lib.IndicatorError as exc:
            results.append({"uid": item.uid, "id": item.id, "outputs": {}, "error": str(exc)})
        except Exception as exc:  # one broken indicator never breaks the whole response
            results.append({"uid": item.uid, "id": item.id, "outputs": {},
                            "error": f"Error interno al calcular el indicador ({type(exc).__name__})"})
    return results


@router.post("/api/indicators/compute")
async def indicators_compute(body: ComputeRequest) -> dict:
    lib = _indicators()
    symbol, tf = _symbol(body.symbol), _tf(body.tf)
    if rt.hub is None:
        raise HTTPException(status_code=503, detail="Los datos de mercado no están disponibles")
    limit = max(1, min(int(body.limit or 1000), INDICATOR_LIMIT_MAX))
    try:
        cs = await rt.hub.get_candles(symbol, tf, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        if type(exc).__name__ == "ProviderError":
            raise HTTPException(status_code=400, detail=str(exc)) from None
        raise HTTPException(status_code=502, detail=f"No se pudieron obtener velas de {symbol}: {exc}") from None
    results = await asyncio.to_thread(_compute_all, lib, cs, list(body.items))
    return clean_json({"time": [int(t) for t in np.asarray(cs.time).tolist()], "results": results})
