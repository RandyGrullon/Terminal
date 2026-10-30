"""Online learner: learns a predictive expert, calibration bookkeeping and JSON state round-trip."""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.engine.experts import EXPERT_IDS, N_EXPERTS, expert_signals
from app.engine.features import compute_features
from app.engine.learner import NexusLearner, feature_matrix
from app.engine.regime import REGIMES, detect_regime
from app.sim.synthetic import generate_candleset


def _toy_stream(n: int = 1500, seed: int = 1):
    """Expert 0 predicts the label 70% of the time; expert 1 is anti-predictive; the rest are noise."""
    rng = np.random.default_rng(seed)
    up = rng.random(n) < 0.5
    s = rng.uniform(-1, 1, size=(n, N_EXPERTS))
    agree = rng.random(n) < 0.7
    sign = np.where(up, 1.0, -1.0)
    s[:, 0] = np.where(agree, sign, -sign) * rng.uniform(0.4, 1.0, n)
    s[:, 1] = -s[:, 0]
    regime = np.zeros(n, dtype=np.int64)
    x = feature_matrix(s, regime, np.full(n, 0.5))
    return s, x, regime, up.astype(int)


def test_learns_predictive_expert_and_fades_inverse():
    s, x, regime, up = _toy_stream()
    lr = NexusLearner()
    for k in range(len(up)):
        _, _, p = lr.predict(x[k], s[k], int(regime[k]))
        lr.update(x[k], s[k], int(regime[k]), int(up[k]), p, 1_000 + k)
    w = lr.weights_for(0)
    assert w[0] > 0.2 and w[1] < -0.1  # trusted vs faded (expert 1 inverted == expert 0: they share the weight)
    assert lr.hit_rate > 0.6 and lr.brier < 0.24
    late = []
    for k in range(200):
        _, _, p = lr.predict(x[k], s[k], 0)
        late.append((p >= 0.5) == bool(up[k]))
    assert np.mean(late) > 0.62
    assert lr.trained_until == 1_000 + len(up) - 1 and lr.updates == len(up)
    cal = lr.calibration()
    assert sum(b["count"] for b in cal) == lr.n_eval and all(0 <= b["observed"] <= 1 for b in cal)
    assert lr.rolling and lr.rolling[-1][0] > lr.rolling[0][0]


def test_state_round_trip_is_exact_and_json_safe():
    s, x, regime, up = _toy_stream(400, seed=3)
    lr = NexusLearner(learning_rate=0.05)
    for k in range(300):
        _, _, p = lr.predict(x[k], s[k], 0)
        lr.update(x[k], s[k], 0, int(up[k]), p, k)
    state = json.loads(json.dumps(lr.to_state(), allow_nan=False))
    clone = NexusLearner.from_state(state, learning_rate=0.05)
    for k in range(300, 400):
        a = lr.predict(x[k], s[k], 0)
        b = clone.predict(x[k], s[k], 0)
        assert a == pytest.approx(b, abs=1e-6)
    assert clone.updates == lr.updates and clone.hit_rate == pytest.approx(lr.hit_rate)
    assert set(state) >= {"version", "trained_until", "updates", "hits", "n_eval", "brier_sum", "hedge", "theta"}
    assert set(clone.weights_dict()) == set(REGIMES)
    assert set(clone.weights_dict()["trend_up"]) == set(EXPERT_IDS)


@pytest.mark.parametrize("bad", [None, {}, {"version": 99}, {"version": 1, "hedge": "x", "theta": [1, 2]},
                                 {"version": 1, "experts": ["other"], "updates": 5}])
def test_corrupt_or_foreign_state_starts_fresh(bad):
    lr = NexusLearner.from_state(bad)
    assert lr.updates == 0 or bad.get("experts") == ["other"]
    x = np.zeros(lr.dim)
    x[-1] = 1.0
    vote, _, p = lr.predict(x, np.zeros(N_EXPERTS), 0)
    assert vote == 0.0 and p == pytest.approx(0.5)


def test_expert_signals_are_bounded_and_causal():
    cs = generate_candleset(n=600, seed=9, tf="1h", end_time=1_760_000_400)
    f = compute_features(cs)
    reg = detect_regime(f)
    sig = expert_signals(f, reg)
    assert sig.shape == (600, N_EXPERTS) and np.all(np.abs(sig) <= 1.0) and np.isfinite(sig).all()
    f2 = compute_features(cs.upto(399))
    sig2 = expert_signals(f2, detect_regime(f2))
    assert np.array_equal(sig[:400], sig2)
    assert np.array_equal(reg[:400], detect_regime(f2))
    assert np.allclose(np.nan_to_num(f.htf_trend[:400], nan=9), np.nan_to_num(f2.htf_trend, nan=9))
