"""Watcher of the NexusService: on every closed bar of a watched market (watchlist at `watch.tf`, or any market
topic with WebSocket subscribers) it recomputes NEXUS on the bars up to the one that closed (the hub already holds
the seconds-old forming bar, which must not stand in for it), publishes `{"type": "nexus", ...}` on
`market:SYMBOL:tf`, records signals / predictions / gate decisions of that bar and emits Spanish events."""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from app import config
from app.engine import fmt
from app.util import clean_json, floor_time, normalize_tf, now_ms, tf_seconds

WATCH_SYNC_S = 60.0
ACTIONABLE = ("BUY", "SELL", "EXIT_LONG", "EXIT_SHORT")
CANDIDATE_KINDS = {"BUY": "long", "SELL": "short", "REJECTED_LONG": "long", "REJECTED_SHORT": "short"}


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def gate_target_ms(decision_time: int, tf: str, time_stop_bars: int) -> int:
    """gate_log.target_at (unix ms) of a decision taken at the close of the bar containing `decision_time`: the
    close of the `time_stop_bars`-th bar after it, the window over which the evaluator judges every source
    (app.sim.evaluator.gate_target_s) and the engine's own candidate horizon (TP1 before the SL within the time
    stop). One convention for the watcher, the paper account and the scanner; never the wall clock of the write."""
    return (floor_time(int(decision_time), tf) + (int(time_stop_bars) + 1) * tf_seconds(tf)) * 1000


def market_topic(symbol: str, tf: str) -> str:
    return f"market:{symbol.upper()}:{tf}"


def gate_text(gate: dict | None) -> str:
    if not gate:
        return "sin filtro"
    return f"{gate.get('verdict_label')} {fmt.num(gate.get('score'), 0)}/100"


class WatchMixin:
    rt: Any

    def _init_watch(self) -> None:
        self._watch: set[tuple[str, str]] = set()
        self._watch_task: asyncio.Task | None = None
        self._listener_on = False
        self._last_bar: dict[tuple[str, str], int] = {}
        self._last_action: dict[tuple[str, str], str] = {}
        self._processing: set[tuple[str, str]] = set()
        self._pending_bar: dict[tuple[str, str], int] = {}  # newest closed bar that arrived while processing
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle

    async def start_watch(self) -> None:
        hub = self.rt.hub
        if hub is None or not hasattr(hub, "add_listener"):
            return
        try:
            await _maybe_await(hub.add_listener(self._on_candle))
            self._listener_on = True
        except Exception as exc:
            self._emit("warn", f"No se pudo escuchar el flujo de velas: {exc}")
            return
        self._watch_task = asyncio.create_task(self._watch_loop(), name="nexus-watch")

    async def stop_watch(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(self._bg_tasks):
            t.cancel()
        self._pending_bar.clear()
        hub = self.rt.hub
        if hub is not None:
            for symbol, tf in list(self._watch):
                try:
                    await _maybe_await(hub.unsubscribe(symbol, tf, "nexus"))
                except Exception:
                    pass
            if self._listener_on and hasattr(hub, "remove_listener"):
                try:
                    await _maybe_await(hub.remove_listener(self._on_candle))
                except Exception:
                    pass
        self._watch.clear()
        self._listener_on = False

    def watching(self) -> list[dict]:
        return [{"symbol": s, "tf": tf} for s, tf in sorted(self._watch)]

    async def _watch_loop(self) -> None:
        while True:
            try:
                await self.sync_watch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._log.should_log("watch-sync", 600):
                    self._emit("warn", f"Vigilancia NEXUS: no se pudo sincronizar la lista ({exc})")
            await asyncio.sleep(WATCH_SYNC_S)

    async def sync_watch(self) -> None:
        """Subscribe the hub (owner 'nexus') for the watchlist at watch.tf; drop removed symbols."""
        hub = self.rt.hub
        if hub is None:
            return
        settings = self.rt.settings
        enabled = bool(settings.get("watch.enabled", True)) if settings is not None else True
        try:
            tf = normalize_tf(settings.get("watch.tf", "1h") if settings is not None else "1h")
        except ValueError:
            tf = "1h"
        desired: set[tuple[str, str]] = set()
        if enabled and config.BACKGROUND and hasattr(hub, "watchlist"):
            symbols = await hub.watchlist()
            desired = {(str(s).upper(), tf) for s in symbols or [] if s}
        for symbol, wtf in sorted(desired - self._watch):
            try:
                await _maybe_await(hub.subscribe(symbol, wtf, "nexus"))
            except Exception as exc:
                desired.discard((symbol, wtf))
                if self._log.should_log(f"sub:{symbol}", 3600):
                    self._emit("warn", f"Vigilancia NEXUS: no se pudo suscribir {symbol} {wtf}: {exc}")
        for symbol, wtf in sorted(self._watch - desired):
            try:
                await _maybe_await(hub.unsubscribe(symbol, wtf, "nexus"))
            except Exception:
                pass
        self._watch = desired

    # ------------------------------------------------------------------ live bars

    def _ws_listeners(self, symbol: str, tf: str) -> bool:
        ws = self.rt.ws
        try:
            return ws is not None and ws.subscribers(market_topic(symbol, tf)) > 0
        except Exception:
            return False

    async def _on_candle(self, symbol: str, tf: str, candle: dict, closed: bool) -> None:
        """Hub listener: schedule a recomputation on each new closed bar of an interesting market."""
        if not closed or not config.BACKGROUND or not isinstance(candle, dict):
            return
        key = (str(symbol).upper(), str(tf))
        if key not in self._watch and not self._ws_listeners(*key):
            return
        try:
            t = int(candle.get("time") or 0)
        except (TypeError, ValueError):
            return
        if self._last_bar.get(key, -1) >= t:
            return
        self._last_bar[key] = t
        if key in self._processing:  # handled right after the running one (only the newest closed bar)
            self._pending_bar[key] = t
            return
        self._start_processing(key, t)

    def _start_processing(self, key: tuple[str, str], t: int) -> None:
        self._processing.add(key)
        task = asyncio.create_task(self._process_guarded(key, t))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _process_guarded(self, key: tuple[str, str], t: int | None = None) -> None:
        try:
            await self.process_closed_bar(*key, upto=t)
        except asyncio.CancelledError:
            self._pending_bar.pop(key, None)
            raise
        except Exception as exc:
            if self._log.should_log(f"watch:{key}", 300):
                self._emit("error", f"NEXUS {key[0]} {key[1]}: fallo al recalcular ({type(exc).__name__}: {exc})")
        finally:
            self._processing.discard(key)
        nxt = self._pending_bar.pop(key, None)
        if nxt is not None and (t is None or nxt > t) and config.BACKGROUND:
            self._start_processing(key, nxt)

    async def process_closed_bar(self, symbol: str, tf: str, upto: int | None = None) -> dict | None:
        """Recompute after a closed bar, publish it and persist signals / predictions / gate decisions.

        `upto` = open time of the bar that closed: the engine runs on the bars up to it, so the returned `current`,
        the markers, signals, predictions, gate_log rows and events describe the decision taken at its close
        (None = latest bars). The chart's message stays the live view (forming bar included, like /api/nexus),
        computed after that decision and any semáforo veto of it are known."""
        resp = await self._resolve(symbol, tf, force=True, upto=upto)  # + semáforo (event windows / volatility)
        if not resp.get("ok") or not resp.get("current"):
            return None
        symbol, tf = resp["symbol"], resp["tf"]
        cur = resp["current"]
        if self.rt.ws is not None:
            view = (await self._live_view(symbol, tf) if upto is not None else None) or resp
            self.rt.ws.publish(market_topic(symbol, tf), {"type": "nexus", "symbol": symbol, "tf": tf,
                                                          "current": view["current"],
                                                          "markers": view.get("markers") or []})
        markers_now = [m for m in resp.get("markers") or [] if m.get("time") == cur["time"]]
        await self._record(symbol, tf, resp, markers_now)
        return resp

    async def _live_view(self, symbol: str, tf: str) -> dict | None:
        """Live NexusResponse for the chart's WebSocket topic; None when no chart listens (a publish without
        subscribers reaches nobody, so it is not computed) or it is not usable."""
        if not self._ws_listeners(symbol, tf):
            return None
        try:
            live = await self._resolve(symbol, tf)
        except Exception:
            return None
        return live if live.get("ok") and live.get("current") else None

    # ------------------------------------------------------------------ persistence + events

    async def _record(self, symbol: str, tf: str, resp: dict, markers_now: list[dict]) -> None:
        db = self.rt.db
        cur = resp["current"]
        key = (symbol, tf)
        action = cur["action"]
        prev = self._last_action.get(key)
        self._last_action[key] = action
        now = now_ms()
        tf_ms = tf_seconds(tf) * 1000
        params = resp.get("params") or {}
        gates = cur.get("gate") or {}
        if db is not None:
            if action != prev and action in ACTIONABLE:
                await self._write_signal(symbol, tf, cur, gates, now)
            await self._write_prediction(symbol, tf, cur, int(params.get("horizon") or 5), now, tf_ms)
            await self._write_gate_log(symbol, tf, cur, gates, markers_now, now,
                                       gate_target_ms(int(cur["time"]), tf, int(params.get("time_stop_bars") or 48)))
        self._events(symbol, tf, cur, gates, markers_now, action != prev)

    async def _write_signal(self, symbol: str, tf: str, cur: dict, gates: dict, now: int) -> None:
        action = cur["action"]
        gate = gates.get("long") if action == "BUY" else gates.get("short") if action == "SELL" else None
        pos = cur.get("position") or {}
        src = gate if gate else pos
        try:
            await self.rt.db.execute(
                "INSERT OR IGNORE INTO signals(symbol, tf, time, action, score, prob_up, confidence, price, sl, tp1, "
                "tp2, tp3, regime, verdict, gate_score, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, tf, int(cur["time"]), action, cur.get("score"), cur.get("prob_up"), cur.get("confidence"),
                 cur.get("price"), src.get("sl"), src.get("tp1"), src.get("tp2"), src.get("tp3"), cur.get("regime"),
                 gate.get("verdict") if gate else None, gate.get("score") if gate else None,
                 cur.get("advice"), now),
            )
        except Exception as exc:
            if self._log.should_log("signal-write", 300):
                self._emit("warn", f"No se pudo guardar la señal de {symbol}: {exc}")

    async def _write_prediction(self, symbol: str, tf: str, cur: dict, horizon: int, now: int, tf_ms: int) -> None:
        p = float(cur.get("prob_up") or 0.5)
        direction = 1 if p > 0.52 else -1 if p < 0.48 else 0
        try:
            recent = await self.rt.db.fetchval(
                "SELECT COUNT(*) FROM predictions WHERE source='nexus' AND symbol=? AND tf=? AND created_at > ?",
                (symbol, tf, now - tf_ms // 2), default=0,
            )
            if int(recent or 0) > 0:
                return
            await self.rt.db.execute(
                "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, "
                "prob_up, score, features) VALUES ('nexus', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, tf, f"{symbol}|{tf}", now, now + max(horizon, 1) * tf_ms, float(cur["price"]), direction,
                 p, float(cur.get("score") or 0.0),
                 json.dumps({"bar_time": int(cur["time"]), "regime": cur.get("regime")})),
            )
        except Exception as exc:
            if self._log.should_log("pred-write", 300):
                self._emit("warn", f"No se pudo guardar la predicción de {symbol}: {exc}")

    async def _write_gate_log(self, symbol: str, tf: str, cur: dict, gates: dict, markers_now: list[dict],
                              now: int, target_at: int) -> None:
        seen: set[str] = set()
        for m in markers_now:
            direction = CANDIDATE_KINDS.get(str(m.get("kind")))
            gate = gates.get(direction) if direction else None
            if not gate or direction in seen:
                continue
            seen.add(direction)
            checks = {c["id"]: (None if c.get("passed") is None else (1 if c["passed"] else 0))
                      for c in gate.get("checks") or [] if isinstance(c, dict) and c.get("id")}
            try:
                await self.rt.db.execute(
                    "INSERT OR IGNORE INTO gate_log(symbol, tf, time, direction, verdict, score, win_prob, entry, sl, "
                    "tp1, tp2, checks, taken, source, created_at, target_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'watch', ?, ?)",
                    (symbol, tf, int(cur["time"]), direction, gate.get("verdict"), float(gate.get("score") or 0.0),
                     gate.get("win_prob"), gate.get("entry"), gate.get("sl"), gate.get("tp1"), gate.get("tp2"),
                     json.dumps(clean_json(checks)), 1 if str(m.get("kind")) in ("BUY", "SELL") else 0, now,
                     target_at),
                )
            except Exception as exc:
                if self._log.should_log("gate-write", 300):
                    self._emit("warn", f"No se pudo registrar la decisión del filtro de {symbol}: {exc}")

    def _events(self, symbol: str, tf: str, cur: dict, gates: dict, markers_now: list[dict], changed: bool) -> None:
        action = cur["action"]
        label = cur.get("action_label") or action
        head = f"{symbol} {tf} → {label}"
        if action in ("BUY", "SELL") and changed:
            gate = gates.get("long" if action == "BUY" else "short")
            self._emit("signal", f"{head} · score {fmt.num(cur.get('score'), 2, True)} · p↑ {fmt.prob(cur.get('prob_up'))}"
                                 f" · Filtro: {gate_text(gate)}", {"symbol": symbol, "tf": tf, "action": action})
        elif action in ("EXIT_LONG", "EXIT_SHORT") and changed:
            self._emit("signal", f"{head} · {cur.get('advice')}", {"symbol": symbol, "tf": tf, "action": action})
        early = next((w for w in cur.get("warnings") or [] if w.get("code") == "EARLY_EXIT"
                      and w.get("severity") == "critical"), None)
        if early is not None and (cur.get("position") or action in ("EXIT_LONG", "EXIT_SHORT")):
            self._emit("warn", f"⚠ {symbol} {tf}: {cur.get('advice')}", {"symbol": symbol, "tf": tf, "code": "EARLY_EXIT"})
        for m in markers_now:
            kind = str(m.get("kind"))
            if kind in ("REJECTED_LONG", "REJECTED_SHORT"):
                side = "compra" if kind == "REJECTED_LONG" else "venta"
                gate = gates.get("long" if kind == "REJECTED_LONG" else "short")
                reason = ((gate or {}).get("reasons_against") or [(gate or {}).get("summary") or ""])[0]
                self._emit("info", f"{symbol} {tf}: señal de {side} rechazada por el filtro ({gate_text(gate)})"
                                   + (f" — {reason}" if reason else ""), {"symbol": symbol, "tf": tf, "kind": kind})
