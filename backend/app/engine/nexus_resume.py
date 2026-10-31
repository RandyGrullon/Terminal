"""Exact resume of a NEXUS run from an anchor snapshot (mixin of NexusRun).

An anchor snapshot (kind grid / origin) holds, besides the learner, gate learner and edge history, a `resume` block
with everything the run needs to continue after its bar exactly as the run that stored it did:

- `feat`: where the features restart: the bar of a stored state of every recursive filter (features.feature_state,
  RESUME_LOOKBACK bars or more before the next bar, so every finite window of a processed bar is recomputed from
  the same values), or `rec: null` = cold at the bar where the chain itself started cold;
- the swing levels, the regime hysteresis and the decaying expert memories in effect after the anchor bar;
- the label queue (inputs of the last `horizon` bars, whose labels are learned after the anchor), the previous
  score (signal exit), the open position and its levels, the pending entry / exit, cooldowns and re-evaluation
  clocks, episode flags, the loss streak and closed-trade count (circuit breaker), the trade / candidate counters and
  the candidates whose gate outcome is still unknown (their outcome is recomputed from the resumed run's bars).

A run that writes a snapshot continues from that same state: it is built from plain JSON values only (what the
database gives back), the learners are reloaded from their rounded stored state and the engine state is rebuilt
from the block, exactly what a later run loads, so resuming and continuing are the same computation. Nothing in the
block depends on bars after its own (candidate outcomes are recomputed, never stored). The block is tied to
RESUME_VERSION and to the path params (fingerprint); any other combination resumes nothing (the anchor's learner
is then a plain prior, as before).
"""
from __future__ import annotations

import hashlib
import heapq
import json
from typing import Any

import numpy as np

from app.engine.features import RESUME_LOOKBACK, restore_point
from app.engine.gate import GateLearner
from app.engine.gate_checks import CHECK_IDS
from app.engine.learner import NexusLearner, feature_matrix
from app.engine.levels import Levels
from app.engine.positions import Position
from app.util import clean_json

# bump whenever a change makes a run compute anything else from the same bars and state (features, experts,
# learner, gate, decisions): stored resume blocks of another version are then ignored
RESUME_VERSION = 1
# engine params that shape the path (the commission included: it moves the gate's R:R / EV)
PATH_PARAMS = ("entry_threshold", "exit_threshold", "horizon", "sl_atr", "tp_r", "trail_atr", "learning_rate",
               "allow_short", "time_stop_bars", "gate_enabled", "take_score", "wait_score", "min_rr", "min_prob",
               "fee_pct")
NEVER = -10**9


def path_fingerprint(params: Any) -> str:
    """Short hash of RESUME_VERSION + the path params: a stored path only resumes runs with the same ones."""
    d = params.to_dict()
    blob = json.dumps({"v": RESUME_VERSION, **{k: d.get(k) for k in PATH_PARAMS}}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def dumps(obj: Any) -> str:
    """Compact JSON text of `obj` as stored in model_state: what json.dumps(clean_json(obj)) gives, without walking
    every value when `obj` is already plain JSON (numpy integers or NaN / inf take the clean_json path)."""
    try:
        return json.dumps(obj, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return json.dumps(clean_json(obj), separators=(",", ":"))


def roundtrip(obj: Any) -> Any:
    """What the database gives back for `obj` (clean_json + JSON text)."""
    return json.loads(dumps(obj))


def _find(times: np.ndarray, t: int) -> int | None:
    k = int(np.searchsorted(times, t))
    return k if k < len(times) and int(times[k]) == int(t) else None


def resume_block(state: Any) -> dict | None:
    """The resume block of a snapshot state (or of a snapshot item {time, state, kind}); None without one."""
    if not isinstance(state, dict):
        return None
    block = state.get("resume")
    if block is None and isinstance(state.get("state"), dict):
        block = state["state"].get("resume")
    return block if isinstance(block, dict) else None


def resume_fit(state: Any, times: Any, params: Any) -> tuple[int, int] | None:
    """(index of the bar the features restart from, index of the anchor bar) in `times` when the anchor snapshot
    `state` resumes exactly with these params; None when it cannot (other engine version or params, a bar missing
    from these bars, nothing after the anchor)."""
    block = resume_block(state)
    if block is None or block.get("v") != RESUME_VERSION or block.get("fp") != path_fingerprint(params):
        return None
    times = np.asarray(times, dtype=np.int64)
    try:
        t_anchor, feat = int(block["time"]), block["feat"]
        t_feat = int(feat["time"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(feat.get("rec"), (dict, type(None))):
        return None
    a, w = _find(times, t_anchor), _find(times, t_feat)
    if a is None or w is None or w > a or a >= len(times) - 1:
        return None
    if feat.get("rec") is not None and a + 1 - w < RESUME_LOOKBACK:
        return None
    return w, a


class ResumeMixin:
    """Requires the NexusRun attributes (f, p, h, start, learner, gate_learner, edge, pos, heap, ...)."""

    f: Any
    p: Any

    # ------------------------------------------------------------------ writing

    def _label_inputs(self, j: int) -> tuple | None:
        """(learner inputs x, expert signals, regime, published p, raw p, ATR rank) of bar j, for the label
        learned at bar j + horizon; None when bar j has nothing to learn."""
        if j >= self.start:
            if j >= self.f.n:
                return None
            r, p = int(self.regime[j]), float(self.prob[j])
            if r < 0 or p != p:
                return None
            return self.X[j], self.S[j], r, p, float(self.raw_prob[j]), float(self.f.atr_rank[j])
        return self.queue.get(j)

    def resume_state(self, i: int) -> dict | None:
        """The resume block after bar i (None when this run cannot tell where the features would restart)."""
        f = self.f
        rp = restore_point(f, i)
        if rp is not None:
            feat = {"time": int(f.time[rp[0]]), "rec": rp[1]}
        elif self.cold_features:
            feat = {"time": int(f.time[0]), "rec": None}  # the chain started cold at this window's first bar
        else:
            return None
        labels = []
        for j in range(i - self.h + 1, i + 1):
            row = self._label_inputs(j)
            if row is not None:
                _, s, r, p, raw, rank = row
                labels.append([int(f.time[j]), [float(v) for v in s], r, rank if rank == rank else None, p, raw])

        def off(k: int) -> int | None:
            return None if k <= NEVER // 2 else int(i - k)

        pend = None
        if self.pending_entry is not None:
            side, lv, gate, d = self.pending_entry
            pend = {"side": int(side), "lv": [lv.entry, lv.sl, lv.tp1, lv.tp2, lv.tp3],
                    "score": (gate or {}).get("score"), "off": int(i - d)}
        cands = [[int(i - c["idx"]), int(c["side"]), float(c["sl"]), float(c["tp1"]), int(c["regime"]),
                  [c["checks"].get(cid) for cid in CHECK_IDS], int(c["seq"])]
                 for c in sorted([e[2] for e in self.heap] + self.unknown, key=lambda c: c["seq"])]
        score = float(self.score[i]) if i >= self.start else None
        return {
            "v": RESUME_VERSION, "fp": self.fp, "time": int(f.time[i]), "feat": feat,
            "swing": [f.value("swing_low", i, None), f.value("swing_high", i, None)],
            "regime": [int(self.regime[i]), int(self.reg_pending[i]), int(self.reg_count[i])],
            "decay": [float(v) for v in self.decay[i]],
            "labels": labels, "score": score if score == score else None,
            "pos": None if self.pos is None else self.pos.to_state(i), "pending": pend,
            "exit": None if self.pending_exit is None else list(self.pending_exit),
            "cond": [bool(self.prev_cond[1]), bool(self.prev_cond[-1])],
            "eval": [off(self.last_eval[1]), off(self.last_eval[-1])],
            "exit_at": [off(self.last_exit[1]), off(self.last_exit[-1])],
            "episode": [self.rejected_in_episode[1] is not None, self.rejected_in_episode[-1] is not None],
            "loss_streak": int(self.loss_streak), "closed": int(self.closed_count),
            "trade_seq": int(self.trade_seq), "seq": int(self.seq), "cands": cands,
        }

    def anchor_snapshot(self, i: int, kind: str) -> dict:
        """Snapshot after bar i (state + resume block, as the database will hold it); the run then continues from
        that same state, exactly like a later run resumed from it. The state is built from plain JSON values only
        (lists, finite floats, ints, None), so it is already what the database gives back (`roundtrip(state) ==
        state`, checked by the tests) and the learners only need their own (rounded) state reloaded."""
        state = self.state_dict(full=True)
        block = self.resume_state(i)
        if block is not None:
            state["resume"] = block
        self.learner = NexusLearner.from_state(state.get("learner"), self.p.learning_rate)
        self.gate_learner = GateLearner.from_state(state.get("gate"))
        self.decision_gate = self.gate_learner
        if state.get("resume") is not None:
            self.restore(state["resume"], i, fresh=False)
        return {"time": int(self.f.time[i]), "state": state, "kind": kind}

    # ------------------------------------------------------------------ reading

    def load_queue(self, block: dict) -> None:
        """Label inputs of the bars before the first processed one (learned during the first `horizon` bars)."""
        times = self.f.time
        for t, s, r, rank, p, raw in block.get("labels") or []:
            j = _find(times, int(t))
            if j is None or j >= self.start:
                continue
            sig = np.asarray(s, dtype=float)
            ar = np.asarray([np.nan if rank is None else float(rank)])
            x = feature_matrix(sig[None, :], np.asarray([int(r)], dtype=np.int64), ar)[0]
            self.queue[j] = (x, sig, int(r), float(p), float(raw), np.nan if rank is None else float(rank))

    def restore(self, b: dict, a: int, fresh: bool) -> None:
        """Engine state after bar a from a resume block. `fresh` = a new run (else the run that just wrote it keeps
        its own episode records, which only feed its window statistics)."""

        def idx(k: Any) -> int:
            return NEVER if k is None else int(a) - int(k)

        score = b.get("score")
        self.score_prev = float(score) if isinstance(score, (int, float)) else None
        self.pos = Position.from_state(b["pos"], a) if b.get("pos") else None
        pe = b.get("pending")
        if pe:
            side = 1 if int(pe["side"]) > 0 else -1
            lv = Levels(side, *[float(x) for x in pe["lv"]])
            self.pending_entry = (side, lv, {"score": pe.get("score")}, int(a) - int(pe["off"]))
        else:
            self.pending_entry = None
        self.pending_exit = (str(b["exit"][0]), str(b["exit"][1])) if b.get("exit") else None
        self.prev_cond = {1: bool(b["cond"][0]), -1: bool(b["cond"][1])}
        self.last_eval = {1: idx(b["eval"][0]), -1: idx(b["eval"][1])}
        self.last_exit = {1: idx(b["exit_at"][0]), -1: idx(b["exit_at"][1])}
        for side, flag in ((1, b["episode"][0]), (-1, b["episode"][1])):
            if not flag:
                self.rejected_in_episode[side] = None
            elif fresh or self.rejected_in_episode[side] is None:
                # rejected before this window: it blocks a second marker of the same episode, outside the stats
                self.rejected_in_episode[side] = {"idx": NEVER, "side": side, "won": None}
        self.loss_streak = int(b["loss_streak"])
        self.closed_count = int(b["closed"])
        self.trade_seq = int(b["trade_seq"])
        self.seq = int(b["seq"])
        self.heap, self.unknown = [], []
        for off, side, sl, tp1, regime, checks, seq in b.get("cands") or []:
            d = int(a) - int(off)
            cand = {"idx": d, "side": int(side), "regime": int(regime), "checks": dict(zip(CHECK_IDS, checks)),
                    "sl": float(sl), "tp1": float(tp1), "seq": int(seq), "won": None, "resolve_idx": None}
            # no stop or target was touched before bar a (else it would be resolved): scan from bar a on
            res = self.outcome_from(d, int(side), float(sl), float(tp1), int(a))
            if res is None:
                self.unknown.append(cand)
            else:
                cand["resolve_idx"], cand["won"] = res
                heapq.heappush(self.heap, (res[0], int(seq), cand))
