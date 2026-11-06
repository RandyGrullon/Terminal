"""Trading costs by asset class (commission + spread, % per side), shared by NEXUS, the backtester and paper.

A single crypto-sized fee (0.1 %/side) is several times the typical cost of forex, index or futures trading and
would make the Trade Gate veto every low-volatility market, so the fee is resolved from the symbol's asset class:
``risk.fee_pct_by_class[class]`` when configured, otherwise ``risk.fee_pct`` (crypto, SIM:* and unknown classes).
"""
from __future__ import annotations

from typing import Any

from app.config import DEFAULT_SETTINGS
from app.data.symbols import asset_class_of, is_sim


def asset_class(symbol: str) -> str:
    """crypto | stock | fund | forex | commodity | index | futures | cfd | bond | option | other (SIM:* counts as
    crypto; option = a theoretical stock option of NEXUS's own account, OPT:*, see app.engine.options)."""
    sym = (symbol or "").strip().upper()
    if not sym or is_sim(sym):
        return "crypto"
    if sym.startswith("OPT:"):
        return "option"
    exchange, _, ticker = sym.partition(":")
    if not ticker:
        exchange, ticker = "", exchange
    return asset_class_of(exchange, ticker)


def fee_pct_for(symbol: str, settings: dict[str, Any] | None = None) -> float:
    """Fee in % per side for ``symbol`` with the given settings (DEFAULT_SETTINGS when None)."""
    s = settings if settings is not None else DEFAULT_SETTINGS
    try:
        base = float(s.get("risk.fee_pct", DEFAULT_SETTINGS["risk.fee_pct"]))
    except (TypeError, ValueError):
        base = float(DEFAULT_SETTINGS["risk.fee_pct"])
    by_class = s.get("risk.fee_pct_by_class")
    if not isinstance(by_class, dict):
        return base
    value = by_class.get(asset_class(symbol))
    try:
        return base if value is None else max(0.0, float(value))
    except (TypeError, ValueError):
        return base
