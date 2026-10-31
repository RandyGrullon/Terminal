"""Open-position bookkeeping for the NEXUS state machine: intrabar SL/TP handling (SL first), partial exits,
break-even, chandelier trailing and the final trade accounting (fees per side, R multiple, MFE/MAE)."""
from __future__ import annotations

from app.engine.fmt import EXIT_LABEL
from app.engine.levels import Levels

PARTIAL = 1.0 / 3.0


class Position:
    __slots__ = (
        "trade_id", "side", "entry_idx", "entry_time", "entry_price", "sl_initial", "sl", "tps", "risk",
        "tp_hits", "size_left", "trail", "hi", "lo", "fills", "gate_score", "entry_regime", "bars",
        "decision_idx",
    )

    def __init__(self, trade_id: int, lv: Levels, entry_idx: int, entry_time: int, entry_price: float,
                 gate_score: float | None, entry_regime: int, decision_idx: int):
        self.trade_id = trade_id
        self.side = lv.side
        self.entry_idx = entry_idx
        self.entry_time = int(entry_time)
        self.entry_price = float(entry_price)
        self.sl_initial = float(lv.sl)
        self.sl = float(lv.sl)
        self.tps = [float(lv.tp1), float(lv.tp2), float(lv.tp3)]
        self.risk = max(abs(self.entry_price - self.sl_initial), 1e-12)
        self.tp_hits = 0
        self.size_left = 1.0
        self.trail: float | None = None
        self.hi = self.entry_price
        self.lo = self.entry_price
        self.fills: list[tuple[float, float]] = []
        self.gate_score = gate_score
        self.entry_regime = entry_regime
        self.bars = 0
        self.decision_idx = decision_idx

    # ------------------------------------------------------------------ persistence (anchor snapshots)

    def to_state(self, ref: int) -> dict:
        """JSON-ready state; bar indices stored as offsets back from bar `ref` (the snapshot bar)."""
        return {
            "id": int(self.trade_id), "side": int(self.side), "entry_off": int(ref) - int(self.entry_idx),
            "entry_time": int(self.entry_time), "entry_price": float(self.entry_price),
            "sl_initial": float(self.sl_initial), "sl": float(self.sl), "tps": [float(x) for x in self.tps],
            "risk": float(self.risk), "tp_hits": int(self.tp_hits), "size_left": float(self.size_left),
            "trail": None if self.trail is None else float(self.trail), "hi": float(self.hi), "lo": float(self.lo),
            "fills": [[float(fr), float(px)] for fr, px in self.fills],
            "gate_score": None if self.gate_score is None else float(self.gate_score),
            "entry_regime": int(self.entry_regime), "bars": int(self.bars),
            "decision_off": int(ref) - int(self.decision_idx),
        }

    @classmethod
    def from_state(cls, d: dict, ref: int) -> "Position":
        """Inverse of `to_state` for a run whose bar `ref` is the snapshot bar."""
        pos = cls.__new__(cls)
        pos.trade_id = int(d["id"])
        pos.side = 1 if int(d["side"]) > 0 else -1
        pos.entry_idx = int(ref) - int(d["entry_off"])
        pos.entry_time = int(d["entry_time"])
        pos.entry_price = float(d["entry_price"])
        pos.sl_initial = float(d["sl_initial"])
        pos.sl = float(d["sl"])
        pos.tps = [float(x) for x in d["tps"]][:3]
        pos.risk = float(d["risk"])
        pos.tp_hits = int(d["tp_hits"])
        pos.size_left = float(d["size_left"])
        pos.trail = None if d.get("trail") is None else float(d["trail"])
        pos.hi = float(d["hi"])
        pos.lo = float(d["lo"])
        pos.fills = [(float(fr), float(px)) for fr, px in d.get("fills") or []]
        pos.gate_score = None if d.get("gate_score") is None else float(d["gate_score"])
        pos.entry_regime = int(d["entry_regime"])
        pos.bars = int(d["bars"])
        pos.decision_idx = int(ref) - int(d["decision_off"])
        if len(pos.tps) != 3 or not pos.risk > 0:
            raise ValueError("posición almacenada incompleta")
        return pos

    # ------------------------------------------------------------------ state

    @property
    def stop(self) -> float:
        if self.trail is None:
            return self.sl
        return max(self.sl, self.trail) if self.side > 0 else min(self.sl, self.trail)

    @property
    def next_tp(self) -> float | None:
        return self.tps[self.tp_hits] if self.tp_hits < 3 else None

    @property
    def is_open(self) -> bool:
        return self.size_left > 1e-9

    def stop_kind(self) -> str:
        """Which stop is active: SL (initial), BE (break-even) or TRAIL."""
        if self.trail is not None and (self.trail > self.sl if self.side > 0 else self.trail < self.sl):
            return "TRAIL"
        return "BE" if self.tp_hits >= 1 else "SL"

    # ------------------------------------------------------------------ bar processing

    def process_bar(self, o: float, h: float, lo: float) -> list[tuple[str, float]]:
        """Intrabar exits for one bar. SL is checked first (pessimistic tie-break); gaps fill at the open."""
        events: list[tuple[str, float]] = []
        s = self.side
        stop = self.stop
        if (s > 0 and o <= stop) or (s < 0 and o >= stop):
            events.append(("STOP", o))
        elif (s > 0 and lo <= stop) or (s < 0 and h >= stop):
            events.append(("STOP", stop))
        else:
            while self.tp_hits < 3:
                tp = self.tps[self.tp_hits]
                if not ((s > 0 and h >= tp) or (s < 0 and lo <= tp)):
                    break
                fill = max(tp, o) if s > 0 else min(tp, o)
                k = self.tp_hits
                self.tp_hits += 1
                frac = min(PARTIAL, self.size_left) if k < 2 else self.size_left
                self._fill(frac, fill)
                events.append((f"TP{k + 1}", fill))
                if k == 0:
                    self.sl = self.entry_price  # break-even from the next bar on
                if not self.is_open:
                    break
        if events and events[0][0] == "STOP":
            self._fill(self.size_left, events[0][1])
        self.hi = max(self.hi, h)
        self.lo = min(self.lo, lo)
        return events

    def update_trail(self, atr: float, trail_atr: float) -> None:
        """Chandelier stop after TP2 (computed at the close, effective from the next bar)."""
        if self.tp_hits < 2 or not (atr == atr) or atr <= 0:
            return
        if self.side > 0:
            cand = self.hi - trail_atr * atr
            self.trail = cand if self.trail is None else max(self.trail, cand)
        else:
            cand = self.lo + trail_atr * atr
            self.trail = cand if self.trail is None else min(self.trail, cand)

    def close_at(self, price: float) -> None:
        if self.is_open:
            self._fill(self.size_left, price)

    def _fill(self, frac: float, price: float) -> None:
        frac = max(min(frac, self.size_left), 0.0)
        if frac > 0:
            self.fills.append((frac, float(price)))
            self.size_left -= frac
            if self.size_left < 1e-9:
                self.size_left = 0.0

    # ------------------------------------------------------------------ accounting

    def result(self, mark_price: float | None, fee_pct: float) -> tuple[float, float]:
        """(pnl_pct, r_multiple) including partial exits and fees on both sides; the open remainder is valued
        at `mark_price` (no exit fee charged for it)."""
        e, s = self.entry_price, self.side
        gross = sum(f * s * (x - e) for f, x in self.fills)
        notional_out = sum(f * x for f, x in self.fills)
        if self.is_open and mark_price is not None:
            gross += self.size_left * s * (mark_price - e)
        fees = fee_pct / 100.0 * (e + notional_out)
        pnl = gross - fees
        return pnl / e * 100.0, pnl / self.risk

    def excursions(self) -> tuple[float, float]:
        """(MFE %, MAE %) relative to the entry; MAE is <= 0."""
        e = self.entry_price
        if self.side > 0:
            return max((self.hi - e) / e * 100.0, 0.0), min((self.lo - e) / e * 100.0, 0.0)
        return max((e - self.lo) / e * 100.0, 0.0), min((e - self.hi) / e * 100.0, 0.0)

    def to_trade(self, exit_time: int | None, exit_price: float | None, reason: str, label: str | None,
                 mark_price: float | None, fee_pct: float) -> dict:
        pnl_pct, r_mult = self.result(mark_price, fee_pct)
        mfe, mae = self.excursions()
        side = "long" if self.side > 0 else "short"
        return {
            "id": self.trade_id,
            "side": side,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "exit_time": exit_time,
            "exit_price": exit_price,
            "sl_initial": self.sl_initial,
            "tp1": self.tps[0],
            "tp2": self.tps[1],
            "tp3": self.tps[2],
            "exit_reason": reason,
            "exit_label": label or EXIT_LABEL.get(reason, reason),
            "pnl_pct": round(pnl_pct, 4),
            "r_multiple": round(r_mult, 4),
            "bars": int(self.bars),
            "mfe_pct": round(mfe, 4),
            "mae_pct": round(mae, 4),
            "gate_score": None if self.gate_score is None else round(float(self.gate_score), 1),
        }
