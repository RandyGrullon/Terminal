"""Curated official release schedules (2026-2027), highest-impact events only, as internal calendar events.

Every date below was read on 2026-10-31 from the official page named in its comment, fetched with a plain request
and the app's honest User-Agent (app.util.http_client). Only dates after that day are listed. Agencies can move a
release (a government shutdown did in 2025), so this is a fallback: the live official calendars (official.py) and
the weekly ForexFactory feed win wherever they cover a date. Never add a date that was not checked on its source.

The FOMC schedule lives in fomc.py (federalreserve.gov, checked again on the same day). Release times:
- BLS and BEA: 08:30 New York time (the schedule pages and the ICS calendars of both agencies).
- ECB: decision 14:15 and press conference 14:45 Frankfurt time ("Monetary policy decisions" item of
  https://www.ecb.europa.eu/rss/press.xml at 29 Oct 2026 14:15 +0100; that day's press release: "press conference
  starting at 14:45 CET"). The meeting calendar gives the dates only, so the time is the ECB's usual one.
- BoE: 12:00 London time (https://www.bankofengland.co.uk/rss/news: the Bank Rate announcements of 30 Jul 2026 and
  17 Sep 2026 were published at 12:00 +0100).
- BoJ: no fixed time, the statement is released when the meeting ends (12:02 Tokyo time on 30 Oct 2026, see
  https://www.boj.or.jp/en/rss/whatsnew.xml). 12:00 Tokyo is only a reference and the title says so.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.macro.fomc import NEW_YORK

FRANKFURT = ZoneInfo("Europe/Berlin")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")
VERIFIED_ON = "2026-10-31"
VERIFIED_LABEL = "31/10/2026"  # the same date, as shown to the user

# BLS, "Schedule of Releases for the Consumer Price Index" (08:30 AM):
# https://www.bls.gov/schedule/news_release/cpi.htm (fetched 2026-10-31).
BLS_CPI: tuple[str, ...] = (
    "2026-11-10", "2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12",
    "2027-06-10", "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10",
)
# BLS, "Schedule of Releases for the Employment Situation" (08:30 AM):
# https://www.bls.gov/schedule/news_release/empsit.htm (fetched 2026-10-31).
BLS_EMPLOYMENT: tuple[str, ...] = (
    "2026-11-06", "2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02", "2027-05-07",
    "2027-06-04", "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03",
)
# BEA release schedule (8:30 AM): https://www.bea.gov/news/schedule/full (2026) and
# https://www.bea.gov/news/schedule/next-year (2027), both fetched 2026-10-31; the same dates are in
# https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics. On each day BEA publishes GDP (estimate
# below) and "Personal Income and Outlays" (PCE inflation) together.
BEA_GDP_PCE: tuple[tuple[str, str], ...] = (
    ("2026-11-25", "second"), ("2026-12-23", "third"), ("2027-01-28", "advance"), ("2027-02-25", "second"),
    ("2027-03-31", "third"), ("2027-04-29", "advance"), ("2027-05-27", "second"), ("2027-06-25", "third"),
    ("2027-07-29", "advance"), ("2027-08-25", "second"), ("2027-09-30", "third"), ("2027-10-28", "advance"),
    ("2027-11-24", "second"), ("2027-12-22", "third"),
)
# ECB, "Meetings of the Governing Council and the General Council": the days marked "monetary policy meeting ...
# (Day 2), followed by press conference": https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
# (fetched 2026-10-31).
ECB_DECISIONS: tuple[str, ...] = (
    "2026-12-17", "2027-02-04", "2027-03-18", "2027-04-29", "2027-06-10", "2027-07-22", "2027-09-09",
    "2027-10-28", "2027-12-16",
)
# Bank of England, "Monetary Policy Committee dates for 2026 and 2027" (confirmed dates):
# https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates (fetched 2026-10-31).
BOE_DECISIONS: tuple[str, ...] = (
    "2026-11-05", "2026-12-17", "2027-02-04", "2027-03-18", "2027-04-29", "2027-06-17", "2027-07-29",
    "2027-09-16", "2027-11-04", "2027-12-16",
)
# Bank of Japan, "Monetary Policy Meetings" (second day of each meeting, when the statement is released):
# https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm (fetched 2026-10-31).
BOJ_DECISIONS: tuple[str, ...] = (
    "2026-12-18", "2027-01-22", "2027-03-18", "2027-04-28", "2027-06-11", "2027-07-22", "2027-09-22",
    "2027-10-29", "2027-12-17",
)

# kind -> (currency, Spanish title, impact). Impacts follow the weekly feed's rating of the same releases.
KINDS: dict[str, tuple[str, str, str]] = {
    "us_cpi": ("USD", "IPC de EE. UU. (inflación)", "high"),
    "us_nfp": ("USD", "Informe de empleo de EE. UU. (nóminas NFP y desempleo)", "high"),
    "us_ppi": ("USD", "Precios al productor de EE. UU. (IPP)", "medium"),
    "us_jolts": ("USD", "Ofertas de empleo JOLTS de EE. UU.", "medium"),
    "us_eci": ("USD", "Índice de costo laboral de EE. UU. (trimestral)", "medium"),
    "us_gdp": ("USD", "PIB de EE. UU. (trimestral)", "high"),
    "us_pce": ("USD", "Ingresos y gasto personal de EE. UU. (inflación PCE)", "high"),
    "ecb_decision": ("EUR", "Decisión de tipos del BCE", "high"),
    "ecb_press": ("EUR", "Rueda de prensa del BCE", "high"),
    "boe_decision": ("GBP", "Decisión de tipos del Banco de Inglaterra", "high"),
    "boj_decision": ("JPY", "Decisión de tipos del Banco de Japón (hora aproximada)", "high"),
}
# GDP estimate -> (BEA wording, Spanish title, impact).
GDP_ESTIMATES: dict[str, tuple[str, str, str]] = {
    "advance": ("Advance Estimate", "PIB adelantado de EE. UU. (trimestral)", "high"),
    "second": ("Second Estimate", "PIB de EE. UU., segunda estimación (trimestral)", "medium"),
    "third": ("Third Estimate", "PIB de EE. UU., tercera estimación (trimestral)", "medium"),
}


def local_time(day: str, hour: int, minute: int, tz: dt.tzinfo) -> int:
    """Unix seconds of `day` (YYYY-MM-DD) at hour:minute in zone `tz` (DST handled by zoneinfo)."""
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).timestamp())


def make_event(source: str, kind: str, ts: int, title: str, *, static: bool, title_es: str | None = None,
               impact: str | None = None) -> dict:
    """Internal calendar event of an official source: the MacroEvent fields plus `kind` and `static` (curated
    copy). The id depends only on source, kind and minute, so a live and a curated copy of a release share it."""
    currency, default_es, default_impact = KINDS[kind]
    stamp = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y%m%d-%H%M")
    return {
        "id": f"{source}-{kind}-{stamp}", "title": title, "title_es": title_es or default_es, "currency": currency,
        "time": int(ts), "impact": impact or default_impact, "forecast": None, "previous": None, "source": source,
        "kind": kind, "static": bool(static),
    }


@lru_cache(maxsize=1)
def _all_static() -> tuple[dict, ...]:
    out: list[dict] = []

    def add(source: str, kind: str, day: str, hm: tuple[int, int], tz: dt.tzinfo, title: str, **kw: str) -> None:
        out.append(make_event(source, kind, local_time(day, hm[0], hm[1], tz), title, static=True, **kw))

    for day in BLS_CPI:
        add("bls", "us_cpi", day, (8, 30), NEW_YORK, "Consumer Price Index")
    for day in BLS_EMPLOYMENT:
        add("bls", "us_nfp", day, (8, 30), NEW_YORK, "Employment Situation")
    for day, estimate in BEA_GDP_PCE:
        wording, title_es, impact = GDP_ESTIMATES[estimate]
        add("bea", "us_gdp", day, (8, 30), NEW_YORK, f"GDP ({wording})", title_es=title_es, impact=impact)
        add("bea", "us_pce", day, (8, 30), NEW_YORK, "Personal Income and Outlays")
    for day in ECB_DECISIONS:
        add("ecb", "ecb_decision", day, (14, 15), FRANKFURT, "ECB monetary policy decisions")
        add("ecb", "ecb_press", day, (14, 45), FRANKFURT, "ECB press conference")
    for day in BOE_DECISIONS:
        add("boe", "boe_decision", day, (12, 0), LONDON, "BoE Bank Rate decision")
    for day in BOJ_DECISIONS:
        add("boj", "boj_decision", day, (12, 0), TOKYO, "BoJ monetary policy decision")
    out.sort(key=lambda e: (e["time"], e["id"]))
    return tuple(out)


def static_events(start_s: int | None = None, end_s: int | None = None) -> list[dict]:
    """Copies of the curated official events within [start_s, end_s] (None = unbounded), oldest first."""
    return [dict(e) for e in _all_static()
            if (start_s is None or e["time"] >= start_s) and (end_s is None or e["time"] <= end_s)]


def last_static(source: str) -> int | None:
    """Unix second of the last curated event of `source` (how far its curated schedule reaches)."""
    times = [e["time"] for e in _all_static() if e["source"] == source]
    return max(times) if times else None
