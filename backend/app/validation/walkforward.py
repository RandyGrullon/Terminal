"""Walk-forward validation of NEXUS + Trade Gate on one candle set (pure, deterministic, CPU only).

NEXUS runs causally over the whole history (`run_nexus` learns online: each decision only uses the bars up to its
close, and the gate starts from its initial weights, without the live global state), so every decision is out of
sample. After a warm-up the decision bars are split into consecutive folds and four variants are compared on
identical risk rules (NEXUS' levels and time stop, the asset-class commission, the same slippage):

    nexus       NEXUS + Trade Gate (the product): the engine's own trades
    ungated     NEXUS without the gate (every candidate taken)
    mechanical  NEXUS + gate entries with the mechanical exits only (same exits as the random baseline)
    buy_hold    buy and hold over the same bars
    random      Monte Carlo of random entries: per fold the same number of trades and the same long/short mix as
                `nexus`, NEXUS' default levels at that bar and the mechanical exits (mechanics.py)

Each trade is assigned to the fold of its decision bar. CPU work: call `validate_series` from a worker thread.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.config import DEFAULT_SETTINGS
from app.engine.costs import asset_class, fee_pct_for
from app.engine.features import compute_features
from app.engine.levels import Levels
from app.engine.nexus import NexusParams, run_nexus
from app.sim.backtest import simulate_account
from app.util import tf_seconds
from app.validation import stats as S
from app.validation.mechanics import random_entry_table, simulate_exit, slippage_r
from app.validation.verdict import gate_effect, series_verdict

MIN_FOLD_BARS = 100  # decision bars per fold, at least
MIN_HISTORY_BARS = 300
MECHANICAL_REASONS = ("SL", "TRAIL", "TP3", "TIME")


class ValidationDataError(ValueError):
    """The series cannot be validated (Spanish message)."""


@dataclass
class WFConfig:
    folds: int = 5
    warmup_bars: int = 500
    random_runs: int = 1000
    bootstrap_runs: int = 2000
    risk_pct: float = 1.0
    slippage_pct: float = 0.02
    seed: int = 7

    def __post_init__(self) -> None:
        self.folds = int(min(max(int(self.folds), 2), 10))
        self.warmup_bars = int(min(max(int(self.warmup_bars), 100), 3000))
        self.random_runs = int(min(max(int(self.random_runs), 100), 5000))
        self.bootstrap_runs = int(min(max(int(self.bootstrap_runs), 200), 10000))
        self.risk_pct = float(min(max(float(self.risk_pct), 0.05), 10.0))
        self.slippage_pct = float(min(max(float(self.slippage_pct), 0.0), 2.0))
        self.seed = int(self.seed)

    def to_dict(self) -> dict[str, Any]:
        return {"folds": self.folds, "warmup_bars": self.warmup_bars, "random_runs": self.random_runs,
                "bootstrap_runs": self.bootstrap_runs, "risk_pct": self.risk_pct, "slippage_pct": self.slippage_pct,
                "seed": self.seed}


def count_gaps(times: np.ndarray, tf_s: int) -> int:
    """Missing bars inside the series (only meaningful for 24/7 markets such as crypto)."""
    if times.size < 2:
        return 0
    step = np.diff(times.astype(np.int64))
    return int(np.sum(np.maximum(step // max(tf_s, 1) - 1, 0)))


def series_gaps(symbol: str, times: np.ndarray, tf_s: int) -> int | None:
    """Missing bars of a 24/7 market (crypto, SIM:*); None for markets with sessions, whose nights, weekends and
    holidays are not missing data."""
    return count_gaps(times, tf_s) if asset_class(symbol) == "crypto" else None


def _side(tr: dict) -> int:
    return 1 if str(tr.get("side")).lower() == "long" else -1


def _engine_trades(trades: list[dict], times: np.ndarray, w0: int, edges: np.ndarray, slip: float) -> list[dict]:
    """Engine trades decided after the warm-up: decision / entry index, fold, R net of commissions and slippage
    (None while still open at the end of the data)."""
    out = []
    for tr in trades:
        e = int(np.searchsorted(times, int(tr.get("entry_time") or 0), side="left"))
        d = e - 1
        if d < w0 or e >= times.size:
            continue
        side = _side(tr)
        r = tr.get("r_multiple")
        closed = tr.get("exit_time") is not None and isinstance(r, (int, float)) and math.isfinite(r)
        net = None
        if closed:
            net = float(r) - slippage_r(side, float(tr["entry_price"]), float(tr["sl_initial"]), float(r), slip)
        fold = int(np.searchsorted(edges, d, side="right") - 1)
        out.append({"d": d, "e": e, "side": side, "fold": min(max(fold, 0), edges.size - 2), "r": net,
                    "reason": tr.get("exit_reason"), "raw": tr})
    return out


def _mechanical_r(f: Any, t: dict, params: NexusParams, fee: float, slip: float) -> float | None:
    tr = t["raw"]
    lv = Levels(t["side"], float(f.close[t["d"]]), float(tr["sl_initial"]), float(tr["tp1"]), float(tr["tp2"]),
                float(tr["tp3"]))
    res = simulate_exit(f, t["d"], lv, fee_pct=fee, time_stop_bars=int(params.time_stop_bars),
                        trail_atr=float(params.trail_atr))
    if res is None:
        return None
    r, _, e = res
    return r - slippage_r(t["side"], float(f.open[e]), lv.sl, r, slip)


def _random_draws(table: dict[int, np.ndarray], edges: np.ndarray, counts: dict[tuple[int, int], int], runs: int,
                  rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, dict[int, list[int]]]:
    """(R matrix runs × trades, decision bars of each draw, columns of each fold). Per fold and side: the same number
    of trades as NEXUS, drawn without replacement among the bars whose random entry has a known outcome."""
    blocks_r, blocks_d, cols = [], [], {k: [] for k in range(edges.size - 1)}
    col = 0
    for k in range(edges.size - 1):
        lo, hi = int(edges[k]), int(edges[k + 1])
        for side in (1, -1):
            m = counts.get((k, side), 0)
            if m <= 0:
                continue
            elig = np.nonzero(np.isfinite(table[side][lo:hi]))[0] + lo
            if elig.size == 0:
                continue
            if elig.size >= m:
                idx = np.argsort(rng.random((runs, elig.size)), axis=1)[:, :m]
            else:
                idx = rng.integers(0, elig.size, size=(runs, m))
            draws = elig[idx]
            blocks_d.append(draws)
            blocks_r.append(table[side][draws])
            cols[k].extend(range(col, col + m))
            col += m
    if not blocks_r:
        return np.zeros((runs, 0)), np.zeros((runs, 0), dtype=np.int64), cols
    return np.concatenate(blocks_r, axis=1), np.concatenate(blocks_d, axis=1), cols


def _random_summary(R: np.ndarray, D: np.ndarray, nexus_mean: float | None, mech_mean: float | None,
                    risk: float) -> dict[str, Any]:
    if R.shape[1] == 0:
        return {"runs": int(R.shape[0]), "trades": 0, "expectancy_r": None, "expectancy_band": None,
                "win_rate": None, "profit_factor_p50": None, "trade_max_dd_p50": None, "percentile": None,
                "p_value": None, "percentile_mechanical": None, "p_value_mechanical": None}
    st = S.run_matrix_stats(R, D, risk)
    pct, p = S.rank_percentile(nexus_mean, st["mean"])
    pct_m, p_m = S.rank_percentile(mech_mean, st["mean"])
    pf = st["profit_factor"][np.isfinite(st["profit_factor"])]
    return {
        "runs": int(R.shape[0]),
        "trades": int(R.shape[1]),
        "expectancy_r": float(st["mean"].mean()),
        "expectancy_band": S.quantiles(st["mean"]),  # p5, p50, p95 of the mean R of each random sequence
        "win_rate": float(st["win_rate"].mean()),
        "profit_factor_p50": float(np.median(pf)) if pf.size else None,
        "trade_max_dd_p50": float(np.median(st["max_dd"])),
        "percentile": pct,
        "p_value": p,
        "percentile_mechanical": pct_m,
        "p_value_mechanical": p_m,
    }


def _variant(rs: list[float], *, eq: np.ndarray | None, t_slice: np.ndarray, tf_s: int, cfg: WFConfig,
             seed: int) -> dict[str, Any]:
    out = S.r_summary(rs, risk_pct=cfg.risk_pct, runs=cfg.bootstrap_runs, seed=seed)
    if eq is not None:
        out.update(S.equity_summary(eq, t_slice, tf_s))
    return out


def validate_series(cs: Any, settings: dict | None = None, config: WFConfig | None = None) -> dict[str, Any]:
    """Validation of one (symbol, tf) candle set (closed bars only). Raises ValidationDataError.

    The result holds internal keys starting with "_" (per-trade data for the cross-asset aggregation) that
    report.py drops before the report is stored or served."""
    cfg = config or WFConfig()
    base = dict(settings if settings is not None else DEFAULT_SETTINGS)
    n = len(cs)
    if n < MIN_HISTORY_BARS:
        raise ValidationDataError(f"Historial insuficiente: {n} velas (mínimo {MIN_HISTORY_BARS})")
    fee = float(fee_pct_for(cs.symbol, base))
    base["risk.fee_pct"] = fee  # the gate's R:R and EV use the same cost as the trades
    p_gate = NexusParams.from_settings(base, gate_enabled=True)
    p_all = NexusParams.from_settings(base, gate_enabled=False)
    res_a = run_nexus(cs, p_gate)
    if not res_a.ok:
        raise ValidationDataError(res_a.message or "NEXUS no pudo analizar la serie")
    res_b = run_nexus(cs, p_all)
    f = compute_features(cs)
    times = np.asarray(cs.time, dtype=np.int64)
    tf_s = tf_seconds(cs.tf)
    w0 = max(cfg.warmup_bars, int(f.warmup) + 1)
    span = n - 1 - w0
    folds = min(cfg.folds, span // MIN_FOLD_BARS) if span > 0 else 0
    if folds < 2:
        raise ValidationDataError(
            f"Historial insuficiente: {n} velas; con {w0} de calentamiento hacen falta al menos "
            f"{w0 + 2 * MIN_FOLD_BARS + 1}")
    edges = np.round(np.linspace(w0, n - 1, folds + 1)).astype(np.int64)
    slip, risk = cfg.slippage_pct, cfg.risk_pct
    seed = S.seed_for(cs.symbol, cs.tf, cfg.seed)

    a_all = _engine_trades(res_a.trades, times, w0, edges, slip)
    b_all = _engine_trades(res_b.trades, times, w0, edges, slip)
    a_closed = [t for t in a_all if t["r"] is not None]
    b_closed = [t for t in b_all if t["r"] is not None]
    for t in a_closed + b_closed:
        t["mech"] = _mechanical_r(f, t, p_gate, fee, slip)

    eq_a = simulate_account(cs, [t["raw"] for t in a_all], risk_pct=risk, fee_pct=fee, slippage_pct=slip)["equity"]
    eq_b = simulate_account(cs, [t["raw"] for t in b_all], risk_pct=risk, fee_pct=fee, slippage_pct=slip)["equity"]
    close = np.asarray(cs.close, dtype=float)

    table = random_entry_table(f, w0, n - 1, p_gate, fee_pct=fee, slippage_pct=slip)

    def counts_of(closed: list[dict]) -> dict[tuple[int, int], int]:
        out: dict[tuple[int, int], int] = {}
        for t in closed:
            out[(t["fold"], t["side"])] = out.get((t["fold"], t["side"]), 0) + 1
        return out

    R, D, cols = _random_draws(table, edges, counts_of(a_closed), cfg.random_runs, np.random.default_rng(seed))
    # the same baseline matched to NEXUS without the gate (its own trade counts and side mix per fold)
    Rb, Db, cols_b = _random_draws(table, edges, counts_of(b_closed), cfg.random_runs,
                                   np.random.default_rng(seed + 1))
    expected = {}  # mean R of a random entry per (fold, side): the baseline of each NEXUS trade
    for k in range(folds):
        for side in (1, -1):
            seg = table[side][int(edges[k]):int(edges[k + 1])]
            seg = seg[np.isfinite(seg)]
            expected[(k, side)] = float(seg.mean()) if seg.size else None

    def block(sel: list[dict], key: str = "r") -> list[float]:
        return [t[key] for t in sel if t.get(key) is not None]

    def section(lo_bar: int, hi_bar: int, fold: int | None, salt: int) -> dict[str, Any]:
        in_fold = (lambda t: True) if fold is None else (lambda t: t["fold"] == fold)
        a_sel = [t for t in a_closed if in_fold(t)]
        b_sel = [t for t in b_closed if in_fold(t)]
        sl = slice(lo_bar - 1, hi_bar)
        t_slice = times[sl]
        nexus = _variant(block(a_sel), eq=eq_a[sl], t_slice=t_slice, tf_s=tf_s, cfg=cfg, seed=seed + salt)
        ungated = _variant(block(b_sel), eq=eq_b[sl], t_slice=t_slice, tf_s=tf_s, cfg=cfg, seed=seed + salt + 1)
        mech = _variant(block(a_sel, "mech"), eq=None, t_slice=t_slice, tf_s=tf_s, cfg=cfg, seed=seed + salt + 2)
        c = cols[fold] if fold is not None else list(range(R.shape[1]))
        rnd = _random_summary(R[:, c], D[:, c], nexus["expectancy_r"], mech["expectancy_r"], risk)
        mech_b = _variant(block(b_sel, "mech"), eq=None, t_slice=t_slice, tf_s=tf_s, cfg=cfg, seed=seed + salt + 3)
        cb = cols_b[fold] if fold is not None else list(range(Rb.shape[1]))
        rnd_b = _random_summary(Rb[:, cb], Db[:, cb], ungated["expectancy_r"], mech_b["expectancy_r"], risk)
        return {"nexus": nexus, "ungated": ungated, "mechanical": mech, "ungated_mechanical": mech_b,
                "buy_hold": S.equity_summary(close[sl], t_slice, tf_s), "random": rnd, "random_ungated": rnd_b}

    overall = section(w0, n, None, 0)
    fold_rows = []
    for k in range(folds):
        lo, hi = int(edges[k]), int(edges[k + 1])
        row = {"index": k + 1, "start_time": int(times[lo]), "end_time": int(times[hi - 1]), "bars": hi - lo}
        row.update(section(lo, hi, k, 10 * (k + 1)))
        fold_rows.append(row)

    def fold_counts(variant: str) -> tuple[int, int]:
        with_trades = [r for r in fold_rows if r[variant]["trades"] > 0]
        return sum(1 for r in with_trades if (r[variant]["expectancy_r"] or 0.0) > 0), len(with_trades)

    positive, with_trades_n = fold_counts("nexus")
    o = overall["nexus"]
    verdict = series_verdict(o["trades"], o["expectancy_r"], o["ci95"], overall["random"]["percentile"],
                             positive, with_trades_n)
    u = overall["ungated"]
    u_pos, u_with = fold_counts("ungated")
    verdict_ungated = series_verdict(u["trades"], u["expectancy_r"], u["ci95"],
                                     overall["random_ungated"]["percentile"], u_pos, u_with)
    reasons: dict[str, int] = {}
    for t in a_closed:
        reasons[str(t["reason"])] = reasons.get(str(t["reason"]), 0) + 1
    stats_a = res_a.stats or {}

    def trade_row(t: dict) -> dict[str, Any]:
        base_r = expected.get((t["fold"], t["side"]))
        mech = t.get("mech")
        return {"time": int(times[t["d"]]), "r": t["r"], "side": t["side"], "fold": t["fold"],
                "excess": (t["r"] - base_r) if base_r is not None else None, "mech": mech,
                "mech_excess": (mech - base_r) if base_r is not None and mech is not None else None}

    return {
        "symbol": cs.symbol, "tf": cs.tf, "provider": cs.provider or None, "history": cs.history, "note": cs.note,
        "bars": n, "start_time": int(times[0]), "end_time": int(times[-1]), "test_start_time": int(times[w0]),
        "warmup_bars": w0, "gaps": series_gaps(cs.symbol, times, tf_s), "fee_pct": fee, "slippage_pct": slip,
        "risk_pct": risk,
        "params": {"entry_threshold": p_gate.entry_threshold, "sl_atr": p_gate.sl_atr, "tp_r": list(p_gate.tp_r),
                   "trail_atr": p_gate.trail_atr, "time_stop_bars": p_gate.time_stop_bars,
                   "take_score": p_gate.take_score, "allow_short": p_gate.allow_short},
        "overall": overall,
        "folds": fold_rows,
        "folds_positive": positive,
        "folds_with_trades": with_trades_n,
        "folds_positive_ungated": u_pos,
        "folds_with_trades_ungated": u_with,
        "gate_effect": gate_effect(block(a_closed), block(b_closed), runs=cfg.bootstrap_runs, seed=seed + 7),
        "verdict": verdict,
        "verdict_ungated": verdict_ungated,
        "open_trades": sum(1 for t in a_all if t["r"] is None),
        "rejected_signals": int(stats_a.get("rejected_signals") or 0),  # whole run, warm-up included
        "exit_reasons": reasons,
        "side_mix": {"long": sum(1 for t in a_closed if t["side"] > 0),
                     "short": sum(1 for t in a_closed if t["side"] < 0)},
        "mechanical_exits": sum(1 for t in a_closed if t["reason"] in MECHANICAL_REASONS),
        "_trades": [trade_row(t) for t in a_closed],
        "_trades_ungated": [trade_row(t) for t in b_closed],
        "_random_means": S.run_matrix_stats(R, None, risk)["mean"] if R.shape[1] else np.zeros(0),
        "_daily": S.daily_log_returns(times, close),
    }
