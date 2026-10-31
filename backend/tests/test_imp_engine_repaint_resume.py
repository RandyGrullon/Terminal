"""engine-repaint: a NEXUS run resumed from an anchor snapshot continues the stored path exactly (position and
levels, pending entry / exit, cooldowns, episode flags, the label queue, unresolved gate candidates, loss streak,
regime / indicator states), snapshots are plain JSON that only depend on bars up to their own, and a resumed run
keeps the zero-lookahead prefix property."""
from __future__ import annotations

import copy

import pytest

from app.engine import nexus_loop
from app.engine.features import WARMUP_BARS
from app.engine.nexus import NexusParams, run_nexus
from app.engine.nexus_resume import path_fingerprint, resume_fit, roundtrip
from app.engine.positions import Position
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

PER_BAR = ("time", "score", "prob_up", "action", "regime", "ema_fast", "ema_slow", "ema_trend", "pos_entry",
           "pos_sl", "pos_tp1", "pos_tp2", "pos_tp3")
P = NexusParams(horizon=8)  # a label queue of several bars


@pytest.fixture(scope="module")
def cs():
    return generate_candleset(n=1900, seed=7, tf="1h", end_time=FIXED_END, symbol="SIM:RESUME")


@pytest.fixture(scope="module")
def rich():
    """A series whose path has every kind of state at some bar (rejections, overlapping candidates, trailing)."""
    return generate_candleset(n=1600, seed=11, tf="1h", end_time=FIXED_END, symbol="SIM:RESUME")


def view(res, lo: int, hi: int) -> tuple:
    """What a run shows for the bars after `lo` and before `hi` (the last bar of the shorter run is "now" there)."""
    b = res.bars
    idx = [i for i, t in enumerate(b["time"]) if lo < t < hi]
    return ({k: [b[k][i] for i in idx] for k in PER_BAR}, [m for m in res.markers if lo < m["time"] < hi],
            [t for t in res.trades if t["exit_time"] is not None and lo < t["exit_time"] < hi])


def resume(cs, snap: dict, end: int, params: NexusParams = P):
    state = roundtrip(snap["state"])  # what the database gives back
    fit = resume_fit(state, cs.time, params)
    assert fit is not None, snap["time"]
    w, a = fit
    res = run_nexus(cs.slice(w, end), params, prior=state)
    return res, w, a


def test_every_kind_of_anchor_state_resumes_exactly(rich, monkeypatch):
    """Anchors on every bar: the ones with a pending entry / exit, an open position past TP1 or trailing, carried
    candidates, an active cooldown or episode, a loss streak... each resume equals the stored path."""
    cs = rich
    monkeypatch.setattr(nexus_loop, "SNAPSHOT_EVERY", 1)
    base = run_nexus(cs.slice(0, 1400), P)
    t_end = base.bars["time"][-1]
    blocks = {s["time"]: s for s in base.snapshots if s["kind"] == "grid"}
    assert len(blocks) > 1200 and all(roundtrip(s) == s for s in base.snapshots + [base.origin])
    wanted = {
        "pending_entry": lambda b: b["pending"] is not None,
        "pending_exit": lambda b: b["exit"] is not None,
        "after_tp1": lambda b: b["pos"] is not None and b["pos"]["tp_hits"] >= 1,
        "trailing": lambda b: b["pos"] is not None and b["pos"]["trail"] is not None,
        "candidates": lambda b: len(b["cands"]) >= 2,
        "cooldown": lambda b: b["exit_at"][0] is not None and b["exit_at"][0] < 3,
        "episode": lambda b: any(b["episode"]),
        "loss_streak": lambda b: b["loss_streak"] >= 1,
        "cold_features": lambda b: b["feat"]["rec"] is None,
        "warm_features": lambda b: b["feat"]["rec"] is not None,
    }
    picked: dict[str, int] = {}
    for t in sorted(blocks):
        b = blocks[t]["state"]["resume"]
        for name, test in wanted.items():
            if name not in picked and t < t_end - 80 * 3600 and test(b):
                picked[name] = t
    assert set(picked) == set(wanted), sorted(set(wanted) - set(picked))
    for name, t in sorted(picked.items(), key=lambda kv: kv[1]):
        res, w, a = resume(cs, blocks[t], 1550)
        assert view(res, t, t_end) == view(base, t, t_end), name
        assert res.bars["time"][0] == int(cs.time[a + 1 - WARMUP_BARS]) or a + 1 < WARMUP_BARS


def test_a_chain_of_resumes_is_one_uninterrupted_run(cs):
    single = run_nexus(cs.slice(0, 1800), P)
    first = run_nexus(cs.slice(0, 900), P)
    g1 = [s for s in first.snapshots if s["kind"] == "grid"][-2]
    second, _, _ = resume(cs, g1, 1350)
    g2 = [s for s in second.snapshots if s["kind"] == "grid"][-3]
    assert g2["state"]["resume"]["feat"]["rec"] is not None  # far enough from the cold start: warm restore point
    third, _, a = resume(cs, g2, 1800)
    t_end = single.bars["time"][-1] + 1
    assert view(third, g2["time"], t_end) == view(single, g2["time"], t_end)
    assert third.current == single.current and third.prediction["prob_up"] == single.prediction["prob_up"]
    # memory: the learner continued, every processed bar counted once
    assert third.learning["memory_bars"] == g2["state"]["learner"]["memory_bars"] + 1800 - 1 - a


def test_snapshots_only_depend_on_bars_up_to_their_own(cs):
    """Zero lookahead in what is stored: a run that saw 400 more bars stores the same anchor states."""
    short = run_nexus(cs.slice(0, 1100), P)
    long = run_nexus(cs.slice(0, 1500), P)
    grid_short = {s["time"]: s for s in short.snapshots if s["kind"] == "grid"}
    grid_long = {s["time"]: s for s in long.snapshots if s["kind"] == "grid"}
    assert len(grid_short) >= 9 and set(grid_short) <= set(grid_long)
    for t, snap in grid_short.items():
        assert snap == grid_long[t], t
    assert short.origin == long.origin


def test_a_resumed_run_has_no_lookahead(cs):
    base = run_nexus(cs.slice(0, 1200), P)
    snap = [s for s in base.snapshots if s["kind"] == "grid"][-3]
    full, w, a = resume(cs, snap, 1700)
    for k in (a + 1, a + 37, a + 260, 1650):
        pre = run_nexus(cs.slice(w, k + 1), P, prior=roundtrip(snap["state"]))
        t_k = int(cs.time[k])
        n = pre.bars["time"].index(t_k)
        assert full.bars["time"][: n + 1] == pre.bars["time"]
        for key in PER_BAR:
            assert pre.bars[key][:n] == full.bars[key][:n], (k, key)
        for key in ("score", "prob_up", "regime", "action"):
            assert pre.bars[key][n] == full.bars[key][n], (k, key)
        assert [m for m in pre.markers if m["time"] <= t_k] == [m for m in full.markers if m["time"] <= t_k]


def test_a_prior_that_cannot_resume_is_a_plain_learner_prior(cs):
    base = run_nexus(cs.slice(0, 1200), P)
    snap = [s for s in base.snapshots if s["kind"] == "grid"][-2]
    state = roundtrip(snap["state"])
    a = int(list(cs.time).index(snap["time"]))
    legacy = {k: v for k, v in state.items() if k != "resume"}
    window = cs.slice(a + 1 - WARMUP_BARS, 1600)
    plain = run_nexus(window, P, prior=legacy)
    assert plain.bars["time"][0] == int(cs.time[a + 1 - WARMUP_BARS]) and plain.bars["score"][WARMUP_BARS] is not None
    # another engine version / params, or a window that does not start where the features restart: no resume
    other = copy.deepcopy(state)
    other["resume"]["v"] = -1
    assert resume_fit(other, cs.time, P) is None
    assert resume_fit(state, cs.time, NexusParams(horizon=8, entry_threshold=0.4)) is None
    assert path_fingerprint(P) != path_fingerprint(NexusParams(horizon=8, fee_pct=0.2))
    assert resume_fit(state, cs.time[: a + 1], P) is None  # nothing after the anchor
    for prior in (other, state):
        res = run_nexus(window, P, prior=prior)  # the window starts at the display bar, not the restore bar
        assert res.bars == plain.bars and res.markers == plain.markers and res.trades == plain.trades


def test_position_state_round_trip():
    from app.engine.levels import Levels

    pos = Position(7, Levels(1, 100.0, 95.0, 105.0, 110.0, 115.0), 40, 1_700_000_000, 100.5, 71.2, 0, 39)
    pos.process_bar(101.0, 106.0, 100.0)
    pos.update_trail(2.0, 3.0)
    pos.bars = 9
    again = Position.from_state(roundtrip(pos.to_state(50)), 60)
    assert again.entry_idx == 50 and again.decision_idx == 49
    for slot in Position.__slots__:
        if slot not in ("entry_idx", "decision_idx"):
            assert getattr(again, slot) == getattr(pos, slot), slot
    assert again.to_trade(None, None, "OPEN", None, 104.0, 0.1) == pos.to_trade(None, None, "OPEN", None, 104.0, 0.1)
