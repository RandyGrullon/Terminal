"""Futures NEXUS's own demo account can trade: index futures with their contract terms (CME / CBOT equity index
futures) and crypto perpetuals (PERPETUALS, below).

A position in a future holds `contracts × multiplier` units of the index (P&L = points × multiplier per contract), so
the paper engine's accounting needs no other change. The initial margins below are APPROXIMATE (exchanges change them
with volatility and every broker adds its own): they only decide, in the simulation, whether a contract fits a small
account; never a broker's number. Symbols are TradingView's: `CME_MINI:MES1!` (continuous) or `CME_MINI:MESZ2026`.
"""
from __future__ import annotations

import re
from typing import Any

from app.data.symbols import split_pair

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


# Crypto perpetual futures (USD-margined, no expiry, 24/7) NEXUS's own account can trade: TradingView symbols such as
# `BINANCE:BTCUSDT.P`. One contract = roughly a large exchange's minimum order step of the coin (approximate), so whole
# contracts behave like the index futures above. There is no fixed margin: it is the notional / the leverage
# (autopilot.perp_leverage), worked out at the entry price. Their candles are those of the equivalent spot pair
# (app.data.symbols routes `.P` to it; the basis between them is usually a few hundredths of a percent), or, for a
# perpetual without a spot pair (gold XAUUSDT), those of Binance's USDⓈ-M futures. Funding payments are NOT simulated.
# coin -> units of the coin per contract.
PERPETUALS: dict[str, float] = {
    "BTC": 0.001, "ETH": 0.001, "BNB": 0.01, "SOL": 1.0, "XRP": 0.1, "DOGE": 1.0, "ADA": 1.0, "LINK": 0.01,
    "AVAX": 1.0,
    # PAX Gold: 1 token = 1 troy ounce of gold, so its price follows XAU/USD (a small premium or discount): the gold
    # NEXUS can trade with full history from Binance when no key gives OANDA:XAUUSD's
    "PAXG": 0.001,
    # Gold (XAU/USD): Binance's XAUUSDT «TradFi» perpetual, in troy ounces (its order step is 0.001 oz); priced like
    # MetaTrader's XAUUSD, so 0.01 lot = 1 oz (app.engine.lots)
    "XAU": 0.001,
}
PERP_NAMES = {"XAU": "Oro XAUUSD"}
PERP_QUOTES = frozenset({"USDT", "USDC", "USD"})
PERP_FEE_PCT = 0.05  # % per side: a taker order at a large exchange's base tier (approximate)
PERP_MAINT_PCT = 0.4  # % of the notional: approximate maintenance margin of the first tier (liquidation price)
PERP_LEVERAGE_MAX = 50.0


def perpetual_spec(symbol: str) -> dict[str, Any] | None:
    """{root, name, multiplier, perpetual: True} of a crypto perpetual NEXUS can trade (`BINANCE:BTCUSDT.P`), or None
    (spot pairs, other coins, pairs not in dollars and any other symbol)."""
    sym = (symbol or "").strip().upper()
    ticker = sym.partition(":")[2] or sym
    if not ticker.endswith(".P"):
        return None
    pair = split_pair(ticker[:-2])
    if pair is None or pair[1] not in PERP_QUOTES:
        return None
    mult = PERPETUALS.get(pair[0])
    if mult is None:
        return None
    name = f"Perpetuo {pair[0]}/{pair[1]}"
    if pair[0] in PERP_NAMES:
        name = f"{PERP_NAMES[pair[0]]} (perpetuo {pair[0]}/{pair[1]})"
    return {"root": ticker, "name": name, "multiplier": mult, "perpetual": True}


def perp_margin(price: float, multiplier: float, leverage: float) -> float:
    """Initial margin of one perpetual contract: its notional / the leverage."""
    return float(price) * float(multiplier) / max(float(leverage), 1.0)


def liquidation_price(entry: float, sign: int, leverage: float) -> float:
    """APPROXIMATE isolated-margin liquidation price: where the loss eats the initial margin down to the maintenance
    margin (long: below the entry, short: above it). Real exchanges add tiers, fees and the mark price."""
    lev = max(float(leverage), 1.0)
    return float(entry) * (1.0 - (1 if sign > 0 else -1) * (1.0 / lev - PERP_MAINT_PCT / 100.0))
