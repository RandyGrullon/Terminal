"""MacroService (`rt.macro`): the semáforo de mercado. Keeps the economic calendar (ForexFactory weekly feed + the
official calendars: FOMC, BLS, BEA, ECB, BoE, BoJ; both persisted in model_state) and the volatility metrics fresh,
evaluates the rules (conditions.py), raises deduplicated alerts (bus events + WS topic "macro") and answers
`conditions()`, `calendar()`, `coverage()` and `gate_check()` for other modules. Works offline from the stored
calendars and the curated official dates."""
from __future__ import annotations

import asyncio
import copy
import time
from typing import Any, Callable

from app import config
from app.macro import calendar as cal
from app.macro import fomc
from app.macro import official as off
from app.macro import schedules
from app.macro.alerts import AlertTracker
from app.macro.conditions import NO_DATA_HEADLINE, assess, settings_view
from app.macro.fmt import until
from app.macro.metrics import MetricsCollector
from app.macro.scope import global_scope, scope_for_symbol
from app.macro.titles import short_title
from app.util import RateLimitedLog, TTLCache, now_ms

LOOP_S = 60
COND_TTL_S = 30
INFO_TIMEOUT_S = 6.0
FOMC_PAST_S = 2 * 86400
FOMC_AHEAD_S = 400 * 86400
STATUS_AHEAD_S = 60 * 86400
OFFICIAL_LOG_S = 6 * 3600
_NO_INFO_KINDS = frozenset({"global", "sim", "crypto", "forex", "us_equities"})


class MacroService:
    def __init__(self, rt: Any, clock: Callable[[], float] | None = None,
                 monotonic: Callable[[], float] | None = None, fetch_feed: Callable[[], Any] | None = None,
                 fetch_official: Callable[[], Any] | None = None):
        self.rt = rt
        self._time = clock or time.time
        self._mono = monotonic or time.monotonic
        self._fetch_feed = fetch_feed or cal.fetch_feed
        self._fetch_official = fetch_official or off.fetch_all
        self.metrics = MetricsCollector(rt, clock=self._mono)
        self.tracker = AlertTracker()
        self.cache: dict | None = None
        self.official: dict | None = None  # live official copies (official.py), None until the first good one
        self._cond = TTLCache(COND_TTL_S, maxsize=64)
        self._cal_next = float("-inf")
        self._cal_error: str | None = None
        self._off_next = float("-inf")
        self._off_errors: dict[str, str] = {}
        self._last_refresh: int | None = None
        self._global: dict | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._log = RateLimitedLog()
        self._started = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        try:
            self.cache = await cal.load_cached(self.rt.db)
        except Exception as exc:
            self._emit("warn", f"Semáforo: no se pudo leer el calendario guardado ({type(exc).__name__}: {exc})")
        try:
            self.official = await off.load_official(self.rt.db)
        except Exception as exc:
            self._emit("warn", f"Semáforo: no se pudo leer el calendario oficial guardado ({type(exc).__name__}: {exc})")
        if self.rt.settings is not None:
            self.rt.settings.add_listener(self._on_setting)
        self._started = True
        if config.BACKGROUND and not config.OFFLINE:
            self._task = asyncio.create_task(self._loop(), name="macro-loop")

    async def stop(self) -> None:
        self._started = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def status(self) -> dict:
        now = self.now()
        cond = self._global
        nxt = cond.get("next_event") if cond else None
        out = {
            "running": self._started,
            "level": cond.get("level") if cond else None,
            "next_event": f"{short_title(nxt['title_es'])} {until(int(nxt['time']) - now)}" if nxt else None,
            "calendar_events": len(self.cache["events"]) if self.cache else 0,
            "stale": self.calendar_stale(now),
            "last_refresh": self._last_refresh,
            "official_events": len(off.official_events(self.official, now, now + STATUS_AHEAD_S)),
        }
        if self._off_errors:
            out["official_errors"] = dict(self._off_errors)
        return out

    def _on_setting(self, key: str, _value: Any) -> None:
        if key.startswith("macro.") or key == "ui.timezone":
            self._cond.clear()

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        if self.rt.bus is not None:
            self.rt.bus.emit(level, "macro", message, data)

    def _setting(self, key: str, default: Any = None) -> Any:
        return self.rt.settings.get(key, default) if self.rt.settings is not None else default

    def _settings(self) -> dict:
        return self.rt.settings.all() if self.rt.settings is not None else dict(config.DEFAULT_SETTINGS)

    def enabled(self) -> bool:
        return bool(self._setting("macro.enabled", True))

    def now(self) -> int:
        return int(self._time())

    # ------------------------------------------------------------------ calendar

    def calendar_stale(self, now_s: int | None = None) -> bool:
        return cal.is_stale(self.cache, self.now() if now_s is None else now_s)

    def events(self, now_s: int | None = None, ahead_s: int = FOMC_AHEAD_S) -> list[dict]:
        """Merged calendar around `now_s`, all currencies: the weekly feed copy + the official calendars (FOMC,
        then the live BLS / BEA copies, then the curated dates nothing live covers), deduplicated."""
        now = self.now() if now_s is None else int(now_s)
        feed = self.cache["events"] if self.cache else []
        start, end = now - FOMC_PAST_S, now + ahead_s
        return cal.merge_extra(feed, fomc.fomc_events(start, end) + off.official_events(self.official, start, end))

    def coverage(self, now_s: int | None = None) -> dict:
        """Which source covers what (CalendarCoverage, optional field of GET /api/macro/calendar): the weekly feed
        reaches `weekly_until` (end of its week, unix s); after it only the official calendars remain."""
        now = self.now() if now_s is None else int(now_s)
        stale = self.calendar_stale(now)
        cache = self.cache or {}
        meetings = fomc.fomc_events(None, None)
        sources = [
            {"id": cal.SOURCE, "label": "ForexFactory (semana actual, todas las divisas)", "live": True,
             "ok": not stale, "updated_at": cache.get("fetched_at"), "until": cache.get("week_end"),
             "error": self._cal_error},
            {"id": fomc.SOURCE, "label": "Fed (reuniones del FOMC)", "live": False, "ok": True, "updated_at": None,
             "until": max(e["time"] for e in meetings) if meetings else None, "error": None},
        ]
        sources += off.coverage_rows(self.official, self._off_errors)
        return {"weekly_until": cache.get("week_end"), "weekly_stale": stale, "verified": schedules.VERIFIED_ON,
                "sources": sources}

    async def refresh_calendar(self, force: bool = False) -> bool:
        if config.OFFLINE or (not force and self._mono() < self._cal_next):
            return False
        try:
            events = await self._fetch_feed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._cal_next = self._mono() + cal.RETRY_S
            self._cal_error = str(exc)
            if self._log.should_log("calendar", 1800):
                kept = "uso la copia guardada" if self.cache else "sin copia guardada todavía"
                self._emit("warn", f"Semáforo: calendario económico no disponible ({exc}); {kept}.")
            return False
        self._cal_next = self._mono() + cal.REFRESH_S
        self._cal_error = None
        refreshes = int((self.cache or {}).get("refreshes") or 0) + 1
        self.cache = cal.make_cache(events, refreshes=refreshes)
        self._cond.clear()
        try:
            await cal.save_cached(self.rt.db, self.cache)
        except Exception as exc:
            if self._log.should_log("calendar-save", 1800):
                self._emit("warn", f"Semáforo: no se pudo guardar el calendario ({type(exc).__name__}: {exc})")
        return True

    async def refresh_official(self, force: bool = False) -> bool:
        """Refresh the live official calendars (BLS, BEA) when due: every 12 h, 1 h after a failure. A failing source
        keeps its stored copy (or the curated dates); the others are still updated."""
        if config.OFFLINE or (not force and self._mono() < self._off_next):
            return False
        try:
            results = await self._fetch_official()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            results = {source: exc for source in off.SOURCES}
        state = copy.deepcopy(self.official) if self.official else off.empty_state()
        updated = False
        for source, res in (results or {}).items():
            if source not in off.SOURCES:
                continue
            good = off.clean_copy(source, res)
            if good is not None and good["kinds"]:
                state["sources"][source] = good
                self._off_errors.pop(source, None)
                updated = True
                continue
            if isinstance(res, cal.CalendarError):
                reason = str(res)
            elif isinstance(res, BaseException):
                reason = f"{type(res).__name__}: {res}"
            else:
                reason = "copia no válida"
            self._off_errors[source] = reason[:200]
            if self._log.should_log(f"official-{source}", OFFICIAL_LOG_S):
                kept = ("uso la copia guardada" if source in state["sources"]
                        else f"uso las fechas oficiales verificadas el {schedules.VERIFIED_LABEL}")
                self._emit("warn", f"Semáforo: calendario oficial del {off.SOURCES[source]['label']} no disponible "
                                   f"({reason}); {kept}.")
        self._off_next = self._mono() + (off.RETRY_S if self._off_errors else off.REFRESH_S)
        if not updated:
            return False
        self.official = state
        self._cond.clear()
        try:
            await off.save_official(self.rt.db, state)
        except Exception as exc:
            if self._log.should_log("official-save", 1800):
                self._emit("warn", f"Semáforo: no se pudo guardar el calendario oficial ({type(exc).__name__}: {exc})")
        return True

    # ------------------------------------------------------------------ loop

    async def _loop(self) -> None:
        await asyncio.sleep(2.0)
        while True:
            try:
                if self.enabled() and not config.OFFLINE:
                    await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._log.should_log("loop", 600):
                    self._emit("error", f"Semáforo: fallo al actualizar ({type(exc).__name__}: {exc})")
            await asyncio.sleep(LOOP_S)

    async def refresh(self, force: bool = False) -> dict:
        """One cycle: refresh what is due, evaluate the global semáforo, raise alerts and publish on WS."""
        async with self._lock:
            if not config.OFFLINE:
                await self.refresh_calendar(force)
                await self.refresh_official(force)
                await self.metrics.refresh(crypto=True, force=force)
            now = self.now()
            settings = self._settings()
            events = self.events(now)
            cond = assess(events, self.metrics.snapshot(), now, settings, global_scope(),
                          calendar_stale=self.calendar_stale(now))
            self._cond.clear()
            self._cond.set(self._key(global_scope()), cond)
            self._global = cond
            self._last_refresh = now_ms()
            alerts = self.tracker.check(cond, events, now, settings_view(settings))
            if bool(settings.get("macro.alerts", True)) and self.enabled():
                for item in alerts:
                    self._emit("success" if item["level"] == "green" else "warn", item["message"],
                               {"alert": item, "level": cond["level"], "scope": "global"})
                    self._publish(cond, item)
            self._publish(cond, None)
            return copy.deepcopy(cond)

    def _publish(self, cond: dict, alert: dict | None) -> None:
        if self.rt.ws is not None:
            try:
                self.rt.ws.publish("macro", {"type": "macro", "conditions": cond, "alert": alert})
            except Exception:
                pass

    # ------------------------------------------------------------------ public API

    @staticmethod
    def _key(scope: dict) -> str:
        return f"{scope['id']}|{','.join(scope.get('currencies') or [])}"

    async def _symbol_info(self, symbol: str) -> dict | None:
        hub = self.rt.hub
        if hub is None or not hasattr(hub, "symbol_info"):
            return None
        try:
            info = await asyncio.wait_for(hub.symbol_info(symbol), timeout=INFO_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return info if isinstance(info, dict) else None

    async def scope(self, symbol: str | None) -> dict:
        scope = scope_for_symbol(symbol)
        if symbol and scope["kind"] not in _NO_INFO_KINDS:
            info = await self._symbol_info(scope["symbol"])
            if info:
                scope = scope_for_symbol(symbol, info)
        return scope

    async def conditions(self, symbol: str | None = None) -> dict:
        """MarketConditions for `symbol` (None = global header semáforo); cached 30 s per scope."""
        scope = await self.scope(symbol)
        key = self._key(scope)
        cond = self._cond.get(key)
        if cond is None:
            now = self.now()
            cond = assess(self.events(now), self.metrics.snapshot(), now, self._settings(), scope,
                          calendar_stale=self.calendar_stale(now))
            self._cond.set(key, cond)
            if scope["id"] == "global":
                self._global = cond
        out = copy.deepcopy(cond)
        out["symbol"] = scope.get("symbol")
        return out

    async def calendar(self, days: int = 7, impacts: tuple[str, ...] | list[str] | None = ("high", "medium"),
                       currencies: list[str] | None = None, past_s: int = 3600) -> list[dict]:
        """Upcoming MacroEvents (from one hour ago to `days` days ahead), oldest first."""
        now = self.now()
        days = max(1, min(int(days), 60))
        events = cal.select(self.events(now, ahead_s=days * 86400 + 86400), start_s=now - past_s,
                            end_s=now + days * 86400, impacts=tuple(impacts) if impacts else None,
                            currencies={c.upper() for c in currencies} if currencies else None)
        return [cal.public_event(e, now) for e in events]

    async def gate_check(self, symbol: str, direction: str = "long") -> dict | None:
        """Trade Gate check 'macro' (critical when red); None when disabled or not applicable (SIM)."""
        if not self.enabled():
            return None
        try:
            cond = await self.conditions(symbol)
        except Exception:
            return None
        if cond.get("scope") == "sim":
            return None
        level = cond["level"]
        no_data = bool(cond.get("stale")) and cond["headline"] == NO_DATA_HEADLINE
        return {
            "id": "macro",
            "label": "Calendario y volatilidad",
            "passed": None if no_data else level == "green",
            "critical": level == "red",
            "weight": 1.0,
            "value": level,
            "detail": cond["headline"],
        }
