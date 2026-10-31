"""Sequential, causal state machine of the NEXUS engine: learner feedback + Trade Gate + positions.

One pass over the bars after the vectorized precomputation. At bar i the engine (1) learns the label of bar
i − horizon (revealed by close[i]), (2) predicts bar i, (3) trains the gate learner with candidates whose outcome is
already known, (4) executes fills decided at the previous close (at open[i]), (5) handles intrabar SL/TP (SL first)
and (6) takes the decisions of the close of bar i.

Snapshots (`NexusResult.snapshots`) hold the state AFTER processing their bar: {"time", "state", "kind"}. kind
"grid" = the first processed bar of each absolute time bucket of SNAPSHOT_EVERY bars (the same bars whatever the
window start, so runs over different windows snapshot the same times); "last" = the last bar when it is not one.
A run that continues from a snapshot must process first the bar right after it (see NexusService).
"""
from __future__ import annotations

import heapq
from typing import Any

import numpy as np

from app.engine.experts import expert_signals
from app.engine.features import compute_features, sr_levels
from app.engine.fmt import EXIT_LABEL
from app.engine.gate import GateLearner
from app.engine.gate_checks import GateContext
from app.engine.learner import NexusLearner, feature_matrix
from app.engine.levels import Levels
from app.engine.nexus import NexusParams, NexusResult
from app.engine.nexus_decide import DecisionsMixin
from app.engine.positions import Position
from app.engine.regime import REGIMES, detect_regime, regime_name

SNAPSHOT_EVERY = 100  # bars per snapshot bucket (absolute time grid: SNAPSHOT_EVERY · tf seconds)
EDGE_WINDOW = 40
EDGE_KEEP = 200
EDGE_IN_STATE = 60
MIN_RUN_BARS = 70
STATE_VERSION = 1


def parse_prior(prior: Any) -> dict:
    """Accept a snapshot state ({learner, gate, edge}), a snapshot item ({time, state}) or a bare learner state."""
    if not isinstance(prior, dict):
        return {}
    if isinstance(prior.get("learner"), dict) or isinstance(prior.get("gate"), dict):
        return prior
    if isinstance(prior.get("state"), dict):
        return parse_prior(prior["state"])
    if "hedge" in prior:
        return {"learner": prior}
    return {}


def parse_vetoes(raw: Any) -> dict[tuple[int, int], dict]:
    """vetoed_entries (list of {time, direction, level, detail}) -> {(bar time, side): {level, detail}}."""
    out: dict[tuple[int, int], dict] = {}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        side = {"long": 1, "short": -1}.get(str(item.get("direction") or "").lower())
        try:
            t = int(item.get("time"))
        except (TypeError, ValueError):
            continue
        if side is not None:
            out[(t, side)] = {"level": "red" if item.get("level") == "red" else "amber",
                              "detail": str(item.get("detail") or "")}
    return out


def _edge_list(raw: Any) -> list[list[int]]:
    out: list[list[int]] = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                r, w = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
            if -1 <= r < len(REGIMES):
                out.append([r, 1 if w else 0])
    return out[-EDGE_KEEP:]


class NexusRun(DecisionsMixin):
    def __init__(self, cs: Any, params: NexusParams, prior: Any, gate_state: Any, vetoed: Any = None):
        self.cs = cs
        self.p = params
        self.vetoes = parse_vetoes(vetoed)  # entries the semáforo vetoed live: rejected instead of opened
        self.f = f = compute_features(cs)
        self.n = n = f.n
        self.h = int(params.horizon)
        self.regime = detect_regime(f) if n else np.zeros(0, dtype=np.int64)
        self.S = expert_signals(f, self.regime)
        self.X = feature_matrix(self.S, self.regime, f.atr_rank)
        state = parse_prior(prior)
        self.learner = NexusLearner.from_state(state.get("learner"), params.learning_rate)
        # The gate learner of the historical bars comes only from the (causal) prior chain and is trained inside
        # the run; the global live gate state (trained by the evaluator with outcomes up to now) is pooled in only
        # for the decisions of the LAST bar, so historical markers/stats never use knowledge from their future.
        self.gate_learner = GateLearner.from_state(state.get("gate"))
        use_global = isinstance(gate_state, dict) and bool(gate_state)
        self.global_gate = GateLearner.from_state(gate_state) if use_global else None
        self.decision_gate = self.gate_learner
        edge = state.get("edge") if isinstance(state.get("edge"), dict) else {}
        self.edge: dict[int, list[list[int]]] = {1: _edge_list(edge.get("long")), -1: _edge_list(edge.get("short"))}
        self.prev_trained = self.learner.trained_until

        self.score = np.full(n, np.nan)
        self.prob = np.full(n, np.nan)
        self.raw_prob = np.full(n, np.nan)
        self.conf = np.full(n, np.nan)
        # strategy return of each bar after the warm-up (fraction of a 1x position, commissions included; 0 when
        # flat); NaN only during the warm-up
        self.bar_ret = np.full(n, np.nan)
        self.action: list[str] = ["WAIT"] * n
        self.lv_entry: list[float | None] = [None] * n
        self.lv_sl: list[float | None] = [None] * n
        self.lv_tp: list[list[float | None]] = [[None] * n, [None] * n, [None] * n]

        self.pos: Position | None = None
        self.pending_entry: tuple[int, Levels, dict | None, int] | None = None
        self.pending_exit: tuple[str, str] | None = None
        self.trades: list[dict] = []
        self.markers: list[dict] = []
        self.candidates: list[dict] = []
        self.rejected: list[dict] = []
        self.heap: list[tuple[int, int, dict]] = []
        self.seq = 0
        self.trade_seq = 0
        self.prev_cond = {1: False, -1: False}
        self.last_eval = {1: -10**9, -1: -10**9}
        # first rejected candidate of the running episode (dropped from the stats if the episode is later taken)
        self.rejected_in_episode: dict[int, dict | None] = {1: None, -1: None}
        self.last_exit = {1: -10**9, -1: -10**9}
        self.loss_streak = 0
        self.closed_count = 0
        self.snapshots: list[dict] = []
        self.ctx_cache: tuple[int, GateContext] | None = None
        # what happened on the current bar (used for `current` at the last bar)
        self.bar_info: dict[str, Any] = {}

    # ------------------------------------------------------------------ helpers

    def state_dict(self) -> dict:
        lr = self.learner
        return {
            "version": STATE_VERSION,
            "trained_until": lr.trained_until,
            "updates": int(lr.updates),
            "hit_rate": None if lr.hit_rate is None else round(lr.hit_rate, 6),
            "brier": None if lr.brier is None else round(lr.brier, 6),
            "learner": lr.to_state(),
            "gate": self.gate_learner.to_state(),
            "edge": {"long": self.edge[1][-EDGE_IN_STATE:], "short": self.edge[-1][-EDGE_IN_STATE:]},
        }

    def edge_stats(self, regime_code: int) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
        stats: dict[str, tuple[int, int]] = {}
        scope: dict[str, str] = {}
        for side, name in ((1, "long"), (-1, "short")):
            hist = self.edge[side]
            same = [w for r, w in reversed(hist) if r == regime_code][:EDGE_WINDOW]
            if len(same) >= 8:
                stats[name] = (sum(same), len(same))
                scope[name] = "en este régimen"
            else:
                recent = [w for _, w in hist[-EDGE_WINDOW:]]
                stats[name] = (sum(recent), len(recent))
                scope[name] = ""
        return stats, scope

    def gate_ctx(self, i: int) -> GateContext:
        if self.ctx_cache is not None and self.ctx_cache[0] == i:
            return self.ctx_cache[1]
        f = self.f
        c = float(f.close[i])
        atr = f.value("atr", i)
        sup, res = sr_levels(f, i, c, atr)
        r = int(self.regime[i])
        edge, scope = self.edge_stats(r)
        shock = max(f.value("range_atr", i, 0.0), f.value("range_atr", i - 1, 0.0) if i > 0 else 0.0)
        sw_lo, sw_hi = f.value("swing_low", i), f.value("swing_high", i)
        ctx = GateContext(
            time=int(f.time[i]), close=c, atr=atr, sigma=f.value("sigma", i), ema_slow=f.value("ema_slow", i),
            rsi=f.value("rsi", i), macd_hist=f.value("macd_hist", i), adx=f.value("adx", i), er=f.value("er", i),
            atr_rank=f.value("atr_rank", i), shock=shock, vol_z=f.value("vol_z", i),
            htf_trend=f.value("htf_trend", i), htf_label=f.htf_label,
            prob_up=float(self.prob[i]) if self.prob[i] == self.prob[i] else 0.5,
            score=float(self.score[i]) if self.score[i] == self.score[i] else 0.0,
            regime=regime_name(r),
            swing_low=sw_lo if sw_lo == sw_lo else None, swing_high=sw_hi if sw_hi == sw_hi else None,
            supports=sup, resistances=res,
            expert_signals=self.S[i].tolist(), expert_weights=self.learner.weights_for(r).tolist(),
            edge=edge, edge_scope=scope, loss_streak=self.loss_streak, closed_trades=self.closed_count,
            prob_skill=self.learner.skill(),
        )
        self.ctx_cache = (i, ctx)
        return ctx

    def outcome(self, i: int, side: int, sl: float, tp1: float) -> tuple[int, bool] | None:
        """Hypothetical result of a candidate decided at the close of bar i: (bar where it is known, won).

        One definition shared with the global gate learner (app.sim.evaluator trains it with outcome == 'TP1'):
        won = TP1 reached before the SL within `time_stop_bars` bars (SL first when both are touched in one bar).
        A candidate that reaches neither by then is a loss (the trade would have been closed by the time stop),
        whatever its close; None while the horizon is not covered yet."""
        f = self.f
        horizon = int(self.p.time_stop_bars)
        end = min(i + horizon, self.n - 1)
        if end <= i:
            return None
        lows = f.low[i + 1 : end + 1]
        highs = f.high[i + 1 : end + 1]
        if side > 0:
            sl_hit, tp_hit = lows <= sl, highs >= tp1
        else:
            sl_hit, tp_hit = highs >= sl, lows <= tp1
        k_sl = int(np.argmax(sl_hit)) if sl_hit.any() else None
        k_tp = int(np.argmax(tp_hit)) if tp_hit.any() else None
        if k_sl is None and k_tp is None:
            if end == i + horizon:
                return end, False
            return None
        if k_tp is None or (k_sl is not None and k_sl <= k_tp):
            return i + 1 + int(k_sl), False
        return i + 1 + int(k_tp), True

    # ------------------------------------------------------------------ main loop

    def run(self) -> NexusResult:
        from app.engine.nexus_finish import finish_run, short_result

        if self.n < MIN_RUN_BARS:
            return short_result(self)
        if bool(np.any(self.f.low <= 0)) or bool(np.any(self.f.close <= 0)):
            return short_result(self, "NEXUS necesita precios positivos: esta serie tiene valores ≤ 0 "
                                      "(diferenciales o datos erróneos).")
        start = self.f.warmup
        times = self.f.time
        bucket_s = SNAPSHOT_EVERY * max(int(self.f.tf_s), 1)
        for i in range(start, self.n):
            self.bar_info = {}
            self.bar_ret[i] = 0.0
            if self.prev_trained is None or times[i] > self.prev_trained:
                self.learner.memory_bars += 1
            self._learn(i)
            self._predict(i)
            self._resolve_candidates(i)
            if i == self.n - 1 and self.global_gate is not None:
                self.decision_gate = GateLearner.pooled(self.gate_learner, self.global_gate)
            self._fills_at_open(i)
            self._intrabar(i)
            self._close_decisions(i)
            self._record(i)
            grid = int(times[i]) // bucket_s != int(times[i - 1]) // bucket_s  # i >= warm-up >= 1
            if grid or i == self.n - 1:
                self.snapshots.append({"time": int(times[i]), "state": self.state_dict(),
                                       "kind": "grid" if grid else "last"})
        return finish_run(self)

    def _learn(self, i: int) -> None:
        j = i - self.h
        if j < self.f.warmup:
            return
        rj = int(self.regime[j])
        pj = self.prob[j]
        if rj < 0 or pj != pj:
            return
        ret = float(self.f.close[i] - self.f.close[j])
        atr_j = self.f.atr[j]
        if abs(ret) <= (0.05 * atr_j if atr_j == atr_j else 0.0):
            return  # ambiguous label: no update
        self.learner.update(self.X[j], self.S[j], rj, 1 if ret > 0 else 0, float(pj), int(self.f.time[i]),
                            raw_p=float(self.raw_prob[j]))

    def _predict(self, i: int) -> None:
        r = int(self.regime[i])
        vote, raw, p = self.learner.predict(self.X[i], self.S[i], r)
        self.raw_prob[i] = raw
        score = 0.5 * vote + 0.5 * (2.0 * p - 1.0)
        agree = self.learner.agreement(self.S[i], r)
        conf = 0.4 * agree + 0.3 * min(1.0, abs(2.0 * p - 1.0) * 2.5) + 0.3 * self.learner.skill()
        self.score[i] = max(-1.0, min(1.0, score))
        self.prob[i] = p
        self.conf[i] = max(0.0, min(1.0, conf))

    def _resolve_candidates(self, i: int) -> None:
        while self.heap and self.heap[0][0] < i:
            _, _, cand = heapq.heappop(self.heap)
            self.gate_learner.update(cand["checks"], cand["won"])
            hist = self.edge[cand["side"]]
            hist.append([cand["regime"], 1 if cand["won"] else 0])
            if len(hist) > EDGE_KEEP:
                del hist[: len(hist) - EDGE_KEEP]

    def _book_fee(self, i: int, notional: float) -> None:
        """Charge the commission of a fill (`notional` as a fraction of the entry notional) to bar i's return."""
        prev = self.bar_ret[i]
        self.bar_ret[i] = (prev if prev == prev else 0.0) - self.p.fee_pct / 100.0 * notional

    def _open(self, side: int, lv: Levels, gate: dict | None, i: int, price: float, decision_idx: int) -> None:
        self.trade_seq += 1
        score = gate.get("score") if gate else None
        self.pos = Position(self.trade_seq, lv, i, int(self.f.time[i]), price, score,
                            int(self.regime[decision_idx]), decision_idx)
        self._book_fee(i, 1.0)

    def _fills_at_open(self, i: int) -> None:
        o = float(self.f.open[i])
        if self.pending_exit is not None and self.pos is not None:
            reason, label = self.pending_exit
            self.pending_exit = None
            prev_close = float(self.f.close[i - 1])
            left = self.pos.size_left
            self._book_fee(i, left * o / self.pos.entry_price)
            self.bar_ret[i] += left * self.pos.side * (o / prev_close - 1.0)
            self.pos.close_at(o)
            self._finalize(i, o, reason, label)
            self.bar_info["exit"]["at_open"] = True  # decided at the previous close
        if self.pending_entry is not None:
            side, lv, gate, decision_idx = self.pending_entry
            self.pending_entry = None
            if side * (o - lv.sl) > 0:  # a gap through the planned stop voids the entry
                self._open(side, lv, gate, i, o, decision_idx)

    def _finalize(self, i: int, price: float, reason: str, label: str | None) -> dict:
        pos = self.pos
        trade = pos.to_trade(int(self.f.time[i]), float(price), reason, label, None, self.p.fee_pct)
        self.trades.append(trade)
        self.pos = None
        self.closed_count += 1
        self.loss_streak = self.loss_streak + 1 if (trade["r_multiple"] or 0.0) < 0 else 0
        self.last_exit[pos.side] = i
        self.bar_info["exit"] = {"side": pos.side, "reason": reason, "label": trade["exit_label"],
                                 "price": float(price), "r": trade["r_multiple"]}
        return trade

    def _set_bar_levels(self, i: int, pos: Position) -> None:
        self.lv_entry[i] = pos.entry_price
        self.lv_sl[i] = pos.stop
        for k in range(3):
            self.lv_tp[k][i] = pos.tps[k] if pos.tp_hits <= k else None

    def _intrabar(self, i: int) -> None:
        from app.engine.nexus_out import exit_marker, tp_marker

        pos = self.pos
        if pos is None:
            return
        f = self.f
        self._set_bar_levels(i, pos)
        stop_kind = pos.stop_kind()
        ref = pos.entry_price if pos.entry_idx == i else float(f.close[i - 1])
        nf = len(pos.fills)
        events = pos.process_bar(float(f.open[i]), float(f.high[i]), float(f.low[i]))
        pos.bars += 1
        t = int(f.time[i])
        for kind, price in events:
            if kind.startswith("TP"):
                k = int(kind[2])
                closing = k == 3  # TP3 closes the remainder (several targets can be hit in one bar)
                r_total = pos.result(None, self.p.fee_pct)[1] if closing else None
                self.markers.append(tp_marker(t, pos.side, k, price, r_total))
                if closing:
                    self._finalize(i, price, "TP3", EXIT_LABEL["TP3"])
            else:
                reason = "TRAIL" if stop_kind == "TRAIL" else "SL"
                label = EXIT_LABEL["TRAIL"] if reason == "TRAIL" else EXIT_LABEL["BE" if stop_kind == "BE" else "SL"]
                r_total = pos.result(None, self.p.fee_pct)[1]
                self.markers.append(exit_marker(t, pos.side, "BE" if stop_kind == "BE" else reason, price, r_total, label))
                self._finalize(i, price, reason, label)
        fills = pos.fills[nf:]
        ret = sum(fr * pos.side * (x / ref - 1.0) for fr, x in fills)
        ret += pos.size_left * pos.side * (float(f.close[i]) / ref - 1.0)
        ret -= self.p.fee_pct / 100.0 * sum(fr * x / pos.entry_price for fr, x in fills)  # exit commissions
        prev = self.bar_ret[i]
        self.bar_ret[i] = ret + (prev if prev == prev else 0.0)
