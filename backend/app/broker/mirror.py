"""Mirror of NEXUS automática in an Alpaca paper (practice) account (`rt.broker`).

NEXUS automática (demo account 3) decides and simulates; the mirror only repeats, in the user's Alpaca PRACTICE
account, what it holds open, so the simulated prices can be compared with the fills of a real order book (paper
orders fill against live quotes, without money): the difference is the slippage the simulation does not see. Off by
default; it needs the user's paper keys (secrets) and only talks to app.broker.alpaca.PAPER_URL. Nothing is mirrored
in NEXUS_OFFLINE mode.

Reconcile every SYNC_S seconds (in the background, or on request): each new position of account 3 that Alpaca can
trade (US stocks and ETFs, crypto quoted in dollars) gets a market order with the same quantity and direction; when
NEXUS takes a partial target the mirror sells the same share of what it bought, and when NEXUS closes the position
the mirror closes its part. One working order per position at a time, with a deterministic client_order_id
("nexus-<position>-<step>"), so a restart never doubles an order. What is not mirrored is recorded with its reason:
options (theoretical), futures and other classes, synthetic markets, crypto shorts (Alpaca has none), shorts of less
than one share (no fractional shorts), orders under MIN_NOTIONAL, a stock position opened while the US market is
closed (a market order would fill at the next open: another trade), positions older than MAX_LAG_S (opened before
the mirror was on, or while it was off).

State in table `meta` (key broker.mirror: the open entries and the last CLOSED_KEEP finished ones), no schema change.
Statistics as «x de n» with the mean price difference in basis points (positive = worse than the simulation) and
no conclusion below 30 trades.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from typing import Any, Awaitable, Callable

from app import config
from app.broker.alpaca import AlpacaClient, AlpacaError
from app.data.symbols import US_STOCK_EXCHANGES, is_sim, split_pair, strip_derivative
from app.engine.costs import asset_class
from app.sim.common import fmt_qty, json_dumps, json_loads
from app.util import RateLimitedLog, now_ms, now_s, safe_float

AUTOPILOT_ACCOUNT = 3  # app.sim.paper.AUTOPILOT_ACCOUNT (NEXUS's own demo account)
SYNC_S = 15.0
MAX_LAG_S = 120  # a position older than this (or closed longer ago) is not compared: the price moved on
ACCOUNT_TTL_S = 30.0
STATE_KEY = "broker.mirror"
CLOSED_KEEP = 100
SHOW_ENTRIES = 20
MIN_NOTIONAL = 1.0  # $: Alpaca's minimum for fractional orders
MIN_SAMPLE = 30
QTY_EPS = 1e-9
MAX_RETRIES = 3
CRYPTO_QUOTES = ("USD", "USDT", "USDC")
FINAL = ("closed", "skipped", "rejected", "error")
DEAD_ORDERS = ("canceled", "expired", "rejected", "done_for_day", "replaced", "suspended")
STATUS_LABEL = {
    "working": "Orden enviada",
    "open": "Abierta en Alpaca",
    "waiting": "Esperando a que abra el mercado",
    "closed": "Cerrada",
    "skipped": "No replicada",
    "rejected": "Rechazada por Alpaca",
    "error": "Con error",
}
ORDER_STATUS_ES = {"canceled": "cancelada", "expired": "caducada", "rejected": "rechazada",
                   "done_for_day": "sin ejecutar al cierre del día", "replaced": "sustituida", "suspended": "suspendida"}
NOTE = ("Cuenta de práctica de Alpaca (paper trading): órdenes con cotizaciones reales y sin dinero real. NEXUS "
        "automática decide y simula; el espejo solo repite sus posiciones para medir la diferencia de precio que la "
        "simulación no ve. Con menos de 30 operaciones, sin conclusiones. No es asesoría financiera.")


class MirrorError(Exception):
    """The mirror cannot run now (off, no keys, offline): a Spanish message for the user."""


def alpaca_symbol(symbol: str, *, stocks: bool = True, crypto: bool = True) -> tuple[str | None, str, str]:
    """(Alpaca symbol, kind "stock" | "crypto", "") or (None, "", why it is not mirrored)."""
    sym = str(symbol or "").strip().upper()
    if not sym or is_sim(sym):
        return None, "", "mercado sintético: no existe en Alpaca"
    cls = asset_class(sym)
    exchange, _, ticker = sym.partition(":")
    if not ticker:
        exchange, ticker = "", exchange
    if cls == "option":
        return None, "", "opción teórica de NEXUS automática: no se replica"
    if cls in ("stock", "fund"):
        if not stocks:
            return None, "", "replicar acciones está desactivado"
        if exchange not in US_STOCK_EXCHANGES or exchange == "OTC":
            return None, "", f"bolsa {exchange or '?'}: Alpaca solo opera acciones y ETF de EE. UU."
        return ticker, "stock", ""
    if cls == "crypto":
        if not crypto:
            return None, "", "replicar cripto está desactivado"
        base, derivative = strip_derivative(ticker)
        if derivative:
            return None, "", "derivado (perpetuo o futuro): Alpaca solo tiene cripto al contado"
        pair = split_pair(base)
        if not pair or pair[1] not in CRYPTO_QUOTES:
            return None, "", "par de cripto que no cotiza en dólares"
        return f"{pair[0]}/USD", "crypto", ""
    return None, "", f"clase de activo «{cls}»: Alpaca no la opera"


def _bps(side: str, sim: float | None, fill: float | None, *, entry: bool) -> float | None:
    """Price difference with the simulation in basis points, positive = worse for the trade."""
    if not sim or not fill or sim <= 0:
        return None
    s = 1.0 if side != "short" else -1.0
    diff = (fill - sim) if entry else (sim - fill)
    return round(s * diff / sim * 1e4, 2)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


class MirrorService:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self._entries: dict[int, dict] = {}
        self._closed: deque[dict] = deque(maxlen=CLOSED_KEEP)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._log = RateLimitedLog()
        self._error: str | None = None
        self._last_sync_ms: int | None = None
        self._account: dict | None = None
        self._account_at = -1e18
        self._account_key = ""

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self._load()
        if config.BACKGROUND:
            self._task = asyncio.create_task(self._loop(), name="broker-mirror")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._save()

    def status(self) -> dict:
        """Short JSON-safe status for /api/health and the terminal's `status` (never the keys)."""
        cfg = self._cfg()
        return {"running": self._task is not None and not self._task.done() if config.BACKGROUND else True,
                "enabled": cfg["enabled"], "configured": cfg["configured"],
                "open": sum(1 for e in self._entries.values() if e["status"] in ("open", "working", "waiting")),
                "error": self._error}

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(SYNC_S)
            try:
                if self._cfg()["ready"]:
                    await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self._error = f"Error interno del espejo: {type(exc).__name__}"
                if self._log.should_log("loop", 600):
                    self._emit("error", f"Espejo en Alpaca: error interno: {type(exc).__name__}: {str(exc)[:200]}")

    # ------------------------------------------------------------------ settings

    def _setting(self, key: str, default: Any = None) -> Any:
        settings = getattr(self.rt, "settings", None)
        if settings is None:
            from app.config import DEFAULT_SETTINGS

            return DEFAULT_SETTINGS.get(key, default)
        return settings.get(key, default)

    def _cfg(self) -> dict[str, Any]:
        key_id = str(self._setting("broker.alpaca_key_id", "") or "").strip()
        secret = str(self._setting("broker.alpaca_secret_key", "") or "").strip()
        enabled = bool(self._setting("broker.alpaca_enabled", False))
        configured = bool(key_id and secret)
        return {"enabled": enabled, "configured": configured, "ready": enabled and configured and not config.OFFLINE,
                "key_id": key_id, "secret": secret,
                "stocks": bool(self._setting("broker.mirror_stocks", True)),
                "crypto": bool(self._setting("broker.mirror_crypto", True))}

    def _client(self, cfg: dict[str, Any]) -> AlpacaClient:
        return AlpacaClient(cfg["key_id"], cfg["secret"])

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        bus = getattr(self.rt, "bus", None)
        if bus is not None:
            bus.emit(level, "broker", message, data)

    # ------------------------------------------------------------------ state

    async def _load(self) -> None:
        db = getattr(self.rt, "db", None)
        if db is None:
            return
        try:
            raw = await db.fetchval("SELECT value FROM meta WHERE key=?", (STATE_KEY,))
        except Exception:
            raw = None
        state = json_loads(raw, None)
        if not isinstance(state, dict):
            return
        for e in state.get("entries") or []:
            if isinstance(e, dict) and isinstance(e.get("pos_id"), int):
                self._entries[e["pos_id"]] = e
        for e in state.get("closed") or []:
            if isinstance(e, dict):
                self._closed.append(e)

    async def _save(self) -> None:
        db = getattr(self.rt, "db", None)
        if db is None:
            return
        state = {"entries": list(self._entries.values()), "closed": list(self._closed)}
        try:
            await db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (STATE_KEY, json_dumps(state)))
        except Exception as exc:
            if self._log.should_log("save", 600):
                self._emit("warn", f"Espejo en Alpaca: no se pudo guardar su estado: {exc}")

    # ------------------------------------------------------------------ reconcile

    async def sync(self) -> None:
        """One reconcile pass. Connection problems are kept in `error` (and logged at most every 10 min)."""
        cfg = self._cfg()
        if not cfg["ready"] or getattr(self.rt, "paper", None) is None:
            return
        async with self._lock:
            client = self._client(cfg)
            cache: dict[str, Any] = {}

            async def market_open() -> bool:
                if "clock" not in cache:
                    cache["clock"] = await client.clock()
                return bool(cache["clock"].get("is_open"))

            async def held_in_alpaca(symbol: str) -> float | None:
                if "positions" not in cache:
                    cache["positions"] = {str(p.get("symbol") or "").replace("/", ""): safe_float(p.get("qty"))
                                          for p in await client.positions()}
                return cache["positions"].get(symbol.replace("/", ""))

            try:
                positions = {int(p["id"]): p for p in self.rt.paper.open_positions(AUTOPILOT_ACCOUNT)}
                for pid, pos in positions.items():
                    if pid not in self._entries:
                        self._entries[pid] = await self._new(pos, cfg, client, market_open)
                for pid, e in list(self._entries.items()):
                    if e["status"] not in FINAL:
                        await self._step(e, positions.get(pid), client, market_open, held_in_alpaca)
                    if e["status"] in FINAL and pid not in positions:
                        self._closed.append(self._entries.pop(pid))
                self._error = None
            except AlpacaError as exc:
                self._error = str(exc)
                if self._log.should_log(f"sync:{exc.status}", 600):
                    self._emit("warn", f"Espejo en Alpaca: {exc}")
            finally:
                self._last_sync_ms = now_ms()
                await self._save()

    async def _new(self, pos: dict, cfg: dict, client: AlpacaClient,
                   market_open: Callable[[], Awaitable[bool]]) -> dict:
        side = "short" if pos.get("side") == "short" else "long"
        e: dict[str, Any] = {
            "pos_id": int(pos["id"]), "symbol": pos["symbol"], "alpaca_symbol": None, "kind": None, "side": side,
            "sim_qty": float(pos.get("qty") or 0.0), "sim_entry": float(pos.get("entry_price") or 0.0),
            "sim_exit": None, "sim_result_pct": None, "sim_closed_s": None,
            "bought": 0.0, "held": 0.0, "fill_entry": None, "fill_exit": None, "fill_close": None,
            "exit_qty": 0.0, "exit_value": 0.0, "slip_entry_bps": None, "slip_exit_bps": None, "late": False,
            "order": None, "step": 0, "retries": 0, "status": "working", "reason": None,
            "opened_at": now_ms(), "closed_at": None,
        }
        sym, kind, why = alpaca_symbol(pos["symbol"], stocks=cfg["stocks"], crypto=cfg["crypto"])
        age = now_s() - int(safe_float(pos.get("entry_time")) or 0)
        qty = e["sim_qty"]
        if sym and kind == "stock" and side == "short":
            qty = float(math.floor(qty + QTY_EPS))
        reason = (why if not sym
                  else f"abierta hace más de {MAX_LAG_S // 60} minutos (antes de activar el espejo o con él apagado)"
                  if age > MAX_LAG_S
                  else "Alpaca no admite cortos en cripto" if kind == "crypto" and side == "short"
                  else "corto de menos de 1 acción: Alpaca no admite cortos fraccionados" if qty < QTY_EPS
                  else f"orden de menos de {MIN_NOTIONAL:.0f} $" if qty * e["sim_entry"] < MIN_NOTIONAL
                  else None)
        e.update({"alpaca_symbol": sym, "kind": kind or None})
        if reason is None and kind == "stock" and not await market_open():
            reason = "mercado de EE. UU. cerrado: una orden a mercado se ejecutaría en la próxima apertura"
        if reason is not None:
            e.update({"status": "skipped", "reason": reason, "closed_at": now_ms()})
            return e
        await self._submit(e, client, "open", qty)
        return e

    async def _submit(self, e: dict, client: AlpacaClient, purpose: str, qty: float) -> None:
        e["step"] += 1
        cid = f"nexus-{e['pos_id']}-{purpose[0]}{e['step']}"
        opening = purpose == "open"
        buy = (e["side"] == "long") == opening
        try:
            order = await client.submit_market(symbol=e["alpaca_symbol"], side="buy" if buy else "sell", qty=qty,
                                               tif="day" if e["kind"] == "stock" else "gtc", client_order_id=cid)
        except AlpacaError as exc:
            if exc.status in (0, 401):
                e["step"] -= 1  # nothing was sent: the same id next time
                raise
            if exc.status == 422 and "client_order_id" in str(exc).lower():  # sent before a restart: the same order
                order = await client.order_by_client_id(cid)
            else:
                self._failed(e, purpose, str(exc))
                return
        e["order"] = {"id": order.get("id"), "cid": cid, "purpose": purpose, "qty": qty,
                      "status": str(order.get("status") or "new")}
        e["status"] = "working"
        what = {"open": "abre", "reduce": "reduce", "close": "cierra"}[purpose]
        self._emit("info", f"Espejo en Alpaca: {what} {e['alpaca_symbol']} ({'compra' if buy else 'venta'} de "
                           f"{fmt_qty(qty)}) como NEXUS automática", {"pos_id": e["pos_id"], "client_order_id": cid})

    def _failed(self, e: dict, purpose: str, reason: str) -> None:
        if purpose == "open":
            e.update({"status": "rejected", "reason": reason, "closed_at": now_ms()})
            return
        e["retries"] += 1
        if e["retries"] >= MAX_RETRIES:
            e.update({"status": "error", "closed_at": now_ms(),
                      "reason": f"no se pudo cerrar en Alpaca ({reason}): ciérrala a mano en tu cuenta de práctica"})
        else:
            e.update({"status": "open", "reason": reason})

    async def _step(self, e: dict, pos: dict | None, client: AlpacaClient,
                    market_open: Callable[[], Awaitable[bool]],
                    held_in_alpaca: Callable[[str], Awaitable[float | None]]) -> None:
        """Check its working order, then follow NEXUS; an order sent here is checked once more right away (a market
        order usually fills at once, so its fill counts in this same pass)."""
        for _ in range(2):
            order = e.get("order")
            if order:
                data = await (client.order(order["id"]) if order.get("id") else client.order_by_client_id(order["cid"]))
                status = str(data.get("status") or "")
                order["status"] = status
                filled = safe_float(data.get("filled_qty")) or 0.0
                price = safe_float(data.get("filled_avg_price"))
                if status == "filled" or (status in DEAD_ORDERS and filled > QTY_EPS):
                    e["order"] = None
                    await self._filled(e, order["purpose"], filled, price)
                elif status in DEAD_ORDERS:
                    e["order"] = None
                    self._failed(e, order["purpose"], f"orden {ORDER_STATUS_ES.get(status, status)} por Alpaca")
                    return
                else:
                    return  # still working: the next pass looks again
                if e["status"] in FINAL:
                    return
            if not await self._follow(e, pos, client, market_open, held_in_alpaca):
                return

    async def _follow(self, e: dict, pos: dict | None, client: AlpacaClient,
                      market_open: Callable[[], Awaitable[bool]],
                      held_in_alpaca: Callable[[str], Awaitable[float | None]]) -> bool:
        """With no working order: follow NEXUS's partial exits and its close. True when it sent an order."""
        if pos is not None:  # NEXUS still holds it: follow its partial exits
            share = float(pos.get("qty_open") or 0.0) / e["sim_qty"] if e["sim_qty"] > 0 else 1.0
            excess = e["held"] - e["bought"] * min(max(share, 0.0), 1.0)
            if e["kind"] == "stock" and e["side"] == "short":
                excess = float(math.floor(excess + QTY_EPS))
            price = e["fill_entry"] or e["sim_entry"]
            if excess > QTY_EPS and excess * price >= MIN_NOTIONAL:
                if e["kind"] == "stock" and not await market_open():
                    e.update({"status": "waiting", "reason": "salida parcial pendiente: el mercado de EE. UU. está "
                                                             "cerrado"})
                    return False
                await self._submit(e, client, "reduce", await self._sellable(e, excess, held_in_alpaca))
                return e.get("order") is not None
            e.update({"status": "open", "reason": None if e["status"] == "waiting" else e["reason"]})
            return False
        # NEXUS closed it: close what the mirror holds
        if e["sim_closed_s"] is None:
            await self._sim_close(e)
        if e["held"] <= QTY_EPS:
            self._finish(e)
            return False
        if e["kind"] == "stock" and not await market_open():
            e.update({"status": "waiting", "reason": "cierre pendiente: el mercado de EE. UU. está cerrado"})
            return False
        qty = await self._sellable(e, e["held"], held_in_alpaca)
        if qty <= QTY_EPS:
            e["held"] = 0.0
            self._finish(e)
            return False
        if now_s() - int(e["sim_closed_s"] or now_s()) > MAX_LAG_S:
            e["late"] = True  # closed long after NEXUS: its price is not comparable
        await self._submit(e, client, "close", qty)
        return e.get("order") is not None

    async def _sellable(self, e: dict, qty: float, held_in_alpaca: Callable[[str], Awaitable[float | None]]) -> float:
        """Crypto: Alpaca takes its fee from the coins bought, so never sell more than the account holds."""
        if e["kind"] != "crypto" or e["side"] != "long":
            return qty
        held = await held_in_alpaca(e["alpaca_symbol"])
        return min(qty, held) if held is not None else qty

    async def _filled(self, e: dict, purpose: str, qty: float, price: float | None) -> None:
        if purpose == "open":
            e.update({"bought": qty, "held": qty, "fill_entry": price, "status": "open", "reason": None,
                      "slip_entry_bps": _bps(e["side"], e["sim_entry"], price, entry=True)})
            return
        e["held"] = max(e["held"] - qty, 0.0)
        if price:
            e["exit_qty"] += qty
            e["exit_value"] += qty * price
            e["fill_exit"] = e["exit_value"] / e["exit_qty"]
        if purpose == "close":
            e["fill_close"] = price
            if e["sim_closed_s"] is None:
                await self._sim_close(e)
            if not e["late"]:
                e["slip_exit_bps"] = _bps(e["side"], e["sim_exit"], price, entry=False)
        if purpose == "close" or e["held"] <= QTY_EPS:
            self._finish(e)
        else:
            e["status"] = "open"

    async def _sim_close(self, e: dict) -> None:
        """NEXUS's own exit of the position: its price, time and result % (after its costs)."""
        try:
            row = await self.rt.db.fetchone(
                "SELECT exit_price, exit_time, pnl, qty, entry_price FROM paper_positions WHERE id=?", (e["pos_id"],))
        except Exception:
            row = None
        row = dict(row) if row else {}
        e["sim_exit"] = safe_float(row.get("exit_price"))
        e["sim_closed_s"] = int(safe_float(row.get("exit_time")) or now_s())
        notional = (safe_float(row.get("qty")) or 0.0) * (safe_float(row.get("entry_price")) or 0.0)
        pnl = safe_float(row.get("pnl"))
        e["sim_result_pct"] = round(pnl / notional * 100.0, 4) if pnl is not None and notional > 0 else None

    def _finish(self, e: dict) -> None:
        e.update({"status": "closed", "closed_at": now_ms(), "order": None})
        if e["late"]:
            e["reason"] = "cerrada tarde (el espejo estaba apagado o el servidor parado): sin comparación de salida"

    # ------------------------------------------------------------------ read

    @staticmethod
    def _result_pct(e: dict) -> float | None:
        entry, exit_ = e.get("fill_entry"), e.get("fill_exit")
        if not entry or not exit_ or entry <= 0:
            return None
        s = 1.0 if e.get("side") != "short" else -1.0
        return round(s * (exit_ - entry) / entry * 100.0, 4)

    def _public(self, e: dict) -> dict:
        return {
            "pos_id": e["pos_id"], "symbol": e["symbol"], "alpaca_symbol": e.get("alpaca_symbol"), "side": e["side"],
            "status": e["status"], "status_label": STATUS_LABEL.get(e["status"], e["status"]), "reason": e.get("reason"),
            "sim_qty": e.get("sim_qty"), "held": round(float(e.get("held") or 0.0), 9),
            "sim_entry": e.get("sim_entry"), "fill_entry": e.get("fill_entry"),
            "sim_exit": e.get("sim_exit"), "fill_exit": e.get("fill_exit"),
            "slip_entry_bps": e.get("slip_entry_bps"), "slip_exit_bps": e.get("slip_exit_bps"),
            "sim_result_pct": e.get("sim_result_pct"), "mirror_result_pct": self._result_pct(e),
            "opened_at": e.get("opened_at"), "closed_at": e.get("closed_at"),
        }

    def stats(self) -> dict[str, Any]:
        everything = [*self._entries.values(), *self._closed]
        mirrored = [e for e in everything if e.get("fill_entry")]
        closed = [e for e in self._closed if e.get("status") == "closed" and e.get("fill_entry")]
        slip_in = [e["slip_entry_bps"] for e in mirrored if e.get("slip_entry_bps") is not None]
        slip_out = [e["slip_exit_bps"] for e in closed if e.get("slip_exit_bps") is not None]
        diffs = [r - e["sim_result_pct"] for e in closed
                 if (r := self._result_pct(e)) is not None and e.get("sim_result_pct") is not None and not e["late"]]
        out = {
            "entries": len(everything), "mirrored": len(mirrored),
            "skipped": sum(1 for e in everything if e.get("status") == "skipped"),
            "rejected": sum(1 for e in everything if e.get("status") in ("rejected", "error")),
            "closed": len(closed),
            "slip_entry_bps": _mean(slip_in), "slip_entry_n": len(slip_in),
            "slip_exit_bps": _mean(slip_out), "slip_exit_n": len(slip_out),
            "result_diff_pct": _mean(diffs), "result_n": len(diffs),
        }
        parts = [f"{out['mirrored']} de {out['entries']} posiciones replicadas"] if everything else [
            "Todavía no hay posiciones de NEXUS automática que replicar"]
        for label, value, n in (("entrada", out["slip_entry_bps"], len(slip_in)),
                                ("salida", out["slip_exit_bps"], len(slip_out))):
            if value is not None:
                word = "igual" if value == 0 else "peor" if value > 0 else "mejor"
                amount = f"{abs(value):.1f}".replace(".", ",")
                parts.append(f"{label}: {amount} pb {word} que la simulación de media ({n})")
        if everything and len(closed) < MIN_SAMPLE:
            parts.append(f"muestra pequeña ({len(closed)} {'cerrada' if len(closed) == 1 else 'cerradas'}): sin "
                         "conclusiones")
        out["text"] = " · ".join(parts)
        return out

    async def overview(self, refresh: bool = False) -> dict[str, Any]:
        """BrokerStatus: settings (never the keys), the practice account, the mirrored positions and the stats."""
        cfg = self._cfg()
        active = sorted(self._entries.values(), key=lambda e: e["pos_id"], reverse=True)
        done = sorted(self._closed, key=lambda e: e.get("closed_at") or 0, reverse=True)
        out: dict[str, Any] = {
            "enabled": cfg["enabled"], "provider": "alpaca", "paper": True, "configured": cfg["configured"],
            "connected": False, "account": None, "error": None, "last_sync_ms": self._last_sync_ms,
            "mirror_stocks": cfg["stocks"], "mirror_crypto": cfg["crypto"],
            "entries": [self._public(e) for e in [*active, *done][:SHOW_ENTRIES]],
            "stats": self.stats(), "note": NOTE,
        }
        if not cfg["enabled"]:
            return out
        if not cfg["configured"]:
            out["error"] = "Faltan las claves de tu cuenta de práctica: Ajustes → Bróker de práctica (Alpaca)."
            return out
        if config.OFFLINE:
            out["error"] = "Modo sin conexión (NEXUS_OFFLINE): el espejo no envía nada."
            return out
        try:
            acct = await self._account_info(cfg, refresh)
            out["connected"] = True
            out["account"] = {"equity": safe_float(acct.get("equity")), "cash": safe_float(acct.get("cash")),
                              "buying_power": safe_float(acct.get("buying_power")),
                              "status": str(acct.get("status") or ""), "currency": str(acct.get("currency") or "USD")}
        except AlpacaError as exc:
            out["error"] = str(exc)
        if self._error and not out["error"]:
            out["error"] = self._error
        return out

    async def sync_now(self) -> dict[str, Any]:
        """A reconcile pass on request (POST /api/broker/sync, `espejo sincroniza`), then the fresh BrokerStatus."""
        cfg = self._cfg()
        if not cfg["enabled"]:
            raise MirrorError("El espejo está desactivado: actívalo en Ajustes → Bróker de práctica (Alpaca).")
        if not cfg["configured"]:
            raise MirrorError("Faltan las claves de tu cuenta de práctica: Ajustes → Bróker de práctica (Alpaca).")
        if config.OFFLINE:
            raise MirrorError("Modo sin conexión (NEXUS_OFFLINE): el espejo no envía nada.")
        await self.sync()
        return await self.overview(refresh=True)

    async def _account_info(self, cfg: dict, refresh: bool) -> dict:
        fresh = time.monotonic() - self._account_at < ACCOUNT_TTL_S and self._account_key == cfg["key_id"]
        if self._account is None or refresh or not fresh:
            self._account = await self._client(cfg).account()
            self._account_at = time.monotonic()
            self._account_key = cfg["key_id"]
        return self._account
