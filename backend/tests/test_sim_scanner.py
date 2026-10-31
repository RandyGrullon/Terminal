"""BE-SIM: scanner features, junk filtering, online model + cold-start blend, scan runs, deep mode, quick signals."""
from __future__ import annotations

import asyncio
import json
import math

import numpy as np
import pytest

from app import config as nexus_config
from app.sim import scanner as sc
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_sim_fakes import FakeHub, FakeNexus, make_current, scan_row


def test_extract_features_full_and_with_nulls():
    feats, completeness = sc.extract_features(scan_row("BINANCE:BTCUSDT"))
    assert completeness == pytest.approx(1.0)
    assert set(feats) == set(sc.FEATURE_NAMES)
    assert all(-3.0 <= v <= 3.0 for v in feats.values())
    assert feats["rec_60"] > 0 and feats["ema_240"] > 0 and feats["macd_60"] > 0
    sparse = {"symbol": "X:Y", "close": 10.0, "RSI": 70, "Recommend.All": None, "ATR": None, "Volatility.D": None,
              "EMA50": "n/a", "relative_volume_10d_calc": 0}
    f2, c2 = sc.extract_features(sparse)
    assert f2 == {"rsi_d": pytest.approx(0.4)} and 0 < c2 < 0.1
    assert sc.extract_features({"close": None})[1] == 0.0


def test_junk_filter_dedupe_and_liquidity():
    rows = [
        scan_row("BINANCE:BTCUSDT", value=2e9),
        scan_row("OKX:BTCUSDT", value=1.4e9),  # same base on another venue -> dropped
        scan_row("UNISWAP:PEPEWETH_A43FE1.USD", value=9e9),  # DEX junk
        scan_row("SUNSWAP2:ETHUSDT_TWNJMG", value=9e9),
        scan_row("BINANCE:USDCUSDT", value=1e9),  # stablecoin pair
        scan_row("BINANCE:ETHUSDT", value=1.4e9),
        scan_row("MEXC:TINYUSDT", value=10_000),  # illiquid
        scan_row("BINANCE:SOLUSDT", value=5e8),
    ]
    out = sc.filter_rows(rows, "crypto", limit=10)
    assert [r["symbol"] for r in out] == ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:SOLUSDT"]
    assert [r["symbol"] for r in sc.filter_rows(rows, "crypto", limit=2)] == ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"]
    assert sc.is_junk_symbol("PANCAKESWAP3BSC:SOLWBNB_1E4600") and not sc.is_junk_symbol("NASDAQ:AAPL")
    assert sc.crypto_base({"currency": "USDT"}, "BINANCE:BTCUSDT") == "BTC"
    fx = [{"symbol": "FX:EURUSD", "close": 1.08, "volume": None}, {"symbol": "FX:GBPUSD", "close": 1.27}]
    assert len(sc.filter_rows(fx, "forex", limit=10)) == 2  # no volume data: no liquidity filter


def test_market_profiles():
    crypto = sc.market_profile("crypto")
    assert crypto["sort"] == "24h_vol|5" and crypto["filters"][0]["left"] == "exchange" and crypto["dedupe_base"]
    assert sc.market_profile("america")["sort"] == "Value.Traded"
    assert sc.market_profile("forex")["min_value"] == 0
    assert sc.market_for_exchange("BINANCE") == "crypto" and sc.market_for_exchange("NASDAQ") == "america"
    assert sc.market_for_exchange("OANDA") == "forex" and sc.market_for_exchange("ZZZ") is None


def test_online_logit_learns_and_roundtrips():
    rng = np.random.default_rng(0)
    model = sc.OnlineLogit()
    i_rec, i_macd = model.names.index("rec_60"), model.names.index("macd_60")
    losses = []
    for epoch in range(3):
        loss = 0.0
        for _ in range(400):
            x = np.zeros(len(model.names))
            x[i_rec], x[i_macd] = rng.uniform(-1, 1), rng.uniform(-1, 1)
            y = 1 if x[i_rec] + 0.5 * x[i_macd] + rng.normal(0, 0.2) > 0 else 0
            p = model.update(x, y, label_time=1000 + epoch)
            loss += -(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9)))
        losses.append(loss / 400)
    assert losses[-1] < losses[0] < math.log(2) + 0.05
    assert model.w[i_rec] > model.w[i_macd] > 0
    assert model.updates == 1200 and model.trained_until == 1002
    assert model.hit_rate() > 0.8 and model.brier() < 0.2
    clone = sc.OnlineLogit.from_state(json.loads(json.dumps(model.to_state())))
    x = np.zeros(len(model.names))
    x[i_rec] = 0.7
    assert clone.predict(x) == pytest.approx(model.predict(x)) and clone.updates == 1200
    renamed = model.to_state()
    renamed["names"] = ["old_feature" if n == "rec_60" else n for n in renamed["names"]]
    partial = sc.OnlineLogit.from_state(renamed)
    assert partial.w[i_rec] == 0.0 and partial.w[i_macd] == pytest.approx(model.w[i_macd])


def test_cold_start_blend_and_reasons():
    row = scan_row("BINANCE:SOLUSDT")
    feats, comp = sc.extract_features(row)
    cold = sc.OnlineLogit()
    p_cold, score_cold, _, alpha_cold = sc.evaluate_features(feats, comp, "1h", cold)
    prior_p = 0.5 + 0.25 * sc.rule_prior(feats, "1h")
    assert alpha_cold == 0.0 and p_cold == pytest.approx(prior_p, abs=1e-9)
    hot = sc.OnlineLogit()
    hot.updates = sc.COLD_UPDATES
    hot.w[:] = -0.05  # a learned model that disagrees with the rules (inside the 1..99 % clamp)
    p_hot, _, _, alpha_hot = sc.evaluate_features(feats, comp, "1h", hot)
    assert alpha_hot == 1.0 and p_hot == pytest.approx(hot.predict(hot.vector(feats)), abs=1e-9) and p_hot < 0.5
    half = sc.OnlineLogit()
    half.updates = sc.COLD_UPDATES // 2
    half.w[:] = -0.05
    p_half, *_ = sc.evaluate_features(feats, comp, "1h", half)
    assert p_hot < p_half < p_cold
    ev = sc.evaluate_row(row, "1h", cold)
    assert ev.action == "BUY" and score_cold > 0.15
    assert any("Modelo en aprendizaje (0/200" in r for r in ev.reasons)
    assert any(r.startswith("Rating técnico 1h") for r in ev.reasons)
    bear = sc.evaluate_row(scan_row("BINANCE:XRPUSDT", bullish=False), "1h", cold)
    assert bear.action == "SELL" and bear.score < 0
    assert sc.evaluate_row({"symbol": "A:B", "close": 0}, "1h", cold) is None


def test_local_quick_from_candles():
    cs = generate_candleset(n=200, seed=3, regime="bull", end_time=FIXED_END)
    q = sc.local_quick_from_candles(cs, "1h")
    assert q is not None and -1 <= q["score"] <= 1 and 0 < q["prob_up"] < 1
    assert q["reasons"][0].startswith("Lectura local con 200 velas")
    assert sc.local_quick_from_candles(cs.tail(10), "1h") is None


def _rows() -> list[dict]:
    # 8 % daily volatility: moves larger than the neutral band are expected, so the reads are directional
    rows = [
        scan_row("BINANCE:BTCUSDT", value=2e9),
        scan_row("BINANCE:ETHUSDT", value=1e9, bullish=False),
        scan_row("BINANCE:SOLUSDT", value=5e8, rating=0.8),
        scan_row("UNISWAP:PEPEWETH_A43FE1.USD", value=9e9),
        scan_row("BINANCE:DOGEUSDT", value=4e8, rating=0.05, change=0.0),
    ]
    return [{**r, "Volatility.D": 8.0} for r in rows]


def test_scan_run_persists_results_predictions_and_events(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", False)

    async def scenario():
        async with runtime_factory() as rt:
            published = []
            monkeypatch.setattr(rt.ws, "publish", lambda topic, msg: published.append((topic, msg)))
            rt.hub = FakeHub(scan_rows=_rows())
            svc = sc.ScannerService(rt)
            rt.scanner = svc
            await svc.start()
            run_id = await svc.run_scan("crypto", "1h", 50, deep=False, wait=True)
            run, results = await svc.results(run_id)
            assert run["status"] == "done" and run["n_symbols"] == 4 and run["deep"] is False
            assert [r["rank"] for r in results] == [1, 2, 3, 4]
            assert all(r["symbol"] != "UNISWAP:PEPEWETH_A43FE1.USD" for r in results)
            keys = {"rank", "symbol", "description", "type", "price", "change_pct", "volume", "score", "prob_up",
                    "action", "confidence", "rating", "deep", "verdict", "gate_score", "direction", "regime", "sl",
                    "tp1", "tp2", "expected_move_pct", "reasons", "nexus_action", "opportunity"}
            assert set(results[0]) == keys
            assert results[0]["opportunity"] is False and results[0]["nexus_action"] is None
            scores = [abs(r["score"]) * r["confidence"] for r in results]
            assert scores == sorted(scores, reverse=True)
            preds = await rt.db.fetchall("SELECT * FROM predictions WHERE source='scanner'")
            assert len(preds) == 4
            p = preds[0]
            assert p["model_key"] == "crypto|1h|14400" and p["target_at"] - p["created_at"] == 14400 * 1000
            stored = json.loads(p["features"])  # packed v4: base features + extras + the scan's context
            assert stored["v"] == 4 and stored["s"] == sc.CONTEXT_SIG and stored["g"] == sc.EXTRA_SIG
            assert len(p["features"]) < 220  # ≈ 0,2 KB per row (JSON lists of the same values: ≈ 0,45 KB)
            known = set(sc.FEATURE_NAMES) | set(sc.EXTRA_NAMES) | set(sc.CONTEXT_NAMES)
            decoded = sc.decode_features(p["features"])  # 4 rows: too few for breadth, the regime is always there
            assert set(decoded) <= known and {f"reg_{r}" for r in sc.REGIMES} <= set(decoded)
            assert {r["direction"] for r in preds} >= {1, -1}
            call = rt.hub.scan_calls[0]
            assert call["sort_by"] == "24h_vol|5" and call["filters"] and "RSI|60" in call["columns"]
            events = [e["message"] for e in rt.bus.recent(level="scan")]
            assert events and events[-1].startswith("Escaneo crypto 1h: 4 activos")
            kinds = [m["event"] for t, m in published if t == "scanner"]
            assert kinds[0] == "started" and kinds[-1] == "done"
            runs = await svc.runs()
            assert runs[0]["id"] == run_id and svc.status()["last_run"] is not None
            # a hub that rejects the server-side filters falls back to a plain scan
            rt.hub.fail_filters = True
            svc2 = sc.ScannerService(rt)
            await svc2.start()
            run2 = await svc2.run_scan("crypto", "1h", 50, wait=True)
            assert (await svc2.results(run2))[0]["status"] == "done"
            assert rt.hub.scan_calls[-1]["filters"] is None
            await svc.stop()
            await svc2.stop()

    asyncio.run(scenario())


def test_scan_errors_are_recorded(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(scan_rows=_rows())
            svc = sc.ScannerService(rt)
            await svc.start()
            run_id = await svc.run_scan("crypto", "1h", wait=True)
            run, results = await svc.results(run_id)
            assert run["status"] == "error" and "sin conexión" in run["message"].lower() and results == []
            with pytest.raises(sc.ScanError):
                await svc.run_scan("crypto; DROP", "1h")
            await svc.stop()

    asyncio.run(scenario())


def test_deep_mode_ranks_take_first_and_logs_gate(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", False)

    from app.util import now_s

    bar = now_s() // 3600 * 3600  # the forming 1h bar: a live (not outdated) series

    def nexus_resp(long_verdict: str, short_verdict: str, score: float) -> dict:
        cur = make_current("BUY" if score > 0 else "SELL", long_verdict=long_verdict, short_verdict=short_verdict,
                           score=score, time=bar)
        return {"ok": True, "current": cur, "prediction": {"expected_return_pct": 1.8}}

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(scan_rows=_rows(), info={"BINANCE:SOLUSDT": {"history": "none"}})
            rt.nexus = FakeNexus(analyze_map={
                "BINANCE:BTCUSDT": nexus_resp("AVOID", "AVOID", 0.4),
                "BINANCE:ETHUSDT": nexus_resp("AVOID", "TAKE", -0.5),
            })
            svc = sc.ScannerService(rt)
            await svc.start()
            run_id = await svc.run_scan("crypto", "1h", 50, deep=True, deep_top=5, wait=True)
            run, results = await svc.results(run_id)
            assert run["deep"] is True
            first = results[0]
            assert first["symbol"] == "BINANCE:ETHUSDT" and first["deep"] and first["verdict"] == "TAKE"
            assert first["direction"] == "short" and first["expected_move_pct"] == 1.8
            assert first["nexus_action"] == "SELL" and first["opportunity"] is True
            assert first["reasons"][0].startswith("Filtro TOMAR 78/100")
            btc = next(r for r in results if r["symbol"] == "BINANCE:BTCUSDT")
            assert btc["verdict"] == "AVOID" and btc["sl"] is not None
            sol = next(r for r in results if r["symbol"] == "BINANCE:SOLUSDT")
            assert sol["deep"] is False  # no full history -> not analysed
            logs = await rt.db.fetchall("SELECT symbol, verdict, source, checks, target_at, created_at FROM gate_log")
            assert {r["symbol"] for r in logs} == {"BINANCE:BTCUSDT", "BINANCE:ETHUSDT"}
            assert all(r["source"] == "scanner" for r in logs)
            assert json.loads(logs[0]["checks"]) == {"trend_alignment": 0, "risk_reward": 1, "volume": None} or \
                json.loads(logs[0]["checks"])["risk_reward"] == 1
            msgs = [e["message"] for e in rt.bus.recent(level="scan")]
            assert any("1 oportunidad TOMAR" in m and "BINANCE:ETHUSDT" in m for m in msgs)
            assert any(m.startswith("Rechazadas por el filtro") for m in msgs)
            await svc.stop()

    asyncio.run(scenario())


def test_quick_signal_tradingview_local_and_score_rows(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", False)

    async def scenario():
        async with runtime_factory() as rt:
            sim_cs = generate_candleset(n=300, seed=4, end_time=FIXED_END, symbol="SIM:NEXUS")
            rt.hub = FakeHub(scan_rows=_rows(), candles={("SIM:NEXUS", "1h"): sim_cs})
            svc = sc.ScannerService(rt)
            await svc.start()
            q = await svc.quick_signal("binance:btcusdt", "60")
            assert q["symbol"] == "BINANCE:BTCUSDT" and q["tf"] == "1h" and q["action"] == "BUY"
            assert set(q) == {"symbol", "tf", "score", "prob_up", "action", "confidence", "rating", "price", "reasons",
                              "model_updates", "time"}
            n_calls = len(rt.hub.scan_calls)
            assert (await svc.quick_signal("BINANCE:BTCUSDT", "1h")) == q and len(rt.hub.scan_calls) == n_calls
            assert rt.hub.scan_calls[-1]["tickers"] == ["BINANCE:BTCUSDT"]
            local = await svc.quick_signal("SIM:NEXUS", "1h")
            assert local is not None and local["rating"] is None and "Lectura local" in local["reasons"][0]
            assert await svc.quick_signal("NASDAQ:NOPE", "1h") is None
            scored = svc.quick_score_rows([scan_row("BINANCE:BTCUSDT"), {"close": None}, "bad"], market="crypto")
            assert scored[0] is not None and -1 <= scored[0][0] <= 1 and scored[1] is None and scored[2] is None
            samples = [("crypto|1h|14400", {"rec_60": 0.8, "macd_60": 1.0}, 1, 1000)] * 3
            touched = await svc.learn_batch(samples)
            assert touched["crypto|1h|14400"].updates == 3
            row = await rt.db.fetchone("SELECT updates, trained_until FROM model_state WHERE kind='scanner'")
            assert row["updates"] == 3 and row["trained_until"] == 1000
            assert svc.status()["model_updates"] == 3
            await svc.stop()

    asyncio.run(scenario())
