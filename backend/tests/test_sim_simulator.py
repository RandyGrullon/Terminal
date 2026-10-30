"""BE-SIM: market simulator (synthetic session, auto-trading of TAKE verdicts, replay, controls, errors)."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from app import config as nexus_config
from app.sim.common import EngineUnavailable
from app.sim.paper import PaperEngine
from app.sim.simulator import SimError, Simulator
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_sim_fakes import FakeHub, make_current, require_engine


def scripted_nexus(buy_every: int = 6, verdict: str = "TAKE"):
    """BUY (with the given gate verdict) every `buy_every` revealed bars, HOLD otherwise."""

    def fn(cs, prior):
        n = len(cs)
        t, close = int(cs.time[-1]), float(cs.close[-1])
        action = "BUY" if n % buy_every == 0 else "HOLD_LONG"
        current = make_current(action, price=close, time=t, long_verdict=verdict, prob_up=0.6, score=0.4)
        markers = []
        if action == "BUY":
            kind = "BUY" if verdict == "TAKE" else "REJECTED_LONG"
            markers.append({"time": t, "kind": kind, "position": "belowBar", "shape": "arrowUp", "color": "#26a69a",
                            "text": "COMPRAR" if verdict == "TAKE" else "Filtro: NO TOMAR"})
        return {"ok": True, "current": current, "markers": markers, "trades": [], "stats": {}}, [(t, {"n": n})]

    return fn


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


async def _setup(rt, hub: FakeHub | None = None):
    rt.hub = hub or FakeHub()
    rt.paper = PaperEngine(rt)
    await rt.paper.start()
    sim = Simulator(rt)
    rt.sim = sim
    await sim.start()
    return sim


def test_synthetic_session_auto_trades_take_signals(runtime_factory, offline, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            published = []
            monkeypatch.setattr(rt.ws, "publish", lambda topic, msg: published.append((topic, msg)))
            sim = await _setup(rt)
            sim.nexus_fn = scripted_nexus(buy_every=6)
            state = await sim.start_session({"mode": "synthetic", "tf": "1h", "speed": 0.5, "seed": 3, "warmup": 60,
                                             "initial_balance": 5000})
            sim._paused = True  # drive the session by hand (deterministic)
            assert state["running"] and state["mode"] == "synthetic" and state["symbol"] == "SIM:NEXUS"
            assert set(state) >= {"running", "paused", "mode", "symbol", "tf", "speed", "regime", "bars_done",
                                  "bars_total", "price", "started_at", "auto_trade", "account", "last_action", "stats"}
            assert state["account"]["initial_balance"] == 5000 and state["bars_total"] is None
            assert len(rt.hub.pushed) == 60 and all(p[0] == "SIM:NEXUS" for p in rt.hub.pushed)
            for _ in range(30):
                await sim.step(sim._market.next_bar())
            assert sim.bars_done == 30 and len(rt.hub.pushed) == 90
            bar_msgs = [m for t, m in published if t == "sim" and m.get("event") == "bar"]
            assert len(bar_msgs) == 30
            msg = bar_msgs[-1]
            assert set(msg) == {"type", "event", "state", "bar", "current", "markers", "positions"}
            seen = [(m["time"], m["kind"]) for b in bar_msgs for m in b["markers"]]
            assert len(seen) == len(set(seen)) >= 4  # each marker published exactly once
            trades = await rt.db.fetchall("SELECT * FROM paper_positions WHERE account_id=2")
            assert trades and all(t["origin"] == "sim" for t in trades)
            assert all(t["gate_verdict"] == "TAKE" for t in trades)
            first = trades[0]
            entry_bar = next(b["bar"] for b in bar_msgs if b["markers"] and b["markers"][0]["kind"] == "BUY")
            assert first["entry_price"] == pytest.approx(entry_bar["close"] * 1.0002)  # bar close + slippage
            assert first["entry_time"] == entry_bar["time"] + 3600
            assert any(e["source"] == "sim" and e["message"].startswith("SIM:NEXUS 1h → COMPRAR")
                       for e in rt.bus.recent(level="signal"))
            snap = await sim.snapshot()
            assert set(snap) == {"state", "candles", "nexus", "positions"}
            assert len(snap["candles"]) == 90 and snap["nexus"]["symbol"] == "SIM:NEXUS"
            assert snap["nexus"]["provider"] == "synthetic" and snap["state"]["bars_done"] == 30
            json.dumps(snap, allow_nan=False)
            snap_times = [t for t, _ in sim._snapshots]
            assert snap_times and snap_times[-1] <= int(snap["candles"][-1]["time"])
            assert all(b - a >= 50 * 3600 for a, b in zip(snap_times, snap_times[1:]))  # thinned, never too dense
            st = await sim.configure(regime="bull", speed=50)
            assert st["regime"] == "bull" and sim._market.regime == "bull" and st["speed"] == 20.0
            stopped = await sim.stop_session()
            assert stopped["running"] is False and rt.paper.sim_positions(None) == []
            closed = await rt.db.fetchall("SELECT status FROM paper_positions WHERE account_id=2")
            assert all(r["status"] == "closed" for r in closed)
            assert any(e["message"].startswith("Simulación detenida:") for e in rt.bus.recent(level="success"))
            assert published[-1][1]["event"] == "stopped"
            assert sim.status() == {"running": False, "mode": "synthetic", "bars_done": 30}
            await rt.paper.stop()

    asyncio.run(scenario())


def test_rejected_signals_do_not_trade(runtime_factory, offline):
    async def scenario():
        async with runtime_factory() as rt:
            sim = await _setup(rt)
            sim.nexus_fn = scripted_nexus(buy_every=3, verdict="AVOID")
            await sim.start_session({"mode": "synthetic", "speed": 0.5, "seed": 5, "warmup": 60})
            sim._paused = True
            for _ in range(12):
                await sim.step(sim._market.next_bar())
            assert await rt.db.fetchval("SELECT COUNT(*) FROM paper_positions WHERE account_id=2") == 0
            assert any("rechazada por el filtro" in e["message"] for e in rt.bus.recent(level="info"))
            await sim.stop_session(emit=False)
            await rt.paper.stop()

    asyncio.run(scenario())


async def _wait_until(predicate, timeout: float = 30.0, step: float = 0.05) -> bool:
    """Poll `predicate` until true or timeout (robust on a loaded machine; returns the final value)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            return bool(predicate())
        await asyncio.sleep(step)
    return True


REPLAY_END = re.compile(r"^Replay terminado: \d+ operaciones, \d+ % aciertos, [+−]?\d+(,\d+)? % · 30 velas$")


def test_realtime_loop_and_replay_end(runtime_factory, offline):
    async def scenario():
        async with runtime_factory() as rt:
            real = generate_candleset(n=130, seed=9, end_time=FIXED_END, symbol="BINANCE:BTCUSDT")
            hub = FakeHub(candles={("BINANCE:BTCUSDT", "1h"): real})
            sim = await _setup(rt, hub)
            sim.nexus_fn = scripted_nexus(buy_every=7)
            await sim.start_session({"mode": "synthetic", "speed": 20, "seed": 1, "warmup": 60})
            assert await _wait_until(lambda: sim.bars_done >= 3)
            await sim.configure(paused=True)
            paused_at = sim.bars_done
            await asyncio.sleep(0.3)
            assert sim.bars_done <= paused_at + 1  # at most the bar that was in flight
            await sim.configure(paused=False)
            st = await sim.start_session({"mode": "replay", "symbol": "BINANCE:BTCUSDT", "tf": "1h", "speed": 20,
                                          "warmup": 100})
            assert st["mode"] == "replay" and st["bars_total"] == 30 and st["regime"] is None
            assert await _wait_until(lambda: not sim.state()["running"], timeout=60.0)
            final = sim.state()
            assert final["running"] is False and final["bars_done"] == 30
            ends = [e for e in rt.bus.recent(level="success") if e["message"].startswith("Replay terminado:")]
            assert len(ends) == 1 and REPLAY_END.match(ends[0]["message"]), ends
            assert final["stats"] is not None and final["stats"]["trades"] == ends[0]["data"]["stats"]["trades"]
            replay_pushes = [p for p in hub.pushed if p[0] == "SIM:REPLAY"]
            assert len(replay_pushes) == 130  # warmup + every revealed bar, under the SIM symbol
            assert [c["time"] for c in (await sim.snapshot())["candles"]] == real.time.tolist()
            await rt.paper.stop()

    asyncio.run(scenario())


def test_controls_and_errors(runtime_factory, offline):
    async def scenario():
        async with runtime_factory() as rt:
            short = generate_candleset(n=50, seed=9, end_time=FIXED_END, symbol="BINANCE:ETHUSDT")
            sim = await _setup(rt, FakeHub(candles={("BINANCE:ETHUSDT", "1h"): short}))
            sim.nexus_fn = scripted_nexus()
            with pytest.raises(SimError):
                await sim.configure(paused=True)
            with pytest.raises(SimError):
                await sim.configure(regime="bull")
            with pytest.raises(SimError):
                await sim.start_session({"mode": "replay"})
            with pytest.raises(SimError):
                await sim.start_session({"mode": "replay", "symbol": "BINANCE:ETHUSDT"})
            with pytest.raises(SimError):
                await sim.start_session({"mode": "nope"})
            with pytest.raises(SimError):
                await sim.start_session({"mode": "synthetic", "regime": "moon"})
            with pytest.raises(SimError):
                await sim.start_session({"mode": "synthetic", "tf": "7x"})
            st = await sim.configure(speed=0.1, auto_trade=False)
            assert st["speed"] == 0.5 and st["auto_trade"] is False
            assert (await sim.stop_session())["running"] is False  # stopping when idle is harmless
            await rt.paper.stop()

    asyncio.run(scenario())


def test_session_without_engine_still_reveals_prices(runtime_factory, offline):
    async def scenario():
        async with runtime_factory() as rt:
            sim = await _setup(rt)

            def broken(cs, prior):
                raise EngineUnavailable("Motor NEXUS no disponible (prueba)")

            sim.nexus_fn = broken
            await sim.start_session({"mode": "synthetic", "speed": 0.5, "warmup": 60})
            sim._paused = True
            for _ in range(3):
                await sim.step(sim._market.next_bar())
            st = sim.state()
            assert st["bars_done"] == 3 and st["price"] is not None and st["last_action"] is None
            warns = [e for e in rt.bus.recent(level="warn") if "Motor NEXUS no disponible" in e["message"]]
            assert len(warns) == 1  # rate-limited
            await sim.stop_session(emit=False)
            await rt.paper.stop()

    asyncio.run(scenario())


def test_real_engine_in_simulator(runtime_factory, offline):
    require_engine()

    async def scenario():
        async with runtime_factory() as rt:
            sim = await _setup(rt)
            await sim.start_session({"mode": "synthetic", "speed": 0.5, "warmup": 220, "seed": 2})
            sim._paused = True
            for _ in range(2):
                await sim.step(sim._market.next_bar())
            snap = await sim.snapshot()
            assert snap["nexus"] is not None and snap["nexus"].get("current") is not None
            await sim.stop_session(emit=False)
            await rt.paper.stop()

    asyncio.run(scenario())


def test_adaptive_nexus_window_keeps_up_with_speed(runtime_factory, offline):
    import time as _time

    async def scenario():
        async with runtime_factory() as rt:
            sim = await _setup(rt)
            seen: list[int] = []
            fast = scripted_nexus()

            def slow(cs, prior):
                seen.append(len(cs))
                _time.sleep(0.06)  # slower than the 0.04 s budget of 20 bars/s
                return fast(cs, prior)

            sim.nexus_fn = slow
            await sim.start_session({"mode": "synthetic", "speed": 20, "seed": 2, "warmup": 650})
            sim._paused = True
            for _ in range(4):
                await sim.step(sim._market.next_bar())
            assert seen == [600, 450, 337, 300, 300]  # shrinks to max(300, nexus.min_bars + 100), never below
            sim.speed = 0.5  # plenty of time per bar: the window grows back 50 bars per step
            sim.nexus_fn = fast
            for _ in range(7):
                await sim.step(sim._market.next_bar())
            assert sim._window == 600
            await rt.settings.set("nexus.min_bars", 400)
            for _ in range(10):
                sim.speed = 20.0
                sim._adapt_window(0.5)
            assert sim._window == 500  # floor follows nexus.min_bars
            await sim.stop_session(emit=False)
            await rt.paper.stop()

    asyncio.run(scenario())
