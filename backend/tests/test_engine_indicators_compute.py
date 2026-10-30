"""Indicator library: every indicator computes, is causal (no lookahead), fast, and matches reference formulas."""
from __future__ import annotations

import time
import warnings

import numpy as np
import pytest

from app.data.base import CandleSet
from app.engine.indicators import CATALOG, IndicatorError, compute_indicator
from app.sim.synthetic import generate_candleset

FIXED_END = 1_760_000_400
NON_NEXUS = [s for s in CATALOG if s["category"] != "NEXUS"]
# indicators that refuse assets without volume data (clear Spanish error instead of a flat/empty series)
VOLUME_BASED = {s["id"] for s in CATALOG if s["category"] == "Volumen"} | {"vwma", "vwap", "vwap_rolling"}

# outputs that are split by state (only one of the group is drawn at each bar) or may legitimately be empty
SPARSE_GROUPS = {
    "supertrend": ("up", "down"),
    "psar": ("bull", "bear"),
    "chandelier": ("long", "short"),
    "volume": ("up", "down"),
    "squeeze": ("on", "off"),
    "sr_levels": ("resistance", "support"),
}


@pytest.fixture(scope="module")
def cs600():
    return generate_candleset(n=600, seed=11, tf="1h", end_time=FIXED_END)


@pytest.fixture(scope="module")
def datasets():
    return [
        generate_candleset(n=600, seed=11, tf="1h", end_time=FIXED_END),
        generate_candleset(n=500, seed=5, tf="15m", regime="volatile", end_time=FIXED_END),
        generate_candleset(n=400, seed=3, tf="1d", regime="bull", end_time=FIXED_END),
    ]


def _assert_prefix_equal(part: dict, full: dict, k: int, label: str) -> None:
    """Bit-exact: values known at bar k must not change when later bars exist (not even in the last bit, or
    exact comparisons downstream such as crossovers would differ)."""
    for key, a in part.items():
        f = full[key][: k + 1]
        assert a.shape == f.shape, f"{label}.{key}: longitud"
        na, nf = np.isnan(a), np.isnan(f)
        assert np.array_equal(na, nf), f"{label}.{key}: NaN distinto en la vela {int(np.flatnonzero(na != nf)[0])} (k={k})"
        diff = np.flatnonzero(a[~na] != f[~nf])
        assert diff.size == 0, f"{label}.{key}: lookahead (k={k}, {diff.size} velas distintas)"


# ----------------------------------------------------------------------------- every indicator


@pytest.mark.parametrize("spec", NON_NEXUS, ids=lambda s: s["id"])
def test_computes_on_600_bars(spec, cs600):
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no numpy RuntimeWarning may escape
        out = compute_indicator(spec["id"], cs600)
    assert set(out) == {o["key"] for o in spec["outputs"]}
    n = len(cs600)
    for key, v in out.items():
        assert v.dtype == np.float64 and v.shape == (n,)
        assert not np.isinf(v).any()
    tail = slice(n - 100, n)
    sparse = SPARSE_GROUPS.get(spec["id"], ())
    for key, v in out.items():
        if key not in sparse:
            assert np.isfinite(v[tail]).all(), f"{spec['id']}.{key}: NaN después del calentamiento"
    if sparse:
        union = np.zeros(100, dtype=bool)
        for key in sparse:
            union |= np.isfinite(out[key][tail])
        if spec["id"] not in ("squeeze", "sr_levels"):
            assert union.all(), f"{spec['id']}: ninguna salida activa en algunas velas"
        else:
            assert union.any()
    if "range" in spec:
        lo, hi = spec["range"]
        for key, v in out.items():
            f = v[np.isfinite(v)]
            assert f.size == 0 or (f.min() >= lo - 1e-9 and f.max() <= hi + 1e-9), f"{spec['id']}.{key} fuera de rango"


@pytest.mark.parametrize("spec", NON_NEXUS, ids=lambda s: s["id"])
def test_causality_no_lookahead(spec, datasets):
    for cs in datasets:
        full = compute_indicator(spec["id"], cs)
        for k in (45, len(cs) // 2, len(cs) - 2):
            part = compute_indicator(spec["id"], cs.upto(k))
            _assert_prefix_equal(part, full, k, f"{spec['id']}@{cs.tf}")


@pytest.mark.parametrize("spec", NON_NEXUS, ids=lambda s: s["id"])
def test_tiny_flat_and_volumeless_inputs(spec, cs600):
    n = 120
    t = FIXED_END - 3600 * np.arange(n, 0, -1)
    flat = CandleSet("X:FLAT", "1h", t, *(np.full(n, 5.0) for _ in range(4)), np.zeros(n))
    novol = CandleSet("X:NOVOL", "1h", cs600.time, cs600.open, cs600.high, cs600.low, cs600.close, np.zeros(len(cs600)))
    for c in (cs600.upto(0), cs600.upto(2), flat, novol):
        if c.symbol in ("X:FLAT", "X:NOVOL") and spec["id"] in VOLUME_BASED:
            with pytest.raises(IndicatorError, match="necesita datos de volumen"):
                compute_indicator(spec["id"], c)
            continue
        out = compute_indicator(spec["id"], c)
        assert all(v.shape == (len(c),) and not np.isinf(v).any() for v in out.values())


@pytest.mark.parametrize("spec", NON_NEXUS, ids=lambda s: s["id"])
def test_param_extremes_and_all_options(spec, cs600):
    variants = []
    for which in ("min", "max"):
        params = {}
        for p in spec["params"]:
            if p["type"] in ("int", "float"):
                params[p["name"]] = p[which]
            elif p["type"] == "bool":
                params[p["name"]] = which == "max"
        variants.append(params)
    for p in spec["params"]:
        if p["type"] in ("select", "source"):
            variants += [{p["name"]: opt} for opt in p["options"]]
    for params in variants:
        out = compute_indicator(spec["id"], cs600, params)
        assert all(v.shape == (len(cs600),) for v in out.values())


def test_performance_budget():
    cs = generate_candleset(n=1000, seed=21, tf="1h", end_time=FIXED_END)
    compute_indicator("sma", cs)  # warm imports
    t0 = time.perf_counter()
    for spec in NON_NEXUS:
        compute_indicator(spec["id"], cs)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"{len(NON_NEXUS)} indicadores en {elapsed:.2f} s"


# --- end of section A ---


# ----------------------------------------------------------------------------- reference values


def _series_cs(close: np.ndarray, tf: str = "1h", volume: np.ndarray | None = None) -> CandleSet:
    n = close.shape[0]
    t = FIXED_END - 3600 * np.arange(n, 0, -1)
    v = np.ones(n) if volume is None else volume
    return CandleSet("X:REF", tf, t, close, close + 0.5, close - 0.5, close, v)


def test_sma_ema_and_bollinger_match_reference(cs600):
    import pandas as pd

    c = pd.Series(cs600.close)
    np.testing.assert_allclose(compute_indicator("sma", cs600, {"length": 20})["sma"], c.rolling(20).mean(), equal_nan=True)
    ema = compute_indicator("ema", cs600, {"length": 10})["ema"]
    ref = np.full(len(c), np.nan)
    ref[9] = c.iloc[:10].mean()
    for i in range(10, len(c)):
        ref[i] = ref[i - 1] + 2.0 / 11.0 * (c.iloc[i] - ref[i - 1])
    np.testing.assert_allclose(ema, ref, equal_nan=True)
    bb = compute_indicator("bbands", cs600, {"length": 20, "mult": 2.0})
    np.testing.assert_allclose(bb["upper"] - bb["basis"], 2.0 * c.rolling(20).std(ddof=0), rtol=1e-9, equal_nan=True)


def test_rsi_and_stoch_extremes():
    up = _series_cs(np.linspace(10.0, 50.0, 60))
    down = _series_cs(np.linspace(50.0, 10.0, 60))
    flat = _series_cs(np.full(60, 7.0))
    assert np.allclose(compute_indicator("rsi", up)["rsi"][20:], 100.0)
    assert np.allclose(compute_indicator("rsi", down)["rsi"][20:], 0.0)
    assert np.allclose(compute_indicator("rsi", flat)["rsi"][20:], 50.0)
    assert np.allclose(compute_indicator("stoch", flat)["k"][20:], 50.0)


def test_linear_trend_statistics():
    cs = _series_cs(100.0 + 0.5 * np.arange(200))
    ct = compute_indicator("corr_time", cs, {"length": 20})
    assert np.allclose(ct["r2"][19:], 1.0) and np.allclose(ct["r"][19:], 1.0)
    slope = compute_indicator("linreg_slope", cs, {"length": 20})["slope"]
    np.testing.assert_allclose(slope[19:], 100.0 * 0.5 / cs.close[19:], rtol=1e-9)
    assert np.allclose(compute_indicator("er", cs, {"length": 10})["er"][10:], 1.0)


def test_vwap_resets_every_session(cs600):
    out = compute_indicator("vwap", cs600, {"anchor": "Día", "source": "hlc3", "mult": 1.0})["vwap"]
    tp = (cs600.high + cs600.low + cs600.close) / 3.0
    day = cs600.time // 86400
    for d in np.unique(day)[:5]:
        idx = np.flatnonzero(day == d)
        w = cs600.volume[idx]
        ref = np.cumsum(tp[idx] * w) / np.cumsum(w)
        np.testing.assert_allclose(out[idx], ref, rtol=1e-9)
        assert out[idx[0]] == pytest.approx(tp[idx[0]])


def test_classic_pivots_use_previous_day(cs600):
    out = compute_indicator("pivots", cs600, {"kind": "Clásico", "anchor": "Día"})
    day = cs600.time // 86400
    days = np.unique(day)
    for prev_d, d in zip(days[:4], days[1:5]):
        prev = day == prev_d
        h, lo, c = cs600.high[prev].max(), cs600.low[prev].min(), cs600.close[prev][-1]
        pp = (h + lo + c) / 3.0
        i = int(np.flatnonzero(day == d)[0])
        assert out["p"][i] == pytest.approx(pp)
        assert out["r1"][i] == pytest.approx(2 * pp - lo) and out["s1"][i] == pytest.approx(2 * pp - h)
        assert out["r2"][i] == pytest.approx(pp + (h - lo)) and out["s3"][i] == pytest.approx(lo - 2 * (h - pp))
    assert np.isnan(out["p"][day == days[0]]).all()
    demark = compute_indicator("pivots", cs600, {"kind": "DeMark", "anchor": "Día"})
    assert np.isnan(demark["r2"]).all() and np.isfinite(demark["r1"][-50:]).all()


def test_split_outputs_are_exclusive(cs600):
    for iid, (a, b) in (("supertrend", ("up", "down")), ("psar", ("bull", "bear")), ("chandelier", ("long", "short"))):
        out = compute_indicator(iid, cs600)
        both = np.isfinite(out[a]) & np.isfinite(out[b])
        assert not both.any(), f"{iid}: dos lados activos a la vez"
    st = compute_indicator("supertrend", cs600)
    assert (st["up"][np.isfinite(st["up"])] <= cs600.high[np.isfinite(st["up"])] + 1e-9).all()


def test_ichimoku_cloud_is_shifted_forward_causally(cs600):
    out = compute_indicator("ichimoku", cs600, {"displacement": 26})
    mid = (out["tenkan"] + out["kijun"]) / 2.0
    np.testing.assert_allclose(out["span_a"][25:], mid[:-25], equal_nan=True)


def test_swing_levels_are_confirmed_past_prices(cs600):
    fr = compute_indicator("fractals", cs600, {"period": 2})
    for i in np.flatnonzero(np.isfinite(fr["up"]))[::25]:
        assert fr["up"][i] in cs600.high[: i - 1]  # confirmed 2 bars after the fractal bar
    zz = compute_indicator("zigzag", cs600, {"deviation": 2.0})
    for i in np.flatnonzero(np.isfinite(zz["low"]))[::25]:
        assert zz["low"][i] in cs600.low[: i + 1]
    sr = compute_indicator("sr_levels", cs600)
    ok = np.isfinite(sr["support"])
    assert (sr["support"][ok] < cs600.close[ok]).all()
    ok = np.isfinite(sr["resistance"])
    assert (sr["resistance"][ok] > cs600.close[ok]).all()


def test_volume_derived_values(cs600):
    rvol = compute_indicator("rvol", cs600, {"length": 20})["rvol"]
    i = 300
    assert rvol[i] == pytest.approx(cs600.volume[i] / cs600.volume[i - 20 : i].mean())
    vol = compute_indicator("volume", cs600)
    total = np.where(np.isfinite(vol["up"]), vol["up"], vol["down"])
    np.testing.assert_allclose(total, cs600.volume)
    dd = compute_indicator("drawdown", cs600)["dd"]
    assert (dd <= 1e-12).all() and dd[int(np.argmax(cs600.close))] == pytest.approx(0.0)


def test_session_range_tracks_running_extremes(cs600):
    out = compute_indicator("session_hl", cs600, {"anchor": "Día"})
    day = cs600.time // 86400
    idx = np.flatnonzero(day == np.unique(day)[2])
    np.testing.assert_allclose(out["high"][idx], np.maximum.accumulate(cs600.high[idx]))
    np.testing.assert_allclose(out["open"][idx], cs600.open[idx[0]])


def test_volume_requirement_flags_and_message(cs600):
    from app.engine.indicators_core import REGISTRY

    assert {k for k, e in REGISTRY.items() if e.needs_volume} == VOLUME_BASED
    novol = CandleSet("X:FX", "1h", cs600.time, cs600.open, cs600.high, cs600.low, cs600.close, np.zeros(len(cs600)))
    with pytest.raises(IndicatorError, match=r"«VWAP de sesión \(anclado\)» necesita datos de volumen"):
        compute_indicator("vwap", novol)
    assert np.isfinite(compute_indicator("rsi", novol)["rsi"][-1])  # price-only indicators still work


def test_ta_primitives_are_bit_exact_on_prefixes():
    """Regression: a whole-array offset in rolling sums / BLAS row grouping made truncated series differ in the
    last bits, flipping StochRSI crossovers inside the NEXUS engine."""
    from app.engine import ta

    cs = generate_candleset(n=900, seed=4, tf="1h", end_time=FIXED_END, start_price=30000.0)
    c, h, lo = cs.close, cs.high, cs.low
    fns = {
        "rolling_sum": lambda x, hh, ll: ta.rolling_sum(x * 1e6, 20),
        "stochrsi_cross": lambda x, hh, ll: np.asarray(ta.crossover(*ta.stochrsi(x, 14, 14, 3, 3)), dtype=float),
        "linreg": lambda x, hh, ll: ta.linreg_stats(x, 50)["tstat"],
        "wma": lambda x, hh, ll: ta.wma(x, 30),
        "alma": lambda x, hh, ll: ta.alma(x, 21),
        "cci": lambda x, hh, ll: ta.cci(hh, ll, x, 20),
        "std": lambda x, hh, ll: ta.rolling_std(x, 25),
        "hurst": lambda x, hh, ll: ta.hurst(x, 100),
        "swma": lambda x, hh, ll: ta.swma(x),
    }
    for name, fn in fns.items():
        full = fn(c, h, lo)
        for k in (150, 451, 777, 898):
            part = fn(c[: k + 1], h[: k + 1], lo[: k + 1])
            assert np.array_equal(part, full[: k + 1], equal_nan=True), f"{name}: difiere al truncar en {k}"
