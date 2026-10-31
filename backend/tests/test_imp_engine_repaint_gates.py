"""engine-repaint: a bar-close decision taken with the global gate state (pooled in at the last bar only) keeps its
verdict, score, reasons and check vector once the bar is history: NexusResult.live_gates records it and a later run
replays it (`live_gates=`); without the record the replay may decide that bar differently (the residual repaint of
the newest marker that this removes)."""
from __future__ import annotations

import pytest

from app.engine.gate import GateLearner
from app.engine.nexus import NexusParams, run_nexus
from app.engine.nexus_decide import gate_digest, gate_record, replay_gate
from app.engine.nexus_resume import roundtrip
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END


def global_gate() -> dict:
    """A global learner that disagrees with what the in-run one learns on three checks."""
    g = GateLearner()
    for _ in range(300):
        g.update({"volume": 1, "momentum": 0, "trend_alignment": 1}, True)
        g.update({"volume": 0, "momentum": 1, "trend_alignment": 0}, False)
    return g.to_state()


@pytest.fixture(scope="module")
def cs():
    return generate_candleset(n=1300, seed=7, tf="1h", end_time=FIXED_END, symbol="SIM:GATES")


def at(res, t: int) -> list[dict]:
    return [m for m in res.markers if m["time"] == t]


def candidate_bars(cs, p: NexusParams, limit: int = 12) -> list[int]:
    """Bars where the engine evaluated a candidate (an entry or a rejection marker) in a run without global state."""
    full = run_nexus(cs, p)
    kinds = ("BUY", "SELL", "REJECTED_LONG", "REJECTED_SHORT")
    bars = sorted({full.bars["time"].index(m["time"]) for m in full.markers if m["kind"] in kinds})
    return [k for k in bars if 300 < k < len(cs) - 2][:limit]


def test_bar_close_gate_decisions_are_replayed_as_taken(cs):
    p, g = NexusParams(), global_gate()
    bars = candidate_bars(cs, p)
    assert len(bars) >= 6
    recorded = repainted = 0
    for k in bars:
        t_k = int(cs.time[k])
        live = run_nexus(cs.upto(k), p, gate_state=g)  # the bar-close decision of bar k
        records = roundtrip(live.live_gates)
        assert all(r["time"] == t_k for r in records)
        replay = run_nexus(cs.upto(k + 1), p, gate_state=g, live_gates=records)
        assert at(replay, t_k) == at(live, t_k), k  # the bar keeps its bar-close decision
        assert replay.bars["action"][k] == live.bars["action"][k]
        recorded += bool(records)
        repainted += at(run_nexus(cs.upto(k + 1), p, gate_state=g), t_k) != at(live, t_k)
        if not records:  # nothing pooled changed what the bar shows: its replay is already identical
            assert at(run_nexus(cs.upto(k + 1), p, gate_state=g), t_k) == at(live, t_k), k
    assert recorded >= 2 and repainted >= 1  # the global state did change some decisions, and records fixed them


def test_without_a_global_gate_state_nothing_is_recorded(cs):
    res = run_nexus(cs, NexusParams())
    assert res.live_gates == []


def test_gate_record_round_trip():
    gate = {"verdict": "WAIT", "verdict_label": "ESPERAR", "score": 61.3, "summary": "ESPERAR: algo",
            "reasons_against": ["Momento dividido", "otra"], "sl": 95.0, "tp1": 105.0,
            "checks": [{"id": "momentum", "passed": False}, {"id": "volume", "passed": True},
                       {"id": "regime", "passed": None}]}
    rec = roundtrip(gate_record(1_700_000_000, "long", gate))
    again = replay_gate({**gate, "verdict": "TAKE", "score": 70.0, "reasons_against": [], "checks": []}, rec)
    assert gate_digest(again) == gate_digest(gate)
    assert again["verdict_label"] == "ESPERAR" and again["sl"] == 95.0
    assert again["reasons_against"] == ["Momento dividido"]


def test_a_record_decided_with_other_params_is_not_replayed(cs):
    from app.engine.nexus_loop import parse_live_gates
    from app.engine.nexus_resume import path_fingerprint

    p, g = NexusParams(), global_gate()
    k, records = next((k, r.live_gates) for k in candidate_bars(cs, p)
                      if (r := run_nexus(cs.upto(k), p, gate_state=g)).live_gates)
    assert all(r["fp"] == path_fingerprint(p) for r in records)
    assert parse_live_gates(records, path_fingerprint(p)) and not parse_live_gates(records, "otro")
    other = NexusParams(take_score=70.0)
    plain = run_nexus(cs.upto(k + 1), other, gate_state=g)
    assert run_nexus(cs.upto(k + 1), other, gate_state=g, live_gates=records).markers == plain.markers
