"""Noticias para PyChart (2/2): la API `noticias` en el sandbox real (velas que tocan cada noticia, filtros de eventos
y divisa, avisos de cobertura, solo lectura) y el paso del calendario desde ScriptService. Sin red."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.macro import fomc
from app.scripting import service as service_mod
from app.scripting.examples import load_examples
from app.scripting.service import ScriptService
from tests.test_scripting_service_run import FakeHub

BASE = fomc.ny_time("2025-01-15", 6, 0)  # 11:00 UTC; the CPI of that day is at 08:30 New York = 13:30 UTC
CPI = fomc.ny_time("2025-01-15", 8, 30)
STEP = 300
NEWS = {
    "events": [
        {"time": CPI, "title": "IPC de EE. UU. (inflación)", "currency": "USD", "impact": "high", "principal": True},
        {"time": CPI + 3600, "title": "Dato menor", "currency": "USD", "impact": "medium", "principal": False},
        {"time": CPI + 7200, "title": "Decisión de tipos del BCE", "currency": "EUR", "impact": "high", "principal": False},
    ],
    "from": fomc.ny_time("2024-02-01", 0, 0),
    "until": fomc.ny_time("2027-12-01", 0, 0),
}


def _candles(start: int = BASE, n: int = 72) -> list[dict]:
    return [{"time": start + k * STEP, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0}
            for k in range(n)]


def _run(code: str, candles: list[dict], news: dict | None = NEWS, inputs: dict | None = None) -> dict:
    sandbox = pytest.importorskip("app.scripting.sandbox")
    kwargs = {"news": news} if news is not None else {}
    return sandbox.run_script(code, candles, inputs or {}, symbol="OANDA:XAUUSD", tf="5m", timeout=30.0, **kwargs)


def _marked(res: dict, title: str) -> list[int]:
    plot = next(p for p in res["plots"] if p["title"] == title)
    return [t for t, v in zip(res["time"], plot["data"]) if v]


PROBE = """indicator("Noticias", overlay=False)
plot(np.where(noticias.cerca(30, 30), 1.0, 0.0), "ventana")
plot(np.where(noticias.cerca(0, 0), 1.0, 0.0), "vela")
plot(np.where(noticias.cerca(0, 0, eventos="medio"), 1.0, 0.0), "medio")
plot(np.where(noticias.cerca(0, 0, eventos="alto", moneda=None), 1.0, 0.0), "alto")
print("disponible", noticias.disponible, "eventos", len(noticias.lista()), len(noticias.lista(eventos="todos", moneda=None)))
"""


def test_cerca_marks_the_bars_around_each_event():
    candles = _candles()
    res = _run(PROBE, candles)
    assert res["ok"], res["errors"]
    window = _marked(res, "ventana")
    # every 5-minute bar overlapping [08:00, 09:00] New York: opens from 08:00 to 09:00 → 13 bars
    assert window == [CPI - 1800 + k * STEP for k in range(13)]
    assert _marked(res, "vela") == [CPI]  # only the principal event, only its bar
    assert _marked(res, "medio") == [CPI, CPI + 3600]  # USD by default: the EUR event is left out
    assert _marked(res, "alto") == [CPI, CPI + 7200]  # moneda=None: every currency, only high impact
    assert "disponible True eventos 1 3" in res["stdout"]
    assert "historial antes de noviembre de 2026" in res["stdout"]  # notice for the non-principal sets


def test_without_calendar_nothing_is_marked_and_the_console_says_so():
    res = _run(PROBE, _candles(), news=None)
    assert res["ok"], res["errors"]
    assert _marked(res, "ventana") == [] and "disponible False eventos 0 0" in res["stdout"]
    assert "no tiene calendario de noticias" in res["stdout"]


def test_bars_before_the_coverage_get_a_notice():
    early = fomc.ny_time("2023-12-01", 0, 0)
    res = _run('indicator("x")\nplot(np.where(noticias.cerca(), 1.0, 0.0), "v")\n', _candles(start=early, n=12))
    assert res["ok"], res["errors"]
    assert "las noticias principales empiezan el" in res["stdout"]


def test_wrong_arguments_are_spanish_runtime_errors():
    res = _run('indicator("x")\nplot(np.where(noticias.cerca(eventos="raros"), 1.0, 0.0), "v")\n', _candles())
    assert not res["ok"] and "eventos debe ser uno de" in res["errors"][0]["message"]
    res = _run('indicator("x")\nplot(np.where(noticias.cerca(-5, 10), 1.0, 0.0), "v")\n', _candles())
    assert not res["ok"] and "minutos entre 0 y 10080" in res["errors"][0]["message"]


def test_noticias_is_read_only_and_hides_its_internals():
    res = _run('indicator("x")\nnoticias.desde = 0\nplot(close, "c")\n', _candles())
    assert not res["ok"] and "solo lectura" in res["errors"][0]["message"]
    res = _run('indicator("x")\nprint(noticias._events)\nplot(close, "c")\n', _candles())
    assert not res["ok"] and res["errors"][0]["kind"] in ("security", "syntax")


def test_strategy_example_skips_signals_near_news():
    """The example strategy: a London-high sweep at 07:30 New York (inside the 30-minute window before an 08:00
    event) is discarded with the filter on and traded with it off."""
    code = next(ex["code"] for ex in load_examples() if ex["file"] == "estrategia_rangos_sesion.py")
    base = fomc.ny_time("2026-01-13", 1, 0)
    candles = []
    for k in range(12 * 12):
        t = base + k * STEP
        o, h, lo, c = 100.0, 100.2, 99.8, 100.0
        if t == fomc.ny_time("2026-01-13", 2, 30):
            h = 120.0  # London high
        if t == fomc.ny_time("2026-01-13", 5, 0):
            lo = 80.0  # London low
        if t == fomc.ny_time("2026-01-13", 7, 30):
            h = 121.0  # sweep of the London high, closes back inside
        candles.append({"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0})
    news = {"events": [{"time": fomc.ny_time("2026-01-13", 8, 0), "title": "Noticia", "currency": "USD",
                        "impact": "high", "principal": True}], "from": NEWS["from"], "until": NEWS["until"]}
    filtered = _run(code, candles, news)
    unfiltered = _run(code, candles, news, {"evitar_noticias": False})
    assert filtered["ok"] and unfiltered["ok"], (filtered["errors"], unfiltered["errors"])
    assert filtered["strategy"]["metrics"]["trades"] == 0 and "Descartadas por noticias: 1" in filtered["stdout"]
    assert unfiltered["strategy"]["metrics"]["trades"] == 1


def test_strategy_example_costs_follow_the_market_of_the_symbol():
    """Its default costs are those of the symbol's market (crypto ten times forex), or the user's own."""
    sandbox = pytest.importorskip("app.scripting.sandbox")
    code = next(ex["code"] for ex in load_examples() if ex["file"] == "estrategia_rangos_sesion.py")
    candles = _candles(n=48)

    def costs(symbol: str, inputs: dict | None = None) -> tuple[float, float, str]:
        res = sandbox.run_script(code, candles, inputs or {}, symbol=symbol, tf="5m", timeout=30.0, news=NEWS)
        assert res["ok"], res["errors"]
        params = res["strategy"]["params"]
        return params["fee_pct"], params["slippage_pct"], res["stdout"]

    fee, slip, out = costs("BINANCE:BTCUSDT")
    assert (fee, slip) == (0.1, 0.02) and "Costes (cripto): comisión 0,1 % y deslizamiento 0,02 % por lado" in out
    assert costs("OANDA:EURUSD")[:2] == (0.005, 0.005)
    assert costs("CME_MINI:NQ1!")[:2] == (0.01, 0.005)
    assert costs("OANDA:XAUUSD")[:2] == (0.02, 0.01) and costs("NASDAQ:AAPL")[:2] == (0.02, 0.01)
    own = costs("BINANCE:BTCUSDT", {"costes": "Personalizados", "comision_propia": 0.04, "desliz_propio": 0.01})
    assert own[:2] == (0.04, 0.01) and "Costes (personalizados)" in own[2]


class RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, code, candles, inputs=None, *, symbol="", tf="", timeout=10.0, news=None):
        self.calls.append({"news": news, "first": candles[0]["time"], "last": candles[-1]["time"]})
        return {"ok": True, "errors": [], "stdout": "", "meta": {"title": "x", "overlay": True, "kind": "indicator"},
                "time": [c["time"] for c in candles], "plots": [], "markers": [], "hlines": [], "bgcolors": [],
                "alerts": [], "strategy": None}


class FakeMacro:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.calls = fail, []

    def script_events(self, start_s, end_s):
        self.calls.append((start_s, end_s))
        if self.fail:
            raise RuntimeError("calendario roto")
        return {"events": [], "from": 1, "until": 2}


def test_service_hands_the_calendar_of_the_candles_to_the_sandbox(runtime_factory, monkeypatch):
    from app.sim.synthetic import generate_candleset
    from tests.conftest import FIXED_END

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(generate_candleset(n=200, seed=9, end_time=FIXED_END, symbol="SIM:NEXUS"))
            sandbox = RecordingSandbox()
            monkeypatch.setattr(service_mod, "_load_sandbox", lambda: sandbox)
            svc = ScriptService(rt)
            await svc.start()
            rt.macro = FakeMacro()
            await svc.run('indicator("x")\nplot(close, "c")\n', "SIM:NEXUS", "1h", 100)
            call = sandbox.calls[-1]
            assert call["news"] == {"events": [], "from": 1, "until": 2}
            assert rt.macro.calls == [(call["first"] - service_mod.NEWS_MARGIN_S, call["last"] + service_mod.NEWS_MARGIN_S)]
            rt.macro = FakeMacro(fail=True)  # a broken calendar never breaks the run
            res = await svc.run('indicator("x")\nplot(close, "c")\n', "SIM:NEXUS", "1h", 100)
            assert res["ok"] and sandbox.calls[-1]["news"] is None
            rt.macro = None
            await svc.run('indicator("x")\nplot(close, "c")\n', "SIM:NEXUS", "1h", 100)
            assert sandbox.calls[-1]["news"] is None

    asyncio.run(scenario())


def test_clean_news_bounds_the_payload():
    sandbox = pytest.importorskip("app.scripting.sandbox")
    many = {"events": [{"time": i, "title": "x" * 500, "currency": "USDXXXXXXXX", "impact": "high", "principal": 1}
                       for i in range(sandbox.MAX_NEWS_EVENTS + 5)] + [{"time": "nope"}, "basura"],
            "from": "2", "until": float("nan")}
    out = sandbox.clean_news(many)
    assert len(out["events"]) == sandbox.MAX_NEWS_EVENTS
    assert len(out["events"][0]["title"]) == 120 and len(out["events"][0]["currency"]) == 8
    assert out["events"][0]["principal"] is True and out["until"] is None
    assert sandbox.clean_news(None) is None and sandbox.clean_news([1, 2]) is None
    assert np.isfinite(out["events"][-1]["time"])
