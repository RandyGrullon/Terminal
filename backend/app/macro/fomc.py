"""Official FOMC meeting schedule (federalreserve.gov) as MacroEvent dicts.

The rate decision is published at 14:00 America/New_York on the second day of each meeting and the Chair's press
conference starts at 14:30. Meetings marked with SEP publish the Summary of Economic Projections (dot plot), which
usually makes them more volatile. 2027 dates are tentative (as published by the Fed).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
SOURCE = "federalreserve"
DECISION_HOUR = (14, 0)
PRESS_HOUR = (14, 30)

# (first day, decision day, with Summary of Economic Projections)
MEETINGS: tuple[tuple[str, str, bool], ...] = (
    ("2026-01-27", "2026-01-28", False),
    ("2026-03-17", "2026-03-18", True),
    ("2026-04-28", "2026-04-29", False),
    ("2026-06-16", "2026-06-17", True),
    ("2026-07-28", "2026-07-29", False),
    ("2026-09-15", "2026-09-16", True),
    ("2026-10-27", "2026-10-28", False),
    ("2026-12-08", "2026-12-09", True),
    ("2027-01-26", "2027-01-27", False),
    ("2027-03-16", "2027-03-17", True),
    ("2027-04-27", "2027-04-28", False),
    ("2027-06-08", "2027-06-09", True),
    ("2027-07-27", "2027-07-28", False),
    ("2027-09-14", "2027-09-15", True),
    ("2027-10-26", "2027-10-27", False),
    ("2027-12-07", "2027-12-08", True),
)

DECISION_TITLE = "FOMC Statement"
PRESS_TITLE = "FOMC Press Conference"
DECISION_ES = "Decisión de tipos de la Fed (FOMC)"
DECISION_SEP_ES = "Decisión de tipos de la Fed (FOMC, con proyecciones económicas)"
PRESS_ES = "Rueda de prensa del presidente de la Fed"
PRESS_SEP_ES = "Rueda de prensa del presidente de la Fed (con proyecciones económicas)"


def ny_time(day: str, hour: int, minute: int) -> int:
    """Unix seconds of `day` (YYYY-MM-DD) at hour:minute New York time (DST handled by zoneinfo)."""
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=NEW_YORK).timestamp())


def _event(kind: str, day: str, sep: bool) -> dict:
    hour, minute = DECISION_HOUR if kind == "decision" else PRESS_HOUR
    if kind == "decision":
        title, title_es = DECISION_TITLE, (DECISION_SEP_ES if sep else DECISION_ES)
    else:
        title, title_es = PRESS_TITLE, (PRESS_SEP_ES if sep else PRESS_ES)
    return {
        "id": f"fomc-{day}-{kind}",
        "title": title,
        "title_es": title_es,
        "currency": "USD",
        "time": ny_time(day, hour, minute),
        "impact": "high",
        "forecast": None,
        "previous": None,
        "source": SOURCE,
        "sep": sep,
        "kind": f"fomc_{kind}",
    }


def fomc_events(start_s: int | None = None, end_s: int | None = None) -> list[dict]:
    """Decision + press conference events of every scheduled meeting, optionally limited to [start_s, end_s]."""
    out: list[dict] = []
    for _first, day, sep in MEETINGS:
        for kind in ("decision", "press"):
            ev = _event(kind, day, sep)
            if start_s is not None and ev["time"] < start_s:
                continue
            if end_s is not None and ev["time"] > end_s:
                continue
            out.append(ev)
    return out


def meeting_for(ts: int, tolerance_s: int = 6 * 3600) -> tuple[str, str, bool] | None:
    """The meeting whose decision day contains (or is within `tolerance_s` of) unix second `ts`."""
    for meeting in MEETINGS:
        decision = ny_time(meeting[1], *DECISION_HOUR)
        if abs(ts - decision) <= tolerance_s:
            return meeting
    return None


def next_meeting(now_s: int) -> dict | None:
    """Next decision event at or after `now_s` (None once the published schedule is exhausted)."""
    for _first, day, sep in MEETINGS:
        ev = _event("decision", day, sep)
        if ev["time"] >= now_s:
            return ev
    return None
