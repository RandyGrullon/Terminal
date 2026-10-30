"""Indicator library: catalog schema (IndicatorSpec contract), parameter validation and the compute API."""
from __future__ import annotations

import json
import math
import re

import numpy as np
import pytest

from app.engine.indicators import (
    CATALOG,
    CATEGORIES,
    IndicatorError,
    catalog_ids,
    compute_indicator,
    compute_items,
    get_spec,
    normalize_params,
)
from app.sim.synthetic import generate_candleset

FIXED_END = 1_760_000_400
PARAM_TYPES = {"int", "float", "bool", "select", "source"}
OUTPUT_TYPES = {"line", "histogram", "area", "dots", "step"}
SOURCES = {"close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "hlcc4"}
COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")

# docs/ARCHITECTURE.md section 9: every listed indicator must exist
REQUIRED_IDS = {
    "sma", "ema", "wma", "hma", "dema", "tema", "trima", "kama", "zlema", "alma", "vwma", "smma", "lsma",
    "mcginley", "t3", "vwap", "vwap_rolling", "supertrend", "psar", "ichimoku", "bbands", "keltner", "donchian",
    "envelopes", "linreg_channel", "chandelier", "pivots", "zigzag", "fractals", "ma_ribbon",
    "rsi", "stoch", "stochrsi", "macd", "cci", "willr", "roc", "mom", "trix", "tsi", "uo", "ao", "ac", "kst",
    "ppo", "dpo", "coppock", "crsi", "fisher", "stc", "elder_ray", "bop", "cmo", "rvgi", "aroon", "aroon_osc",
    "adx", "vortex", "choppiness", "mass_index",
    "atr", "natr", "bb_pctb", "bb_width", "hv", "stdev", "ulcer", "chaikin_vol",
    "volume", "obv", "ad", "cmf", "chaikin_osc", "mfi", "force", "eom", "vol_osc", "pvt", "nvi", "pvi",
    "klinger", "rvol",
    "zscore", "hurst", "er", "corr_time",
    "nexus_score", "nexus_prob",
}


@pytest.fixture(scope="module")
def cs():
    return generate_candleset(n=600, seed=11, tf="1h", end_time=FIXED_END)


# ----------------------------------------------------------------------------- catalog schema


def test_catalog_size_ids_and_categories():
    ids = [s["id"] for s in CATALOG]
    assert len(CATALOG) >= 70
    assert len(ids) == len(set(ids)), "ids duplicados"
    assert ids == catalog_ids()
    assert REQUIRED_IDS <= set(ids), f"faltan: {sorted(REQUIRED_IDS - set(ids))}"
    assert {s["category"] for s in CATALOG} == set(CATEGORIES)
    names = [s["name"] for s in CATALOG]
    assert len(names) == len(set(names)), "nombres duplicados"
    # ordered by category as the UI expects
    ranks = [CATEGORIES.index(s["category"]) for s in CATALOG]
    assert ranks == sorted(ranks)
    json.dumps(CATALOG, allow_nan=False)  # JSON-safe


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s["id"])
def test_spec_shape(spec):
    allowed = {"id", "name", "category", "overlay", "description", "params", "outputs", "levels", "range"}
    assert set(spec) <= allowed
    assert {"id", "name", "category", "overlay", "description", "params", "outputs"} <= set(spec)
    assert re.fullmatch(r"[a-z][a-z0-9_]*", spec["id"])
    assert isinstance(spec["overlay"], bool)
    assert spec["name"].strip() and len(spec["description"]) >= 30
    names = set()
    for p in spec["params"]:
        assert set(p) <= {"name", "label", "type", "default", "min", "max", "step", "options"}
        assert p["type"] in PARAM_TYPES and p["label"].strip()
        assert p["name"] not in names
        names.add(p["name"])
        if p["type"] in ("int", "float"):
            assert p["min"] <= p["default"] <= p["max"] and p["step"] > 0
            if p["type"] == "int":
                assert all(type(p[k]) is int for k in ("default", "min", "max", "step"))
            else:
                assert all(isinstance(p[k], float) for k in ("default", "min", "max", "step"))
        elif p["type"] == "bool":
            assert isinstance(p["default"], bool)
        else:
            assert p["options"] and p["default"] in p["options"]
            if p["type"] == "source":
                assert set(p["options"]) <= SOURCES
    keys = set()
    assert spec["outputs"]
    for o in spec["outputs"]:
        assert set(o) == {"key", "title", "type", "color"}
        assert o["type"] in OUTPUT_TYPES and o["title"].strip() and COLOR_RE.match(o["color"])
        assert o["key"] not in keys
        keys.add(o["key"])
    if "levels" in spec:
        assert spec["levels"] and all(isinstance(v, float) and math.isfinite(v) for v in spec["levels"])
        assert not spec["overlay"]
    if "range" in spec:
        lo, hi = spec["range"]
        assert isinstance(lo, float) and isinstance(hi, float) and lo < hi
        assert not spec["overlay"]


def test_get_spec_returns_copies_and_is_case_insensitive():
    spec = get_spec("RSI")
    assert spec is not None and spec["id"] == "rsi"
    spec["name"] = "roto"
    assert get_spec("rsi")["name"] != "roto"
    assert get_spec(" macd ")["id"] == "macd"
    assert get_spec("no_existe") is None
    assert get_spec(None) is None  # type: ignore[arg-type]


def test_oscillator_levels_present():
    assert get_spec("rsi")["levels"] == [30.0, 50.0, 70.0]
    assert get_spec("rsi")["range"] == [0.0, 100.0]
    assert get_spec("stoch")["levels"][0] == 20.0
    assert get_spec("willr")["range"] == [-100.0, 0.0]
    assert get_spec("cci")["levels"] == [-100.0, 0.0, 100.0]


# ----------------------------------------------------------------------------- parameter validation


def test_defaults_and_coercion():
    assert normalize_params("rsi") == {"source": "close", "length": 14, "ma_length": 14}
    p = normalize_params("rsi", {"length": "21", "source": "HLC3", "extra": 1})
    assert p == {"source": "hlc3", "length": 21, "ma_length": 14}  # unknown names ignored
    assert normalize_params("rsi", {"length": 14.6})["length"] == 15
    assert normalize_params("rsi", {"length": None})["length"] == 14
    assert normalize_params("bbands", {"mult": "2,5"})["mult"] == 2.5
    assert normalize_params("pivots", {"kind": "camarilla", "anchor": "dia"}) == {"kind": "Camarilla", "anchor": "Día"}
    assert normalize_params("chandelier", {"use_close": "no"})["use_close"] is False
    assert normalize_params("chandelier", {"use_close": "Sí"})["use_close"] is True
    assert normalize_params("chandelier", {"use_close": 1})["use_close"] is True


def test_numbers_are_clamped():
    assert normalize_params("rsi", {"length": 100000})["length"] == 200
    assert normalize_params("rsi", {"length": -5})["length"] == 2
    assert normalize_params("bbands", {"mult": 99.0})["mult"] == 5.0
    out = compute_indicator("sma", generate_candleset(n=50, seed=1, end_time=FIXED_END), {"length": 10**9})
    assert np.isnan(out["sma"]).all()  # clamped to 1000 > 50 bars: all warm-up, no exception


@pytest.mark.parametrize(
    "iid, params",
    [
        ("rsi", {"length": "abc"}),
        ("rsi", {"length": float("nan")}),
        ("rsi", {"length": float("inf")}),
        ("rsi", {"length": True}),
        ("rsi", {"length": [14]}),
        ("rsi", {"source": "precio"}),
        ("rsi", {"source": 3}),
        ("pivots", {"kind": "Inventado"}),
        ("chandelier", {"use_close": "quizás"}),
        ("chandelier", {"use_close": 2}),
        ("rsi", ["length", 14]),
        ("rsi", "length=14"),
    ],
)
def test_invalid_params_raise_spanish_error(cs, iid, params):
    with pytest.raises(IndicatorError) as err:
        compute_indicator(iid, cs, params)  # type: ignore[arg-type]
    msg = str(err.value)
    assert ("inválido" in msg or "parámetros" in msg) and msg[0].isupper()


@pytest.mark.parametrize("bad_id", ["no_existe", "", "   ", None, 42])
def test_unknown_indicator(cs, bad_id):
    with pytest.raises(IndicatorError, match="Indicador desconocido"):
        compute_indicator(bad_id, cs)  # type: ignore[arg-type]
    with pytest.raises(IndicatorError):
        normalize_params(bad_id)  # type: ignore[arg-type]


def test_indicator_error_is_value_error():
    assert issubclass(IndicatorError, ValueError)


# ----------------------------------------------------------------------------- compute API


def test_compute_returns_float_arrays_for_every_output(cs):
    out = compute_indicator("macd", cs, {"fast": 8, "slow": 21})
    assert set(out) == {"hist", "macd", "signal"}
    for v in out.values():
        assert isinstance(v, np.ndarray) and v.dtype == np.float64 and v.shape == (len(cs),)
    np.testing.assert_allclose(out["hist"], out["macd"] - out["signal"], equal_nan=True)


def test_empty_candleset_returns_empty_outputs(cs):
    empty = cs.upto(-1)
    assert len(empty) == 0
    out = compute_indicator("ichimoku", empty)
    assert set(out) == {"tenkan", "kijun", "span_a", "span_b"} and all(v.shape == (0,) for v in out.values())


def test_compute_items_response_shape(cs):
    tail = cs.tail(40)
    res = compute_items(
        tail,
        [
            {"uid": "a", "id": "rsi", "params": {"length": 14}},
            {"uid": "b", "id": "no_existe", "params": {}},
            {"uid": "c", "id": "sma", "params": {"length": "x"}},
            {"uid": "d", "id": "BBANDS"},
            "basura",
        ],
    )
    assert res["time"] == [int(t) for t in tail.time]
    a, b, c, d, e = res["results"]
    assert a["uid"] == "a" and a["error"] is None and set(a["outputs"]) == {"rsi", "ma"}
    assert len(a["outputs"]["rsi"]) == 40 and a["outputs"]["rsi"][0] is None and isinstance(a["outputs"]["rsi"][-1], float)
    assert b["error"].startswith("Indicador desconocido") and b["outputs"] == {}
    assert "inválido" in c["error"]
    assert d["error"] is None and set(d["outputs"]) == {"basis", "upper", "lower"}
    assert e["error"] and e["outputs"] == {}
    json.dumps(res, allow_nan=False)
    assert compute_items(tail, None) == {"time": res["time"], "results": []}
