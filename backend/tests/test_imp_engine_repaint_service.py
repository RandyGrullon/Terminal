"""engine-repaint: how NexusService keeps the stored path consistent.

- Memory written before full resume states (or with other engine params) is replaced bar by bar by the first run
  that recomputes it, so history changes once (the upgrade / the settings change) and then never again.
- An anchor whose run had not seen a semáforo veto of a bar at or before it (recorded later) is dropped.
- merge_snapshots: write-once, except an anchor without the current path fingerprint at the same bar.
- Engine budget for the resumed window of a default analysis (extra bars included).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.engine import service as service_mod
from app.engine.nexus import NexusParams, run_nexus
from app.engine.nexus_resume import path_fingerprint, resume_fit
from app.engine.service import ANCHOR_SPAN, NexusService, merge_snapshots, snapshot_fingerprint, stale_for_vetoes
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_imp_engine_repaint_history import Hub, first_processed, history
from tests.test_fix_engine_watch import with_forming

SYM = "SIM:REPAINT2"
H = 3600
LIMIT = 300


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 0}
    monkeypatch.setattr(service_mod, "now_s", lambda: now["t"])
    monkeypatch.setattr(service_mod, "CACHE_TTL_S", 0.0)
    return now


async def stored(rt) -> dict:
    row = await rt.db.fetchone("SELECT state FROM model_state WHERE kind='nexus' AND key=?", (f"{SYM}|1h",))
    return json.loads(row["state"]) if row else {}


async def closes(svc, rt, cs, clock, ks) -> tuple[int, int, dict]:
    """Bar-close runs over the bars `ks`: (runs whose past changed, anchor steps, last response)."""
    prev, changed, steps = None, 0, 0
    for k in ks:
        clock["t"] = int(cs.time[k + 1]) + 120
        rt.hub = Hub(with_forming(cs, k))
        resp = await svc.analyze(SYM, "1h", LIMIT, upto=int(cs.time[k]))
        if prev is not None:
            last = prev["bars"]["time"][-1]
            start = max(first_processed(prev), first_processed(resp))
            steps += first_processed(prev) != first_processed(resp)
            changed += history(prev, start, last, last) != history(resp, start, last, last)
        prev = resp
    return changed, steps, prev


def test_memory_without_resume_states_is_rewritten_once(runtime_factory, clock):
    cs = generate_candleset(n=LIMIT + ANCHOR_SPAN + 700, seed=9, tf="1h", end_time=FIXED_END, symbol=SYM)
    k0 = LIMIT + ANCHOR_SPAN + 2

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            await closes(svc, rt, cs, clock, range(k0, k0 + 250, 5))
            state = await stored(rt)
            for snap in state["snapshots"]:  # what a database written before this change holds
                snap["state"].pop("resume", None)
            await rt.db.execute("UPDATE model_state SET state=? WHERE kind='nexus' AND key=?",
                                (json.dumps(state), f"{SYM}|1h"))
            changed, steps, last = await closes(svc, rt, cs, clock, range(k0 + 250, k0 + 500))
            assert steps >= 2 and changed <= 1  # only the first run recomputed the window (no resume state)
            snaps = (await stored(rt))["snapshots"]
            fp = path_fingerprint(svc.params(SYM))
            used = [s for s in snaps if s["kind"] != "last" and s["time"] > int(cs.time[k0 + 260])]
            assert used and all(snapshot_fingerprint(s) == fp for s in used)

    asyncio.run(scenario())


def test_a_settings_change_recomputes_history_once(runtime_factory, clock):
    cs = generate_candleset(n=LIMIT + ANCHOR_SPAN + 700, seed=9, tf="1h", end_time=FIXED_END, symbol=SYM)
    k0 = LIMIT + ANCHOR_SPAN + 2

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            await closes(svc, rt, cs, clock, range(k0, k0 + 200, 4))
            before = path_fingerprint(svc.params(SYM))
            await rt.settings.set("nexus.entry_threshold", 0.25)
            assert path_fingerprint(svc.params(SYM)) != before
            changed, steps, _ = await closes(svc, rt, cs, clock, range(k0 + 200, k0 + 450))
            assert steps >= 2 and changed <= 1

    asyncio.run(scenario())


def test_an_anchor_that_missed_a_later_recorded_veto_is_dropped(runtime_factory, clock):
    cs = generate_candleset(n=LIMIT + ANCHOR_SPAN + 400, seed=9, tf="1h", end_time=FIXED_END, symbol=SYM)
    k0 = LIMIT + ANCHOR_SPAN + 2

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            await closes(svc, rt, cs, clock, range(k0, k0 + 260, 13))
            anchors = [s for s in (await stored(rt))["snapshots"] if s["kind"] != "last"]
            newest = anchors[-1]
            assert isinstance(newest["state"]["resume"]["made"], int)
            vetoed_bar = newest["time"] - 5 * H  # recorded now, after that anchor's run read the vetoes
            await svc._record_veto(SYM, "1h", {"time": vetoed_bar, "direction": "long", "level": "red",
                                               "detail": "prueba"})
            veto_at = (await svc._vetoes_for(SYM, "1h"))[(vetoed_bar, "long")]["at"]
            await closes(svc, rt, cs, clock, [k0 + 261])
            after = [s for s in (await stored(rt))["snapshots"] if s["kind"] != "last"]
            assert len(after) >= len(anchors) - 1
            for s in after:  # every anchor at or after the vetoed bar comes from a run that applied the veto
                if s["time"] >= vetoed_bar:
                    assert s["state"]["resume"]["made"] > veto_at, s["time"]

    asyncio.run(scenario())


def test_stale_for_vetoes_rule():
    snap = {"time": 1000, "kind": "grid", "state": {"resume": {"made": 5_000}}}
    assert stale_for_vetoes(snap, [{"time": 900, "at": 6_000}])
    assert not stale_for_vetoes(snap, [{"time": 900, "at": 4_000}])  # known when the run read the vetoes
    assert not stale_for_vetoes(snap, [{"time": 1100, "at": 6_000}])  # a later bar: not part of that path
    assert not stale_for_vetoes({**snap, "kind": "last"}, [{"time": 900, "at": 6_000}])
    assert not stale_for_vetoes({"time": 1000, "kind": "grid", "state": {}}, [{"time": 900, "at": 6_000}])


def test_merge_replaces_only_anchors_without_the_current_fingerprint():
    def snap(t: int, fp: str | None, kind: str = "grid") -> dict:
        state = {"learner": {"t": t}} if fp is None else {"resume": {"fp": fp, "t": t}}
        return {"time": t * H, "state": state, "kind": kind}

    old = [snap(100, None), snap(200, "OLD"), snap(300, "NEW"), snap(340, None, "origin")]
    new = [snap(100, "NEW"), snap(200, "NEW"), snap(300, "NEW"), snap(400, "NEW"), snap(380, "NEW")]
    for item in new[:3]:
        item["state"]["resume"]["mark"] = "run2"
    kept = {s["time"] // H: s for s in merge_snapshots(old, new, H, fingerprint="NEW")}
    assert sorted(kept) == [100, 200, 300, 340, 400]  # 380 crowds the kept origin (write-once gap)
    assert kept[100]["state"]["resume"]["mark"] == "run2" and kept[200]["state"]["resume"]["mark"] == "run2"
    assert "mark" not in kept[300]["state"]["resume"]  # same fingerprint: the stored one stays
    assert kept[340]["kind"] == "origin" and snapshot_fingerprint(kept[340]) is None
    legacy = {s["time"] // H: s for s in merge_snapshots(old, new, H)}  # no fingerprint given: strictly write-once
    assert snapshot_fingerprint(legacy[100]) is None and snapshot_fingerprint(legacy[200]) == "OLD"


def test_resumed_default_window_stays_within_the_engine_budget():
    cs = generate_candleset(n=2600, seed=5, tf="1h", end_time=FIXED_END)
    p = NexusParams()
    base = run_nexus(cs.slice(0, 1500), p)
    snap = [s for s in base.snapshots if s["kind"] == "grid"][-5]
    w, a = resume_fit(snap["state"], cs.time, p)
    window = cs.slice(w, a + 1 + 1000)  # what the service runs for a default analysis
    best = []
    for _ in range(2):
        t0 = time.perf_counter()
        res = run_nexus(window, p, prior=snap["state"])
        best.append(time.perf_counter() - t0)
    assert len(res.bars["time"]) == 1000 + 60 and min(best) < 0.5, f"{min(best):.3f}s"


def test_bar_close_gate_store_keeps_the_first_decision_per_params(runtime_factory, clock):
    from app.engine.service import GATE_KIND

    def rec(t: int, fp: str, verdict: str = "TAKE") -> dict:
        return {"time": t, "direction": "long", "verdict": verdict, "score": 70.0, "reason": None,
                "summary": "TOMAR", "checks": [None] * 13, "fp": fp}

    async def scenario():
        async with runtime_factory() as rt:
            svc = NexusService(rt)
            await svc._record_gates(SYM, "1h", [rec(100 * H, "A")])
            await svc._record_gates(SYM, "1h", [rec(100 * H, "A", "WAIT")])  # the bar-close one stays
            assert (await svc._gates_for(SYM, "1h"))[(100 * H, "long")]["verdict"] == "TAKE"
            await svc._record_gates(SYM, "1h", [rec(100 * H, "B", "WAIT")])  # decided with other params
            fresh = NexusService(rt)  # persisted: a restart reads them back
            got = (await fresh._gates_for(SYM, "1h"))[(100 * H, "long")]
            assert got["verdict"] == "WAIT" and got["fp"] == "B" and isinstance(got["at"], int)
            row = await rt.db.fetchone("SELECT updates FROM model_state WHERE kind=? AND key=?",
                                       (GATE_KIND, f"{SYM}|1h"))
            assert int(row["updates"]) == 1

    asyncio.run(scenario())


def test_bar_close_rule_shared_by_vetoes_and_gate_records(clock):
    resp = {"tf": "1h", "current": {"time": 100 * H}}
    assert NexusService._decided_at_close(resp, "1h", upto=100 * H)
    clock["t"] = 100 * H + H // 2  # the bar is still forming
    assert not NexusService._decided_at_close(resp, "1h", None)
    clock["t"] = 101 * H + 60  # closed one minute ago: the live view of that close
    assert NexusService._decided_at_close(resp, "1h", None)
    clock["t"] = 101 * H + 3600  # a market sitting on an old close (weekend): that view only
    assert not NexusService._decided_at_close(resp, "1h", None)
