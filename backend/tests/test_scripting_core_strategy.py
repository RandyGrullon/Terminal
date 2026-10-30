"""PyChart strategy backtester: fills, intrabar stop/limit rules, sizing, fees, metrics and causality."""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.scripting.runtime import Bars, ScriptContext, build_namespace, collect_result, make_print
from app.scripting.validator import build_code
from app.sim.synthetic import generate_candleset

T0 = 1_700_000_000 - 1_700_000_000 % 3600
METRIC_KEYS = {
    "initial_capital", "final_equity", "total_return_pct", "cagr_pct", "buy_hold_return_pct", "trades", "win_rate",
    "profit_factor", "avg_win_pct", "avg_loss_pct", "expectancy_pct", "avg_r", "max_dd_pct", "sharpe", "sortino",
    "calmar", "exposure_pct", "avg_bars_in_trade", "best_trade_pct", "worst_trade_pct", "max_consecutive_losses",
    "fees_paid", "rejected_signals", "rejected_would_win_rate", "gate_lift_pct",
}


def make_bars(n: int = 12, overrides: dict[int, tuple[float, float, float, float]] | None = None) -> Bars:
    """Flat market (o=c=100, h=101, l=99) with per-bar (o, h, l, c) overrides."""
    rows = [(100.0, 101.0, 99.0, 100.0)] * n
    for i, row in (overrides or {}).items():
        rows[i] = row
    o, h, l, c = (np.array(col, dtype=float) for col in zip(*rows))
    return Bars(T0 + 3600 * np.arange(n), o, h, l, c, np.full(n, 1000.0))


def run(code: str, bars: Bars) -> dict:
    code_obj, errors = build_code(code)
    assert not errors, errors
    ctx = ScriptContext(bars, {}, symbol="SIM:TEST", tf="1h")
    ns = build_namespace(ctx)
    ns.update(np=np, math=math, print=make_print(ctx.stdout))
    exec(code_obj, ns)
    result = collect_result(ctx)
    assert result["strategy"] is not None
    return result["strategy"]


HEADER = "strategy('T', initial_capital=10000, risk_pct=1, fee_pct=0)\n"


def test_signal_fills_next_open_and_stop_intrabar_with_risk_sizing():
    bars = make_bars(overrides={3: (100.5, 101.0, 99.0, 100.0), 6: (100.0, 100.5, 96.0, 97.5)})
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2)\nstrategy.exit('SL', 'L', stop=97)\n", bars)
    (tr,) = st["trades"]
    assert tr["side"] == "long" and tr["entry_time"] == T0 + 3 * 3600 and tr["entry_price"] == 100.5
    assert tr["exit_time"] == T0 + 6 * 3600 and tr["exit_price"] == 97.0 and tr["exit_reason"] == "SL"
    assert tr["sl_initial"] == 97.0 and tr["bars"] == 4
    assert tr["account_pct"] == pytest.approx(-1.0, abs=1e-6)  # risk_pct = 1 % of equity at the stop
    assert tr["r_multiple"] == pytest.approx(-1.0, abs=1e-6)
    kinds = [m["kind"] for m in st["markers"]]
    assert kinds == ["BUY", "EXIT_SL"]
    assert st["markers"][0]["time"] == T0 + 3 * 3600


def test_stop_wins_when_stop_and_limit_touch_the_same_bar():
    bars = make_bars(overrides={5: (100.0, 104.0, 96.0, 100.0)})
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2)\nstrategy.exit('x', 'L', stop=97, limit=103)\n", bars)
    assert st["trades"][0]["exit_reason"] == "SL" and st["trades"][0]["exit_price"] == 97.0


def test_gap_through_stop_fills_at_open():
    bars = make_bars(overrides={5: (95.0, 96.0, 94.0, 95.5)})
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2)\nstrategy.exit('x', 'L', stop=97, limit=103)\n", bars)
    tr = st["trades"][0]
    assert (tr["exit_reason"], tr["exit_price"], tr["exit_time"]) == ("SL", 95.0, T0 + 5 * 3600)


def test_limit_take_profit_and_short_mirror():
    bars = make_bars(overrides={5: (100.0, 104.0, 99.5, 103.5)})
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2, limit=103, stop=97)\n", bars)
    tr = st["trades"][0]
    assert (tr["exit_reason"], tr["exit_price"], tr["tp1"]) == ("TP1", 103.0, 103.0)
    assert tr["pnl"] > 0 and st["markers"][-1]["kind"] == "TP1"
    bars = make_bars(overrides={6: (100.0, 104.0, 99.5, 103.0)})
    st = run(HEADER + "strategy.entry('S', strategy.short, when=bar_index == 2)\nstrategy.exit('x', 'S', stop=103, limit=95)\n", bars)
    tr = st["trades"][0]
    assert (tr["side"], tr["exit_reason"], tr["exit_price"]) == ("short", "SL", 103.0)
    assert tr["pnl"] < 0 and st["markers"][0]["kind"] == "SELL"


def test_reverse_close_and_no_pyramiding():
    st = run(HEADER + "strategy.entry('L', strategy.long, when=(bar_index == 2) | (bar_index == 4))\n"
             "strategy.entry('S', strategy.short, when=bar_index == 6)\n", make_bars(n=12))
    first, second = st["trades"]
    assert (first["side"], first["entry_time"], first["exit_reason"]) == ("long", T0 + 3 * 3600, "REVERSE")
    assert first["exit_time"] == T0 + 7 * 3600 == second["entry_time"]
    assert (second["side"], second["exit_reason"], second["exit_time"]) == ("short", "OPEN", None)
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2)\nstrategy.close('L', when=bar_index == 5)\n",
             make_bars(n=12))
    (tr,) = st["trades"]
    assert (tr["exit_reason"], tr["exit_time"], tr["exit_price"]) == ("SIGNAL", T0 + 6 * 3600, 100.0)


def test_fees_qty_pct_and_metrics_shape():
    code = ("strategy('F', initial_capital=10000, fee_pct=0.1)\n"
            "strategy.entry('L', strategy.long, when=bar_index == 2, qty_pct=50)\n"
            "strategy.close_all(when=bar_index == 5)\n")
    st = run(code, make_bars(n=10))
    (tr,) = st["trades"]
    assert tr["qty"] == pytest.approx(50.0) and tr["pnl"] == pytest.approx(-10.0)
    m = st["metrics"]
    assert set(m) == METRIC_KEYS
    assert m["fees_paid"] == pytest.approx(10.0) and m["final_equity"] == pytest.approx(9990.0)
    assert m["trades"] == 1 and m["win_rate"] == 0.0 and m["max_consecutive_losses"] == 1
    assert m["profit_factor"] == 0.0 and m["gate_lift_pct"] is None and m["rejected_signals"] == 0
    assert len(st["equity"]) == 10 and len(st["benchmark"]) == 10 and st["candles"] == [] and st["ungated"] is None
    assert set(st["equity"][0]) == {"time", "equity", "drawdown_pct"}
    assert st["params"]["fee_pct"] == pytest.approx(0.1) and st["params"]["notes"]


def test_open_trade_is_marked_to_market_but_not_counted():
    bars = make_bars(n=8, overrides={7: (100.0, 106.0, 99.5, 105.0)})
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2)\n", bars)
    (tr,) = st["trades"]
    assert tr["exit_reason"] == "OPEN" and tr["exit_label"] == "Abierta" and tr["exit_price"] is None
    assert st["metrics"]["trades"] == 0 and st["metrics"]["win_rate"] is None
    assert st["metrics"]["final_equity"] == pytest.approx(10000 + 100 * 5.0)  # 100 % of equity without a stop


def test_atr_trailing_stop_locks_profit():
    n = 40
    rows = {}
    for i in range(n - 1):
        c = 100.0 + i
        rows[i] = (c - 1.0 if i else c, c + 0.5, c - 0.5, c)
    rows[n - 1] = (138.0, 138.5, 120.0, 121.0)
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 20)\nstrategy.exit('T', 'L', trail_atr=2)\n",
             make_bars(n=n, overrides=rows))
    tr = st["trades"][0]
    assert tr["exit_reason"] == "TRAIL" and tr["exit_time"] == T0 + (n - 1) * 3600
    assert tr["exit_price"] > tr["entry_price"] and tr["pnl"] > 0
    assert st["markers"][-1]["kind"] == "EXIT_TRAIL"


def test_exit_levels_fixed_by_default_and_updated_on_request():
    n = 30
    rows = {i: (100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i) for i in range(n)}
    rows[25] = (125.0, 125.5, 100.0, 101.0)
    script = HEADER + "strategy.entry('L', strategy.long, when=bar_index == 5)\nstrategy.exit('x', 'L', stop=low - 1{})\n"
    fixed = run(script.format(""), make_bars(n=n, overrides=rows))["trades"][0]
    moving = run(script.format(", update=True"), make_bars(n=n, overrides=rows))["trades"][0]
    assert fixed["exit_price"] == pytest.approx(103.0)  # low[5] - 1, captured on the signal bar
    assert moving["exit_price"] == pytest.approx(122.0)  # low[24] - 1, refreshed every bar
    assert fixed["sl_initial"] == moving["sl_initial"] == pytest.approx(103.0)


def test_strategy_has_no_lookahead():
    cs = generate_candleset(n=900, seed=5, tf="1h", end_time=1_760_000_400)
    code = ("strategy('EMA', initial_capital=10000, risk_pct=1, fee_pct=0.1, slippage_pct=0.02)\n"
            "f, s = ta.ema(close, 9), ta.ema(close, 21)\natr = ta.atr(14)\n"
            "strategy.entry('L', strategy.long, when=ta.crossover(f, s))\n"
            "strategy.entry('S', strategy.short, when=ta.crossunder(f, s))\n"
            "strategy.exit('xl', 'L', stop=close - 2 * atr, limit=close + 3 * atr)\n"
            "strategy.exit('xs', 'S', stop=close + 2 * atr, trail_atr=2.5)\n")

    def bars(k: int) -> Bars:
        return Bars(cs.time[:k], cs.open[:k], cs.high[:k], cs.low[:k], cs.close[:k], cs.volume[:k])

    full = run(code, bars(len(cs)))
    cut = 600
    part = run(code, bars(cut))
    last = int(cs.time[cut - 1])
    done_full = [t for t in full["trades"] if t["exit_time"] is not None and t["exit_time"] < last]
    done_part = [t for t in part["trades"] if t["exit_time"] is not None and t["exit_time"] < last]
    assert len(done_part) >= 5 and done_full == done_part
    eq_full = [p["equity"] for p in full["equity"][: cut - 1]]
    eq_part = [p["equity"] for p in part["equity"][: cut - 1]]
    assert eq_full == eq_part
    assert full["metrics"]["trades"] > part["metrics"]["trades"] > 0
