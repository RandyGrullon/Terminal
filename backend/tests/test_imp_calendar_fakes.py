"""Samples and helpers for the calendar-beyond-this-week tests (official release calendars).

(No tests in this module.) The ICS samples are trimmed copies of the real calendars fetched on 2026-10-31 with the
app's honest client: https://www.bls.gov/schedule/news_release/bls.ics (LF line endings, TZID=US-Eastern) and
https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics (CRLF, UTC times, folded lines).
"""
from __future__ import annotations

import datetime as dt

from app.macro import official as off

UTC = dt.timezone.utc

_BLS_EVENTS = [
    ("20250110T083000", "Employment Situation"),  # old: counts for `kinds`, not kept
    ("20261030T083000", "Employment Cost Index"),
    ("20261103T100000", "Job Openings and Labor Turnover Survey"),
    ("20261106T083000", "Employment Situation"),
    ("20261110T083000", "Real Earnings"),  # not mapped (published with the CPI)
    ("20261110T083000", "Consumer Price Index"),
    ("20261113T083000", "Producer Price Index"),
    ("20261230T100000", "Metropolitan Area Employment and Unemployment (Monthly)"),  # last entry of the calendar
]


def _bls_event(start: str, summary: str, n: int) -> str:
    return (f"BEGIN:VEVENT\nSEQUENCE:1\nCLASS:PUBLIC\nUID:sample-{n}\nDTSTART;TZID=US-Eastern:{start}\n"
            f"DURATION:PT0M\nSUMMARY:{summary}\nLOCATION:Washington\\, DC\nTRANSP:TRANSPARENT\n"
            f"CATEGORIES:IMPORTANT, BLS\nEND:VEVENT\n")


BLS_ICS = (
    "BEGIN:VCALENDAR\nPRODID:-//Department of Labor//Bureau of Labor Statistics//EN\nVERSION:2.0\n"
    "CALSCALE:GREGORIAN\nMETHOD:PUBLISH\nSUMMARY:BLS.gov Economic News Release Schedule\n"
    "X-WR-CALNAME:BLS.gov Economic News Release Schedule\nX-WR-TIMEZONE:US-Eastern\nBEGIN:VTIMEZONE\n"
    "TZID:US-Eastern\nBEGIN:DAYLIGHT\nTZOFFSETFROM:-0500\nTZOFFSETTO:-0400\nDTSTART:20070311T020000\n"
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\nTZNAME:EDT\nEND:DAYLIGHT\nBEGIN:STANDARD\nTZOFFSETFROM:-0400\n"
    "TZOFFSETTO:-0500\nDTSTART:20071104T020000\nRRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\nTZNAME:EST\n"
    "END:STANDARD\nEND:VTIMEZONE\n\n\n\n"
    + "".join(_bls_event(start, summary, n) for n, (start, summary) in enumerate(_BLS_EVENTS))
    + "END:VCALENDAR\n"
)

BEA_ICS = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID://BEA-Release-Calendar-Subscription//",
    "NAME:BEA-Release-Calendar-Subscription", "X-WR-CALNAME:BEA-Release-Calendar-Subscription",
    "TZID:America/New_York", "X-WR-TIMEZONE:America/New_York",
    "BEGIN:VEVENT", "SUMMARY:Gross Domestic Product\\, 4th Quarter and Year 2024 (Advance Estima", " te)",
    "DTSTART;VALUE=DATE-TIME:20250130T133000Z", "DURATION:PT30M", "UID:2ec758be", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:GDP (Advance Estimate)\\, 3rd Quarter 2026", "DTSTART:20261029T123000Z",
    "DURATION:PT30M", "UID:afcb09fd", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Personal Income and Outlays\\, September 2026", "DTSTART:20261029T123000Z",
    "DURATION:PT30M", "UID:ff8bd8ad", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:U.S. International Trade in Goods and Services\\, September 2026",
    "DTSTART:20261104T133000Z", "DURATION:PT30M", "UID:a270c9cd", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:GDP (Second Estimate) and Corporate Profits\\, 3rd Quarter 2026",
    "DTSTART:20261125T133000Z", "DURATION:PT30M", "UID:395d58c3", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Personal Income and Outlays\\, October 2026", "DTSTART:20261125T133000Z",
    "DURATION:PT30M", "UID:c6b23055", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:GDP by County and Personal Income by County\\, 2025", "DTSTART:20261202T133000Z",
    "DURATION:PT30M", "UID:24ad66f8", "END:VEVENT",
    # the real file folds this line inside the escaped comma "\," (backslash, CRLF, space, comma)
    "BEGIN:VEVENT", "SUMMARY:GDP (Third Estimate)\\, Industries\\, Corporate Profits\\, State GDP\\",
    " , and State Personal Income\\, 3rd Quarter 2027", "DTSTART:20271222T133000Z", "DURATION:PT30M",
    "UID:fbd98f64", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Personal Income and Outlays\\, November 2027", "DTSTART:20271222T133000Z",
    "DURATION:PT30M", "UID:6c4e2eb6", "END:VEVENT",
    "END:VCALENDAR", "",
])

# What a site that refuses automated clients may answer with HTTP 200 (a page instead of a calendar).
CHALLENGE_HTML = "<!DOCTYPE html><html><head><title>Access Denied</title></head><body>Request blocked.</body></html>"

NOW_OCT31 = int(dt.datetime(2026, 10, 31, 16, 0, tzinfo=UTC).timestamp())  # Saturday 12:00 New York


def utc(*args: int) -> int:
    return int(dt.datetime(*args, tzinfo=UTC).timestamp())


def official_state(now_s: int = NOW_OCT31, bls: bool = True, bea: bool = True) -> dict:
    """Stored official state built from the samples, as after a successful refresh at `now_s`."""
    state = off.empty_state()
    if bls:
        state["sources"]["bls"] = off.build_copy("bls", off.parse_ics(BLS_ICS), now_s * 1000, now_s)
    if bea:
        state["sources"]["bea"] = off.build_copy("bea", off.parse_ics(BEA_ICS), now_s * 1000, now_s)
    return state
