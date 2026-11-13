"""Default settings of NEXUS's own demo account (account 3, "NEXUS automática"), merged into DEFAULT_SETTINGS.

The account trades on its own: on each closed bar of `autopilot.tf` NEXUS opens a position when its signal is
COMPRAR / VENDER and the Trade Gate says TOMAR, and closes it on its exit rules (app.sim.paper, profile
"autopilot"). It is off until the user turns it on.
"""
from __future__ import annotations

DEFAULTS: dict = {
    "autopilot.enabled": False,
    # scanner = the best candidates of the latest scan (its TOMAR opportunities first, then the markets the gate approves
    # while NEXUS has no entry signal there yet; in dollars only) · list = autopilot.symbols · both = the list plus those
    # candidates. Default both: the scanner's candidates come and go (a scan often has none), so it always watches a
    # few liquid markets in dollars too.
    "autopilot.source": "both",
    "autopilot.symbols": ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:SOLUSDT"],
    "autopilot.tf": "1h",
    "autopilot.max_positions": 5,  # open positions at most (one per symbol)
    "autopilot.risk_pct": 1.0,  # % of the account's equity risked per trade (distance to the stop)
    "autopilot.scanner_top": 10,  # opportunities of the latest scan it may watch
    "autopilot.initial_balance": 1000.0,  # its demo account starts with this (and a reset without another amount)
    # a cash account: what it holds in shares / funds / crypto / other non-derivatives is at most this % of its equity
    "autopilot.max_exposure_pct": 100.0,
    # loss brake: no new entries once its equity is this % below its initial balance (0 = off)
    "autopilot.max_drawdown_pct": 20.0,
    # index futures: the most of its equity tied up as (approximate) initial margin (0 = it trades no futures)
    "autopilot.futures_margin_pct": 50.0,
    # stocks and funds: auto = shares when NEXUS says BUY and a put when it says SELL (a cash account cannot sell
    # short) · shares = shares both ways (a simulated short sale) · options = a call or a put (theoretical prices)
    "autopilot.stock_instrument": "auto",
    # options: the most premium it pays per trade, % of its equity (0 = it buys no options)
    "autopilot.options_premium_pct": 10.0,
    # it learns from its losses: no entries in a context (session, regime, news, filter score, direction, market) where
    # its trades of the last 90 days lose with strong evidence (30+ trades, the whole 99 % interval below zero)
    "autopilot.learn_guard": True,
    "autopilot.script_ids": [],  # saved PyChart strategies that also give it entries (at most 3)
    "autopilot.script_gate": True,  # their entries also need the gate's TOMAR
}
