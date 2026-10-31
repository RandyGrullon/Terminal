"""Improvement 5 (scanner-model), adversarial verification.

- the verdict names the real length of its independent windows (a group that mixes the scanner's 4 h horizon with
  NEXUS 5-day predictions is clustered by 5-day windows; sub-hour horizons are clustered by target hour);
- one absurd or corrupt row (subnormal volatility, a ratio that underflows to 0, a legacy vector with strings or huge
  logs) never stops a scan, the learning step or the offline replay;
- a single-symbol read without a scan of its own timeframe says so (the context is kept per market, timeframe and
  horizon);
- end to end on a controlled clock: the offline replay of the stored rows reproduces the live v2 predictions.
"""
from __future__ import annotations

import asyncio
import json
import math

import numpy as np
import pytest

from app import config as nexus_config
from app.sim import evaluator as ev
from app.sim import scanner as sc
from tests.test_sim_fakes import FakeHub, scan_row

H4 = 14_400_000
D5 = 5 * 86_400_000
NOW = 1_760_000_400_000
KEY = "crypto|1h|14400"


def test_the_verdict_names_the_real_length_of_its_windows():
    recs = []
    for w in range(42):  # 7 days of 4 h scanner windows
        t = NOW - 7 * 86_400_000 + w * H4 + H4 // 2
        recs += [ev._replay_record(H4, t, 1, 0.6, 1.0 if i < 30 else -1.0) for i in range(50)]
    for d in range(7):  # NEXUS 1d signals judged 5 bars later
        t = NOW - 7 * 86_400_000 + d * 86_400_000 + 3_600_000
        recs += [{**ev._replay_record(D5, t, 1, 0.6, 1.0), "source": "nexus"} for _ in range(3)]
    mixed = ev.skill_group(recs, gid="all", label="Todas", common_blocks=True)
    assert mixed["horizon"] == "4 h" and mixed["window"] == "5 d" and mixed["windows"] <= 3
    assert "ventanas independientes de 5 d" in mixed["verdict_text"] and "de 4 h" not in mixed["verdict_text"]
    scanner = ev.skill_group([r for r in recs if r["hz"] == H4], gid="scanner", label="Escáner")
    assert scanner["window"] == scanner["horizon"] == "4 h" and scanner["windows"] == 42
    # a 15-minute horizon: the records are per target hour, so its windows are (at least) 1 h long
    quick = [ev._replay_record(900_000, NOW - k * 900_000, 1, 0.6, 1.0) for k in range(4)]  # NOW: a whole hour
    q = ev.skill_group(quick, gid="q", label="q")
    assert q["horizon"] == "15 min" and q["window"] == "1 h" and q["windows"] == 2
    assert "2 ventanas independientes de 1 h" in q["verdict_text"]
    assert ev.skill_group([], gid="e", label="e")["window"] is None
    paired = ev._paired_verdict({(900_000, 4): [10, 1.0, 0.0]}, 900_000)
    assert "1 ventana independiente de 1 h" in paired["text"]


def test_one_absurd_or_corrupt_row_never_stops_a_scan_or_the_learning():
    rows = [{**scan_row(f"BINANCE:A{i}USDT", value=1e9 - i * 1e7), "Volatility.D": 3.0 + i} for i in range(6)]
    rows[2].update({"Volatility.D": None, "close": 1e300, "ATR": 1e-310})  # ATR / price underflows to 0
    rows[3].update({"Volatility.D": 5e-324, "ATR": 5e-324})  # subnormal scales: their scaled forms underflow to 0
    assert sc.extract_extras(rows[3]).get("vol_d_log") is None
    model = sc.ScannerModel(KEY)
    evals, ctx, batch = sc.evaluate_scan(rows, model)
    assert len(evals) == 6 and batch is not None
    odd = next(e for r, e in evals if r["symbol"] == "BINANCE:A2USDT")
    assert "vol_d_log" not in odd.features and "atr_log" not in odd.features and 0 < odd.prob_up < 1
    tiny = next(e for r, e in evals if r["symbol"] == "BINANCE:A3USDT")
    assert "chg_d" not in tiny.features and "macd_d" not in tiny.features and 0 < tiny.prob_up < 1
    # legacy vectors with strings, nested values and logs far outside the stored ±3 (corruption): ignored values
    bad = [json.dumps({"v": 1, "x": {"rec_60": "abc", "vol_d_log": 900, "chg_240": "x", "reg_range": "y"}}),
           json.dumps({"chg_60": [1], "reg_trend_up": {}, "atr_log": 800, "rec_60": 0.4})]
    decoded = [sc.decode_features(raw) for raw in bad]
    assert decoded[0] == {"vol_d_log": 900.0} and decoded[1] == {"atr_log": 800.0, "rec_60": 0.4}
    samples = [sc.LearnSample(key=KEY, feats=f, up=1, created_ms=NOW, ret_pct=1.0, symbol=f"X:{i}")
               for i, f in enumerate(decoded)]
    assert model.learn_scan(samples) == 2 and all(math.isfinite(v) for v in model.dir.weights().values())
    json.dumps(model.to_state(), allow_nan=False)
    rows_db = [{"id": i, "symbol": f"X:{i}", "created_at": NOW, "target_at": NOW + H4, "direction": 1, "prob_up": 0.6,
                "correct": 1, "outcome_return_pct": 1.0, "features": raw} for i, raw in enumerate(bad)]
    assert ev.replay_compute(rows_db, KEY)["rows"] == 2  # the replay reads them too


def test_a_single_read_without_a_scan_of_its_timeframe_says_so():
    row = {**scan_row("BINANCE:BTCUSDT"), "Volatility.D": 6.0}
    fresh = sc.ScannerModel("crypto|4h|14400")
    ev4 = sc.evaluate_single(row, fresh, NOW)
    assert any("sin escaneos de este mercado en 4h todavía" in r for r in ev4.reasons)
    scanned = sc.ScannerModel("crypto|4h|14400")
    _, _, b = sc.evaluate_scan([{**scan_row(f"BINANCE:A{i}USDT"), "Volatility.D": 6.0} for i in range(6)], scanned)
    scanned.remember(b, NOW - sc.CONTEXT_MAX_AGE_MS - 60_000)
    old = sc.evaluate_single(row, scanned, NOW)
    assert any("sin escaneo reciente del mercado en 4h (el último, hace 6 horas)" in r for r in old.reasons)


def test_a_confident_neutral_call_never_heads_the_ranking_or_the_summary():
    def row(symbol, action, score, conf, **extra):
        return {"symbol": symbol, "action": action, "score": score, "prob_up": (score + 1) / 2, "confidence": conf,
                "deep": False, "verdict": None, "gate_score": None, "opportunity": False, **extra}

    # a calm asset with a strong rating: P↑ 70 % but 80 % inside the band → neutral, with a large |score| × confidence
    results = [row("BINANCE:CALMUSDT", "WAIT", 0.4, 0.42), row("BINANCE:AUSDT", "BUY", 0.12, 0.2),
               row("BINANCE:BUSDT", "SELL", -0.06, 0.1), row("BINANCE:TAKEUSDT", "WAIT", 0.05, 0.1, deep=True,
                                                             verdict="TAKE", gate_score=81.0, opportunity=True)]
    sc.rank_rows(results)
    assert [r["symbol"] for r in results] == ["BINANCE:TAKEUSDT", "BINANCE:AUSDT", "BINANCE:BUSDT",
                                              "BINANCE:CALMUSDT"]  # TOMAR first, then directional, then neutral
    assert [r["rank"] for r in results] == [1, 2, 3, 4]
    quick = [r for r in results if not r["opportunity"]]
    sc.rank_rows(quick)
    summary = sc.ScannerService._summary("crypto", "1h", quick, False, "range")
    assert summary.endswith("· 1 alcista · 1 bajista · 1 neutral · régimen lateral · mejor: BINANCE:AUSDT (p↑ 56 %)")
    calm = [row("BINANCE:CALMUSDT", "WAIT", 0.4, 0.42)]
    assert "mejor" not in sc.ScannerService._summary("crypto", "1h", calm, False, "range")  # no directional read


def test_the_offline_replay_reproduces_the_live_model_end_to_end(runtime_factory, monkeypatch):
    """Live scans every 15 min and the evaluator on a controlled clock, then the replay of the stored rows: the v2
    model replayed from scratch gives the probabilities that were served live (the stored vectors keep 4 decimals)."""
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", False)
    clock = {"t": NOW}
    monkeypatch.setattr(sc, "now_ms", lambda: clock["t"])
    rng = np.random.default_rng(5)
    symbols = ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"] + [f"BINANCE:C{i}USDT" for i in range(2, 24)]
    price = {s: 100.0 for s in symbols}
    n_scans = 36

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices=dict(price))
            svc = rt.scanner = sc.ScannerService(rt)
            await svc.start()
            evaluator = ev.PredictionEvaluator(rt)
            for k in range(n_scans + 17):
                clock["t"] = NOW + k * 900_000
                market = 0.15 if (k // 24) % 2 == 0 else -0.15  # % per 15 min: a rally, then a drop
                for s in symbols:
                    price[s] *= math.exp((market + rng.normal(0, 0.3)) / 100)
                rt.hub.prices = dict(price)
                await evaluator.run_once(now=clock["t"])  # resolves (and learns) the scan of 4 h ago first
                if k < n_scans:
                    rows = []
                    for i, s in enumerate(symbols):
                        r = scan_row(s, close=price[s], value=1e9 - i * 1e7, rating=float(rng.uniform(0, 1)),
                                     bullish=bool(rng.uniform() < 0.5), rsi=float(rng.uniform(35, 75)))
                        r.update({"Volatility.D": float(rng.uniform(1.5, 8.0)),
                                  "change|240": float(rng.normal(market * 16, 1.0))})
                        rows.append(r)
                    rt.hub.scan_rows = rows
                    await svc.run_scan("crypto", "1h", 100, wait=True)
            model = svc.get_model_sync(KEY)
            assert model.scans >= n_scans - 1 and model.dir.lessons > 1.5  # learned live, Δt / horizon per scan
            stored = await rt.db.fetchall(
                "SELECT id, symbol, created_at, target_at, direction, prob_up, correct, outcome_return_pct, features "
                "FROM predictions WHERE resolved=1 AND correct IS NOT NULL")
            assert len(stored) >= n_scans * len(symbols) - 10
            out = ev.replay_compute([dict(r) for r in stored], KEY, keep_records=True)
            live, replayed = out["records"]["recorded"], out["records"]["v2"]
            diffs = np.array([abs(a["p_sum"] - b["p_sum"]) for a, b in zip(replayed, live)])
            assert len(diffs) == len(stored) and float(np.median(diffs)) < 1e-3 and float(diffs.max()) < 0.02
            assert sum(1 for a, b in zip(replayed, live) if a["d"] != b["d"]) <= 0.02 * len(live)
            await svc.stop()

    asyncio.run(scenario())
