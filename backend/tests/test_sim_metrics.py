"""BE-SIM: portfolio math, sizing & costs, account simulation from hand-made trades, backtest orchestration."""
from __future__ import annotations

import asyncio
import json
import math

import numpy as np
import pytest

from app.data.base import CandleSet
from app.sim import backtest as bt
from app.sim import metrics as M

T0 = 1_700_000_000 - (1_700_000_000 % 3600)


def flat_candles(n: int = 30, price: float = 100.0, highs: dict[int, float] | None = None,
                 lows: dict[int, float] | None = None, closes: dict[int, float] | None = None) -> CandleSet:
    rows = []
    for i in range(n):
        c = (closes or {}).get(i, price)
        rows.append({"time": T0 + i * 3600, "open": price, "high": max((highs or {}).get(i, price), c, price),
                     "low": min((lows or {}).get(i, price), c, price), "close": c, "volume": 10.0})
    return CandleSet.from_rows(rows, "TEST:FLAT", "1h", provider="test")


def trade(i_entry: int, i_exit: int | None, side: str = "long", entry: float = 100.0, sl: float = 98.0,
          r: float | None = None, exit_price: float | None = None, risk: float = 2.0) -> dict:
    s = 1 if side == "long" else -1
    return {
        "id": i_entry, "side": side, "entry_time": T0 + i_entry * 3600, "entry_price": entry,
        "exit_time": T0 + i_exit * 3600 if i_exit is not None else None, "exit_price": exit_price,
        "sl_initial": sl, "tp1": entry + s * risk, "tp2": entry + s * 2 * risk, "tp3": entry + s * 3 * risk,
        "exit_reason": "TP3" if i_exit is not None else "OPEN", "exit_label": None, "pnl_pct": None,
        "r_multiple": r, "bars": (i_exit - i_entry + 1) if i_exit is not None else 0, "mfe_pct": 0.0,
        "mae_pct": 0.0, "gate_score": 70.0,
    }


# ----------------------------------------------------------------------------- pure metrics


def test_drawdown_and_returns():
    eq = [100, 110, 99, 120, 90]
    dd = M.drawdown_pct(eq)
    assert np.allclose(dd, [0, 0, 10, 0, 25])
    assert M.max_drawdown_pct(eq) == pytest.approx(25.0)
    assert np.allclose(M.simple_returns([100, 110, 99]), [0.1, -0.1])
    assert M.max_drawdown_pct([]) == 0.0


def test_sharpe_sortino_cagr_calmar():
    rng = np.random.default_rng(0)
    r = 0.001 + rng.normal(0, 0.01, 2000)
    s = M.sharpe_ratio(r, 8766)
    assert s is not None and s > 0
    assert M.sharpe_ratio(np.full(10, 0.01), 252) is None  # zero variance
    assert M.sortino_ratio(np.full(10, 0.01), 252) is None  # no downside
    assert M.sortino_ratio(r, 8766) > s  # downside deviation < total deviation for a positive-drift series
    assert M.cagr_pct(100, 121, 2 * M.YEAR_S) == pytest.approx(10.0)
    assert M.cagr_pct(100, 121, 5 * 86400) is None  # too short to annualise
    assert M.cagr_pct(100, 0, 2 * M.YEAR_S) == -100.0
    assert M.calmar_ratio(10.0, 5.0) == pytest.approx(2.0)
    assert M.calmar_ratio(None, 5.0) is None and M.calmar_ratio(10.0, 0.0) is None


def test_periods_per_year_uses_empirical_rate_for_session_markets():
    t_crypto = np.arange(0, 3600 * 24 * 60, 3600)
    assert M.periods_per_year(t_crypto, 3600) == pytest.approx(M.YEAR_S / 3600, rel=0.02)
    t_stock = np.array([d * 86400 + h * 3600 for d in range(0, 90) if d % 7 < 5 for h in range(7)])
    assert M.periods_per_year(t_stock, 3600) < 0.3 * M.YEAR_S / 3600


def test_trade_summary_and_streaks():
    s = M.trade_summary([10, -5, 20, -5], [1.0, -0.5, 2.0, -0.5], [1.0, -1.0, 2.0, -1.0], [3, 4, 5, 6])
    assert s["trades"] == 4 and s["win_rate"] == 0.5
    assert s["profit_factor"] == pytest.approx(3.0)
    assert s["avg_win_pct"] == pytest.approx(1.5) and s["avg_loss_pct"] == pytest.approx(-0.5)
    assert s["expectancy_pct"] == pytest.approx(0.5) and s["avg_r"] == pytest.approx(0.25)
    assert s["best_trade_pct"] == 2.0 and s["worst_trade_pct"] == -0.5 and s["avg_bars_in_trade"] == 4.5
    assert M.max_consecutive_losses([1, -1, -2, 3, -1, -1, -1, 2]) == 3
    empty = M.trade_summary([], [])
    assert empty["trades"] == 0 and empty["win_rate"] is None and empty["profit_factor"] is None
    assert M.trade_summary([5, 6], [0.5, 0.6])["profit_factor"] is None  # no losses: undefined, not infinite


def test_position_size_fees_and_slippage():
    assert M.position_size(10000, 1.0, 100, 98) == pytest.approx(50.0)
    assert M.position_size(10000, 1.0, 100, 102) == pytest.approx(50.0)  # shorts: distance only
    capped = M.position_size(10000, 1.0, 100, 99.99, max_leverage=5.0)
    assert capped == pytest.approx(500.0)  # 5x notional cap
    assert M.position_size(10000, 1.0, 100, 100) == 0.0
    assert M.position_size(0, 1.0, 100, 98) == 0.0
    assert M.trade_cost(10000, 0.1, 0.02) == pytest.approx(12.0)
    assert M.slipped_price(100, 1, 0.1, opening=True) == pytest.approx(100.1)
    assert M.slipped_price(100, 1, 0.1, opening=False) == pytest.approx(99.9)
    assert M.slipped_price(100, -1, 0.1, opening=True) == pytest.approx(99.9)
    assert M.slipped_price(100, -1, 0.1, opening=False) == pytest.approx(100.1)
    assert M.r_multiple(1, 100, 104, 98) == pytest.approx(2.0)
    assert M.r_multiple(-1, 100, 104, 102) == pytest.approx(-2.0)


# ----------------------------------------------------------------------------- account simulation


def test_simulate_account_single_long_trade_sizing_and_costs():
    cs = flat_candles(20)
    sim = bt.simulate_account(cs, [trade(5, 10, r=2.0, exit_price=104.0)], initial_capital=10000, risk_pct=1.0,
                              fee_pct=0.1, slippage_pct=0.02)
    # qty = 1% of 10000 / 2 = 50 ; gross = 2R × 50 × 2 = 200 ; costs = 0.12 % of (5000 + 5200)
    expected_costs = 50 * 100 * 0.0012 + 50 * 104 * 0.0012
    assert sim["costs"] == pytest.approx(expected_costs)
    assert sim["equity"][-1] == pytest.approx(10000 + 200 - expected_costs)
    assert sim["equity"][4] == 10000  # flat before the entry
    assert sim["equity"][5] == pytest.approx(10000 - 50 * 100 * 0.0012)  # entry costs paid
    assert sim["in_position"].sum() == 6
    t = sim["trades"][0]
    assert t["qty"] == pytest.approx(50.0) and t["pnl"] == pytest.approx(200 - expected_costs, abs=0.01)
    assert t["pnl_account_pct"] == pytest.approx((200 - expected_costs) / 100, abs=1e-3)


def test_simulate_account_partial_tp_mark_to_market_and_residual():
    # TP1 (102) is touched at bar 7; engine result: 1/3 at +1R and the rest at break-even -> r = 1/3
    cs = flat_candles(20, highs={7: 102.5})
    sim = bt.simulate_account(cs, [trade(5, 10, r=1 / 3, exit_price=100.0)], fee_pct=0.0, slippage_pct=0.0)
    eq = sim["equity"]
    assert eq[6] == pytest.approx(10000)
    assert eq[7] == pytest.approx(10000 + 50 * 2 / 3)  # 1/3 realised at TP1
    assert eq[-1] == pytest.approx(10000 + 50 * 2 / 3)  # the residual keeps the engine total exact
    assert sim["trades"][0]["r_multiple"] == pytest.approx(1 / 3, abs=1e-4)


def test_simulate_account_short_open_and_overlap():
    cs = flat_candles(30, closes={29: 96.0}, lows={29: 96.0})
    trades = [
        trade(2, 6, side="short", sl=102.0, r=1.0, exit_price=98.0),
        trade(4, 8, side="long", r=1.0),  # overlaps the short -> skipped
        trade(6, 9, side="long", r=-1.0, exit_price=98.0),  # re-entry on the exit bar is allowed
        trade(20, None, side="short", sl=102.0, risk=10.0),  # open at the end (targets out of reach): MTM
    ]
    sim = bt.simulate_account(cs, trades, fee_pct=0.0, slippage_pct=0.0)
    assert len(sim["trades"]) == 3
    assert sim["pnls"][0] == pytest.approx(100.0)  # +1R short (qty 50 × 2)
    assert sim["pnls"][1] == pytest.approx(-101.0)  # -1R with qty sized from equity 10100
    open_trade = sim["trades"][-1]
    assert open_trade["exit_reason"] == "OPEN"
    assert open_trade["r_multiple"] == pytest.approx(2.0)  # (100 - 96) / 2
    assert sim["equity"][-1] == pytest.approx(10000 + 100 - 101 + sim["pnls"][2])


def test_simulate_account_ignores_invalid_trades():
    cs = flat_candles(10)
    bad = [trade(2, 4, sl=101.0), {"side": "up"}, trade(50, 60)]  # stop on the wrong side / bad side / no data
    sim = bt.simulate_account(cs, bad)
    assert sim["trades"] == [] and np.all(sim["equity"] == 10000)


def test_compute_metrics_fields():
    cs = flat_candles(40, closes={39: 110.0}, highs={39: 110.0})
    sim = bt.simulate_account(cs, [trade(5, 10, r=2.0), trade(12, 15, r=-1.0), trade(20, 25, r=1.5)])
    m = bt.compute_metrics(cs, sim, 10000)
    for key in ("initial_capital", "final_equity", "total_return_pct", "cagr_pct", "buy_hold_return_pct", "trades",
                "win_rate", "profit_factor", "avg_win_pct", "avg_loss_pct", "expectancy_pct", "avg_r", "max_dd_pct",
                "sharpe", "sortino", "calmar", "exposure_pct", "avg_bars_in_trade", "best_trade_pct",
                "worst_trade_pct", "max_consecutive_losses", "fees_paid", "rejected_signals",
                "rejected_would_win_rate", "gate_lift_pct"):
        assert key in m
    assert m["trades"] == 3 and m["win_rate"] == pytest.approx(2 / 3)
    assert m["buy_hold_return_pct"] == pytest.approx(10.0)
    assert m["cagr_pct"] is None  # 40 hours: too short to annualise
    assert 0 < m["exposure_pct"] < 100 and m["max_consecutive_losses"] == 1


# ----------------------------------------------------------------------------- backtest orchestration (fake engine)


class _FakeParams:
    def __init__(self, gate_enabled: bool):
        self.gate_enabled = gate_enabled
        self.horizon = 5

    @classmethod
    def from_settings(cls, settings: dict, **kw):
        if kw:
            raise TypeError(f"unexpected {sorted(kw)}")
        return cls(bool(settings.get("gate.enabled", True)))


def _fake_engine(monkeypatch):
    def run_engine(cs, params, prior=None):
        good = [trade(160, 170, r=2.0), trade(180, 190, r=1.0)]
        bad = [trade(200, 205, r=-1.0), trade(210, 215, r=-1.0)]
        trades = good if params.gate_enabled else good + bad
        stats = {"rejected_signals": 2 if params.gate_enabled else 0,
                 "rejected_would_win_rate": 0.0 if params.gate_enabled else None}
        markers = [{"time": int(cs.time[160]), "kind": "BUY", "position": "belowBar", "shape": "arrowUp",
                    "color": "#26a69a", "text": "COMPRAR"}]
        return {"trades": trades, "markers": markers, "stats": stats}, None

    monkeypatch.setattr(bt, "load_engine", lambda: (None, _FakeParams))
    monkeypatch.setattr(bt, "run_engine", run_engine)


def test_run_backtest_gated_vs_ungated(monkeypatch):
    _fake_engine(monkeypatch)
    cs = flat_candles(260)
    res = bt.run_backtest(cs, {"nexus.sl_atr": 2.5, "unknown_param": 1}, settings=None)
    json.dumps(res, allow_nan=False)
    assert set(res) >= {"symbol", "tf", "bars", "start_time", "end_time", "params", "metrics", "ungated", "equity",
                        "benchmark", "trades", "markers", "candles"}
    m, u = res["metrics"], res["ungated"]
    assert m["trades"] == 2 and u["trades"] == 4
    assert m["rejected_signals"] == 2 and m["rejected_would_win_rate"] == 0.0
    assert m["gate_lift_pct"] == pytest.approx(m["total_return_pct"] - u["total_return_pct"])
    assert m["gate_lift_pct"] > 0 and u["gate_lift_pct"] is None
    assert len(res["equity"]) == res["bars"] == len(res["benchmark"]) == len(res["candles"]) == 260
    assert res["params"]["ignored_params"] == ["unknown_param"]
    assert res["equity"][0]["drawdown_pct"] == 0.0
    no_gate = bt.run_backtest(cs, use_gate=False)
    assert no_gate["ungated"] is None and no_gate["metrics"]["gate_lift_pct"] is None
    assert no_gate["metrics"]["trades"] == 4


def test_run_backtest_validates_input(monkeypatch):
    _fake_engine(monkeypatch)
    with pytest.raises(bt.BacktestError):
        bt.run_backtest(flat_candles(50))
    with pytest.raises(bt.BacktestError):
        bt.run_backtest(flat_candles(300), initial_capital=0)
    with pytest.raises(ValueError):
        bt.run_backtest(flat_candles(300), {"nexus.horizon": "abc"})


def test_save_backtest_row(runtime_factory, monkeypatch):
    _fake_engine(monkeypatch)
    res = bt.run_backtest(flat_candles(260), include_candles=False)
    assert res["candles"] == []

    async def scenario():
        async with runtime_factory() as rt:
            rid = await bt.save_backtest(rt.db, res)
            assert rid > 0
            row = await rt.db.fetchone("SELECT symbol, tf, metrics FROM backtests WHERE id=?", (rid,))
            assert row["symbol"] == "TEST:FLAT" and json.loads(row["metrics"])["metrics"]["trades"] == 2

    asyncio.run(scenario())
    assert "Backtest TEST:FLAT 1h: 2 operaciones" in bt.summarize_backtest(res)


# ----------------------------------------------------------------------------- real engine (when available)


def test_real_engine_backtest_is_causal(candles):
    pytest.importorskip("app.engine.nexus")
    full = bt.run_backtest(candles, include_candles=False)
    assert full["bars"] == len(candles)
    assert all(math.isfinite(p["equity"]) for p in full["equity"])
    k = 900
    prefix = bt.run_backtest(candles.upto(k), include_candles=False, compare_ungated=False)
    cutoff = int(candles.time[k]) - 3600
    closed_full = [(t["entry_time"], t["exit_time"], t["side"]) for t in full["trades"]
                   if t.get("exit_time") is not None and t["exit_time"] <= cutoff]
    closed_prefix = [(t["entry_time"], t["exit_time"], t["side"]) for t in prefix["trades"]
                     if t.get("exit_time") is not None and t["exit_time"] <= cutoff]
    assert closed_full == closed_prefix  # nothing that happened after bar k changed earlier trades
