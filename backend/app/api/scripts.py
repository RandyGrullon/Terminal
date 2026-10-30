"""REST API for PyChart scripts (contract §6.5).

    GET    /api/scripts               -> {scripts: ScriptMeta[]}
    GET    /api/scripts/reference     -> ScriptReference
    POST   /api/scripts/compile       {code} -> CompileResult
    POST   /api/scripts/run           {code, symbol, tf, limit=1000, inputs={}} -> ScriptRunResult
    POST   /api/scripts               {name, code, description?} -> Script
    GET    /api/scripts/{id}          -> Script
    PUT    /api/scripts/{id}          {name?, code?, description?} -> Script
    DELETE /api/scripts/{id}          -> {ok: true}

Errors are `{"detail": "<español>"}`: 400 invalid input / no market data, 403 examples are read-only, 404 unknown
script, 409 duplicate name, 503 when the PyChart compiler or the scripts service is not available.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.runtime import rt
from app.scripting.reference import REFERENCE
from app.scripting.service import ScriptServiceError

router = APIRouter()


# ----------------------------------------------------------------------------- request models


class ScriptCreateRequest(BaseModel):
    name: str | None = Field(default=None, description="Nombre único del script")
    code: str | None = Field(default=None, description="Código Python (API PyChart)")
    description: str | None = Field(default="", description="Descripción breve")


class ScriptUpdateRequest(BaseModel):
    name: str | None = None
    code: str | None = None
    description: str | None = None


class CompileRequest(BaseModel):
    code: str | None = Field(default=None, description="Código a validar (no se ejecuta)")


class RunRequest(BaseModel):
    code: str | None = Field(default=None, description="Código a ejecutar en el sandbox")
    symbol: str | None = Field(default=None, description="Símbolo, p. ej. BINANCE:BTCUSDT o SIM:NEXUS")
    tf: str | None = Field(default="1h", description="Temporalidad (1m … 1w)")
    limit: int | None = Field(default=1000, description="Número de velas (50 … 5000)")
    inputs: dict[str, Any] | None = Field(default=None, description="Valores de las entradas {nombre: valor}")


# ----------------------------------------------------------------------------- helpers


def _service() -> Any:
    svc = getattr(rt, "scripts", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="El servicio de scripts PyChart no está disponible (el módulo no se cargó)")
    return svc


async def _call(coro: Any) -> Any:
    try:
        return await coro
    except ScriptServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


# ----------------------------------------------------------------------------- routes (static paths first)


@router.get("/api/scripts")
async def list_scripts() -> dict:
    return {"scripts": await _call(_service().list_scripts())}


@router.get("/api/scripts/reference")
async def script_reference() -> dict:
    return REFERENCE


@router.post("/api/scripts/compile")
async def compile_script(body: CompileRequest) -> dict:
    return await _call(_service().compile(body.code or ""))


@router.post("/api/scripts/run")
async def run_script(body: RunRequest) -> dict:
    return await _call(
        _service().run(body.code, body.symbol, body.tf or "1h", body.limit if body.limit is not None else 1000, body.inputs or {})
    )


@router.post("/api/scripts")
async def create_script(body: ScriptCreateRequest) -> dict:
    return await _call(_service().create(body.name, body.code, body.description or ""))


@router.get("/api/scripts/{script_id}")
async def get_script(script_id: str) -> dict:
    return await _call(_service().get(script_id))


@router.put("/api/scripts/{script_id}")
async def update_script(script_id: str, body: ScriptUpdateRequest) -> dict:
    return await _call(_service().update(script_id, name=body.name, code=body.code, description=body.description))


@router.delete("/api/scripts/{script_id}")
async def delete_script(script_id: str) -> dict:
    return await _call(_service().delete(script_id))
