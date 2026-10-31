"""Decisions taken at the close of each bar (mixin of NexusRun): Trade Gate on candidates, entries, exits
(early exit / signal / time / reverse) and the per-bar action."""
from __future__ import annotations

import heapq
from typing import Any

from app.engine.fmt import EXIT_LABEL, VERDICT_LABEL
from app.engine.gate import CHECK_IDS, checks_vector, evaluate_gate
from app.engine.levels import Levels, ev_hold
from app.engine.nexus_current import EARLY_EV, risk_flags
from app.engine.positions import Position

REEVAL_BARS = 3
COOLDOWN_BARS = 3


def gate_digest(gate: dict) -> tuple:
    """What a historical bar shows of a gate verdict: markers (verdict, score, first reason / summary), the trade's
    gate score and the check vector the gate learner trains on (the levels do not depend on the learner)."""
    reasons = gate.get("reasons_against") or []
    vec = checks_vector(gate.get("checks"))
    return (gate.get("verdict"), gate.get("score"), reasons[0] if reasons else None, gate.get("summary"),
            tuple(vec.get(cid) for cid in CHECK_IDS))


def gate_record(t: int, direction: str, gate: dict) -> dict:
    """Bar-close gate decision to store (JSON): the parts of `gate_digest` a later replay must reproduce."""
    verdict, score, reason, summary, vec = gate_digest(gate)
    return {"time": int(t), "direction": direction, "verdict": verdict, "score": score, "reason": reason,
            "summary": summary, "checks": list(vec)}


def record_digest(rec: dict) -> tuple:
    """`gate_digest` of a stored record (what the bar showed at its close)."""
    return (rec.get("verdict"), rec.get("score"), rec.get("reason"), rec.get("summary"),
            tuple(rec.get("checks") or ()))


def replay_gate(gate: dict, rec: dict) -> dict:
    """`gate` (evaluated with the run's own gate learner) as it was decided at that bar's close: verdict, score,
    reasons and check vector of the stored record (the global gate state pooled in then)."""
    g = dict(gate)
    verdict = str(rec.get("verdict"))
    g["verdict"], g["verdict_label"] = verdict, VERDICT_LABEL.get(verdict, verdict)
    g["score"] = rec.get("score")
    g["summary"] = rec.get("summary")
    g["reasons_against"] = [rec["reason"]] if rec.get("reason") else []
    g["checks"] = [{"id": cid, "passed": None if v is None else bool(v)} for cid, v in zip(CHECK_IDS, rec["checks"])]
    return g


class DecisionsMixin:
    """Requires the NexusRun attributes (f, p, n, h, score, prob, regime, pos, markers, ...)."""

    p: Any
    f: Any

    def _eligible(self, side: int, i: int) -> bool:
        return (not self.prev_cond[side]) or (i - self.last_eval[side] >= REEVAL_BARS)

    def _evaluate_candidate(self, i: int, side: int) -> tuple[dict, bool]:
        from app.engine.macro_merge import veto_gate
        from app.engine.nexus_out import rejected_marker

        ctx = self.gate_ctx(i)
        direction = "long" if side > 0 else "short"
        gate = evaluate_gate(ctx, direction, self.p, gate_state=self.decision_gate)
        t = int(self.f.time[i])
        rec = self.live.get((t, side)) if self.live else None
        if rec is not None:  # decided at that bar's close with the global gate state pooled in: replayed as taken
            full = rec.get("gate") if i == self.n - 1 else None
            if isinstance(full, dict) and gate_digest(full) == record_digest(rec):
                # that close is "now" again (a market sitting on it): the whole GateResult as decided then, so the
                # gate block of `current` agrees with the replayed action and advice (not a fresh global state)
                gate = dict(full)
                self.replayed_now[side] = gate
            else:
                gate = replay_gate(gate, rec)
        elif i == self.n - 1 and self.decision_gate is not self.gate_learner:
            # this close's decision uses the live global state; when that changes what the bar shows, the service
            # stores it (NexusResult.live_gates) so the bar keeps this decision once it is history; "gate" = the
            # whole GateResult, for the views of this same close (the service keeps it for the newest bar only)
            own = evaluate_gate(ctx, direction, self.p, gate_state=self.gate_learner)
            if gate_digest(own) != gate_digest(gate):
                self.live_records.append({**gate_record(t, direction, gate), "fp": self.fp, "gate": gate})
        self.last_eval[side] = i
        take = gate["verdict"] == "TAKE" or not self.p.gate_enabled
        # the gate learner sees the same candidates and check vectors with or without a macro veto
        sample, checks = take, checks_vector(gate["checks"])
        veto = self.vetoes.get((int(self.f.time[i]), side)) if take and self.vetoes else None
        if veto is not None:  # the semáforo vetoed this entry when it was live: reject it, never open it
            gate = veto_gate(gate, veto, self.p.take_score)
            take = False
        new_episode = not self.prev_cond[side]
        cand = None
        if new_episode or sample:
            res = self.outcome(i, side, float(gate["sl"]), float(gate["tp1"]))
            self.seq += 1
            cand = {"idx": i, "side": side, "regime": int(self.regime[i]), "checks": checks,
                    "verdict": gate["verdict"], "taken": take, "won": res[1] if res else None,
                    "resolve_idx": res[0] if res else None, "sl": float(gate["sl"]), "tp1": float(gate["tp1"]),
                    "seq": self.seq}
            self.candidates.append(cand)
            if res is not None:
                heapq.heappush(self.heap, (res[0], self.seq, cand))
            else:
                self.unknown.append(cand)  # outcome beyond these bars: a snapshot carries it to later runs
        if not take:
            marker = rejected_marker(int(self.f.time[i]), side, gate, float(self.f.close[i]))
            if self.rejected_in_episode[side] is None:
                if cand is None:
                    res = self.outcome(i, side, float(gate["sl"]), float(gate["tp1"]))
                    cand = {"idx": i, "side": side, "won": res[1] if res else None}
                self.rejected_in_episode[side] = cand
                self.rejected.append(cand)
                self.markers.append(marker)
            elif veto is not None:  # the episode is already counted; the veto still gets its own marker
                self.markers.append(marker)
            self.bar_info["rejected"] = gate
        else:
            earlier = self.rejected_in_episode[side]
            if earlier is not None and not new_episode:  # the gate waited, then confirmed: not a rejected signal
                self.rejected = [c for c in self.rejected if c is not earlier]
        return gate, take

    def _enter(self, i: int, side: int, gate: dict) -> None:
        from app.engine.nexus_out import entry_marker

        c = float(self.f.close[i])
        lv = Levels(side, c, float(gate["sl"]), float(gate["tp1"]), float(gate["tp2"]), float(gate["tp3"]))
        self.markers.append(entry_marker(int(self.f.time[i]), side, c, gate, lv.sl, [lv.tp1, lv.tp2, lv.tp3]))
        self.rejected_in_episode[side] = None
        self.bar_info["entry"] = gate
        if i == self.n - 1:  # the user should act now, at the last close
            self._open(side, lv, gate, i, c, i)
            self._set_bar_levels(i, self.pos)
        else:
            self.pending_entry = (side, lv, gate, i)

    def _exit_now_or_next(self, i: int, reason: str, label: str, detail: str) -> None:
        from app.engine.nexus_out import exit_marker

        pos = self.pos
        c = float(self.f.close[i])
        r_now = pos.result(c, self.p.fee_pct)[1]
        self.markers.append(exit_marker(int(self.f.time[i]), pos.side, reason, c, r_now, detail))
        if i == self.n - 1:
            self._book_fee(i, pos.size_left * c / pos.entry_price)
            pos.close_at(c)
            self._finalize(i, c, reason, label)
            self.bar_info["exit"]["detail"] = detail
        else:
            self.pending_exit = (reason, label)
            self.bar_info["exit_decision"] = {"side": pos.side, "reason": reason, "label": label, "detail": detail}

    def _exit_reason(self, i: int, pos: Position) -> tuple[str, str] | None:
        from app.engine import fmt

        f = self.f
        side = pos.side
        p_up = float(self.prob[i])
        p_dir = p_up if side > 0 else 1.0 - p_up
        c = float(f.close[i])
        ev, _ = ev_hold(side, c, pos.stop, pos.next_tp, pos.risk, p_dir, f.value("sigma", i, 0.0), self.h)
        self.bar_info["ev_hold"] = ev
        s_side = side * float(self.score[i])
        if ev <= EARLY_EV and s_side < 0:
            return "EARLY", f"Salida anticipada: valor esperado de mantener negativo ({fmt.rr(ev)})"
        if ev < 0 and s_side < 0.05:
            flags = risk_flags(f, i, side, pos, int(self.regime[i]))
            for code, why in (("VOL_SPIKE", "vela de volatilidad extrema en contra"),
                              ("DIVERGENCE", "divergencia RSI en contra"),
                              ("REGIME_CHANGE", "el régimen giró en contra")):
                if code in flags:
                    return "EARLY", f"Salida anticipada: {why} ({fmt.rr(ev)})"
        thr = -self.p.exit_threshold
        prev = self.score_before(i)
        if s_side < thr and prev == prev and side * float(prev) < thr:
            return "SIGNAL", f"Señal de salida: el score NEXUS giró en contra ({fmt.num(self.score[i], 2, True)})"
        if pos.bars >= self.p.time_stop_bars and pos.tp_hits == 0:
            return "TIME", f"Tiempo agotado: {pos.bars} velas sin alcanzar el objetivo 1"
        return None

    def _close_decisions(self, i: int) -> None:
        p = self.p
        s = float(self.score[i])
        cond = {1: s >= p.entry_threshold, -1: bool(p.allow_short) and s <= -p.entry_threshold}
        direction = 1 if cond[1] else (-1 if cond[-1] else 0)
        if self.regime[i] < 0:
            direction = 0
        pos = self.pos
        if pos is not None and self.pending_exit is None:
            pos.update_trail(self.f.value("atr", i), p.trail_atr)
            if direction == -pos.side and self._eligible(direction, i):
                gate, take = self._evaluate_candidate(i, direction)
                if take:
                    self._exit_now_or_next(i, "REVERSE", EXIT_LABEL["REVERSE"],
                                           f"Giro: señal contraria confirmada ({gate['verdict_label']} "
                                           f"{gate['score']:.0f}/100)")
                    self._enter(i, direction, gate)
                    self.action[i] = "BUY" if direction > 0 else "SELL"
                    return self._end_bar(cond)
            why = self._exit_reason(i, pos)
            if why is not None:
                reason, detail = why
                self._exit_now_or_next(i, reason, EXIT_LABEL[reason], detail)
                self.action[i] = "EXIT_LONG" if pos.side > 0 else "EXIT_SHORT"
            else:
                self.action[i] = "HOLD_LONG" if pos.side > 0 else "HOLD_SHORT"
            return self._end_bar(cond)
        if pos is None and direction != 0 and self._eligible(direction, i) \
                and i - self.last_exit[direction] >= COOLDOWN_BARS:
            gate, take = self._evaluate_candidate(i, direction)
            if take:
                self._enter(i, direction, gate)
                self.action[i] = "BUY" if direction > 0 else "SELL"
                return self._end_bar(cond)
        exit_info = self.bar_info.get("exit")
        if pos is None and exit_info is not None and not exit_info.get("at_open"):
            self.action[i] = "EXIT_LONG" if exit_info["side"] > 0 else "EXIT_SHORT"
        elif pos is not None:  # exit already decided (fills at the next open)
            self.action[i] = "EXIT_LONG" if pos.side > 0 else "EXIT_SHORT"
        else:
            self.action[i] = "WAIT"
        return self._end_bar(cond)

    def _end_bar(self, cond: dict[int, bool]) -> None:
        for side in (1, -1):
            if not cond[side]:
                self.rejected_in_episode[side] = None
        self.prev_cond = cond

    def _record(self, i: int) -> None:
        pos = self.pos
        if pos is not None and self.lv_entry[i] is None:
            self._set_bar_levels(i, pos)
