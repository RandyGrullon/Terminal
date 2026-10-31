"""Improvement 5 (scanner-model): offline prequential replay on the stored feature vectors.

- causal: a scan is predicted only with what was learned from scans whose horizon had ended at that moment;
- faithful: replaying the legacy cold model reproduces the predictions that were recorded live;
- end to end: a real scan stores its features and context, the evaluator keeps them, the replay reads them back.
"""
from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from app import config as nexus_config
from app.sim import evaluator as ev
from app.sim import scanner as sc
from tests.test_sim_fakes import FakeHub, scan_row

KEY = "crypto|1h|14400"
H_MS = 14_400_000
T0 = 1_760_000_000_000


def _stored_rows(n_scans: int, per_scan: int, seed: int = 7, start: int = T0) -> list[dict]:
    """Resolved scanner rows: 4 h of rallies, then 4 h of drops, …; the recorded calls are the legacy cold model's."""
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(n_scans):
        created = start + k * 900_000
        market = 1.0 if (k // 16) % 2 == 0 else -1.0
        for i in range(per_scan):
            rec = float(rng.uniform(-1, 1))
            ret = market + 0.8 * rec + float(rng.normal(0, 0.3))
            feats = {"rec_60": rec, "rec_all": float(rng.uniform(-1, 1)), "chg_240": float(rng.normal(0, 1)),
                     "vol_d_log": math.log(8.0 / 5.0)}
            base = {n: feats[n] for n in sc.FEATURE_NAMES if n in feats}
            p, score, _, _ = sc.evaluate_features(base, len(base) / len(sc.FEATURE_NAMES), "1h", sc.OnlineLogit())
            action = sc.action_for(score)
            d = 1 if action == "BUY" else -1 if action == "SELL" else 0
            rows.append({
                "symbol": f"BINANCE:A{i}USDT", "created_at": created, "target_at": created + H_MS, "direction": d,
                "prob_up": p, "correct": int(ev.judge_prediction(d, ret, H_MS / 1000)), "outcome_return_pct": ret,
                "features": sc.encode_features(feats, {"reg_range": 1.0}),
            })
    return rows


def test_replay_is_prequential_and_reproduces_the_recorded_cold_model():
    rows = _stored_rows(40, 12)
    out = ev.replay_compute(rows, KEY, keep_records=True)
    assert out["rows"] == 480 and out["scans"] == 40 and out["with_context"] == 480 and out["horizon"] == "4 h"
    v2, v1, rec = (out["records"][k] for k in ("v2", "v1", "recorded"))
    first_block = 16 * 12  # scans made before the first outcome was known
    # nothing was learned yet: identical to a model that never saw any outcome
    fresh = sc.ScannerModel(KEY)
    p_fresh = []
    for k in range(16):
        feats = [sc.decode_features(r["features"]) for r in rows[k * 12:(k + 1) * 12]]
        p_fresh += [float(p) for p in fresh.predict(fresh.batch(feats, {"reg_range": 1.0}))[1]]
    assert [r["p_sum"] for r in v2[:first_block]] == pytest.approx(p_fresh)
    assert [r["p_sum"] for r in v2[first_block:first_block + 12]] != pytest.approx(p_fresh[:12])  # learned since
    # the legacy model replayed from scratch gives what was recorded while it had not learned (the stored vectors
    # keep 4 decimals: tiny differences in the probabilities)
    assert [r["d"] for r in v1[:first_block]] == [r["d"] for r in rec[:first_block]]
    assert [r["p_sum"] for r in v1[:first_block]] == pytest.approx([r["p_sum"] for r in rec[:first_block]], abs=1e-3)
    designs = {d["id"]: d for d in out["designs"]}
    assert designs["v2"]["n"] == designs["v1"]["n"] == designs["recorded"]["n"] == 480
    c = out["comparison"]
    assert c["n"] == 480 and c["windows"] <= 4 and c["verdict"] == "insufficient"
    assert "no se puede afirmar que un diseño sea mejor" in c["text"]


def test_replay_report_reads_the_stored_rows(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            now = T0 + 40 * 900_000 + H_MS + 60_000
            for r in _stored_rows(40, 6):
                await db.execute(
                    "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, "
                    "prob_up, score, features, resolved, outcome_price, outcome_return_pct, correct, resolved_at) "
                    "VALUES ('scanner', ?, '1h', ?, ?, ?, 100, ?, ?, 0, ?, 1, ?, ?, ?, ?)",
                    (r["symbol"], KEY, r["created_at"], r["target_at"], r["direction"], r["prob_up"], r["features"],
                     100 + r["outcome_return_pct"], r["outcome_return_pct"], r["correct"], r["target_at"]),
                )
            await db.execute("UPDATE predictions SET features=NULL WHERE id % 7 = 0")  # rows without a vector
            rep = await ev.replay_report(db, days=3, now=now)
            stored = await db.fetchval("SELECT COUNT(*) FROM predictions WHERE features IS NOT NULL")
            assert rep["key"] == KEY and rep["rows"] == stored and rep["scans"] == 40
            assert [d["id"] for d in rep["designs"]] == ["v2", "v1", "recorded"]
            assert rep["comparison"]["verdict"] == "insufficient"
            assert rep["note"].startswith("Repetición secuencial: cada predicción se evalúa antes de aprender de ella")
            assert "prequential" not in rep["note"].lower()  # user-facing text in Spanish
            empty = await ev.replay_report(db, days=1, now=now + 30 * 86_400_000)
            assert empty["key"] is None and empty["comparison"]["verdict"] == "none"

    asyncio.run(scenario())


def test_scan_stores_context_the_evaluator_keeps_it_and_the_replay_uses_it(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", False)
    rows = [{**scan_row(f"BINANCE:C{i}USDT", value=1e9 - i * 1e7, bullish=i % 3 != 0), "Volatility.D": 2.0 + i}
            for i in range(12)]

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(scan_rows=rows, prices={r["symbol"]: 100.0 + (1 if i % 2 else -1) for i, r in
                                                     enumerate(rows)})
            svc = rt.scanner = sc.ScannerService(rt)
            await svc.start()
            await svc.run_scan("crypto", "1h", 50, wait=True)
            model = svc.get_model_sync(KEY)
            assert model.memory and model.memory["ctx"] and sc.context_regime(model.memory["ctx"]) in sc.REGIMES
            stored = await rt.db.fetchall("SELECT features, direction FROM predictions")
            assert len(stored) == 12 and all(sc.decode_features(r["features"]).keys() >= set(sc.CONTEXT_NAMES)
                                             for r in stored)
            assert {r["direction"] for r in stored} >= {0}  # the calm assets (2-4 % a day) are called neutral
            # a single-symbol read now uses this scan's context
            q = await svc.quick_signal("BINANCE:C5USDT", "1h")
            assert any("contexto del último escaneo" in r for r in q["reasons"])
            target = int(await rt.db.fetchval("SELECT MAX(target_at) FROM predictions"))
            out = await ev.PredictionEvaluator(rt).run_once(now=target + 1000)
            assert out["predictions"] == 12 and out["scanner_updates"] == 12
            assert await rt.db.fetchval("SELECT COUNT(*) FROM predictions WHERE features IS NOT NULL") == 12
            assert model.updates == 12 and model.scans == 1 and model.recent and model.recent[-1][1] == 12
            rep = await ev.replay_report(rt.db, now=target + 2000)
            assert rep["rows"] == 12 and rep["with_context"] == 12
            await svc.stop()

    asyncio.run(scenario())
