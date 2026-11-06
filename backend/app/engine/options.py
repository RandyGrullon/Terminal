"""Theoretical stock options for the simulation of NEXUS's own demo account (it only BUYS calls and puts).

Real option quotes need a paid data feed, so every option price here is THEORETICAL: Black-Scholes (European, no
dividends) with the underlying's live price, the annualized 20-day historical volatility of its daily closes and a
flat risk-free rate. Real premiums differ (implied volatility, dividends, the bid/ask spread, early exercise): the app
always says so. Contract = 100 shares; expiries = the monthly ones (third Friday); strikes = a simplified grid by price.

Symbol of an option position: "OPT:<EXCHANGE>:<TICKER>:<YYYYMMDD>:<C|P>:<STRIKE>", e.g. OPT:NASDAQ:AAPL:20261218:C:230.
Pure: no network, no database.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

MULTIPLIER = 100  # shares per contract
RATE = 0.04  # flat annual risk-free rate of the theoretical price
# commission + half the bid/ask spread per side, % of the premium: an approximation for liquid options (about
# 0,65 $ of commission per contract plus a couple of cents of half-spread on a 1 $ premium); illiquid ones cost more
FEE_PCT = 2.0
VOL_WINDOW = 20  # daily returns of the historical volatility
MIN_SIGMA, MAX_SIGMA = 0.05, 3.0
MIN_DAYS = 30  # it buys the first monthly expiry at least this many days away
EXIT_DAYS = 7  # and closes the option this many days before it expires (time decay speeds up)
MIN_DELTA = 0.30  # strikes further out of the money than this are never bought
MIN_PREMIUM = 0.05  # $ per share: cheaper options are not traded
STOP_FRACTION = 0.5  # premium stop: −50 %
TARGETS = (1.5, 2.0, 2.5)  # premium targets: +50 %, +100 %, +150 % (1R, 2R, 3R with the stop at −50 %)
PREFIX = "OPT:"


def is_option(symbol: Any) -> bool:
    return isinstance(symbol, str) and symbol.upper().startswith(PREFIX)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def intrinsic(right: str, spot: float, strike: float) -> float:
    return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)


def _d1(spot: float, strike: float, t: float, sigma: float, rate: float) -> float:
    return (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))


def bs_price(right: str, spot: float, strike: float, t: float, sigma: float, rate: float = RATE) -> float:
    """Black-Scholes premium per share (European, no dividends); the intrinsic value at or after expiry."""
    if spot <= 0 or strike <= 0:
        return 0.0
    if t <= 0 or sigma <= 0:
        return intrinsic(right, spot, strike)
    d1 = _d1(spot, strike, t, sigma, rate)
    d2 = d1 - sigma * math.sqrt(t)
    disc = strike * math.exp(-rate * t)
    if right == "call":
        return max(spot * norm_cdf(d1) - disc * norm_cdf(d2), 0.0)
    return max(disc * norm_cdf(-d2) - spot * norm_cdf(-d1), 0.0)


def bs_delta(right: str, spot: float, strike: float, t: float, sigma: float, rate: float = RATE) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    if t <= 0 or sigma <= 0:
        itm = spot > strike if right == "call" else spot < strike
        return (1.0 if right == "call" else -1.0) if itm else 0.0
    d1 = _d1(spot, strike, t, sigma, rate)
    return norm_cdf(d1) if right == "call" else norm_cdf(d1) - 1.0


def historical_vol(closes: Sequence[Any], window: int = VOL_WINDOW) -> float | None:
    """Annualized volatility of the last `window` daily log returns (×√252), clamped; None with too few closes."""
    c = [float(x) for x in closes if x is not None and float(x) > 0][-(int(window) + 1):]
    if len(c) < int(window) + 1:
        return None
    r = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    mean = sum(r) / len(r)
    var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    return min(max(math.sqrt(var) * math.sqrt(252.0), MIN_SIGMA), MAX_SIGMA)


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)  # the third Friday is between the 15th and the 21st
    return d + timedelta(days=(4 - d.weekday()) % 7)


def monthly_expiry(today: date, min_days: int = MIN_DAYS) -> date:
    """The first monthly expiry (third Friday) at least `min_days` days after `today`."""
    y, m = today.year, today.month
    for _ in range(36):
        e = third_friday(y, m)
        if (e - today).days >= min_days:
            return e
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise ValueError("Sin vencimiento mensual")


def expiry_ts(expiry: date) -> int:
    """Unix time of an expiry: the close of that Friday (21:00 UTC ≈ 16:00 in New York)."""
    return int(datetime(expiry.year, expiry.month, expiry.day, 21, 0, tzinfo=timezone.utc).timestamp())


def years_to(expiry: date, now_s: int) -> float:
    return max(expiry_ts(expiry) - int(now_s), 0) / (365.0 * 86400.0)


def days_to(expiry: date, now_s: int) -> float:
    return (expiry_ts(expiry) - int(now_s)) / 86400.0


def strike_step(price: float) -> float:
    """A simplified strike grid: 0,5 under 25 $, 1 under 100 $, 2,5 under 200 $, 5 above."""
    return 0.5 if price < 25 else 1.0 if price < 100 else 2.5 if price < 200 else 5.0


def strike_grid(price: float, right: str, steps: int = 4) -> list[float]:
    """The at-the-money strike first, then `steps` − 1 strikes further out of the money (up for a call)."""
    step = strike_step(price)
    atm = round(price / step) * step
    sign = 1.0 if right == "call" else -1.0
    return [round(atm + sign * i * step, 4) for i in range(int(steps)) if atm + sign * i * step > 0]


def _strike_text(strike: float) -> str:
    return f"{strike:.4f}".rstrip("0").rstrip(".")


def option_symbol(underlying: str, expiry: date, right: str, strike: float) -> str:
    return f"{PREFIX}{underlying.upper()}:{expiry:%Y%m%d}:{'C' if right == 'call' else 'P'}:{_strike_text(strike)}"


def parse_option_symbol(symbol: str) -> dict[str, Any] | None:
    """{underlying, expiry (date), right, strike} of an option symbol, or None."""
    if not is_option(symbol):
        return None
    parts = symbol.upper().split(":")
    if len(parts) != 6:
        return None
    _, exchange, ticker, ymd, cp, strike = parts
    try:
        expiry = datetime.strptime(ymd, "%Y%m%d").date()
        k = float(strike)
    except ValueError:
        return None
    if cp not in ("C", "P") or k <= 0 or not exchange or not ticker:
        return None
    return {"underlying": f"{exchange}:{ticker}", "expiry": expiry, "right": "call" if cp == "C" else "put", "strike": k}


def option_name(underlying: str, expiry: date, right: str, strike: float) -> str:
    """"AAPL 230 C 18/12/2026": ticker, strike, C (call) / P (put), expiry."""
    ticker = underlying.partition(":")[2] or underlying
    return f"{ticker} {_strike_text(strike).replace('.', ',')} {'C' if right == 'call' else 'P'} {expiry:%d/%m/%Y}"


def choose_option(spot: float, right: str, t: float, sigma: float, budget: float, *,
                  rate: float = RATE) -> dict[str, Any] | None:
    """The strike NEXUS buys: the at-the-money one, or the first further out whose contract fits `budget` ($ of
    premium), never past |delta| MIN_DELTA nor under MIN_PREMIUM. {strike, premium, delta, contracts, cost} or None
    with `cheapest` (the cost of the cheapest allowed contract) in a dict {"none": True, "cheapest": ...}."""
    cheapest = None
    for strike in strike_grid(spot, right):
        premium = bs_price(right, spot, strike, t, sigma, rate)
        delta = abs(bs_delta(right, spot, strike, t, sigma, rate))
        if delta < MIN_DELTA or premium < MIN_PREMIUM:
            break
        cost = premium * MULTIPLIER
        cheapest = cost if cheapest is None else min(cheapest, cost)
        if cost <= budget:
            return {"strike": strike, "premium": premium, "delta": delta, "contracts": int(budget // cost),
                    "cost": cost}
    return {"none": True, "cheapest": cheapest}
