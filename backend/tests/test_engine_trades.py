"""Trade accounting: SL-first tie-break, partial exits, break-even, chandelier trailing, fees, early exit."""
from __future__ import annotations

import numpy as np
import pytest

from app.engine.levels import Levels, ev_hold, first_passage, plan_ev, plan_levels
from app.engine.nexus import NexusParams
from app.engine.nexus_loop import NexusRun
from app.engine.positions import Position
from app.sim.synthetic import generate_candleset

FEE = 0.1


def long_position() -> Position:
    lv = Levels(1, 100.0, 98.0, 102.0, 104.0, 106.0)
    return Position(1, lv, entry_idx=10, entry_time=1000, entry_price=100.0, gate_score=80.0, entry_regime=0,
                    decision_idx=9)


def test_sl_first_when_both_levels_touched():
    pos = long_position()
    events = pos.process_bar(100.0, 103.0, 97.5)  # touches TP1 (102) and SL (98) in the same bar
    assert events == [("STOP", 98.0)] and not pos.is_open
    pnl_pct, r = pos.result(None, FEE)
    assert pnl_pct == pytest.approx(-2.0 - FEE / 100 * (100 + 98) / 100 * 100)
    assert r == pytest.approx(pnl_pct / 100 * 100 / 2.0)
    trade = pos.to_trade(2000, 98.0, "SL", None, None, FEE)
    assert trade["exit_reason"] == "SL" and trade["exit_label"] == "Stop loss" and trade["r_multiple"] < -1.0


def test_gap_through_stop_fills_at_open():
    pos = long_position()
    assert pos.process_bar(97.0, 97.5, 96.0) == [("STOP", 97.0)]
    assert pos.fills == [(1.0, 97.0)]


def test_partials_break_even_and_trailing():
    pos = long_position()
    assert pos.process_bar(100.2, 102.5, 99.5) == [("TP1", 102.0)]
    assert pos.tp_hits == 1 and pos.sl == 100.0 and pos.size_left == pytest.approx(2 / 3)
    assert pos.stop_kind() == "BE"
    assert pos.process_bar(102.4, 104.2, 101.8) == [("TP2", 104.0)]
    pos.update_trail(atr=1.0, trail_atr=3.0)
    assert pos.trail == pytest.approx(104.2 - 3.0) and pos.stop == pytest.approx(101.2)
    assert pos.stop_kind() == "TRAIL"
    assert pos.process_bar(104.0, 104.1, 101.0) == [("STOP", pytest.approx(101.2))]
    gross = (2.0 + 4.0 + 1.2) / 3
    fees = FEE / 100 * (100 + (102 + 104 + 101.2) / 3)
    pnl_pct, r = pos.result(None, FEE)
    assert pnl_pct == pytest.approx(gross - fees)
    assert r == pytest.approx((gross - fees) / 2.0)
    mfe, mae = pos.excursions()
    assert mfe == pytest.approx(4.2) and mae == pytest.approx(-0.5)


def test_tp3_closes_everything():
    pos = long_position()
    events = pos.process_bar(100.0, 107.0, 99.9)  # one huge bar: TP1, TP2, TP3
    assert [e[0] for e in events] == ["TP1", "TP2", "TP3"] and not pos.is_open
    assert sum(f for f, _ in pos.fills) == pytest.approx(1.0)
    assert pos.result(None, 0.0)[1] == pytest.approx((2 + 4 + 6) / 3 / 2)


def test_short_position_mirror():
    lv = Levels(-1, 100.0, 102.0, 98.0, 96.0, 94.0)
    pos = Position(2, lv, 5, 500, 100.0, None, 1, 4)
    assert pos.process_bar(99.8, 100.5, 97.9) == [("TP1", 98.0)]
    assert pos.sl == 100.0
    assert pos.process_bar(99.0, 100.2, 98.5) == [("STOP", 100.0)]
    pnl_pct, r = pos.result(None, 0.0)
    assert pnl_pct == pytest.approx(2.0 / 3) and r == pytest.approx(1.0 / 3)


def test_plan_levels_and_ev():
    lv = plan_levels(1, 100.0, atr=1.0, swing=97.0, sl_atr=2.0, tp_r=(1.0, 2.0, 3.0))
    assert lv.sl == pytest.approx(97.0 - 0.25) and lv.tp2 == pytest.approx(100 + 2 * 3.25)
    far = plan_levels(1, 100.0, atr=1.0, swing=80.0, sl_atr=2.0, tp_r=(1.0, 2.0, 3.0))
    assert far.sl == pytest.approx(98.0)  # a swing too far away is ignored
    assert first_passage(1.0, 1.0, 0.0) == pytest.approx(0.5)
    assert first_passage(1.0, 3.0, 0.0) == pytest.approx(0.75)
    assert first_passage(1.0, 1.0, 2.0) > 0.5 > first_passage(1.0, 1.0, -2.0)
    neutral = plan_ev(Levels(1, 100, 98, 102, 104, 106), 0.5, 0.0, 1.0, 3.0, 0.0)
    good = plan_ev(Levels(1, 100, 98, 102, 104, 106), 0.65, 0.5, 1.0, 3.0, 0.0)
    assert good.ev_r > neutral.ev_r and good.rr == pytest.approx(2.0)


def test_ev_hold_is_zero_without_edge_and_negative_against():
    ev0, p0 = ev_hold(1, 100.0, 98.0, 102.0, 2.0, 0.5, 1.0, 5)
    assert ev0 == pytest.approx(0.0, abs=1e-9) and p0 == pytest.approx(0.5)
    ev_bad, _ = ev_hold(1, 100.0, 98.0, 102.0, 2.0, 0.3, 1.0, 5)
    ev_good, _ = ev_hold(1, 100.0, 98.0, 102.0, 2.0, 0.7, 1.0, 5)
    assert ev_bad < 0 < ev_good
    near_tp, _ = ev_hold(1, 101.8, 98.0, 102.0, 2.0, 0.5, 1.0, 5)
    assert near_tp == pytest.approx(0.0, abs=1e-9)  # no edge -> no early exit just because the target is near


def test_early_exit_rule_on_live_run():
    """Force an adverse state on an open long: EV of holding < −0.1R with the score against -> EXIT_EARLY."""
    cs = generate_candleset(n=400, seed=21, tf="1h", end_time=1_760_000_400)
    run = NexusRun(cs, NexusParams(), None, None)
    run.run()
    i = run.n - 1
    c = float(cs.close[i])
    lv = Levels(1, c, c - 2.0, c + 2.0, c + 4.0, c + 6.0)
    pos = Position(99, lv, i - 3, int(cs.time[i - 3]), c, 70.0, 0, i - 4)
    pos.bars = 3
    run.prob[i] = 0.25
    run.score[i] = -0.4
    run.score[i - 1] = 0.2
    reason = run._exit_reason(i, pos)
    assert reason is not None and reason[0] == "EARLY" and "negativo" in reason[1]
    run.prob[i] = 0.75
    run.score[i] = 0.5
    assert run._exit_reason(i, pos) is None
    pos.bars = run.p.time_stop_bars
    assert run._exit_reason(i, pos)[0] == "TIME"


def test_trades_respect_levels_and_fees():
    cs = generate_candleset(n=1200, seed=3, tf="1h", end_time=1_760_000_400)
    res = NexusRun(cs, NexusParams(fee_pct=0.2), None, None).run()
    closed = [t for t in res.trades if t["exit_time"] is not None]
    assert closed
    times = list(res.bars["time"])
    for t in closed:
        s = 1 if t["side"] == "long" else -1
        assert s * (t["entry_price"] - t["sl_initial"]) > 0
        assert s * (t["tp1"] - t["entry_price"]) > 0 or t["tp1"] == t["tp2"]
        assert t["exit_time"] >= t["entry_time"]
        assert t["exit_reason"] in ("SL", "TRAIL", "TP3", "EARLY", "REVERSE", "TIME", "SIGNAL")
        i0 = times.index(t["entry_time"])
        assert t["entry_price"] == pytest.approx(cs.open[i0]) or i0 == len(times) - 1
        if t["exit_reason"] == "SL" and t["exit_label"] == "Stop loss":
            assert t["r_multiple"] < -0.99  # full loss plus fees
    total = np.prod([1 + t["pnl_pct"] / 100 for t in closed]) - 1
    assert res.stats["total_return_pct"] == pytest.approx(total * 100, abs=1e-3)
    assert res.stats["trades"] == len(closed)


def test_several_targets_in_one_bar_close_once():
    """TP2 and TP3 touched by the same bar: two TP markers, exactly one closed trade (regression test)."""
    cs = generate_candleset(n=300, seed=21, tf="1h", end_time=1_760_000_400)
    run = NexusRun(cs, NexusParams(), None, None)
    i = 250
    pos = long_position()
    pos.process_bar(100.0, 102.5, 99.5)  # TP1 earlier
    run.pos = pos
    run.f.open[i], run.f.high[i], run.f.low[i], run.f.close[i] = 103.0, 107.0, 102.8, 106.5
    run.f.close[i - 1] = 103.0
    n_trades = len(run.trades)
    run._intrabar(i)
    assert run.pos is None and len(run.trades) == n_trades + 1
    assert run.trades[-1]["exit_reason"] == "TP3" and run.trades[-1]["exit_price"] == pytest.approx(106.0)
    kinds = [m["kind"] for m in run.markers if m["time"] == int(cs.time[i])]
    assert kinds == ["TP2", "TP3"]
