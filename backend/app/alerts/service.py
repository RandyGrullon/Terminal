"""AlertService (`rt.alerts`): alerts outside the app, so a red semáforo, a NEXUS entry / exit of a watched symbol or
a demo stop reach the user when the tab is closed or hidden.

Flow: a bus listener (sync, cheap) turns each event into an `Alert` (app.alerts.classify) and queues it; one worker
collects the alerts that arrive together (BATCH_WINDOW_S, e.g. every watched market closing the same hour) and, after
the user's filters (category switches, minimum priority, watched symbols), dedupe, quiet hours and the hourly limit
(app.alerts.policy), sends ONE short Spanish message to every active channel (app.alerts.channels). The worker only
reacts to events (no polling), so it also runs with NEXUS_BACKGROUND=0; NEXUS_OFFLINE=1 sends nothing.

Counters, the last results per channel, the last test and the recent alerts are kept in model_state
(kind 'alerts', key 'state') so they survive restarts; the dedupe memory too (no repeated message after a deploy).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Any, Callable

from app import config
from app.alerts.channels import ChannelError, ChannelInfo, EmailChannel, TelegramChannel
from app.alerts.classify import CATEGORY_INLINE, PRIORITY_LABEL, Alert, classify, collapse, compose, top_priority
from app.alerts.policy import Prefs, Policy, symbol_matches
from app.util import RateLimitedLog, clean_json, normalize_tf, now_ms

STATE_KIND, STATE_KEY = "alerts", "state"
STATE_VERSION = 1
BATCH_WINDOW_S = 1.5  # alerts arriving within this window go out as one message
MAX_BATCH = 12
QUEUE_MAX = 500
RECENT_KEEP = 30
SEND_TIMEOUT_S = 45.0  # per channel and message (SMTP can be slow; Telegram retries one 429)
TEST_COOLDOWN_S = 5.0
SAVE_DELAY_S = 2.0
DELAY_LOOKUP_S = 4.0
FAIL_LOG_EVERY_S = 900
HARD_CAP_PER_HOUR = 60  # critical alerts skip the user's hourly limit, never more than max(2 × limit, this)
COUNTERS = ("received", "sent", "failed", "filtered", "deduped", "quiet", "rate_limited", "no_channel", "dropped")
# "puede recibir": the test also goes to a configured channel that is still disabled (it gets nothing until enabled)
TEST_TEXT = "✅ Prueba de NEXUS Terminal: este canal puede recibir tus alertas."


class AlertBusy(Exception):
    """A test is already running (status 409) or the previous one was a moment ago (429). Spanish message."""

    def __init__(self, message: str, status: int = 429):
        super().__init__(message)
        self.status = status


class AlertService:
    def __init__(self, rt: Any, *, clock: Callable[[], float] | None = None, channels: list[Any] | None = None):
        self.rt = rt
        self._time = clock or time.time
        self.channels = list(channels) if channels is not None else [TelegramChannel(), EmailChannel()]
        self.policy = Policy()
        self.counters: dict[str, int] = dict.fromkeys(COUNTERS, 0)
        self.since = now_ms()
        self.recent: deque[dict] = deque(maxlen=RECENT_KEEP)
        self.last: dict[str, dict] = {}  # channel id -> last delivery result (alerts, not tests)
        self.last_test: dict | None = None
        self.batch_window_s = BATCH_WINDOW_S
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._save_task: asyncio.Task | None = None
        self._bus: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: int | None = None
        self._running = False
        self._busy = 0  # batches being processed (status "pending")
        self._test_lock = asyncio.Lock()
        self._last_test_mono = float("-inf")
        self._log = RateLimitedLog()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self._load_state()
        self._loop = asyncio.get_running_loop()
        self._thread = threading.get_ident()
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._running = True
        bus = self.rt.bus
        if bus is not None and hasattr(bus, "add_listener"):
            bus.add_listener(self._on_event)
            self._bus = bus
        self._worker = asyncio.create_task(self._work(), name="alerts-worker")

    async def stop(self) -> None:
        self._running = False
        self._detach()
        # the worker first: a batch cancelled mid-delivery may still schedule a save, cancelled right after
        for attr in ("_worker", "_save_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await self._save_state()

    def _detach(self) -> None:
        bus, self._bus = self._bus, None
        if bus is None:
            return
        try:
            bus.remove_listener(self._on_event)  # the bus it was added to (after a take-over rt.bus is a new one)
        except Exception:  # a bus without remove_listener (test double): a stopped service ignores events anyway
            pass

    def _get(self, key: str, default: Any = None) -> Any:
        settings = self.rt.settings
        if settings is None:
            return config.DEFAULT_SETTINGS.get(key, default)
        return settings.get(key, default)

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        if self.rt.bus is not None:
            try:
                self.rt.bus.emit(level, "alerts", message, data)
            except Exception:
                pass

    # ------------------------------------------------------------------ intake

    def _on_event(self, ev: dict) -> None:
        """Bus listener (sync): classify and queue. Never raises, never blocks the emitter."""
        if not self._running:
            return
        try:
            alert = classify(ev)
        except Exception:
            return
        if alert is None:
            return
        if self._loop is not None and threading.get_ident() != self._thread:
            try:
                self._loop.call_soon_threadsafe(self._enqueue, alert)
            except RuntimeError:  # loop closed
                pass
            return
        self._enqueue(alert)

    def _enqueue(self, alert: Alert) -> None:
        if self._queue is None or not self._running:
            return
        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            self.counters["dropped"] += 1

    async def _work(self) -> None:
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        while True:
            first = await self._queue.get()
            batch = [first]
            deadline = loop.time() + self.batch_window_s
            while len(batch) < MAX_BATCH:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), remaining))
                except asyncio.TimeoutError:
                    break
            try:
                await self.process(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._log.should_log("process", 300):
                    self._emit("error", f"Alertas externas: fallo al procesar avisos ({type(exc).__name__}: {exc})")

    # ------------------------------------------------------------------ processing

    async def process(self, alerts: list[Alert]) -> dict | None:
        """Filter, dedupe and deliver one batch. Returns the delivery summary ({status, results}) or None when nothing
        was sent."""
        self._busy += 1
        try:
            return await self._process(alerts)
        finally:
            self._busy -= 1
            self._schedule_save()

    async def _process(self, alerts: list[Alert]) -> dict | None:
        prefs = Prefs.read(self._get)
        now = self._time()
        self.counters["received"] += len(alerts)
        wanted: list[Alert] = []
        # an exit + early exit of one market become the early exit before the dedupe sees either of them
        for a in collapse(alerts):
            # a new state of its group (semáforo level, a market's signal) first, even when filtered out below:
            # red -> green (not wanted at 'warn') -> red still announces the second red
            self.policy.supersede(a.group, a.key, now)
            if not await self._wanted(a, prefs):
                self.counters["filtered"] += 1
            elif self.policy.duplicate(a.key, now):
                self.counters["deduped"] += 1
            else:
                wanted.append(a)
        if not wanted:
            return None
        quiet_now = prefs.quiet_active(now)
        if quiet_now:
            wanted, held = _split(wanted, lambda a: a.priority == "critical" and prefs.quiet_critical)
            self._hold(held, "quiet")
        if wanted and not self.policy.room(now, prefs.max_per_hour):
            # critical alerts ignore the user's limit, up to a hard ceiling against floods
            ceiling = self.policy.room(now, max(2 * prefs.max_per_hour, HARD_CAP_PER_HOUR))
            wanted, held = _split(wanted, lambda a: a.priority == "critical" and ceiling)
            self._hold(held, "rate_limited")
        if not wanted:
            return None
        channels = [c for c, info in self._infos() if info.active]
        if not channels:
            for a in wanted:
                self._record(a, "no_channel", [])
            self.counters["no_channel"] += len(wanted)
            return None
        await self._delay_notes(wanted)
        notes, taken = self.policy.take_notes(prefs.max_per_hour, quiet_now)
        text, subject = compose(wanted, notes)
        results = await self._deliver(channels, text, subject)
        ok = [r for r in results if r["ok"]]
        status = "sent" if len(ok) == len(results) else "partial" if ok else "failed"
        if ok:
            self.policy.record_sent(now)
        else:
            self.policy.give_back(taken)
        self.counters["sent" if ok else "failed"] += len(wanted)
        for a in wanted:
            self._record(a, status, results)
        return {"status": status, "priority": top_priority(wanted), "results": results, "text": text}

    async def _wanted(self, a: Alert, prefs: Prefs) -> bool:
        if not prefs.categories.get(a.category, False) or not prefs.passes_level(a.priority):
            return False
        if a.category == "signals" and a.symbol:
            if not prefs.wants_symbol(a.symbol):
                return False
            if not prefs.watch_enabled:
                return False
            if a.tf and (a.symbol, a.tf) not in await self._watched():
                return False  # e.g. a chart open on a market outside the watchlist
        return True

    async def _watched(self) -> set[tuple[str, str]]:
        """(symbol, tf) pairs the NEXUS watcher follows: the watchlist at watch.tf."""
        nexus = self.rt.nexus
        if nexus is not None and hasattr(nexus, "watching"):
            try:
                items = nexus.watching() or []
                pairs = {(str(i["symbol"]).upper(), str(i["tf"])) for i in items if isinstance(i, dict)}
                if pairs:
                    return pairs
            except Exception:
                pass
        hub = self.rt.hub
        if hub is None or not hasattr(hub, "watchlist"):
            return set()
        try:
            symbols = await hub.watchlist()
            tf = normalize_tf(self._get("watch.tf", "1h"))
        except Exception:
            return set()
        return {(str(s).upper(), tf) for s in symbols or [] if s}

    def _unwatched_subset(self, prefs: Prefs) -> list[str]:
        """Entries of alerts.signal_symbols that match no market the NEXUS watcher follows (known only while it runs):
        their signals never reach the channels, so the status says so."""
        if not prefs.signal_symbols or not prefs.categories.get("signals") or not prefs.watch_enabled:
            return []
        nexus = self.rt.nexus
        try:
            items = nexus.watching() if nexus is not None and hasattr(nexus, "watching") else []
            watched = {str(i["symbol"]).upper() for i in items or [] if isinstance(i, dict)}
        except Exception:
            return []
        if not watched:
            return []
        return [e for e in sorted(prefs.signal_symbols) if not any(symbol_matches(w, e) for w in watched)]

    async def _delay_notes(self, alerts: list[Alert]) -> None:
        """Mark signals computed from delayed quotes (e.g. US stocks on TradingView, ~15 min) as delayed."""
        hub = self.rt.hub
        symbols = sorted({a.symbol for a in alerts if a.category == "signals" and a.symbol and not _crypto(a.symbol)})
        if not symbols or config.OFFLINE or hub is None or not hasattr(hub, "quotes"):
            return
        try:
            quotes = await asyncio.wait_for(hub.quotes(symbols), timeout=DELAY_LOOKUP_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        for a in alerts:
            q = quotes.get(a.symbol) if isinstance(quotes, dict) and a.category == "signals" else None
            delay = q.get("delay_s") if isinstance(q, dict) else None
            if isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay >= 60:
                a.notes.append(f"datos con ~{int(round(delay / 60))} min de retraso")

    def _hold(self, alerts: list[Alert], status: str) -> None:
        if not alerts:
            return
        self.counters["quiet" if status == "quiet" else "rate_limited"] += len(alerts)
        self.policy.omitted["quiet" if status == "quiet" else "rate"] += len(alerts)
        for a in alerts:
            self._record(a, status, [])

    def _record(self, a: Alert, status: str, results: list[dict]) -> None:
        self.recent.appendleft({
            "at": now_ms(), "category": a.category, "kind": a.kind, "priority": a.priority, "text": a.line(),
            "symbol": a.symbol, "status": status,
            "results": [{"channel": r["channel"], "ok": r["ok"], "error": r.get("error")} for r in results],
        })

    # ------------------------------------------------------------------ delivery

    def _infos(self) -> list[tuple[Any, ChannelInfo]]:
        out = []
        for ch in self.channels:
            try:
                out.append((ch, ch.info(self._get)))
            except Exception as exc:  # a broken channel must not hide the others
                problem = f"No se pudo leer su configuración ({type(exc).__name__})."
                out.append((ch, ChannelInfo(ch.id, ch.label, False, False, [], problem)))
        return out

    async def _deliver(self, channels: list[Any], text: str, subject: str, *, test: bool = False) -> list[dict]:
        async def one(ch: Any) -> dict:
            t0 = time.monotonic()
            error: str | None = None
            try:
                await asyncio.wait_for(ch.send(self._get, text, subject), timeout=SEND_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except ChannelError as exc:
                error = str(exc) or f"{ch.label}: error desconocido."
            except asyncio.TimeoutError:
                error = f"{ch.label} no respondió en {int(SEND_TIMEOUT_S)} s."
            except Exception as exc:  # never leak details that could hold a secret: only the type
                error = f"Error inesperado al enviar por {ch.label} ({type(exc).__name__})."
            res = {"channel": ch.id, "label": ch.label, "ok": error is None, "error": error, "skipped": False,
                   "test": test, "at": now_ms(), "duration_ms": int((time.monotonic() - t0) * 1000)}
            if not test:
                self.last[ch.id] = res
                if error and self._log.should_log(f"fail:{ch.id}", FAIL_LOG_EVERY_S):
                    self._emit("warn", f"Alertas externas: {ch.label} no entregó un aviso: {error}")
            return res

        return list(await asyncio.gather(*(one(c) for c in channels)))

    # ------------------------------------------------------------------ test

    async def send_test(self, channel: str | None = None) -> dict:
        """Send a test message to every configured channel (or only `channel`), whether it is enabled or not."""
        ids = [c.id for c in self.channels]
        if channel is not None and channel not in ids:
            raise ValueError(f"Canal desconocido: {channel!r} (usa {', '.join(ids)})")
        if self._test_lock.locked():
            raise AlertBusy("Ya hay una prueba en curso: espera a que termine.", 409)
        wait = TEST_COOLDOWN_S - (time.monotonic() - self._last_test_mono)
        if wait > 0:
            raise AlertBusy(f"Espera {max(1, int(wait + 0.999))} s antes de otra prueba.")
        async with self._test_lock:
            self._last_test_mono = time.monotonic()
            prefs = Prefs.read(self._get)
            infos = [(c, i) for c, i in self._infos() if channel in (None, c.id)]
            ready = [c for c, i in infos if i.configured]
            text = "\n".join([TEST_TEXT, self._summary_line(prefs), "— NEXUS · mensaje de prueba"])
            delivered = await self._deliver(ready, text, "NEXUS · mensaje de prueba", test=True)
            sent = {r["channel"]: r for r in delivered}
            results = []
            for c, info in infos:
                res = sent.get(c.id) or {"channel": c.id, "label": c.label, "ok": False, "error": info.reason()
                                         or "Sin configurar.", "skipped": True, "test": True, "at": now_ms(),
                                         "duration_ms": 0}
                res["enabled"] = info.enabled
                results.append(res)
            tried = [r for r in results if not r["skipped"]]
            ok = bool(tried) and all(r["ok"] for r in tried)
            message = self._test_message(results)
            self.last_test = {"at": now_ms(), "ok": ok, "message": message, "results": results}
            self._emit("info" if ok else "warn", f"Prueba de alertas: {message}")
            self._schedule_save()
            return {"ok": ok, "message": message, "results": results}

    @staticmethod
    def _test_message(results: list[dict]) -> str:
        tried = [r for r in results if not r["skipped"]]
        if not tried:
            return "No hay ningún canal configurado: completa Telegram o el correo y guarda los ajustes."
        parts = []
        for r in tried:
            if r["ok"]:
                note = "" if r.get("enabled") else " (canal desactivado: actívalo para recibir los avisos)"
                parts.append(f"{r['label']}: enviado{note}")
            else:
                parts.append(f"{r['label']}: {r['error']}")
        return " · ".join(parts)

    @staticmethod
    def _summary_line(prefs: Prefs) -> str:
        cats = [CATEGORY_INLINE[c] for c, on in prefs.categories.items() if on]
        what = ", ".join(cats) if cats else "nada (todas las categorías están desactivadas)"
        quiet = f" · silencio de {prefs.quiet_start} a {prefs.quiet_end}" if prefs.quiet_enabled else ""
        return f"Con tus ajustes se envían: {what} · nivel: {PRIORITY_LABEL[prefs.min_level].lower()}{quiet}."

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        """Short JSON-safe status for /api/health (no secrets)."""
        active = [i.label for _, i in self._infos() if i.active]
        return {
            "running": self._running,
            "channels": ", ".join(active) if active else "ninguno",
            "sent": self.counters["sent"],
            "failed": self.counters["failed"],
            "queued": self._queue.qsize() if self._queue is not None else 0,
        }

    def full_status(self) -> dict:
        """AlertsStatus (types-alerts.ts): channels, filters, counters, recent alerts and the last test."""
        prefs = Prefs.read(self._get)
        now = self._time()
        channels = []
        for ch, info in self._infos():
            item = info.to_dict()
            item["last"] = self.last.get(ch.id)
            channels.append(item)
        warnings = list(prefs.problems)
        if config.OFFLINE:
            warnings.insert(0, "Modo sin conexión (NEXUS_OFFLINE=1): no se envía ningún aviso.")
        if self._running and self._bus is None:
            warnings.append("El bus de eventos no está disponible: no llegan avisos que reenviar.")
        if prefs.categories.get("signals") and not prefs.watch_enabled:
            warnings.append("La vigilancia NEXUS está desactivada (Ajustes → Vigilancia): no habrá avisos de señales.")
        unwatched = self._unwatched_subset(prefs)
        if unwatched:
            names = ", ".join(unwatched[:5]) + ("…" if len(unwatched) > 5 else "")
            verb = "no está" if len(unwatched) == 1 else "no están"
            warnings.append(f"Señales solo de estos símbolos: {names} {verb} en lo que vigila NEXUS (tu lista de "
                            "seguimiento en la temporalidad vigilada), así que no habrá avisos de sus señales.")
        if prefs.categories.get("macro") and not (bool(self._get("macro.enabled", True))
                                                   and bool(self._get("macro.alerts", True))):
            warnings.append("Los avisos del semáforo están desactivados (macro.enabled / macro.alerts).")
        return clean_json({
            "running": self._running,
            "offline": bool(config.OFFLINE),
            "channels": channels,
            "active_channels": sum(1 for c in channels if c["active"]),
            "categories": dict(prefs.categories),
            "min_level": prefs.min_level,
            "signal_symbols": sorted(prefs.signal_symbols),
            "max_per_hour": prefs.max_per_hour,
            "sent_last_hour": self.policy.used(now),
            "quiet": {"enabled": prefs.quiet_enabled, "start": prefs.quiet_start, "end": prefs.quiet_end,
                      "critical": prefs.quiet_critical, "active": prefs.quiet_active(now),
                      "timezone": prefs.tz_label()},
            "counters": dict(self.counters),
            "since": self.since,
            "pending": (self._queue.qsize() if self._queue is not None else 0) + self._busy,
            "omitted": dict(self.policy.omitted),
            "recent": list(self.recent),
            "last_test": self.last_test,
            "warnings": warnings,
        })

    # ------------------------------------------------------------------ persistence

    def _schedule_save(self) -> None:
        if not self._running:
            return  # stop() saves once at the end
        if self._save_task is not None and not self._save_task.done():
            return
        try:
            self._save_task = asyncio.get_running_loop().create_task(self._save_later(), name="alerts-save")
        except RuntimeError:
            self._save_task = None

    async def _save_later(self) -> None:
        await asyncio.sleep(SAVE_DELAY_S)
        await self._save_state()

    def _state(self) -> dict:
        return {"version": STATE_VERSION, "since": self.since, "counters": dict(self.counters),
                "recent": list(self.recent), "last": dict(self.last), "last_test": self.last_test,
                "policy": self.policy.dump()}

    async def _save_state(self) -> None:
        db = self.rt.db
        if db is None:
            return
        try:
            payload = json.dumps(clean_json(self._state()), ensure_ascii=False, separators=(",", ":"))
            await db.execute(
                "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, key) DO UPDATE SET state=excluded.state, updates=excluded.updates, "
                "trained_until=excluded.trained_until, updated_at=excluded.updated_at",
                (STATE_KIND, STATE_KEY, payload, int(self.counters["received"]), None, now_ms()),
            )
        except Exception as exc:
            if self._log.should_log("save", 1800):
                self._emit("warn", f"Alertas externas: no se pudo guardar su estado ({type(exc).__name__}: {exc})")

    async def _load_state(self) -> None:
        db = self.rt.db
        if db is None:
            return
        try:
            row = await db.fetchone("SELECT state FROM model_state WHERE kind=? AND key=?", (STATE_KIND, STATE_KEY))
            state = json.loads(row["state"]) if row else None
        except Exception:
            return
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return
        counters = state.get("counters")
        if isinstance(counters, dict):
            for k in COUNTERS:
                v = counters.get(k)
                if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                    self.counters[k] = v
        since = state.get("since")
        if isinstance(since, int) and not isinstance(since, bool) and since > 0:
            self.since = since
        recent = state.get("recent")
        if isinstance(recent, list):
            self.recent = deque([r for r in recent if isinstance(r, dict)][:RECENT_KEEP], maxlen=RECENT_KEEP)
        last = state.get("last")
        if isinstance(last, dict):
            ids = {c.id for c in self.channels}
            self.last = {k: v for k, v in last.items() if k in ids and isinstance(v, dict)}
        if isinstance(state.get("last_test"), dict):
            self.last_test = state["last_test"]
        self.policy.load(state.get("policy"), self._time())


def _split(alerts: list[Alert], keep: Callable[[Alert], bool]) -> tuple[list[Alert], list[Alert]]:
    kept: list[Alert] = []
    rest: list[Alert] = []
    for a in alerts:
        (kept if keep(a) else rest).append(a)
    return kept, rest


def _crypto(symbol: str) -> bool:
    try:
        from app.engine.costs import asset_class

        return asset_class(symbol) == "crypto"
    except Exception:
        return False
