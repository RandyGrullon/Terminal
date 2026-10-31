"""Default settings of the paper_risk feature (merged into DEFAULT_SETTINGS).

Portfolio-risk limits of the demo account (account 1, app.sim.paper): they block new orders and pending fills, manual
and from the auto-trader, never the closing of positions. 0 switches a rule off.
"""
from __future__ import annotations

DEFAULTS: dict = {
    # total open risk (entry -> current stop of every open position, without fees), % of equity
    "paper.max_open_risk_pct": 6.0,
    # open positions per correlated group (asset class; see paper.crypto_one_group)
    "paper.max_positions_per_group": 3,
    # all crypto counts as ONE correlated group (the coins tend to move together); off: one group per coin
    "paper.crypto_one_group": True,
    # realized loss of the UTC day, % of the balance the day started with: blocks new trades until 00:00 UTC
    "paper.daily_loss_limit_pct": 3.0,
}
