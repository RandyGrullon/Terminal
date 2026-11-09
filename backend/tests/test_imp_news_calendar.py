"""Noticias para PyChart (1/2): fechas oficiales pasadas (app.macro.history) y el calendario que MacroService entrega a
cada ejecución (script_events). Sin red: las fechas son las verificadas en las páginas oficiales."""
from __future__ import annotations

import asyncio
import datetime as dt

from app.macro import calendar as cal
from app.macro import fomc, history, schedules
from app.macro.fomc import NEW_YORK
from tests.test_macro_service import make_service


def _days(dates: tuple[str, ...]) -> list[dt.date]:
    return [dt.date.fromisoformat(d) for d in dates]


def _ny(ts: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, NEW_YORK)


def test_history_dates_are_ordered_weekdays_and_unique():
    for dates in (history.BLS_CPI, history.BLS_EMPLOYMENT):
        days = _days(dates)
        assert days == sorted(days) and len(set(days)) == len(days)
        assert all(d.weekday() < 5 for d in days)
        assert days[0] >= dt.date.fromisoformat(history.FROM_DAY) and days[-1] <= dt.date.fromisoformat(history.UNTIL_DAY)
    assert len(history.BLS_CPI) == 32 and len(history.BLS_EMPLOYMENT) == 32  # Oct 2025 was not published on its own
    for first, decision, _sep in history.FOMC_MEETINGS:
        assert dt.date.fromisoformat(decision) - dt.date.fromisoformat(first) == dt.timedelta(days=1)
    assert len(history.FOMC_MEETINGS) == 16


def test_history_includes_the_releases_moved_by_the_2025_lapse():
    assert "2025-10-24" in history.BLS_CPI and "2025-12-18" in history.BLS_CPI
    assert "2025-11-20" in history.BLS_EMPLOYMENT and "2025-12-16" in history.BLS_EMPLOYMENT
    # nothing between them: the October 2025 reports were not published on their own
    assert not [d for d in history.BLS_CPI if "2025-10-24" < d < "2025-12-18"]
    assert not [d for d in history.BLS_EMPLOYMENT if "2025-09-05" < d < "2025-11-20"]


def test_history_is_continuous_with_the_forward_schedules():
    """No overlap and no hole: the gap between consecutive releases is ~1 month except the 2025 lapse."""
    for past, forward in ((history.BLS_CPI, schedules.BLS_CPI), (history.BLS_EMPLOYMENT, schedules.BLS_EMPLOYMENT)):
        assert past[-1] < forward[0]
        days = _days(past + forward)
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        assert max(gaps) <= 80 and sum(1 for g in gaps if g > 45) == 1, gaps
    assert history.FOMC_MEETINGS[-1][1] < fomc.MEETINGS[0][1]
    assert not {m[1] for m in history.FOMC_MEETINGS} & {m[1] for m in fomc.MEETINGS}


def test_history_events_have_official_times_and_kinds():
    events = history.history_events()
    assert len(events) == 32 + 32 + 2 * 16
    for ev in events:
        local = _ny(ev["time"])
        if ev["kind"] in ("us_cpi", "us_nfp"):
            assert (local.hour, local.minute) == (8, 30) and ev["source"] == "bls"
        else:
            assert ev["kind"] in ("fomc_decision", "fomc_press")
            assert (local.hour, local.minute) == ((14, 0) if ev["kind"] == "fomc_decision" else (14, 30))
        assert ev["kind"] in history.PRINCIPAL_KINDS and ev["impact"] == "high" and ev["currency"] == "USD"
    window = history.history_events(fomc.ny_time("2025-01-01", 0, 0), fomc.ny_time("2025-01-31", 23, 59))
    assert [(e["kind"], _ny(e["time"]).date().isoformat()) for e in window] == [
        ("us_nfp", "2025-01-10"), ("us_cpi", "2025-01-15"), ("fomc_decision", "2025-01-29"), ("fomc_press", "2025-01-29")]


def test_coverage_bounds():
    assert _ny(history.from_s()).date().isoformat() == history.FROM_DAY
    assert history.until_s() > fomc.ny_time("2027-11-01", 0, 0)
    assert history.from_s() < history.until_s()


def test_script_events_merge_history_official_and_feed(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc, _calls = make_service(rt, fomc.ny_time("2026-11-09", 9, 0), feed=RuntimeError("sin red"))
            cpi = fomc.ny_time("2026-11-10", 8, 30)
            svc.cache = cal.make_cache([
                {"id": "ff-cpi", "title": "CPI m/m", "title_es": "IPC m/m", "currency": "USD", "time": cpi,
                 "impact": "high", "forecast": None, "previous": None, "source": cal.SOURCE},
                {"id": "ff-zew", "title": "German ZEW Economic Sentiment", "title_es": None, "currency": "EUR",
                 "time": cpi - 4 * 3600, "impact": "medium", "forecast": None, "previous": None, "source": cal.SOURCE},
            ])
            out = svc.script_events(fomc.ny_time("2025-10-20", 0, 0), fomc.ny_time("2026-11-12", 0, 0))
            assert out["from"] == history.from_s() and out["until"] == history.until_s()
            events = out["events"]
            assert all(set(e) == {"time", "title", "currency", "impact", "principal"} for e in events)
            by_time = {}
            for e in events:
                by_time.setdefault(e["time"], []).append(e)
            # past release moved by the 2025 lapse, from history.py
            moved = by_time[fomc.ny_time("2025-10-24", 8, 30)]
            assert [e["principal"] for e in moved] == [True]
            # the weekly feed copy of the CPI stands in for the curated date (deduplicated) and is principal
            assert [(e["title"], e["principal"]) for e in by_time[cpi]] == [("IPC m/m", True)]
            zew = by_time[cpi - 4 * 3600]
            assert zew[0]["currency"] == "EUR" and zew[0]["principal"] is False
            # a non-principal official event (PPI, schedules.py) is delivered but not principal
            assert not any(e["principal"] for e in events if "productor" in e["title"])
            times = [e["time"] for e in events]
            assert times == sorted(times)

    asyncio.run(scenario())
