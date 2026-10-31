"""Regression tests (engine fixes): honest NexusStats (Sharpe over every bar after the warm-up and net of fees,
rejected_resolved), one gate label for the in-run learner (TP1 before SL within the time stop), per-asset-class
commission in NEXUS params and a gate that does not veto a quiet market only because of the commission, and the
exit policy (a negative EV of holding is an amber "vigila la salida", never a close instruction)."""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import DEFAULT_SETTINGS
from app.engine.gate import evaluate_gate
from app.engine.levels import Levels
from app.engine.nexus import NexusParams
from app.engine.nexus_current import build_advice, build_warnings, position_state
from app.engine.nexus_loop import NexusRun
from app.engine.nexus_out import compute_stats, empty_stats
from app.engine.positions import Position
from app.engine.service import NexusService
from app.sim.metrics import periods_per_year, sharpe_ratio
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_engine_gate import favorable_ctx

# ----------------------------------------------------------------------------- statistics


def test_sharpe_counts_flat_bars_and_rejected_resolved_is_exposed():
    rng = np.random.default_rng(3)
    rets = np.full(500, np.nan)
    rets[100:] = 0.0  # 400 bars after the warm-up, flat unless in a trade
    rets[100:140] = rng.normal(0.002, 0.004, 40)  # 10 % exposure
    times = np.arange(500, dtype=np.int64) * 3600
    stats = compute_stats([], [True, False, None, True], rets, 3600, times)
    expected = sharpe_ratio(rets[100:], periods_per_year(times[100:], 3600))
    assert stats["sharpe"] == pytest.approx(expected, abs=1e-3)
    active_only = float(np.mean(rets[100:140]) / np.std(rets[100:140]) * math.sqrt(365 * 24))  # the old figure
    assert abs(stats["sharpe"]) < abs(active_only) / 2
    assert stats["rejected_signals"] == 4 and stats["rejected_resolved"] == 3
    assert stats["rejected_would_win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert empty_stats()["rejected_resolved"] == 0 and empty_stats()["sharpe"] is None


def test_terminal_rejected_line_shows_the_resolved_sample():
    from app.engine.commands import _rejected_text

    assert _rejected_text({"rejected_signals": 0}) == "0"
    assert _rejected_text({"rejected_signals": 5, "rejected_resolved": 0}) == "5 (ninguna resuelta todavía)"
    small = _rejected_text({"rejected_signals": 27, "rejected_resolved": 26, "rejected_would_win_rate": 0.4615})
    assert small.startswith("27 (resueltas 26: el 46 % habría alcanzado TP1") and "muestra pequeña" in small
    big = _rejected_text({"rejected_signals": 40, "rejected_resolved": 35, "rejected_would_win_rate": 0.5})
    assert "muestra" not in big and "resueltas 35" in big


def test_engine_bar_returns_are_net_of_fees_and_cover_every_bar():
    cs = generate_candleset(n=900, seed=3, tf="1h", end_time=FIXED_END)
    # gate off: the same trades at every commission, so only the accounting changes
    free = NexusRun(cs, NexusParams(gate_enabled=False, fee_pct=0.0), None, None)
    paid = NexusRun(cs, NexusParams(gate_enabled=False, fee_pct=0.3), None, None)
    res_free, res_paid = free.run(), paid.run()
    assert [t["entry_time"] for t in res_free.trades] == [t["entry_time"] for t in res_paid.trades]
    warm = free.f.warmup
    assert np.isfinite(paid.bar_ret[warm:]).all() and np.isnan(paid.bar_ret[:warm]).all()
    assert (paid.bar_ret[warm:] == 0.0).any()  # flat bars count as 0
    fees = sum((a["pnl_pct"] - b["pnl_pct"]) / 100.0 for a, b in zip(res_free.trades, res_paid.trades))
    assert fees > 0.01
    # the commissions booked on the bars are exactly the trades' (pnl_pct is rounded to 4 decimals)
    booked = float(np.sum(free.bar_ret[warm:]) - np.sum(paid.bar_ret[warm:]))
    assert booked == pytest.approx(fees, abs=1e-6 * len(res_paid.trades))
    assert res_paid.stats["sharpe"] < res_free.stats["sharpe"]


# ----------------------------------------------------------------------------- gate label


def test_in_run_label_is_tp1_before_sl_within_the_time_stop():
    cs = generate_candleset(n=300, seed=21, tf="1h", end_time=FIXED_END)
    run = NexusRun(cs, NexusParams(time_stop_bars=10), None, None)
    f, i = run.f, 200
    c = float(f.close[i])
    f.high[i + 1 : i + 11] = c * 1.001
    f.low[i + 1 : i + 11] = c * 0.999
    f.close[i + 10] = c * 1.0008  # time stop with a positive close: still not "TP1 before SL" -> loss
    assert run.outcome(i, 1, sl=c * 0.98, tp1=c * 1.02) == (i + 10, False)
    assert run.outcome(i, -1, sl=c * 1.02, tp1=c * 0.98) == (i + 10, False)
    f.high[i + 4] = c * 1.03  # TP1 touched first
    assert run.outcome(i, 1, sl=c * 0.98, tp1=c * 1.02) == (i + 4, True)
    f.low[i + 4] = c * 0.97  # SL in the same bar: the stop counts first
    assert run.outcome(i, 1, sl=c * 0.98, tp1=c * 1.02) == (i + 4, False)
    assert run.outcome(run.n - 3, 1, sl=0.0, tp1=1e12) is None  # horizon not covered yet


# ----------------------------------------------------------------------------- commissions


class _Settings:
    def __init__(self, values: dict) -> None:
        self.values = values

    def all(self) -> dict:
        return dict(self.values)


def test_params_use_the_commission_of_the_asset_class():
    svc = NexusService(SimpleNamespace(settings=_Settings(dict(DEFAULT_SETTINGS)), db=None, bus=None, hub=None,
                                       ws=None, scanner=None, macro=None))
    assert svc.params("FX:EURUSD").fee_pct == pytest.approx(0.005)
    assert svc.params("NASDAQ:AAPL").fee_pct == pytest.approx(0.02)
    assert svc.params("BINANCE:BTCUSDT").fee_pct == pytest.approx(0.1)
    assert svc.params("SIM:X").fee_pct == pytest.approx(0.1)
    assert svc.params().fee_pct == pytest.approx(0.1) and svc.params("FX:EURUSD", fee_pct=0.3).fee_pct == 0.3


def test_commission_driven_rr_is_not_a_critical_veto_and_names_the_fee():
    ctx = favorable_ctx(atr=0.06, sigma=0.05)  # R ≈ 0,12 % of the price: an hourly forex-like range
    crypto_fee = evaluate_gate(ctx, "long", NexusParams(fee_pct=0.1))
    rr = next(c for c in crypto_fee["checks"] if c["id"] == "risk_reward")
    assert rr["passed"] is False and rr["critical"] is False
    assert "0,1 %" in rr["detail"] and "sin ellas" in rr["detail"]
    ev = next(c for c in crypto_fee["checks"] if c["id"] == "expected_value")
    assert ev["critical"] is True and "sin la comisión (0,1 % por lado)" in ev["detail"]  # the cost is named
    forex_fee = evaluate_gate(ctx, "long", NexusParams(fee_pct=0.005))
    rr_fx = next(c for c in forex_fee["checks"] if c["id"] == "risk_reward")
    assert rr_fx["passed"] is True and "0,005 %" in rr_fx["detail"]
    # a stop/target geometry below 1.2R stays a critical failure whatever the commission
    poor = evaluate_gate(favorable_ctx(), "long", NexusParams(fee_pct=0.0), sl=98.0, tp=102.2)
    rr_poor = next(c for c in poor["checks"] if c["id"] == "risk_reward")
    assert rr_poor["critical"] is True and poor["verdict"] == "AVOID"


# ----------------------------------------------------------------------------- exit policy (I7)


def _holding_run(prob: float, score: float):
    cs = generate_candleset(n=400, seed=21, tf="1h", end_time=FIXED_END)
    run = NexusRun(cs, NexusParams(), None, None)
    run.run()
    i = run.n - 1
    c = float(cs.close[i])
    lv = Levels(1, c, c - 2.0, c + 2.0, c + 4.0, c + 6.0)
    pos = Position(99, lv, i - 3, int(cs.time[i - 3]), c, 70.0, int(run.regime[i]), i - 4)
    pos.bars = 3
    run.pos = pos
    run.prob[i], run.score[i], run.score[i - 1] = prob, score, score
    run.action[i] = "HOLD_LONG"
    run.bar_info = {}
    return run, i, pos


@pytest.mark.parametrize("prob,deep", [(0.46, False), (0.2, True)])
def test_negative_ev_while_holding_is_watch_exit_not_close(prob, deep):
    run, i, pos = _holding_run(prob, score=0.1)  # score still with the long
    assert run._exit_reason(i, pos) is None  # the engine keeps the position
    state, ev = position_state(run, i)
    assert ev < 0 and state["ev_hold_r"] < 0 and (ev <= -0.10) == deep
    warnings = build_warnings(run, i, ev)
    watch = [w for w in warnings if w["code"] == "WATCH_EXIT"]
    assert len(watch) == 1 and watch[0]["severity"] == "warn"
    assert watch[0]["message"].startswith("Vigila la salida: el valor esperado de mantener es negativo (−0,")
    assert not [w for w in warnings if w["code"] == "EARLY_EXIT" or w["severity"] == "critical"]
    assert ("aún no gira en contra" in watch[0]["message"]) == deep
    advice = build_advice(run, i, "HOLD_LONG", {}, warnings)
    assert advice.startswith("Mantener el largo y vigilar la salida: el valor esperado") and "errar" not in advice


def test_early_exit_keeps_the_close_now_instruction():
    run, i, pos = _holding_run(0.25, score=-0.4)
    reason = run._exit_reason(i, pos)
    assert reason is not None and reason[0] == "EARLY"
    run._exit_now_or_next(i, reason[0], "Salida anticipada", reason[1])
    assert run.pos is None
    warnings = build_warnings(run, i, None)
    assert [w["severity"] for w in warnings if w["code"] == "EARLY_EXIT"] == ["critical"]
    assert build_advice(run, i, "EXIT_LONG", {}, warnings).startswith("Cerrar ahora")
