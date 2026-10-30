"""PyChart core: compiler (syntax, meta, inputs) and the Pine-like runtime executed in-process."""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.engine import ta as T
from app.scripting.runtime import Bars, ScriptContext, ScriptRuntimeError, build_namespace, collect_result, make_print
from app.scripting.validator import build_code, compile_script
from app.sim.synthetic import generate_candleset

END = 1_760_000_400


@pytest.fixture(scope="module")
def cs():
    return generate_candleset(n=600, seed=21, tf="1h", end_time=END)


def bars_of(cs, upto: int | None = None) -> Bars:
    k = len(cs) if upto is None else upto
    return Bars(cs.time[:k], cs.open[:k], cs.high[:k], cs.low[:k], cs.close[:k], cs.volume[:k])


def run_inprocess(code: str, bars: Bars, inputs: dict | None = None) -> dict:
    """Same steps as the sandbox worker, without the sandbox (fast unit tests)."""
    code_obj, errors = build_code(code)
    assert not errors, errors
    ctx = ScriptContext(bars, inputs or {}, symbol="SIM:NEXUS", tf="1h")
    ns = build_namespace(ctx)
    ns.update(np=np, math=math, print=make_print(ctx.stdout))
    exec(code_obj, ns)
    return collect_result(ctx)


# ----------------------------------------------------------------------------- compiler


def test_syntax_error_has_line_and_col():
    res = compile_script("indicator('x')\ny = (1,\nplot(close)\n")
    assert res["ok"] is False
    err = res["errors"][0]
    assert err["kind"] == "syntax"
    assert err["line"] == 2 and err["col"] == 5
    assert err["message"].startswith("Error de sintaxis")


def test_indentation_and_compiler_stage_errors():
    res = compile_script("if True:\nx = 1\n")
    assert res["errors"][0]["kind"] == "syntax" and res["errors"][0]["line"] == 2
    res = compile_script("indicator('x')\nreturn 5\n")
    assert res["ok"] is False and res["errors"][0]["line"] == 2
    assert "fuera de una función" in res["errors"][0]["message"]


def test_empty_and_oversized_code():
    assert compile_script("   \n")["errors"][0]["kind"] == "syntax"
    assert compile_script("x = 1\n" * 30_000)["ok"] is False


def test_meta_and_inputs_extraction():
    code = (
        "strategy('Cruce EMA', overlay=False)\n"
        "length = input.int(14, 'Longitud', minval=2, maxval=200, step=1)\n"
        "mult = input.float(2.5, 'Multiplicador')\n"
        "src = input.source(hl2, 'Fuente')\n"
        "mode = input.select('EMA', 'Tipo', ['EMA', 'SMA'])\n"
        "show = input.bool(True, 'Mostrar')\n"
        "label = input.string('hola', 'Texto')\n"
        "auto = input(7, 'Auto')\n"
        "plot(ta.sma(close, input.int(50, 'Lenta')))\n"
    )
    res = compile_script(code)
    assert res["ok"], res["errors"]
    assert res["meta"] == {"title": "Cruce EMA", "overlay": False, "kind": "strategy"}
    by_name = {i["name"]: i for i in res["inputs"]}
    assert list(by_name) == ["length", "mult", "src", "mode", "show", "label", "auto", "input_8"]
    assert by_name["length"] == {"name": "length", "title": "Longitud", "type": "int", "default": 14, "min": 2, "max": 200, "step": 1}
    assert by_name["mult"]["type"] == "float" and by_name["mult"]["default"] == 2.5
    assert by_name["src"]["type"] == "source" and by_name["src"]["default"] == "hl2" and "close" in by_name["src"]["options"]
    assert by_name["mode"]["options"] == ["EMA", "SMA"] and by_name["mode"]["default"] == "EMA"
    assert by_name["show"] == {"name": "show", "title": "Mostrar", "type": "bool", "default": True}
    assert by_name["auto"]["type"] == "int" and by_name["auto"]["default"] == 7
    assert by_name["input_8"]["title"] == "Lenta"


def test_missing_declaration_warns():
    res = compile_script("plot(close)\n")
    assert res["ok"] and res["meta"]["kind"] == "indicator"
    assert any("indicator" in w for w in res["warnings"])


# ----------------------------------------------------------------------------- runtime


def test_plot_alignment_and_nan_become_null(cs):
    res = run_inprocess(
        "indicator('SMA', overlay=True)\n"
        "plot(ta.sma(close, 20), 'SMA 20', color=color.orange, linewidth=2)\n"
        "plot(np.where(close > 0, np.nan, 1.0), 'Vacío')\n"
        "plot(ta.rsi(close, 14), 'RSI', pane=1, style=plot.style_histogram)\n",
        bars_of(cs),
    )
    assert len(res["time"]) == len(cs)
    sma, empty, rsi = res["plots"]
    assert len(sma["data"]) == len(cs) and sma["data"][:19] == [None] * 19
    assert all(isinstance(v, float) for v in sma["data"][19:])
    np.testing.assert_allclose(sma["data"][19:], T.sma(cs.close, 20)[19:])
    assert set(empty["data"]) == {None}
    assert sma["overlay"] is True and sma["pane"] == 0 and sma["color"] == "#FF9800" and sma["linewidth"] == 2
    assert rsi["overlay"] is False and rsi["pane"] == 1 and rsi["style"] == "histogram"
    assert [p["id"] for p in res["plots"]] == ["plot_0", "plot_1", "plot_2"]


def test_default_pane_follows_declaration(cs):
    res = run_inprocess("indicator('Osc', overlay=False)\nplot(ta.rsi(close, 14))\nplot(close, overlay=True)\n", bars_of(cs))
    assert (res["plots"][0]["pane"], res["plots"][0]["overlay"]) == (1, False)
    assert (res["plots"][1]["pane"], res["plots"][1]["overlay"]) == (0, True)
    assert res["meta"] == {"title": "Osc", "overlay": False, "kind": "indicator"}


def test_input_overrides_clamping_and_sources(cs):
    code = (
        "indicator('In')\n"
        "length = input.int(10, 'L', minval=2, maxval=50)\n"
        "src = input.source(close, 'Fuente')\n"
        "kind = input.select('EMA', 'Tipo', options=['EMA', 'SMA'])\n"
        "plot(ta.sma(src, length) if kind == 'SMA' else ta.ema(src, length), 'MA')\n"
        "plot(src, 'src')\n"
    )
    b = bars_of(cs)
    base = run_inprocess(code, b)
    assert base["plots"][0]["data"][:9] == [None] * 9 and base["plots"][0]["data"][9] is not None
    over = run_inprocess(code, b, {"length": 30, "src": "high", "kind": "SMA"})
    assert over["plots"][0]["data"][:29] == [None] * 29
    np.testing.assert_allclose(over["plots"][1]["data"], cs.high)
    np.testing.assert_allclose(over["plots"][0]["data"][29:], T.sma(cs.high, 30)[29:])
    clamped = run_inprocess(code, b, {"length": 500})
    assert clamped["plots"][0]["data"][:49] == [None] * 49 and clamped["plots"][0]["data"][49] is not None
    bad = run_inprocess(code, b, {"length": "abc", "kind": "WMA"})
    assert bad["plots"][0]["data"][:9] == [None] * 9
    assert "valor inválido" in bad["stdout"]


def test_ta_matches_engine_and_pine_conventions(cs):
    res = run_inprocess(
        "indicator('ta')\n"
        "plot(ta.rsi(close, 14))\nplot(ta.atr(14))\nplot(ta.ema(close, 9))\n"
        "m, u, l = ta.bb(close, 20, 2)\nplot(m)\nplot(u)\n"
        "line, d = ta.supertrend(3, 10)\nplot(d)\n"
        "plot(ta.highestbars(10))\nplot(ta.tsi(close, 13, 25))\n",
        bars_of(cs),
    )
    data = [np.array([np.nan if v is None else v for v in p["data"]]) for p in res["plots"]]
    np.testing.assert_allclose(data[0], T.rsi(cs.close, 14), equal_nan=True)
    np.testing.assert_allclose(data[1], T.atr(cs.high, cs.low, cs.close, 14), equal_nan=True)
    np.testing.assert_allclose(data[2], T.ema(cs.close, 9), equal_nan=True)
    np.testing.assert_allclose(data[3], T.sma(cs.close, 20), equal_nan=True)
    assert np.nanmax(data[4] - data[3]) > 0
    _, engine_dir = T.supertrend(cs.high, cs.low, cs.close, 10, 3.0)
    np.testing.assert_allclose(data[5], -engine_dir, equal_nan=True)  # Pine: -1 = alcista
    assert np.nanmax(data[6]) <= 0 and np.nanmin(data[6]) >= -9
    assert np.nanmax(np.abs(data[7])) <= 1.0


CAUSAL_EXPRS = [
    "ta.sma(close, 10)", "ta.ema(close, 12)", "ta.wma(close, 9)", "ta.hma(close, 16)", "ta.rma(close, 14)",
    "ta.vwma(close, 20)", "ta.alma(close, 9)", "ta.dema(close, 10)", "ta.tema(close, 10)", "ta.linreg(close, 20, 0)",
    "ta.rsi(close, 14)", "ta.macd(close)[2]", "ta.stoch(close, high, low, 14)", "ta.cci(hlc3, 20)",
    "ta.mfi(hlc3, 14)", "ta.roc(close, 9)", "ta.mom(close, 9)", "ta.cmo(close, 14)", "ta.tsi(close, 13, 25)",
    "ta.wpr(14)", "ta.tr()", "ta.atr(14)", "ta.stdev(close, 20)", "ta.variance(close, 20)", "ta.dev(close, 20)",
    "ta.bb(close, 20, 2)[1]", "ta.bbw(close, 20, 2)", "ta.kc(close, 20, 1.5)[2]", "ta.donchian(20)[0]",
    "ta.supertrend(3, 10)[0]", "ta.sar()", "ta.dmi(14, 14)[0]", "ta.adx(14)", "ta.obv()", "ta.vwap()",
    "ta.vwap(close, 'W')", "ta.cum(volume)", "ta.highest(20)", "ta.lowest(low, 20)", "ta.highestbars(high, 20)",
    "ta.lowestbars(20)", "ta.pivothigh(5, 5)", "ta.pivotlow(low, 3, 4)", "ta.change(close, 3)",
    "ta.crossover(ta.ema(close, 9), ta.ema(close, 21))", "ta.crossunder(close, ta.sma(close, 50))",
    "ta.cross(close, ta.sma(close, 20))", "ta.rising(close, 3)", "ta.falling(close, 3)",
    "ta.barssince(close > open)", "ta.valuewhen(ta.crossover(close, ta.sma(close, 20)), close, 1)",
    "ta.correlation(close, volume, 20)", "ta.percentrank(close, 20)", "ta.median(close, 15)", "fixnan(ta.pivothigh(3, 3))",
]


@pytest.mark.parametrize("expr", CAUSAL_EXPRS)
def test_ta_functions_are_causal(cs, expr):
    """Values computed with all the bars equal the values computed with only the past: no lookahead."""
    code = f"indicator('c')\nplot({expr})\n"
    full = run_inprocess(code, bars_of(cs))["plots"][0]["data"]
    cut = 420
    part = run_inprocess(code, bars_of(cs, cut))["plots"][0]["data"]
    a = np.array([np.nan if v is None else v for v in full[:cut]], dtype=float)
    b = np.array([np.nan if v is None else v for v in part], dtype=float)
    np.testing.assert_allclose(a, b, rtol=1e-9, atol=1e-9, equal_nan=True)
    assert np.isfinite(a).sum() > 0


def test_markers_hlines_bgcolors_alerts(cs):
    res = run_inprocess(
        "indicator('Señales')\n"
        "up = ta.crossover(ta.ema(close, 9), ta.ema(close, 21))\n"
        "plotshape(up, 'Compra', location.belowbar, color.green, shape.triangleup, 'C')\n"
        "plotshape(ta.crossunder(ta.ema(close, 9), ta.ema(close, 21)), 'Venta', shape.arrowdown, location.abovebar)\n"
        "hline(70, 'Sobrecompra', color.red, hline.style_dotted)\nhline(nan, 'oculta')\n"
        "bgcolor(close > ta.sma(close, 50), color.new(color.green, 80))\n"
        "alertcondition(up, 'Cruce', 'EMA 9 cruza la EMA 21')\n",
        bars_of(cs),
    )
    buys = [m for m in res["markers"] if m["tooltip"] == "Compra"]
    sells = [m for m in res["markers"] if m["tooltip"] == "Venta"]
    assert buys and sells
    assert buys[0]["position"] == "belowBar" and buys[0]["shape"] == "arrowUp" and buys[0]["text"] == "C"
    assert sells[0]["position"] == "aboveBar" and sells[0]["shape"] == "arrowDown"  # Pine positional order
    assert all(m["kind"] == "SCRIPT" for m in res["markers"])
    times = [m["time"] for m in res["markers"]]
    assert times == sorted(times)
    assert res["hlines"] == [{"price": 70.0, "title": "Sobrecompra", "color": "#FF5252", "style": "dotted"}]
    assert res["bgcolors"] and res["bgcolors"][0]["color"].startswith("rgba(76, 175, 80, 0.2")
    assert len(res["alerts"]) == len(buys) and res["alerts"][0]["message"] == "EMA 9 cruza la EMA 21"


def test_na_helpers_and_colors(cs):
    res = run_inprocess(
        "indicator('na')\n"
        "x = ta.sma(close, 5)\n"
        "plot(nz(x))\nplot(na(x))\nplot(fixnan(ta.pivothigh(2, 2)))\n"
        "print(nz(nan, 3), na(None), na(1.0), color.new('#FF0000', 50), color.rgb(0, 128, 255), syminfo.ticker, timeframe.period)\n",
        bars_of(cs),
    )
    assert res["plots"][0]["data"][:4] == [0.0] * 4
    assert res["plots"][1]["data"][:5] == [1.0, 1.0, 1.0, 1.0, 0.0]
    filled = res["plots"][2]["data"]
    first = next(i for i, v in enumerate(filled) if v is not None)
    assert all(v is not None for v in filled[first:])
    assert res["stdout"].strip() == "3 True False rgba(255, 0, 0, 0.5) #0080FF NEXUS 1h"


def test_stdout_is_capped(cs):
    res = run_inprocess("indicator('p')\nfor i in range(5000):\n    print('x' * 10)\n", bars_of(cs))
    assert len(res["stdout"]) < 20_100 and "truncada" in res["stdout"]


@pytest.mark.parametrize(
    "code, fragment",
    [
        ("plot(close[:-1])", "599 valores"),
        ("ta.sma(close, 0)", "debe ser ≥ 1"),
        ("ta.sma(close, 2.5)", "entero"),
        ("plot(close, style='zigzag')", "estilo desconocido"),
        ("plot(close, color='azulito')", "color inválido"),
        ("strategy.entry('L', strategy.long, when=close > open)", "declara strategy"),
        ("input.select('X', 'Tipo', ['A', 'B'])", "options"),
        ("strategy.position_size", "no está disponible"),
    ],
)
def test_api_misuse_raises_spanish_errors(cs, code, fragment):
    with pytest.raises((ScriptRuntimeError, AttributeError)) as info:
        run_inprocess("indicator('e')\n" + code + "\n", bars_of(cs, 600))
    assert fragment in str(info.value)
