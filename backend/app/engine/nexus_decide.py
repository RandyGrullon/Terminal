"""Decisions taken at the close of each bar (mixin of NexusRun): Trade Gate on candidates, entries, exits
(early exit / signal / time / reverse) and the per-bar action."""
from __future__ import annotations

import heapq
from typing import Any

import numpy as np

from app.engine.fmt import EXIT_LABEL
from app.engine.gate import checks_vector, evaluate_gate
from app.engine.levels import Levels, ev_hold
from app.engine.nexus_current import EARLY_EV, risk_flags
from app.engine.positions import Position

REEVAL_BARS = 3
COOLDOWN_BARS = 3


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
        gate = evaluate_gate(ctx, "long" if side > 0 else "short", self.p, gate_state=self.decision_gate)
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
                    "resolve_idx": res[0] if res else None}
            self.candidates.append(cand)
            if res is not None:
                heapq.heappush(self.heap, (res[0], self.seq, cand))
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
        prev = self.score[i - 1] if i > 0 else np.nan
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
