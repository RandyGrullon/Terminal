"""PyChart end to end: ``run_script`` launches the isolated worker and returns a ScriptRunResult."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.scripting import sandbox
from app.scripting.sandbox import clean_candles, run_script
from app.scripting.validator import compile_script
from app.sim.synthetic import generate_candleset

RESULT_KEYS = {
    "ok", "errors", "stdout", "duration_ms", "meta", "time", "plots", "markers", "hlines", "bgcolors", "alerts",
    "strategy", "candles",
}


@pytest.fixture(scope="module")
def candles():
    return generate_candleset(n=500, seed=8, tf="1h", end_time=1_760_000_400).to_list()


INDICATOR = """import numpy as np
indicator('Bandas', overlay=True)
length = input.int(20, 'Longitud', minval=2)
mult = input.float(2.0, 'Desviaciones')
mid, up, lo = ta.bb(close, length, mult)
plot(mid, 'Media', color.orange)
plot(up, 'Superior')
plot(lo, 'Inferior')
plot(ta.rsi(close, 14), 'RSI', pane=1)
hline(70, 'Sobrecompra')
plotshape(ta.crossover(close, up), 'Ruptura', location.abovebar, color.red, shape.triangledown)
bgcolor(close < lo, color.new(color.green, 85))
alertcondition(ta.crossunder(close, lo), 'Bajo banda', 'Cierre bajo la banda inferior')
print('velas:', len(close), 'último:', round(float(close[-1]), 2), 'ñandú ✓')
"""


def test_indicator_end_to_end(candles):
    res = run_script(INDICATOR, candles, symbol="SIM:NEXUS", tf="1h")
    assert set(res) == RESULT_KEYS
    assert res["ok"] is True and res["errors"] == []
    assert res["meta"] == {"title": "Bandas", "overlay": True, "kind": "indicator"}
    assert len(res["candles"]) == len(res["time"]) == 500 and res["time"][0] == candles[0]["time"]
    assert [p["title"] for p in res["plots"]] == ["Media", "Superior", "Inferior", "RSI"]
    assert all(len(p["data"]) == 500 for p in res["plots"])
    assert res["plots"][0]["data"][:19] == [None] * 19 and isinstance(res["plots"][0]["data"][19], float)
    assert res["plots"][3]["pane"] == 1 and res["plots"][3]["overlay"] is False
    assert res["hlines"][0]["price"] == 70.0 and res["strategy"] is None
    assert "velas: 500" in res["stdout"] and "ñandú ✓" in res["stdout"]
    assert res["duration_ms"] > 0
    json.dumps(res, allow_nan=False)


def test_inputs_use_the_names_reported_by_the_compiler(candles):
    compiled = compile_script(INDICATOR)
    names = [i["name"] for i in compiled["inputs"]]
    assert names == ["length", "mult"]
    res = run_script(INDICATOR, candles, {"length": 50, "mult": 1.0})
    assert res["ok"] and res["plots"][0]["data"][:49] == [None] * 49 and res["plots"][0]["data"][49] is not None
    inline = "indicator('i')\nplot(ta.sma(close, input.int(5, 'Periodo')))\n"
    assert compile_script(inline)["inputs"][0]["name"] == "input_1"
    res = run_script(inline, candles, {"input_1": 40})
    assert res["plots"][0]["data"][:39] == [None] * 39 and res["plots"][0]["data"][39] is not None


STRATEGY = """strategy('Cruce EMA', overlay=True, initial_capital=5000, risk_pct=1, fee_pct=0.1)
fast = input.int(9, 'Rápida')
slow = input.int(21, 'Lenta')
f, s = ta.ema(close, fast), ta.ema(close, slow)
atr = ta.atr(14)
plot(f, 'Rápida')
plot(s, 'Lenta')
strategy.entry('Largo', strategy.long, when=ta.crossover(f, s))
strategy.entry('Corto', strategy.short, when=ta.crossunder(f, s))
strategy.exit('Protección L', 'Largo', stop=close - 2 * atr, limit=close + 3 * atr)
strategy.exit('Protección C', 'Corto', stop=close + 2 * atr, trail_atr=2.5)
"""


def test_strategy_end_to_end(candles):
    res = run_script(STRATEGY, candles, symbol="SIM:NEXUS", tf="1h")
    assert res["ok"], res["errors"]
    st = res["strategy"]
    assert res["meta"]["kind"] == "strategy"
    assert st["symbol"] == "SIM:NEXUS" and st["tf"] == "1h" and st["bars"] == 500
    assert st["metrics"]["initial_capital"] == 5000 and st["metrics"]["trades"] > 0
    assert len(st["equity"]) == 500 and st["trades"] and st["markers"]
    kinds = {m["kind"] for m in st["markers"]}
    assert {"BUY", "SELL"} <= kinds
    assert {t["exit_reason"] for t in st["trades"]} <= {"SL", "TRAIL", "TP1", "REVERSE", "SIGNAL", "OPEN"}
    assert res["markers"] == []  # strategy markers live in strategy.markers only


def test_runtime_errors_map_to_user_lines(candles):
    res = run_script("indicator('e')\nx = 1\ny = x / 0\n", candles)
    err = res["errors"][0]
    assert res["ok"] is False and err["kind"] == "runtime" and err["line"] == 3 and err["col"] == 5
    assert "División por cero" in err["message"]
    res = run_script("indicator('e')\ndef f(v):\n    return v + desconocido\nplot(f(close))\n", candles)
    err = res["errors"][0]
    assert err["line"] == 3 and "'desconocido' no está definido" in err["message"]
    assert res["candles"] and res["meta"]["title"] == "e"


def test_infinite_loop_times_out(candles):
    res = run_script("indicator('loop')\nwhile True:\n    pass\n", candles, timeout=2)
    assert res["ok"] is False and res["errors"][0]["kind"] == "timeout"
    assert 1900 <= res["duration_ms"] < 15_000


def test_no_candles_and_invalid_code_do_not_launch(monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("worker launched")

    monkeypatch.setattr(sandbox, "_launch", boom)
    res = run_script("indicator('x')\nplot(close)\n", [])
    assert res["ok"] is False and "No hay velas" in res["errors"][0]["message"]
    res = run_script("indicator('x'\n", [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    assert res["errors"][0]["kind"] == "syntax" and len(res["candles"]) == 1


def test_clean_candles_sorts_dedupes_and_drops_bad_rows():
    rows = [
        {"time": 30, "open": 3, "high": 3, "low": 3, "close": 3, "volume": None},
        {"time": 10, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 5},
        {"time": 20, "open": float("nan"), "high": 2, "low": 2, "close": 2, "volume": 5},
        (10, 9, 9, 9, 9, 9),
        "basura",
    ]
    out = clean_candles(rows)
    assert [r["time"] for r in out] == [10, 30]
    assert out[0]["close"] == 9 and out[1]["volume"] == 0.0


def test_parallel_runs(candles):
    code = "indicator('p')\nplot(ta.ema(close, input.int(10, 'n')))\n"
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda k: run_script(code, candles, {"input_1": 5 + k}), range(3)))
    assert all(r["ok"] for r in results)
    assert [r["plots"][0]["data"].index(next(v for v in r["plots"][0]["data"] if v is not None)) for r in results] == [4, 5, 6]
