"""BE-SCRIPT: ScriptService compile/run plumbing — market data, sandbox call, result normalization, events and
graceful degradation when the PyChart compiler is missing. The sandbox is faked here (its own behavior is tested
in test_scripting_examples.py), so these tests always run."""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.data.base import CandleSet
from app.scripting import service as service_mod
from app.scripting.service import MSG_UNAVAILABLE, ScriptService, ScriptServiceError
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

CODE = 'indicator("Prueba", overlay=True)\nplot(close, "Cierre")\n'


class FakeHub:
    def __init__(self, cs: CandleSet | None):
        self.cs = cs
        self.calls: list[tuple] = []

    async def get_candles(self, symbol, tf, limit=500):
        self.calls.append((symbol, tf, limit))
        if self.cs is None:
            return CandleSet.empty(symbol, tf, note="sin datos de prueba")
        return self.cs.tail(int(limit))

    async def resolve(self, text):
        return {"btc": "BINANCE:BTCUSDT"}.get(str(text).lower())


class FakeSandbox:
    """Records the call and answers like run_script (alerts on the last bar, stdout, strategy metrics)."""

    def __init__(self, ok: bool = True, raises: Exception | None = None):
        self.ok, self.raises, self.calls = ok, raises, []

    def __call__(self, code, candles, inputs=None, *, symbol="", tf="", timeout=10.0):
        self.calls.append({"code": code, "n": len(candles), "inputs": inputs, "symbol": symbol, "tf": tf, "timeout": timeout})
        if self.raises:
            raise self.raises
        last = candles[-1]["time"]
        if not self.ok:
            return {"ok": False, "errors": [{"line": 2, "col": 1, "message": "NameError: 'x' no existe", "kind": "runtime"}],
                    "stdout": "", "meta": {"title": "Prueba", "overlay": True, "kind": "indicator"}}
        return {
            "ok": True, "errors": [], "stdout": "hola\nmundo\n", "duration_ms": 12.4,
            "meta": {"title": "Prueba", "overlay": True, "kind": "strategy"},
            "time": [c["time"] for c in candles],
            "plots": [{"id": "p0", "title": "Cierre", "color": "#fff", "linewidth": 1, "style": "line", "overlay": True,
                       "pane": 0, "data": [c["close"] for c in candles]}],
            "markers": [], "hlines": [], "bgcolors": [],
            "alerts": [{"time": candles[0]["time"], "title": "Vieja", "message": "no"},
                       {"time": last, "title": "Compra", "message": "Cruce alcista"}],
            "strategy": {"metrics": {"trades": 3, "total_return_pct": 4.5, "win_rate": 0.6667}},
        }


def run(coro):
    return asyncio.run(coro)


async def _setup(rt, monkeypatch, sandbox=None, cs="default") -> ScriptService:
    if cs == "default":
        cs = generate_candleset(n=600, seed=9, end_time=FIXED_END, symbol="SIM:NEXUS")
    rt.hub = FakeHub(cs)
    if sandbox is not None:
        monkeypatch.setattr(service_mod, "_load_sandbox", lambda: sandbox)
    svc = ScriptService(rt)
    await svc.start()
    return svc


def _events(rt, level=None):
    return rt.bus.recent(500, level=level, source="scripts")


def test_run_fetches_candles_calls_sandbox_and_emits_events(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeSandbox()
            svc = await _setup(rt, monkeypatch, fake)
            res = await svc.run(CODE, "SIM:NEXUS", "60", 300, {"longitud": 5})
            assert rt.hub.calls == [("SIM:NEXUS", "1h", 300)]
            call = fake.calls[0]
            assert call["n"] == 300 and call["symbol"] == "SIM:NEXUS" and call["tf"] == "1h"
            assert call["inputs"] == {"longitud": 5} and call["timeout"] == service_mod.RUN_TIMEOUT_S
            assert res["ok"] is True and res["duration_ms"] == 12 and len(res["candles"]) == 300
            assert res["time"][-1] == res["candles"][-1]["time"]
            for key in ("errors", "stdout", "meta", "plots", "markers", "hlines", "bgcolors", "alerts", "strategy"):
                assert key in res
            info = _events(rt, "info")[-1]["message"]
            assert "Prueba" in info and "SIM:NEXUS 1h" in info and "3 ops" in info and "+4.50%" in info and "66.67%" in info
            signals = _events(rt, "signal")
            assert len(signals) == 1 and "Compra: Cruce alcista" in signals[0]["message"]
            assert [e["message"] for e in _events(rt, "debug")][-2:] == ["PyChart ▸ hola", "PyChart ▸ mundo"]
            await svc.run(CODE, "SIM:NEXUS", "1h", 300)
            assert len(_events(rt, "signal")) == 1  # same alert on the same bar is not repeated
            st = svc.status()
            assert st["runs"] == 2 and st["last_run_ms"] == 12 and st["run_errors"] == 0

    run(scenario())


def test_run_clamps_limit_and_resolves_symbols(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeSandbox()
            svc = await _setup(rt, monkeypatch, fake)
            await svc.run(CODE, "SIM:NEXUS", "1h", 10)
            await svc.run(CODE, "SIM:NEXUS", "1h", 10**9)
            await svc.run(CODE, "btc", "1h", None)
            assert [c[2] for c in rt.hub.calls] == [service_mod.LIMIT_MIN, service_mod.LIMIT_MAX, 1000]
            assert rt.hub.calls[-1][0] == "BINANCE:BTCUSDT"

    run(scenario())


def test_run_input_errors_are_spanish_400(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            svc = await _setup(rt, monkeypatch, FakeSandbox())
            cases = [
                (CODE, "", "1h", 100, None),
                (CODE, "SIM:NEXUS", "7x", 100, None),
                ("   ", "SIM:NEXUS", "1h", 100, None),
                (CODE, "SIM:NEXUS", "1h", "mucho", None),
                (CODE, "SIM:NEXUS", "1h", 100, ["no", "dict"]),
            ]
            for args in cases:
                with pytest.raises(ScriptServiceError) as info:
                    await svc.run(*args)
                assert info.value.status_code == 400 and info.value.detail

            rt.hub = FakeHub(None)  # hub answers but has no candles
            with pytest.raises(ScriptServiceError) as info:
                await svc.run(CODE, "SIM:NADA", "1h", 100)
            assert info.value.status_code == 400 and "Sin velas" in info.value.detail
            rt.hub = None  # market module not loaded
            with pytest.raises(ScriptServiceError) as info:
                await svc.run(CODE, "SIM:NEXUS", "1h", 100)
            assert info.value.status_code == 400 and "mercado" in info.value.detail

    run(scenario())


def test_failed_and_crashing_runs_never_raise(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            svc = await _setup(rt, monkeypatch, FakeSandbox(ok=False))
            res = await svc.run(CODE, "SIM:NEXUS", "1h", 200)
            assert res["ok"] is False and res["errors"][0]["line"] == 2 and len(res["candles"]) == 200
            assert "línea 2" in _events(rt, "warn")[-1]["message"]

            monkeypatch.setattr(service_mod, "_load_sandbox", lambda: FakeSandbox(raises=RuntimeError("boom")))
            res = await svc.run(CODE, "SIM:NEXUS", "1h", 200)
            assert res["ok"] is False and res["errors"][0]["kind"] == "runtime" and "boom" in res["errors"][0]["message"]
            assert any("sandbox" in e["message"] for e in _events(rt, "error"))
            assert svc.status()["run_errors"] == 2

    run(scenario())


def test_missing_compiler_gives_503_but_crud_keeps_working(runtime_factory, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.scripting.validator", None)
    monkeypatch.setitem(sys.modules, "app.scripting.sandbox", None)

    async def scenario():
        async with runtime_factory() as rt:
            svc = await _setup(rt, monkeypatch)
            for coro in (svc.compile(CODE), svc.run(CODE, "SIM:NEXUS", "1h", 100)):
                with pytest.raises(ScriptServiceError) as info:
                    await coro
                assert info.value.status_code == 503 and info.value.detail == MSG_UNAVAILABLE
            assert (await svc.create("Sigue funcionando", CODE))["name"] == "Sigue funcionando"
            assert service_mod.compiler_available() is False

    run(scenario())


def test_compile_reports_errors_and_warns_once(runtime_factory, monkeypatch):
    pytest.importorskip("app.scripting.validator")

    async def scenario():
        async with runtime_factory() as rt:
            svc = await _setup(rt, monkeypatch)
            good = await svc.compile('indicator("Bien", overlay=False)\nn = input.int(14, "Longitud")\nplot(ta.rsi(close, n))\n')
            assert good["ok"] is True and good["errors"] == [] and good["meta"]["title"] == "Bien"
            assert good["meta"]["overlay"] is False and good["inputs"][0]["name"] == "n"
            bad = await svc.compile('indicator("Mal")\nx = (\n')
            assert bad["ok"] is False and bad["errors"][0]["kind"] == "syntax" and bad["errors"][0]["line"]
            unsafe = await svc.compile('import os\nindicator("Hack")\n')
            assert unsafe["ok"] is False and unsafe["errors"][0]["kind"] == "security"
            warns = [e for e in _events(rt, "warn") if "compilación" in e["message"]]
            assert len(warns) == 1  # rate-limited: the editor compiles on every keystroke
            assert svc.status()["compiles"] == 3

    run(scenario())
