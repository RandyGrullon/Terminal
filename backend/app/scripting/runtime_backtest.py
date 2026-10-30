"""Bar-by-bar replay of the order rules of a PyChart strategy -> BacktestResult dict (types.ts).

Execution model (causal): rules are evaluated at the close of bar j and market orders fill at the open of bar
j+1 (with slippage). Protective levels are checked intrabar: a gap beyond a level fills at the open, otherwise
the stop is assumed to trade before the limit (conservative). Sizing: ``qty_pct`` % of equity when given, else
``risk_pct`` % of equity at risk when the trade has a stop, else 100 % of equity; never more than 100 % of
equity (no leverage). Fees (``fee_pct`` per side) apply to the notional of every fill.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from app.engine import ta as T

from .runtime_base import Bars
from .runtime_metrics import benchmark_points, compute_metrics, equity_points
from .runtime_strategy import OrderRules

GREEN = "#26A69A"
RED = "#EF5350"
EXITS = {  # reason -> (marker kind, Spanish label, short text)
    "SL": ("EXIT_SL", "Stop loss", "SL"),
    "TRAIL": ("EXIT_TRAIL", "Stop dinámico (trailing ATR)", "Trail"),
    "TP1": ("TP1", "Objetivo (límite)", "TP"),
    "SIGNAL": ("EXIT_SIGNAL", "Cierre por señal", "Cierre"),
    "REVERSE": ("EXIT_REVERSE", "Señal contraria", "Reverso"),
}
NOTES = [
    "Señales al cierre de la vela y ejecución en la apertura de la siguiente; stops y objetivos intrabarra "
    "(si ambos se tocan en la misma vela, se asume el stop).",
    "Sin apalancamiento: cada operación usa como máximo el 100% del capital. fees_paid incluye comisiones.",
    "Resultados simulados sobre datos históricos: no garantizan resultados futuros.",
]


def _finite(v: Any) -> bool:
    return v is not None and isinstance(v, float) and math.isfinite(v)


def _as_list(arr: np.ndarray | None) -> list | None:
    return None if arr is None else arr.tolist()


class _Position:
    __slots__ = (
        "side", "entry_id", "qty", "price", "index", "notional", "fee", "equity_before", "fixed_stop",
        "fixed_limit", "rule_levels", "trail", "best", "sl_initial", "tp_initial", "fav", "adv",
    )

    def __init__(self, **kw: Any) -> None:
        for key in self.__slots__:
            setattr(self, key, kw.get(key))


class _Backtester:
    def __init__(self, bars: Bars, rules: OrderRules, config: dict[str, Any]) -> None:
        self.n = bars.n
        self.t = bars.time.tolist()
        self.o, self.h = bars.open.tolist(), bars.high.tolist()
        self.l, self.c = bars.low.tolist(), bars.close.tolist()
        self.capital = float(config["initial_capital"])
        self.risk = float(config["risk_pct"]) / 100.0
        self.fee = float(config["fee_pct"]) / 100.0
        self.slip = float(config["slippage_pct"]) / 100.0
        self.entries = [
            dict(r, when=r["when"].tolist(), stop=_as_list(r["stop"]), limit=_as_list(r["limit"]), qty_pct=_as_list(r["qty_pct"]))
            for r in rules.entries
        ]
        self.exits = [dict(r, when=r["when"].tolist(), stop=_as_list(r["stop"]), limit=_as_list(r["limit"])) for r in rules.exits]
        self.closes = [dict(r, when=r["when"].tolist()) for r in rules.closes]
        need_atr = any(r["trail"] for r in self.exits)
        self.atr = T.atr(bars.high, bars.low, bars.close, 14).tolist() if need_atr else None
        self.cash = self.capital
        self.pos: _Position | None = None
        self.fees_paid = 0.0
        self.equity = [self.capital] * self.n
        self.in_pos = [False] * self.n
        self.trades: list[dict] = []
        self.markers: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def _rules_for(self, entry_id: str) -> list[tuple[int, dict]]:
        return [(k, r) for k, r in enumerate(self.exits) if r["from"] is None or r["from"] == entry_id]

    def _capture_levels(self, pos: _Position, j: int) -> None:
        """Captures exit-rule levels on bar j: fixed rules once (first bar with a finite level where ``when`` is
        true, starting at the signal bar), ``update=True`` rules on every such bar."""
        for k, rule in self._rules_for(pos.entry_id):
            if not rule["when"][j] or (k in pos.rule_levels and not rule["update"]):
                continue
            stop = rule["stop"][j] if rule["stop"] is not None else None
            limit = rule["limit"][j] if rule["limit"] is not None else None
            if not _finite(stop) and not _finite(limit):
                continue
            prev = pos.rule_levels.get(k, (None, None))
            pos.rule_levels[k] = (stop if _finite(stop) else prev[0], limit if _finite(limit) else prev[1])

    def _effective(self, pos: _Position) -> tuple[float | None, str, float | None]:
        stops = [s for s, _ in pos.rule_levels.values() if s is not None]
        limits = [lim for _, lim in pos.rule_levels.values() if lim is not None]
        if pos.fixed_stop is not None:
            stops.append(pos.fixed_stop)
        if pos.fixed_limit is not None:
            limits.append(pos.fixed_limit)
        pick_stop, pick_limit = (max, min) if pos.side > 0 else (min, max)
        stop = pick_stop(stops) if stops else None
        kind = "SL"
        if pos.trail is not None and (stop is None or pick_stop(stop, pos.trail) == pos.trail):
            stop, kind = pos.trail, "TRAIL"
        return stop, kind, pick_limit(limits) if limits else None

    # ------------------------------------------------------------------ orders
    def _open(self, rule: dict, i: int, j: int) -> None:
        side = rule["dir"]
        price = self.o[j] * (1.0 + self.slip * side)
        equity_now = self.cash
        if not price > 0 or equity_now <= 0:
            return
        pos = _Position(side=side, entry_id=rule["id"], price=price, index=j, rule_levels={}, trail=None)
        stop0 = rule["stop"][i] if rule["stop"] is not None else None
        limit0 = rule["limit"][i] if rule["limit"] is not None else None
        pos.fixed_stop = stop0 if _finite(stop0) else None
        pos.fixed_limit = limit0 if _finite(limit0) else None
        self._capture_levels(pos, i)
        stop, _, limit = self._effective(pos)
        valid_stop = stop is not None and (price - stop) * side > 0
        size = rule["qty_pct"][i] if rule["qty_pct"] is not None else None
        if _finite(size) and size > 0:
            notional = equity_now * min(size, 100.0) / 100.0
        elif valid_stop:
            notional = min(equity_now * self.risk / abs(price - stop) * price, equity_now)
        else:
            notional = equity_now
        qty = notional / price
        if not qty > 0:
            return
        pos.qty, pos.notional, pos.equity_before = qty, notional, equity_now
        pos.fee = notional * self.fee
        pos.sl_initial = stop if valid_stop else None
        pos.tp_initial = limit if limit is not None and (limit - price) * side > 0 else None
        pos.best, pos.fav, pos.adv = price, 0.0, 0.0
        self.cash -= pos.fee
        self.fees_paid += pos.fee
        self.pos = pos
        long = side > 0
        self.markers.append(
            {
                "time": int(self.t[j]),
                "kind": "BUY" if long else "SELL",
                "position": "belowBar" if long else "aboveBar",
                "shape": "arrowUp" if long else "arrowDown",
                "color": GREEN if long else RED,
                "text": rule["comment"] or rule["id"],
                "tooltip": f"Entrada {'larga' if long else 'corta'} '{rule['id']}' @ {price:.6g}",
                "price": price,
            }
        )

    def _close(self, j: int, raw_price: float, reason: str, *, at_open: bool, label: str | None = None) -> None:
        pos = self.pos
        assert pos is not None
        side = pos.side
        fill = raw_price if reason == "TP1" else raw_price * (1.0 - self.slip * side)
        move = (fill - pos.price) * side
        pos.fav, pos.adv = max(pos.fav, move), max(pos.adv, -move)
        gross = pos.qty * move
        fee_exit = pos.qty * fill * self.fee
        self.cash += gross - fee_exit
        self.fees_paid += fee_exit
        pnl = gross - pos.fee - fee_exit
        kind, default_label, short = EXITS[reason]
        trade = self._trade_dict(pos, pnl)
        trade.update(
            {
                "exit_time": int(self.t[j]),
                "exit_price": fill,
                "exit_reason": reason,
                "exit_label": label or default_label,
                "r_multiple": move / abs(pos.price - pos.sl_initial) if pos.sl_initial is not None else None,
                "bars": max(1, j - pos.index + (0 if at_open else 1)),
                "fees": pos.fee + fee_exit,
            }
        )
        self.trades.append(trade)
        self.markers.append(
            {
                "time": int(self.t[j]),
                "kind": kind,
                "position": "aboveBar" if side > 0 else "belowBar",
                "shape": "circle",
                "color": GREEN if pnl > 0 else RED,
                "text": f"{short} {trade['pnl_pct']:+.1f}%",
                "tooltip": f"{label or default_label} '{pos.entry_id}' @ {fill:.6g} · {trade['pnl_pct']:+.2f}%",
                "price": fill,
            }
        )
        self.pos = None

    def _trade_dict(self, pos: _Position, pnl: float) -> dict:
        return {
            "id": len(self.trades) + 1,
            "side": "long" if pos.side > 0 else "short",
            "entry_id": pos.entry_id,
            "entry_time": int(self.t[pos.index]),
            "entry_price": pos.price,
            "exit_time": None,
            "exit_price": None,
            "sl_initial": pos.sl_initial,
            "tp1": pos.tp_initial,
            "tp2": None,
            "tp3": None,
            "exit_reason": "OPEN",
            "exit_label": "Abierta",
            "pnl": pnl,
            "pnl_pct": pnl / pos.notional * 100.0 if pos.notional > 0 else 0.0,
            "account_pct": pnl / pos.equity_before * 100.0 if pos.equity_before > 0 else 0.0,
            "qty": pos.qty,
            "r_multiple": None,
            "bars": self.n - pos.index,
            "mfe_pct": pos.fav / pos.price * 100.0,
            "mae_pct": pos.adv / pos.price * 100.0,
            "gate_score": None,
        }

    # --- end of section A: orders ---

    # ------------------------------------------------------------------ bar loop
    def _check_levels(self, j: int) -> None:
        """Intrabar protective exits: gaps fill at the open, then stop before limit."""
        pos = self.pos
        assert pos is not None
        stop, kind, limit = self._effective(pos)
        op, hi, lo, s = self.o[j], self.h[j], self.l[j], pos.side
        if stop is not None and (op - stop) * s <= 0:
            self._close(j, op, kind, at_open=True)
        elif limit is not None and (op - limit) * s >= 0:
            self._close(j, op, "TP1", at_open=True)
        elif stop is not None and ((lo - stop) if s > 0 else (stop - hi)) <= 0:
            self._close(j, stop, kind, at_open=False)
        elif limit is not None and ((hi - limit) if s > 0 else (limit - lo)) >= 0:
            self._close(j, limit, "TP1", at_open=False)

    def _update_trail(self, j: int) -> None:
        pos = self.pos
        assert pos is not None
        s = pos.side
        pos.best = max(pos.best, self.h[j]) if s > 0 else min(pos.best, self.l[j])
        if self.atr is None or not _finite(self.atr[j]):
            return
        mults = [r["trail"] for _, r in self._rules_for(pos.entry_id) if r["trail"] and r["when"][j]]
        if not mults:
            return
        level = pos.best - s * min(mults) * self.atr[j]
        if pos.trail is None or (level - pos.trail) * s > 0:
            pos.trail = level  # ratchets only in the trade's favour

    def run(self) -> None:
        pending_close: tuple[str, str | None] | None = None
        pending_entry: tuple[dict, int] | None = None
        ruined = False
        for j in range(self.n):
            if pending_close is not None and self.pos is not None:
                self._close(j, self.o[j], pending_close[0], at_open=True, label=pending_close[1])
            if pending_entry is not None and self.pos is None and not ruined:
                self._open(pending_entry[0], pending_entry[1], j)
            pending_close = pending_entry = None
            if self.pos is not None:
                self.in_pos[j] = True
                self._check_levels(j)
            pos = self.pos
            if pos is not None:
                s = pos.side
                pos.fav = max(pos.fav, (self.h[j] - pos.price) if s > 0 else (pos.price - self.l[j]))
                pos.adv = max(pos.adv, (pos.price - self.l[j]) if s > 0 else (self.h[j] - pos.price))
                self.equity[j] = self.cash + pos.qty * (self.c[j] - pos.price) * s
                if self.equity[j] <= 0:  # account wiped out: liquidate at the close
                    self._close(j, self.c[j], "SIGNAL", at_open=False, label="Capital agotado")
                    self.equity[j] = self.cash
            else:
                self.equity[j] = self.cash
            if self.equity[j] <= 0:
                ruined = True
            if j == self.n - 1:
                break
            pos = self.pos
            if pos is not None:
                self._update_trail(j)
                self._capture_levels(pos, j)
                for rule in self.closes:
                    if rule["when"][j] and (rule["id"] is None or rule["id"] == pos.entry_id):
                        pending_close = ("SIGNAL", rule["comment"])
                        break
            if ruined:
                continue
            current = 0 if (pos is None or pending_close is not None) else pos.side
            for rule in self.entries:
                if rule["when"][j] and rule["dir"] != current:
                    if current != 0:
                        pending_close = ("REVERSE", None)
                    pending_entry = (rule, j)
                    break

    def result(self, symbol: str, tf: str, title: str) -> dict:
        closed = list(self.trades)
        if self.pos is not None:  # still open at the last bar: marked to market
            pos, last = self.pos, self.n - 1
            move = (self.c[last] - pos.price) * pos.side
            trade = self._trade_dict(pos, pos.qty * move - pos.fee)
            trade["r_multiple"] = move / abs(pos.price - pos.sl_initial) if pos.sl_initial is not None else None
            trade["fees"] = pos.fee
            self.trades.append(trade)
        times = np.asarray(self.t, dtype=np.int64)
        equity = np.asarray(self.equity, dtype=float)
        closes = np.asarray(self.c, dtype=float)
        metrics = compute_metrics(times, closes, equity, np.asarray(self.in_pos), closed, self.capital, self.fees_paid)
        for tr in self.trades:
            for key, nd in (("pnl", 2), ("pnl_pct", 4), ("account_pct", 4), ("r_multiple", 4), ("mfe_pct", 4), ("mae_pct", 4), ("fees", 4)):
                if isinstance(tr.get(key), float):
                    tr[key] = round(tr[key], nd) if math.isfinite(tr[key]) else None
        return {
            "symbol": symbol,
            "tf": tf,
            "bars": self.n,
            "start_time": int(self.t[0]) if self.n else 0,
            "end_time": int(self.t[-1]) if self.n else 0,
            "params": {
                "engine": "pychart",
                "title": title,
                "initial_capital": self.capital,
                "risk_pct": self.risk * 100.0,
                "fee_pct": self.fee * 100.0,
                "slippage_pct": self.slip * 100.0,
                "notes": list(NOTES),
            },
            "metrics": metrics,
            "ungated": None,
            "equity": equity_points(times, equity),
            "benchmark": benchmark_points(times, closes, self.capital),
            "trades": self.trades,
            "markers": self.markers,
            "candles": [],
        }


def simulate(bars: Bars, rules: OrderRules, config: dict[str, Any], *, symbol: str = "", tf: str = "", title: str = "") -> dict:
    """Runs the registered order rules over the bars and returns a BacktestResult-shaped dict."""
    bt = _Backtester(bars, rules, config)
    if bars.n:
        bt.run()
    return bt.result(symbol, tf, title)
