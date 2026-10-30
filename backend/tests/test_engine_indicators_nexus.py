"""NEXUS indicators (nexus_score / nexus_prob): engine adapter with a fake engine + the real engine when present."""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import numpy as np
import pytest

from app.engine import indicators_nexus
from app.engine.indicators import IndicatorError, compute_indicator
from app.sim.synthetic import generate_candleset

FIXED_END = 1_760_000_400
UNAVAILABLE = "Motor NEXUS no disponible todavía"


@pytest.fixture(autouse=True)
def _clear_cache():
    indicators_nexus._CACHE.clear()
    yield
    indicators_nexus._CACHE.clear()


@pytest.fixture(scope="module")
def cs():
    return generate_candleset(n=400, seed=11, tf="1h", end_time=FIXED_END)


def _fake_engine(mode: str = "columns", warmup: int = 50, error: Exception | None = None):
    """Module object mimicking app.engine.nexus: causal score = tanh(5-bar return), prob from the score."""
    calls = {"n": 0}

    @dataclass
    class NexusParams:
        horizon: int = 5

        @classmethod
        def from_settings(cls, settings, **overrides):
            return cls(**overrides)

    def run_nexus(cs, params=None, prior=None):
        calls["n"] += 1
        if error is not None:
            raise error
        c = np.asarray(cs.close, dtype=float)
        ret = np.full(c.shape[0], np.nan)
        ret[5:] = c[5:] / c[:-5] - 1.0
        score = np.tanh(20.0 * ret)
        t, s = cs.time[warmup:], score[warmup:]  # the engine only reports bars after its warm-up
        cols = {
            "time": [int(x) for x in t],
            "score": [None if not np.isfinite(v) else float(v) for v in s],
            "prob_up": [None if not np.isfinite(v) else float(0.5 + 0.4 * v) for v in s],
        }
        if mode == "columns":
            return types.SimpleNamespace(bars=cols)
        if mode == "rows":
            rows = [{"time": a, "score": b, "prob_up": c_} for a, b, c_ in zip(cols["time"], cols["score"], cols["prob_up"])]
            return types.SimpleNamespace(bars=rows)
        return types.SimpleNamespace(bars=None, to_response=lambda _cs=None: {"bars": cols})

    mod = types.ModuleType("app.engine.nexus")
    mod.NexusParams = NexusParams
    mod.run_nexus = run_nexus
    return mod, calls


@pytest.mark.parametrize("mode", ["columns", "rows", "response"])
def test_fake_engine_series_alignment(monkeypatch, cs, mode):
    mod, calls = _fake_engine(mode)
    monkeypatch.setitem(sys.modules, "app.engine.nexus", mod)
    score = compute_indicator("nexus_score", cs, {"smooth": 1})
    prob = compute_indicator("nexus_prob", cs)
    assert calls["n"] == 1, "una sola ejecución del motor para ambos indicadores (caché)"
    assert score["score"].shape == (len(cs),) and prob["prob"].shape == (len(cs),)
    assert np.isnan(score["score"][:50]).all() and np.isfinite(score["score"][60:]).all()
    c = cs.close
    expected = np.tanh(20.0 * (c[100] / c[95] - 1.0))
    assert score["score"][100] == pytest.approx(expected)
    np.testing.assert_allclose(score["signal"], score["score"], equal_nan=True)  # smooth = 1
    assert prob["prob"][100] == pytest.approx(100.0 * (0.5 + 0.4 * expected))
    f = prob["prob"][np.isfinite(prob["prob"])]
    assert f.min() >= 0.0 and f.max() <= 100.0


def test_fake_engine_wrapper_is_causal(monkeypatch, cs):
    mod, _ = _fake_engine()
    monkeypatch.setitem(sys.modules, "app.engine.nexus", mod)
    full = compute_indicator("nexus_score", cs, {"smooth": 5})
    for k in (120, 250, 398):
        part = compute_indicator("nexus_score", cs.upto(k), {"smooth": 5})
        for key in ("score", "signal"):
            np.testing.assert_allclose(part[key], full[key][: k + 1], rtol=1e-9, equal_nan=True)


def test_missing_engine_reports_unavailable(monkeypatch, cs):
    monkeypatch.setitem(sys.modules, "app.engine.nexus", None)  # makes the import raise ImportError
    with pytest.raises(IndicatorError, match=UNAVAILABLE):
        compute_indicator("nexus_score", cs)
    with pytest.raises(IndicatorError, match=UNAVAILABLE):
        compute_indicator("nexus_prob", cs)


def test_engine_partial_install_reports_unavailable(monkeypatch, cs):
    mod, _ = _fake_engine(error=ModuleNotFoundError("No module named 'app.engine.nexus_loop'"))
    monkeypatch.setitem(sys.modules, "app.engine.nexus", mod)
    with pytest.raises(IndicatorError, match=UNAVAILABLE):
        compute_indicator("nexus_score", cs)


def test_engine_failure_is_wrapped(monkeypatch, cs):
    mod, _ = _fake_engine(error=RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "app.engine.nexus", mod)
    with pytest.raises(IndicatorError, match="El motor NEXUS falló"):
        compute_indicator("nexus_prob", cs)


def test_engine_without_series_is_reported(monkeypatch, cs):
    mod, _ = _fake_engine()
    mod.run_nexus = lambda cs, params=None, prior=None: types.SimpleNamespace(bars={"otra": []})
    monkeypatch.setitem(sys.modules, "app.engine.nexus", mod)
    with pytest.raises(IndicatorError, match="no devolvió series"):
        compute_indicator("nexus_score", cs)


# ----------------------------------------------------------------------------- real engine (when available)


def _real_or_skip(iid: str, cs, params=None) -> dict:
    pytest.importorskip("app.engine.nexus")
    try:
        return compute_indicator(iid, cs, params)
    except IndicatorError as exc:
        if str(exc) == UNAVAILABLE:
            pytest.skip("El motor NEXUS todavía no está completo")
        raise


def test_real_engine_score_and_probability(cs):
    score = _real_or_skip("nexus_score", cs)
    prob = _real_or_skip("nexus_prob", cs)
    for out in (score, prob):
        assert all(v.shape == (len(cs),) and not np.isinf(v).any() for v in out.values())
    s = score["score"][np.isfinite(score["score"])]
    p = prob["prob"][np.isfinite(prob["prob"])]
    assert s.size > 0 and p.size > 0
    assert s.min() >= -1.0 and s.max() <= 1.0 and p.min() >= 0.0 and p.max() <= 100.0


def test_real_engine_is_causal(cs):
    full = _real_or_skip("nexus_score", cs, {"smooth": 1})["score"]
    k = 300
    part = _real_or_skip("nexus_score", cs.upto(k), {"smooth": 1})["score"]
    np.testing.assert_allclose(part, full[: k + 1], rtol=1e-6, atol=1e-9, equal_nan=True)
