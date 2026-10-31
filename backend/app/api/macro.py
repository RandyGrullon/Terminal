"""REST API of the semáforo de mercado: MarketConditions (global or per symbol) and the economic calendar."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from app.data.symbols import clean_symbol
from app.runtime import rt
from app.util import clean_json

router = APIRouter()

IMPACTS = ("high", "medium", "low", "holiday")
MAX_DAYS = 60
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _service():
    if rt.macro is None:
        raise HTTPException(status_code=503, detail="El semáforo de mercado no está disponible en este momento")
    return rt.macro


async def _symbol(text: str | None) -> str | None:
    raw = "".join(str(text or "").split())
    if not raw:
        return None
    if ":" in raw:
        try:
            return clean_symbol(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    if rt.hub is not None:
        try:
            resolved = await rt.hub.resolve(raw)
        except Exception:
            resolved = None
        if resolved:
            return resolved
    raise HTTPException(
        status_code=400,
        detail=f"Símbolo no válido: {text!r} (formato EXCHANGE:TICKER, por ejemplo BINANCE:BTCUSDT)",
    )


@router.get("/api/macro")
async def get_macro(symbol: str | None = None) -> dict:
    """MarketConditions: global semáforo, or scoped to `symbol` (crypto, forex pair, US stocks...)."""
    svc = _service()
    sym = await _symbol(symbol)
    return clean_json(await svc.conditions(sym))


@router.get("/api/macro/calendar")
async def get_macro_calendar(days: str = "7", impact: str = "high,medium", currency: str | None = None) -> dict:
    """Upcoming economic events: ?days=1..60 &impact=high,medium|all &currency=USD,EUR. Each event names its
    `source`; `coverage` (optional) tells how far the weekly feed reaches and the state of each official calendar."""
    svc = _service()
    try:
        n_days = int(str(days).strip())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Parámetro 'days' no válido: {days!r} (entero de 1 a {MAX_DAYS})") from None
    if not 1 <= n_days <= MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"Parámetro 'days' fuera de rango: usa un entero de 1 a {MAX_DAYS}")
    parts = [p.strip().lower() for p in str(impact or "").split(",") if p.strip()]
    impacts: tuple[str, ...] | None
    if not parts or "all" in parts:
        impacts = None
    else:
        bad = [p for p in parts if p not in IMPACTS]
        if bad:
            raise HTTPException(status_code=400,
                                detail=f"Impacto no válido: {', '.join(bad)} (usa high, medium, low, holiday o all)")
        impacts = tuple(dict.fromkeys(parts))
    currencies = None
    if currency and currency.strip():
        currencies = [c.strip().upper() for c in currency.split(",") if c.strip()]
        bad = [c for c in currencies if not _CURRENCY.match(c)]
        if bad:
            raise HTTPException(status_code=400,
                                detail=f"Divisa no válida: {', '.join(bad)} (códigos de 3 letras, por ejemplo USD,EUR)")
    events = await svc.calendar(days=n_days, impacts=impacts, currencies=currencies)
    out = {"events": clean_json(events)}
    coverage = getattr(svc, "coverage", None)
    if callable(coverage):
        out["coverage"] = clean_json(coverage())
    return out
