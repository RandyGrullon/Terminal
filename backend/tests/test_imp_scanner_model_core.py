"""Improvement 5 (scanner-model): the v2 scanner model.

- one market move is ONE lesson (scan-level part), cross-sectional deviations are the row lessons (capped);
- overlapping scans add Δt / horizon of a lesson; a learned scan's stragglers are not learned twice;
- a planted cross-sectional signal is learned, a pure market move does not leak into the row weights;
- the neutral class: predict neutral when P(move inside the band) beats both directions;
- Platt recalibration only after its minimum sample; context, regime and persistence.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from app.sim import scanner as sc
from app.sim.scanner import LearnSample, ScannerModel

KEY = "crypto|1h|14400"
H_MS = 14_400_000
T0 = 1_760_000_000_000  # ms


def _scan(rng, created_ms: int, n: int, *, market: float = 0.0, signal: float = 0.0, vol: float = 8.0,
          noise: float = 1.0) -> list[LearnSample]:
    """One resolved scan: each asset's return = market move + signal × rec_60 + noise (in %)."""
    out = []
    for i in range(n):
        rec = float(rng.uniform(-1, 1))
        ret = market + signal * rec + float(rng.normal(0, noise))
        feats = {"rec_60": rec, "macd_60": float(rng.uniform(-1, 1)), "chg_240": float(rng.normal(0, 1)),
                 "vol_d_log": math.log(vol / 5.0)}
        out.append(LearnSample(key=KEY, feats=feats, up=1 if ret > 0 else 0, label_time=(created_ms + H_MS) // 1000,
                               created_ms=created_ms, ret_pct=ret, symbol=f"BINANCE:A{i}USDT"))
    return out


def test_one_market_move_is_one_lesson_whatever_the_number_of_assets():
    rng = np.random.default_rng(1)
    small, big = ScannerModel(KEY), ScannerModel(KEY)
    small.learn_scan(_scan(rng, T0, 20, market=2.0, noise=0.1))
    big.learn_scan(_scan(rng, T0, 400, market=2.0, noise=0.1))
    # every asset went up: the scan-level bias moves by one lesson, not by 20 or 400 of them
    assert small.dir.b > 0 and big.dir.b == pytest.approx(small.dir.b, rel=0.05)
    assert small.dir.lessons == big.dir.lessons == 1.0
    # a market-wide move says nothing about which asset beats the others: the row weights barely move
    assert np.abs(big.dir.wr).max() < 0.01
    assert big.dir.row_lessons == sc.ROW_CAP  # cross-sectional lessons are capped per scan
    assert small.updates == 20 and big.updates == 400 and big.scans == 1


def test_overlapping_scans_add_a_fraction_of_a_lesson_and_stragglers_are_not_relearned():
    rng = np.random.default_rng(2)
    model = ScannerModel(KEY)
    for k in range(17):  # one scan every 15 min, judged over 4 h windows
        model.learn_scan(_scan(rng, T0 + k * 900_000, 30))
    assert model.dir.lessons == pytest.approx(1.0 + 16 / 16)  # ≈ 2 independent windows, not 17
    late = _scan(rng, T0 + 16 * 900_000, 3)  # 3 rows of the last scan resolved later
    assert model.learn_scan(late) == 0 and model.skipped == 3 and model.scans == 17
    lessons = model.dir.lessons
    bigger = _scan(rng, T0 + 16 * 900_000, 12)  # a larger late part: learned with the minimum weight, same scan
    assert model.learn_scan(bigger) == 12 and model.scans == 17 and model.recent[-1][1] == 42
    assert model.dir.lessons - lessons == pytest.approx(sc.MIN_TIME_WEIGHT)
    after_gap = _scan(rng, T0 + 16 * 900_000 + 2 * H_MS, 30)  # the scanner was off for 8 h: a full lesson
    before = model.dir.lessons
    model.learn_scan(after_gap)
    assert model.dir.lessons - before == pytest.approx(1.0)


def test_learns_a_planted_cross_sectional_signal():
    rng = np.random.default_rng(3)
    model = ScannerModel(KEY)
    for k in range(400):  # ≈ 25 independent windows of scans
        model.learn_scan(_scan(rng, T0 + k * 900_000, 60, market=float(rng.normal(0, 1.5)), signal=1.5, noise=1.0))
    w = model.dir.weights()
    assert w["rec_60"] > 0.15 and w["rec_60"] > 3 * max(abs(w["macd_60"]), abs(w["chg_240"]))
    # the served probabilities rank the assets of a new scan by the planted signal
    feats = [{"rec_60": r, "macd_60": 0.0, "chg_240": 0.0, "vol_d_log": math.log(8 / 5)} for r in (-0.9, 0.0, 0.9)]
    _, p_up, _ = model.predict(model.batch(feats, {"reg_range": 1.0}))
    assert p_up[0] < p_up[1] < p_up[2]


def test_neutral_when_the_expected_move_is_small_versus_the_band():
    assert sc.decide_direction(0.55, 0.30) == 0  # 70 % inside the band beats 55 % up
    assert sc.decide_direction(0.55, 0.90) == 1 and sc.decide_direction(0.40, 0.90) == -1
    assert sc.decide_direction(0.70, 0.35) == 1  # a strong direction still wins
    # the volatility prior: σ(4 h) = 0.5 × Volatility.D × √(4/24); P(|move| > 0.5 %)
    calm, wild = sc.mag_prior_logit(1.0, 14400), sc.mag_prior_logit(10.0, 14400)
    sigma = 0.5 * 3.0 * math.sqrt(4 / 24)
    p3 = math.erfc(0.5 / (sigma * math.sqrt(2)))
    assert sc.mag_prior_logit(3.0, 14400) == pytest.approx(math.log(p3 / (1 - p3)))
    assert calm < 0 < wild and sc.mag_prior_logit(None, 14400) is None
    model = ScannerModel(KEY)
    rows = [{"symbol": "BINANCE:BTCUSDT", "close": 100.0, "Volatility.D": 1.0, "RSI": 55, "Recommend.All|60": 0.2},
            {"symbol": "BINANCE:PEPEUSDT", "close": 1.0, "Volatility.D": 12.0, "RSI": 70, "Recommend.All|60": 0.6}]
    evals, ctx, _ = sc.evaluate_scan(rows, model)
    calm_ev = next(ev for row, ev in evals if row["symbol"] == "BINANCE:BTCUSDT")
    wild_ev = next(ev for row, ev in evals if row["symbol"] == "BINANCE:PEPEUSDT")
    assert calm_ev.action == "WAIT" and calm_ev.direction == 0 and calm_ev.prob_move < 0.3
    assert calm_ev.reasons[0].startswith("Movimiento esperado pequeño: P(moverse más de ±0,50 % en 4 h)")
    assert wild_ev.action in ("BUY", "SELL") and wild_ev.prob_move > 0.8  # σ(4 h) ≈ 2,4 %: P ≈ 0,84


def test_platt_recalibration_only_after_its_minimum_sample():
    rng = np.random.default_rng(4)
    cal = sc.Platt()
    z = rng.normal(0, 2.0, 2000)
    y = (rng.uniform(size=z.size) < 1 / (1 + np.exp(-z / 4.0))).astype(float)  # the logits are 4× overconfident
    assert not cal.active() and cal.prob(np.array([2.0]))[0] == pytest.approx(1 / (1 + math.exp(-2.0)))
    for k in range(0, 2000, 100):
        cal.update(z[k:k + 100], y[k:k + 100], 0.25, 0.25 * sc.ROW_CAP)
    assert cal.rows == 2000 and cal.lessons == pytest.approx(5.0) and cal.active()
    assert 0.1 < cal.a < 0.6  # shrinks the overconfident logits
    assert cal.prob(np.array([2.0]))[0] < 0.75
    assert sc.Platt.from_state(json.loads(json.dumps(cal.to_state()))).a == pytest.approx(cal.a, abs=1e-6)


def test_market_context_breadth_majors_and_regime():
    up = [{"chg_60": 0.5, "chg_240": 0.8, "chg_d": 1.0, "ema_60": 1.0, "ema_240": 1.0, "rec_240": 0.6,
           "volatility": -0.4} for _ in range(10)]
    symbols = ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"] + [f"BINANCE:A{i}USDT" for i in range(8)]
    ctx = sc.market_context(up, symbols, "crypto")
    assert ctx["ctx_breadth_1h"] == 1.0 and ctx["ctx_breadth_4h"] == 1.0 and ctx["ctx_breadth_1d"] == 1.0
    assert ctx["ctx_majors_trend"] > 0.5 and sc.context_regime(ctx) == "trend_up"
    assert "régimen alcista" in sc.context_text(ctx, "crypto") and "100 % de los activos al alza" in sc.context_text(
        ctx, "crypto")
    shock = [dict(f, chg_240=-2.5) for f in up]
    assert sc.context_regime(sc.market_context(shock, symbols, "crypto")) == "volatile"
    calm = [{"chg_60": (-1) ** i * 0.2, "chg_240": (-1) ** i * 0.3, "chg_d": (-1) ** i * 0.1} for i in range(10)]
    ctx_calm = sc.market_context(calm, symbols, "crypto")
    assert ctx_calm["ctx_breadth_4h"] == 0.0 and sc.context_regime(ctx_calm) == "range"
    assert sum(ctx_calm[f"reg_{r}"] for r in sc.REGIMES) == 1.0
    assert sc.major_indices(["NASDAQ:AAPL", "NASDAQ:MSFT"], "america") == [0, 1]  # elsewhere: the most liquid ones


def test_regime_intercepts_survive_other_regimes_and_stale_bias_fades():
    model = ScannerModel(KEY)
    c_up = np.array([1.0 if n == "reg_trend_up" else 0.0 for n in sc.DIR_COMMON])
    c_range = np.array([1.0 if n == "reg_range" else 0.0 for n in sc.DIR_COMMON])
    r = np.zeros((10, len(sc.DIR_ROW)))
    for _ in range(10):  # a rally: everything goes up
        model.dir.step(c_up, r, np.ones(10), 1.0, 10.0)
    i_up = sc.DIR_COMMON.index("reg_trend_up")
    learned, bias = model.dir.wc[i_up], model.dir.b
    assert learned > 0 and bias > 0
    for _ in range(20):  # then a balanced range
        model.dir.step(c_range, r, np.array([1.0, 0.0] * 5), 1.0, 10.0)
    assert model.dir.wc[i_up] == pytest.approx(learned)  # the rally's intercept waits for the next rally
    assert abs(model.dir.b) < 0.5 * bias  # the stale market bias faded


def test_state_roundtrip_and_a_legacy_state_starts_fresh():
    rng = np.random.default_rng(5)
    model = ScannerModel(KEY)
    for k in range(5):
        model.learn_scan(_scan(rng, T0 + k * 900_000, 40, signal=1.0))
    feats = [{"rec_60": 0.5, "vol_d_log": 0.3}, {"rec_60": -0.5, "vol_d_log": 0.3}]
    model.remember(model.batch(feats, {"reg_range": 1.0}), T0)
    clone = ScannerModel.from_state(json.loads(json.dumps(model.to_state())), key=KEY)
    b1, b2 = model.batch(feats, {"reg_range": 1.0}), clone.batch(feats, {"reg_range": 1.0})
    assert np.allclose(model.predict(b1)[1], clone.predict(b2)[1])
    assert clone.updates == 200 and clone.scans == 5 and clone.recent == model.recent
    assert clone.remembered(T0 + 60_000)[0] == {"reg_range": 1.0}
    assert clone.remembered(T0 + sc.CONTEXT_MAX_AGE_MS + 1)[0] is None  # old context is not reused
    legacy = sc.OnlineLogit()
    legacy.updates = 3227
    fresh = ScannerModel.from_state(legacy.to_state(), key=KEY)
    assert fresh.updates == 0 and fresh.dir.lessons == 0.0 and float(np.abs(fresh.dir.wr).sum()) == 0.0


def test_recent_summary_is_compared_with_the_reference():
    model = ScannerModel(KEY)
    rows = []
    for i in range(10):  # 8 of 10 went up; the recorded calls got 5 right
        rows.append(LearnSample(key=KEY, feats={"rec_60": 0.1 * i}, up=1 if i < 8 else 0, label_time=(T0 + H_MS) // 1000,
                                created_ms=T0, ret_pct=1.0 if i < 8 else -1.0, symbol=f"X:{i}", direction=1,
                                correct=1 if i < 5 else 0))
    model.learn_scan(rows)
    s = model.recent_summary()
    assert s == {"n": 10, "hits": 5, "ref_hits": 8, "ref_label": "siempre «alcista»", "windows": 1.0}
    text = model.recent_text()
    assert text.startswith("Acierto reciente 5 de 10 (50 %) · referencia 80 % (siempre «alcista»)")
    assert "sin conclusión todavía" in text
