"""BE-SIM: Monte Carlo (bootstrap determinism, percentile fan, risk statistics, synthetic mode caps)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.sim import montecarlo as mc
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

RETURNS = [0.02, -0.01, 0.015, -0.01, 0.03, -0.012, 0.01, -0.008, 0.025, -0.01]


def test_bootstrap_is_deterministic_per_seed():
    a = mc.bootstrap_paths(RETURNS, 200, 30, seed=7)
    b = mc.bootstrap_paths(RETURNS, 200, 30, seed=7)
    c = mc.bootstrap_paths(RETURNS, 200, 30, seed=8)
    assert a.shape == (200, 31)
    assert np.array_equal(a, b) and not np.array_equal(a, c)
    assert np.all(a[:, 0] == 100.0)
    with pytest.raises(mc.MonteCarloError):
        mc.bootstrap_paths([], 10, 10, seed=1)


def test_summary_percentiles_are_monotonic_and_stats_consistent():
    eq = mc.bootstrap_paths(RETURNS, 500, 40, seed=3)
    res = mc.summarize_paths(eq, mode="bootstrap")
    json.dumps(res, allow_nan=False)
    assert res["paths"] == 500 and res["steps"] == 40
    for p in res["percentiles"]:
        assert p["p5"] <= p["p25"] <= p["p50"] <= p["p75"] <= p["p95"]
    assert res["percentiles"][0]["p50"] == 100.0 and res["percentiles"][0]["step"] == 0
    assert res["percentiles"][-1]["step"] == 40
    assert len(res["final_returns_pct"]) == len(res["max_drawdowns_pct"]) == 500
    assert 0.0 <= res["prob_profit"] <= 1.0 and 0.0 <= res["prob_ruin"] <= 1.0
    assert res["cvar95_pct"] <= res["var95_pct"] <= res["median_return_pct"]
    assert 1 <= len(res["sample_paths"]) <= 20
    assert all(len(sp) == len(res["percentiles"]) for sp in res["sample_paths"])
    finals = sorted(sp[-1] for sp in res["sample_paths"])
    assert finals[0] <= res["percentiles"][-1]["p5"] + 1e-6 and finals[-1] >= res["percentiles"][-1]["p95"] - 1e-6


def test_ruin_probability_and_downsampling():
    eq = mc.bootstrap_paths([-0.2], 10, 5, seed=1)  # every trade loses 20 % -> drawdown > 50 %
    res = mc.summarize_paths(eq, mode="bootstrap")
    assert res["prob_ruin"] == 1.0 and res["prob_profit"] == 0.0
    long_eq = mc.bootstrap_paths(RETURNS, 20, 999, seed=2)
    res2 = mc.summarize_paths(long_eq, mode="bootstrap", max_points=200)
    assert len(res2["percentiles"]) <= 200 and res2["percentiles"][-1]["step"] == 999


def test_run_bootstrap_uses_backtest_trades(monkeypatch):
    calls = []

    def fake_curve(cs, params=None, **kw):
        calls.append(len(cs))
        return np.full(len(cs), 100.0), list(RETURNS) * 2, {}

    monkeypatch.setattr(mc, "equity_curve_pct", fake_curve)
    cs = generate_candleset(n=400, seed=1, end_time=FIXED_END)
    a = mc.run_bootstrap(cs, paths=100, seed=5)
    b = mc.run_bootstrap(cs, paths=100, seed=5)
    assert a == b and a["mode"] == "bootstrap" and a["steps"] == 20 and a["paths"] == 100
    assert any("Bootstrap de 20 operaciones" in n for n in a["notes"])
    assert calls == [400, 400]


def test_run_bootstrap_rejects_too_few_trades(monkeypatch):
    monkeypatch.setattr(mc, "equity_curve_pct", lambda cs, params=None, **kw: (np.full(len(cs), 100.0), [0.01], {}))
    cs = generate_candleset(n=400, seed=1, end_time=FIXED_END)
    with pytest.raises(mc.MonteCarloError):
        mc.run_bootstrap(cs, paths=100, seed=1)
    with pytest.raises(mc.MonteCarloError):
        mc.run_bootstrap(generate_candleset(n=100, seed=1, end_time=FIXED_END), paths=100)


def test_run_synthetic_caps_and_determinism(monkeypatch):
    seen = []

    def fake_curve(cs, params=None, **kw):
        seen.append((len(cs), float(cs.close[-1])))
        growth = np.linspace(100.0, 100.0 + (cs.close[-1] - cs.close[0]) / 10.0, len(cs))
        return growth, [0.01, -0.005], {}

    monkeypatch.setattr(mc, "equity_curve_pct", fake_curve)
    res = mc.run_synthetic(paths=500, bars=5000, seed=2)
    assert res["mode"] == "synthetic" and res["paths"] == mc.MAX_SYNTH_RUNS
    assert all(n == mc.MAX_SYNTH_BARS for n, _ in seen)
    assert res["steps"] == mc.MAX_SYNTH_BARS - 1
    seen.clear()
    res2 = mc.run_synthetic(paths=500, bars=5000, seed=2)
    assert res2 == res
    small = mc.run_synthetic(paths=1, bars=10, seed=2)
    assert small["paths"] == mc.MIN_SYNTH_RUNS and small["steps"] == mc.MIN_SYNTH_BARS - 1


def test_run_montecarlo_validation():
    import asyncio

    with pytest.raises(mc.MonteCarloError):
        asyncio.run(mc.run_montecarlo(None, mode="nope"))
    with pytest.raises(mc.MonteCarloError):
        asyncio.run(mc.run_montecarlo(None, mode="bootstrap", symbol=None))


def test_real_engine_synthetic_montecarlo():
    from tests.test_sim_fakes import require_engine

    require_engine()
    res = mc.run_synthetic(paths=5, bars=300, seed=1)
    assert res["paths"] == 5 and len(res["percentiles"]) > 10
    json.dumps(res, allow_nan=False)
