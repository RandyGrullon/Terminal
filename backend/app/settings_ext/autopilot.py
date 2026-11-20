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
    # several timeframes at once, one trader each (e.g. ["5m", "1h", "4h"]: quick, measured and long-term trades),
    # at most 3; [] = only autopilot.tf. Still one position per market (the first timeframe with an entry takes it).
    "autopilot.tfs": [],
    "autopilot.max_positions": 5,  # open positions at most (one per symbol)
    "autopilot.risk_pct": 1.0,  # % of the account's equity risked per trade (distance to the stop)
    # MT5 lots (app.engine.lots: gold 100 oz, crypto 1 coin, stocks 1 share, forex 100,000): its size rounded down to
    # steps of 0.01 lot; under 0.01 lot, the minimum lot only if it risks at most lot_max_risk_pct % of its equity at
    # the stop (else the trade is skipped)
    "autopilot.lots": False,
    "autopilot.lot_max_risk_pct": 5.0,
    "autopilot.scanner_top": 10,  # opportunities of the latest scan it may watch
    "autopilot.initial_balance": 1000.0,  # its demo account starts with this (and a reset without another amount)
    # a cash account: what it holds in shares / funds / crypto / other non-derivatives is at most this % of its equity
    "autopilot.max_exposure_pct": 100.0,
    # loss brake: no new entries once its equity is this % below its initial balance (0 = off)
    "autopilot.max_drawdown_pct": 20.0,
    # index futures and crypto perpetuals: the most of its equity tied up as (approximate) initial margin (0 = it
    # trades no futures)
    "autopilot.futures_margin_pct": 50.0,
    # crypto perpetuals (BINANCE:BTCUSDT.P…): leverage of their margin (1..50); the entry is refused when the
    # (approximate) liquidation would come before the stop
    "autopilot.perp_leverage": 10.0,
    # crypto spot pairs (BINANCE:SOLUSDT…, not perpetuals): true = also short (a simulated short sale) · false = like
    # a spot wallet, it only buys and later sells what it holds; its bets on a fall go through perpetuals (.P)
    "autopilot.spot_short": True,
    # buy-and-hold portfolio (like a broker account): this % of its equity spread over hold_basket, each asset bought at
    # market once (stocks with their market open) and held until the user sells it or its emergency stop
    # (hold_stop_pct below the purchase) is hit; outside the trading limits. 0 = off.
    "autopilot.hold_pct": 0.0,
    "autopilot.hold_basket": [
        {"symbol": "AMEX:SPY", "weight": 25}, {"symbol": "NASDAQ:QQQ", "weight": 15},
        {"symbol": "NASDAQ:AAPL", "weight": 10}, {"symbol": "NASDAQ:NVDA", "weight": 10},
        {"symbol": "NASDAQ:MSFT", "weight": 10}, {"symbol": "NASDAQ:AMZN", "weight": 10},
        {"symbol": "BINANCE:BTCUSDT", "weight": 10}, {"symbol": "BINANCE:ETHUSDT", "weight": 10},
    ],
    "autopilot.hold_stop_pct": 50.0,
    # stocks and funds: auto = shares when NEXUS says BUY and a put when it says SELL (a cash account cannot sell
    # short) · shares = shares both ways (a simulated short sale) · options = a call or a put (theoretical prices)
    "autopilot.stock_instrument": "auto",
    # options: the most premium it pays per trade, % of its equity (0 = it buys no options)
    "autopilot.options_premium_pct": 10.0,
    # it learns from its losses: no entries in a context (session, regime, news, filter score, direction, market, signal
    # source, timeframe) where
    # its trades of the last 90 days lose with strong evidence (30+ trades, the whole 99 % interval below zero)
    "autopilot.learn_guard": True,
    "autopilot.script_ids": [],  # saved PyChart strategies that also give it entries (at most 3)
    "autopilot.script_gate": True,  # their entries also need the gate's TOMAR
    # its aggressive mode (app.sim.autopilot_goal.AGGRESSIVE) during the New York stock session only (Monday to Friday
    # 9:30–16:00 New York time, NYSE holidays and early closes: app.sim.market_session); its saved settings outside it
    "autopilot.session_aggressive": False,
    # one position per market and timeframe instead of one per market: e.g. with 5m, 15m and 1h it may hold three
    # XAUUSD trades at once (one per timeframe; also its challenge accounts). Still within the portfolio risk limits
    # (positions per correlated group, open risk)
    "autopilot.per_tf_positions": False,
    # the vault (app.sim.autopilot_vault): the realized balance of the 10k and the 1k accounts above their initial
    # balance is set aside there (they keep trading from their base); off by default
    "autopilot.vault": False,
    # the 10k account's goal in $ (0 = none): its progress counts the account plus what it put in the vault
    "autopilot.main_target": 0.0,
}
