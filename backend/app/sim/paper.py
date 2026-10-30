"""Paper trading (`rt.paper`): demo accounts, orders checked by the Trade Gate, live position management.

Accounts: 1 = "Principal" (manual / auto-trading), 2 = "Simulación" (driven by the market simulator).
Margin-style accounting: the balance only moves with realised P&L and fees; equity = balance + unrealised P&L.

Order flow: price → Trade Gate (`rt.nexus.gate`) → AVOID blocks the order unless `force` (PaperRejected → HTTP
409) → SL/TP from the gate when not given ("hasta aquí") → size by risk % of equity and the stop distance →
market fill with slippage + fee → rows in paper_positions / paper_fills inside one DB transaction.

Management (live feed through the hub, 1m bars for real symbols): SL first; TP1 closes 1/3 and moves the stop to
break-even; TP2 closes 1/3 and starts a chandelier trailing stop; TP3 closes the rest. NEXUS advice is attached
to every open position (≤ 1 refresh/min per symbol); when NEXUS says the trade turned bad (negative expected value
of holding) auto-managed positions are closed ("Cierre anticipado") and manual ones get a warning event.
Positions with origin 'sim' belong to the simulator: they are priced and managed only through `sim_bar`.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app import config
from app.sim import metrics as M
from app.sim.common import (
    EXIT_LABEL,
    SIDE_LABEL,
    call_maybe_async,
    fmt_num,
    fmt_pct,
    fmt_price,
    fmt_prob,
    fmt_qty,
    fmt_r,
    gate_checks_vector,
    gate_line,
    json_dumps,
    json_loads,
    side_sign,
)
from app.util import RateLimitedLog, normalize_tf, now_ms, now_s, safe_float, tf_seconds

ACCOUNTS = {1: "Principal", 2: "Simulación"}
MAX_LEVERAGE = M.DEFAULT_MAX_LEVERAGE
PUBLISH_EVERY_S = 5.0
ADVICE_EVERY_S = 60.0
PRICE_STALE_S = 20.0
WARN_REPEAT_S = 900.0
HISTORY_LIMIT = 100


class PaperError(ValueError):
    """Invalid paper order / operation (Spanish message)."""


class PaperRejected(Exception):
    """The Trade Gate said NO TOMAR and the order was not forced."""

    def __init__(self, gate: dict, message: str):
        super().__init__(message)
        self.gate = gate
        self.message = message


# ----------------------------------------------------------------------------- pure level management


@dataclass
class LevelState:
    side: int
    entry: float
    qty: float
    qty_open: float
    sl: float
    tps: list[float | None]
    tp_hits: int = 0
    trail: float | None = None
    atr: float = 0.0
    trail_mult: float = 3.0
    high_water: float = 0.0
    low_water: float = 0.0
    partial: bool = True

    def effective_stop(self) -> float:
        if self.trail is None:
            return self.sl
        return max(self.sl, self.trail) if self.side > 0 else min(self.sl, self.trail)

    def targets(self) -> list[tuple[int, float]]:
        defined = [(k + 1, lvl) for k, lvl in enumerate(self.tps) if lvl is not None]
        if not self.partial and defined:
            return [next((t for t in defined if t[0] == 2), defined[-1])]
        return defined

    def next_target(self) -> float | None:
        for k, lvl in self.targets():
            if k > self.tp_hits:
                return lvl
        return None


@dataclass
class Fill:
    kind: str  # partial | close
    reason: str  # SL | TRAIL | TP1 | TP2 | TP3 | ...
    price: float
    qty: float
    market: bool  # market fills pay slippage (stops); limit fills (targets) do not
    label: str = ""
    extra: dict = field(default_factory=dict)


def process_bar(st: LevelState, high: float, low: float, close: float, *, open_: float | None = None,
                use_hl: bool = True) -> list[Fill]:
    """Apply one price update to the position levels (mutates `st`). Stop first, then targets in order.

    `use_hl=False` uses only the close (bar that started before the entry, or a polled last price).
    """
    if not use_hl:
        high = low = close
    s = st.side
    fills: list[Fill] = []
    stop = st.effective_stop()
    if (s > 0 and low <= stop) or (s < 0 and high >= stop):
        px = stop
        if not use_hl:
            px = close
        elif open_ is not None and ((s > 0 and open_ < stop) or (s < 0 and open_ > stop)):
            px = open_  # gap through the stop: filled at the open
        trailing = st.trail is not None and abs(st.effective_stop() - st.trail) < 1e-12 and st.tp_hits >= 2
        if trailing:
            reason, label = "TRAIL", EXIT_LABEL["TRAIL"]
        elif st.tp_hits >= 1 and abs(st.sl - st.entry) <= 1e-9 * max(1.0, abs(st.entry)):
            reason, label = "SL", EXIT_LABEL["BE"]
        else:
            reason, label = "SL", EXIT_LABEL["SL"]
        fills.append(Fill("close", reason, px, st.qty_open, True, label))
        st.qty_open = 0.0
        return fills
    st.high_water = max(st.high_water, high)
    st.low_water = min(st.low_water, low) if st.low_water > 0 else low
    targets = st.targets()
    for idx, (k, lvl) in enumerate(targets):
        if k <= st.tp_hits:
            continue
        touched = high >= lvl if s > 0 else low <= lvl
        if not touched:
            break
        if idx == len(targets) - 1 or st.qty_open <= st.qty / 3.0 + 1e-12:
            fills.append(Fill("close", f"TP{k}", lvl, st.qty_open, False, EXIT_LABEL.get(f"TP{k}", f"TP{k}")))
            st.qty_open = 0.0
            st.tp_hits = k
            return fills
        part = min(st.qty / 3.0, st.qty_open)
        fills.append(Fill("partial", f"TP{k}", lvl, part, False, EXIT_LABEL.get(f"TP{k}", f"TP{k}")))
        st.qty_open -= part
        st.tp_hits = k
        if k == 1:  # break-even
            st.sl = max(st.sl, st.entry) if s > 0 else min(st.sl, st.entry)
    if st.tp_hits >= 2 and st.atr > 0:
        if s > 0:
            candidate = st.high_water - st.trail_mult * st.atr
            st.trail = max(st.trail if st.trail is not None else -math.inf, candidate)
        else:
            candidate = st.low_water + st.trail_mult * st.atr
            st.trail = min(st.trail if st.trail is not None else math.inf, candidate)
    return fills


def assess_position(side: str, price: float, stop: float, next_tp: float | None, risk_unit: float,
                    current: dict | None, exit_threshold: float = 0.0) -> dict:
    """Live NEXUS advice for an open position: expected value of holding (R) and early-exit flag."""
    if not current or risk_unit <= 0 or price is None:
        return {"early_exit": False, "ev_r": None, "advice": None, "warnings": []}
    s = side_sign(side)
    p_up = safe_float(current.get("prob_up"), 0.5)
    p_dir = p_up if s > 0 else 1.0 - p_up
    dist_tp = abs(next_tp - price) / risk_unit if next_tp is not None else 1.0
    dist_sl = max(s * (price - stop), 0.0) / risk_unit
    ev = p_dir * dist_tp - (1.0 - p_dir) * dist_sl
    score = safe_float(current.get("score"), 0.0)
    action = str(current.get("action") or "")
    against_action = action in (("EXIT_LONG", "SELL") if s > 0 else ("EXIT_SHORT", "BUY"))
    score_against = s * score < -max(exit_threshold, 0.1)
    early = against_action or (ev < -0.05 and score_against)
    warnings = [w for w in (current.get("warnings") or []) if isinstance(w, dict)]
    side_lbl = SIDE_LABEL.get(side, side)
    if early:
        why = current.get("action_label") or action or "señal contraria"
        advice = f"Cerrar ya: NEXUS recomienda cerrar el {side_lbl} (EV {fmt_r(ev)}, {why})"
        warnings = [{"code": "EARLY_EXIT", "severity": "critical", "message": advice}] + warnings
    else:
        critical = next((w for w in warnings if w.get("severity") == "critical"), None)
        if critical:
            advice = str(critical.get("message") or "")
        else:
            advice = f"Mantener: EV {fmt_r(ev)} · probabilidad a favor {fmt_prob(p_dir)}"
    return {"early_exit": bool(early), "ev_r": ev, "advice": advice, "warnings": warnings}


def atr_from_candles(cs: Any, n: int = 14) -> float | None:
    if cs is None or len(cs) < n + 1:
        return None
    high, low, close = (np.asarray(a, dtype=float) for a in (cs.high, cs.low, cs.close))
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])
    atr = float(np.mean(tr[-n:]))
    return atr if math.isfinite(atr) and atr > 0 else None


# ----------------------------------------------------------------------------- engine


class PaperEngine:
    def __init__(self, rt: Any):
        self.rt = rt
        self._started = False
        self._open: dict[int, dict] = {}
        self._lock = asyncio.Lock()
        self._prices: dict[str, tuple[float, float]] = {}
        self._advice_at: dict[str, float] = {}
        self._warned: dict[int, float] = {}
        self._subs: set[tuple[str, str, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self._listener_registered = False
        self._last_publish = 0.0
        self._sim_info: dict[str, Any] = {"auto_trade": False, "symbols": [], "tf": "1h"}
        self._log = RateLimitedLog()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._started = True
        await self.ensure_accounts()
        rows = await self.rt.db.fetchall("SELECT * FROM paper_positions WHERE status='open'")
        for row in rows:
            pos = _pos_from_row(row)
            self._open[pos["id"]] = pos
        stale_sim = [p for p in self._open.values() if p["origin"] == "sim"]
        for pos in stale_sim:  # the simulator session that owned them no longer exists
            px = safe_float(pos["meta"].get("last_price")) or pos["entry_price"]
            await self._close(pos, px, "MANUAL", "Sesión de simulación finalizada", now_s(), market=False, emit=False)
        await self._register_listener()
        if self.rt.settings is not None:
            self.rt.settings.add_listener(self._on_setting)
        if config.BACKGROUND:
            self._loop_task = asyncio.create_task(self._monitor_loop(), name="paper-monitor")
            await self._sync_subscriptions()

    async def stop(self) -> None:
        self._started = False
        tasks = [t for t in [self._loop_task, *self._tasks] if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._loop_task = None
        hub = self.rt.hub
        for symbol, tf, owner in list(self._subs):
            try:
                await call_maybe_async(hub.unsubscribe, symbol, tf, owner)
            except Exception:
                pass
        self._subs.clear()

    def status(self) -> dict:
        return {
            "running": self._started,
            "open_positions": sum(1 for p in self._open.values() if p["account_id"] == 1),
        }

    async def ensure_accounts(self) -> None:
        size = float(self._setting("risk.account_size", 10000.0) or 10000.0)
        ts = now_ms()
        await self.rt.db.executemany(
            "INSERT OR IGNORE INTO paper_accounts(id, name, balance, initial_balance, currency, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'USD', ?, ?)",
            [(aid, name, size, size, ts, ts) for aid, name in ACCOUNTS.items()],
        )

    async def _register_listener(self) -> None:
        hub = self.rt.hub
        if self._listener_registered or hub is None or not hasattr(hub, "add_listener"):
            return
        try:
            await call_maybe_async(hub.add_listener, self._on_candle)
            self._listener_registered = True
        except Exception as exc:
            self._emit("warn", "paper", f"No se pudo escuchar el feed de precios: {exc}")

    # ------------------------------------------------------------------ helpers

    def _setting(self, key: str, default: Any = None) -> Any:
        if self.rt.settings is None:
            from app.config import DEFAULT_SETTINGS

            return DEFAULT_SETTINGS.get(key, default)
        return self.rt.settings.get(key, default)

    def _emit(self, level: str, source: str, message: str, data: Any = None) -> None:
        if self.rt.bus is not None:
            self.rt.bus.emit(level, source, message, data)

    @staticmethod
    def _source(pos_or_origin: Any) -> str:
        origin = pos_or_origin.get("origin") if isinstance(pos_or_origin, dict) else pos_or_origin
        return "sim" if origin == "sim" else "paper"

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _fee_pct(self) -> float:
        return float(self._setting("risk.fee_pct", 0.1) or 0.0)

    def _slip_pct(self) -> float:
        return float(self._setting("risk.slippage_pct", 0.02) or 0.0)

    def cached_price(self, symbol: str, max_age_s: float | None = None) -> float | None:
        item = self._prices.get(symbol.upper())
        if item is None:
            return None
        price, ts = item
        if max_age_s is not None and time.monotonic() - ts > max_age_s:
            return None
        return price

    async def last_price(self, symbol: str) -> float | None:
        symbol = symbol.upper()
        cached = self.cached_price(symbol, 10.0)
        if cached is not None:
            return cached
        hub = self.rt.hub
        if hub is None:
            return None
        try:
            price = safe_float(await asyncio.wait_for(hub.last_price(symbol), timeout=20))
        except Exception:
            price = None
        if price is not None and price > 0:
            self._prices[symbol] = (price, time.monotonic())
            return price
        return None

    def _position_price(self, pos: dict) -> float | None:
        if pos["origin"] == "sim":
            return safe_float(pos["meta"].get("last_price"))
        return self.cached_price(pos["symbol"]) or safe_float(pos["meta"].get("last_price"))

    # ------------------------------------------------------------------ views

    def position_dict(self, pos: dict) -> dict:
        meta = pos["meta"]
        s = side_sign(pos["side"])
        qty, entry = pos["qty"], pos["entry_price"]
        risk_unit = safe_float(meta.get("risk_unit")) or abs(entry - (safe_float(meta.get("initial_sl")) or pos["sl"]))
        is_open = pos["status"] == "open"
        price = self._position_price(pos) if is_open else safe_float(pos.get("exit_price"))
        if is_open:
            unreal = s * ((price or entry) - entry) * pos["qty_open"]
            gross = float(meta.get("realized_gross", 0.0)) + unreal
            pnl = gross - float(meta.get("fees", 0.0))
            r_mult = gross / (qty * risk_unit) if qty > 0 and risk_unit > 0 else None
        else:
            pnl = float(pos.get("pnl") or 0.0)
            r_mult = safe_float(pos.get("r_multiple"))
        notional = qty * entry
        reason = pos.get("exit_reason")
        return {
            "id": pos["id"],
            "account_id": pos["account_id"],
            "symbol": pos["symbol"],
            "tf": pos.get("tf") or "1h",
            "side": pos["side"],
            "qty": qty,
            "qty_open": pos["qty_open"],
            "entry_price": entry,
            "entry_time": pos["entry_time"],
            "sl": pos["sl"],
            "tp1": pos.get("tp1"),
            "tp2": pos.get("tp2"),
            "tp3": pos.get("tp3"),
            "trail": pos.get("trail"),
            "tp_hits": int(pos.get("tp_hits") or 0),
            "status": pos["status"],
            "price": price,
            "exit_price": pos.get("exit_price"),
            "exit_time": pos.get("exit_time"),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / notional * 100.0, 4) if notional > 0 else 0.0,
            "r_multiple": round(r_mult, 3) if r_mult is not None else None,
            "exit_reason": reason,
            "exit_label": meta.get("exit_label") or (EXIT_LABEL.get(reason) if reason else None),
            "origin": pos.get("origin") or "manual",
            "gate_verdict": pos.get("gate_verdict"),
            "gate_score": pos.get("gate_score"),
            "advice": meta.get("advice"),
            "warnings": meta.get("warnings") or [],
        }

    def open_positions(self, account_id: int = 1, symbol: str | None = None) -> list[dict]:
        items = [p for p in self._open.values() if p["account_id"] == account_id
                 and (symbol is None or p["symbol"] == symbol.upper())]
        return [self.position_dict(p) for p in sorted(items, key=lambda p: p["id"])]

    async def history(self, account_id: int = 1, limit: int = HISTORY_LIMIT) -> list[dict]:
        rows = await self.rt.db.fetchall(
            "SELECT * FROM paper_positions WHERE account_id=? AND status='closed' ORDER BY exit_time DESC, id DESC LIMIT ?",
            (account_id, int(limit)),
        )
        return [self.position_dict(_pos_from_row(r)) for r in rows]

    async def account(self, account_id: int = 1) -> dict:
        row = await self.rt.db.fetchone("SELECT * FROM paper_accounts WHERE id=?", (account_id,))
        if row is None:
            await self.ensure_accounts()
            row = await self.rt.db.fetchone("SELECT * FROM paper_accounts WHERE id=?", (account_id,))
        stats = await self.rt.db.fetchone(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins FROM paper_positions "
            "WHERE account_id=? AND status='closed'",
            (account_id,),
        ) or {}
        balance = float(row["balance"])
        initial = float(row["initial_balance"])
        unreal = 0.0
        open_n = 0
        for pos in self._open.values():
            if pos["account_id"] != account_id:
                continue
            open_n += 1
            price = self._position_price(pos)
            if price is not None:
                unreal += side_sign(pos["side"]) * (price - pos["entry_price"]) * pos["qty_open"]
        trades = int(stats.get("n") or 0)
        wins = int(stats.get("wins") or 0)
        equity = balance + unreal
        if account_id == 1:
            auto = bool(self._setting("paper.auto_trade", False))
            symbols = list(self._setting("paper.auto_symbols", []) or [])
            auto_tf = str(self._setting("paper.auto_tf", "1h"))
        else:
            auto = bool(self._sim_info.get("auto_trade"))
            symbols = list(self._sim_info.get("symbols") or [])
            auto_tf = str(self._sim_info.get("tf") or "1h")
        return {
            "id": account_id,
            "name": row["name"],
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "initial_balance": round(initial, 2),
            "realized_pnl": round(balance - initial, 2),
            "unrealized_pnl": round(unreal, 2),
            "return_pct": round((equity / initial - 1.0) * 100.0, 4) if initial > 0 else 0.0,
            "trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate": wins / trades if trades else None,
            "open_positions": open_n,
            "auto_trade": auto,
            "auto_symbols": symbols,
            "auto_tf": auto_tf,
        }

    async def snapshot(self, account_id: int = 1) -> dict:
        return {
            "account": await self.account(account_id),
            "positions": self.open_positions(account_id),
            "history": await self.history(account_id),
        }

    async def publish(self, account_id: int = 1) -> None:
        """WS topic 'paper' carries account 1 only (account 2 travels inside the simulator's 'sim' messages)."""
        if account_id != 1 or self.rt.ws is None:
            return
        try:
            self.rt.ws.publish("paper", {"type": "paper", "account": await self.account(1),
                                         "positions": self.open_positions(1)})
            self._last_publish = time.monotonic()
        except Exception:
            pass

    async def equity(self, account_id: int) -> float:
        return float((await self.account(account_id))["equity"])

    def set_sim_info(self, *, auto_trade: bool, symbols: list[str], tf: str) -> None:
        self._sim_info = {"auto_trade": bool(auto_trade), "symbols": list(symbols), "tf": tf}

    # ------------------------------------------------------------------ orders

    async def open_position(
        self,
        account_id: int = 1,
        symbol: str = "",
        side: str = "long",
        tf: str | None = None,
        qty: float | None = None,
        risk_pct: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        force: bool = False,
        origin: str = "manual",
        price: float | None = None,
        *,
        gate: dict | None = None,
        check_gate: bool = True,
        time_s: int | None = None,
        tps: list[float | None] | None = None,
    ) -> dict:
        """Open a paper position. Returns {"position": Position, "gate": GateResult | None}."""
        side = str(side or "").strip().lower()
        if side in ("buy", "compra", "largo"):
            side = "long"
        elif side in ("sell", "venta", "corto"):
            side = "short"
        if side not in ("long", "short"):
            raise PaperError("Lado no válido: usa 'long' (compra) o 'short' (venta)")
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            raise PaperError("Falta el símbolo")
        if account_id not in ACCOUNTS:
            raise PaperError(f"La cuenta {account_id} no existe")
        try:
            tf_n = normalize_tf(tf or self._setting("paper.auto_tf", "1h") or "1h")
        except ValueError as exc:
            raise PaperError(str(exc)) from None
        if origin not in ("manual", "auto", "sim", "script"):
            origin = "manual"
        sl = safe_float(sl)
        tp = safe_float(tp)
        qty = safe_float(qty)
        if qty is not None and qty <= 0:
            raise PaperError("La cantidad debe ser mayor que 0")
        risk = safe_float(risk_pct)
        if risk is None:
            risk = float(self._setting("risk.per_trade_pct", 1.0) or 1.0)
        if not 0 < risk <= 100:
            raise PaperError("El riesgo por operación debe estar entre 0 y 100 %")

        ref = safe_float(price)
        if ref is None:
            ref = await self.last_price(symbol)
        if ref is None or ref <= 0:
            raise PaperError(f"No hay precio disponible para {symbol}")

        if gate is None and check_gate and self.rt.nexus is not None and hasattr(self.rt.nexus, "gate"):
            try:
                gate = await asyncio.wait_for(
                    self.rt.nexus.gate(symbol, tf_n, side, entry=ref, sl=sl, tp=tp), timeout=90
                )
            except Exception as exc:
                gate = None
                if self._log.should_log(f"gate:{symbol}", 60):
                    self._emit("warn", self._source(origin),
                               f"No se pudo evaluar el filtro para {symbol}: {type(exc).__name__}: {exc}")
        if not isinstance(gate, dict):
            gate = None
        if gate is not None and gate.get("verdict") == "AVOID" and not force:
            summary = gate.get("summary") or "el filtro desaconseja esta operación"
            raise PaperRejected(gate, f"Operación bloqueada por el filtro ({gate_line(gate)}): {summary}")

        s = side_sign(side)
        fill = M.slipped_price(ref, s, self._slip_pct(), opening=True)
        stop = await self._choose_stop(symbol, tf_n, s, fill, sl, gate)
        risk_unit = abs(fill - stop)
        targets = self._choose_targets(s, fill, risk_unit, sl, tp, tps, gate)

        if qty is None:
            eq = await self.equity(account_id)
            qty = M.position_size(eq, risk, fill, stop, max_leverage=MAX_LEVERAGE)
        if qty is None or qty <= 0 or not math.isfinite(qty):
            raise PaperError("Tamaño de posición nulo: revisa el riesgo, el stop y el saldo")
        fee = qty * fill * self._fee_pct() / 100.0
        atr = await self._atr_estimate(symbol, tf_n, risk_unit, origin)
        entry_time = int(time_s) if time_s is not None else now_s()
        meta = {
            "initial_sl": stop,
            "risk_unit": risk_unit,
            "atr": atr,
            "fees": fee,
            "realized_gross": 0.0,
            "high_water": fill,
            "low_water": fill,
            "last_price": ref,
            "risk_pct": risk,
            "partial": bool(self._setting("paper.partial_tp", True)),
            "advice": None,
            "warnings": [],
            "gate_summary": gate.get("summary") if gate else None,
            "forced": bool(force and gate is not None and gate.get("verdict") == "AVOID"),
        }
        verdict = gate.get("verdict") if gate else None
        gate_score = safe_float(gate.get("score")) if gate else None
        async with self._lock:
            async with self.rt.db.transaction() as tx:
                pid = await tx.execute(
                    "INSERT INTO paper_positions(account_id, symbol, tf, side, qty, qty_open, entry_price, entry_time, sl, "
                    "tp1, tp2, tp3, trail, tp_hits, status, pnl, pnl_pct, origin, gate_verdict, gate_score, meta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 'open', ?, 0, ?, ?, ?, ?)",
                    (account_id, symbol, tf_n, side, qty, qty, fill, entry_time, stop, targets[0], targets[1],
                     targets[2], -fee, origin, verdict, gate_score, json_dumps(meta)),
                )
                await tx.execute(
                    "INSERT INTO paper_fills(position_id, account_id, time, side, qty, price, fee, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'Apertura')",
                    (pid, account_id, entry_time, "buy" if s > 0 else "sell", qty, fill, fee),
                )
                await tx.execute(
                    "UPDATE paper_accounts SET balance = balance - ?, updated_at=? WHERE id=?",
                    (fee, now_ms(), account_id),
                )
                if gate is not None:
                    await tx.execute(
                        "INSERT OR IGNORE INTO gate_log(symbol, tf, time, direction, verdict, score, win_prob, entry, sl, "
                        "tp1, tp2, checks, taken, source, created_at, target_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                        (
                            symbol, tf_n, int(safe_float(gate.get("time")) or entry_time), side, verdict or "WAIT",
                            gate_score or 0.0, safe_float(gate.get("win_prob")), fill, stop, targets[0], targets[1],
                            json_dumps(gate_checks_vector(gate)), "sim" if origin == "sim" else "paper", now_ms(),
                            now_ms() + int(self._setting("nexus.time_stop_bars", 48) or 48) * tf_seconds(tf_n) * 1000,
                        ),
                    )
            pos = {
                "id": pid, "account_id": account_id, "symbol": symbol, "tf": tf_n, "side": side, "qty": qty,
                "qty_open": qty, "entry_price": fill, "entry_time": entry_time, "sl": stop, "tp1": targets[0],
                "tp2": targets[1], "tp3": targets[2], "trail": None, "tp_hits": 0, "status": "open",
                "exit_price": None, "exit_time": None, "pnl": -fee, "pnl_pct": 0.0, "r_multiple": None,
                "exit_reason": None, "origin": origin, "gate_verdict": verdict, "gate_score": gate_score, "meta": meta,
            }
            self._open[pid] = pos
        if origin != "sim":
            self._prices.setdefault(symbol, (ref, time.monotonic()))
        forced =" · FORZADA contra el filtro" if meta["forced"] else ""
        tps_txt = " · ".join(f"TP{i + 1} {fmt_price(t)}" for i, t in enumerate(targets) if t is not None)
        self._emit(
            "trade",
            self._source(origin),
            f"Abierto {symbol} {SIDE_LABEL[side]} {fmt_qty(qty)} @ {fmt_price(fill)} · SL {fmt_price(stop)}"
            + (f" · {tps_txt}" if tps_txt else "") + (f" · Filtro {gate_line(gate)}" if gate else "") + forced,
            {"position_id": pid, "account_id": account_id, "symbol": symbol, "side": side},
        )
        await self.publish(account_id)
        if config.BACKGROUND and origin != "sim":
            await self._sync_subscriptions()
        return {"position": self.position_dict(pos), "gate": gate}

    async def _choose_stop(self, symbol: str, tf: str, s: int, fill: float, sl: float | None, gate: dict | None) -> float:
        side_word = "por debajo" if s > 0 else "por encima"
        if sl is not None:
            if s * (fill - sl) <= 0:
                raise PaperError(f"El stop ({fmt_price(sl)}) debe estar {side_word} del precio de entrada "
                                 f"({fmt_price(fill)})")
            return float(sl)
        gsl = safe_float(gate.get("sl")) if gate else None
        if gsl is not None and s * (fill - gsl) > 0:
            return gsl
        atr = None
        if self.rt.hub is not None:
            try:
                cs = await asyncio.wait_for(self.rt.hub.get_candles(symbol, tf, 60), timeout=20)
                atr = atr_from_candles(cs)
            except Exception:
                atr = None
        dist = atr * float(self._setting("nexus.sl_atr", 2.0) or 2.0) if atr else fill * 0.02
        return fill - s * dist

    def _choose_targets(self, s: int, fill: float, risk_unit: float, sl: float | None, tp: float | None,
                        tps: list[float | None] | None, gate: dict | None) -> list[float | None]:
        def valid(x: Any) -> float | None:
            v = safe_float(x)
            return v if v is not None and s * (v - fill) > 0 else None

        if tps is not None:
            out = [valid(x) for x in list(tps)[:3]]
            out += [None] * (3 - len(out))
            if any(v is not None for v in out):
                return out
        if tp is not None:
            v = valid(tp)
            if v is None:
                raise PaperError(f"El objetivo ({fmt_price(tp)}) debe estar {'por encima' if s > 0 else 'por debajo'} "
                                 f"del precio de entrada ({fmt_price(fill)})")
            return [v, None, None]
        if sl is None and gate is not None:
            out = [valid(gate.get(k)) for k in ("tp1", "tp2", "tp3")]
            if out[0] is not None:
                return out
        mults = self._setting("nexus.tp_r", [1.0, 2.0, 3.0]) or [1.0, 2.0, 3.0]
        out = [fill + s * float(m) * risk_unit for m in list(mults)[:3]]
        return out + [None] * (3 - len(out))

    async def _atr_estimate(self, symbol: str, tf: str, risk_unit: float, origin: str) -> float:
        sl_atr = float(self._setting("nexus.sl_atr", 2.0) or 2.0)
        fallback = risk_unit / max(sl_atr, 0.1)
        if origin == "sim" or self.rt.hub is None:
            return fallback
        try:
            cs = await asyncio.wait_for(self.rt.hub.get_candles(symbol, tf, 60), timeout=15)
            atr = atr_from_candles(cs)
        except Exception:
            atr = None
        return atr if atr else fallback

    # ------------------------------------------------------------------ fills & closing

    async def _apply_fills(self, pos: dict, fills: list[Fill], time_s: int, st: LevelState | None = None,
                           *, emit: bool = True) -> bool:
        """Book fills / level changes for a position (caller holds self._lock). True when the position closed."""
        if not fills and st is None:
            return False
        meta = pos["meta"]
        s = side_sign(pos["side"])
        fee_pct, slip = self._fee_pct(), self._slip_pct()
        closed = False
        rows: list[tuple] = []
        delta_balance = 0.0
        events: list[str] = []
        for f in fills:
            qty = min(f.qty, pos["qty_open"])
            if qty <= 0:
                continue
            px = M.slipped_price(f.price, s, slip, opening=False) if f.market else f.price
            fee = qty * px * fee_pct / 100.0
            gross = s * (px - pos["entry_price"]) * qty
            meta["realized_gross"] = float(meta.get("realized_gross", 0.0)) + gross
            meta["fees"] = float(meta.get("fees", 0.0)) + fee
            delta_balance += gross - fee
            pos["qty_open"] = max(pos["qty_open"] - qty, 0.0)
            rows.append((pos["id"], pos["account_id"], time_s, "sell" if s > 0 else "buy", qty, px, fee, f.label or f.reason))
            if f.kind == "close" or pos["qty_open"] <= pos["qty"] * 1e-9:
                closed = True
                pos["qty_open"] = 0.0
                pos["status"] = "closed"
                pos["exit_price"] = px
                pos["exit_time"] = time_s
                pos["exit_reason"] = f.reason
                meta["exit_label"] = f.label or EXIT_LABEL.get(f.reason, f.reason)
            else:
                r_part = s * (px - pos["entry_price"]) / max(float(meta.get("risk_unit") or 0.0), 1e-12)
                extra = " · stop a punto de equilibrio" if f.reason == "TP1" else " · stop dinámico activado" if f.reason == "TP2" else ""
                events.append(f"{pos['symbol']} {SIDE_LABEL[pos['side']]}: {f.reason} alcanzado ({fmt_r(r_part)}) · "
                              f"1/3 cerrado a {fmt_price(px)}{extra}")
        if st is not None:
            pos["sl"] = st.sl
            pos["trail"] = st.trail
            pos["tp_hits"] = st.tp_hits
            meta["high_water"] = st.high_water
            meta["low_water"] = st.low_water
        risk_unit = float(meta.get("risk_unit") or 0.0)
        net = float(meta.get("realized_gross", 0.0)) - float(meta.get("fees", 0.0))
        pos["pnl"] = net
        if closed:
            notional = pos["qty"] * pos["entry_price"]
            pos["pnl_pct"] = net / notional * 100.0 if notional > 0 else 0.0
            pos["r_multiple"] = (float(meta["realized_gross"]) / (pos["qty"] * risk_unit)
                                 if risk_unit > 0 and pos["qty"] > 0 else None)
        async with self.rt.db.transaction() as tx:
            if rows:
                await tx.executemany(
                    "INSERT INTO paper_fills(position_id, account_id, time, side, qty, price, fee, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            if delta_balance:
                await tx.execute("UPDATE paper_accounts SET balance = balance + ?, updated_at=? WHERE id=?",
                                 (delta_balance, now_ms(), pos["account_id"]))
            await tx.execute(
                "UPDATE paper_positions SET qty_open=?, sl=?, trail=?, tp_hits=?, status=?, exit_price=?, exit_time=?, "
                "pnl=?, pnl_pct=?, r_multiple=?, exit_reason=?, meta=? WHERE id=?",
                (pos["qty_open"], pos["sl"], pos["trail"], pos["tp_hits"], pos["status"], pos["exit_price"],
                 pos["exit_time"], pos["pnl"], pos.get("pnl_pct") or 0.0, pos.get("r_multiple"), pos["exit_reason"],
                 json_dumps(meta), pos["id"]),
            )
        source = self._source(pos)
        if emit:
            for msg in events:
                self._emit("trade", source, msg, {"position_id": pos["id"], "account_id": pos["account_id"]})
        if closed:
            self._open.pop(pos["id"], None)
            self._warned.pop(pos["id"], None)
        if closed and emit:
            self._emit(
                "trade",
                source,
                f"Cerrado {pos['symbol']} {SIDE_LABEL[pos['side']]} {fmt_r(pos.get('r_multiple'))} "
                f"({fmt_pct(pos.get('pnl_pct'), 2)} · {fmt_num(pos['pnl'], 2, True)}) · {meta.get('exit_label')}",
                {"position_id": pos["id"], "account_id": pos["account_id"], "pnl": pos["pnl"],
                 "r_multiple": pos.get("r_multiple"), "reason": pos["exit_reason"]},
            )
        return closed

    async def _close(self, pos: dict, price: float, reason: str, label: str | None, time_s: int, *,
                     market: bool = True, emit: bool = True) -> dict:
        fill = Fill("close", reason, float(price), pos["qty_open"], market, label or EXIT_LABEL.get(reason, reason))
        await self._apply_fills(pos, [fill], time_s, emit=emit)
        return self.position_dict(pos)

    async def close_position(
        self,
        position_id: int | None = None,
        symbol: str | None = None,
        all: bool = False,
        *,
        account_id: int = 1,
        reason: str = "MANUAL",
        label: str | None = None,
        price: float | None = None,
        time_s: int | None = None,
    ) -> list[dict]:
        """Close at market: one position by id, every position of a symbol, or all (of the account)."""
        if position_id is None and not symbol and not all:
            raise PaperError("Indica qué cerrar: un id de posición, un símbolo o 'all'")
        if position_id is not None:
            pos = self._open.get(int(position_id))
            if pos is None or pos["account_id"] != account_id:
                raise PaperError(f"No hay una posición abierta con id {position_id}")
            targets = [pos]
        elif all:
            targets = [p for p in self._open.values() if p["account_id"] == account_id]
        else:
            sym = str(symbol).upper()
            targets = [p for p in self._open.values() if p["account_id"] == account_id and p["symbol"] == sym]
            if not targets:
                raise PaperError(f"No hay posiciones abiertas en {sym}")
        closed: list[dict] = []
        for pos in sorted(targets, key=lambda p: p["id"]):
            px = safe_float(price)
            if px is None:
                px = self._position_price(pos) if pos["origin"] == "sim" else await self.last_price(pos["symbol"])
            if px is None:
                px = safe_float(pos["meta"].get("last_price"))
            if px is None:
                raise PaperError(f"No hay precio para cerrar {pos['symbol']}")
            async with self._lock:
                if pos["id"] not in self._open:
                    continue
                closed.append(await self._close(pos, px, reason, label, int(time_s or now_s())))
        if closed:
            await self.publish(account_id)
            if config.BACKGROUND:
                await self._sync_subscriptions()
        return closed

    async def reset(self, initial_balance: float | None = None, account_id: int = 1) -> dict:
        amount = safe_float(initial_balance)
        if amount is None:
            amount = float(self._setting("risk.account_size", 10000.0) or 10000.0)
        if amount <= 0:
            raise PaperError("El saldo inicial debe ser mayor que 0")
        await self.ensure_accounts()
        async with self._lock:
            for pid in [pid for pid, p in self._open.items() if p["account_id"] == account_id]:
                self._open.pop(pid, None)
                self._warned.pop(pid, None)
            async with self.rt.db.transaction() as tx:
                await tx.execute("DELETE FROM paper_fills WHERE account_id=?", (account_id,))
                await tx.execute("DELETE FROM paper_positions WHERE account_id=?", (account_id,))
                await tx.execute(
                    "UPDATE paper_accounts SET balance=?, initial_balance=?, updated_at=? WHERE id=?",
                    (amount, amount, now_ms(), account_id),
                )
        if account_id == 1:
            self._emit("info", "paper", f"Cuenta demo reiniciada con saldo {fmt_num(amount, 2)}")
        await self.publish(account_id)
        if config.BACKGROUND:
            await self._sync_subscriptions()
        return await self.snapshot(account_id)

    # ------------------------------------------------------------------ price processing

    def _level_state(self, pos: dict) -> LevelState:
        meta = pos["meta"]
        return LevelState(
            side=side_sign(pos["side"]),
            entry=pos["entry_price"],
            qty=pos["qty"],
            qty_open=pos["qty_open"],
            sl=pos["sl"],
            tps=[pos.get("tp1"), pos.get("tp2"), pos.get("tp3")],
            tp_hits=int(pos.get("tp_hits") or 0),
            trail=pos.get("trail"),
            atr=float(meta.get("atr") or 0.0),
            trail_mult=float(self._setting("nexus.trail_atr", 3.0) or 3.0),
            high_water=float(meta.get("high_water") or pos["entry_price"]),
            low_water=float(meta.get("low_water") or pos["entry_price"]),
            partial=bool(meta.get("partial", True)),
        )

    async def _process_price(self, pos: dict, high: float, low: float, close: float, *, open_: float | None,
                             use_hl: bool, time_s: int) -> bool:
        """Run level management for one position (caller holds self._lock). Returns True if it closed."""
        meta = pos["meta"]
        meta["last_price"] = close
        st = self._level_state(pos)
        before = (st.sl, st.trail, st.tp_hits)
        fills = process_bar(st, high, low, close, open_=open_, use_hl=use_hl)
        # water marks only matter for the trailing stop: keep them in memory, persisted with the next write
        meta["high_water"] = st.high_water
        meta["low_water"] = st.low_water
        if not fills and before == (st.sl, st.trail, st.tp_hits):
            return False
        return await self._apply_fills(pos, fills, time_s, st)

    async def _on_candle(self, symbol: str, tf: str, candle: dict, closed: bool) -> None:
        """Hub listener: live updates of every subscribed (symbol, tf)."""
        try:
            sym = str(symbol).upper()
            close = safe_float((candle or {}).get("close"))
            if close is None or close <= 0:
                return
            self._prices[sym] = (close, time.monotonic())
            is_sim_symbol = sym.startswith("SIM:")
            if is_sim_symbol or tf == "1m":
                positions = [p for p in self._open.values() if p["symbol"] == sym and p["origin"] != "sim"]
                if positions:
                    changed_accounts: set[int] = set()
                    bar_time = int(safe_float(candle.get("time")) or now_s())
                    high = safe_float(candle.get("high")) or close
                    low = safe_float(candle.get("low")) or close
                    async with self._lock:
                        for pos in positions:
                            if pos["id"] not in self._open:
                                continue
                            use_hl = bar_time >= int(pos["entry_time"])
                            n_before = (pos["qty_open"], pos["tp_hits"])
                            await self._process_price(pos, high, low, close, open_=safe_float(candle.get("open")),
                                                      use_hl=use_hl, time_s=now_s())
                            if (pos["qty_open"], pos["tp_hits"]) != n_before:
                                changed_accounts.add(pos["account_id"])
                    for aid in changed_accounts:
                        await self.publish(aid)
            if closed and self._setting("paper.auto_trade", False):
                auto_symbols = {str(s).upper() for s in (self._setting("paper.auto_symbols", []) or [])}
                if sym in auto_symbols and tf == str(self._setting("paper.auto_tf", "1h")):
                    self._spawn(self._auto_check(sym, tf))
        except Exception as exc:
            if self._log.should_log("listener", 120):
                self._emit("warn", "paper", f"Error gestionando posiciones con el feed: {type(exc).__name__}: {exc}")

    async def sim_bar(self, symbol: str, tf: str, bar: dict, *, account_id: int = 2) -> list[dict]:
        """Simulator hook: manage the simulator's positions with a revealed bar. Returns closed positions."""
        sym = symbol.upper()
        bar_time = int(bar["time"])
        closed: list[dict] = []
        async with self._lock:
            for pos in [p for p in self._open.values()
                        if p["account_id"] == account_id and p["symbol"] == sym and p["origin"] == "sim"]:
                use_hl = bar_time >= int(pos["entry_time"])
                if await self._process_price(pos, float(bar["high"]), float(bar["low"]), float(bar["close"]),
                                             open_=float(bar["open"]), use_hl=use_hl,
                                             time_s=bar_time + tf_seconds(tf)):
                    closed.append(self.position_dict(pos))
        return closed

    def sim_positions(self, symbol: str | None = None, account_id: int = 2) -> list[dict]:
        return [p for p in self.open_positions(account_id, symbol) if p["origin"] == "sim"]

    def advise(self, position_id: int, current: dict | None, price: float | None = None) -> dict | None:
        """Attach NEXUS advice/warnings to an open position; returns the assessment (see assess_position)."""
        pos = self._open.get(position_id)
        if pos is None or not isinstance(current, dict):
            return None
        st = self._level_state(pos)
        px = price if price is not None else (self._position_price(pos) or safe_float(current.get("price")))
        info = assess_position(
            pos["side"], px, st.effective_stop(), st.next_target(), float(pos["meta"].get("risk_unit") or 0.0),
            current, float(self._setting("nexus.exit_threshold", 0.0) or 0.0),
        )
        pos["meta"]["advice"] = info["advice"]
        pos["meta"]["warnings"] = info["warnings"]
        return info

    # ------------------------------------------------------------------ NEXUS advice / early exit

    async def refresh_advice(self, *, force: bool = False) -> int:
        """Attach live NEXUS advice to open positions (≤ 1/min per symbol); early-exit handling. Returns #closed."""
        nexus = self.rt.nexus
        if nexus is None or not hasattr(nexus, "current"):
            return 0
        by_key: dict[tuple[str, str], list[dict]] = {}
        for pos in self._open.values():
            if pos["origin"] == "sim":
                continue
            by_key.setdefault((pos["symbol"], pos.get("tf") or "1h"), []).append(pos)
        closed_n = 0
        mono = time.monotonic()
        for (symbol, tf), positions in by_key.items():
            if not force and mono - self._advice_at.get(f"{symbol}|{tf}", -1e18) < ADVICE_EVERY_S:
                continue
            self._advice_at[f"{symbol}|{tf}"] = mono
            try:
                current = await asyncio.wait_for(nexus.current(symbol, tf), timeout=90)
            except Exception as exc:
                if self._log.should_log(f"advice:{symbol}", 600):
                    self._emit("debug", "paper", f"Sin consejo NEXUS para {symbol}: {type(exc).__name__}")
                continue
            if not isinstance(current, dict):
                continue
            for pos in positions:
                if pos["id"] not in self._open:
                    continue
                price = self._position_price(pos) or safe_float(current.get("price"))
                info = self.advise(pos["id"], current, price)
                if info is None:
                    continue
                if not info["early_exit"]:
                    self._warned.pop(pos["id"], None)
                    continue
                auto = pos["origin"] != "manual"  # the user's own trades are never closed for them: warn instead
                if auto and price is not None:
                    async with self._lock:
                        if pos["id"] in self._open:
                            await self._close(pos, price, "EARLY", "Cierre anticipado", now_s())
                            closed_n += 1
                    continue
                last = self._warned.get(pos["id"])
                if last is None or mono - last >= WARN_REPEAT_S:
                    self._warned[pos["id"]] = mono
                    self._emit(
                        "warn",
                        "paper",
                        f"⚠ {pos['symbol']}: NEXUS recomienda CERRAR el {SIDE_LABEL[pos['side']]} "
                        f"(EV {fmt_r(info['ev_r'])}) para no perder más",
                        {"position_id": pos["id"], "ev_r": info["ev_r"]},
                    )
        if closed_n:
            await self.publish(1)
        return closed_n

    # ------------------------------------------------------------------ auto-trading (account 1)

    async def _auto_check(self, symbol: str, tf: str) -> None:
        try:
            if not self._setting("paper.auto_trade", False) or self.rt.nexus is None:
                return
            if any(p["account_id"] == 1 and p["symbol"] == symbol for p in self._open.values()):
                return
            current = await asyncio.wait_for(self.rt.nexus.current(symbol, tf), timeout=90)
            if not isinstance(current, dict):
                return
            action = current.get("action")
            if action == "BUY":
                side = "long"
            elif action == "SELL" and self._setting("nexus.allow_short", True):
                side = "short"
            else:
                return
            gate = ((current.get("gate") or {}).get(side)) or None
            if not isinstance(gate, dict) or gate.get("verdict") != "TAKE":
                self._emit("info", "paper",
                           f"Auto: {symbol} {SIDE_LABEL[side]} descartado por el filtro ({gate_line(gate)})"
                           + (f": {gate.get('summary')}" if isinstance(gate, dict) and gate.get("summary") else ""))
                return
            await self.open_position(1, symbol, side, tf, origin="auto", gate=gate, check_gate=False)
        except (PaperError, PaperRejected) as exc:
            self._emit("warn", "paper", f"Auto-trading {symbol}: {exc}")
        except Exception as exc:
            if self._log.should_log(f"auto:{symbol}", 300):
                self._emit("error", "paper", f"Auto-trading {symbol}: {type(exc).__name__}: {exc}")

    async def set_auto(self, enabled: bool, symbols: list[str] | None = None, tf: str | None = None) -> dict:
        values: dict[str, Any] = {"paper.auto_trade": bool(enabled)}
        if symbols is not None:
            values["paper.auto_symbols"] = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if tf is not None:
            try:
                values["paper.auto_tf"] = normalize_tf(tf)
            except ValueError as exc:
                raise PaperError(str(exc)) from None
        if self.rt.settings is not None:
            await self.rt.settings.update(values)
        auto = {
            "enabled": bool(self._setting("paper.auto_trade", False)),
            "symbols": list(self._setting("paper.auto_symbols", []) or []),
            "tf": str(self._setting("paper.auto_tf", "1h")),
        }
        if auto["enabled"]:
            syms = ", ".join(auto["symbols"]) or "ningún símbolo (añade alguno)"
            self._emit("info", "paper", f"Auto-trading activado en {syms} ({auto['tf']}): solo señales TOMAR")
        else:
            self._emit("info", "paper", "Auto-trading desactivado")
        if config.BACKGROUND:
            await self._sync_subscriptions()
        await self.publish(1)
        return auto

    def _on_setting(self, key: str, value: Any) -> None:
        if key.startswith("paper.auto") and config.BACKGROUND and self._started:
            try:
                self._spawn(self._sync_subscriptions())
            except RuntimeError:
                pass

    async def _sync_subscriptions(self) -> None:
        hub = self.rt.hub
        if hub is None or not hasattr(hub, "subscribe"):
            return
        wanted: set[tuple[str, str, str]] = set()
        for pos in self._open.values():
            if pos["origin"] == "sim":
                continue
            tf = (pos.get("tf") or "1h") if pos["symbol"].startswith("SIM:") else "1m"
            wanted.add((pos["symbol"], tf, "paper"))
        if self._setting("paper.auto_trade", False):
            tf = str(self._setting("paper.auto_tf", "1h"))
            for s in self._setting("paper.auto_symbols", []) or []:
                wanted.add((str(s).upper(), tf, "paper:auto"))
        for item in wanted - self._subs:
            try:
                await call_maybe_async(hub.subscribe, *item)
                self._subs.add(item)
            except Exception as exc:
                if self._log.should_log(f"sub:{item[0]}", 600):
                    self._emit("warn", "paper", f"No se pudo seguir {item[0]} en vivo: {exc}")
        for item in self._subs - wanted:
            try:
                await call_maybe_async(hub.unsubscribe, *item)
            except Exception:
                pass
            self._subs.discard(item)

    # ------------------------------------------------------------------ background monitor

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(PUBLISH_EVERY_S)
                await self._poll_stale_prices()
                await self.refresh_advice()
                if any(p["account_id"] == 1 for p in self._open.values()):
                    await self.publish(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._log.should_log("monitor", 300):
                    self._emit("error", "paper", f"Monitor de posiciones: {type(exc).__name__}: {exc}")

    async def _poll_stale_prices(self) -> None:
        """Fallback when no live candles arrive: poll the last price and manage levels with it."""
        symbols = sorted({p["symbol"] for p in self._open.values()
                          if p["origin"] != "sim" and self.cached_price(p["symbol"], PRICE_STALE_S) is None})
        hub = self.rt.hub
        if not symbols or hub is None:
            return
        for sym in symbols:
            try:
                price = safe_float(await asyncio.wait_for(hub.last_price(sym), timeout=15))
            except Exception:
                price = None
            if price is None or price <= 0:
                continue
            self._prices[sym] = (price, time.monotonic())
            changed = False
            async with self._lock:
                for pos in [p for p in self._open.values() if p["symbol"] == sym and p["origin"] != "sim"]:
                    before = (pos["qty_open"], pos["tp_hits"])
                    await self._process_price(pos, price, price, price, open_=None, use_hl=False, time_s=now_s())
                    changed = changed or (pos["qty_open"], pos["tp_hits"]) != before
            if changed:
                await self.publish(1)


def _pos_from_row(row: dict) -> dict:
    meta = json_loads(row.get("meta"), {}) or {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": int(row["id"]),
        "account_id": int(row["account_id"]),
        "symbol": str(row["symbol"]).upper(),
        "tf": row.get("tf") or "1h",
        "side": row["side"],
        "qty": float(row["qty"]),
        "qty_open": float(row["qty_open"]),
        "entry_price": float(row["entry_price"]),
        "entry_time": int(row["entry_time"]),
        "sl": float(row["sl"]) if row.get("sl") is not None else float(row["entry_price"]),
        "tp1": safe_float(row.get("tp1")),
        "tp2": safe_float(row.get("tp2")),
        "tp3": safe_float(row.get("tp3")),
        "trail": safe_float(row.get("trail")),
        "tp_hits": int(row.get("tp_hits") or 0),
        "status": row.get("status") or "open",
        "exit_price": safe_float(row.get("exit_price")),
        "exit_time": int(row["exit_time"]) if row.get("exit_time") is not None else None,
        "pnl": float(row.get("pnl") or 0.0),
        "pnl_pct": float(row.get("pnl_pct") or 0.0),
        "r_multiple": safe_float(row.get("r_multiple")),
        "exit_reason": row.get("exit_reason"),
        "origin": row.get("origin") or "manual",
        "gate_verdict": row.get("gate_verdict"),
        "gate_score": safe_float(row.get("gate_score")),
        "meta": meta,
    }
