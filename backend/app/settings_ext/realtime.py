"""Default settings of the realtime feature (merged into DEFAULT_SETTINGS): real-time quotes from the API-key
providers (app.data.realtime). The limits default to the free plans (Twelve Data Basic: 8 credits per minute and
800 per day; Polygon Basic: 5 calls per minute); 0 credits per day = no daily limit (paid plans)."""
from __future__ import annotations

DEFAULTS: dict = {
    "realtime.enabled": True,
    "realtime.twelvedata_credits_min": 8,
    "realtime.twelvedata_credits_day": 800,
    "realtime.polygon_calls_min": 5,
    "realtime.reserve_pct": 25.0,
    "realtime.max_symbols": 4,
    "realtime.min_interval_s": 15,
}
