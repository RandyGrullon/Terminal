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


# ----------------------------------------------------------------------------- overnight swap (MT5 lots)
#
# A broker charges (or pays) a swap on a position held through its daily rollover. NEXUS automática trades in MT5 lots
# as on XM, so its gold positions pay XM's GOLD swap, measured on its demo account on 2026-11-18 (GenXAU): longs about
# −75 USD per 1.00 lot and night, shorts about +16 USD, triple on Wednesday's rollover (it covers the weekend). It is
# XM's CURRENT swap: it changes with interest rates, so it lives in the setting `risk.swap_by_symbol` (USD per 1.00
# lot per night, by asset: XAU), never in the code. The rollover is 00:00 of XM's server (GMT+2, GMT+3 with the US
# daylight saving time) = 17:00 New York: 22:00 UTC in winter, 21:00 UTC in summer; one each night from Monday to
# Friday, Wednesday's counting three. Binance's gold perpetual has a funding rate instead: this models the MT5 costs.

ROLLOVER_HOUR_NY = 17  # 00:00 of XM's server = 17:00 America/New_York all year round
SWAP_TRIPLE_WEEKDAY = 2  # Wednesday (Monday 0): its rollover counts three nights
_GOLD_BASES = {"XAU", "GOLD"}


def swap_asset(symbol: str) -> str | None:
    """The asset whose swap applies to `symbol`: 'XAU' for gold (BINANCE:XAUUSDT.P, OANDA:XAUUSD, XM's GOLD…), else
    its base ticker (EUR for EURUSD); None for options and synthetic markets."""
    sym = (symbol or "").strip().upper()
    if not sym or is_sim(sym) or sym.startswith("OPT:"):
        return None
    ticker = sym.partition(":")[2] or sym
    if ticker.endswith(".P"):
        ticker = ticker[:-2]
    for gold in _GOLD_BASES:
        if ticker.startswith(gold):
            return "XAU"
    for quote in ("USDT", "USDC", "USD"):
        if ticker.endswith(quote) and len(ticker) > len(quote):
            return ticker[: -len(quote)]
    return ticker


def swap_rule(symbol: str, rules: Any) -> dict[str, float] | None:
    """{long, short}: USD per 1.00 lot and night for `symbol` from `risk.swap_by_symbol` (None: no swap)."""
    asset = swap_asset(symbol)
    if asset is None or not isinstance(rules, dict):
        return None
    raw = rules.get(asset)
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for side in ("long", "short"):
        try:
            v = float(raw.get(side, 0.0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        out[side] = v if v == v else 0.0  # NaN -> 0
    return out if (out["long"] or out["short"]) else None


def rollovers(after_s: float, until_s: float) -> list[tuple[int, int]]:
    """[(unix s, nights)] of the rollovers in (after_s, until_s]: 17:00 New York from Monday to Friday, Wednesday's
    counting three nights."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    if until_s <= after_s:
        return []
    day = datetime.fromtimestamp(int(after_s), ny).date()
    end = datetime.fromtimestamp(int(until_s), ny).date()
    out: list[tuple[int, int]] = []
    while day <= end:
        if day.weekday() < 5:
            ts = int(datetime(day.year, day.month, day.day, ROLLOVER_HOUR_NY, tzinfo=ny).timestamp())
            if after_s < ts <= until_s:
                out.append((ts, 3 if day.weekday() == SWAP_TRIPLE_WEEKDAY else 1))
        day += timedelta(days=1)
    return out


def swap_for(rule: dict[str, float], side: str, lots: float, nights: int) -> float:
    """USD credited (+) or charged (−) for `lots` lots held `nights` nights."""
    return float(rule.get("long" if side == "long" else "short", 0.0)) * float(lots) * int(nights)
