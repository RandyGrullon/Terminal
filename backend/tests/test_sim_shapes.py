"""BE-SIM JSON shapes against frontend/src/api/types.ts (the canonical contract): exact key sets."""
from __future__ import annotations

import asyncio
import json

import numpy as np

from app import config as nexus_config
from app.sim import backtest as bt
from app.sim import evaluator as ev
from app.sim import scanner as sc
from app.sim.montecarlo import summarize_paths
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_sim_fakes import FakeHub, scan_row

SCAN_RUN = {"id", "market", "tf", "started_at", "finished_at", "n_symbols", "status", "deep", "message"}
QUICK = {"symbol", "tf", "score", "prob_up", "action", "confidence", "rating", "price", "reasons", "model_updates",
         "time"}
PREDICTION_ROW = {"id", "source", "symbol", "tf", "created_at", "target_at", "price", "direction", "prob_up", "score",
                  "resolved", "outcome_price", "outcome_return_pct", "correct"}
PREDICTION_STATS = {"total", "resolved", "pending", "hit_rate", "brier", "by_source", "rolling"}
LEARNING = {"scanner_model", "nexus_models", "gate", "predictions", "timeline"}
SCANNER_MODEL = {"key", "updates", "hit_rate", "brier", "features", "calibration"}
GATE_SUMMARY = {"decisions", "taken", "avoided", "avoided_would_lose_rate", "taken_win_rate"}
METRICS = {"initial_capital", "final_equity", "total_return_pct", "cagr_pct", "buy_hold_return_pct", "trades",
           "win_rate", "profit_factor", "avg_win_pct", "avg_loss_pct", "expectancy_pct", "avg_r", "max_dd_pct",
           "sharpe", "sortino", "calmar", "exposure_pct", "avg_bars_in_trade", "best_trade_pct", "worst_trade_pct",
           "max_consecutive_losses", "fees_paid", "rejected_signals", "rejected_would_win_rate", "gate_lift_pct"}
MONTE_CARLO = {"mode", "paths", "steps", "percentiles", "final_returns_pct", "max_drawdowns_pct", "prob_profit",
               "prob_ruin", "median_return_pct", "expected_return_pct", "var95_pct", "cvar95_pct", "sample_paths",
               "notes"}
SIM_STATE = {"running", "paused", "mode", "symbol", "tf", "speed", "regime", "bars_done", "bars_total", "price",
             "started_at", "auto_trade", "account", "last_action", "stats"}
SIM_STATE_EXTRAS = {"market_regime", "seed"}  # optional extras read by the simulator UI (SimStatusBar)


def test_scanner_predictions_and_learning_shapes(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", False)
    rows = [scan_row("BINANCE:BTCUSDT", value=2e9), scan_row("BINANCE:ETHUSDT", value=1e9, bullish=False),
            scan_row("BINANCE:SOLUSDT", value=5e8, rating=0.8)]

    async def scenario():
        async with runtime_factory() as rt:
            published = []
            monkeypatch.setattr(rt.ws, "publish", lambda topic, msg: published.append((topic, msg)))
            rt.hub = FakeHub(scan_rows=rows, prices={"BINANCE:BTCUSDT": 101.0, "BINANCE:ETHUSDT": 99.0,
                                                     "BINANCE:SOLUSDT": 98.0})
            svc = rt.scanner = sc.ScannerService(rt)
            await svc.start()
            run_id = await svc.run_scan("crypto", "1h", 50, wait=True)
            run, results = await svc.results(run_id)
            assert set(run) == SCAN_RUN and run["status"] == "done" and len(results) == 3
            assert set((await svc.runs())[0]) == SCAN_RUN
            for topic, msg in published:
                if topic == "scanner":
                    assert msg["type"] == "scanner" and msg["run_id"] == run_id
                    assert {"type", "event", "run_id"} <= set(msg) <= {"type", "event", "run_id", "progress", "message"}
                    assert msg["event"] in ("started", "progress", "done", "error")
            quick = await svc.quick_signal("BINANCE:SOLUSDT", "1h")
            assert set(quick) == QUICK and quick["action"] in ("BUY", "SELL", "WAIT")

            target = int(await rt.db.fetchval("SELECT MAX(target_at) FROM predictions"))
            counters = await ev.PredictionEvaluator(rt).run_once(now=target + 1000)
            assert counters["predictions"] == 3 and counters["scanner_updates"] == 3
            preds = await ev.list_predictions(rt.db)
            assert len(preds) == 3 and all(set(p) == PREDICTION_ROW for p in preds)
            assert all(p["resolved"] is True and isinstance(p["correct"], bool) for p in preds)
            stats = await ev.prediction_stats(rt.db)
            assert set(stats) == PREDICTION_STATS and stats["resolved"] == 3 and stats["pending"] == 0
            assert set(stats["by_source"]["scanner"]) == {"resolved", "hit_rate", "brier"}
            assert all(set(r) == {"time", "hit_rate", "n"} for r in stats["rolling"])

            summary = await ev.learning_summary(rt.db, rt.settings.all())
            json.dumps(summary, allow_nan=False)
            assert set(summary) == LEARNING and set(summary["gate"]) == GATE_SUMMARY
            model = summary["scanner_model"]
            assert set(model) == SCANNER_MODEL and model["key"] == "crypto|1h|14400" and model["updates"] == 3
            assert all(set(f) == {"name", "label", "weight"} for f in model["features"])
            assert all(set(c) == {"bin", "predicted", "observed", "count"} for c in model["calibration"])
            assert summary["timeline"] and all(set(t) == {"time", "hit_rate", "brier", "n"} for t in summary["timeline"])
            await svc.stop()

    asyncio.run(scenario())


def test_backtest_metrics_and_montecarlo_shapes():
    cs = generate_candleset(n=300, seed=4, end_time=FIXED_END)
    entry = float(cs.open[101])
    trade = {"side": "long", "entry_time": int(cs.time[101]), "entry_price": entry, "sl_initial": entry * 0.98,
             "tp1": entry * 1.02, "tp2": entry * 1.04, "tp3": entry * 1.06, "exit_time": int(cs.time[120]),
             "exit_price": entry * 1.04, "r_multiple": 2.0, "bars": 19, "exit_reason": "TP3"}
    sim = bt.simulate_account(cs, [trade])
    metrics = bt.compute_metrics(cs, sim, 10000.0)
    assert set(metrics) == METRICS and metrics["trades"] == 1
    assert set(bt.public_trade(sim["trades"][0], 0)) == {
        "id", "side", "entry_time", "entry_price", "exit_time", "exit_price", "sl_initial", "tp1", "tp2", "tp3",
        "exit_reason", "exit_label", "pnl_pct", "r_multiple", "bars", "mfe_pct", "mae_pct", "gate_score"}
    assert set(bt.equity_points(cs, sim["equity"])[0]) == {"time", "equity", "drawdown_pct"}
    assert set(bt.benchmark_points(cs, 10000.0)[0]) == {"time", "equity"}

    eq = 100.0 * np.cumprod(1.0 + np.random.default_rng(3).normal(0.001, 0.01, size=(40, 60)), axis=1)
    res = summarize_paths(np.hstack([np.full((40, 1), 100.0), eq]), mode="bootstrap")
    json.dumps(res, allow_nan=False)
    assert set(res) == MONTE_CARLO and res["paths"] == 40 and res["steps"] == 60
    assert all(set(p) == {"step", "p5", "p25", "p50", "p75", "p95"} for p in res["percentiles"])


def test_sim_state_shape(runtime_factory, monkeypatch):
    from app.sim.simulator import Simulator

    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)

    async def scenario():
        async with runtime_factory() as rt:
            sim = Simulator(rt)
            await sim.start()
            state = sim.state()
            assert set(state) == SIM_STATE | SIM_STATE_EXTRAS and state["running"] is False
            snap = await sim.snapshot()
            assert set(snap) == {"state", "candles", "nexus", "positions"} and snap["positions"] == []

    asyncio.run(scenario())
