"""Fix round (simlearn), verifier: defects left after the first pass.

- gate-label-mismatch / replay-gatelog-horizon: every gate_log row is judged over the engine's window (the
  time_stop_bars bars after the decision bar), whatever its writer stored in target_at. Rows the watcher and the
  paper account wrote before the shared convention (service_watch.gate_target_ms, the close of that window) hold
  decision + time_stop_bars bars, which with a min() cap left them one bar short.
- stale-delisted-pairs-served-live: the scanner's own freshness rule for 24/7 markets is the hub's (more than 3 bars
  behind AND closed more than an hour ago), so a quiet minute of an illiquid pair is not "Datos desactualizados".
- global-fee-low-vol: the simulator's NEXUS runs (replay of a real symbol) charge the asset class's commission.
"""
from __future__ import annotations

import asyncio
import copy
import json

import numpy as np
import pytest

from app import config as nexus_config
from app.config import DEFAULT_SETTINGS
from app.data.base import CandleSet
from app.sim import evaluator as ev
from app.sim import scanner as sc
from app.sim import simulator as simulator_mod

H = 3600
NB = 48  # nexus.time_stop_bars default
NOW = 1_760_000_400_000
NOW_S = NOW // 1000
CHECKS = {"trend_alignment": 1, "volume": 0}


class _Hub:
    def __init__(self, candles: dict[tuple[str, str], CandleSet]):
        self.candles = candles

    async def get_candles(self, symbol, tf, limit=500):
        cs = self.candles.get((symbol, tf))
        return cs.tail(limit) if cs is not None else CandleSet.empty(symbol, tf)


class _Learner:
    seen: list[tuple[dict, bool]] = []

    def __init__(self, state=None):
        self.state = dict(state or {"n": 0})

    @classmethod
    def from_state(cls, state):
        return cls(state)

    def update(self, checks, won):
        _Learner.seen.append((dict(checks), bool(won)))

    def to_state(self):
        return self.state


def _bars(symbol: str, decision: int, n: int, tp_bar: int) -> CandleSet:
    """Flat 1h bars from the decision bar on; only bar `tp_bar` after the decision reaches TP1 (102)."""
    rows = [{"time": decision + k * H, "open": 100.0, "high": 102.5 if k == tp_bar else 100.5, "low": 99.5,
             "close": 100.0, "volume": 1.0} for k in range(n)]
    return CandleSet.from_rows(rows, symbol, "1h")


def _engine_label(cs: CandleSet, horizon: int) -> tuple[int, bool] | None:
    """NexusRun.outcome for a long candidate decided at the close of bar 0 (entry 100, SL 98, TP1 102)."""
    from app.engine import nexus_loop

    class _P:
        time_stop_bars = horizon

    class _F:
        high = np.asarray(cs.high, dtype=float)
        low = np.asarray(cs.low, dtype=float)

    run = object.__new__(nexus_loop.NexusRun)
    run.f, run.p, run.n = _F(), _P(), len(cs)
    return run.outcome(0, 1, 98.0, 102.0)


def test_gate_target_counts_from_the_decision_bar():
    d = NOW_S - NOW_S % H - 100 * H
    end = d + (NB + 1) * H  # close of the 48th bar after the decision bar
    assert ev.gate_target_s(d, "1h", NB, (d + NB * H) * 1000) == end  # older watcher / paper rows
    assert ev.gate_target_s(d, "1h", NB, (d + (NB + 1) * H) * 1000) == end  # every writer now (gate_target_ms)
    assert ev.gate_target_s(d, "1h", NB, NOW + NB * H * 1000) == end  # replay logged with the wall clock
    assert ev.gate_target_s(d + 1234, "1h", NB, None) == end  # decided inside the bar (fill time): same 48 bars
    week = 1_759_708_800  # a Monday 00:00 UTC (weekly bars open on Mondays)
    assert ev.gate_target_s(week, "1w", NB, None) == week + (NB + 1) * 7 * 86400
    assert ev.gate_target_s(0, "1h", NB, 5_000_000) == 5000  # no decision time: the stored target


@pytest.mark.parametrize("source", ["watch", "paper", "scanner"])
def test_every_writer_is_judged_over_the_engine_window(runtime_factory, monkeypatch, source):
    """TP1 reached by the 48th bar after the decision is a win for the engine's in-run learner, so it must be one for
    the global learner too, whatever target_at the writer stored."""
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    _Learner.seen = []
    monkeypatch.setattr(ev, "_gate_learner_api", lambda: ("class", _Learner))
    # what each writer stored before the shared convention (the scanner's value is the convention itself)
    stored = {"watch": NB, "paper": NB, "scanner": NB + 1}[source]

    async def scenario():
        async with runtime_factory() as rt:
            decision = NOW_S - NOW_S % H - 100 * H
            on_time = _bars("BINANCE:BTCUSDT", decision, 101, tp_bar=NB)
            too_late = _bars("BINANCE:ETHUSDT", decision, 101, tp_bar=NB + 1)
            assert _engine_label(on_time, NB) == (NB, True)
            assert _engine_label(too_late, NB) == (NB, False)  # time stop: not a win
            rt.hub = _Hub({("BINANCE:BTCUSDT", "1h"): on_time, ("BINANCE:ETHUSDT", "1h"): too_late})
            for sym in ("BINANCE:BTCUSDT", "BINANCE:ETHUSDT"):
                await rt.db.execute(
                    "INSERT INTO gate_log(symbol, tf, time, direction, verdict, score, win_prob, entry, sl, tp1, tp2, "
                    "checks, taken, source, created_at, target_at) VALUES (?, '1h', ?, 'long', 'TAKE', 70, 0.6, 100, "
                    "98, 102, 104, ?, 0, ?, ?, ?)",
                    (sym, decision, json.dumps(CHECKS), source, (decision + H) * 1000, (decision + stored * H) * 1000),
                )
            out = await ev.PredictionEvaluator(rt).run_once(now=NOW)
            assert out["gate"] == 2
            got = {r["symbol"]: (r["outcome"], r["outcome_r"]) for r in
                   await rt.db.fetchall("SELECT symbol, outcome, outcome_r FROM gate_log")}
            assert got["BINANCE:BTCUSDT"] == ("TP1", pytest.approx(1.0))
            assert got["BINANCE:ETHUSDT"] == ("NONE", pytest.approx(0.0))
            assert sorted(w for _, w in _Learner.seen) == [False, True]

    asyncio.run(scenario())


def test_live_decision_is_not_resolved_before_its_window_ends(runtime_factory, monkeypatch):
    """A watcher row whose stored target (decision + 48 bars) has passed while the 48th bar is still forming waits."""
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            now_s = NOW_S - NOW_S % H + 1800  # half-way through the forming bar
            decision = now_s - now_s % H - NB * H  # the forming bar is the 48th after the decision bar
            rt.hub = _Hub({("BINANCE:BTCUSDT", "1h"): _bars("BINANCE:BTCUSDT", decision, NB + 1, tp_bar=999)})
            await rt.db.execute(
                "INSERT INTO gate_log(symbol, tf, time, direction, verdict, score, win_prob, entry, sl, tp1, tp2, "
                "checks, taken, source, created_at, target_at) VALUES ('BINANCE:BTCUSDT', '1h', ?, 'long', 'TAKE', 70, "
                "0.6, 100, 98, 102, 104, ?, 0, 'watch', ?, ?)",
                (decision, json.dumps(CHECKS), (decision + H) * 1000, (decision + NB * H) * 1000),
            )
            assert (await ev.PredictionEvaluator(rt).run_once(now=now_s * 1000))["gate"] == 0
            assert (await rt.db.fetchone("SELECT resolved FROM gate_log"))["resolved"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("tf,bars_behind,stale", [
    ("1m", 5, False), ("5m", 5, False), ("15m", 5, False),  # quiet minutes of an illiquid pair
    ("1m", 120, True), ("15m", 6, True), ("1h", 4, True), ("1h", 3, False), ("1d", 4, True),
])
def test_scanner_freshness_rule_is_the_hubs(tf, bars_behind, stale):
    from app.data import hub
    from app.util import tf_seconds

    tf_s = tf_seconds(tf)
    now = 1_800_000_000
    newest = now // tf_s * tf_s - bars_behind * tf_s
    resp = {"ok": True, "current": {"time": newest}}
    got = sc.stale_reason(resp, tf, "crypto", now)
    assert (got is not None) is stale
    assert (hub.stale_note(newest, tf, now, True) is not None) is stale
    if stale:
        assert got.startswith("Datos desactualizados: última vela hace ")


def test_simulator_nexus_charges_the_asset_class_fee(monkeypatch):
    seen: list[float] = []

    class _Params:
        @classmethod
        def from_settings(cls, settings, **kw):
            seen.append(float(settings["risk.fee_pct"]))
            return cls()

    monkeypatch.setattr(simulator_mod, "load_engine", lambda: (None, _Params))
    monkeypatch.setattr(simulator_mod, "run_engine", lambda cs, params, prior=None, gate_state=None: ({}, None))
    monkeypatch.setattr(simulator_mod, "current_settings", lambda: copy.deepcopy(DEFAULT_SETTINGS))
    rows = [{"time": 1_700_000_000 + i * H, "open": 1.08, "high": 1.0805, "low": 1.0795, "close": 1.08,
             "volume": 1.0} for i in range(5)]
    simulator_mod._default_nexus(CandleSet.from_rows(rows, "FX:EURUSD", "1h"), None)  # replay of a real symbol
    simulator_mod._default_nexus(CandleSet.from_rows(rows, "SIM:NEXUS", "1h"), None)  # synthetic market
    assert seen == [pytest.approx(0.005), pytest.approx(0.1)]
