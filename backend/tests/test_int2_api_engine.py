"""int2-api · NexusService._save_model drops stale stored anchors BEFORE merging the run's snapshots.

An anchor is stale when a semáforo veto (or a bar-close gate record) of a bar at or before it was recorded after its
run read the stored decisions. merge_snapshots is write-once, so a stale stored anchor used to win over the run's own
fresh anchor of the same bar (or crowd out a nearby one) and was dropped only afterwards: that bucket stayed empty
until a later, longer run. Now the run's fresh anchor takes its place at once. Nothing else changes.
Runs on PostgreSQL too with NEXUS_TEST_PG_URL (runtime_factory).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.engine import service as service_mod
from app.engine.service import ANCHOR_SPAN, NexusService
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_imp_engine_repaint_service import LIMIT, SYM, closes

H = 3600
FP = "fp-int2"


class _Res:
    """The two attributes of a NexusResult that _save_model reads."""

    def __init__(self, snapshots: list[dict]):
        self.snapshots = snapshots
        self.learning = {"updates": 3, "hit_rate": 0.5, "brier": 0.25, "trained_until": 0, "memory_bars": 0}


def _anchor(t: int, mark: str, made: int | None = None) -> dict:
    resume = {"fp": FP, "mark": mark}
    if made is not None:
        resume["made"] = made
    return {"time": t, "kind": "grid", "state": {"resume": resume}}


async def _stored(rt) -> list[dict]:
    row = await rt.db.fetchone("SELECT state FROM model_state WHERE kind='nexus' AND key=?", (f"{SYM}|1h",))
    return json.loads(row["state"])["snapshots"] if row else []


def test_a_fresh_anchor_replaces_a_stale_stored_one_of_the_same_bar(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            t = 500 * H
            await svc._record_veto(SYM, "1h", {"time": t - 5 * H, "direction": "long", "level": "red", "detail": "x"})
            veto_at = (await svc._vetoes_for(SYM, "1h"))[(t - 5 * H, "long")]["at"]
            old = {"snapshots": [_anchor(t - 200 * H, "old-kept", veto_at - 1), _anchor(t, "old-stale", veto_at - 1),
                                 None, "junk", {"time": "bad", "state": {}}]}  # junk never breaks the save
            run = _Res([_anchor(t - 200 * H, "run"), _anchor(t, "run"), _anchor(t + 30 * H, "run")])
            ver = svc._anchor_ver.get((SYM, "1h"), 0)
            await svc._save_model(SYM, "1h", old, run, until=t + 100 * H, fingerprint=FP, made=veto_at + 1)
            snaps = {s["time"]: s for s in await _stored(rt)}
            # the bar before the vetoed one: not stale, write-once as before (the stored anchor stays)
            assert snaps[t - 200 * H]["state"]["resume"]["mark"] == "old-kept"
            # the stale anchor is gone and the run's own anchor of that bar (which applied the veto) is kept
            assert snaps[t]["state"]["resume"]["mark"] == "run" and snaps[t]["state"]["resume"]["made"] > veto_at
            assert t + 30 * H not in snaps  # still 50 bars from every kept anchor (the write-once gap)
            assert svc._anchor_ver[(SYM, "1h")] == ver + 1

    asyncio.run(scenario())


def test_a_stale_stored_anchor_no_longer_crowds_out_a_nearby_fresh_one(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            t = 500 * H
            await svc._record_veto(SYM, "1h", {"time": t - 1 * H, "direction": "short", "level": "amber", "detail": "x"})
            veto_at = (await svc._vetoes_for(SYM, "1h"))[(t - H, "short")]["at"]
            old = {"snapshots": [_anchor(t, "old-stale", veto_at - 1)]}
            run = _Res([_anchor(t + 20 * H, "run")])  # 20 bars away: inside the 50-bar gap of the stored one
            await svc._save_model(SYM, "1h", old, run, until=t + 100 * H, fingerprint=FP, made=veto_at + 1)
            assert [(s["time"], s["state"]["resume"]["mark"]) for s in await _stored(rt)] == [(t + 20 * H, "run")]

    asyncio.run(scenario())


def test_an_anchor_of_this_run_that_missed_a_veto_recorded_meanwhile_is_not_stored(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            t = 500 * H
            await svc._record_veto(SYM, "1h", {"time": t - 2 * H, "direction": "long", "level": "red", "detail": "x"})
            veto_at = (await svc._vetoes_for(SYM, "1h"))[(t - 2 * H, "long")]["at"]
            run = _Res([_anchor(t - 100 * H, "run"), _anchor(t, "run")])  # read the vetoes before it was recorded
            await svc._save_model(SYM, "1h", {}, run, until=t + 100 * H, fingerprint=FP, made=veto_at - 1)
            assert [s["time"] for s in await _stored(rt)] == [t - 100 * H]

    asyncio.run(scenario())


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 0}
    monkeypatch.setattr(service_mod, "now_s", lambda: now["t"])
    monkeypatch.setattr(service_mod, "CACHE_TTL_S", 0.0)
    return now


def test_the_vetoed_bucket_keeps_an_anchor_after_the_next_close(runtime_factory, clock):
    """End to end with the engine: once a veto is recorded, the next bar-close run stores its own anchor at the bar
    of the stale one (before this change that bucket stayed empty after that run)."""
    cs = generate_candleset(n=LIMIT + ANCHOR_SPAN + 400, seed=9, tf="1h", end_time=FIXED_END, symbol=SYM)
    k0 = LIMIT + ANCHOR_SPAN + 2

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            await closes(svc, rt, cs, clock, range(k0, k0 + 260, 13))
            anchors = [s for s in await _stored(rt) if s["kind"] != "last"]
            newest = anchors[-1]
            vetoed_bar = newest["time"] - 5 * H
            await svc._record_veto(SYM, "1h", {"time": vetoed_bar, "direction": "long", "level": "red",
                                               "detail": "prueba"})
            veto_at = (await svc._vetoes_for(SYM, "1h"))[(vetoed_bar, "long")]["at"]
            await closes(svc, rt, cs, clock, [k0 + 261])
            after = {s["time"]: s for s in await _stored(rt) if s["kind"] != "last"}
            # the anchor of that bar was rewritten by a run that read the veto: `made` (run start − 1 ms) is not before
            # the veto's `at` (stale_for_vetoes: stale only when at > made); a fast run can land in the same ms
            assert newest["time"] in after and after[newest["time"]]["state"]["resume"]["made"] >= veto_at
            assert {s["time"] for s in anchors} <= set(after)  # no bucket was left empty
            for t, s in after.items():
                if t >= vetoed_bar:  # every anchor from the vetoed bar on includes the veto (same rule as above)
                    assert s["state"]["resume"]["made"] >= veto_at, t

    asyncio.run(scenario())
