"""Official release calendars beyond the current week: ICS parsing (BLS, BEA), release mapping, curated schedules
(BLS, BEA, ECB, BoE, BoJ), coverage rules, merge / dedupe with the weekly feed, persistence and fetch errors."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.macro import calendar as cal
from app.macro import official as off
from app.macro import schedules
from tests.test_imp_calendar_fakes import BEA_ICS, BLS_ICS, CHALLENGE_HTML, NOW_OCT31, official_state, utc
from tests.test_macro_fakes import FakeHTTP, FakeResponse


def by_kind(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e.get("kind") == kind]


# ----------------------------------------------------------------------------- ICS parsing


def test_parse_ics_bls_sample_times_in_new_york():
    entries = off.parse_ics(BLS_ICS)
    assert len(entries) == 8
    got = dict((summary, ts) for ts, summary in entries if ts >= utc(2026, 1, 1))
    assert got["Employment Cost Index"] == utc(2026, 10, 30, 12, 30)  # 08:30 EDT
    assert got["Job Openings and Labor Turnover Survey"] == utc(2026, 11, 3, 15, 0)  # 10:00 EST (DST ended Nov 1)
    assert got["Consumer Price Index"] == utc(2026, 11, 10, 13, 30)
    assert entries[0] == (utc(2025, 1, 10, 13, 30), "Employment Situation")


def test_parse_ics_bea_sample_unfolds_and_unescapes():
    entries = off.parse_ics(BEA_ICS)
    summaries = [s for _, s in entries]
    assert summaries[0] == "Gross Domestic Product, 4th Quarter and Year 2024 (Advance Estimate)"
    assert ("GDP (Third Estimate), Industries, Corporate Profits, State GDP, and State Personal Income, "
            "3rd Quarter 2027") in summaries
    assert entries[1] == (utc(2026, 10, 29, 12, 30), "GDP (Advance Estimate), 3rd Quarter 2026")


def test_parse_ics_rejects_pages_and_skips_untimed_entries():
    with pytest.raises(cal.CalendarError) as err:
        off.parse_ics(CHALLENGE_HTML)
    assert "no devolvió un calendario ICS" in str(err.value)
    text = "\n".join([
        "BEGIN:VCALENDAR", "X-WR-TIMEZONE:America/New_York",
        "BEGIN:VEVENT", "DTSTART;VALUE=DATE:20261126", "SUMMARY:Thanksgiving", "END:VEVENT",  # all-day
        "BEGIN:VEVENT", "DTSTART:20261106T083000", "END:VEVENT",  # no summary
        "BEGIN:VEVENT", "DTSTART;TZID=Mars/Olympus:20261106T083000", "SUMMARY:Employment Situation", "END:VEVENT",
        "BEGIN:VEVENT", "DTSTART:2026-11-06", "SUMMARY:Broken", "END:VEVENT",
        "END:VCALENDAR",
    ])
    assert off.parse_ics(text) == [(utc(2026, 11, 6, 13, 30), "Employment Situation")]  # unknown zone = calendar's


# ----------------------------------------------------------------------------- mapping and stored copies


def test_bls_copy_maps_releases_kinds_and_coverage():
    copy = off.build_copy("bls", off.parse_ics(BLS_ICS), 123, NOW_OCT31)
    assert copy["kinds"] == ["us_cpi", "us_eci", "us_jolts", "us_nfp", "us_ppi"]
    assert copy["until"] == utc(2026, 12, 30, 15, 0) and copy["fetched_at"] == 123
    events = copy["events"]
    assert [e["kind"] for e in events] == ["us_eci", "us_jolts", "us_nfp", "us_cpi", "us_ppi"]  # 2025 one dropped
    nfp = by_kind(events, "us_nfp")[0]
    assert nfp["time"] == utc(2026, 11, 6, 13, 30) and nfp["impact"] == "high" and nfp["currency"] == "USD"
    assert nfp["source"] == "bls" and nfp["static"] is False and nfp["title"] == "Employment Situation"
    assert nfp["title_es"] == "Informe de empleo de EE. UU. (nóminas NFP y desempleo)"
    assert {e["impact"] for e in events if e["kind"] in ("us_ppi", "us_jolts", "us_eci")} == {"medium"}
    static_nfp = next(e for e in schedules.static_events() if e["kind"] == "us_nfp")
    assert static_nfp["id"] == nfp["id"] and static_nfp["static"] is True  # one id per release, live or curated


def test_bea_copy_maps_gdp_estimates_and_pce():
    copy = off.build_copy("bea", off.parse_ics(BEA_ICS), 1, NOW_OCT31)
    assert copy["kinds"] == ["us_gdp", "us_pce"] and copy["until"] == utc(2027, 12, 22, 13, 30)
    rows = [(dt.datetime.fromtimestamp(e["time"], dt.timezone.utc).date().isoformat(), e["kind"], e["impact"],
             e["title_es"]) for e in copy["events"]]
    assert rows == [
        ("2026-10-29", "us_gdp", "high", "PIB adelantado de EE. UU. (trimestral)"),
        ("2026-10-29", "us_pce", "high", "Ingresos y gasto personal de EE. UU. (inflación PCE)"),
        ("2026-11-25", "us_pce", "high", "Ingresos y gasto personal de EE. UU. (inflación PCE)"),
        ("2026-11-25", "us_gdp", "medium", "PIB de EE. UU., segunda estimación (trimestral)"),
        ("2027-12-22", "us_pce", "high", "Ingresos y gasto personal de EE. UU. (inflación PCE)"),
        ("2027-12-22", "us_gdp", "medium", "PIB de EE. UU., tercera estimación (trimestral)"),
    ]  # trade and "GDP by County" are not mapped
    assert off.bea_match("Gross Domestic Product by State and Personal Income by State, 2nd Quarter 2026") is None


# ----------------------------------------------------------------------------- curated schedules


def test_curated_schedules_dates_times_and_sources():
    events = schedules.static_events()
    assert len({e["id"] for e in events}) == len(events)
    counts = {s: sum(1 for e in events if e["source"] == s) for s in ("bls", "bea", "ecb", "boe", "boj")}
    assert counts == {"bls": 28, "bea": 28, "ecb": 18, "boe": 10, "boj": 9}
    verified = utc(2026, 11, 1)
    assert all(e["time"] >= verified for e in events)  # only dates after the check (31/10/2026)
    assert all(dt.datetime.fromtimestamp(e["time"], schedules.NEW_YORK).weekday() < 5 for e in events
               if e["source"] in ("bls", "bea"))
    at = {(e["kind"], e["time"]) for e in events}
    assert ("us_cpi", utc(2026, 11, 10, 13, 30)) in at  # 08:30 EST
    assert ("us_nfp", utc(2027, 7, 2, 12, 30)) in at  # 08:30 EDT
    assert ("ecb_decision", utc(2026, 12, 17, 13, 15)) in at and ("ecb_press", utc(2026, 12, 17, 13, 45)) in at
    assert ("ecb_decision", utc(2027, 6, 10, 12, 15)) in at  # 14:15 CEST
    assert ("boe_decision", utc(2026, 11, 5, 12, 0)) in at and ("boe_decision", utc(2027, 6, 17, 11, 0)) in at
    boj = next(e for e in events if e["kind"] == "boj_decision")
    assert boj["time"] == utc(2026, 12, 18, 3, 0) and "hora aproximada" in boj["title_es"]
    nov25 = [e for e in events if e["source"] == "bea" and e["time"] == utc(2026, 11, 25, 13, 30)]
    assert {(e["kind"], e["impact"]) for e in nov25} == {("us_gdp", "medium"), ("us_pce", "high")}
    assert schedules.last_static("boe") == utc(2027, 12, 16, 12, 0) and schedules.last_static("nope") is None
    window = schedules.static_events(utc(2026, 11, 2), utc(2026, 11, 9))
    assert [(e["source"], e["kind"]) for e in window] == [("boe", "boe_decision"), ("bls", "us_nfp")]


def test_official_events_live_copy_wins_within_its_coverage():
    start, end = utc(2026, 11, 1), utc(2027, 1, 31)
    curated = off.official_events(None, start, end)
    assert any(e["kind"] == "us_nfp" and e["time"] == utc(2026, 12, 4, 13, 30) and e["static"] for e in curated)
    live = off.official_events(official_state(), start, end)
    bls = [e for e in live if e["source"] == "bls"]
    # the live calendar reaches 30/12/2026: there it is the authority (the trimmed sample has no December
    # releases), after it the curated dates apply again
    assert [(e["kind"], e["static"]) for e in bls] == [("us_jolts", False), ("us_nfp", False), ("us_cpi", False),
                                                       ("us_ppi", False), ("us_nfp", True), ("us_cpi", True)]
    assert [e["time"] for e in bls][-2:] == [utc(2027, 1, 8, 13, 30), utc(2027, 1, 13, 13, 30)]
    assert all(not e["static"] for e in live if e["source"] == "bea")  # BEA's calendar reaches 2027-12
    assert sum(1 for e in live if e["source"] == "ecb") == 2  # no live ECB source: curated 17/12 decision + press
    no_jobs = BLS_ICS.replace("SUMMARY:Employment Situation", "SUMMARY:Renamed release")
    state = off.empty_state()
    state["sources"]["bls"] = off.build_copy("bls", off.parse_ics(no_jobs), 1, NOW_OCT31)
    jobs = by_kind(off.official_events(state, start, end), "us_nfp")
    assert jobs and all(e["static"] for e in jobs)  # a kind missing from the live copy keeps its curated dates


# ----------------------------------------------------------------------------- merge with the weekly feed


def ff(title: str, currency: str, when: str, impact: str = "High") -> dict:
    return {"title": title, "country": currency, "date": when, "impact": impact, "forecast": "", "previous": ""}


def test_merge_extra_dedupes_feed_and_official_copies():
    feed = cal.parse_feed([
        ff("ADP Non-Farm Employment Change", "USD", "2026-11-04T08:15:00-05:00"),
        ff("Non-Farm Employment Change", "USD", "2026-11-06T08:30:00-05:00"),
        ff("Unemployment Rate", "USD", "2026-11-06T08:30:00-05:00"),
        ff("Official Bank Rate", "GBP", "2026-11-05T07:00:00-05:00"),
        ff("Monetary Policy Statement", "JPY", "2026-11-05T07:30:00-05:00"),
    ])
    extra = off.official_events(None, utc(2026, 11, 1), utc(2026, 11, 30))
    merged = cal.merge_extra(feed, extra)
    assert not [e for e in merged if e["source"] == "bls" and e["kind"] == "us_nfp"]  # the feed copy wins
    assert not [e for e in merged if e["source"] == "boe"]  # same release, same currency and time
    assert [e["source"] for e in merged if e["title"] == "ADP Non-Farm Employment Change"] == ["forexfactory"]
    cpi = [e for e in merged if e.get("kind") == "us_cpi"]
    assert len(cpi) == 1 and cpi[0]["source"] == "bls"  # not in the feed: the official date stays
    assert len(merged) == len(feed) + len(extra) - 2
    live_copy = dict(extra[0], id="live-copy", time=extra[0]["time"] + 60, static=False)
    twice = cal.merge_extra([], [live_copy] + extra)
    assert sum(1 for e in twice if e.get("kind") == extra[0]["kind"]) == 1 and twice[0]["id"] == "live-copy"


@pytest.mark.parametrize("kind,currency,title,match", [
    ("us_cpi", "USD", "Core CPI m/m", True),
    ("us_cpi", "USD", "CPI y/y", True),
    ("us_cpi", "USD", "Trimmed Mean CPI m/m", False),
    ("us_nfp", "USD", "Average Hourly Earnings m/m", True),
    ("us_nfp", "USD", "ADP Non-Farm Employment Change", False),
    ("us_gdp", "USD", "Prelim GDP q/q", True),
    ("us_gdp", "USD", "Advance GDP Price Index q/q", True),
    ("us_pce", "USD", "Core PCE Price Index m/m", True),
    ("us_pce", "USD", "Personal Spending m/m", True),
    ("ecb_decision", "EUR", "Main Refinancing Rate", True),
    ("ecb_press", "EUR", "ECB Press Conference", True),
    ("boe_decision", "GBP", "MPC Official Bank Rate Votes", True),
    ("boj_decision", "JPY", "BOJ Policy Rate", True),
    ("boj_decision", "JPY", "BOJ Press Conference", False),
    ("fomc_decision", "USD", "Federal Funds Rate", True),
])
def test_feed_matches(kind, currency, title, match):
    assert cal.feed_matches(kind, title) is match


def test_merge_keeps_currencies_apart():
    feed = cal.parse_feed([ff("Monetary Policy Statement", "EUR", "2026-12-18T07:00:00-05:00")])  # not the BoJ
    merged = cal.merge_extra(feed, off.official_events(None, utc(2026, 12, 17), utc(2026, 12, 19)))
    assert [e["source"] for e in merged if e["currency"] == "JPY"] == ["boj"]


# ----------------------------------------------------------------------------- persistence


def test_official_persistence_roundtrip(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            assert await off.load_official(rt.db) is None
            state = official_state()
            state["sources"]["bls"]["events"].append({"id": "junk", "time": "mañana"})
            await off.save_official(rt.db, state)
            await off.save_official(rt.db, state)  # upsert
            loaded = await off.load_official(rt.db)
            assert set(loaded["sources"]) == {"bls", "bea"}
            assert loaded["sources"]["bea"] == official_state()["sources"]["bea"]
            assert len(loaded["sources"]["bls"]["events"]) == 5 and loaded["sources"]["bls"]["until"] == utc(
                2026, 12, 30, 15, 0)
            count = await rt.db.fetchval(
                "SELECT COUNT(*) FROM model_state WHERE kind='macro' AND key='calendar_official'")
            assert int(count) == 1
            await rt.db.execute("UPDATE model_state SET state=? WHERE kind='macro' AND key='calendar_official'",
                                ('{"sources": []}',))
            assert await off.load_official(rt.db) is None

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- network (fakes)


def test_fetch_source_success_and_refusals():
    ok = FakeHTTP({"bls.gov": FakeResponse(200, text=BLS_ICS)})
    copy = asyncio.run(off.fetch_source("bls", ok, now_s=NOW_OCT31))
    assert ok.calls == [(off.SOURCES["bls"]["url"], None)] and copy["kinds"][0] == "us_cpi"
    nothing = BLS_ICS.replace("Consumer Price Index", "X").replace("Employment", "X").replace("Producer", "X") \
        .replace("Job Openings", "X")
    for resp, fragment in [(FakeResponse(403, {}), "HTTP 403"), (FakeResponse(200, text=CHALLENGE_HTML), "ICS"),
                           (OSError("down"), "sin conexión"), (FakeResponse(200, text=nothing), "ninguno"),
                           (FakeResponse(200, text="BEGIN:VCALENDAR\nEND:VCALENDAR\n"), "vacío")]:
        with pytest.raises(cal.CalendarError) as err:
            asyncio.run(off.fetch_source("bls", FakeHTTP({"bls.gov": resp}), now_s=NOW_OCT31))
        assert fragment in str(err.value)


def test_fetch_all_reports_each_source():
    http = FakeHTTP({"bls.gov": FakeResponse(403, {}), "bea.gov": FakeResponse(200, text=BEA_ICS)})
    out = asyncio.run(off.fetch_all(http))
    assert isinstance(out["bls"], cal.CalendarError) and str(out["bls"]) == "HTTP 403"
    assert out["bea"]["kinds"] == ["us_gdp", "us_pce"]
    assert [url for url, _ in http.calls] == [off.SOURCES["bls"]["url"], off.SOURCES["bea"]["url"]]
    assert all(url.startswith(("https://www.bls.gov/", "https://www.bea.gov/")) for url, _ in http.calls)


@pytest.mark.network
def test_live_official_calendars_parse():
    out = asyncio.run(off.fetch_all())
    for source, res in out.items():
        assert isinstance(res, dict), f"{source}: {res}"
        assert res["kinds"] and res["until"] is not None
