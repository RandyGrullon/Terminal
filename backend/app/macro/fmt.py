"""Spanish formatting for the semáforo: numbers like the UI (es-ES: '22,65', '28.293,5'), signed changes
('+29,7 %', '-0,8 pb'), countdowns ('24 min', '1 h 30 min') and wall-clock times ('9:00', 'jue 8:30').

Times are shown in the user's zone: setting `ui.timezone` (IANA name) when present, else the server's local zone
(the server runs on the user's machine in local mode)."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any
from zoneinfo import ZoneInfo

TZ_SETTING = "ui.timezone"
WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
DASH = "—"


def num(x: Any, decimals: int = 2) -> str:
    """es-ES number: decimal comma, '.' thousands separator only from 5 integer digits (like toLocaleString)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return DASH
    if not math.isfinite(v):
        return DASH
    r = round(v, decimals)
    text = f"{abs(r):,.{decimals}f}"
    int_part, _, frac = text.partition(".")
    digits = int_part.replace(",", "")
    int_part = int_part.replace(",", ".") if len(digits) >= 5 else digits
    out = f"{int_part},{frac}" if frac else int_part
    return f"-{out}" if r < 0 else out


def signed(x: Any, decimals: int = 1, suffix: str = "") -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(v):
        return ""
    r = round(v, decimals)
    sign = "+" if r > 0 else ""
    return f"{sign}{num(r, decimals)}{suffix}"


def pct(x: Any, decimals: int = 1) -> str:
    """'+29,7 %' (Spanish style: space before %)."""
    return signed(x, decimals, " %")


def bp(x: Any) -> str:
    """Basis points: '-0,8 pb', '+12 pb'."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return signed(v, 1 if abs(v) < 10 else 0, " pb")


def duration(seconds: Any, ceil: bool = False) -> str:
    """'menos de 1 min', '24 min', '1 h 30 min', '2 h', '3 d 4 h'."""
    try:
        s = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return DASH
    minutes = int(math.ceil(s / 60.0)) if ceil else int(s // 60)
    if minutes < 1:
        return "menos de 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {mins} min" if mins else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def until(seconds: Any) -> str:
    """Countdown phrase: 'en 24 min' / 'ahora' / 'hace 10 min'."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if -60 < s <= 30:
        return "ahora"
    if s > 0:
        return f"en {duration(s, ceil=True)}"
    return f"hace {duration(-s)}"


def zone(tz_name: Any = None) -> dt.tzinfo | None:
    if not tz_name:
        return None
    try:
        return ZoneInfo(str(tz_name))
    except Exception:
        return None


def to_local(ts: int | float, tz_name: Any = None) -> dt.datetime:
    tz = zone(tz_name)
    if tz is not None:
        return dt.datetime.fromtimestamp(ts, tz)
    return dt.datetime.fromtimestamp(ts).astimezone()


def clock(ts: int | float, tz_name: Any = None) -> str:
    """'9:00' (24 h, no leading zero on the hour)."""
    d = to_local(ts, tz_name)
    return f"{d.hour}:{d.minute:02d}"


def day_clock(ts: int | float, now_s: int | float, tz_name: Any = None) -> str:
    """'hoy 8:30', 'mañana 14:00', 'jue 8:30', 'jue 05/11 8:30' (more than 6 days away), 'ayer 14:00'."""
    d = to_local(ts, tz_name)
    today = to_local(now_s, tz_name).date()
    delta = (d.date() - today).days
    hhmm = f"{d.hour}:{d.minute:02d}"
    if delta == 0:
        return f"hoy {hhmm}"
    if delta == 1:
        return f"mañana {hhmm}"
    if delta == -1:
        return f"ayer {hhmm}"
    wd = WEEKDAYS[d.weekday()]
    if 1 < delta <= 6:
        return f"{wd} {hhmm}"
    return f"{wd} {d.day:02d}/{d.month:02d} {hhmm}"


def date_label(ts: int | float, tz_name: Any = None) -> str:
    """'sáb 31/10'."""
    d = to_local(ts, tz_name)
    return f"{WEEKDAYS[d.weekday()]} {d.day:02d}/{d.month:02d}"


def tz_label(tz_name: Any = None) -> str:
    """Short zone label for table headers ('hora local' or the IANA name)."""
    return str(tz_name) if zone(tz_name) is not None else "hora local"
