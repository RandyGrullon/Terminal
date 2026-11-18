"""Strategy research API (app.sim.research): GET /api/research -> {busy, job, reports} · POST /api/research/run
{symbol, tf, bars?, nexus?} · POST /api/research/import/binance {symbols, months?, tfs?} · POST
/api/research/import/file {file (a name in data/import), symbol, tz_offset_h?} · GET /api/research/imported?symbol=.
A job runs in the background (one at a time): the POSTs answer at once with {busy, job}; 400 {detail} in Spanish when
another job runs, a file is outside data/import or a value is not valid."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.runtime import rt
from app.util import clean_json

router = APIRouter()


class ResearchRun(BaseModel):
    symbol: str
    tf: str = "1h"
    bars: int | None = None
    nexus: bool = True


class ImportBinance(BaseModel):
    symbols: list[str]
    months: int = 36
    tfs: list[str] | None = None


class ImportFile(BaseModel):
    file: str
    symbol: str
    tz_offset_h: float = 0.0


def _service():
    svc = getattr(rt, "research", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="El módulo de investigación no está cargado")
    return svc


def _errors():
    from app.data.importer import ImportError_
    from app.sim.research import ResearchError

    return (ResearchError, ImportError_, ValueError)


@router.get("/api/research")
async def research_status() -> dict:
    svc = _service()
    return clean_json({**svc.snapshot(), "reports": await svc.reports()})


@router.post("/api/research/run")
async def research_run(req: ResearchRun) -> dict:
    svc = _service()
    from app.sim.research import RESEARCH_BARS

    try:
        return clean_json(svc.start_research(req.symbol, req.tf, bars=req.bars or RESEARCH_BARS, nexus=req.nexus))
    except _errors() as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/api/research/import/binance")
async def research_import_binance(req: ImportBinance) -> dict:
    try:
        return clean_json(_service().start_import_binance(req.symbols, months=req.months, tfs=req.tfs))
    except _errors() as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/api/research/import/file")
async def research_import_file(req: ImportFile) -> dict:
    try:
        return clean_json(_service().start_import_csv(req.file, req.symbol, tz_offset_h=req.tz_offset_h))
    except _errors() as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/api/research/imported")
async def research_imported(symbol: str = Query(..., description="Símbolo, por ejemplo BINANCE:BTCUSDT")) -> dict:
    return clean_json({"symbol": symbol.strip().upper(), "tfs": await _service().imported(symbol)})
