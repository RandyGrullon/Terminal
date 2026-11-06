"""Index futures NEXUS's own demo account can trade, with their contract terms (CME / CBOT equity index futures).

A position in a future holds `contracts × multiplier` units of the index (P&L = points × multiplier per contract), so
the paper engine's accounting needs no other change. The initial margins below are APPROXIMATE (exchanges change them
with volatility and every broker adds its own): they only decide, in the simulation, whether a contract fits a small
account; never a broker's number. Symbols are TradingView's: `CME_MINI:MES1!` (continuous) or `CME_MINI:MESZ2026`.
"""
from __future__ import annotations

import re
from typing import Any

FUTURES: dict[str, dict[str, Any]] = {
    "ES": {"name": "E-mini S&P 500", "multiplier": 50.0, "tick": 0.25, "margin": 14000.0},
    "MES": {"name": "Micro E-mini S&P 500", "multiplier": 5.0, "tick": 0.25, "margin": 1500.0},
    "NQ": {"name": "E-mini Nasdaq-100", "multiplier": 20.0, "tick": 0.25, "margin": 20000.0},
    "MNQ": {"name": "Micro E-mini Nasdaq-100", "multiplier": 2.0, "tick": 0.25, "margin": 2000.0},
    "YM": {"name": "E-mini Dow", "multiplier": 5.0, "tick": 1.0, "margin": 10000.0},
    "MYM": {"name": "Micro E-mini Dow", "multiplier": 0.5, "tick": 1.0, "margin": 1000.0},
    "RTY": {"name": "E-mini Russell 2000", "multiplier": 50.0, "tick": 0.1, "margin": 7000.0},
    "M2K": {"name": "Micro E-mini Russell 2000", "multiplier": 5.0, "tick": 0.1, "margin": 700.0},
}
# root + continuous suffix ("1!") or month code + year ("Z2026", "H26")
_TICKER = re.compile(r"^([A-Z0-9]+?)(?:\d!|[FGHJKMNQUVXZ]\d{2,4})$")


def futures_spec(symbol: str) -> dict[str, Any] | None:
    """{root, name, multiplier, tick, margin} of an index future NEXUS can trade, or None (any other symbol)."""
    sym = (symbol or "").strip().upper()
    ticker = sym.partition(":")[2] or sym
    m = _TICKER.match(ticker)
    root = m.group(1) if m else ticker
    spec = FUTURES.get(root)
    return {"root": root, **spec} if spec is not None else None
