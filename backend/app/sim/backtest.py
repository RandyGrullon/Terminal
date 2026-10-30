"""Backtests on top of NEXUS trades (BacktestResult).

`run_nexus` already produces causal trades (entries at the next bar open, SL/TP intrabar, SL first). This module
turns them into an account: position size from risk % of equity at entry and the SL distance, commissions and
slippage on every fill, partial take-profits for the mark-to-market curve, per-bar equity and drawdown, a buy &
hold benchmark and every BacktestMetrics field. With the gate enabled the same history is replayed with the gate
disabled so the user sees exactly what the "¿Tomo esta operación?" filter adds (gate_lift_pct).

`simulate_account` / `compute_metrics` are pure and testable with hand-made trades; `run_backtest` needs the
engine (imported lazily). CPU work: call `run_backtest` from `asyncio.to_thread` in async code.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from app.data.base import CandleSet
from app.sim import metrics as M
from app.sim.common import (
    DISCLAIMER,
    EXIT_LABEL,
    build_nexus_params,
    current_settings,
    finite,
    json_dumps,
    load_engine,
    params_to_dict,
    run_engine,
)
from app.util import clean_json, now_ms, tf_seconds

PARTIAL_FRACTION = 1.0 / 3.0
MIN_BARS_DEFAULT = 150


class BacktestError(ValueError):
    """Invalid backtest input (message in Spanish)."""


# ----------------------------------------------------------------------------- trade normalisation


def normalize_trade(tr: dict) -> dict | None:
    """Validate an engine Trade dict; returns floats/ints or None when the trade is unusable."""
    if not isinstance(tr, dict):
        return None
    side = str(tr.get("side") or "").lower()
    if side not in ("long", "short"):
        return None
    sign = 1 if side == "long" else -1
    entry_time = finite(tr.get("entry_time"))
    entry = finite(tr.get("entry_price"))
    stop = finite(tr.get("sl_initial", tr.get("sl")))
    if entry_time is None or entry is None or stop is None or entry <= 0:
        return None
    if sign * (entry - stop) <= 0:  # stop must be on the losing side
        return None
    tps: list[float | None] = []
    for key in ("tp1", "tp2", "tp3"):
        lvl = finite(tr.get(key))
        tps.append(lvl if lvl is not None and sign * (lvl - entry) > 0 else None)
    exit_time = finite(tr.get("exit_time"))
    return {
        "side": side,
        "sign": sign,
        "entry_time": int(entry_time),
        "entry": float(entry),
        "stop": float(stop),
        "risk_unit": abs(entry - stop),
        "tp1": tps[0],
        "tp2": tps[1],
        "tp3": tps[2],
        "exit_time": int(exit_time) if exit_time is not None else None,
        "exit_price": finite(tr.get("exit_price")),
        "r_multiple": finite(tr.get("r_multiple")),
        "bars": finite(tr.get("bars")),
        "exit_reason": tr.get("exit_reason"),
    }


def public_trade(tr: dict, index: int) -> dict:
    """Exactly the `Trade` shape of types.ts (account-only fields such as qty / pnl are internal)."""
    reason = tr.get("exit_reason")
    if reason is None and finite(tr.get("exit_time")) is None:
        reason = "OPEN"
    gate_score = finite(tr.get("gate_score"))
    trade_id = finite(tr.get("id"))
    exit_time = finite(tr.get("exit_time"))
    return {
        "id": int(trade_id) if trade_id is not None else index + 1,
        "side": str(tr.get("side") or "").lower(),
        "entry_time": int(finite(tr.get("entry_time")) or 0),
        "entry_price": finite(tr.get("entry_price")),
        "exit_time": int(exit_time) if exit_time is not None else None,
        "exit_price": finite(tr.get("exit_price")),
        "sl_initial": finite(tr.get("sl_initial", tr.get("sl"))),
        "tp1": finite(tr.get("tp1")),
        "tp2": finite(tr.get("tp2")),
        "tp3": finite(tr.get("tp3")),
        "exit_reason": str(reason) if reason is not None else None,
        "exit_label": tr.get("exit_label") or (EXIT_LABEL.get(str(reason)) if reason is not None else None),
        "pnl_pct": finite(tr.get("pnl_pct")),
        "r_multiple": finite(tr.get("r_multiple")),
        "bars": int(finite(tr.get("bars")) or 0),
        "mfe_pct": finite(tr.get("mfe_pct")) or 0.0,
        "mae_pct": finite(tr.get("mae_pct")) or 0.0,
        "gate_score": gate_score,
    }


# ----------------------------------------------------------------------------- account simulation


def simulate_account(
    cs: CandleSet,
    trades: Sequence[dict],
    *,
    initial_capital: float = 10000.0,
    risk_pct: float = 1.0,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.02,
    max_leverage: float = M.DEFAULT_MAX_LEVERAGE,
    trades_net_of_fees: bool = True,
) -> dict[str, Any]:
    """Size and account the engine trades over the candle set (one position at a time).

    P&L of a closed trade = r_multiple × qty × |entry − SL| (includes the engine's partial exits) minus the
    account-level costs. The NEXUS engine already charges commissions inside r_multiple (Position.result: fees on
    the entry and on every exit fill), so with `trades_net_of_fees` (default) the account only deducts slippage
    here and commissions are reported, not charged twice. Trades still open at the end are valued from prices
    (gross), so their commissions are deducted explicitly. For the per-bar mark-to-market curve the TP1/TP2
    partial exits (1/3 each) are placed on the bar where the level is first touched; the final fill realises
    the residual so the trade total always matches the engine's R multiple.
    """
    acct_fee_pct = 0.0 if trades_net_of_fees else fee_pct
    n = len(cs)
    equity = np.full(n, float(initial_capital))
    in_pos = np.zeros(n, dtype=bool)
    if n == 0:
        return {"equity": equity, "in_position": in_pos, "costs": 0.0, "trades": [], "pnls": [], "pnl_pcts": [],
                "r": [], "bars": [], "ruined": False}

    times = cs.time
    high, low, close = cs.high, cs.low, cs.close
    cash = float(initial_capital)
    total_costs = 0.0
    filled_upto = -1  # last bar index whose equity is final
    out_trades: list[dict] = []
    pnls: list[float] = []
    pnl_pcts: list[float] = []
    rs: list[float | None] = []
    bars_list: list[float] = []
    ruined = False

    normalized = []
    for raw in trades:
        tr = normalize_trade(raw)
        if tr is not None:
            normalized.append((tr, raw))
    normalized.sort(key=lambda x: x[0]["entry_time"])

    for tr, raw in normalized:
        if cash <= 0:
            ruined = True
            break
        ei = int(np.searchsorted(times, tr["entry_time"], side="left"))
        if ei >= n or ei < filled_upto:  # outside the data or overlapping the previous position
            continue
        closed = tr["exit_time"] is not None
        if closed:
            xi = int(np.searchsorted(times, tr["exit_time"], side="left"))
            xi = min(max(xi, ei), n - 1)
        else:
            xi = n - 1
        if filled_upto + 1 < ei:
            equity[filled_upto + 1 : ei] = cash

        sign, entry, risk_unit = tr["sign"], tr["entry"], tr["risk_unit"]
        eq_entry = cash
        qty = M.position_size(eq_entry, risk_pct, entry, tr["stop"], max_leverage=max_leverage)
        if qty <= 0:
            continue
        entry_cost = M.trade_cost(qty * entry, acct_fee_pct, slippage_pct)
        cash -= entry_cost
        size_left = 1.0
        realized = 0.0
        partial_notional = 0.0  # sum of part × level of the partial exits (for commission reporting)
        partial_levels = [lvl for lvl in (tr["tp1"], tr["tp2"]) if lvl is not None]
        hits = 0
        exit_cost = 0.0
        avg_exit = None
        r_value = tr["r_multiple"]
        if r_value is None and closed and tr["exit_price"] is not None:
            r_value = sign * (tr["exit_price"] - entry) / risk_unit

        for k in range(ei, xi + 1):
            if closed and k == xi:
                if r_value is None:
                    r_value = sign * (close[k] - entry) / risk_unit
                total_gross = r_value * qty * risk_unit
                avg_exit = max(entry + sign * r_value * risk_unit, 0.0)
                exit_cost = M.trade_cost(qty * avg_exit, acct_fee_pct, slippage_pct)
                cash += (total_gross - realized) - exit_cost
                realized = total_gross
                size_left = 0.0
                equity[k] = cash
                break
            while hits < len(partial_levels) and size_left > 1e-9:
                lvl = partial_levels[hits]
                touched = high[k] >= lvl if sign > 0 else low[k] <= lvl
                if not touched:
                    break
                part = min(PARTIAL_FRACTION, size_left)
                gain = part * qty * sign * (lvl - entry)
                partial_notional += part * lvl
                realized += gain
                cash += gain
                size_left -= part
                hits += 1
            equity[k] = cash + size_left * qty * sign * (close[k] - entry)

        fee_rate = fee_pct / 100.0
        if not closed:  # still open at the end of the data: valued at the last close, no exit fill
            total_gross = realized + size_left * qty * sign * (close[n - 1] - entry)
            # valued from prices (gross): charge the commissions already paid (entry + partial exits)
            open_fees = qty * fee_rate * (entry + partial_notional) if trades_net_of_fees else 0.0
            total_gross -= open_fees
            r_value = total_gross / (qty * risk_unit)
            fees = open_fees if trades_net_of_fees else 0.0
        else:
            total_gross = realized
            fees = qty * fee_rate * (entry + (avg_exit or entry)) if trades_net_of_fees else 0.0
        in_pos[ei : xi + 1] = True
        filled_upto = xi
        costs = entry_cost + exit_cost  # what this function deducts (slippage only when trades are net)
        total_costs += costs + fees  # reported as fees_paid: commissions + slippage
        pnl = total_gross - costs
        pnl_pct = pnl / eq_entry * 100.0 if eq_entry > 0 else 0.0
        n_bars = tr["bars"] if tr["bars"] is not None else float(xi - ei + 1)
        pnls.append(pnl)
        pnl_pcts.append(pnl_pct)
        rs.append(r_value)
        bars_list.append(n_bars)
        enriched = dict(raw)
        enriched.update(
            {
                "r_multiple": round(float(r_value), 4) if r_value is not None else None,
                "qty": qty,
                "pnl": round(pnl, 2),
                "pnl_account_pct": round(pnl_pct, 4),
                "costs": round(costs + fees, 2),
                "equity_before": round(eq_entry, 2),
            }
        )
        if not closed:
            enriched.setdefault("exit_reason", "OPEN")
        out_trades.append(enriched)

    if filled_upto + 1 < n:
        equity[filled_upto + 1 :] = cash
    return {
        "equity": equity,
        "in_position": in_pos,
        "costs": total_costs,
        "trades": out_trades,
        "pnls": pnls,
        "pnl_pcts": pnl_pcts,
        "r": rs,
        "bars": bars_list,
        "ruined": ruined or bool(cash <= 0),
    }


def compute_metrics(cs: CandleSet, sim: dict[str, Any], initial_capital: float) -> dict[str, Any]:
    """BacktestMetrics from a `simulate_account` result (gate fields filled by the caller)."""
    equity = np.asarray(sim["equity"], dtype=float)
    n = len(cs)
    final = float(equity[-1]) if n else float(initial_capital)
    tf_s = tf_seconds(cs.tf)
    span = float(cs.time[-1] - cs.time[0] + tf_s) if n else 0.0
    rets = M.simple_returns(equity)
    ppy = M.periods_per_year(cs.time, tf_s)
    cagr = M.cagr_pct(initial_capital, final, span)
    max_dd = M.max_drawdown_pct(equity)
    summary = M.trade_summary(sim["pnls"], sim["pnl_pcts"], sim["r"], sim["bars"])
    bh = float((cs.close[-1] / cs.close[0] - 1.0) * 100.0) if n and cs.close[0] > 0 else 0.0
    return {
        "initial_capital": float(initial_capital),
        "final_equity": final,
        "total_return_pct": (final / initial_capital - 1.0) * 100.0 if initial_capital > 0 else 0.0,
        "cagr_pct": cagr,
        "buy_hold_return_pct": bh,
        "trades": summary["trades"],
        "win_rate": summary["win_rate"],
        "profit_factor": summary["profit_factor"],
        "avg_win_pct": summary["avg_win_pct"],
        "avg_loss_pct": summary["avg_loss_pct"],
        "expectancy_pct": summary["expectancy_pct"],
        "avg_r": summary["avg_r"],
        "max_dd_pct": max_dd,
        "sharpe": M.sharpe_ratio(rets, ppy),
        "sortino": M.sortino_ratio(rets, ppy),
        "calmar": M.calmar_ratio(cagr, max_dd),
        "exposure_pct": float(np.asarray(sim["in_position"]).mean() * 100.0) if n else 0.0,
        "avg_bars_in_trade": summary["avg_bars_in_trade"],
        "best_trade_pct": summary["best_trade_pct"],
        "worst_trade_pct": summary["worst_trade_pct"],
        "max_consecutive_losses": summary["max_consecutive_losses"],
        "fees_paid": float(sim["costs"]),
        "rejected_signals": 0,
        "rejected_would_win_rate": None,
        "gate_lift_pct": None,
    }


def equity_points(cs: CandleSet, equity: np.ndarray) -> list[dict]:
    dd = M.drawdown_pct(equity)
    t = cs.time.tolist()
    eq = np.round(np.asarray(equity, dtype=float), 2).tolist()
    d = np.round(dd, 3).tolist()
    return [{"time": t[i], "equity": eq[i], "drawdown_pct": d[i]} for i in range(len(t))]


def benchmark_points(cs: CandleSet, initial_capital: float) -> list[dict]:
    if not len(cs) or cs.close[0] <= 0:
        return []
    eq = np.round(initial_capital * cs.close / cs.close[0], 2).tolist()
    t = cs.time.tolist()
    return [{"time": t[i], "equity": eq[i]} for i in range(len(t))]


# ----------------------------------------------------------------------------- engine-driven backtest


def _stats_value(stats: Any, key: str) -> Any:
    return stats.get(key) if isinstance(stats, dict) else None


def _variant(
    cs: CandleSet,
    response: dict,
    *,
    initial_capital: float,
    risk_pct: float,
    fee_pct: float,
    slippage_pct: float,
    max_leverage: float,
) -> dict[str, Any]:
    sim = simulate_account(
        cs,
        response.get("trades") or [],
        initial_capital=initial_capital,
        risk_pct=risk_pct,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        max_leverage=max_leverage,
    )
    metrics = compute_metrics(cs, sim, initial_capital)
    stats = response.get("stats") or {}
    rejected = finite(_stats_value(stats, "rejected_signals"))
    metrics["rejected_signals"] = int(rejected) if rejected is not None else 0
    metrics["rejected_would_win_rate"] = finite(_stats_value(stats, "rejected_would_win_rate"))
    return {"sim": sim, "metrics": metrics}


def run_backtest(
    cs: CandleSet,
    params: Any = None,
    *,
    initial_capital: float = 10000.0,
    risk_pct: float = 1.0,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.02,
    use_gate: bool = True,
    compare_ungated: bool = True,
    max_leverage: float = M.DEFAULT_MAX_LEVERAGE,
    settings: dict | None = None,
    include_candles: bool = True,
) -> dict[str, Any]:
    """Full NEXUS backtest -> BacktestResult dict (JSON-safe). Raises BacktestError / EngineUnavailable."""
    if initial_capital <= 0:
        raise BacktestError("El capital inicial debe ser mayor que 0")
    if not 0 < risk_pct <= 100:
        raise BacktestError("El riesgo por operación debe estar entre 0 y 100 %")
    if fee_pct < 0 or slippage_pct < 0:
        raise BacktestError("Comisión y deslizamiento no pueden ser negativos")
    settings = dict(settings if settings is not None else current_settings())
    settings["risk.fee_pct"] = float(fee_pct)  # the gate's expected value uses the same costs as the account
    min_bars = int(settings.get("nexus.min_bars", MIN_BARS_DEFAULT) or MIN_BARS_DEFAULT)
    if len(cs) < min_bars + 20:
        raise BacktestError(
            f"Historial insuficiente para un backtest: {len(cs)} velas (se necesitan al menos {min_bars + 20})"
        )

    _, nexus_params_cls = load_engine()
    gated_params, ignored = build_nexus_params(nexus_params_cls, settings, params, gate_enabled=bool(use_gate))
    response, _ = run_engine(cs, gated_params)
    kw = dict(
        initial_capital=float(initial_capital),
        risk_pct=float(risk_pct),
        fee_pct=float(fee_pct),
        slippage_pct=float(slippage_pct),
        max_leverage=float(max_leverage),
    )
    main = _variant(cs, response, **kw)
    metrics = main["metrics"]
    ungated_metrics = None
    if use_gate and compare_ungated:
        ungated_params, _ = build_nexus_params(nexus_params_cls, settings, params, gate_enabled=False)
        ungated_response, _ = run_engine(cs, ungated_params)
        ungated = _variant(cs, ungated_response, **kw)
        ungated_metrics = ungated["metrics"]
        ungated_metrics["rejected_signals"] = 0
        ungated_metrics["rejected_would_win_rate"] = None
        metrics["gate_lift_pct"] = metrics["total_return_pct"] - ungated_metrics["total_return_pct"]

    result = {
        "symbol": cs.symbol,
        "tf": cs.tf,
        "bars": len(cs),
        "start_time": int(cs.time[0]),
        "end_time": int(cs.time[-1]),
        "params": {
            "nexus": params_to_dict(gated_params),
            "initial_capital": float(initial_capital),
            "risk_pct": float(risk_pct),
            "fee_pct": float(fee_pct),
            "slippage_pct": float(slippage_pct),
            "use_gate": bool(use_gate),
            "max_leverage": float(max_leverage),
            "ignored_params": ignored,
            "provider": cs.provider,
            "history": cs.history,
            "notes": [
                "Tamaño por riesgo: % del capital al entrar / distancia al stop (apalancamiento máx. "
                f"{max_leverage:g}×).",
                "Los % por operación (avg_win_pct, expectancy_pct…) son sobre el capital, netos de costes.",
                "fees_paid incluye comisiones y deslizamiento.",
                "El filtro parte de sus pesos iniciales (no usa lo aprendido en vivo), así el resultado es fuera de "
                "muestra.",
                DISCLAIMER,
            ],
        },
        "metrics": metrics,
        "ungated": ungated_metrics,
        "equity": equity_points(cs, main["sim"]["equity"]),
        "benchmark": benchmark_points(cs, float(initial_capital)),
        "trades": [public_trade(t, i) for i, t in enumerate(main["sim"]["trades"])],
        "markers": response.get("markers") or [],
        "candles": cs.to_list() if include_candles else [],
    }
    return clean_json(result)


def equity_curve_pct(
    cs: CandleSet,
    params: Any = None,
    *,
    settings: dict | None = None,
    risk_pct: float = 1.0,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.02,
) -> tuple[np.ndarray, list[float], dict]:
    """Gated NEXUS run -> (equity in % of initial per bar, per-trade account returns as fractions, stats).

    Used by Monte Carlo (synthetic markets); no ungated comparison, no JSON conversion.
    """
    settings = settings if settings is not None else current_settings()
    _, nexus_params_cls = load_engine()
    p, _ = build_nexus_params(nexus_params_cls, settings, params)
    response, _ = run_engine(cs, p)
    sim = simulate_account(
        cs, response.get("trades") or [], initial_capital=100.0, risk_pct=risk_pct, fee_pct=fee_pct,
        slippage_pct=slippage_pct,
    )
    returns = [x / 100.0 for x in sim["pnl_pcts"]]
    return np.asarray(sim["equity"], dtype=float), returns, response.get("stats") or {}


async def save_backtest(db: Any, result: dict) -> int:
    """Insert the summary row in table `backtests`; returns its id (0 when the DB is unavailable)."""
    if db is None:
        return 0
    try:
        return await db.execute(
            "INSERT INTO backtests(symbol, tf, params, metrics, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                result.get("symbol", ""),
                result.get("tf", ""),
                json_dumps(result.get("params") or {}),
                json_dumps(
                    {
                        "metrics": result.get("metrics"),
                        "ungated": result.get("ungated"),
                        "bars": result.get("bars"),
                        "start_time": result.get("start_time"),
                        "end_time": result.get("end_time"),
                    }
                ),
                now_ms(),
            ),
        )
    except Exception:
        return 0


def summarize_backtest(result: dict) -> str:
    """One Spanish line for events/terminal."""
    from app.sim.common import fmt_pct, fmt_prob

    m = result.get("metrics") or {}
    parts = [
        f"Backtest {result.get('symbol')} {result.get('tf')}: {m.get('trades', 0)} operaciones",
        f"retorno {fmt_pct(m.get('total_return_pct'))}",
        f"acierto {fmt_prob(m.get('win_rate'))}",
        f"DD máx {fmt_pct(m.get('max_dd_pct'), signed=False)}",
    ]
    lift = m.get("gate_lift_pct")
    if lift is not None and math.isfinite(lift):
        parts.append(f"el filtro aporta {fmt_pct(lift)}")
    return " · ".join(parts)
