"""Per-account plans of NEXUS automática (autopilot.account_plans): each of its accounts trades with a risk, a daily
goal and a daily loss limit scaled to its size, because a 10.000 $ account and a 100 $ one are not the same game. The
tier comes from the account's INITIAL balance (it does not change as the account grows or shrinks):

- risk_pct: % of the equity risked per trade (distance to the stop);
- day_target_pct: when the day's realized P&L reaches this % of the balance it started the day with, the account opens
  nothing new until 00:00 UTC (it keeps managing what is open): «la meta del día»;
- day_loss_pct: the daily loss limit (the usual block).

The goal is about two good trades (≈ 2 × the risk) and the loss limit three losing ones, so one bad day never costs
more than about three good ones earn. Simulated money; a goal is a discipline rule, not a forecast.
"""
from __future__ import annotations

from typing import Any

PLANS_KEY = "autopilot.account_plans"

# (minimum initial balance, tier, risk %, daily goal %, daily loss %), largest first
TIERS: tuple[tuple[float, str, float, float, float], ...] = (
    (5000.0, "grande", 0.75, 1.5, 2.25),
    (1000.0, "media", 1.5, 3.0, 4.5),
    (300.0, "pequeña", 2.0, 4.0, 6.0),
    (0.0, "micro", 2.5, 5.0, 7.5),
)
TIER_TEXT = {
    "grande": "Cuenta grande: protege lo ganado. 0,75 % de riesgo por operación, meta del día +1,5 %, pérdida máxima del "
              "día 2,25 %.",
    "media": "Cuenta media: 1,5 % por operación, meta del día +3 %, pérdida máxima del día 4,5 %.",
    "pequeña": "Cuenta pequeña: 2 % por operación, meta del día +4 %, pérdida máxima del día 6 %.",
    "micro": "Cuenta micro: 2,5 % por operación, meta del día +5 %, pérdida máxima del día 7,5 % (con tan poco saldo "
             "cada dólar cuenta, pero una racha mala también).",
}


def plan_for(initial: Any) -> dict[str, Any]:
    """The plan of an account by its initial balance."""
    try:
        ini = float(initial)
    except (TypeError, ValueError):
        ini = 0.0
    for floor, tier, risk, goal, loss in TIERS:
        if ini >= floor:
            return {"tier": tier, "risk_pct": risk, "day_target_pct": goal, "day_loss_pct": loss,
                    "text": TIER_TEXT[tier]}
    raise AssertionError("unreachable")


def view(account_id: int, plan: dict[str, Any], daily: dict[str, Any]) -> dict[str, Any]:
    """AutopilotAccountPlan: the plan plus where the day stands (realized P&L vs its goal and its loss limit)."""
    start = float(daily.get("start_balance") or 0.0)
    target = round(start * plan["day_target_pct"] / 100.0, 2) if start > 0 else None
    loss = round(start * plan["day_loss_pct"] / 100.0, 2) if start > 0 else None
    pnl = round(float(daily.get("realized") or 0.0), 2)
    return {
        "account_id": int(account_id),
        "tier": plan["tier"],
        "risk_pct": plan["risk_pct"],
        "risk_usd": round(start * plan["risk_pct"] / 100.0, 2) if start > 0 else None,
        "day_target_pct": plan["day_target_pct"],
        "day_target": target,
        "day_loss_pct": plan["day_loss_pct"],
        "day_loss_limit": loss,
        "day_pnl": pnl,
        "day_progress_pct": round(max(min(pnl / target * 100.0, 100.0), -100.0), 1) if target else None,
        "target_hit": bool(daily.get("target_hit")),
        "loss_hit": bool(daily.get("blocked")) and not bool(daily.get("target_hit")),
        "text": plan["text"],
    }
