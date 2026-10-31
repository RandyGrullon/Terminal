"""Fix round (simlearn): gate_log resolution and the global gate learner / learning summary.

- replay-gatelog-horizon: the outcome horizon counts from the decision bar (whatever clock wrote target_at),
  simulated decisions (source 'sim', SIM:* symbols) never train the learner and one decision logged by several
  sources is learned once.
- gate-label-mismatch: one definition everywhere: won = TP1 before the SL within the time stop (NONE = not won).
- overclaimed-filter-proof: the summary exposes how many decisions are judged (taken_judged / avoided_judged).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config as nexus_config
from app.data.base import CandleSet
from app.sim import evaluator as ev

H = 3600
NOW = 1_760_000_400_000
NOW_S = NOW // 1000
CHECKS = {"trend_alignment": 1, "volume": 0}


class _Hub:
    def __init__(self, candles: dict[tuple[str, str], CandleSet]):
        self.candles = candles

    async def get_candles(self, symbol, tf, limit=500):
        cs = self.candles.get((symbol, tf))
        return cs.tail(limit) if cs is not None else CandleSet.empty(symbol, tf)


def _bars(symbol: str, t0: int, n: int, *, highs: dict[int, float] | None = None,
          lows: dict[int, float] | None = None, close: float = 100.0) -> CandleSet:
    rows = [{"time": t0 + i * H, "open": close, "high": (highs or {}).get(i, close + 0.5),
             "low": (lows or {}).get(i, close - 0.5), "close": close, "volume": 1.0} for i in range(n)]
    return CandleSet.from_rows(rows, symbol, "1h")


async def _gate(db, *, symbol="BINANCE:BTCUSDT", time_s, direction="long", verdict="TAKE", source="watch",
                target_ms=None, entry=100.0, sl=98.0, tp1=102.0, resolved=0, outcome=None, outcome_r=None):
    await db.execute(
        "INSERT INTO gate_log(symbol, tf, time, direction, verdict, score, win_prob, entry, sl, tp1, tp2, checks, "
        "taken, source, created_at, target_at, resolved, outcome, outcome_r) "
        "VALUES (?, '1h', ?, ?, ?, 70, 0.6, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
        (symbol, time_s, direction, verdict, entry, sl, tp1, tp1 + 2, json.dumps(CHECKS), source, NOW,
         target_ms if target_ms is not None else (time_s + 49 * H) * 1000, resolved, outcome, outcome_r),
    )


class _Learner:
    """Engine-style GateLearner that records the samples it was given."""

    seen: list[tuple[dict, bool]] = []

    def __init__(self, state=None):
        self.state = dict(state or {"n": 0})

    @classmethod
    def from_state(cls, state):
        return cls(state)

    def update(self, checks, won):
        _Learner.seen.append((dict(checks), bool(won)))
        self.state["n"] += 1

    def to_state(self):
        return self.state


@pytest.fixture
def learner(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    _Learner.seen = []
    monkeypatch.setattr(ev, "_gate_learner_api", lambda: ("class", _Learner))
    return _Learner


def test_gate_outcome_unknown_when_the_feed_never_reaches_the_horizon():
    t0, target = 1000 * H, 1000 * H + 10 * H
    bars = [(t0 + (i + 1) * H, 101.0, 99.0, 100.6) for i in range(3)]  # the feed stopped after 3 bars
    assert ev.gate_outcome("long", 100, 98, 102, bars, t0, target, target + H, H) is None  # waiting for data
    late = target + ev.GATE_STALE_GRACE_S + H
    assert ev.gate_outcome("long", 100, 98, 102, bars, t0, target, late, H) == ("NONE", None)  # unknown, not a loss


def test_replayed_decision_is_judged_within_its_own_horizon(runtime_factory, learner):
    """A replay decision 60 days old logged with a wall-clock target (now + 48 bars) must not be labelled by a TP1
    touched weeks after its time stop."""

    async def scenario():
        async with runtime_factory() as rt:
            decision = NOW_S - NOW_S % H - 60 * 24 * H
            n = (NOW_S - decision) // H + 1
            cs = _bars("BINANCE:BTCUSDT", decision, n, highs={30 * 24: 103.0})  # TP1 only 30 days later
            rt.hub = _Hub({("BINANCE:BTCUSDT", "1h"): cs})
            await _gate(rt.db, time_s=decision, source="paper", target_ms=NOW + 48 * H * 1000)
            out = await ev.PredictionEvaluator(rt).run_once(now=NOW)
            assert out["gate"] == 1  # resolved now: its own horizon ended long ago
            row = await rt.db.fetchone("SELECT outcome, outcome_r FROM gate_log")
            assert row["outcome"] == "NONE" and row["outcome_r"] == pytest.approx(0.0)  # time stop, not TP1
            assert learner.seen == [(CHECKS, False)]

    asyncio.run(scenario())


def test_decision_older_than_the_fetched_data_is_unknown(runtime_factory, learner):
    """The candles start 10 bars after the decision: the walk must not begin at the first bar it happens to have."""

    async def scenario():
        async with runtime_factory() as rt:
            decision = NOW_S - NOW_S % H - 6 * 24 * H
            cs = _bars("BINANCE:ETHUSDT", decision + 10 * H, (NOW_S - decision) // H - 9, highs={2: 103.0})
            rt.hub = _Hub({("BINANCE:ETHUSDT", "1h"): cs})
            await _gate(rt.db, symbol="BINANCE:ETHUSDT", time_s=decision)
            out = await ev.PredictionEvaluator(rt).run_once(now=NOW)
            assert out["gate"] == 1
            row = await rt.db.fetchone("SELECT resolved, outcome, outcome_r FROM gate_log")
            assert row["resolved"] == 1 and row["outcome"] == "NONE" and row["outcome_r"] is None
            assert learner.seen == []

    asyncio.run(scenario())


def test_simulated_decisions_never_train_the_learner(runtime_factory, learner):
    async def scenario():
        async with runtime_factory() as rt:
            decision = NOW_S - NOW_S % H - 100 * H
            candles = {(s, "1h"): _bars(s, decision, 101, highs={3: 103.0})
                       for s in ("BINANCE:BTCUSDT", "BINANCE:SOLUSDT", "SIM:NEXUS")}
            rt.hub = _Hub(candles)
            await _gate(rt.db, symbol="BINANCE:SOLUSDT", time_s=decision, source="sim")  # replay on a real symbol
            await _gate(rt.db, symbol="SIM:NEXUS", time_s=decision, source="watch")  # synthetic market
            await _gate(rt.db, symbol="BINANCE:BTCUSDT", time_s=decision, source="watch")  # live decision
            out = await ev.PredictionEvaluator(rt).run_once(now=NOW)
            assert out["gate"] == 3  # all resolved (and shown)...
            assert learner.seen == [(CHECKS, True)]  # ...but only the live one is learned

    asyncio.run(scenario())


def test_one_decision_logged_by_several_sources_is_learned_once(runtime_factory, learner):
    async def scenario():
        async with runtime_factory() as rt:
            decision = NOW_S - NOW_S % H - 100 * H
            rt.hub = _Hub({("BINANCE:BTCUSDT", "1h"): _bars("BINANCE:BTCUSDT", decision, 101, lows={2: 97.0})})
            await _gate(rt.db, time_s=decision, source="watch")
            await _gate(rt.db, time_s=decision, source="paper", entry=100.1)
            evaluator = ev.PredictionEvaluator(rt)
            assert (await evaluator.run_once(now=NOW))["gate"] == 2
            assert learner.seen == [(CHECKS, False)]
            # the scanner logs the same decision later: resolved in another cycle, still not learned twice
            await _gate(rt.db, time_s=decision, source="scanner")
            evaluator._gate_checked.clear()
            assert (await evaluator.run_once(now=NOW))["gate"] == 1
            assert learner.seen == [(CHECKS, False)]
            # a different decision (the other direction) is a new sample
            await _gate(rt.db, time_s=decision, source="scanner", direction="short", sl=102.0, tp1=98.0)
            evaluator._gate_checked.clear()
            await evaluator.run_once(now=NOW)
            assert learner.seen == [(CHECKS, False), (CHECKS, True)]

    asyncio.run(scenario())


def test_learning_summary_uses_the_engine_target_and_judged_counts(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            t = NOW_S - 300 * H
            # TAKE: TP1, time stop with a positive close (not a win), SL
            await _gate(db, time_s=t, verdict="TAKE", resolved=1, outcome="TP1", outcome_r=1.0)
            await _gate(db, time_s=t + H, verdict="TAKE", resolved=1, outcome="NONE", outcome_r=0.4)
            await _gate(db, time_s=t + 2 * H, verdict="TAKE", resolved=1, outcome="SL", outcome_r=-1.0)
            # the same TP1 decision also logged by the paper account and a simulator replay: counted once
            await _gate(db, time_s=t, verdict="TAKE", source="paper", resolved=1, outcome="TP1", outcome_r=1.0)
            await _gate(db, time_s=t + 3 * H, verdict="TAKE", source="sim", resolved=1, outcome="TP1", outcome_r=1.0)
            await _gate(db, symbol="SIM:NEXUS", time_s=t, verdict="TAKE", resolved=1, outcome="TP1", outcome_r=1.0)
            # AVOID: SL, time stop (would not have reached TP1), TP1, unknown, pending
            await _gate(db, time_s=t + 10 * H, verdict="AVOID", resolved=1, outcome="SL", outcome_r=-1.0)
            await _gate(db, time_s=t + 11 * H, verdict="AVOID", resolved=1, outcome="NONE", outcome_r=0.3)
            await _gate(db, time_s=t + 12 * H, verdict="AVOID", resolved=1, outcome="TP1", outcome_r=1.0)
            await _gate(db, time_s=t + 13 * H, verdict="AVOID", resolved=1, outcome="NONE", outcome_r=None)
            await _gate(db, time_s=t + 14 * H, verdict="AVOID")
            await _gate(db, time_s=t + 15 * H, verdict="WAIT", resolved=1, outcome="SL", outcome_r=-1.0)
            g = (await ev.learning_summary(db, rt.settings.all()))["gate"]
            assert g["decisions"] == 9 and g["taken"] == 3 and g["avoided"] == 5
            assert g["taken_judged"] == 3 and g["taken_win_rate"] == pytest.approx(1 / 3)
            assert g["avoided_judged"] == 3 and g["avoided_would_lose_rate"] == pytest.approx(2 / 3)

    asyncio.run(scenario())
