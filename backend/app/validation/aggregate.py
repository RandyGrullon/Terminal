"""Cross-asset aggregation of validated series (per timeframe and all together), without treating correlated assets
as independent evidence.

- Intervals of the pooled expectancy resample whole TIME BLOCKS (every trade of every asset in the same period is
  drawn together), so five crypto pairs that rise and fall together count roughly as one bet repeated, not five.
- "Better than random" for a group = the excess R of each NEXUS trade over the mean R of a random entry in the same
  fold and side of its own series, with the same block interval.
- The average pairwise correlation of the daily returns gives the effective number of independent assets
  (n / (1 + (n − 1)·ρ)), reported next to the verdict.
"""
from __future__ import annotations

from typing import Any, Callable

from app.util import TIMEFRAMES
from app.validation import stats as S
from app.validation.verdict import group_verdict

BLOCKS_TARGET = 30  # aim for about this many time blocks over the tested span
DAY_S = 86400


def block_seconds(span_s: float) -> int:
    """Length of the bootstrap time blocks: span / 30, between 1 and 30 days (whole days)."""
    days = int(round(max(float(span_s), 0.0) / BLOCKS_TARGET / DAY_S))
    return int(min(max(days, 1), 30) * DAY_S)


def _mean(values: list[float]) -> float | None:
    x = S.finite_array(values)
    return float(x.mean()) if x.size else None


def _avg(series: list[dict], path: tuple[str, ...]) -> float | None:
    vals = []
    for s in series:
        v: Any = s
        for key in path:
            v = v.get(key) if isinstance(v, dict) else None
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return _mean(vals)


def _pooled(series: list[dict], key: str, blk: int, *, runs: int, seed: int) -> dict[str, Any]:
    """Pooled trades of one variant (`_trades` = NEXUS + gate, `_trades_ungated` = without the gate): mean R with
    its block and iid intervals and the excess over the random entries of the same fold and side."""
    trades = [t for s in series for t in s.get(key) or [] if t.get("r") is not None]
    rs = [float(t["r"]) for t in trades]
    blocks = [int(t["time"]) // blk for t in trades]
    ex = [(float(t["excess"]), int(t["time"]) // blk) for t in trades if t.get("excess") is not None]
    ci = S.block_bootstrap_mean_ci(rs, blocks, runs=runs, seed=seed) if rs else None
    iid = S.bootstrap_mean_ci(rs, runs=runs, seed=seed + 1)
    ex_ci = S.block_bootstrap_mean_ci([e for e, _ in ex], [b for _, b in ex], runs=runs, seed=seed + 2) if ex else None
    wins = sum(1 for r in rs if r > 0)

    def pooled_of(value: Callable[[dict], Any], salt: int) -> tuple[float | None, list[float] | None]:
        """Mean and block interval of a per-trade value (trades where it is known)."""
        pairs = [(float(v), int(t["time"]) // blk) for t in trades if (v := value(t)) is not None]
        if not pairs:
            return None, None
        got = S.block_bootstrap_mean_ci([v for v, _ in pairs], [b for _, b in pairs], runs=runs, seed=seed + salt)
        return _mean([v for v, _ in pairs]), [got[0], got[1]] if got else None

    mech_r, _ = pooled_of(lambda t: t.get("mech"), 3)
    mech_ex, mech_ex_ci = pooled_of(lambda t: t.get("mech_excess"), 4)
    # model exits − mechanical exits on the same entry (paired)
    exit_eff, exit_eff_ci = pooled_of(lambda t: None if t.get("mech") is None else float(t["r"]) - float(t["mech"]), 5)
    return {
        # same entries with the mechanical exits: the excess over random isolates the entry signal (same exits)
        "mechanical_r": mech_r,
        "mechanical_excess_r": mech_ex,
        "mechanical_excess_ci95": mech_ex_ci,
        # what NEXUS' model exits (early exit, signal, reverse) add over the mechanical ones, per trade
        "exit_effect_r": exit_eff,
        "exit_effect_ci95": exit_eff_ci,
        "trades": len(rs),
        "wins": wins,
        "win_rate": wins / len(rs) if rs else None,
        "expectancy_r": _mean(rs),
        "ci95_blocks": [ci[0], ci[1]] if ci else None,
        "ci95_iid": [iid[0], iid[1]] if iid else None,
        "blocks": len(set(blocks)),
        "excess_r": _mean([e for e, _ in ex]),
        "excess_ci95": [ex_ci[0], ex_ci[1]] if ex_ci else None,
    }


def aggregate_group(key: str, label: str, series: list[dict], *, runs: int, seed: int) -> dict[str, Any]:
    start = min(int(s["test_start_time"]) for s in series)
    end = max(int(s["end_time"]) for s in series)
    blk = block_seconds(end - start)
    main = _pooled(series, "_trades", blk, runs=runs, seed=seed)
    rs_n, mean, ci = main["trades"], main["expectancy_r"], main["ci95_blocks"]
    ex_mean, ex_ci = main["excess_r"], main["excess_ci95"]

    by_symbol: dict[str, dict[int, float]] = {}
    for s in series:  # one daily-return series per asset (the longest one)
        daily = s.get("_daily") or {}
        if len(daily) > len(by_symbol.get(s["symbol"], {})):
            by_symbol[s["symbol"]] = daily
    rho, pairs = S.mean_pairwise_correlation(list(by_symbol.values()))
    assets = len(by_symbol)
    n_eff = S.effective_count(assets, rho) if assets > 1 else float(assets)
    n_eff_known = n_eff if rho is not None else None
    positive = sum(1 for s in series if (s["overall"]["nexus"].get("expectancy_r") or 0.0) > 0)
    edge = sum(1 for s in series if s.get("verdict", {}).get("code") == "edge")
    verdict = group_verdict(rs_n, mean, ci, ex_mean, ex_ci, positive, len(series), assets, n_eff_known, rho)
    ungated = _pooled(series, "_trades_ungated", blk, runs=runs, seed=seed + 10)
    u_positive = sum(1 for s in series if (s["overall"]["ungated"].get("expectancy_r") or 0.0) > 0)
    ungated.update(positive_series=u_positive, verdict=group_verdict(
        ungated["trades"], ungated["expectancy_r"], ungated["ci95_blocks"], ungated["excess_r"],
        ungated["excess_ci95"], u_positive, len(series), assets, n_eff_known, rho))
    return {
        "key": key,
        "label": label,
        "series": len(series),
        "assets": assets,
        "symbols": sorted(by_symbol),
        "tfs": [tf for tf in TIMEFRAMES if any(s["tf"] == tf for s in series)],
        **main,
        "block_days": blk // DAY_S,
        "ungated_expectancy_r": ungated["expectancy_r"],
        "ungated": ungated,
        "nexus_return_pct_avg": _avg(series, ("overall", "nexus", "return_pct")),
        "buy_hold_return_pct_avg": _avg(series, ("overall", "buy_hold", "return_pct")),
        "random_expectancy_r_avg": _avg(series, ("overall", "random", "expectancy_r")),
        "positive_series": positive,
        "edge_series": edge,
        "avg_correlation": rho,
        "correlation_pairs": pairs,
        "effective_assets": n_eff if rho is not None else None,
        "verdict": verdict,
    }


def aggregate(series: list[dict], *, runs: int = 2000, seed: int = 7) -> list[dict[str, Any]]:
    """Groups: one per timeframe with series, then "all" when there is more than one timeframe."""
    ok = [s for s in series if s.get("overall")]
    groups: list[dict[str, Any]] = []
    for tf in TIMEFRAMES:
        members = [s for s in ok if s["tf"] == tf]
        if members:
            groups.append(aggregate_group(tf, f"Temporalidad {tf}", members, runs=runs, seed=S.seed_for("tf", tf, seed)))
    if len(groups) > 1:
        groups.append(aggregate_group("all", "Todas las series", ok, runs=runs, seed=S.seed_for("all", seed)))
    return groups
