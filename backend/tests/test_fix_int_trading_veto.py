"""Integration (trading): a semáforo veto is stored only for a real closed-bar decision (contract I3, verifies.engine
new issue): a run with `upto` (bar-close watcher, paper auto-trader), or a live view of the bar that has just
closed. A market sitting on an old closed bar (session closed, weekend) is vetoed for that view only, so the
history never depends on the semáforo of the moment someone opened the chart."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.engine import service as service_mod
from app.engine.service import VETO_RECORD_GRACE_S, NexusService
from tests.test_fix_engine_veto import SYM, FakeMacro, series_and_entry
from tests.test_fix_engine_watch import SeriesHub, WSRecorder

H = 3600


async def stored_vetoes(rt) -> list[dict]:
    row = await rt.db.fetchone("SELECT state FROM model_state WHERE kind='nexus_veto' AND key=?", (f"{SYM}|1h",))
    return json.loads(row["state"])["entries"] if row else []


@pytest.mark.parametrize("closed_ago, stored", [
    (30, True),  # the bar that just closed
    (VETO_RECORD_GRACE_S, True),
    (VETO_RECORD_GRACE_S + 1, False),
    (3 * H, False),  # session closed for the day
    (2 * 86400 + 600, False),  # weekend
])
def test_a_plain_live_view_stores_the_veto_only_of_the_bar_that_just_closed(runtime_factory, monkeypatch,
                                                                             closed_ago, stored):
    series, k, action, side = series_and_entry()
    t_k = int(series.time[k])
    monkeypatch.setattr(service_mod, "now_s", lambda: t_k + H + closed_ago)  # no newer bar: the market sits on k

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub, rt.ws, rt.macro = SeriesHub(series.upto(k)), WSRecorder(), FakeMacro("red")
            svc = rt.nexus = NexusService(rt)
            red = (await svc.analyze(SYM, "1h"))["current"]
            assert red["time"] == t_k and red["action"] == "WAIT" and red["position"] is None  # this view: vetoed
            assert "Fed" in red["advice"]
            entries = await stored_vetoes(rt)
            assert [(e["time"], e["direction"]) for e in entries] == ([(t_k, side)] if stored else [])
            rt.macro = FakeMacro("green")
            green = (await svc.analyze(SYM, "1h"))["current"]
            if stored:  # the decision taken at that close: the entry stays rejected
                assert green["action"] == "WAIT" and green["position"] is None
            else:  # a later semáforo does not rewrite an old close
                assert green["action"] == action and green["position"]["entry_time"] == t_k

    asyncio.run(scenario())


def test_a_bar_close_run_stores_the_veto_even_when_processed_late(runtime_factory, monkeypatch):
    """`upto` is the bar-close decision itself (watcher, auto-trader): stored whatever the clock, e.g. a feed that
    caught up hours after the close."""
    series, k, action, side = series_and_entry()
    t_k = int(series.time[k])
    monkeypatch.setattr(service_mod, "now_s", lambda: t_k + H + 3 * H)

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub, rt.ws, rt.macro = SeriesHub(series.upto(k)), WSRecorder(), FakeMacro("red")
            svc = rt.nexus = NexusService(rt)
            cur = await svc.current(SYM, "1h", upto=t_k)
            assert cur["time"] == t_k and cur["action"] == "WAIT" and cur["position"] is None
            assert [(e["time"], e["direction"]) for e in await stored_vetoes(rt)] == [(t_k, side)]
            rt.macro = FakeMacro("green")
            later = (await svc.analyze(SYM, "1h"))["current"]
            assert later["action"] == "WAIT" and later["position"] is None

    asyncio.run(scenario())
