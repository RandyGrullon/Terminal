"""``noticias``: the economic calendar inside PyChart scripts (read-only; the worker has no network, so the server
hands the events in the payload: ``MacroService.script_events`` -> ``sandbox.run_script(news=...)``).

- ``noticias.cerca(antes=30, despues=30, eventos="principales", moneda="USD")`` -> boolean series: True on every bar
  whose interval [open, open + duration) overlaps [event − antes, event + despues] (minutes). Scheduled release times
  are public in advance, so using them is not looking at future prices.
- ``noticias.lista(eventos="principales", moneda="USD")`` -> [(time, title, currency, impact), ...] of the run.
- ``noticias.desde`` / ``noticias.hasta`` -> unix seconds bounding the continuous data of the «principales» (CPI,
  employment, FOMC); ``noticias.disponible`` -> False when the run had no calendar (tests, macro module off).

Event sets: "principales" (CPI, employment report, FOMC decision and press conference: continuous data from
February 2024, app.macro.history), "alto" (every high-impact event), "medio" (high + medium), "todos". Only the
principales have history before November 2026, so the other sets are not fit for long backtests (a notice says so).
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

import numpy as np

from .runtime_base import ScriptContext, ScriptRuntimeError, to_float
from .runtime_metrics import bar_seconds

EVENT_SETS = ("principales", "alto", "medio", "todos")
MAX_MINUTES = 7 * 24 * 60.0
LIST_MARGIN_S = 86400


def _day(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%d/%m/%Y")


class NewsAPI:
    """The ``noticias`` object of a run (immutable for the script)."""

    __slots__ = ("_ctx", "_events", "_from", "_until", "_given")

    def __init__(self, ctx: ScriptContext) -> None:
        news = ctx.news if isinstance(ctx.news, dict) else None
        events: list[tuple[int, str, str, str, bool]] = []
        for ev in (news or {}).get("events") or []:
            if not isinstance(ev, dict):
                continue
            try:
                t = int(ev["time"])
            except (KeyError, TypeError, ValueError):
                continue
            events.append((t, str(ev.get("title") or "")[:120], str(ev.get("currency") or "").upper()[:8],
                           str(ev.get("impact") or "low").lower()[:10], bool(ev.get("principal"))))
        events.sort(key=lambda e: e[0])

        def bound(key: str) -> int | None:
            value = (news or {}).get(key)
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None

        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_events", tuple(events))
        object.__setattr__(self, "_from", bound("from"))
        object.__setattr__(self, "_until", bound("until"))
        object.__setattr__(self, "_given", news is not None)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("noticias es de solo lectura")

    def __repr__(self) -> str:
        return f"<noticias: {len(self._events)} eventos>"

    # ------------------------------------------------------------------ read-only values

    @property
    def disponible(self) -> bool:
        """True when the run received the economic calendar."""
        return bool(self._given)

    @property
    def desde(self) -> int | None:
        """Unix second from which the principal events (CPI, employment, Fed) are complete."""
        return self._from

    @property
    def hasta(self) -> int | None:
        """Unix second up to which every principal kind has a scheduled date."""
        return self._until

    # ------------------------------------------------------------------ helpers

    def _select(self, eventos: Any, moneda: Any) -> list[tuple[int, str, str, str, bool]]:
        kind = str(eventos or "").strip().lower()
        if kind not in EVENT_SETS:
            raise ScriptRuntimeError(f"noticias: eventos debe ser uno de {', '.join(EVENT_SETS)} (recibido: {eventos!r}).")
        currency = "" if moneda is None else str(moneda).strip().upper()
        out = []
        for ev in self._events:
            if currency and ev[2] != currency:
                continue
            if kind == "principales" and not ev[4]:
                continue
            if kind == "alto" and ev[3] != "high":
                continue
            if kind == "medio" and ev[3] not in ("high", "medium"):
                continue
            out.append(ev)
        if kind != "principales":
            self._ctx.notice(f"noticias con eventos=\"{kind}\": solo las principales (CPI, empleo y Fed) tienen historial "
                             "antes de noviembre de 2026; para backtests largos usa eventos=\"principales\".")
        return out

    def _coverage_notices(self, times: np.ndarray) -> None:
        ctx = self._ctx
        if not self._given:
            ctx.notice("esta ejecución no tiene calendario de noticias: noticias.cerca() no marca ninguna vela.")
            return
        if times.size == 0:
            return
        if self._from is not None and int(times[0]) < self._from:
            ctx.notice(f"las noticias principales empiezan el {_day(self._from)}: antes de esa fecha noticias.cerca() "
                       "no ve eventos y el filtro no bloquea nada.")
        if self._until is not None and int(times[-1]) > self._until:
            ctx.notice(f"el calendario de noticias llega hasta el {_day(self._until)}: después no ve eventos.")

    # ------------------------------------------------------------------ API

    def cerca(self, antes: Any = 30, despues: Any = 30, eventos: Any = "principales", moneda: Any = "USD") -> np.ndarray:
        """Bars that overlap the window [event − antes, event + despues] (minutes) of a selected event."""
        a = to_float(antes, "noticias.cerca(antes)")
        d = to_float(despues, "noticias.cerca(despues)")
        if not (0 <= a <= MAX_MINUTES and 0 <= d <= MAX_MINUTES):
            raise ScriptRuntimeError("noticias.cerca: antes y despues son minutos entre 0 y 10080 (una semana).")
        selected = self._select(eventos, moneda)
        bars = self._ctx.bars
        times = bars.time.astype(np.int64)
        self._coverage_notices(times)
        if not selected or bars.n == 0:
            return np.zeros(bars.n, dtype=bool)
        duration = int(bar_seconds(bars.time)) if bars.n >= 2 else 60
        event_times = np.asarray([e[0] for e in selected], dtype=np.int64)
        lo = np.searchsorted(event_times, times - int(round(d * 60)), side="left")
        hi = np.searchsorted(event_times, times + duration + int(round(a * 60)), side="left")
        return hi > lo

    def lista(self, eventos: Any = "principales", moneda: Any = "USD") -> list[tuple[int, str, str, str]]:
        """Selected events from one day before the first bar to the end of the last one, oldest first."""
        selected = self._select(eventos, moneda)
        bars = self._ctx.bars
        if bars.n == 0:
            return []
        first = int(bars.time[0]) - LIST_MARGIN_S
        last = int(bars.time[-1]) + (int(bar_seconds(bars.time)) if bars.n >= 2 else 60)
        return [(t, title, cur, imp) for t, title, cur, imp, _p in selected if first <= t <= last]
