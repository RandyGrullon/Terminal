"""Strategy lab of the copilot (app.ai.lab + the tools `referencia_pychart` and `probar_estrategia`): the compact PyChart
reference the model reads, the honest summary of a backtest (development / check periods, each against random entries
with the same direction, duration and costs; nothing concluded below 30 trades nor when the interval includes 0), the
real PyChart sandbox running the model's script, and a whole conversation against a fake Ollama (no model, no
network). Running a script is a simulation on past candles: nothing is traded nor changed."""
from __future__ import annotations

import asyncio
import json
import math
import re
from types import SimpleNamespace

import numpy as np

from app.ai import lab
from app.ai import tools as tools_mod
from app.ai.service import MAX_ROUNDS, SYSTEM_PROMPT, AIService
from app.scripting.reference import REFERENCE
from app.scripting.service import ScriptService
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_imp_ai_copilot import FakeOllama, _call
from tests.test_scripting_service_run import FakeHub

H = 3600
COST = 0.2  # 0.1 % commission per side, no slippage (the default of strategy(...))


def _candles(n: int = 3000, seed: int = 3) -> list[dict]:
    """An hourly market that swings every 48 bars: a rule that knows the swing beats random entries by far."""
    rng = np.random.default_rng(seed)
    t0 = 1_740_000_000 - 1_740_000_000 % H
    close = 100.0 + 6.0 * np.sin(2 * math.pi * np.arange(n) / 48.0) + rng.normal(0, 0.05, n)
    out, prev = [], float(close[0])
    for i in range(n):
        c = float(close[i])
        out.append({"time": t0 + i * H, "open": prev, "high": max(prev, c) + 0.05, "low": min(prev, c) - 0.05,
                    "close": c, "volume": 1.0})
        prev = c
    return out


def _trade(candles: list[dict], i: int, bars: int, side: int) -> dict:
    move = candles[i + bars - 1]["close"] / candles[i]["open"] - 1.0
    return {"side": "long" if side > 0 else "short", "entry_time": candles[i]["time"],
            "exit_time": candles[i + bars - 1]["time"], "exit_reason": "SIGNAL", "bars": bars,
            "pnl_pct": side * move * 100.0 - COST}


def _result(candles: list[dict], trades: list[dict], **over) -> dict:
    return {"ok": True, "errors": [], "stdout": "", "meta": {"title": "Oráculo", "kind": "strategy"},
            "candles": candles,
            "strategy": {"symbol": "SIM:NEXUS", "params": {"fee_pct": 0.1, "slippage_pct": 0.0},
                         "metrics": {"total_return_pct": 12.345, "buy_hold_return_pct": 1.0, "max_dd_pct": 3.0,
                                     "profit_factor": 2.5}, "trades": trades}, **over}


def _oracle(candles: list[dict], start: int, stop: int, every: int = 40, bars: int = 12, flip: bool = False) -> list:
    """Trades at every `every` bars that know where the next `bars` bars go (flip: the wrong way, always)."""
    out = []
    for i in range(start, stop - bars, every):
        move = candles[i + bars - 1]["close"] - candles[i]["open"]
        side = 1 if move > 0 else -1
        out.append(_trade(candles, i, bars, -side if flip else side))
    return out


def test_reference_for_the_model_is_compact_and_complete():
    text = lab.reference_text()
    assert len(text) <= tools_mod.CHAR_LIMITS["referencia_pychart"], len(text)
    signatures = [i["signature"] for sec in REFERENCE["sections"] for i in sec["items"]]
    assert all(sig in text for sig in signatures), [s for s in signatures if s not in text]
    # the docs a strategy needs (orders at the next open, Pine pitfalls), and a whole example without comments
    assert "Las órdenes se ejecutan en la APERTURA de la vela siguiente" in text and "close[1] NO es el cierre" in text
    example = lab.example_script()
    assert 'strategy("Cruce de EMAs"' in example and "strategy.entry(" in text.split("## Ejemplo completo")[1]
    assert not any(line.lstrip().startswith("#") for line in example.splitlines())
    assert lab.script_title('strategy("Mi cruce", overlay=True)') == "Mi cruce" and lab.script_title("x = 1") == "Estrategia"

    async def scenario():
        res = await tools_mod.run_tool(SimpleNamespace(), "referencia_pychart", {}, {})
        assert res["referencia"] == text  # never trimmed to 400 characters
        content = tools_mod.tool_content("referencia_pychart", res, 6000)
        assert content == text and len(content) > 6000  # plain text (readable code), not JSON, not cut at 6000

    asyncio.run(scenario())


def test_each_period_is_compared_with_random_entries_and_small_samples_conclude_nothing():
    candles = _candles()
    split = int(len(candles) * lab.DEV_SHARE)
    good = _oracle(candles, 0, split) + _oracle(candles, split, len(candles))
    open_at_end = {**_trade(candles, len(candles) - 5, 4, 1), "exit_time": None, "exit_reason": "OPEN"}
    out = lab.summarize_run(_result(candles, good + [open_at_end], stdout="hola"), symbol="SIM:NEXUS", tf="1h", seed=1)
    assert out["ok"] and out["estrategia"] == "Oráculo" and out["velas"] == 3000 and out["consola"] == "hola"
    assert out["costes"] == {"comision_pct_por_lado": 0.1, "deslizamiento_pct": 0.0}
    total = out["total"]
    assert total["operaciones"] == len(good) and total["abierta_al_final"] is True  # the open one is left out
    assert total["rentabilidad_pct"] == 12.35 and total["factor_beneficio"] == 2.5
    dev, chk = out["periodos"]
    assert dev["periodo"].startswith("desarrollo") and chk["periodo"].startswith("comprobación")
    assert (dev["velas"], chk["velas"]) == (2100, 900)
    assert dev["desde"] == "2025-02-19 21:00 UTC" and chk["desde"] == lab._iso(candles[split]["time"])
    assert dev["operaciones"] >= 30 > chk["operaciones"] > 0
    assert dev["ganadoras"] == f"{dev['operaciones']} de {dev['operaciones']}"
    assert re.fullmatch(r"\d+ de \d+", chk["ganadoras"])
    # random entries with the same direction and duration lose about the costs on this swinging market
    assert dev["azar_medio_pct"] < 0 < dev["diferencia_con_azar_pct"] and 0 < dev["ganadoras_al_azar_pct"] < 100
    assert dev["ic95_diferencia_pct"][0] > 0 and dev["veredicto"] == "Por encima de entrar al azar en este periodo"
    assert chk["veredicto"] == "Muestra pequeña: sin conclusiones" and chk["veredicto_clave"] == "small"
    assert "Referencia: entradas al azar" in out["referencia"] and "no es una garantía" in out["nota"]

    bad = _oracle(candles, 0, split, flip=True)
    dev_bad = lab.summarize_run(_result(candles, bad), symbol="SIM:NEXUS", tf="1h")["periodos"][0]
    assert dev_bad["ganadoras"] == f"0 de {len(bad)}" and dev_bad["ic95_diferencia_pct"][1] < 0
    assert dev_bad["veredicto_clave"] == "below"

    # coin flips of the direction have no edge: a difference with random entries is claimed only rarely (a 95 %
    # interval: about 1 in 20 by luck, which is why every variant tested is another chance and the note says so)
    claims = 0
    for s in range(20):
        rng = np.random.default_rng(s)
        coin = [_trade(candles, i, 12, int(rng.choice([-1, 1]))) for i in range(0, split - 12, 15)]
        dev_coin = lab.summarize_run(_result(candles, coin), symbol="SIM:NEXUS", tf="1h", seed=s)["periodos"][0]
        claims += dev_coin["veredicto_clave"] in ("above", "below")
        assert dev_coin["veredicto_clave"] in ("above", "below", "unclear")
    assert claims <= 3, claims
    assert "otra oportunidad de salir «por encima» por suerte" in out["nota"]

    none = lab.summarize_run(_result(candles, []), symbol="SIM:NEXUS", tf="1h")
    assert [p["veredicto_clave"] for p in none["periodos"]] == ["none", "none"]
    assert none["total"]["ganadoras"] == "0 de 0"
    # deterministic: the same seed gives the same summary
    again = lab.summarize_run(_result(candles, good), symbol="SIM:NEXUS", tf="1h", seed=1)
    assert again == lab.summarize_run(_result(candles, good), symbol="SIM:NEXUS", tf="1h", seed=1)


def test_failed_scripts_and_indicators_say_what_to_fix():
    failed = {"ok": False, "errors": [{"line": 3, "col": 5, "kind": "runtime", "message": "NameError: 'x' no existe"}]}
    out = lab.summarize_run(failed, symbol="SIM:NEXUS", tf="1h")
    assert out == {"ok": False, "errores": [{"linea": 3, "columna": 5, "tipo": "runtime",
                                             "mensaje": "NameError: 'x' no existe"}], "consejo": lab.FIX_HINT}
    indicator = {"ok": True, "meta": {"kind": "indicator"}, "strategy": None, "candles": _candles(10)}
    assert "declara strategy(...)" in lab.summarize_run(indicator, symbol="S", tf="1h")["errores"][0]["mensaje"]
    assert lab.summarize_run(None, symbol="S", tf="1h")["errores"] == [{"mensaje": "El script falló sin detalle."}]


def test_probar_estrategia_runs_the_script_in_the_pychart_sandbox(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(generate_candleset(n=3000, seed=11, end_time=FIXED_END, symbol="SIM:NEXUS"))
            cfg: dict = {}
            res = await tools_mod.run_tool(rt, "probar_estrategia", {"codigo": "x", "simbolo": "SIM:NEXUS"}, cfg)
            assert res == {"error": "El módulo PyChart no está cargado."}
            rt.scripts = ScriptService(rt)
            await rt.scripts.start()
            code = lab.example_script()
            res = await tools_mod.run_tool(rt, "probar_estrategia", {"codigo": code, "simbolo": "SIM:NEXUS",
                                                                     "temporalidad": "1h", "velas": 3000}, cfg)
            assert res["ok"] is True and res["estrategia"] == "Cruce de EMAs" and res["velas"] == 3000, res
            assert res["total"]["operaciones"] > 0 and len(res["periodos"]) == 2
            assert all(p["veredicto"] in lab.VERDICT_LABEL.values() for p in res["periodos"])
            assert res["consola"].startswith("Señales de compra:")
            events = [e["message"] for e in rt.bus.recent(50, source="scripts")]
            assert any("Copiloto: Cruce de EMAs" in m for m in events), events
            # an error comes back with its line so the model can fix it
            broken = code.replace("ta.ema(close, rapida)", "ta.emaa(close, rapida)")
            line = broken.splitlines().index("ema_r = ta.emaa(close, rapida)") + 1
            res = await tools_mod.run_tool(rt, "probar_estrategia", {"codigo": broken, "simbolo": "SIM:NEXUS"}, cfg)
            assert res["ok"] is False and res["errores"][0]["linea"] == line and "emaa" in res["errores"][0]["mensaje"]
            # what the sandbox forbids stays forbidden
            res = await tools_mod.run_tool(rt, "probar_estrategia",
                                           {"codigo": "import os\nstrategy('x')\n", "simbolo": "SIM:NEXUS"}, cfg)
            assert res["ok"] is False and "os" in res["errores"][0]["mensaje"]
            assert (await tools_mod.run_tool(rt, "probar_estrategia", {"codigo": code}, cfg)) == {
                "error": "Falta el símbolo."}
            assert "demasiado largo" in (await tools_mod.run_tool(
                rt, "probar_estrategia", {"codigo": "x" * 20_001, "simbolo": "SIM:NEXUS"}, cfg))["error"]
            await rt.scripts.stop()

    asyncio.run(scenario())


class FakeScripts:
    """rt.scripts for the conversation test: records the runs; a script containing BROKEN fails at line 2."""

    def __init__(self, candles: list[dict], trades: list[dict]):
        self.candles, self.trades, self.calls = candles, trades, []

    async def run(self, code, symbol, tf="1h", limit=1000, inputs=None, *, name=None):
        self.calls.append({"symbol": symbol, "tf": tf, "limit": limit, "name": name})
        if "BROKEN" in code:
            return {"ok": False, "errors": [{"line": 2, "col": 1, "kind": "runtime", "message": "NameError: BROKEN"}]}
        return _result(self.candles, self.trades)


def test_lab_conversation_writes_fixes_and_tests_a_strategy(runtime_factory, monkeypatch):
    good = 'strategy("Oráculo")\nstrategy.entry("L", "long", when=close > open)\n'

    async def scenario():
        async with runtime_factory() as rt:
            candles = _candles()
            rt.scripts = FakeScripts(candles, _oracle(candles, 0, len(candles)))
            answer = f"```python\n{good}```\nDesarrollo: 52 de 52, por encima de entrar al azar."
            fake = FakeOllama([_call("referencia_pychart"),
                               _call("probar_estrategia", {"codigo": "x = 1\nBROKEN", "simbolo": "SIM:NEXUS"}),
                               _call("probar_estrategia", {"codigo": good, "simbolo": "SIM:NEXUS", "velas": 999999,
                                                           "temporalidad": "3h"}),
                               {"role": "assistant", "content": answer}])
            fake.install(monkeypatch)
            await rt.settings.update({"ai.enabled": True})
            res = await AIService(rt).chat([{"role": "user", "content": "Crea y prueba una estrategia en BTC 1h"}])
            assert res["reply"] == answer
            assert res["tools"] == [{"name": "referencia_pychart", "ok": True}, {"name": "probar_estrategia", "ok": True},
                                    {"name": "probar_estrategia", "ok": True}]
            first = fake.chats[0]
            system = first["messages"][0]["content"]
            assert "referencia_pychart" in system and "probar_estrategia" in system
            assert "tu conocimiento del pasado no vale como prueba" in system and "no abre operaciones" in system
            assert {"referencia_pychart", "probar_estrategia"} <= {t["function"]["name"] for t in first["tools"]}
            # Ollama gets a context big enough for the reference + a test (its 4096 default would cut it)
            assert all(c["options"]["num_ctx"] == 16384 for c in fake.chats)
            ref_msg = fake.chats[1]["messages"][-1]
            assert ref_msg["role"] == "tool" and ref_msg["content"] == lab.reference_text()
            failed = json.loads(fake.chats[2]["messages"][-1]["content"])
            assert failed["ok"] is False and failed["errores"][0]["linea"] == 2
            tested = json.loads(fake.chats[3]["messages"][-1]["content"])
            assert tested["ok"] is True and tested["periodos"][0]["veredicto_clave"] == "above"
            # bounded run: at most 5000 candles, a known timeframe, and the run is named after the copilot
            assert rt.scripts.calls[1] == {"symbol": "SIM:NEXUS", "tf": "1h", "limit": 5000,
                                           "name": "Copiloto: Oráculo"}
            await rt.settings.update({"ai.context_length": 100})
            fake.turns = [{"role": "assistant", "content": "ok"}]
            await AIService(rt).chat([{"role": "user", "content": "hola"}])
            assert fake.chats[-1]["options"]["num_ctx"] == 2048  # clamped
            assert MAX_ROUNDS >= 4 and "Laboratorio de estrategias" in SYSTEM_PROMPT

    asyncio.run(scenario())
