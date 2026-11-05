"""Default settings of NEXUS's own demo account (account 3, "NEXUS automática"), merged into DEFAULT_SETTINGS.

The account trades on its own: on each closed bar of `autopilot.tf` NEXUS opens a position when its signal is
COMPRAR / VENDER and the Trade Gate says TOMAR, and closes it on its exit rules (app.sim.paper, profile
"autopilot"). It is off until the user turns it on.
"""
from __future__ import annotations

DEFAULTS: dict = {
    "autopilot.enabled": False,
    # scanner = the best TOMAR opportunities (gate TOMAR + NEXUS signal) of the latest scan · list = autopilot.symbols
    "autopilot.source": "scanner",
    "autopilot.symbols": [],
    "autopilot.tf": "1h",
    "autopilot.max_positions": 5,  # open positions at most (one per symbol)
    "autopilot.risk_pct": 1.0,  # % of the account's equity risked per trade (distance to the stop)
    "autopilot.scanner_top": 10,  # opportunities of the latest scan it may watch
}
