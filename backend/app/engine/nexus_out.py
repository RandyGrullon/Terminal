"""Output helpers of the NEXUS engine: chart markers (Spanish tooltips) and summary statistics."""
from __future__ import annotations

import math

import numpy as np

from app.engine import fmt
from app.sim.metrics import periods_per_year, sharpe_ratio

GREEN = "#16c784"
RED = "#ea3943"
AMBER = "#f0b90b"
MUTED = "#787b86"
BLUE = "#2962ff"
PURPLE = "#9c27b0"

EXIT_MARKER = {
    "SL": ("EXIT_SL", "SL", RED),
    "BE": ("EXIT_SL", "SL", MUTED),
    "TRAIL": ("EXIT_TRAIL", "TRAIL", BLUE),
    "EARLY": ("EXIT_EARLY", "SALIDA", AMBER),
    "REVERSE": ("EXIT_REVERSE", "GIRO", PURPLE),
    "TIME": ("EXIT_TIME", "TIEMPO", MUTED),
    "SIGNAL": ("EXIT_SIGNAL", "CIERRE", AMBER),
}


def entry_marker(t: int, side: int, price: float, gate: dict | None, sl: float, tps: list[float]) -> dict:
    long = side > 0
    score = gate.get("score") if gate else None
    word = "COMPRA" if long else "VENTA"
    text = f"{word} · {score:.0f}" if isinstance(score, (int, float)) else word
    verdict = f"filtro {gate.get('verdict_label')} {fmt.num(score, 0)}/100" if gate else "sin filtro"
    tooltip = (f"{'Compra' if long else 'Venta'} a {fmt.price(price)} · {verdict} · stop {fmt.price(sl)} · "
               f"objetivos {fmt.price(tps[0])} / {fmt.price(tps[1])} / {fmt.price(tps[2])}")
    return {
        "time": int(t), "kind": "BUY" if long else "SELL",
        "position": "belowBar" if long else "aboveBar", "shape": "arrowUp" if long else "arrowDown",
        "color": GREEN if long else RED, "text": text, "tooltip": tooltip, "price": float(price),
    }


def tp_marker(t: int, side: int, k: int, price: float, r_total: float | None) -> dict:
    if k == 1:
        what = "cerrado 1/3 y stop a punto de equilibrio"
    elif k == 2:
        what = "cerrado otro 1/3; stop dinámico activado"
    else:
        what = f"posición cerrada ({fmt.rr(r_total)})" if r_total is not None else "posición cerrada"
    return {
        "time": int(t), "kind": f"TP{k}", "position": "aboveBar" if side > 0 else "belowBar", "shape": "circle",
        "color": GREEN, "text": f"TP{k}", "tooltip": f"Objetivo {k} alcanzado en {fmt.price(price)}: {what}",
        "price": float(price),
    }


def exit_marker(t: int, side: int, reason: str, price: float, r_total: float | None, detail: str) -> dict:
    kind, text, color = EXIT_MARKER.get(reason, ("EXIT_SIGNAL", "CIERRE", AMBER))
    r_txt = f" ({fmt.rr(r_total)})" if r_total is not None else ""
    return {
        "time": int(t), "kind": kind, "position": "aboveBar" if side > 0 else "belowBar", "shape": "square",
        "color": color, "text": text, "tooltip": f"{detail} en {fmt.price(price)}{r_txt}", "price": float(price),
    }


def rejected_marker(t: int, side: int, gate: dict, price: float) -> dict:
    long = side > 0
    reason = (gate.get("reasons_against") or [gate.get("summary") or ""])[0]
    tooltip = (f"{'Compra' if long else 'Venta'} rechazada ({gate.get('verdict_label')} "
               f"{fmt.num(gate.get('score'), 0)}/100): {reason}")
    return {
        "time": int(t), "kind": "REJECTED_LONG" if long else "REJECTED_SHORT",
        "position": "belowBar" if long else "aboveBar", "shape": "circle",
        "color": AMBER if gate.get("verdict") == "WAIT" else MUTED, "text": "NO", "tooltip": tooltip,
        "price": float(price),
    }


def _round(x: float | None, d: int = 4) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(float(x), d)


def compute_stats(trades: list[dict], rejected_outcomes: list[bool | None], bar_returns: np.ndarray,
                  tf_s: int, times: np.ndarray | None = None) -> dict:
    """NexusStats. `bar_returns` = per-bar strategy returns (NaN during the warm-up), `times` = their bar times."""
    closed = [t for t in trades if t.get("exit_time") is not None and t.get("pnl_pct") is not None]
    n = len(closed)
    pnls = np.array([float(t["pnl_pct"]) for t in closed]) if n else np.zeros(0)
    rs = np.array([float(t["r_multiple"]) for t in closed if t.get("r_multiple") is not None])
    win_rate = float((pnls > 0).sum() / n) if n else None
    gross_win = float(pnls[pnls > 0].sum()) if n else 0.0
    gross_loss = float(-pnls[pnls < 0].sum()) if n else 0.0
    pf = gross_win / gross_loss if gross_loss > 1e-12 else None
    equity = np.cumprod(1.0 + pnls / 100.0) if n else np.ones(1)
    total = (float(equity[-1]) - 1.0) * 100.0 if n else 0.0
    curve = np.concatenate([[1.0], equity])
    peak = np.maximum.accumulate(curve)
    max_dd = float(np.max((peak - curve) / peak) * 100.0) if len(curve) else 0.0
    # Sharpe of the strategy's per-bar returns: EVERY bar after the warm-up counts (flat bars are 0, so a strategy
    # that is rarely in the market is not scored as if always invested), the commissions are charged on the bars
    # of the fills (net of fees, like total_return_pct and the R multiples), and it is annualised like the
    # backtests (app.sim.metrics: sample std, bars per year from the bar times; fewer than 365 days of bars for
    # markets with sessions).
    fin = np.isfinite(bar_returns)
    rets = bar_returns[fin]
    sharpe = None
    if len(rets) >= 10 and np.count_nonzero(rets) >= 5:
        t_fin = np.asarray(times)[fin] if times is not None and len(times) == len(bar_returns) else np.zeros(0)
        sharpe = sharpe_ratio(rets, periods_per_year(t_fin, tf_s))
    known = [o for o in rejected_outcomes if o is not None]
    return {
        "trades": n,
        "win_rate": _round(win_rate),
        "profit_factor": _round(pf, 3),
        "total_return_pct": round(total, 4),
        "max_dd_pct": round(max_dd, 4),
        "sharpe": _round(sharpe, 3),
        "avg_r": _round(float(rs.mean()) if len(rs) else None),
        "expectancy_pct": _round(float(pnls.mean()) if n else None),
        "rejected_signals": len(rejected_outcomes),
        # outcome = TP1 reached before the SL within the time stop (NexusRun.outcome); the rate is over the
        # rejected signals whose outcome is already known (`rejected_resolved`), not over `rejected_signals`
        "rejected_resolved": len(known),
        "rejected_would_win_rate": _round(sum(1 for o in known if o) / len(known) if known else None),
    }


def empty_stats() -> dict:
    return compute_stats([], [], np.zeros(0), 3600)
