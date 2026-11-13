"""PyChart strategies as signal sources of NEXUS automática: the backtest tells what its rules ask for at the open after
the last bar (strategy.next, causal: the same signal the longer run executes), runs can leave out a forming candle and
be quiet; NEXUS automática opens a strategy's entry (with or without the gate's TOMAR) when NEXUS itself has none, with
the strategy's stop and target, closes it with the strategy's close rule (not with NEXUS's exits), ignores stale
signals, checks the chosen scripts (strategies without errors, at most 3), shows them and their errors; its journal
groups trades by source and «Aprender de sus pérdidas» pauses a losing strategy (never NEXUS's own signals as a
whole). Plus the terminal flags and the API."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.scripting import sandbox
from app.scripting import service as service_mod
from app.scripting.service import ScriptService, ScriptServiceError
from app.sim import journal as J
from app.sim import paper as pp
from app.sim.synthetic import generate_candleset
from app.util import floor_time, now_s
from tests.conftest import FIXED_END
from tests.test_imp_autopilot import SYM, H, BarNexus, _candle, _engine, _settle, no_background  # noqa: F401
from tests.test_scripting_service_run import FakeHub as ScriptHub
from tests.test_sim_fakes import make_current

CROSS = '''strategy("Cruce de prueba", initial_capital=10000, risk_pct=1, fee_pct=0.1)
rapida = ta.ema(close, 5)
lenta = ta.ema(close, 20)
arriba = ta.crossover(rapida, lenta)
abajo = ta.crossunder(rapida, lenta)
atr = ta.atr(14)
strategy.entry("L", "long", when=arriba)
strategy.exit("S", from_entry="L", stop=close - 2 * atr, limit=close + 3 * atr)
strategy.close("L", when=abajo)
'''


def _run(candles: list[dict]) -> dict:
    res = sandbox.run_script(CROSS, candles, {}, symbol="SIM:NEXUS", tf="1h", timeout=30.0)
    assert res["ok"], res["errors"]
    return res["strategy"]


def test_strategy_next_is_the_signal_the_longer_run_executes():
    candles = generate_candleset(n=500, seed=21, end_time=FIXED_END, symbol="SIM:NEXUS").to_list()
    full = _run(candles)
    times = [c["time"] for c in candles]
    entry = next(t for t in full["trades"] if t["exit_reason"] == "SIGNAL")  # entered and closed by its own rules
    j_in = times.index(entry["entry_time"])
    j_out = times.index(entry["exit_time"])
    # cut at the signal bar: its next action is that entry, at the next open
    nxt = _run(candles[:j_in])["next"]
    assert nxt["action"] == "entry" and nxt["side"] == "long" and nxt["id"] == "L" and nxt["time"] == times[j_in - 1]
    assert nxt["stop"] < nxt["price"] < nxt["limit"] and nxt["reverse"] is False
    assert nxt["price"] == candles[j_in - 1]["close"]
    # cut at the close signal of that trade: its next action closes it
    out = _run(candles[:j_out])["next"]
    assert out["action"] == "close" and out["id"] == "L" and out["time"] == times[j_out - 1]
    # a bar where no rule fires: nothing to do
    quiet = next(k for k in range(j_in + 1, j_out - 1) if _run(candles[:k + 1])["next"] is None)
    assert quiet is not None


class FakeSandbox:
    def __init__(self):
        self.candles: list[dict] = []

    def __call__(self, code, candles, inputs=None, *, symbol="", tf="", timeout=10.0, **kw):
        self.candles = list(candles)
        return {"ok": True, "errors": [], "stdout": "", "meta": {"title": "X", "overlay": True, "kind": "strategy"},
                "time": [c["time"] for c in candles], "plots": [], "markers": [], "hlines": [], "bgcolors": [],
                "alerts": [], "strategy": {"metrics": {"trades": 0}, "next": None}}


def test_closed_only_leaves_out_the_forming_candle_and_quiet_runs_log_nothing(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            forming = floor_time(now_s(), "1h")
            cs = generate_candleset(n=300, seed=4, end_time=forming + H, symbol="SIM:NEXUS")  # its last bar: forming
            rt.hub = ScriptHub(cs)
            fake = FakeSandbox()
            monkeypatch.setattr(service_mod, "_load_sandbox", lambda: fake)
            svc = ScriptService(rt)
            await svc.start()
            await svc.run(CROSS, "SIM:NEXUS", "1h", 300)
            assert fake.candles[-1]["time"] == forming  # by default the forming candle is in
            before = len(rt.bus.recent(500, source="scripts"))
            res = await svc.run(CROSS, "SIM:NEXUS", "1h", 300, closed_only=True, quiet=True)
            assert fake.candles[-1]["time"] == forming - H and res["ok"]
            assert len(rt.bus.recent(500, source="scripts")) == before  # a quiet run: no event (the caller reports)
            fake_fail = lambda *a, **k: {"ok": False, "errors": [{"line": 1, "col": 1, "kind": "runtime",  # noqa: E731
                                                                  "message": "x"}]}
            monkeypatch.setattr(service_mod, "_load_sandbox", lambda: fake_fail)
            res = await svc.run(CROSS, "SIM:NEXUS", "1h", 300, closed_only=True, quiet=True)
            assert res["ok"] is False and len(rt.bus.recent(500, source="scripts")) == before

    asyncio.run(scenario())


class FakeScripts:
    """rt.scripts for NEXUS automática: saved scripts by id; `next_at(upto)` answers the strategy's next action."""

    def __init__(self):
        self.saved = {7: {"id": 7, "name": "Cruce", "code": "strategy('Cruce')"},
                      8: {"id": 8, "name": "Mi RSI", "code": "indicator('RSI')"}}
        self.next_at = lambda upto: None
        self.fail: str | None = None
        self.runs: list[dict] = []

    async def get(self, script_id):
        if int(script_id) not in self.saved:
            raise ScriptServiceError(404, "Script no encontrado")
        return dict(self.saved[int(script_id)])

    async def compile(self, code):
        kind = "indicator" if "indicator(" in str(code) else "strategy"
        return {"ok": True, "errors": [], "warnings": [], "inputs": [], "meta": {"title": "x", "overlay": True,
                                                                              "kind": kind}}

    async def run(self, code, symbol, tf="1h", limit=1000, inputs=None, *, name=None, closed_only=False, quiet=False):
        self.runs.append({"symbol": symbol, "tf": tf, "limit": limit, "name": name, "closed_only": closed_only,
                          "quiet": quiet})
        if self.fail:
            return {"ok": False, "errors": [{"line": 3, "col": 1, "kind": "runtime", "message": self.fail}],
                    "meta": {"kind": "strategy"}, "strategy": None}
        return {"ok": True, "errors": [], "meta": {"kind": "strategy"}, "strategy": {"next": self.next_at(None)}}


def _entry(t: int, side: str = "long", stop: float | None = 95.0, limit: float | None = 110.0) -> dict:
    return {"action": "entry", "id": "L", "side": side, "time": t, "price": 100.0, "stop": stop, "limit": limit,
            "reverse": False, "comment": None}


async def _with_scripts(rt, nexus_action: str = "WAIT", **kw):
    engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current(nexus_action)))
    rt.scripts = FakeScripts()
    return engine


def test_a_strategy_entry_opens_with_its_levels_and_its_close_rule_closes_it(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _with_scripts(rt)
            rt.scripts.next_at = lambda _: None  # no signal on the bar it decides when it starts
            snap = await engine.set_autopilot(enabled=True, source="list", symbols=[SYM], script_ids=[7],
                                              script_gate=True)
            assert snap["autopilot"]["script_ids"] == [7] and snap["autopilot"]["script_gate"] is True
            assert snap["autopilot"]["scripts"][0]["name"] == "Cruce" and snap["autopilot"]["scripts"][0]["error"] is None
            await _settle(engine)
            t = engine._auto_bar[(3, SYM, "1h")]
            assert engine.open_positions(3) == []  # no signal on that bar from NEXUS nor from the strategy
            # next bar: NEXUS waits, the strategy enters (its signal is on that very bar)
            rt.scripts.next_at = lambda _: _entry(t + H)
            rt.nexus.closed[t + H] = make_current("WAIT", time=t + H)
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert (pos["side"], pos["sl"], pos["tp1"], pos["tp2"]) == ("long", 95.0, 110.0, None)
            log = engine.autopilot_status()["log"]
            assert log[0]["kind"] == "open" and "de la estrategia «Cruce»" in log[0]["text"]
            run = rt.scripts.runs[-1]
            assert (run["closed_only"], run["quiet"], run["limit"]) == (True, True, pp.SCRIPT_BARS)
            meta = engine._open[pos["id"]]["meta"]
            assert meta["context"]["source"] == "script:7:Cruce"
            # next bar: NEXUS says EXIT_LONG, but a strategy's position follows only its own rules: it stays open
            rt.scripts.next_at = lambda _: None
            rt.nexus.closed[t + 2 * H] = make_current("EXIT_LONG", time=t + 2 * H)
            await engine._on_candle(SYM, "1h", _candle(t + 2 * H), True)
            await _settle(engine)
            assert len(engine.open_positions(3)) == 1
            # then the strategy's close rule: closed at market, by its rule
            rt.scripts.next_at = lambda _: {**_entry(t + 3 * H), "action": "close", "stop": None, "limit": None}
            rt.nexus.closed[t + 3 * H] = make_current("WAIT", time=t + 3 * H)
            await engine._on_candle(SYM, "1h", _candle(t + 3 * H), True)
            await _settle(engine)
            assert engine.open_positions(3) == []
            [h] = await engine.history(3)
            assert h["exit_reason"] == "SIGNAL" and h["exit_label"] == "Salida de la estrategia «Cruce»"
            assert engine.autopilot_status()["log"][0]["text"].startswith("Cierra largo a ")
            await engine.stop()

    asyncio.run(scenario())


def test_gate_stale_signals_nexus_first_and_no_targets(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _with_scripts(rt)
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM], script_ids=[7], script_gate=True)
            await _settle(engine)
            t = engine._auto_bar[(3, SYM, "1h")]
            # the gate says NO TOMAR for longs: with script_gate on, the strategy's entry is skipped
            rt.nexus.closed[t + H] = make_current("WAIT", time=t + H, long_verdict="AVOID")
            rt.scripts.next_at = lambda _: _entry(t + H)
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await _settle(engine)
            assert engine.open_positions(3) == []
            assert "de la estrategia «Cruce» descartado por el filtro" in engine.autopilot_status()["log"][0]["text"]
            # a signal of an older bar (the candles were late): stale, never traded
            rt.nexus.closed[t + 2 * H] = make_current("WAIT", time=t + 2 * H)
            rt.scripts.next_at = lambda _: _entry(t + H)
            await engine.set_autopilot(script_gate=False)
            await engine._on_candle(SYM, "1h", _candle(t + 2 * H), True)
            await _settle(engine)
            assert engine.open_positions(3) == []
            # without the gate and with no target: opens with its stop only (its exits: stop + its close rule)
            rt.nexus.closed[t + 3 * H] = make_current("WAIT", time=t + 3 * H, long_verdict="AVOID")
            rt.scripts.next_at = lambda _: _entry(t + 3 * H, limit=None)
            await engine._on_candle(SYM, "1h", _candle(t + 3 * H), True)
            await _settle(engine)
            assert len(engine.open_positions(3)) == 1, engine.autopilot_status()["log"][:3]
            [pos] = engine.open_positions(3)
            assert (pos["sl"], pos["tp1"], pos["tp2"], pos["tp3"]) == (95.0, None, None, None)
            assert "filtro no exigido (NO TOMAR" in engine.autopilot_status()["log"][0]["text"]
            await engine.close_position(position_id=pos["id"], account_id=3)
            # NEXUS's own BUY comes first: its entry is NEXUS's, the strategy is not even asked
            runs = len(rt.scripts.runs)
            rt.nexus.closed[t + 4 * H] = make_current("BUY", time=t + 4 * H)
            await engine._on_candle(SYM, "1h", _candle(t + 4 * H), True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert engine._open[pos["id"]]["meta"]["context"]["source"] == "nexus" and len(rt.scripts.runs) == runs
            await engine.stop()

    asyncio.run(scenario())


def test_chosen_scripts_are_checked_and_their_errors_shown(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _with_scripts(rt)
            with pytest.raises(pp.PaperError, match="Script 99: Script no encontrado"):
                await engine.set_autopilot(script_ids=[99])
            with pytest.raises(pp.PaperError, match="«Mi RSI» es un indicador"):
                await engine.set_autopilot(script_ids=[8])
            rt.scripts.saved.update({k: {"id": k, "name": f"E{k}", "code": "strategy('x')"} for k in (9, 10, 11)})
            with pytest.raises(pp.PaperError, match="Como mucho 3 estrategias"):
                await engine.set_autopilot(script_ids=[7, 9, 10, 11])
            with pytest.raises(pp.PaperError, match="no válido"):
                await engine.set_autopilot(script_ids=["x"])
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM], script_ids=[7, 7, 9])
            assert engine.autopilot_status()["script_ids"] == [7, 9]
            rt.scripts.fail = "NameError: 'x' no existe"
            await _settle(engine)
            t = engine._auto_bar[(3, SYM, "1h")]
            rt.nexus.closed[t + H] = make_current("WAIT", time=t + H)
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await _settle(engine)
            st = engine.autopilot_status()
            assert st["scripts"][0]["error"] == "línea 3: NameError: 'x' no existe"
            assert any("Estrategia «Cruce»: línea 3" in e["text"] for e in st["log"])
            await engine.set_autopilot(script_ids=[])
            assert engine.autopilot_status()["scripts"] == []
            await engine.stop()

    asyncio.run(scenario())


def _trade(k: int, r: float, source: str) -> dict:
    day = 86_400
    t0 = 1_767_000_000 - 120 * day  # within the 90-day window relative to the trades themselves
    return {"r": r, "side": "long", "symbol": SYM, "entry_time": t0 + k * day, "exit_time": t0 + k * day + 3600,
            "exit_reason": "SIGNAL", "gate_score": 75.0, "context": {"regime": "trend_up", "source": source},
            "journal": {"cause": "salida_nexus_perdida" if r < 0 else "salida_nexus_ganancia", "mfe_r": 0.2},
            "instrument": None}


def test_journal_by_source_and_the_guard_pauses_a_losing_strategy_never_nexus():
    rng = np.random.default_rng(3)
    losing = [_trade(k, float(-1.0 + rng.normal(0, 0.05)), "script:7:Cruce") for k in range(40)]
    nexus = [_trade(k, float(-1.0 + rng.normal(0, 0.05)), "nexus") for k in range(40)]
    j = J.analyze(losing + nexus)
    group = next(g for g in j["groups"] if g["dimension"] == "source")
    assert group["label"] == "Fuente de la señal"
    assert {b["label"] for b in group["buckets"]} == {"Señales de NEXUS", "Estrategia «Cruce»"}
    causes = next(g for g in j["groups"] if g["dimension"] == "cause")
    assert "Su estrategia la cerró antes, en pérdida" in {b["label"] for b in causes["buckets"]}
    pauses = J.losing_contexts(losing + nexus)
    by_source = [(p["dimension"], p["key"]) for p in pauses if p["dimension"] == "source"]
    assert by_source == [("source", "script:7:Cruce")]  # NEXUS's own signals as a whole are never paused
    assert pp.script_id_of({"meta": {"context": {"source": "script:7:Cruce"}}}) == 7
    assert pp.script_id_of({"meta": {"context": {"source": "nexus"}}}) is None
    assert pp.script_levels({"next": _entry(1, limit=None)}) == {"sl": 95.0, "no_targets": True}
    assert pp.script_levels({"next": _entry(1)}) == {"sl": 95.0, "tps": [110.0]}


def test_terminal_flags_and_api(api_client):
    res = api_client.post("/api/paper/autopilot", json={"script_ids": [987654]})
    assert res.status_code == 400 and "Script 987654" in res.json()["detail"]
    st = api_client.get("/api/paper/autopilot").json()["autopilot"]
    assert (st["script_ids"], st["script_gate"], st["scripts"]) == ([], True, [])
    out = api_client.post("/api/terminal", json={"text": "autopilot --estrategias abc"}).json()
    assert out["ok"] is False and "--estrategias" in "\n".join(line["text"] for line in out["lines"])
    out = api_client.post("/api/terminal", json={"text": "autopilot --filtro-estrategias quizas"}).json()
    assert out["ok"] is False
    # an example strategy of PyChart (seeded at start): chosen from the terminal, then removed
    scripts = api_client.get("/api/scripts").json()["scripts"]
    cross = next(s for s in scripts if s["name"] == "Cruce de EMAs (estrategia)")
    out = api_client.post("/api/terminal", json={"text": f"autopilot --estrategias {cross['id']} "
                                                         "--filtro-estrategias off"}).json()
    text = "\n".join(line["text"] for line in out["lines"])
    assert out["ok"], text
    assert f"Estrategias de PyChart: «Cruce de EMAs (estrategia)» ({cross['id']}) · sin el filtro TOMAR" in text
    out = api_client.post("/api/terminal", json={"text": "autopilot --estrategias no"}).json()
    assert out["ok"] and api_client.get("/api/paper/autopilot").json()["autopilot"]["script_ids"] == []
