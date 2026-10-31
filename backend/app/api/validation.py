"""REST endpoints of the walk-forward validation (app.validation, types in frontend/src/api/types-validation.ts).

POST /api/validation/run {symbols?, tfs?, bars?} -> ValidationStatus   (400 invalid input, 409 a job is running)
GET  /api/validation/status                     -> ValidationStatus
GET  /api/validation/latest                     -> {report: ValidationReport | null}
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.runtime import rt
from app.util import clean_json
from app.validation.runner import ValidationInputError
from app.validation.service import ValidationBusy

router = APIRouter()


class ValidationRunRequest(BaseModel):
    symbols: list[str] | str | None = None
    tfs: list[str] | str | None = None
    bars: int | None = None


def _service() -> Any:
    svc = getattr(rt, "validation", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="La validación no está disponible (el módulo no se cargó)")
    return svc


@router.post("/api/validation/run")
async def validation_run(req: ValidationRunRequest) -> dict:
    svc = _service()
    try:
        return await svc.run(req.symbols, req.tfs, req.bars)
    except ValidationInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ValidationBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.get("/api/validation/status")
async def validation_status() -> dict:
    return _service().status()


@router.get("/api/validation/latest")
async def validation_latest() -> dict:
    report = await _service().latest()
    return clean_json({"report": report})
