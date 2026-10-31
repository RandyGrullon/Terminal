"""Delivery policy of the external alerts (pure, injected clock): the user's preferences read from the settings,
quiet hours in the user's zone, dedupe of repeated alerts and the hourly message limit (critical alerts are never
held back by it). Alerts left out by the limit or the quiet hours are counted and announced in the next message."""
from __future__ import annotations

import datetime as dt
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.alerts.classify import CATEGORIES, PRIORITIES, plural, rank

DEDUPE_S = 600  # the same alert (same key) is not sent twice within 10 min
RATE_WINDOW_S = 3600
MAX_PER_HOUR = (1, 200)
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
TZ_SETTING = "ui.timezone"

Getter = Callable[[str, Any], Any]


def parse_hhmm(text: Any) -> int | None:
    """'23:30' -> minutes after midnight; None when not a valid HH:MM."""
    m = HHMM.match(str(text or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def zone(name: Any) -> dt.tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(str(name))
    except Exception:
        return None


def local_minutes(now_s: float, tz_name: Any = None) -> int:
    """Minutes after midnight of `now_s` in the zone `tz_name` (else the server's local zone)."""
    tz = zone(tz_name)
    d = dt.datetime.fromtimestamp(now_s, tz) if tz is not None else dt.datetime.fromtimestamp(now_s).astimezone()
    return d.hour * 60 + d.minute


def _omitted(n: int) -> str:
    """'se omitió 1 aviso' / 'se omitieron 3 avisos'."""
    return f"se omitió {plural(n, 'aviso', 'avisos')}" if n == 1 else f"se omitieron {plural(n, 'aviso', 'avisos')}"


def in_window(minute: int, start: int, end: int) -> bool:
    """start <= minute < end, across midnight when start > end (23:00 -> 07:00)."""
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def symbol_matches(symbol: str, entry: str) -> bool:
    """`symbol` (EXCHANGE:TICKER) is the alerts.signal_symbols entry `entry`; an entry without exchange ('AAPL',
    'BTCUSDT') matches that ticker on any exchange (NASDAQ:AAPL, BINANCE:BTCUSDT)."""
    return symbol == entry or (":" not in entry and symbol.rpartition(":")[2] == entry)


@dataclass
class Prefs:
    categories: dict[str, bool]
    min_level: str
    max_per_hour: int
    signal_symbols: set[str]
    quiet_enabled: bool
    quiet_start: str
    quiet_end: str
    quiet_critical: bool
    tz: str
    watch_enabled: bool
    problems: list[str] = field(default_factory=list)

    @classmethod
    def read(cls, get: Getter) -> "Prefs":
        problems: list[str] = []
        level = str(get("alerts.min_level", "warn") or "warn").strip().lower()
        if level not in PRIORITIES:
            problems.append(f"Nivel mínimo no válido ({level!r}): se usa «importantes y críticos».")
            level = "warn"
        try:
            limit = int(get("alerts.max_per_hour", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(MAX_PER_HOUR[0], min(MAX_PER_HOUR[1], limit))
        raw_symbols = get("alerts.signal_symbols", []) or []
        symbols = {str(s).strip().upper() for s in raw_symbols if str(s).strip()} if isinstance(raw_symbols, list) else set()
        quiet = bool(get("alerts.quiet_enabled", False))
        start, end = str(get("alerts.quiet_start", "23:00") or ""), str(get("alerts.quiet_end", "07:00") or "")
        if quiet:
            a, b = parse_hhmm(start), parse_hhmm(end)
            if a is None or b is None:
                problems.append("Horas de silencio no válidas (usa HH:MM): no se aplican.")
                quiet = False
            elif a == b:
                problems.append("Las horas de silencio empiezan y terminan a la misma hora: no se aplican.")
                quiet = False
        return cls(
            categories={c: bool(get(f"alerts.{c}", True)) for c in CATEGORIES},
            min_level=level,
            max_per_hour=limit,
            signal_symbols=symbols,
            quiet_enabled=quiet,
            quiet_start=start,
            quiet_end=end,
            quiet_critical=bool(get("alerts.quiet_critical", False)),
            tz=str(get(TZ_SETTING, "") or ""),
            watch_enabled=bool(get("watch.enabled", True)),
            problems=problems,
        )

    def passes_level(self, priority: str) -> bool:
        return rank(priority) >= rank(self.min_level)

    def wants_symbol(self, symbol: str | None) -> bool:
        """The signal subset lets `symbol` through (no subset = every watched market)."""
        if not self.signal_symbols:
            return True
        s = str(symbol or "").strip().upper()
        return bool(s) and any(symbol_matches(s, e) for e in self.signal_symbols)

    def quiet_active(self, now_s: float) -> bool:
        if not self.quiet_enabled:
            return False
        a, b = parse_hhmm(self.quiet_start), parse_hhmm(self.quiet_end)
        if a is None or b is None or a == b:
            return False
        return in_window(local_minutes(now_s, self.tz), a, b)

    def tz_label(self) -> str:
        return self.tz if zone(self.tz) is not None else "hora del servidor"


class Policy:
    """Dedupe memory, sent-message times (rolling hour) and the alerts left out since the last delivered message."""

    def __init__(self) -> None:
        self.seen: dict[str, float] = {}
        self.sent: deque[float] = deque()
        self.omitted = {"rate": 0, "quiet": 0}
        self.latest: dict[str, tuple[str, float]] = {}  # dedupe group -> (key of its newest alert, when)

    def _prune(self, now: float) -> None:
        for key in [k for k, t in self.seen.items() if now - t >= DEDUPE_S]:
            del self.seen[key]
        while self.sent and now - self.sent[0] >= RATE_WINDOW_S:
            self.sent.popleft()
        for group in [g for g, (_, t) in self.latest.items() if now - t >= DEDUPE_S]:
            del self.latest[group]

    def supersede(self, group: str | None, key: str, now: float) -> None:
        """`key` is the newest alert of `group` (the semáforo's state, or the signal of one market): the previous one
        leaves the dedupe memory, so going back to an earlier state within DEDUPE_S (red -> amber -> red, or
        BUY -> EXIT -> BUY on a short timeframe) is announced again, while a repeat of the current one is not."""
        if not group:
            return
        self._prune(now)
        prev = self.latest.get(group)
        if prev is not None and prev[0] != key:
            self.seen.pop(prev[0], None)
        self.latest[group] = (key, now)

    def duplicate(self, key: str, now: float) -> bool:
        """True when `key` was handled within DEDUPE_S; otherwise remembers it."""
        self._prune(now)
        if key in self.seen:
            return True
        self.seen[key] = now
        return False

    def used(self, now: float) -> int:
        self._prune(now)
        return len(self.sent)

    def room(self, now: float, limit: int) -> bool:
        return self.used(now) < limit

    def record_sent(self, now: float) -> None:
        self.sent.append(now)

    def take_notes(self, limit: int, quiet_now: bool) -> tuple[list[str], dict[str, int]]:
        """Lines announcing the alerts left out (the quiet-hours line only once the quiet hours are over)."""
        notes: list[str] = []
        taken = {"rate": 0, "quiet": 0}
        if self.omitted["rate"]:
            taken["rate"] = self.omitted["rate"]
            notes.append(f"Además {_omitted(taken['rate'])} por el límite de {limit} por hora.")
        if self.omitted["quiet"] and not quiet_now:
            taken["quiet"] = self.omitted["quiet"]
            notes.append(f"Durante las horas de silencio {_omitted(taken['quiet'])}.")
        for k, n in taken.items():
            self.omitted[k] -= n
        return notes, taken

    def give_back(self, taken: dict[str, int]) -> None:
        """The message with the notes was not delivered: announce them in the next one."""
        for k, n in taken.items():
            self.omitted[k] = self.omitted.get(k, 0) + n

    # ------------------------------------------------------------------ persistence

    def dump(self) -> dict:
        return {"seen": dict(self.seen), "sent": list(self.sent), "omitted": dict(self.omitted),
                "latest": {g: [k, t] for g, (k, t) in self.latest.items()}}

    def load(self, state: Any, now: float) -> None:
        if not isinstance(state, dict):
            return
        seen = state.get("seen")
        if isinstance(seen, dict):
            self.seen = {str(k): float(v) for k, v in seen.items() if isinstance(v, (int, float))
                         and 0 <= now - float(v) < DEDUPE_S}
        sent = state.get("sent")
        if isinstance(sent, list):
            self.sent = deque(sorted(float(t) for t in sent if isinstance(t, (int, float))
                                     and 0 <= now - float(t) < RATE_WINDOW_S))
        omitted = state.get("omitted")
        if isinstance(omitted, dict):
            for k in ("rate", "quiet"):
                v = omitted.get(k)
                if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1_000_000:
                    self.omitted[k] = v
        latest = state.get("latest")
        if isinstance(latest, dict):
            self.latest = {
                str(g): (v[0], float(v[1])) for g, v in latest.items()
                if isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], str)
                and isinstance(v[1], (int, float)) and not isinstance(v[1], bool) and 0 <= now - float(v[1]) < DEDUPE_S
            }
