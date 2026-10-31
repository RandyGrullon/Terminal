"""Validation feature: mechanical exits of the baselines (the engine's own Position rules) and the random-entry table.

The key guarantee: for every NEXUS trade that ended on a mechanical rule (stop, break-even, trailing stop, TP3 or the
time stop) the mechanical simulator reproduces the engine's R multiple and exit bar exactly, so the random baseline
really uses the same risk rules as the product."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from app.config import DEFAULT_SETTINGS
from app.engine.features import compute_features
from app.engine.levels import Levels
from app.engine.nexus import NexusParams, run_nexus
from app.sim.synthetic import generate_candleset
from app.validation.mechanics import random_entry_table, simulate_exit, slippage_r
from app.validation.walkforward import MECHANICAL_REASONS

FEE = 0.1


def bars(rows: list[tuple[float, float, float, float]], atr: float = 1.0) -> SimpleNamespace:
    """Minimal feature object: (open, high, low, close) rows, hourly times, constant ATR."""
    a = np.asarray(rows, dtype=float)
    n = a.shape[0]
    return SimpleNamespace(n=n, time=np.arange(n, dtype=np.int64) * 3600, open=a[:, 0], high=a[:, 1], low=a[:, 2],
                           close=a[:, 3], atr=np.full(n, atr))


def fee_r(entry: float, exits_notional: float, risk: float) -> float:
    return FEE / 100 * (entry + exits_notional) / risk


LONG = Levels(1, 100.0, 98.0, 102.0, 104.0, 106.0)  # R = 2


def test_stop_loss_first_and_fees_in_r():
    f = bars([(100, 100, 100, 100), (100, 101, 97, 98)])  # decision at bar 0, entry at open[1] = 100
    r, exit_idx, entry_idx = simulate_exit(f, 0, LONG, fee_pct=FEE, time_stop_bars=48, trail_atr=3.0)
    assert (entry_idx, exit_idx) == (1, 1)
    assert r == pytest.approx(-1.0 - fee_r(100, 98, 2.0))


def test_gap_through_the_stop_voids_the_entry():
    f = bars([(100, 100, 100, 100), (97.5, 99, 97, 98)])
    assert simulate_exit(f, 0, LONG, fee_pct=FEE, time_stop_bars=48, trail_atr=3.0) is None


def test_tp1_then_break_even():
    f = bars([(100, 100, 100, 100), (100, 102.5, 99.5, 101), (101, 101.5, 99.5, 100.5)])
    r, exit_idx, _ = simulate_exit(f, 0, LONG, fee_pct=FEE, time_stop_bars=48, trail_atr=3.0)
    assert exit_idx == 2  # the stop moved to 100 from the bar after TP1
    gross = (2.0 / 3.0) / 2.0  # 1/3 closed at +2, the rest at break-even
    assert r == pytest.approx(gross - fee_r(100, 102 / 3 + 2 * 100 / 3, 2.0))


def test_all_targets_in_one_bar():
    f = bars([(100, 100, 100, 100), (100, 107, 99.5, 106)])
    r, exit_idx, _ = simulate_exit(f, 0, LONG, fee_pct=FEE, time_stop_bars=48, trail_atr=3.0)
    assert exit_idx == 1
    assert r == pytest.approx((2 + 4 + 6) / 3 / 2 - fee_r(100, (102 + 104 + 106) / 3, 2.0))


def test_time_stop_closes_at_the_next_open():
    rows = [(100, 100, 100, 100)] + [(100, 100.5, 99.5, 100.2)] * 5 + [(100.4, 100.6, 99.8, 100)]
    f = bars(rows)
    r, exit_idx, entry_idx = simulate_exit(f, 0, LONG, fee_pct=FEE, time_stop_bars=5, trail_atr=3.0)
    assert entry_idx == 1 and exit_idx == 6  # 5 bars without TP1 -> out at open[6]
    assert r == pytest.approx(0.4 / 2.0 - fee_r(100, 100.4, 2.0))


def test_unknown_outcome_when_the_data_ends():
    f = bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    assert simulate_exit(f, 0, LONG, fee_pct=FEE, time_stop_bars=48, trail_atr=3.0) is None
    assert simulate_exit(f, 2, LONG, fee_pct=FEE, time_stop_bars=48, trail_atr=3.0) is None  # no next bar


def test_short_side_mirror():
    short = Levels(-1, 100.0, 102.0, 98.0, 96.0, 94.0)
    f = bars([(100, 100, 100, 100), (100, 101, 93, 94)])
    r, _, _ = simulate_exit(f, 0, short, fee_pct=FEE, time_stop_bars=48, trail_atr=3.0)
    assert r == pytest.approx(2.0 - fee_r(100, 96, 2.0))


def test_slippage_r():
    assert slippage_r(1, 100.0, 98.0, -1.0, 0.0) == 0.0
    # entry 100 + average exit 98 (a -1R loss), 0.05 % per fill, risk 2
    assert slippage_r(1, 100.0, 98.0, -1.0, 0.05) == pytest.approx(0.05 / 100 * 198 / 2)
    assert slippage_r(-1, 100.0, 102.0, 1.0, 0.05) == pytest.approx(0.05 / 100 * 198 / 2)


@pytest.mark.parametrize("seed", [3, 11])
def test_mechanical_exits_reproduce_the_engine_trades(seed):
    cs = generate_candleset(n=3000, seed=seed, tf="1h", end_time=1_760_000_400)
    p = NexusParams.from_settings(DEFAULT_SETTINGS, gate_enabled=False)
    res = run_nexus(cs, p)
    f = compute_features(cs)
    times = np.asarray(cs.time)
    checked = 0
    for tr in res.trades:
        if tr["exit_time"] is None or tr["exit_reason"] not in MECHANICAL_REASONS:
            continue
        side = 1 if tr["side"] == "long" else -1
        d = int(np.searchsorted(times, tr["entry_time"])) - 1
        lv = Levels(side, float(f.close[d]), tr["sl_initial"], tr["tp1"], tr["tp2"], tr["tp3"])
        out = simulate_exit(f, d, lv, fee_pct=p.fee_pct, time_stop_bars=p.time_stop_bars, trail_atr=p.trail_atr)
        assert out is not None
        assert out[0] == pytest.approx(tr["r_multiple"], abs=1e-3)
        assert int(times[out[1]]) == tr["exit_time"]
        checked += 1
    assert checked >= 30


def test_random_entry_table_is_causal_and_charges_slippage():
    cs = generate_candleset(n=1200, seed=5, tf="1h", end_time=1_760_000_400)
    p = NexusParams.from_settings(DEFAULT_SETTINGS)
    f = compute_features(cs)
    full = random_entry_table(f, 200, len(cs) - 1, p, fee_pct=0.1, slippage_pct=0.0)
    assert set(full) == {1, -1}
    assert np.isnan(full[1][:200]).all() and np.isfinite(full[1]).sum() > 800
    # the outcome of an entry known before the cut never depends on the bars after it
    cut = 900
    ft = compute_features(cs.slice(0, cut))
    trunc = random_entry_table(ft, 200, cut - 1, p, fee_pct=0.1, slippage_pct=0.0)
    for side in (1, -1):
        known = np.isfinite(trunc[side])
        assert known.sum() > 500
        assert np.allclose(trunc[side][known], full[side][:cut][known])
    slipped = random_entry_table(f, 200, len(cs) - 1, p, fee_pct=0.1, slippage_pct=0.05)
    ok = np.isfinite(full[1]) & np.isfinite(slipped[1])
    assert (slipped[1][ok] < full[1][ok]).all()
