"""MT5-style lots («lotaje»): how many units of an asset one lot holds, the way most MetaTrader 5 brokers define it.

NEXUS's own demo account can size its trades in lots (setting `autopilot.lots`): the size its risk per trade asks for is
rounded DOWN to steps of 0.01 lot, and a trade whose size is under 0.01 lot opens with 0.01 lot only when that minimum
lot risks at most `autopilot.lot_max_risk_pct` % of its equity at the stop (otherwise it is skipped, never forced).

Contract sizes are the usual ones (they vary by broker; check your platform's contract specification): gold 100 troy
ounces (PAX Gold, 1 token = 1 ounce, and Binance's XAUUSDT perpetual count as gold), silver 5,000 ounces, forex 100,000 units of the base currency,
crypto 1 coin, stocks and funds 1 share, anything else 1 unit. Index futures already trade in whole contracts and
options in contracts of 100 shares: they have no lot here.
"""
from __future__ import annotations

import math
from typing import Any

from app.data.symbols import fx_pair, split_pair, strip_derivative
from app.engine.costs import asset_class

LOT_STEP = 0.01  # the minimum lot and its step
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})  # 1 token = 1 troy ounce of gold
GOLD_TICKERS = frozenset({"GOLD", "XAU"})
SILVER_TICKERS = frozenset({"SILVER", "XAG"})
EPS = 1e-9


def lot_spec(symbol: str, instrument: Any = None) -> dict[str, Any] | None:
    """{size, unit}: units of the asset in 1 lot and their name ("oz", "BTC", "acción", "EUR", "unidad"), or None
    (an option or an index future: they trade in whole contracts)."""
    if isinstance(instrument, dict):
        kind = instrument.get("kind")
        if kind == "option" or (kind == "future" and not instrument.get("perpetual")):
            return None
    sym = (symbol or "").strip().upper()
    if not sym or sym.startswith("OPT:"):
        return None
    ticker = sym.partition(":")[2] or sym
    cls = asset_class(sym)
    if cls == "futures":
        return None
    base, _ = strip_derivative(ticker)
    fx = fx_pair(base)
    pair = split_pair(base)
    coin = pair[0] if pair else None
    if coin in GOLD_TOKENS or coin in GOLD_TICKERS or base in GOLD_TICKERS or (fx is not None and fx[0] == "XAU"):
        return {"size": 100.0, "unit": "oz"}
    if coin in SILVER_TICKERS or base in SILVER_TICKERS or (fx is not None and fx[0] == "XAG"):
        return {"size": 5000.0, "unit": "oz"}
    if fx is not None and cls in ("forex", "cfd", "other"):
        return {"size": 100000.0, "unit": fx[0]}
    if cls == "crypto":
        return {"size": 1.0, "unit": coin or base}
    if cls in ("stock", "fund"):
        return {"size": 1.0, "unit": "acción"}
    return {"size": 1.0, "unit": "unidad"}


def lot_info(symbol: str, qty: Any, instrument: Any = None) -> dict[str, Any] | None:
    """{lots, size, unit} of a position of `qty` units (lots = qty / size), or None without a lot (see lot_spec)."""
    spec = lot_spec(symbol, instrument)
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return None
    if spec is None or not math.isfinite(q) or q < 0:
        return None
    return {"lots": round(q / spec["size"], 6), "size": spec["size"], "unit": spec["unit"]}


def step_units(size: float, contract: float | None = None) -> float:
    """Units in one step of LOT_STEP lots; with `contract` (a perpetual's units per contract) a whole number of
    contracts, at least one."""
    step = float(size) * LOT_STEP
    if contract is not None and contract > 0:
        step = float(contract) * max(1, math.ceil(step / float(contract) - EPS))
    return step


def fit_steps(qty: float, step: float, risk_unit: float, max_risk: float) -> float | None:
    """`qty` rounded down to whole steps; under one step: one step (the minimum lot) when its money at the stop
    (step × risk_unit) is at most `max_risk`, else None (the trade does not fit)."""
    if step <= 0 or not math.isfinite(qty) or qty <= 0:
        return None
    n = math.floor(qty / step + EPS)
    if n >= 1:
        return round(n * step, 10)  # no binary noise (413.40000000000003)
    return step if step * max(risk_unit, 0.0) <= max_risk * (1.0 + EPS) else None


def unit_word(unit: str, qty: float) -> str:
    """'acción' → 'acciones' and 'unidad' → 'unidades' unless qty is 1; coins, currencies and 'oz' unchanged."""
    if abs(qty - 1.0) < EPS:
        return unit
    return {"acción": "acciones", "unidad": "unidades"}.get(unit, unit)
