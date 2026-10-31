"""Validation feature: pure statistics (bootstrap intervals, block bootstrap across correlated assets, percentiles,
profit factor, drawdowns, Monte Carlo matrix stats, correlation / effective number of assets)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.validation import stats as S


def test_bootstrap_mean_ci_is_deterministic_and_brackets_the_mean():
    rng = np.random.default_rng(1)
    x = rng.normal(0.1, 1.0, 200)
    a = S.bootstrap_mean_ci(x, runs=2000, seed=5)
    b = S.bootstrap_mean_ci(x, runs=2000, seed=5)
    assert a == b
    assert a[0] < x.mean() < a[1]
    se = x.std(ddof=1) / math.sqrt(x.size)
    assert a[1] - a[0] == pytest.approx(2 * 1.96 * se, rel=0.15)
    assert S.bootstrap_mean_ci([1.0], runs=500) is None
    assert S.bootstrap_mean_ci([None, float("nan"), 2.0], runs=500) is None  # one finite value


def test_block_bootstrap_does_not_count_correlated_copies_as_independent():
    rng = np.random.default_rng(3)
    base = rng.normal(0.05, 1.0, 60)
    copies = np.tile(base, 5)  # five perfectly correlated "assets": the same trade outcome in the same period
    blocks = np.tile(np.arange(base.size), 5)
    iid_copies = S.bootstrap_mean_ci(copies, runs=3000, seed=1)
    block = S.block_bootstrap_mean_ci(copies, blocks, runs=3000, seed=1)
    iid_base = S.bootstrap_mean_ci(base, runs=3000, seed=1)
    w = lambda ci: ci[1] - ci[0]  # noqa: E731
    assert w(block) == pytest.approx(w(iid_base), rel=0.15)  # one bet repeated, not five
    assert w(iid_copies) < 0.6 * w(block)  # treating the copies as independent would overstate the certainty
    assert S.block_bootstrap_mean_ci(copies, np.tile(np.arange(5), 60), runs=500) is None  # < 8 blocks
    with pytest.raises(ValueError):
        S.block_bootstrap_mean_ci([1.0, 2.0], [1], runs=100)


def test_bootstrap_diff_ci():
    a = np.full(50, 0.5) + np.linspace(-0.1, 0.1, 50)
    b = np.zeros(50) + np.linspace(-0.1, 0.1, 50)
    lo, hi = S.bootstrap_diff_ci(a, b, runs=1000, seed=2)
    assert lo > 0.4 and hi < 0.6
    assert S.bootstrap_diff_ci([1.0], b, runs=100) is None


def test_rank_percentile_and_p_value():
    dist = np.arange(100, dtype=float)  # 0..99
    pct, p = S.rank_percentile(1000.0, dist)
    assert pct == 100.0 and p == pytest.approx(1 / 101)
    pct, p = S.rank_percentile(-5.0, dist)
    assert pct == 0.0 and p == 1.0
    pct, _ = S.rank_percentile(50.0, dist)  # 50 below, one tie counted half
    assert pct == pytest.approx(50.5)
    assert S.rank_percentile(None, dist) == (None, None)
    assert S.rank_percentile(1.0, []) == (None, None)


def test_profit_factor_and_drawdown_from_r():
    assert S.profit_factor([1.0, -0.5, 2.0, -1.0]) == pytest.approx(2.0)
    assert S.profit_factor([1.0, 2.0]) is None  # no losing trade: undefined, never "infinite"
    assert S.profit_factor([]) is None
    # 1 % risk: two full losses then a 2R win -> peak 100, trough 98.01
    assert S.max_dd_from_r([-1.0, -1.0, 2.0], 1.0) == pytest.approx(1.99, abs=1e-6)
    eq = S.equity_from_r([1.0, -1.0], 2.0)
    assert eq.tolist() == pytest.approx([100.0, 102.0, 99.96])


def test_r_summary_fields():
    out = S.r_summary([1.0, -1.0, 0.5, -0.2], risk_pct=1.0, runs=500, seed=1)
    assert out["trades"] == 4 and out["wins"] == 2 and out["win_rate"] == 0.5
    assert out["expectancy_r"] == pytest.approx(0.075)
    assert out["profit_factor"] == pytest.approx(1.5 / 1.2)
    assert out["total_r"] == pytest.approx(0.3)
    assert out["ci95"] is None  # 4 trades: no interval (fewer than MIN_CI_N)
    ten = S.r_summary([1.0, -1.0, 0.5, -0.2] * 2 + [0.3, -0.4], risk_pct=1.0, runs=500, seed=1)
    assert ten["trades"] == S.MIN_CI_N == 10
    assert len(ten["ci95"]) == 2 and ten["ci95"][0] <= ten["expectancy_r"] <= ten["ci95"][1]
    empty = S.r_summary([], risk_pct=1.0, runs=500, seed=1)
    assert empty["trades"] == 0 and empty["expectancy_r"] is None and empty["ci95"] is None


def test_no_bootstrap_interval_for_tiny_samples():
    """A percentile bootstrap of a handful of trades is far narrower than the real uncertainty: below MIN_CI_N
    values every interval of the validation is None (series, periods, gate effect, pooled groups)."""
    nine = [-1.2, -0.7, 0.4, -1.0, 2.1, -0.3, -0.9, 0.2, -1.1]
    ten = nine + [0.5]
    assert S.bootstrap_mean_ci(nine, runs=500, seed=1) is None
    assert S.bootstrap_mean_ci(ten, runs=500, seed=1) is not None
    assert S.bootstrap_mean_ci(nine, runs=500, seed=1, min_n=2) is not None  # the primitive still works on request
    assert S.bootstrap_diff_ci(nine, ten * 5, runs=500, seed=1) is None
    assert S.bootstrap_diff_ci(ten, ten * 5, runs=500, seed=1) is not None
    blocks9, blocks10 = list(range(9)), list(range(10))
    assert S.block_bootstrap_mean_ci(nine, blocks9, runs=500, seed=1) is None
    assert S.block_bootstrap_mean_ci(ten, blocks10, runs=500, seed=1) is not None


def test_run_matrix_stats_orders_trades_before_the_drawdown():
    R = np.array([[1.0, -1.0, -1.0], [-1.0, 1.0, 1.0]])
    order = np.array([[2, 1, 0], [0, 1, 2]])  # row 0 in time order: -1, -1, +1
    st = S.run_matrix_stats(R, order, 1.0)
    assert st["mean"].tolist() == pytest.approx([-1 / 3, 1 / 3])
    assert st["win_rate"].tolist() == pytest.approx([1 / 3, 2 / 3])
    assert st["profit_factor"].tolist() == pytest.approx([0.5, 2.0])
    assert st["max_dd"][0] == pytest.approx(1.99, abs=1e-6)
    assert st["max_dd"][1] == pytest.approx(1.0, abs=1e-6)
    no_loss = S.run_matrix_stats(np.array([[1.0, 2.0]]), None, 1.0)
    assert math.isnan(no_loss["profit_factor"][0])


def test_equity_summary():
    times = np.arange(0, 3600 * 100, 3600)
    flat = S.equity_summary(np.full(100, 50.0), times, 3600)
    assert flat["return_pct"] == 0.0 and flat["max_dd_pct"] == 0.0 and flat["sharpe"] is None
    up = S.equity_summary(np.linspace(100, 110, 100) + np.sin(np.arange(100)), times, 3600)
    assert up["return_pct"] > 0 and up["sharpe"] is not None
    assert S.equity_summary(np.array([1.0]), times[:1], 3600)["return_pct"] is None


def test_correlation_and_effective_assets():
    day = 86400
    t = np.arange(0, 120 * day, 3600)
    rng = np.random.default_rng(4)
    a = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, t.size)))
    b = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, t.size)))
    da, db = S.daily_log_returns(t, a), S.daily_log_returns(t, b)
    assert len(da) == 119
    rho_same, pairs = S.mean_pairwise_correlation([da, da, da])
    assert rho_same == pytest.approx(1.0) and pairs == 3
    rho_ind, _ = S.mean_pairwise_correlation([da, db])
    assert abs(rho_ind) < 0.3
    assert S.mean_pairwise_correlation([da]) == (None, 0)
    assert S.effective_count(5, 1.0) == pytest.approx(1.0)
    assert S.effective_count(5, 0.0) == pytest.approx(5.0)
    assert S.effective_count(5, 0.75) == pytest.approx(1.25)
    assert S.effective_count(5, -0.3) == pytest.approx(5.0)  # negative correlation is not credited
    assert S.effective_count(3, None) == 3.0


def test_seed_for_is_stable():
    assert S.seed_for("BINANCE:BTCUSDT", "1h", 7) == S.seed_for("BINANCE:BTCUSDT", "1h", 7)
    assert S.seed_for("BINANCE:BTCUSDT", "1h", 7) != S.seed_for("BINANCE:ETHUSDT", "1h", 7)
    assert 0 <= S.seed_for("x") < 2 ** 31
