"""Mechanical exits of the validation baselines: the engine's own risk rules without its model-driven exits.

A trade decided at the close of bar d fills at open[d+1] (voided when that open gaps through the planned stop, as in
the engine) and is then managed bar by bar with `app.engine.positions.Position`, the class NEXUS itself uses: SL
first when both are touched in one bar, gaps fill at the open, TP1 closes 1/3 and moves the stop to break-even, TP2
closes 1/3 and starts the chandelier trail (`trail_atr`·ATR, effective from the next bar), TP3 closes the rest, and
the time stop closes at the next open after `time_stop_bars` bars without TP1. Commissions per side are inside the R
multiple (`Position.result`); slippage is charged with `slippage_r` on every variant alike.

What is NOT here: NEXUS' model exits (early exit on negative expected value, score turning against, reverse signal).
They need the model, so random entries cannot have them; the "same exits" comparison therefore re-runs NEXUS' own
entries through these mechanical exits too (see walkforward.py).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from app.engine.levels import Levels, plan_levels
from app.engine.positions import Position


def slippage_r(side: int, entry: float, stop: float, r_net: float, slippage_pct: float) -> float:
    """Slippage of the entry and exit fills in R: slippage % × (entry + average exit) / risk (the account rule of
    app.sim.backtest.simulate_account, where the average exit is entry + side × R × risk)."""
    risk = abs(float(entry) - float(stop))
    if risk <= 0 or slippage_pct <= 0 or not math.isfinite(r_net):
        return 0.0
    avg_exit = max(float(entry) + side * float(r_net) * risk, 0.0)
    return float(slippage_pct) / 100.0 * (float(entry) + avg_exit) / risk


def simulate_exit(f: Any, decision_idx: int, lv: Levels, *, fee_pct: float, time_stop_bars: int,
                  trail_atr: float) -> tuple[float, int, int] | None:
    """(R net of commissions, exit bar index, entry bar index) of a trade decided at the close of `decision_idx` with
    the levels `lv`; None when the entry is voided by a gap through the stop or the outcome is not known yet (the data
    ends first)."""
    n = int(f.n)
    e = int(decision_idx) + 1
    if e >= n:
        return None
    side = int(lv.side)
    o = float(f.open[e])
    if side * (o - lv.sl) <= 0:  # a gap through the planned stop voids the entry (engine rule)
        return None
    pos = Position(0, lv, e, int(f.time[e]), o, None, 0, int(decision_idx))
    opens, highs, lows, atrs = f.open, f.high, f.low, f.atr
    for k in range(e, n):
        pos.process_bar(float(opens[k]), float(highs[k]), float(lows[k]))
        pos.bars += 1
        if not pos.is_open:
            return float(pos.result(None, fee_pct)[1]), k, e
        pos.update_trail(float(atrs[k]), trail_atr)  # computed at the close, effective from the next bar
        if pos.bars >= time_stop_bars and pos.tp_hits == 0:
            if k + 1 >= n:
                return None
            pos.close_at(float(opens[k + 1]))
            return float(pos.result(None, fee_pct)[1]), k + 1, e
    return None


def plan_for(f: Any, d: int, side: int, params: Any) -> Levels:
    """NEXUS' default plan at bar d (the Trade Gate's levels without a user override): SL beyond max(sl_atr·ATR, the
    last swing ± 0.25 ATR), TP1..3 at tp_r·R."""
    atr = float(f.atr[d])
    swing_arr = f.swing_low if side > 0 else f.swing_high
    swing = float(swing_arr[d])
    return plan_levels(side, float(f.close[d]), atr, swing if swing == swing else None, float(params.sl_atr),
                       tuple(params.tp_r))


def random_entry_table(f: Any, start: int, end: int, params: Any, *, fee_pct: float,
                       slippage_pct: float) -> dict[int, np.ndarray]:
    """R multiple (commissions and slippage included) of a long (+1) and a short (−1) entry decided at the close of
    every bar in [start, end), with NEXUS' default levels and the mechanical exits. NaN = voided or unknown."""
    out: dict[int, np.ndarray] = {}
    end = min(int(end), int(f.n) - 1)
    for side in (1, -1):
        table = np.full(int(f.n), np.nan)
        for d in range(max(int(start), 0), end):
            lv = plan_for(f, d, side, params)
            res = simulate_exit(f, d, lv, fee_pct=fee_pct, time_stop_bars=int(params.time_stop_bars),
                                trail_atr=float(params.trail_atr))
            if res is None:
                continue
            r, _, e = res
            table[d] = r - slippage_r(side, float(f.open[e]), lv.sl, r, slippage_pct)
        out[side] = table
    return out
