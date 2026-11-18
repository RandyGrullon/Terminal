"""Market hours of what NEXUS automática trades: for its New York session mode and for the page.

- New York stock exchange (NYSE / Nasdaq core session): Monday to Friday 9:30–16:00 America/New_York, closed on the NYSE
  holidays and closing at 13:00 on its early-close days. The lists come from the official calendar
  (nyse.com/markets/hours-calendars, checked 2026-11-17) and run through December 2027: from 2028 only weekends are
  skipped until the lists are updated (`known` false), so update them every year with the dates on that page.
- CME equity index futures (Globex): Sunday 18:00 to Friday 17:00 New York time, with a daily pause 17:00–18:00. Their
  holidays are not included.
- Crypto and the gold perpetual (Binance…): 24 hours, 7 days.

Times are unix seconds; New York's daylight saving time comes from zoneinfo.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
NY_OPEN = time(9, 30)
NY_CLOSE = time(16, 0)
NYSE_HOLIDAYS = frozenset({
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
    date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
})
NYSE_EARLY_CLOSE = {date(2026, 11, 27): time(13, 0), date(2026, 12, 24): time(13, 0), date(2027, 11, 26): time(13, 0)}
NYSE_KNOWN_UNTIL = date(2027, 12, 31)
CME_PAUSE = (17, 18)  # daily pause of CME Globex, hours of New York (also its Friday close and Sunday open)


def _ts(d: date, t: time) -> int:
    return int(datetime.combine(d, t, tzinfo=NEW_YORK).timestamp())


def ny_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def ny_session(now_s: int) -> dict[str, Any]:
    """The New York stock session now: {open, opens_at, closes_at, early_close, known}. Open: the times of this
    session. Closed: those of the next one. `known` false when that day is past the holiday lists."""
    now = datetime.fromtimestamp(int(now_s), NEW_YORK)
    for i in range(15):  # the next trading day is never more than a few days away
        d = now.date() + timedelta(days=i)
        if not ny_trading_day(d):
            continue
        close_t = NYSE_EARLY_CLOSE.get(d, NY_CLOSE)
        opens_at, closes_at = _ts(d, NY_OPEN), _ts(d, close_t)
        if int(now_s) >= closes_at:
            continue
        return {"open": int(now_s) >= opens_at, "opens_at": opens_at, "closes_at": closes_at,
                "early_close": d in NYSE_EARLY_CLOSE, "known": d <= NYSE_KNOWN_UNTIL}
    raise RuntimeError("no New York trading day in the next 15 days")  # unreachable with the lists above


def cme_session(now_s: int) -> dict[str, Any]:
    """CME equity index futures now: {open, opens_at, closes_at} (opens_at None while open; closes_at None while
    closed). Weekly schedule only (no holidays)."""
    t = datetime.fromtimestamp(int(now_s), NEW_YORK)
    wd, minutes = t.weekday(), t.hour * 60 + t.minute  # Monday 0 … Sunday 6
    pause_from, pause_to = CME_PAUSE[0] * 60, CME_PAUSE[1] * 60

    def at(days: int, hour: int) -> int:
        return int((t.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)).timestamp())

    is_open = ((wd == 6 and minutes >= pause_to) or (wd <= 3 and not pause_from <= minutes < pause_to)
               or (wd == 4 and minutes < pause_from))
    if is_open:
        same_day = wd != 6 and minutes < pause_from
        return {"open": True, "opens_at": None, "closes_at": at(0 if same_day else 1, CME_PAUSE[0])}
    if wd <= 3:  # the daily pause
        return {"open": False, "opens_at": at(0, CME_PAUSE[1]), "closes_at": None}
    return {"open": False, "opens_at": at(6 - wd, CME_PAUSE[1]), "closes_at": None}  # weekend: Sunday 18:00


def markets_view(now_s: int) -> list[dict[str, Any]]:
    """AutopilotMarket[]: the markets NEXUS automática may trade, open or closed now and their next change."""
    ny = ny_session(now_s)
    cme = cme_session(now_s)
    ny_note = "9:30–16:00 de Nueva York, días hábiles; festivos de la NYSE"
    if ny["early_close"]:
        ny_note += "; hoy cierra a las 13:00" if ny["open"] else "; su próxima sesión cierra a las 13:00"
    if not ny["known"]:
        ny_note += "; festivos sin actualizar para este año"
    return [
        {"key": "crypto", "label": "Cripto y oro (perpetuos)", "open": True, "opens_at": None, "closes_at": None,
         "note": "24 horas, 7 días"},
        {"key": "ny", "label": "Bolsa de Nueva York (acciones y fondos)", "open": ny["open"],
         "opens_at": None if ny["open"] else ny["opens_at"], "closes_at": ny["closes_at"] if ny["open"] else None,
         "note": ny_note},
        {"key": "cme", "label": "Futuros de índices (CME)", "open": cme["open"], "opens_at": cme["opens_at"],
         "closes_at": cme["closes_at"],
         "note": "Domingo 18:00 a viernes 17:00 de Nueva York, pausa diaria 17:00–18:00; sin festivos"},
    ]
