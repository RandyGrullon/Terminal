"""Official release calendars beyond the weekly feed.

Live: the iCalendar (ICS) feeds the BLS (CPI, Employment Situation, PPI, JOLTS, ECI) and the BEA (GDP, Personal
Income and Outlays = PCE) publish for calendar apps, one plain request each with the app's honest User-Agent
(app.util.http_client) at most every 12 h. Both answered that client on 2026-10-31. A source that refuses it (HTTP
403, a challenge page instead of a calendar) is reported and skipped: never worked around. The last good copy of each
source lives in model_state(kind='macro', key='calendar_official').

Curated: schedules.py (BLS, BEA, ECB, BoE, BoJ dates checked on the official pages), used for every (source, kind)
where no live copy reaches, i.e. offline, when a source refuses the client, or after the end of its live calendar.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from app.macro import schedules
from app.macro.calendar import CalendarError, _valid_event, sort_key
from app.macro.fomc import NEW_YORK
from app.util import clean_json, http_client, now_ms

STATE_KIND, STATE_KEY = "macro", "calendar_official"
REFRESH_S = 12 * 3600  # the agencies update their calendars a few times a month
RETRY_S = 3600  # after a failure (the stored copy and the curated dates keep serving meanwhile)
KEEP_PAST_S = 7 * 86400  # past releases kept in the stored copy
MAX_BYTES = 3_000_000
MAX_EVENTS = 5000
UTC = dt.timezone.utc
TZ_ALIASES = {"US-Eastern": "America/New_York", "US/Eastern": "America/New_York"}

# A match is (kind, English title, Spanish title or None = default of the kind, impact or None = default).
Match = tuple[str, str, Optional[str], Optional[str]]
BLS_KINDS: dict[str, str] = {
    "Consumer Price Index": "us_cpi",
    "Employment Situation": "us_nfp",
    "Producer Price Index": "us_ppi",
    "Job Openings and Labor Turnover Survey": "us_jolts",
    "Employment Cost Index": "us_eci",
}
_GDP = re.compile(r"^(?:GDP|Gross Domestic Product)(?! by)\b.*\((Advance|Second|Third) Estimate\)")
_PCE = re.compile(r"^Personal Income and Outlays\b")


def bls_match(summary: str) -> Match | None:
    kind = BLS_KINDS.get(summary)
    return (kind, summary, None, None) if kind else None


def bea_match(summary: str) -> Match | None:
    m = _GDP.match(summary)
    if m:
        _wording, title_es, impact = schedules.GDP_ESTIMATES[m.group(1).lower()]
        return "us_gdp", summary, title_es, impact
    if _PCE.match(summary):
        return "us_pce", summary, None, None
    return None


SOURCES: dict[str, dict[str, Any]] = {
    "bls": {"label": "BLS", "name": "BLS (IPC, empleo, IPP, JOLTS)", "match": bls_match,
            "url": "https://www.bls.gov/schedule/news_release/bls.ics"},
    "bea": {"label": "BEA", "name": "BEA (PIB e inflación PCE)", "match": bea_match,
            "url": "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"},
}
# Sources with a curated schedule only (no machine-readable calendar).
CURATED: dict[str, str] = {
    "ecb": "BCE (decisiones de tipos)",
    "boe": "Banco de Inglaterra (decisiones de tipos)",
    "boj": "Banco de Japón (decisiones de tipos, hora aproximada)",
}


# ----------------------------------------------------------------------------- iCalendar parsing (pure)


def _unescape(value: str) -> str:
    text = re.sub(r"\\(.)", lambda m: " " if m.group(1) in "nN" else m.group(1), value)
    return " ".join(text.split())


def _zone(name: str | None, default: dt.tzinfo) -> dt.tzinfo:
    name = TZ_ALIASES.get(name or "", name or "")
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except Exception:
        return default


def _when(params: dict[str, str], value: str, cal_tz: dt.tzinfo) -> int | None:
    """DTSTART value -> unix s ('20261106T083000' in TZID / the calendar zone, '...Z' = UTC); all-day -> None."""
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value.strip())
    if not m or params.get("VALUE", "DATE-TIME").upper() == "DATE":
        return None
    try:
        naive = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    tz = UTC if m.group(3) else _zone(params.get("TZID", "").strip('"'), cal_tz)
    return int(naive.replace(tzinfo=tz).timestamp())


def parse_ics(text: str) -> list[tuple[int, str]]:
    """Timed VEVENTs of an iCalendar text -> [(unix s, summary)]. Raises CalendarError when it is not a calendar
    (an HTML page instead, e.g. a site that refuses automated clients)."""
    if "BEGIN:VCALENDAR" not in str(text or "")[:4096]:
        raise CalendarError("no devolvió un calendario ICS: posible bloqueo de clientes automáticos")
    cal_tz: dt.tzinfo = NEW_YORK
    out: list[tuple[int, str]] = []
    cur: dict[str, tuple[dict[str, str], str]] | None = None
    for line in re.sub(r"\r?\n[ \t]", "", text).splitlines():
        marker = line.strip().upper()
        if marker == "BEGIN:VEVENT":
            cur = {}
            continue
        if marker == "END:VEVENT":
            if cur and "DTSTART" in cur and "SUMMARY" in cur:
                ts = _when(*cur["DTSTART"], cal_tz)
                summary = _unescape(cur["SUMMARY"][1])
                if ts is not None and summary:
                    out.append((ts, summary))
            cur = None
            if len(out) >= MAX_EVENTS:
                break
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key, *raw_params = name.split(";")
        key = key.strip().upper()
        if cur is None:
            if key == "X-WR-TIMEZONE":
                cal_tz = _zone(value.strip(), NEW_YORK)
            continue
        if key in ("DTSTART", "SUMMARY") and key not in cur:
            params = dict(p.split("=", 1) for p in raw_params if "=" in p)
            cur[key] = ({k.strip().upper(): v.strip() for k, v in params.items()}, value)
    return out


def build_copy(source: str, entries: list[tuple[int, str]], fetched_at_ms: int, now_s: int) -> dict:
    """Parsed ICS entries -> stored copy {events, kinds, until, fetched_at}. `until` = the last entry of the whole
    calendar (how far it reaches), `kinds` = the releases it lists (also past ones)."""
    match: Callable[[str], Match | None] = SOURCES[source]["match"]
    events: dict[str, dict] = {}
    kinds: set[str] = set()
    until: int | None = None
    for ts, summary in entries:
        until = ts if until is None else max(until, ts)
        hit = match(summary)
        if hit is None:
            continue
        kind, title, title_es, impact = hit
        kinds.add(kind)
        if ts < now_s - KEEP_PAST_S:
            continue
        ev = schedules.make_event(source, kind, ts, title, static=False, title_es=title_es, impact=impact)
        events.setdefault(ev["id"], ev)
    return {"events": sorted(events.values(), key=sort_key), "kinds": sorted(kinds), "until": until,
            "fetched_at": int(fetched_at_ms)}


# ----------------------------------------------------------------------------- merge helpers (pure)


def empty_state() -> dict:
    return {"version": 1, "sources": {}}


def official_events(state: dict | None, start_s: int, end_s: int) -> list[dict]:
    """Official events within [start_s, end_s]: the live copies, plus the curated dates of every (source, kind)
    that no live copy covers (a live copy covers its kinds up to the end of its calendar)."""
    out: list[dict] = []
    covered: dict[tuple[str, str], int] = {}
    for source, copy in ((state or {}).get("sources") or {}).items():
        until = copy.get("until")
        if until is not None:
            for kind in copy.get("kinds") or []:
                covered[(source, kind)] = int(until)
        out.extend(dict(e) for e in copy.get("events") or [] if start_s <= int(e["time"]) <= end_s)
    for ev in schedules.static_events(start_s, end_s):
        until = covered.get((ev["source"], ev["kind"]))
        if until is None or ev["time"] > until:
            out.append(ev)
    out.sort(key=sort_key)
    return out


def coverage_rows(state: dict | None, errors: dict[str, str]) -> list[dict]:
    """CalendarCoverage source rows (MacroService.coverage) of the official calendars."""
    rows = []
    for source, spec in SOURCES.items():
        copy = ((state or {}).get("sources") or {}).get(source)
        ends = [t for t in (copy.get("until") if copy else None, schedules.last_static(source)) if t is not None]
        rows.append({"id": source, "label": spec["name"], "live": True, "ok": bool(copy) and source not in errors,
                     "updated_at": copy.get("fetched_at") if copy else None, "until": max(ends) if ends else None,
                     "error": errors.get(source)})
    for source, name in CURATED.items():
        rows.append({"id": source, "label": name, "live": False, "ok": True, "updated_at": None,
                     "until": schedules.last_static(source), "error": None})
    return rows


# ----------------------------------------------------------------------------- persistence


def clean_copy(source: str, copy: Any) -> dict | None:
    """A stored / fetched copy of `source` with only valid events and known kinds (None when unusable)."""
    if not isinstance(copy, dict) or not isinstance(copy.get("events"), list):
        return None
    events = [e for e in copy["events"] if _valid_event(e) and e.get("source") == source
              and e.get("kind") in schedules.KINDS and isinstance(e.get("title_es"), str)]
    raw_kinds = copy.get("kinds")
    kinds = sorted({k for k in raw_kinds if k in schedules.KINDS}) if isinstance(raw_kinds, list) else []
    until, fetched = copy.get("until"), copy.get("fetched_at")
    return {"events": sorted(events, key=sort_key), "kinds": kinds,
            "until": until if isinstance(until, int) else None,
            "fetched_at": int(fetched) if isinstance(fetched, (int, float)) else 0}


async def load_official(db: Any) -> dict | None:
    row = await db.fetchone("SELECT state FROM model_state WHERE kind=? AND key=?", (STATE_KIND, STATE_KEY))
    if not row:
        return None
    try:
        state = json.loads(row["state"])
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("sources"), dict):
        return None
    out = empty_state()
    for source in SOURCES:
        copy = clean_copy(source, state["sources"].get(source))
        if copy is not None:
            out["sources"][source] = copy
    return out


async def save_official(db: Any, state: dict) -> None:
    payload = json.dumps(clean_json(state), ensure_ascii=False, separators=(",", ":"))
    ends = [c.get("until") for c in state.get("sources", {}).values() if c.get("until") is not None]
    await db.execute(
        "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, key) DO UPDATE SET state=excluded.state, updates=excluded.updates, "
        "trained_until=excluded.trained_until, updated_at=excluded.updated_at",
        (STATE_KIND, STATE_KEY, payload, len(state.get("sources", {})), max(ends) if ends else None, now_ms()),
    )


# ----------------------------------------------------------------------------- network


async def fetch_source(source: str, client: Any = None, now_s: int | None = None) -> dict:
    """GET one official ICS calendar (plain request) -> stored copy. Raises CalendarError (short Spanish reason)."""
    own = client is None
    http = client if client is not None else http_client(timeout=20.0)
    try:
        try:
            resp = await http.get(SOURCES[source]["url"])
        except Exception as exc:
            raise CalendarError(f"sin conexión ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise CalendarError(f"HTTP {resp.status_code}")
        text = str(resp.text or "")
        if len(text) > MAX_BYTES:
            raise CalendarError("respuesta demasiado grande")
        entries = parse_ics(text)
        if not entries:
            raise CalendarError("el calendario llegó vacío")
        copy = build_copy(source, entries, now_ms(), int(time.time()) if now_s is None else int(now_s))
        if not copy["kinds"]:
            raise CalendarError("el calendario no trae ninguno de los datos esperados")
        return copy
    finally:
        if own:
            await http.aclose()


async def fetch_all(client: Any = None) -> dict[str, dict | Exception]:
    """Every live official source -> its new copy, or the exception that prevented it (one bad source never
    hides the others)."""
    own = client is None
    http = client if client is not None else http_client(timeout=20.0)
    out: dict[str, dict | Exception] = {}
    try:
        for source in SOURCES:
            try:
                out[source] = await fetch_source(source, http)
            except Exception as exc:
                out[source] = exc
    finally:
        if own:
            await http.aclose()
    return out
