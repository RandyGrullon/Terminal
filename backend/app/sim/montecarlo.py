"""Monte Carlo analysis (MonteCarloResult).

- bootstrap: resample (with replacement) the per-trade account returns of a NEXUS backtest of the symbol → many
  alternative orderings / mixes of the same edge → distribution of final return and drawdown.
- synthetic: run NEXUS on N synthetic markets (different seeds and regimes) → how the strategy behaves on markets
  it has never seen. Cost is capped (≤ 60 markets × ≤ 800 bars, plus a time budget).

Everything is seeded and deterministic. Equity is expressed in % of the initial capital (100 = flat).
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np

from app.data.base import CandleSet
from app.sim.backtest import equity_curve_pct
from app.sim.common import DISCLAIMER, current_settings, fmt_num, fmt_pct, fmt_prob
from app.sim.synthetic import generate_candleset
from app.util import clean_json, normalize_tf

MAX_BOOTSTRAP_PATHS = 5000
MIN_BOOTSTRAP_TRADES = 5
MAX_SYNTH_RUNS = 60
MIN_SYNTH_RUNS = 5
MAX_SYNTH_BARS = 800
MIN_SYNTH_BARS = 300
SYNTH_TIME_BUDGET_S = 60.0
MAX_POINTS = 200
MAX_SAMPLES = 20
RUIN_DD_PCT = 50.0
SYNTH_REGIMES = ("auto", "bull", "bear", "range", "volatile")
SYNTH_END_TIME = 1_760_000_400  # fixed clock so synthetic runs are reproducible


class MonteCarloError(ValueError):
    """Invalid Monte Carlo request (Spanish message)."""


# ----------------------------------------------------------------------------- pure core


def bootstrap_paths(returns: Any, paths: int, steps: int, seed: int) -> np.ndarray:
    """Equity paths (paths × steps+1, % of initial) compounding resampled per-trade returns (fractions)."""
    r = np.clip(np.asarray(list(returns), dtype=float), -1.0, None)
    r = r[np.isfinite(r)]
    if r.size == 0 or paths <= 0 or steps <= 0:
        raise MonteCarloError("No hay rendimientos para remuestrear")
    rng = np.random.default_rng(int(seed))
    idx = rng.integers(0, r.size, size=(int(paths), int(steps)))
    eq = np.empty((int(paths), int(steps) + 1), dtype=float)
    eq[:, 0] = 100.0
    eq[:, 1:] = 100.0 * np.cumprod(1.0 + r[idx], axis=1)
    return eq


def path_drawdowns(eq: np.ndarray) -> np.ndarray:
    """Max drawdown (%) of every path."""
    peaks = np.maximum.accumulate(eq, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (peaks - eq) / peaks * 100.0, 0.0)
    return np.clip(np.nan_to_num(dd, nan=0.0), 0.0, 100.0).max(axis=1)


def sample_steps(n_points: int, max_points: int = MAX_POINTS) -> np.ndarray:
    if n_points <= max_points:
        return np.arange(n_points)
    return np.unique(np.round(np.linspace(0, n_points - 1, max_points)).astype(int))


def summarize_paths(
    eq: np.ndarray,
    *,
    mode: str,
    notes: list[str] | None = None,
    max_points: int = MAX_POINTS,
    max_samples: int = MAX_SAMPLES,
) -> dict[str, Any]:
    """MonteCarloResult from an equity matrix (paths × points, % of initial)."""
    eq = np.asarray(eq, dtype=float)
    if eq.ndim != 2 or eq.shape[0] == 0 or eq.shape[1] < 2:
        raise MonteCarloError("Simulación vacía")
    n_paths, n_points = eq.shape
    idx = sample_steps(n_points, max_points)
    q = np.percentile(eq[:, idx], [5, 25, 50, 75, 95], axis=0)
    percentiles = [
        {
            "step": int(s),
            "p5": round(float(q[0, j]), 3),
            "p25": round(float(q[1, j]), 3),
            "p50": round(float(q[2, j]), 3),
            "p75": round(float(q[3, j]), 3),
            "p95": round(float(q[4, j]), 3),
        }
        for j, s in enumerate(idx)
    ]
    final = eq[:, -1] - 100.0
    max_dd = path_drawdowns(eq)
    var95 = float(np.percentile(final, 5))
    tail = final[final <= var95]
    cvar95 = float(tail.mean()) if tail.size else var95
    order = np.argsort(final, kind="stable")
    k = min(n_paths, max_samples)
    picks = order[np.unique(np.round(np.linspace(0, n_paths - 1, k)).astype(int))]
    result = {
        "mode": mode,
        "paths": int(n_paths),
        "steps": int(n_points - 1),
        "percentiles": percentiles,
        "final_returns_pct": np.round(final, 4).tolist(),
        "max_drawdowns_pct": np.round(max_dd, 4).tolist(),
        "prob_profit": float((final > 0).mean()),
        "prob_ruin": float((max_dd >= RUIN_DD_PCT).mean()),
        "median_return_pct": float(np.median(final)),
        "expected_return_pct": float(final.mean()),
        "var95_pct": var95,
        "cvar95_pct": cvar95,
        "sample_paths": [np.round(eq[i, idx], 3).tolist() for i in picks],
        "notes": list(notes or []),
    }
    return result


def _summary_notes(res: dict, initial_capital: float) -> list[str]:
    median_money = initial_capital * (1 + res["median_return_pct"] / 100.0)
    return [
        f"Probabilidad de terminar en ganancia: {fmt_prob(res['prob_profit'])} · retorno mediano "
        f"{fmt_pct(res['median_return_pct'])} (capital {fmt_num(initial_capital, 0)} → {fmt_num(median_money, 0)}).",
        f"VaR 95 %: en el 5 % de los peores escenarios el resultado es {fmt_pct(res['var95_pct'])} o peor "
        f"(media de esa cola, CVaR: {fmt_pct(res['cvar95_pct'])}).",
        f"Riesgo de ruina (caída ≥ {RUIN_DD_PCT:.0f} % desde un máximo): {fmt_prob(res['prob_ruin'], 1)}.",
        DISCLAIMER,
    ]


# ----------------------------------------------------------------------------- modes (CPU: run in a thread)


def run_bootstrap(
    cs: CandleSet,
    *,
    paths: int = 300,
    seed: int = 1,
    initial_capital: float = 10000.0,
    settings: dict | None = None,
    risk_pct: float | None = None,
) -> dict[str, Any]:
    settings = settings if settings is not None else current_settings()
    min_bars = int(settings.get("nexus.min_bars", 150) or 150)
    if len(cs) < min_bars + 50:
        raise MonteCarloError(
            f"Historial insuficiente para el backtest base: {len(cs)} velas (mínimo {min_bars + 50})"
        )
    risk = float(risk_pct if risk_pct is not None else settings.get("risk.per_trade_pct", 1.0))
    _, returns, _ = equity_curve_pct(
        cs,
        settings=settings,
        risk_pct=risk,
        fee_pct=float(settings.get("risk.fee_pct", 0.1)),
        slippage_pct=float(settings.get("risk.slippage_pct", 0.02)),
    )
    if len(returns) < MIN_BOOTSTRAP_TRADES:
        raise MonteCarloError(
            f"El backtest de {cs.symbol} {cs.tf} solo generó {len(returns)} operaciones: se necesitan al menos "
            f"{MIN_BOOTSTRAP_TRADES} para un Monte Carlo fiable (prueba con más velas o el modo sintético)"
        )
    n_paths = int(min(max(int(paths), 50), MAX_BOOTSTRAP_PATHS))
    eq = bootstrap_paths(returns, n_paths, len(returns), seed)
    res = summarize_paths(eq, mode="bootstrap")
    wins = sum(1 for r in returns if r > 0)
    res["notes"] = [
        f"Bootstrap de {len(returns)} operaciones del backtest NEXUS de {cs.symbol} {cs.tf} ({len(cs)} velas, "
        f"{wins} ganadoras), remuestreadas {n_paths} veces con riesgo {fmt_num(risk, 2)} % por operación.",
        "Cada paso del abanico es una operación; el orden y la mezcla cambian en cada camino.",
        *_summary_notes(res, initial_capital),
    ]
    return clean_json(res)


def run_synthetic(
    *,
    paths: int = 30,
    bars: int = 500,
    seed: int = 1,
    tf: str = "1h",
    initial_capital: float = 10000.0,
    settings: dict | None = None,
    time_budget_s: float = SYNTH_TIME_BUDGET_S,
) -> dict[str, Any]:
    settings = settings if settings is not None else current_settings()
    tf = normalize_tf(tf)
    n_runs = int(min(max(int(paths), MIN_SYNTH_RUNS), MAX_SYNTH_RUNS))
    n_bars = int(min(max(int(bars), MIN_SYNTH_BARS), MAX_SYNTH_BARS))
    risk = float(settings.get("risk.per_trade_pct", 1.0))
    started = time.monotonic()
    curves: list[np.ndarray] = []
    trades_total = 0
    regimes_used: dict[str, int] = {}
    truncated = False
    for i in range(n_runs):
        if len(curves) >= MIN_SYNTH_RUNS and time.monotonic() - started > time_budget_s:
            truncated = True
            break
        regime = SYNTH_REGIMES[i % len(SYNTH_REGIMES)]
        cs = generate_candleset(
            n=n_bars, seed=int(seed) * 1000 + i, tf=tf, regime=regime, end_time=SYNTH_END_TIME,
            start_price=100.0,
        )
        curve, returns, _ = equity_curve_pct(
            cs,
            settings=settings,
            risk_pct=risk,
            fee_pct=float(settings.get("risk.fee_pct", 0.1)),
            slippage_pct=float(settings.get("risk.slippage_pct", 0.02)),
        )
        curves.append(curve)
        trades_total += len(returns)
        regimes_used[regime] = regimes_used.get(regime, 0) + 1
    eq = np.vstack(curves)
    res = summarize_paths(eq, mode="synthetic")
    regime_txt = ", ".join(f"{k} ×{v}" for k, v in regimes_used.items())
    notes = [
        f"NEXUS ejecutado en {len(curves)} mercados sintéticos de {n_bars} velas {tf} (regímenes: {regime_txt}); "
        f"{trades_total} operaciones en total, riesgo {fmt_num(risk, 2)} % por operación.",
        "Cada paso del abanico es una vela; las primeras velas son calentamiento del modelo (sin operaciones).",
    ]
    if truncated:
        notes.append(f"Se detuvo por tiempo tras {len(curves)} de {n_runs} mercados para mantener la respuesta ágil.")
    res["notes"] = notes + _summary_notes(res, initial_capital)
    return clean_json(res)


async def run_montecarlo(
    rt: Any,
    *,
    mode: str = "bootstrap",
    symbol: str | None = None,
    tf: str = "1h",
    paths: int = 300,
    bars: int = 500,
    seed: int = 1,
    initial_capital: float = 10000.0,
) -> dict[str, Any]:
    """Async orchestration (I/O on the loop, CPU in a worker thread)."""
    import asyncio

    mode = (mode or "bootstrap").lower()
    if mode not in ("bootstrap", "synthetic"):
        raise MonteCarloError("Modo no válido: usa 'bootstrap' o 'synthetic'")
    if initial_capital <= 0:
        raise MonteCarloError("El capital inicial debe ser mayor que 0")
    tf = normalize_tf(tf or "1h")
    settings = current_settings()
    if mode == "synthetic":
        return await asyncio.to_thread(
            run_synthetic, paths=paths, bars=bars, seed=seed, tf=tf, initial_capital=initial_capital,
            settings=settings,
        )
    if not symbol:
        raise MonteCarloError("El modo bootstrap necesita un símbolo")
    if rt is None or rt.hub is None:
        raise MonteCarloError("Servicio de datos no disponible")
    limit = int(min(max(int(bars), 1000), 5000))
    try:
        cs = await rt.hub.get_candles(symbol, tf, limit)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # ProviderError (invalid input) or an unexpected provider failure
        raise MonteCarloError(f"No se pudieron obtener las velas de {symbol} {tf}: {exc}") from None
    if cs is None or len(cs) == 0:
        note = getattr(cs, "note", None) if cs is not None else None
        raise MonteCarloError(f"Sin velas para {symbol} {tf}" + (f": {note}" if note else ""))
    return await asyncio.to_thread(
        run_bootstrap, cs, paths=paths, seed=seed, initial_capital=initial_capital, settings=settings
    )
