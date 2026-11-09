"""Past official release dates (February 2024 – October 2026) of the US events with the biggest market impact: CPI,
the Employment Situation report (NFP) and the FOMC decisions. They exist so that PyChart backtests can avoid trading
around news (``noticias`` in app.scripting.runtime_news): the live calendars and schedules.py only reach forward.

Every date was read on 2026-11-09 from the official page named in its comment, fetched with a plain request. The
BLS archive pages list each release by its link (``cpi_MMDDYYYY.htm`` / ``empsit_MMDDYYYY.htm``): these are the
ACTUAL publication dates, including the ones moved by the 2025 lapse in federal appropriations (September 2025 CPI on
24 Oct 2025, September 2025 employment on 20 Nov 2025; the October 2025 CPI and employment reports were not
published on their own). Never add a date that was not checked on its source.

Release times: BLS 08:30 New York time; FOMC statement 14:00 and press conference 14:30 New York time (fomc.py). The
FOMC notation vote of 22 Aug 2025 is not listed: it was not a scheduled decision with a statement at 14:00.

Coverage is continuous with the forward schedules: history ends on 2026-10-31 and schedules.py / fomc.py go on from
there (CPI 2026-11-10, employment 2026-11-06, FOMC 2026 in fomc.py). Before FROM_DAY the data is incomplete.
"""
from __future__ import annotations

from functools import lru_cache

from app.macro import fomc, schedules
from app.macro.fomc import NEW_YORK

FROM_DAY = "2024-02-01"  # first day with every principal kind covered (see BLS_CPI)
UNTIL_DAY = "2026-10-31"
VERIFIED_ON = "2026-11-09"

# https://www.bls.gov/bls/news-release/cpi.htm (archive of CPI releases, read 2026-11-09). Reference months
# Jan 2024 … Sep 2026; October 2025 was not published. The January 2024 release (December 2023 data) was not
# checked, so coverage starts on FROM_DAY.
BLS_CPI: tuple[str, ...] = (
    "2024-02-13", "2024-03-12", "2024-04-10", "2024-05-15", "2024-06-12", "2024-07-11", "2024-08-14", "2024-09-11",
    "2024-10-10", "2024-11-13", "2024-12-11", "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10", "2025-05-13",
    "2025-06-11", "2025-07-15", "2025-08-12", "2025-09-11", "2025-10-24", "2025-12-18", "2026-01-13", "2026-02-13",
    "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14",
)

# https://www.bls.gov/bls/news-release/empsit.htm (archive of Employment Situation releases, read 2026-11-09).
# Reference months Jan 2024 … Sep 2026; October 2025 was not published on its own. The October 2026 report
# (2026-11-06) is in schedules.BLS_EMPLOYMENT.
BLS_EMPLOYMENT: tuple[str, ...] = (
    "2024-02-02", "2024-03-08", "2024-04-05", "2024-05-03", "2024-06-07", "2024-07-05", "2024-08-02", "2024-09-06",
    "2024-10-04", "2024-11-01", "2024-12-06", "2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04", "2025-05-02",
    "2025-06-06", "2025-07-03", "2025-08-01", "2025-09-05", "2025-11-20", "2025-12-16", "2026-01-09", "2026-02-11",
    "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07", "2026-09-04", "2026-10-02",
)

# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (read 2026-11-09): (first day, decision day, SEP).
# 2026 and later are in fomc.MEETINGS.
FOMC_MEETINGS: tuple[tuple[str, str, bool], ...] = (
    ("2024-01-30", "2024-01-31", False),
    ("2024-03-19", "2024-03-20", True),
    ("2024-04-30", "2024-05-01", False),
    ("2024-06-11", "2024-06-12", True),
    ("2024-07-30", "2024-07-31", False),
    ("2024-09-17", "2024-09-18", True),
    ("2024-11-06", "2024-11-07", False),
    ("2024-12-17", "2024-12-18", True),
    ("2025-01-28", "2025-01-29", False),
    ("2025-03-18", "2025-03-19", True),
    ("2025-05-06", "2025-05-07", False),
    ("2025-06-17", "2025-06-18", True),
    ("2025-07-29", "2025-07-30", False),
    ("2025-09-16", "2025-09-17", True),
    ("2025-10-28", "2025-10-29", False),
    ("2025-12-09", "2025-12-10", True),
)

# Kinds with continuous data from FROM_DAY on (history here + forward schedules): the «principales» of PyChart.
PRINCIPAL_KINDS = frozenset({"us_cpi", "us_nfp", "fomc_decision", "fomc_press"})


@lru_cache(maxsize=1)
def _all() -> tuple[dict, ...]:
    out: list[dict] = []
    for day in BLS_CPI:
        ts = schedules.local_time(day, 8, 30, NEW_YORK)
        out.append(schedules.make_event("bls", "us_cpi", ts, "Consumer Price Index", static=True))
    for day in BLS_EMPLOYMENT:
        ts = schedules.local_time(day, 8, 30, NEW_YORK)
        out.append(schedules.make_event("bls", "us_nfp", ts, "Employment Situation", static=True))
    for _first, day, sep in FOMC_MEETINGS:
        out.append(fomc.meeting_event("decision", day, sep))
        out.append(fomc.meeting_event("press", day, sep))
    out.sort(key=lambda e: (e["time"], e["id"]))
    return tuple(out)


def history_events(start_s: int | None = None, end_s: int | None = None) -> list[dict]:
    """Copies of the past official events within [start_s, end_s] (None = unbounded), oldest first."""
    return [dict(e) for e in _all()
            if (start_s is None or e["time"] >= start_s) and (end_s is None or e["time"] <= end_s)]


def from_s() -> int:
    """Unix second from which the principal events are complete (00:00 New York time of FROM_DAY)."""
    return schedules.local_time(FROM_DAY, 0, 0, NEW_YORK)


def until_s() -> int:
    """Unix second up to which every principal kind has a scheduled date (the shortest forward schedule)."""
    ends = [max(schedules.local_time(d, 8, 30, NEW_YORK) for d in schedules.BLS_CPI),
            max(schedules.local_time(d, 8, 30, NEW_YORK) for d in schedules.BLS_EMPLOYMENT),
            max(e["time"] for e in fomc.fomc_events(None, None))]
    return min(ends)
