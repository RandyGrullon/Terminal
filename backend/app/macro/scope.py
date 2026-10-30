"""Semáforo scope of a symbol: which calendar currencies and which metrics matter for it.

crypto → USD events + crypto metrics · forex EURUSD → EUR + USD events · US stocks / indices / commodities → USD ·
other assets → their own currency + USD · SIM:* → simulated market (always green, no macro events).
"""
from __future__ import annotations

from typing import Any

from app.data import symbols as S
from app.util import parse_symbol

# Scanner market -> trading currency (fallback when SymbolInfo has no currency).
MARKET_CURRENCY: dict[str, str] = {
    "america": "USD", "mexico": "MXN", "spain": "EUR", "brazil": "BRL", "argentina": "ARS", "colombia": "COP",
    "chile": "CLP", "peru": "PEN", "uk": "GBP", "germany": "EUR", "france": "EUR", "italy": "EUR",
    "netherlands": "EUR", "canada": "CAD", "japan": "JPY", "india": "INR", "hongkong": "HKD",
    "australia": "AUD", "china": "CNY", "korea": "KRW", "switzerland": "CHF",
}
_STABLES = {"USDT", "USDC", "FDUSD", "USDE", "DAI", "TUSD", "BUSD"}


def _scope(sid: str, kind: str, symbol: str | None, currencies: list[str], crypto: bool = False,
           sim: bool = False) -> dict:
    seen: list[str] = []
    for c in currencies:
        c = str(c or "").upper()
        if c and c not in seen:
            seen.append(c)
    return {"id": sid, "kind": kind, "symbol": symbol, "currencies": seen, "crypto": crypto, "sim": sim}


def global_scope() -> dict:
    """Header semáforo: currencies come from the setting macro.currencies (resolved in conditions.assess)."""
    return _scope("global", "global", None, [])


def _with_usd(currency: str) -> list[str]:
    cur = (currency or "").upper()
    if not cur or cur in _STABLES or cur == "USD":
        return ["USD"]
    return [cur, "USD"]


def scope_for_symbol(symbol: str | None = None, info: dict | None = None) -> dict:
    """Scope dict {id, kind, symbol, currencies, crypto, sim}; `info` = SymbolInfo when the hub knows the symbol."""
    if not symbol or not str(symbol).strip():
        return global_scope()
    sym = "".join(str(symbol).split()).upper()
    if S.is_sim(sym):
        return _scope("sim", "sim", sym, [], sim=True)
    exchange, ticker = parse_symbol(sym)
    info = info or {}
    tv_type = str(info.get("type") or "")
    market = str(info.get("market") or "")
    currency = str(info.get("currency") or "").upper()
    aclass = S.asset_class_of(exchange, ticker, tv_type, market)
    if aclass == "crypto":
        return _scope("crypto", "crypto", sym, ["USD"], crypto=True)
    plain, _derivative = S.strip_derivative(ticker)
    pair = S.fx_pair(plain)
    if aclass == "forex" and pair:
        base, quote = pair
        return _scope(f"forex:{base}/{quote}", "forex", sym, [base, quote])
    if aclass == "commodity" or (pair and pair[0] in S.METAL_CODES):
        quote = pair[1] if pair else currency
        return _scope("commodities", "commodities", sym, ["USD"] + _with_usd(quote))
    if aclass in ("stock", "fund") and exchange in S.US_STOCK_EXCHANGES:
        return _scope("us_equities", "us_equities", sym, ["USD"])
    if aclass in ("index", "futures", "cfd", "bond"):
        cur = currency or "USD"
        sid = {"index": "indices", "futures": "futures", "cfd": "cfd", "bond": "bonds"}[aclass]
        if cur != "USD" and cur not in _STABLES:
            sid = f"{sid}:{cur}"
        return _scope(sid, aclass, sym, _with_usd(cur))
    mkt = S.market_for(exchange, ticker, tv_type)
    if mkt == "global" and market:
        mkt = market
    cur = currency or MARKET_CURRENCY.get(mkt, "")
    if not cur or cur == "USD":
        return _scope("us_equities" if exchange in S.US_STOCK_EXCHANGES else "stocks:USD", "stocks", sym, ["USD"])
    return _scope(f"stocks:{cur}", "stocks", sym, _with_usd(cur))


def describe(scope: dict) -> str:
    """Short Spanish description for the terminal ('Forex EUR/USD: eventos EUR y USD')."""
    if scope.get("sim"):
        return "Mercado simulado"
    curs = scope.get("currencies") or []
    events = f"eventos {' y '.join(curs)}" if curs else "eventos de las divisas configuradas"
    names: dict[str, Any] = {
        "global": "Mercado global", "crypto": "Cripto", "forex": "Forex", "commodities": "Materias primas",
        "us_equities": "Acciones de EE. UU.", "index": "Índices", "futures": "Futuros", "cfd": "CFD",
        "bond": "Bonos", "stocks": "Acciones",
    }
    name = names.get(scope.get("kind"), "Mercado")
    if scope.get("kind") == "forex":
        name = f"Forex {scope['id'].split(':', 1)[1]}"
    return f"{name}: {events}"
