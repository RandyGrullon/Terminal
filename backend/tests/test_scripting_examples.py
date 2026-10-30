"""BE-SCRIPT: bundled example scripts and the API reference.

- the registry is complete and every example compiles with the PyChart validator;
- every example runs in the real sandbox on synthetic data (long, short and volume-less series) — these tests
  skip until app.scripting.sandbox is available;
- the reference documents every API item of ARCHITECTURE §12 and matches the runtime `ta` namespace.
"""
from __future__ import annotations

import pytest

from app.scripting import examples as examples_mod
from app.scripting.reference import REFERENCE, reference_names
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

REQUIRED = {
    "Cruce de EMAs (estrategia)", "RSI con divergencias", "Supertrend + filtro ADX (estrategia)", "Squeeze de Bollinger",
    "MACD + volumen", "Ichimoku completo", "Bandas VWAP", "NEXUS-lite: mini ensamble con filtro de no operar",
    "Breakout de Donchian con stop ATR (estrategia)",
}
STRATEGIES = {"Cruce de EMAs (estrategia)", "Supertrend + filtro ADX (estrategia)",
              "NEXUS-lite: mini ensamble con filtro de no operar", "Breakout de Donchian con stop ATR (estrategia)"}
EXAMPLES = examples_mod.load_examples()
IDS = [ex["file"] for ex in EXAMPLES]

SECTION_12 = (
    ["open", "high", "low", "close", "volume", "time", "hl2", "hlc3", "ohlc4", "bar_index", "indicator", "strategy",
     "nz", "na", "fixnan", "plot", "plotshape", "hline", "bgcolor", "alertcondition", "strategy.entry",
     "strategy.exit", "strategy.close", "print", "color.red", "color.green"]
    + [f"input.{k}" for k in ("int", "float", "bool", "string", "select", "source")]
    + [f"ta.{k}" for k in (
        "sma ema wma hma rma vwma rsi macd stoch atr tr bb kc donchian supertrend sar adx dmi cci mfi obv roc mom "
        "stdev variance highest lowest highestbars lowestbars change crossover crossunder cross rising falling "
        "pivothigh pivotlow barssince valuewhen cum linreg correlation percentrank median vwap wpr cmo tsi").split()]
)


def _candles(n: int = 1500, seed: int = 7, regime: str = "auto", zero_volume: bool = False) -> list[dict]:
    rows = generate_candleset(n=n, seed=seed, tf="1h", regime=regime, end_time=FIXED_END).to_list()
    if zero_volume:
        for row in rows:
            row["volume"] = 0.0
    return rows


def _run(code: str, candles: list[dict], inputs: dict | None = None) -> dict:
    sandbox = pytest.importorskip("app.scripting.sandbox")
    return sandbox.run_script(code, candles, inputs or {}, symbol="SIM:NEXUS", tf="1h", timeout=30.0)


# ----------------------------------------------------------------------------- registry + compile


def test_registry_is_complete():
    names = [ex["name"] for ex in EXAMPLES]
    assert len(names) >= 9 and len(set(names)) == len(names)
    assert REQUIRED <= set(names)
    for ex in EXAMPLES:
        assert ex["description"] and len(ex["description"]) <= 500
        assert "\r" not in ex["code"] and ex["code"].lstrip().startswith("#")  # Spanish header comment
        assert "indicator(" in ex["code"] or "strategy(" in ex["code"]


@pytest.mark.parametrize("example", EXAMPLES, ids=IDS)
def test_example_compiles(example):
    validator = pytest.importorskip("app.scripting.validator")
    res = validator.compile_script(example["code"])
    assert res["ok"] is True, res["errors"]
    assert res["errors"] == []
    assert res["meta"]["kind"] == ("strategy" if example["name"] in STRATEGIES else "indicator")
    assert res["inputs"] and all(inp["default"] is not None for inp in res["inputs"])


# ----------------------------------------------------------------------------- sandbox runs


@pytest.mark.parametrize("example", EXAMPLES, ids=IDS)
def test_example_runs_on_synthetic_data(example):
    candles = _candles()
    res = _run(example["code"], candles)
    assert res["ok"] is True, (res["errors"], res.get("stdout"))
    assert res["errors"] == [] and res["stdout"].strip()
    assert len(res["time"]) == len(candles) and res["plots"]
    for plot in res["plots"]:
        assert len(plot["data"]) == len(candles)
        assert any(v is not None for v in plot["data"]), plot["title"]
    if example["name"] in STRATEGIES:
        assert res["strategy"] is not None and res["strategy"]["metrics"]["trades"] >= 1
    else:
        assert res["strategy"] is None


@pytest.mark.parametrize("example", EXAMPLES, ids=IDS)
def test_example_is_robust_to_short_and_volume_less_series(example):
    res = _run(example["code"], _candles(n=120, seed=3, zero_volume=True))
    assert res["ok"] is True, (res["errors"], res.get("stdout"))
    res = _run(example["code"], _candles(n=600, seed=5, regime="volatile"))
    assert res["ok"] is True, (res["errors"], res.get("stdout"))


def test_inputs_change_example_behaviour():
    ema = next(ex for ex in EXAMPLES if ex["file"] == "ema_cross.py")
    candles = _candles(seed=11)
    only_long = _run(ema["code"], candles)
    both = _run(ema["code"], candles, {"permitir_cortos": True, "tendencia": 100})
    assert only_long["ok"] and both["ok"]
    assert both["strategy"]["metrics"]["trades"] >= only_long["strategy"]["metrics"]["trades"]


def test_nexus_lite_no_trade_filter_blocks_entries():
    lite = next(ex for ex in EXAMPLES if ex["file"] == "nexus_lite.py")
    candles = _candles(n=2000, seed=13)
    gated = _run(lite["code"], candles)
    ungated = _run(lite["code"], candles, {"usar_filtro": False})
    assert gated["ok"] and ungated["ok"]
    assert gated["strategy"]["metrics"]["trades"] <= ungated["strategy"]["metrics"]["trades"]
    assert "bloqueados" in gated["stdout"]
    blocked = [m for m in gated["markers"] if m.get("text") == "NO"]
    assert blocked and not [m for m in ungated["markers"] if m.get("text") == "NO"]


# ----------------------------------------------------------------------------- reference


def test_reference_documents_every_section_12_item():
    names = reference_names()
    missing = [name for name in SECTION_12 if name not in names]
    assert not missing, missing
    for section in REFERENCE["sections"]:
        assert section["title"] and section["items"]
        for item in section["items"]:
            assert item["name"] and item["signature"] and len(item["doc"]) >= 10


def test_reference_matches_runtime_ta_namespace():
    runtime_ta = pytest.importorskip("app.scripting.runtime_ta")
    public = {name for name in dir(runtime_ta.TA) if not name.startswith("_")}
    documented = {name[3:] for name in reference_names() if name.startswith("ta.")}
    assert public - documented == set(), "funciones ta.* sin documentar"
    assert documented - public == set(), "funciones ta.* documentadas que no existen"


def test_reference_colors_match_runtime():
    runtime_plot = pytest.importorskip("app.scripting.runtime_plot")
    documented = {name[6:] for name in reference_names() if name.startswith("color.")}
    assert set(runtime_plot.PINE_COLORS) | {"new", "rgb"} == documented
