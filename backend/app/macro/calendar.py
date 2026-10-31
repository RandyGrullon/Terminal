"""Economic calendar: the ForexFactory weekly JSON feed (plain request, honest UA) merged with the official release
calendars (FOMC in fomc.py; BLS, BEA, ECB, BoE and BoJ in official.py / schedules.py), deduplicated by currency,
release and time. The last good feed copy lives in model_state(kind='macro', key='calendar') so the semáforo keeps
working offline and across restarts; it is stale once the cached week has ended. ForexFactory only publishes the
current week (ff_calendar_nextweek.json answered HTTP 404 on 2026-10-31), so later weeks come from the official
calendars alone: their high-impact releases, without forecast / previous."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

from app.macro import fomc
from app.macro.titles import spanish_title
from app.util import clean_json, http_client, now_ms

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE = "forexfactory"
REFRESH_S = 3600  # successful refreshes at most every 60 min (the feed asks clients to be gentle)
RETRY_S = 900  # after a failure, try again in 15 min (the cached copy keeps serving meanwhile)
STATE_KIND, STATE_KEY = "macro", "calendar"
IMPACTS = {"high": "high", "medium": "medium", "low": "low", "holiday": "holiday", "non-economic": "low"}
IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "holiday": 0}
EVENT_FIELDS = ("id", "title", "title_es", "currency", "time", "impact", "forecast", "previous", "source")
# Feed titles that are the same thing as an official FOMC event (decision / press conference).
FOMC_FEED_TITLES = {
    "fomc_decision": frozenset({"FOMC Statement", "Federal Funds Rate"}),
    "fomc_press": frozenset({"FOMC Press Conference"}),
}
# Feed titles (same currency) that are the same release as an official event of each kind (official.py).
FEED_MATCH: dict[str, re.Pattern[str]] = {
    "us_cpi": re.compile(r"^(Core )?CPI (m/m|y/y)$"),
    "us_nfp": re.compile(r"^(Non-Farm Employment Change|Unemployment Rate|Average Hourly Earnings m/m)$"),
    "us_ppi": re.compile(r"^(Core )?PPI (m/m|y/y)$"),
    "us_jolts": re.compile(r"^JOLTS Job Openings$"),
    "us_eci": re.compile(r"^Employment Cost Index q/q$"),
    "us_gdp": re.compile(r"^(Advance |Prelim |Final )?GDP (Price Index )?q/q$"),
    "us_pce": re.compile(r"^((Core )?PCE Price Index (m/m|y/y)|Personal (Income|Spending) m/m)$"),
    "ecb_decision": re.compile(r"^(Main Refinancing Rate|Deposit Facility Rate|Monetary Policy Statement)$"),
    "ecb_press": re.compile(r"^ECB Press Conference$"),
    "boe_decision": re.compile(r"^(Official Bank Rate|MPC Official Bank Rate Votes|Monetary Policy Summary"
                               r"|(BOE )?Monetary Policy Report)$"),
    "boj_decision": re.compile(r"^(BOJ Policy Rate|BOJ Outlook Report|Monetary Policy Statement)$"),
}
SEP_NOTE = "con proyecciones económicas"
DEDUPE_WINDOW_S = 2 * 3600


class CalendarError(Exception):
    """The feed could not be fetched or parsed (Spanish message)."""


# ----------------------------------------------------------------------------- parsing


def parse_time(text: Any) -> int | None:
    """ISO 8601 with offset ('2026-10-28T14:00:00-04:00') -> unix seconds. Naive times are New York time."""
    s = str(text or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=fomc.NEW_YORK)
    return int(d.timestamp())


def _text(value: Any) -> str | None:
    s = " ".join(str(value).split()) if value is not None else ""
    return s or None


def event_id(currency: str, title: str, ts: int) -> str:
    return "ff-" + hashlib.sha1(f"{currency}|{title}|{ts}".encode("utf-8")).hexdigest()[:12]


def sort_key(ev: dict) -> tuple:
    return (int(ev["time"]), -IMPACT_RANK.get(ev.get("impact"), 0), str(ev.get("title") or ""))


def parse_feed(data: Any) -> list[dict]:
    """ForexFactory JSON list -> internal events (MacroEvent fields without countdown), sorted by time."""
    if not isinstance(data, list):
        raise CalendarError("el calendario económico no tiene el formato esperado")
    out: list[dict] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        ts = parse_time(item.get("date"))
        if not title or ts is None:
            continue
        currency = "".join(str(item.get("country") or "").split()).upper()[:8]
        impact = IMPACTS.get(str(item.get("impact") or "").strip().lower(), "low")
        eid = event_id(currency, title, ts)
        if eid in seen:
            continue
        seen.add(eid)
        out.append({
            "id": eid, "title": title, "title_es": spanish_title(title, currency), "currency": currency,
            "time": ts, "impact": impact, "forecast": _text(item.get("forecast")),
            "previous": _text(item.get("previous")), "source": SOURCE,
        })
    out.sort(key=sort_key)
    return out


def with_sep_note(title_es: str) -> str:
    """'Comunicado de la Fed (FOMC)' -> 'Comunicado de la Fed (FOMC, con proyecciones económicas)'."""
    if SEP_NOTE in title_es:
        return title_es
    if title_es.endswith(")") and "(" in title_es:
        return f"{title_es[:-1]}, {SEP_NOTE})"
    return f"{title_es} ({SEP_NOTE})"


def feed_matches(kind: Any, title: str) -> bool:
    """Is the feed title `title` the same release as an official event of `kind`?"""
    if kind in FOMC_FEED_TITLES:
        return title in FOMC_FEED_TITLES[kind]
    pattern = FEED_MATCH.get(str(kind))
    return bool(pattern and pattern.match(title))


def merge_extra(events: list[dict], extra: list[dict]) -> list[dict]:
    """Feed events + official events (FOMC, BLS, BEA, ECB, BoE, BoJ), sorted by time.

    An official event the feed already lists (same currency and release, ±2 h) is dropped: the feed copy carries
    forecast / previous (an FOMC copy also becomes high impact and inherits the SEP note). Two official copies of one
    release (live and curated, same kind and currency, ±2 h) are kept once: the first one in `extra` wins."""
    out = [dict(e) for e in events]
    feed = [e for e in out if e.get("source") == SOURCE]
    ids = {str(e.get("id")) for e in out}
    kept: list[dict] = []
    for ev in extra:
        t, kind, currency = int(ev["time"]), ev.get("kind"), ev.get("currency")
        if str(ev.get("id")) in ids:
            continue
        dupes = [e for e in feed if e.get("currency") == currency and abs(int(e["time"]) - t) <= DEDUPE_WINDOW_S
                 and feed_matches(kind, str(e.get("title") or ""))]
        if dupes:
            if str(kind).startswith("fomc_"):
                for e in dupes:
                    e["impact"] = "high"
                    if ev.get("sep"):
                        e["title_es"] = with_sep_note(str(e.get("title_es") or e["title"]))
            continue
        if any(o.get("kind") == kind and o.get("currency") == currency and abs(int(o["time"]) - t) <= DEDUPE_WINDOW_S
               for o in kept):
            continue
        kept.append(dict(ev))
        ids.add(str(ev.get("id")))
    out.extend(kept)
    out.sort(key=sort_key)
    return out


def merge_fomc(events: list[dict], start_s: int | None = None, end_s: int | None = None) -> list[dict]:
    """Feed events + official FOMC events in [start_s, end_s] (see merge_extra)."""
    return merge_extra(events, fomc.fomc_events(start_s, end_s))


def public_event(ev: dict, now_s: int) -> dict:
    """Exactly the MacroEvent shape of types.ts (internal keys dropped, countdown added)."""
    out = {k: ev.get(k) for k in EVENT_FIELDS}
    out["time"] = int(ev["time"])
    out["countdown_s"] = int(ev["time"]) - int(now_s)
    return out


def select(events: list[dict], *, start_s: int, end_s: int, impacts: tuple[str, ...] | None = None,
           currencies: set[str] | None = None) -> list[dict]:
    """Events within [start_s, end_s], optionally filtered by impact and currency."""
    wanted = set(impacts) if impacts else None
    out = []
    for ev in events:
        t = int(ev["time"])
        if t < start_s or t > end_s:
            continue
        if wanted is not None and ev.get("impact") not in wanted:
            continue
        if currencies is not None and ev.get("currency") not in currencies:
            continue
        out.append(ev)
    return out


# ----------------------------------------------------------------------------- week & staleness


def week_bounds(events: list[dict]) -> tuple[int, int] | None:
    """[Sunday 00:00, next Sunday 00:00) New York time of the week the feed covers (from its median event)."""
    times = sorted(int(e["time"]) for e in events)
    if not times:
        return None
    day = dt.datetime.fromtimestamp(times[len(times) // 2], fomc.NEW_YORK).date()
    sunday = day - dt.timedelta(days=(day.weekday() + 1) % 7)
    nxt = sunday + dt.timedelta(days=7)
    start = dt.datetime(sunday.year, sunday.month, sunday.day, tzinfo=fomc.NEW_YORK)
    end = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=fomc.NEW_YORK)
    return int(start.timestamp()), int(end.timestamp())


def is_stale(cache: dict | None, now_s: int) -> bool:
    if not cache or not cache.get("events"):
        return True
    week_end = cache.get("week_end")
    return week_end is None or int(now_s) >= int(week_end)


def make_cache(events: list[dict], fetched_at_ms: int | None = None, refreshes: int = 1) -> dict:
    bounds = week_bounds(events)
    return {
        "version": 1,
        "events": [{k: e.get(k) for k in EVENT_FIELDS} for e in events],
        "fetched_at": int(fetched_at_ms if fetched_at_ms is not None else now_ms()),
        "week_start": bounds[0] if bounds else None,
        "week_end": bounds[1] if bounds else None,
        "refreshes": int(refreshes),
    }


# ----------------------------------------------------------------------------- persistence


def _valid_event(ev: Any) -> bool:
    return (isinstance(ev, dict) and isinstance(ev.get("time"), int) and bool(ev.get("title"))
            and ev.get("impact") in IMPACT_RANK)


async def load_cached(db: Any) -> dict | None:
    row = await db.fetchone(
        "SELECT state, updated_at FROM model_state WHERE kind=? AND key=?", (STATE_KIND, STATE_KEY)
    )
    if not row:
        return None
    try:
        state = json.loads(row["state"])
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("events"), list):
        return None
    events = [e for e in state["events"] if _valid_event(e)]
    for e in events:
        e.setdefault("title_es", spanish_title(e["title"], str(e.get("currency") or "")))
        e.setdefault("source", SOURCE)
    events.sort(key=sort_key)
    cache = make_cache(events, int(state.get("fetched_at") or row.get("updated_at") or 0),
                       int(state.get("refreshes") or 1))
    return cache


async def save_cached(db: Any, cache: dict) -> None:
    payload = json.dumps(clean_json(cache), ensure_ascii=False, separators=(",", ":"))
    await db.execute(
        "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, key) DO UPDATE SET state=excluded.state, updates=excluded.updates, "
        "trained_until=excluded.trained_until, updated_at=excluded.updated_at",
        (STATE_KIND, STATE_KEY, payload, int(cache.get("refreshes") or 1), cache.get("week_end"), now_ms()),
    )


# ----------------------------------------------------------------------------- network


async def fetch_feed(client: Any = None) -> list[dict]:
    """GET the weekly feed (one plain request). Raises CalendarError with a Spanish message."""
    own = client is None
    http = client if client is not None else http_client(timeout=15.0)
    try:
        try:
            resp = await http.get(FF_URL)
        except Exception as exc:
            raise CalendarError(f"sin conexión con el calendario económico ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise CalendarError(f"el calendario económico respondió HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise CalendarError("el calendario económico no devolvió JSON (límite de peticiones)") from None
        events = parse_feed(data)
        if not events:
            raise CalendarError("el calendario económico llegó vacío")
        return events
    finally:
        if own:
            await http.aclose()
