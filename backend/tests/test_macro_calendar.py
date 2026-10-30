"""Economic calendar: ForexFactory parsing, Spanish titles, official FOMC schedule, merge/dedupe, persistence."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.macro import calendar as cal
from app.macro import fomc
from app.macro.titles import short_title, spanish_title
from tests.test_macro_fakes import FF_SAMPLE, FakeHTTP, FakeResponse

UTC = dt.timezone.utc


def utc(*args: int) -> int:
    return int(dt.datetime(*args, tzinfo=UTC).timestamp())


def test_parse_feed_fields_and_times():
    events = cal.parse_feed(FF_SAMPLE)
    assert len(events) == len(FF_SAMPLE)
    by_title = {e["title"]: e for e in events}
    rate = by_title["Federal Funds Rate"]
    assert rate["time"] == utc(2026, 10, 28, 18, 0)  # 14:00 -04:00
    assert rate["currency"] == "USD" and rate["impact"] == "high" and rate["source"] == "forexfactory"
    assert rate["forecast"] == "4.00%" and rate["previous"] == "4.00%"
    assert by_title["FOMC Statement"]["forecast"] is None and by_title["FOMC Statement"]["previous"] is None
    assert by_title["Bank Holiday"]["impact"] == "holiday"
    assert by_title["Crude Oil Inventories"]["impact"] == "low"
    assert by_title["CB Consumer Confidence"]["impact"] == "medium"
    assert [e["time"] for e in events] == sorted(e["time"] for e in events)
    assert len({e["id"] for e in events}) == len(events)
    assert cal.parse_feed(FF_SAMPLE)[0]["id"] == events[0]["id"]  # stable ids
    assert set(events[0]) == set(cal.EVENT_FIELDS)


def test_parse_feed_tolerates_junk_and_rejects_bad_shape():
    junk = [None, {"title": "", "date": "2026-10-28T10:00:00-04:00"}, {"title": "X", "date": "mañana"},
            {"title": "CPI m/m", "country": "usd", "date": "2026-10-28T08:30:00Z", "impact": "weird"}]
    events = cal.parse_feed(junk + FF_SAMPLE[:1] + FF_SAMPLE[:1])
    assert len(events) == 2  # junk dropped, duplicate removed
    cpi = next(e for e in events if e["title"] == "CPI m/m")
    assert cpi["currency"] == "USD" and cpi["impact"] == "low" and cpi["time"] == utc(2026, 10, 28, 8, 30)
    with pytest.raises(cal.CalendarError):
        cal.parse_feed({"error": "denied"})
    assert cal.parse_time("2026-10-28T14:00:00") == utc(2026, 10, 28, 18, 0)  # naive = New York


@pytest.mark.parametrize("title,currency,expected", [
    ("CPI m/m", "USD", "IPC de EE. UU. (mensual)"),
    ("Core CPI m/m", "USD", "IPC subyacente de EE. UU. (mensual)"),
    ("CPI y/y", "AUD", "IPC de Australia (anual)"),
    ("Non-Farm Employment Change", "USD", "Nóminas no agrícolas de EE. UU. (NFP)"),
    ("Unemployment Claims", "USD", "Solicitudes de subsidio por desempleo de EE. UU."),
    ("Federal Funds Rate", "USD", "Decisión de tipos de la Fed"),
    ("FOMC Meeting Minutes", "USD", "Actas de la Fed (FOMC)"),
    ("Main Refinancing Rate", "EUR", "Decisión de tipos del BCE"),
    ("Monetary Policy Statement", "JPY", "Comunicado de política monetaria del Banco de Japón"),
    ("Advance GDP q/q", "USD", "PIB adelantado de EE. UU. (trimestral)"),
    ("Prelim UoM Consumer Sentiment", "USD", "Sentimiento preliminar del consumidor (U. de Michigan)"),
    ("German Prelim CPI m/m", "EUR", "IPC preliminar de Alemania (mensual)"),
    ("Tokyo Core CPI y/y", "JPY", "IPC subyacente de Tokio (anual)"),
    ("Fed Chair Powell Speaks", "USD", "Discurso de Powell (presidente de la Fed)"),
    ("FOMC Member Daly Speaks", "USD", "Discurso de Daly (miembro del FOMC)"),
    ("Bank Holiday", "NZD", "Festivo bancario de Nueva Zelanda"),
    ("Autumn Forecast Statement", "GBP", "Previsiones fiscales de otoño del Reino Unido"),
    ("Non-Manufacturing PMI", "CNY", "PMI no manufacturero de China"),
    ("Treasury Currency Report", "USD", "Treasury Currency Report"),  # unknown: original kept
])
def test_spanish_titles(title, currency, expected):
    assert spanish_title(title, currency) == expected


def test_short_title_and_sep_note():
    assert short_title("IPC de EE. UU. (mensual)") == "IPC de EE. UU."
    assert short_title("Rueda de prensa del BCE") == "Rueda de prensa del BCE"
    assert cal.with_sep_note("Comunicado de la Fed (FOMC)") == "Comunicado de la Fed (FOMC, con proyecciones económicas)"
    assert cal.with_sep_note("Decisión de tipos de la Fed") == "Decisión de tipos de la Fed (con proyecciones económicas)"


def test_fomc_schedule_and_timezone():
    events = fomc.fomc_events()
    assert len(events) == 2 * len(fomc.MEETINGS) == 32
    dec = next(e for e in events if e["id"] == "fomc-2026-12-09-decision")
    assert dec["time"] == utc(2026, 12, 9, 19, 0)  # 14:00 New York (EST) = 19:00 UTC
    assert "con proyecciones económicas" in dec["title_es"] and dec["impact"] == "high"
    press = next(e for e in events if e["id"] == "fomc-2026-12-09-press")
    assert press["time"] == utc(2026, 12, 9, 19, 30)
    summer = next(e for e in events if e["id"] == "fomc-2026-06-17-decision")
    assert summer["time"] == utc(2026, 6, 17, 18, 0)  # EDT
    plain = next(e for e in events if e["id"] == "fomc-2026-10-28-decision")
    assert "proyecciones" not in plain["title_es"]
    sep_days = {m[1] for m in fomc.MEETINGS if m[2]}
    assert sep_days == {"2026-03-18", "2026-06-17", "2026-09-16", "2026-12-09",
                        "2027-03-17", "2027-06-09", "2027-09-15", "2027-12-08"}
    assert fomc.next_meeting(utc(2026, 10, 30, 12))["id"] == "fomc-2026-12-09-decision"
    assert fomc.meeting_for(utc(2026, 10, 28, 18)) == ("2026-10-27", "2026-10-28", False)
    assert fomc.fomc_events(utc(2027, 12, 1), None)[0]["id"] == "fomc-2027-12-08-decision"


def test_merge_dedupes_feed_fomc_and_adds_future_meetings():
    feed = cal.parse_feed(FF_SAMPLE)
    merged = cal.merge_fomc(feed, utc(2026, 10, 20), utc(2026, 12, 31))
    october = [e for e in merged if e["currency"] == "USD" and utc(2026, 10, 28) <= e["time"] < utc(2026, 10, 29)
               and ("FOMC" in e["title"] or e["title"] == "Federal Funds Rate")]
    assert len(october) == 3 and all(e["source"] == "forexfactory" for e in october)  # no official duplicates
    assert not any(e["id"].startswith("fomc-2026-10-28") for e in merged)
    assert any(e["id"] == "fomc-2026-12-09-decision" for e in merged)
    assert len(merged) == len(feed) + 2


def test_merge_marks_sep_meetings_on_feed_copies():
    feed = cal.parse_feed([
        {"title": "Federal Funds Rate", "country": "USD", "date": "2026-12-09T14:00:00-05:00", "impact": "High"},
        {"title": "FOMC Press Conference", "country": "USD", "date": "2026-12-09T14:30:00-05:00", "impact": "High"},
    ])
    merged = cal.merge_fomc(feed, utc(2026, 12, 1), utc(2026, 12, 31))
    assert len(merged) == 2
    assert merged[0]["title_es"] == "Decisión de tipos de la Fed (con proyecciones económicas)"
    assert "proyecciones" in merged[1]["title_es"]


def test_week_bounds_and_staleness():
    events = cal.parse_feed(FF_SAMPLE)
    start, end = cal.week_bounds(events)
    assert start == utc(2026, 10, 25, 4, 0)  # Sunday 00:00 New York (EDT)
    assert end == utc(2026, 11, 1, 4, 0)
    cache = cal.make_cache(events, fetched_at_ms=1)
    assert not cal.is_stale(cache, utc(2026, 10, 31, 12))
    assert cal.is_stale(cache, utc(2026, 11, 1, 5))
    assert cal.is_stale(None, utc(2026, 10, 28)) and cal.is_stale({"events": []}, utc(2026, 10, 28))


def test_select_and_public_event():
    events = cal.merge_fomc(cal.parse_feed(FF_SAMPLE), utc(2026, 10, 1), utc(2026, 12, 31))
    now = utc(2026, 10, 29, 12, 0)
    picked = cal.select(events, start_s=now, end_s=now + 86400, impacts=("high",), currencies={"USD", "EUR"})
    assert [e["title"] for e in picked] == ["Advance GDP q/q", "Core PCE Price Index m/m", "Main Refinancing Rate",
                                           "ECB Press Conference"]
    pub = cal.public_event(picked[0], now)
    assert set(pub) == set(cal.EVENT_FIELDS) | {"countdown_s"}
    assert pub["countdown_s"] == picked[0]["time"] - now == 30 * 60


def test_fetch_feed_success_and_errors():
    ok = FakeHTTP({"ff_calendar": FakeResponse(200, FF_SAMPLE)})
    events = asyncio.run(cal.fetch_feed(ok))
    assert len(events) == len(FF_SAMPLE) and ok.calls[0][0] == cal.FF_URL
    for resp, fragment in [(FakeResponse(429, {}), "HTTP 429"), (FakeResponse(200, text="<html>"), "JSON"),
                           (FakeResponse(200, []), "vacío"), (OSError("down"), "sin conexión")]:
        with pytest.raises(cal.CalendarError) as err:
            asyncio.run(cal.fetch_feed(FakeHTTP({"ff_calendar": resp})))
        assert fragment in str(err.value)


def test_calendar_persistence_roundtrip(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            assert await cal.load_cached(rt.db) is None
            cache = cal.make_cache(cal.parse_feed(FF_SAMPLE), fetched_at_ms=1_793_300_000_000, refreshes=3)
            await cal.save_cached(rt.db, cache)
            await cal.save_cached(rt.db, cache)  # upsert
            loaded = await cal.load_cached(rt.db)
            assert loaded["events"] == cache["events"]
            assert loaded["week_end"] == cache["week_end"] and loaded["refreshes"] == 3
            assert loaded["fetched_at"] == 1_793_300_000_000
            count = await rt.db.fetchval("SELECT COUNT(*) FROM model_state WHERE kind='macro' AND key='calendar'")
            assert int(count) == 1
            await rt.db.execute("UPDATE model_state SET state=? WHERE kind='macro' AND key='calendar'", ("{oops",))
            assert await cal.load_cached(rt.db) is None

    asyncio.run(scenario())


@pytest.mark.network
def test_live_feed_parses():
    events = asyncio.run(cal.fetch_feed())
    assert events and all(e["impact"] in cal.IMPACT_RANK for e in events)
