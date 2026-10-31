"""Regression tests (engine fixes): the bar-close watcher decides on the bar that CLOSED, not on the seconds-old
forming bar the hub already holds (watcher-forming-bar, contract I1), and its gate_log horizon starts at the
decision bar (replay-gatelog-horizon)."""
from __future__ import annotations

import asyncio
import json

from app import config
from app.config import DEFAULT_SETTINGS
from app.data.base import CandleSet
from app.engine.nexus import NexusParams, run_nexus
from app.engine.service import NexusService
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

SYM = "SIM:FORMING"


class SeriesHub:
    """Hub fake that serves a fixed candle set (the newest bar may be a forming one)."""

    def __init__(self, cs: CandleSet) -> None:
        self.cs = cs

    async def get_candles(self, symbol: str, tf: str = "1h", limit: int = 500) -> CandleSet:
        return self.cs.tail(limit)


class WSRecorder:
    def __init__(self, listeners: int = 0) -> None:
        self.messages: list[tuple[str, dict]] = []
        self.listeners = listeners

    def publish(self, topic: str, message: dict) -> None:
        self.messages.append((topic, message))

    def subscribers(self, topic: str) -> int:
        return self.listeners


def with_forming(cs: CandleSet, k: int) -> CandleSet:
    """Bars 0..k (closed) + bar k+1 as the hub holds it seconds after it opened (o = h = l = c, ~no volume)."""
    rows = [(int(cs.time[i]), float(cs.open[i]), float(cs.high[i]), float(cs.low[i]), float(cs.close[i]),
             float(cs.volume[i])) for i in range(k + 1)]
    o = float(cs.open[k + 1])
    rows.append((int(cs.time[k + 1]), o, o, o, o, 0.001))
    return CandleSet.from_rows(rows, cs.symbol, cs.tf, provider="synthetic", history="full")


def entry_bar(cs: CandleSet, lo: int, hi: int) -> tuple[int, str]:
    """First bar k in [lo, hi) where NEXUS decides BUY/SELL at the close. The series is shorter than the default
    limit, so the service analyses every bar from 0; by causality the decision at k is the same with or without
    the later bars."""
    actions = run_nexus(cs.upto(hi), NexusParams.from_settings(DEFAULT_SETTINGS)).bars["action"]
    for k in range(lo, hi):
        if actions[k] in ("BUY", "SELL"):
            return k, actions[k]
    raise AssertionError("no entry decision found in the synthetic series")


async def make_service(rt, cs: CandleSet) -> NexusService:
    rt.hub, rt.ws = SeriesHub(cs), WSRecorder()
    rt.nexus = NexusService(rt)
    return rt.nexus


def test_closed_bar_decision_ignores_the_forming_bar(runtime_factory):
    series = generate_candleset(n=700, seed=11, tf="1h", end_time=FIXED_END, symbol=SYM)
    k, action = entry_bar(series, 440, 690)
    t_k, t_forming = int(series.time[k]), int(series.time[k + 1])
    side = "long" if action == "BUY" else "short"

    async def scenario():
        async with runtime_factory() as rt:
            svc = await make_service(rt, with_forming(series, k))
            rt.ws.listeners = 1  # the chart of this market is open
            # what the live chart sees: the forming bar is the last bar and the entry decided at k already filled
            live = await svc.analyze(SYM, "1h")
            assert live["current"]["time"] == t_forming and live["current"]["action"] not in ("BUY", "SELL")

            resp = await svc.process_closed_bar(SYM, "1h", upto=t_k)
            cur = resp["current"]
            assert cur["time"] == t_k and cur["action"] == action
            assert cur["price"] == float(series.close[k])
            # the chart keeps receiving its live view (like /api/nexus), which carries the entry marker of bar k
            nexus_msgs = [m for t, m in rt.ws.messages if t == f"market:{SYM}:1h" and m.get("type") == "nexus"]
            assert len(nexus_msgs) == 1 and nexus_msgs[0]["current"]["time"] == t_forming
            assert any(m["time"] == t_k and m["kind"] == action for m in nexus_msgs[0]["markers"])

            sig = await rt.db.fetchall("SELECT time, action FROM signals WHERE symbol=?", (SYM,))
            assert [(int(r["time"]), r["action"]) for r in sig] == [(t_k, action)]
            pred = await rt.db.fetchone("SELECT features FROM predictions WHERE source='nexus' AND symbol=?", (SYM,))
            assert json.loads(pred["features"])["bar_time"] == t_k
            rows = await rt.db.fetchall(
                "SELECT time, direction, taken, source, target_at FROM gate_log WHERE symbol=?", (SYM,))
            taken = [r for r in rows if int(r["taken"]) == 1]
            assert len(taken) == 1 and int(taken[0]["time"]) == t_k and taken[0]["direction"] == side
            assert taken[0]["source"] == "watch"
            # outcome horizon = close of the time_stop_bars-th bar after the decision bar (not the wall clock of the
            # write): the evaluator's window, one convention for every gate_log writer
            assert all(int(r["target_at"]) == (int(r["time"]) + 49 * 3600) * 1000 for r in rows)
            word = "COMPRAR" if action == "BUY" else "VENDER"
            events = [e["message"] for e in rt.bus.recent(50) if e["level"] == "signal"]
            assert any(f"→ {word}" in m for m in events)

            # the paper auto-trader's closed-bar read (contract I1) sees the same decision; the live view is cached
            # apart and still shows the forming bar
            closed = await svc.current(SYM, "1h", upto=t_k)
            assert closed["time"] == t_k and closed["action"] == action
            again = await svc.analyze(SYM, "1h")
            assert again["current"]["time"] == t_forming

    asyncio.run(scenario())


def test_upto_after_the_last_bar_keeps_every_bar(runtime_factory):
    series = generate_candleset(n=500, seed=4, tf="1h", end_time=FIXED_END, symbol=SYM)

    async def scenario():
        async with runtime_factory() as rt:
            svc = await make_service(rt, series)
            cur = await svc.current(SYM, "1h", upto=int(series.time[-1]) + 10 * 3600)  # hub not behind: nothing cut
            assert cur["time"] == int(series.time[-1])
            older = await svc.current(SYM, "1h", upto=int(series.time[-5]))
            assert older["time"] == int(series.time[-5])

    asyncio.run(scenario())


def test_on_candle_passes_the_closed_bar_and_queues_the_newest(runtime_factory, monkeypatch):
    monkeypatch.setattr(config, "BACKGROUND", True)
    series = generate_candleset(n=300, seed=4, tf="1h", end_time=FIXED_END, symbol=SYM)

    async def scenario():
        async with runtime_factory() as rt:
            svc = await make_service(rt, series)
            svc._watch.add((SYM, "1h"))
            calls: list = []
            release = asyncio.Event()

            async def fake_process(symbol, tf, upto=None):
                calls.append((symbol, tf, upto))
                if len(calls) == 1:
                    await release.wait()

            svc.process_closed_bar = fake_process
            await svc._on_candle(SYM, "1h", {"time": 1000}, True)
            await asyncio.sleep(0)
            await svc._on_candle(SYM, "1h", {"time": 2000}, True)  # closes while 1000 is still processing
            await svc._on_candle(SYM, "1h", {"time": 3000}, True)  # newer: replaces the queued one
            await svc._on_candle(SYM, "1h", {"time": 3000}, True)  # duplicate: ignored
            release.set()
            for _ in range(20):
                await asyncio.gather(*list(svc._bg_tasks))
                if not svc._bg_tasks:
                    break
            assert calls == [(SYM, "1h", 1000), (SYM, "1h", 3000)]
            assert not svc._processing and not svc._pending_bar

    asyncio.run(scenario())
