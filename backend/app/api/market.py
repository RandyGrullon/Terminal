"""Market REST API (docs/ARCHITECTURE.md §6.2). Shapes follow frontend/src/api/types.ts; errors are Spanish.

GET    /api/markets                                    -> {markets: MarketInfo[]}
GET    /api/symbols/search?q=&market=&limit=30          -> {results: SearchResult[]}
GET    /api/universe?market=&offset=&limit=&sort=&order=&q=&type=  -> UniverseResponse
GET    /api/candles?symbol=&tf=&limit=                  -> CandlesResponse
GET    /api/quotes?symbols=A,B                          -> {quotes: {symbol: Quote}}
GET    /api/realtime                                    -> RealtimeStatus (types-realtime.ts)
GET    /api/symbol?symbol=                              -> SymbolDetail
GET    /api/watchlist                                   -> {items: WatchItem[]}
POST   /api/watchlist {symbol}                          -> {items}
DELETE /api/watchlist?symbol=                           -> {items}
"""
from __future__ import annotations

import math
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.data.base import ProviderError
from app.data.symbols import clean_symbol, valid_market
from app.runtime import rt
from app.util import clean_json, normalize_tf

router = APIRouter()

MAX_CANDLES = 5000
MAX_QUOTES = 200
_SORT_RE = re.compile(r"^[A-Za-z0-9_.|+\-\[\]]{1,48}$")
_TYPE_RE = re.compile(r"^[a-z_]{1,24}$")
_SYMBOL_HINT = "formato EXCHANGE:TICKER, por ejemplo BINANCE:BTCUSDT"


def _hub() -> Any:
    hub = rt.hub
    if hub is None:
        raise HTTPException(status_code=503, detail="El servicio de datos de mercado no está disponible")
    return hub


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _int_param(value: Any, name: str, default: int, lo: int, hi: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        x = float(str(value).strip())
    except (ValueError, OverflowError):
        x = math.nan
    if not math.isfinite(x):  # 'abc', 'nan', 'inf', '1e999'
        raise _bad(f"El parámetro '{name}' debe ser un número entero")
    return max(lo, min(hi, int(x)))


def _tf_param(tf: Any) -> str:
    try:
        return normalize_tf(tf)
    except ValueError as exc:
        raise _bad(str(exc)) from None


def _market_param(market: Any, default: str | None) -> str | None:
    value = str(market or "").strip().lower() or default
    if value is not None and not valid_market(value):
        raise _bad(f"Mercado no válido: {market!r}")
    return value


async def _symbol_param(text: Any, hub: Any) -> str:
    """Full symbols are validated; short names ('btc', 'aapl') are resolved through the hub."""
    raw = " ".join(str(text or "").split())
    if not raw:
        raise _bad(f"Falta el símbolo ({_SYMBOL_HINT})")
    if ":" in raw:
        try:
            return clean_symbol(raw)
        except ValueError as exc:
            raise _bad(str(exc)) from None
    resolved = await hub.resolve(raw)
    if not resolved:
        raise _bad(f"No se encontró el símbolo '{raw}' ({_SYMBOL_HINT})")
    return resolved


# ----------------------------------------------------------------------------- markets & symbols


@router.get("/api/markets")
async def get_markets() -> dict:
    return clean_json({"markets": await _hub().markets()})


@router.get("/api/symbols/search")
async def search_symbols(q: str = "", market: str | None = None, limit: str | None = None) -> dict:
    hub = _hub()
    n = _int_param(limit, "limit", 30, 1, 100)
    m = _market_param(market, None)
    try:
        results = await hub.search(q, market=m, limit=n)
    except ProviderError as exc:
        raise _bad(str(exc)) from None
    return clean_json({"results": results})


@router.get("/api/universe")
async def get_universe(
    market: str = "crypto",
    offset: str | None = None,
    limit: str | None = None,
    sort: str = "volume",
    order: str = "desc",
    q: str | None = None,
    type_: str | None = Query(None, alias="type"),
) -> dict:
    hub = _hub()
    m = _market_param(market, "crypto")
    sort_key = (sort or "volume").strip()
    if not _SORT_RE.match(sort_key):
        raise _bad(f"Columna de orden no válida: {sort!r}")
    direction = (order or "desc").strip().lower()
    if direction not in ("asc", "desc"):
        raise _bad("El orden debe ser 'asc' o 'desc'")
    kind = (type_ or "").strip().lower() or None
    if kind is not None and not _TYPE_RE.match(kind):
        raise _bad(f"Tipo de activo no válido: {type_!r}")
    try:
        data = await hub.universe(
            m,
            offset=_int_param(offset, "offset", 0, 0, 1_000_000),
            limit=_int_param(limit, "limit", 100, 1, 500),
            sort=sort_key,
            order=direction,
            q=q,
            type_=kind,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return clean_json(data)


@router.get("/api/symbol")
async def get_symbol(symbol: str = "") -> dict:
    hub = _hub()
    sym = await _symbol_param(symbol, hub)
    try:
        detail = await hub.symbol_detail(sym)
    except ProviderError as exc:
        raise _bad(str(exc)) from None
    return clean_json(detail)


# ----------------------------------------------------------------------------- candles & quotes


@router.get("/api/candles")
async def get_candles(symbol: str = "", tf: str = "1h", limit: str | None = None) -> dict:
    hub = _hub()
    tf_n = _tf_param(tf)
    n = _int_param(limit, "limit", 500, 1, MAX_CANDLES)
    sym = await _symbol_param(symbol, hub)
    try:
        cs = await hub.get_candles(sym, tf_n, n)
    except ProviderError as exc:
        raise _bad(str(exc)) from None
    out = {
        "symbol": cs.symbol,
        "tf": cs.tf,
        "provider": cs.provider,
        "history": cs.history,
        "note": cs.note,
        "candles": cs.to_list(),
    }
    if getattr(cs, "stale", False):
        out["stale"] = True  # newest bar far too old: not live data (the note says why), like Quote.stale
    return clean_json(out)


@router.get("/api/quotes")
async def get_quotes(symbols: str = "") -> dict:
    hub = _hub()
    items = [s.strip() for s in str(symbols or "").split(",") if s.strip()]
    if not items:
        raise _bad("Indica al menos un símbolo (symbols=BINANCE:BTCUSDT,NASDAQ:AAPL)")
    if len(items) > MAX_QUOTES:
        raise _bad(f"Máximo {MAX_QUOTES} símbolos por consulta")
    wanted: list[str] = []
    for item in items:
        try:
            sym = clean_symbol(item)
        except ValueError as exc:
            raise _bad(str(exc)) from None
        if sym not in wanted:
            wanted.append(sym)
    return clean_json({"quotes": await hub.quotes(wanted)})


@router.get("/api/realtime")
async def get_realtime() -> dict:
    """Real-time quotes of the API-key providers: what each plan serves live, credits used and symbols polled."""
    service = getattr(_hub(), "realtime", None)
    if service is None:
        raise HTTPException(status_code=503, detail="El servicio de cotizaciones en tiempo real no está disponible")
    return clean_json(service.status())


# ----------------------------------------------------------------------------- watchlist


@router.get("/api/watchlist")
async def get_watchlist() -> dict:
    return clean_json({"items": await _hub().watch_items()})


@router.post("/api/watchlist")
async def add_watchlist(payload: dict[str, Any] | None = Body(None)) -> dict:
    hub = _hub()
    sym = await _symbol_param((payload or {}).get("symbol"), hub)
    try:
        await hub.watch_add(sym)
    except ProviderError as exc:
        raise _bad(str(exc)) from None
    return clean_json({"items": await hub.watch_items()})


@router.delete("/api/watchlist")
async def remove_watchlist(symbol: str = "") -> dict:
    hub = _hub()
    sym = await _symbol_param(symbol, hub)
    try:
        await hub.watch_remove(sym)
    except ProviderError as exc:
        raise _bad(str(exc)) from None
    return clean_json({"items": await hub.watch_items()})
