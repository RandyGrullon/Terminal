"""Real-time quotes from the API-key providers for stocks / forex (`hub.realtime`, used only with a key in Ajustes).

What each provider serves in real time depends on the user's plan (public documentation, checked oct. 2026):
- Twelve Data, plan Basic (free): real-time US equities and ETFs, forex and crypto; 8 API credits per minute and 800
  per day, 1 credit per symbol for /quote (also inside a comma-separated batch). Other markets only on paid plans
  (Grow: end-of-day global equities). https://twelvedata.com/pricing - https://twelvedata.com/docs
- Polygon (now Massive): stock snapshots are not in Basic (free: end-of-day bars, 5 calls per minute), 15-minute
  delayed on Starter / Developer and real time on Advanced / Business; currency snapshots are real time from
  Currencies Starter. https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot
- Alpha Vantage, free: 25 requests per day and quotes updated at the end of each trading day; real-time or 15-minute
  delayed US data needs a premium entitlement. Never used for live quotes.
  https://www.alphavantage.co/documentation/ - https://www.alphavantage.co/premium/

Honesty: the plan is not known, so every quote is also judged by the provider's own timestamp: `realtime` is True only
while the market is open and the price is at most REALTIME_MAX_AGE_S old; an older price carries the measured
`delay_s`, and a quote from a closed market carries `market_open: False` (its last price, no real-time claim).

Cadence: only symbols on screen (WebSocket chart subscriptions) or with open demo positions are polled, at most
`realtime.max_symbols`, never more often than `realtime.min_interval_s`, inside the per-minute and per-day limits of
the plan (settings realtime.*), keeping `realtime.reserve_pct` of them for candle downloads and spreading the
daily credits over the market hours left today. Closed markets are re-checked every CLOSED_POLL_S.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import time
from collections import deque
from typing import Any

from app import config
from app.data import symbols as S
from app.data.providers_http import DataProviderError, HttpResult, request_json
from app.settings_ext.realtime import DEFAULTS
from app.util import TTLCache, clamp, now_ms, parse_symbol, safe_float

REALTIME_PROVIDERS: tuple[str, ...] = ("twelvedata", "polygon")  # Alpha Vantage: 25 requests a day, end of day
LABELS = {"twelvedata": "Twelve Data", "polygon": "Polygon", "alphavantage": "Alpha Vantage"}
KEY_SETTINGS = {"twelvedata": "providers.twelvedata_key", "polygon": "providers.polygon_key",
                "alphavantage": "providers.alphavantage_key"}
PLAN_NOTES: dict[str, str] = {
    "twelvedata": "Plan Basic (gratis): tiempo real para acciones y ETF de EE. UU., forex y cripto; 8 créditos por "
                  "minuto y 800 al día (1 crédito por símbolo y consulta). Otros mercados, solo con planes de pago.",
    "polygon": "Plan Basic (gratis): sin cotizaciones en vivo (solo velas de fin de día, 5 peticiones por minuto). "
               "Acciones: 15 min de retraso en Starter y Developer, tiempo real en Advanced. Divisas: tiempo real "
               "desde Currencies Starter.",
    "alphavantage": "Plan gratuito: 25 peticiones al día y cotizaciones de fin de día; el tiempo real (o 15 min de "
                    "retraso) de EE. UU. es de pago. No se usa para precios en vivo.",
}
DOC_URLS = {"twelvedata": "https://twelvedata.com/pricing",
            "polygon": "https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot",
            "alphavantage": "https://www.alphavantage.co/premium/"}
TD_QUOTE_URL = "https://api.twelvedata.com/quote"
PG_STOCKS_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
PG_FOREX_URL = "https://api.polygon.io/v2/snapshot/locale/global/markets/forex/tickers"

REALTIME_MAX_AGE_S = 180  # a price older than this while the market is open is not called real time
CLOSED_POLL_S = 1800.0  # a closed market is re-checked this often
QUOTE_FRESH_FACTOR = 3.0  # a cached quote serves as live for this many poll intervals (at least FRESH_MIN_S)
FRESH_MIN_S = 120.0
ERROR_BACKOFF_S = 60.0  # network / server error of a provider: next try after this
AUTH_BACKOFF_S = 600.0  # rejected key
PLAN_BLOCK_S = 6 * 3600.0  # plan without live quotes for a market / symbol: checked again after this
DELAYED_BLOCK_S = 3600.0  # a symbol whose keyed price came 10+ min old while the market was open
DAY_FLOOR_CREDITS = 20  # never used for quotes: left for the candle downloads of the day
RATE_LIMIT_LONG_S = 900.0  # pause after a second "limit reached" answer in a row
PRIORITY_OWNERS = ("paper", "ws:")  # subscriptions worth real-time credits: open demo positions, charts on screen


# ----------------------------------------------------------------------------- helpers


def num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def to_ms(value: Any) -> int | None:
    """Unix time in s, ms, µs or ns -> ms (None when missing or absurd)."""
    v = num(value)
    if v is None or v <= 0:
        return None
    if v > 1e17:
        v /= 1e6
    elif v > 1e14:
        v /= 1e3
    elif v < 1e11:
        v *= 1000.0
    return int(v)


def flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return True if text == "true" else False if text == "false" else None


def _ny(ts: float) -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York"))
    except Exception:  # no tz database: fixed EST/EDT approximation of keyed._us_eastern_offset
        from app.data.keyed import _us_eastern_offset

        naive = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).replace(tzinfo=None)
        return (naive + dt.timedelta(seconds=_us_eastern_offset(naive))).replace(tzinfo=dt.timezone.utc)


def market_group(symbol: str, asset_class: str) -> str:
    """stocks (US equities / funds / indices) | forex (currencies and metals) | other."""
    exchange, _ticker = parse_symbol(symbol)
    if asset_class in ("forex", "commodity", "cfd") and S.fx_pair(_ticker):
        return "forex"
    if asset_class in ("stock", "fund", "index") and (exchange in S.US_STOCK_EXCHANGES or asset_class == "index"):
        return "stocks"
    return "other"


def session_open(group: str, ts: float) -> bool | None:
    """Regular session guess (holidays ignored): US stocks 9:30-16:00 New York on weekdays; forex from Sunday 17:00
    to Friday 17:00 New York. None for other markets."""
    local = _ny(ts)
    if group == "stocks":
        if local.weekday() >= 5:
            return False
        minutes = local.hour * 60 + local.minute
        return 9 * 60 + 30 <= minutes < 16 * 60
    if group == "forex":
        wd, hour = local.weekday(), local.hour
        if wd == 5 or (wd == 4 and hour >= 17) or (wd == 6 and hour < 17):
            return False
        return True
    return None


def next_open_in(group: str, ts: float, horizon: float = CLOSED_POLL_S) -> float | None:
    """Seconds until the regular session (guess) opens, when it opens within `horizon`; None otherwise."""
    t = (math.floor(ts / 60.0) + 1) * 60.0
    while t <= ts + horizon:
        if session_open(group, t) is True:
            return t - ts
        t += 60.0
    return None


def open_seconds_left(group: str, ts: float) -> float:
    """Seconds of today's (UTC) market hours still ahead (where the daily credits are worth spending)."""
    day_end = (math.floor(ts / 86400.0) + 1) * 86400.0
    step, left, t = 300.0, 0.0, float(ts)
    while t < day_end:
        if session_open(group, t) is not False:
            left += min(step, day_end - t)
        t += step
    return left


# ----------------------------------------------------------------------------- credit budget


class CreditBudget:
    """API credits spent with one provider: sliding last minute and the current UTC day (quotes and candles; the
    daily counters restart at 00:00 UTC). `clock` = time.time (wall clock, tests may replace it)."""

    def __init__(self, clock: Any = time.time):
        self.clock = clock
        self._minute: deque[tuple[float, int]] = deque()
        self._day = (-1, 0, 0)  # (UTC day number, total credits, quote credits)
        self.blocked_until = 0.0  # the provider said "limit reached": nothing until then

    def _roll(self, now: float) -> None:
        while self._minute and now - self._minute[0][0] >= 60.0:
            self._minute.popleft()
        day = int(now // 86400)
        if self._day[0] != day:
            self._day = (day, 0, 0)

    def spend(self, credits: int, quotes: bool = False) -> None:
        now = float(self.clock())
        self._roll(now)
        n = max(0, int(credits))
        self._minute.append((now, n))
        day, total, quote = self._day
        self._day = (day, total + n, quote + (n if quotes else 0))

    def used_minute(self) -> int:
        self._roll(float(self.clock()))
        return sum(n for _, n in self._minute)

    def used_day(self, quotes_only: bool = False) -> int:
        self._roll(float(self.clock()))
        return self._day[2] if quotes_only else self._day[1]

    def block(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, float(self.clock()) + max(0.0, float(seconds)))

    def block_day(self) -> None:
        now = float(self.clock())
        self.blocked_until = max(self.blocked_until, (math.floor(now / 86400.0) + 1) * 86400.0 + 5.0)

    def blocked(self) -> bool:
        return float(self.clock()) < self.blocked_until


# ----------------------------------------------------------------------------- parsers


def td_ident(params: dict) -> str:
    """Twelve Data batch identifier ("AAPL", "EUR/USD", "VOD:LSE")."""
    sym = str(params.get("symbol") or "")
    return f"{sym}:{params['exchange']}" if params.get("exchange") else sym


def td_error(data: dict) -> tuple[str, str] | None:
    """(kind, Spanish detail) of a Twelve Data error object; None when it is a quote."""
    if str(data.get("status", "ok")).lower() != "error" and "code" not in data:
        return None
    try:
        code = int(data.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    low = " ".join(str(data.get("message") or "").split()).lower()
    if code == 401 or "apikey" in low or "api key" in low:
        return "auth", "clave API inválida o no autorizada"
    if code == 429 or "run out of api credits" in low or "credits" in low:
        if "for the day" in low or "daily" in low:
            return "day", "créditos del día agotados (se renuevan a las 00:00 UTC)"
        return "rate_limit", "créditos del minuto agotados"
    if code == 403 or "upgrade" in low or "plan" in low or "grow" in low or "pro " in low:
        return "premium", "tu plan no incluye este mercado en tiempo real"
    if code in (400, 404) or "not found" in low or "invalid" in low:
        return "not_found", "símbolo no disponible"
    return "error", f"error {code or '?'}"


def td_quote(symbol: str, d: dict, now: float) -> dict | None:
    """Quote (types.ts + source / realtime / delay_s / market_open) from a Twelve Data /quote object."""
    price = num(d.get("close"))
    if price is None or price <= 0:
        return None
    at = to_ms(d.get("last_quote_at")) or to_ms(d.get("timestamp"))
    return judged({
        "symbol": symbol, "price": price, "change_pct": num(d.get("percent_change")), "change": num(d.get("change")),
        "volume": num(d.get("volume")), "high": num(d.get("high")), "low": num(d.get("low")),
        "open": num(d.get("open")), "ts": at or int(now * 1000), "source": "twelvedata",
        "market_open": flag(d.get("is_market_open")),
    }, now)


def pg_quote(symbol: str, t: dict, now: float, group: str) -> dict | None:
    """Quote from a Polygon snapshot ticker (stocks: last trade; currencies: bid/ask midpoint)."""
    day = t.get("day") if isinstance(t.get("day"), dict) else {}
    last_q = t.get("lastQuote") if isinstance(t.get("lastQuote"), dict) else {}
    last_t = t.get("lastTrade") if isinstance(t.get("lastTrade"), dict) else {}
    minute = t.get("min") if isinstance(t.get("min"), dict) else {}
    bid, ask = num(last_q.get("b")), num(last_q.get("a"))
    price = num(last_t.get("p"))
    if price is None and bid and ask:
        price = (bid + ask) / 2.0
    price = price if price is not None else num(minute.get("c")) or num(day.get("c"))
    if price is None or price <= 0:
        return None
    at = to_ms(last_t.get("t")) or to_ms(last_q.get("t")) or to_ms(t.get("updated")) or to_ms(minute.get("t"))
    return judged({
        "symbol": symbol, "price": price, "change_pct": num(t.get("todaysChangePerc")),
        "change": num(t.get("todaysChange")), "volume": num(day.get("v")), "high": num(day.get("h")),
        "low": num(day.get("l")), "open": num(day.get("o")), "ts": at or int(now * 1000), "source": "polygon",
        "market_open": session_open(group, now),
    }, now)


def judged(q: dict, now: float) -> dict:
    """Freshness from the provider's own timestamp: real time only while the market is open and the price is at
    most REALTIME_MAX_AGE_S old; otherwise the measured delay (closed markets carry no claim)."""
    age = max(0.0, now - q["ts"] / 1000.0)
    if q.get("market_open") is False:
        q.pop("realtime", None)
        return q
    if age <= REALTIME_MAX_AGE_S:
        q["realtime"] = True
    else:
        q["realtime"] = False
        q["delay_s"] = int(round(age))
    return q


def pg_error(result: HttpResult) -> tuple[str, str] | None:
    data = result.data if isinstance(result.data, dict) else {}
    status = str(data.get("status") or "").upper()
    msg = " ".join(str(data.get("error") or data.get("message") or "").split()).lower()
    if result.status == 401 or "api key" in msg:
        return "auth", "clave API inválida"
    if result.status == 403 or status == "NOT_AUTHORIZED":
        return "premium", "tu plan no incluye cotizaciones en vivo"
    if result.status == 429 or "exceeded" in msg or "maximum requests" in msg:
        return "rate_limit", "límite de peticiones por minuto alcanzado"
    if result.status >= 400 or status == "ERROR":
        return "error", f"HTTP {result.status}"
    return None


# ----------------------------------------------------------------------------- service


class PollFailure(Exception):
    """The whole request failed (key rejected, limit reached, provider error): `kind` as in _fail."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class RealtimeQuotes:
    """Real-time quote poller owned by the MarketDataHub (`hub.realtime`). No I/O until `poll()`; `clock` is the
    wall clock (tests replace it)."""

    def __init__(self, hub: Any, clock: Any = time.time):
        self.hub = hub
        self.clock = clock
        self._budgets: dict[str, CreditBudget] = {}
        self._quotes: dict[str, dict] = {}  # symbol -> newest keyed quote (+ private _at / _interval)
        self._next: dict[str, float] = {}  # symbol -> wall time of its next poll
        self._blocked = TTLCache(PLAN_BLOCK_S, maxsize=4096)  # (provider, symbol | market group) -> reason
        self._down: dict[str, tuple[float, str]] = {}  # provider -> (until, reason) after auth / server errors
        self._selected: dict[str, str] = {}  # symbol -> provider of the current selection
        self._jobs: dict[str, asyncio.Future] = {}
        self._job_symbols: dict[str, set[str]] = {}  # provider -> symbols of the request scheduled last (due())
        self._limited: dict[str, int] = {}  # provider -> rate-limit answers in a row
        self.polls = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_poll_at: int | None = None

    # ------------------------------------------------------------------ settings

    def _setting(self, key: str) -> Any:
        return self.hub._setting(key, DEFAULTS.get(key))

    def enabled(self) -> bool:
        return bool(self._setting("realtime.enabled")) and not config.OFFLINE

    def key(self, provider: str) -> str:
        return str(self.hub._setting(KEY_SETTINGS.get(provider, ""), "") or "").strip()

    def limits(self, provider: str) -> tuple[int, int]:
        """(credits or calls per minute, credits per day; 0 = no daily limit) of the user's plan."""
        if provider == "twelvedata":
            per_min = safe_float(self._setting("realtime.twelvedata_credits_min"), 8.0) or 8.0
            per_day = safe_float(self._setting("realtime.twelvedata_credits_day"), 800.0)
            return int(clamp(per_min, 1, 100_000)), int(clamp(per_day if per_day is not None else 800, 0, 10**8))
        per_min = safe_float(self._setting("realtime.polygon_calls_min"), 5.0) or 5.0
        return int(clamp(per_min, 1, 100_000)), 0

    def reserve(self) -> float:
        return float(clamp(safe_float(self._setting("realtime.reserve_pct"), 25.0) or 0.0, 0.0, 90.0)) / 100.0

    def budget(self, provider: str) -> CreditBudget:
        b = self._budgets.get(provider)
        if b is None:
            b = self._budgets[provider] = CreditBudget(self.clock)
        return b

    def reset(self) -> None:
        """Keys or limits changed: forget plan blocks, back-offs and cached quotes."""
        self._blocked.clear()
        self._down.clear()
        self._quotes.clear()
        self._next.clear()

    # ------------------------------------------------------------------ selection

    def provider_for(self, symbol: str, asset_class: str) -> str | None:
        """First provider with a key that can quote `symbol` live (US stocks / funds / indices, forex, metals)."""
        group = market_group(symbol, asset_class)
        if group == "other":
            return None
        mappers = {"twelvedata": S.twelvedata_params, "polygon": S.polygon_ticker}
        for prov in REALTIME_PROVIDERS:
            if not self.key(prov) or self._blocked.get((prov, group)) or self._blocked.get((prov, symbol)):
                continue
            try:
                ok = mappers[prov](symbol, asset_class) is not None
            except Exception:
                ok = False
            if ok:
                return prov
        return None

    def select(self) -> dict[str, str]:
        """Symbols worth real-time credits (open demo positions first, then charts on screen), at most
        realtime.max_symbols: {symbol: provider}."""
        if not self.enabled():
            self._selected = {}
            return {}
        hub = self.hub
        rank: dict[str, int] = {}
        for (symbol, _tf), owners in list(hub._subs.items()):
            for i, prefix in enumerate(PRIORITY_OWNERS):
                if any(str(o).startswith(prefix) for o in owners):
                    rank[symbol] = min(rank.get(symbol, i), i)
        limit = int(clamp(safe_float(self._setting("realtime.max_symbols"), 4.0) or 4.0, 1, 50))
        chosen: dict[str, str] = {}
        for symbol in sorted(rank, key=lambda s: (rank[s], s)):
            if len(chosen) >= limit or S.is_sim(symbol):
                continue
            route = hub.route(symbol)
            if route.kind != "other" or hub.history_kind(symbol)[1] != "keyed":
                continue
            prov = self.provider_for(symbol, route.asset_class)
            if prov is not None:
                chosen[symbol] = prov
        self._selected = chosen
        return chosen

    def _down_reason(self, provider: str) -> str | None:
        until = self._down.get(provider)
        if until is not None and self.clock() < until[0]:
            return until[1]
        b = self._budgets.get(provider)
        if b is not None and b.blocked():
            return "límite de créditos alcanzado"
        return None

    def serves(self, symbol: str) -> bool:
        """True while the live feed of `symbol` comes from real-time quotes (TradingView snapshots skipped)."""
        prov = self._selected.get(symbol)
        if prov is None or self._down_reason(prov) is not None:
            return False
        if symbol not in self._quotes:
            # first poll in flight (a failed one blocks the symbol or the provider). None scheduled - the plan's
            # limits leave no credits for it, or it answered without this symbol: TradingView keeps serving it
            job = self._jobs.get(prov)
            return job is not None and not job.done() and symbol in self._job_symbols.get(prov, ())
        return self.quote(symbol) is not None

    def cost(self, provider: str, symbols: list[str]) -> int:
        """Credits (Twelve Data) or calls (Polygon) of one quote request for these symbols."""
        if provider == "twelvedata":
            return len(symbols)  # 1 credit per symbol, also inside a batch
        # Polygon: one snapshot call per market (stocks, forex) for all its tickers
        return max(1, len({market_group(s, self.hub.route(s).asset_class) for s in symbols}))

    def interval_for(self, provider: str, symbols: list[str]) -> float | None:
        """Seconds until the next poll of these symbols, or None when the plan's limits leave nothing for quotes."""
        now = float(self.clock())
        per_min, per_day = self.limits(provider)
        reserve = self.reserve()
        cost = self.cost(provider, symbols)
        usable = per_min - (max(1, math.ceil(per_min * reserve)) if per_min > 1 and reserve > 0 else 0)
        if usable < cost:
            return None
        min_s = float(clamp(safe_float(self._setting("realtime.min_interval_s"), 15.0) or 15.0, 5.0, 3600.0))
        interval = max(min_s, 60.0 * cost / usable)
        if per_day > 0:
            b = self.budget(provider)
            left = min(per_day * (1.0 - reserve) - b.used_day(quotes_only=True),
                       per_day - b.used_day() - DAY_FLOOR_CREDITS)
            if left < cost:
                return None
            groups = {market_group(s, self.hub.route(s).asset_class) for s in symbols}
            horizon = max((open_seconds_left(g, now) for g in groups), default=0.0)
            if horizon > 0:
                interval = max(interval, horizon * cost / left)
        return interval

    # ------------------------------------------------------------------ quotes

    def quote(self, symbol: str) -> dict | None:
        """The newest keyed quote of `symbol` while it is usable: fresh (a few poll intervals) and real time, or the
        last price of a closed market. None otherwise (TradingView / the local cache decide)."""
        q = self._quotes.get(symbol)
        if q is None:
            return None
        now = float(self.clock())
        age = now - q["_at"]
        closed = q.get("market_open") is False
        limit = CLOSED_POLL_S * 1.5 if closed else max(FRESH_MIN_S, QUOTE_FRESH_FACTOR * q.get("_interval", 60.0))
        if age > limit or (not closed and q.get("realtime") is not True):
            return None
        if closed:
            group = market_group(symbol, self.hub.route(symbol).asset_class)
            if session_open(group, now) is True and session_open(group, q["_at"]) is False:
                return None  # asked before the session opened: no longer the state of the market (next poll decides)
        return {k: v for k, v in q.items() if not k.startswith("_")}

    def due(self) -> list[tuple[str, list[str]]]:
        """(provider, symbols) batches whose poll is due now (one request in flight per provider)."""
        now = float(self.clock())
        by_provider: dict[str, list[str]] = {}
        for symbol, prov in self._selected.items():
            if self._next.get(symbol, 0.0) <= now + 2.0:
                by_provider.setdefault(prov, []).append(symbol)
        out: list[tuple[str, list[str]]] = []
        for prov, syms in by_provider.items():
            job = self._jobs.get(prov)
            if (job is not None and not job.done()) or self._down_reason(prov) is not None:
                continue
            interval = self.interval_for(prov, syms)
            if interval is None:
                continue
            if self.budget(prov).used_minute() + self.cost(prov, syms) > self.limits(prov)[0]:
                continue  # candle downloads used this minute's credits: retried on a later tick
            for s in syms:
                self._next[s] = now + interval
                if s in self._quotes:
                    self._quotes[s]["_interval"] = interval
            self._job_symbols[prov] = set(syms)
            out.append((prov, syms))
        return out

    def track(self, provider: str, job: asyncio.Future) -> None:
        self._jobs[provider] = job

    async def stop(self) -> None:
        jobs = [j for j in self._jobs.values() if j is not None and not j.done()]
        self._jobs.clear()
        for job in jobs:
            job.cancel()
        for job in jobs:
            try:
                await job
            except (asyncio.CancelledError, Exception):
                pass

    async def poll(self, provider: str, symbols: list[str]) -> dict[str, dict]:
        """One request for these symbols. Returns {symbol: quote} of the answers (errors are recorded, never
        raised: TradingView keeps serving those symbols)."""
        now = float(self.clock())
        key = self.key(provider)
        if not key or not symbols or not self.enabled():
            return {}
        try:
            if provider == "twelvedata":
                got = await self._poll_twelvedata(symbols, key, now)
            else:
                got = await self._poll_polygon(symbols, key, now)
        except asyncio.CancelledError:
            raise
        except PollFailure as exc:
            self._fail(provider, exc.kind, exc.detail, symbols)
            return {}
        except DataProviderError as exc:
            self._fail(provider, exc.kind, str(exc), symbols)
            return {}
        except Exception as exc:  # unexpected answer: never crash the feed
            self._fail(provider, "error", f"{type(exc).__name__}: {exc}", symbols)
            return {}
        self.polls += 1
        self.last_poll_at = now_ms()
        self._down.pop(provider, None)
        self._limited.pop(provider, None)
        interval = self.interval_for(provider, list(got)) or CLOSED_POLL_S
        for symbol, q in got.items():
            self._quotes[symbol] = {**q, "_at": now, "_interval": interval}
            if q.get("market_open") is False:
                # re-checked every CLOSED_POLL_S, or right after the regular session opens (guess) when that is sooner;
                # closed although the guess says open (holiday, early close): the normal re-check
                group = market_group(symbol, self.hub.route(symbol).asset_class)
                soon = next_open_in(group, now) if session_open(group, now) is False else None
                self._next[symbol] = now + (min(CLOSED_POLL_S, soon + 5.0) if soon is not None else CLOSED_POLL_S)
            elif q.get("realtime") is False and (q.get("delay_s") or 0) >= 600:
                # a delayed plan (e.g. Polygon Starter: 15 min) or no trades for a while: not worth its credits
                minutes = int(round((q.get("delay_s") or 0) / 60.0))
                self._blocked.set((provider, symbol), f"precio con ~{minutes} min de retraso (plan con retraso o "
                                  "sin operaciones recientes)", ttl=DELAYED_BLOCK_S)
        return got

    async def _poll_twelvedata(self, symbols: list[str], key: str, now: float) -> dict[str, dict]:
        idents: dict[str, str] = {}
        for symbol in symbols:
            params = S.twelvedata_params(symbol, self.hub.route(symbol).asset_class)
            if params:
                idents[td_ident(params)] = symbol
        if not idents:
            return {}
        self.budget("twelvedata").spend(len(idents), quotes=True)
        result = await request_json(
            self.hub._http(), "GET", TD_QUOTE_URL, provider="twelvedata", label=LABELS["twelvedata"],
            params={"symbol": ",".join(idents), "interval": "1day"}, headers={"Authorization": f"apikey {key}"},
            retries=0, timeout=10.0,
        )
        data = result.data
        if not isinstance(data, dict):
            raise DataProviderError(f"Twelve Data: respuesta inesperada (HTTP {result.status})", provider="twelvedata")
        if len(idents) == 1:
            items: dict[str, Any] = {next(iter(idents)): data}
        else:
            top = td_error(data) if not any(i in data for i in idents) else None
            if top is not None:
                items = {i: data for i in idents}
            else:
                items = data
        out: dict[str, dict] = {}
        whole: tuple[str, str] | None = None
        for ident, symbol in idents.items():
            base = ident.split(":")[0]
            obj = items.get(ident) or items.get(base)
            if obj is None:
                obj = next((v for v in items.values() if isinstance(v, dict)
                            and str(v.get("symbol") or "").upper() == base.upper()), None)
            if not isinstance(obj, dict):
                self._blocked.set(("twelvedata", symbol), "Twelve Data no devolvió esta cotización")
                continue
            err = td_error(obj)
            if err is not None:
                kind, detail = err
                if kind in ("auth", "rate_limit", "day"):
                    whole = err
                    break
                self._blocked.set(("twelvedata", symbol), f"Twelve Data: {detail}")
                continue
            q = td_quote(symbol, obj, now)
            if q is not None:
                out[symbol] = q
        if whole is not None:
            raise PollFailure(whole[0], f"Twelve Data: {whole[1]}")
        return out

    async def _poll_polygon(self, symbols: list[str], key: str, now: float) -> dict[str, dict]:
        groups: dict[str, dict[str, str]] = {}
        for symbol in symbols:
            aclass = self.hub.route(symbol).asset_class
            ticker = S.polygon_ticker(symbol, aclass)
            group = market_group(symbol, aclass)
            if ticker and group in ("stocks", "forex"):
                groups.setdefault(group, {})[ticker] = symbol
        out: dict[str, dict] = {}
        for group, tickers in groups.items():
            self.budget("polygon").spend(1, quotes=True)
            result = await request_json(
                self.hub._http(), "GET", PG_STOCKS_URL if group == "stocks" else PG_FOREX_URL, provider="polygon",
                label=LABELS["polygon"], params={"tickers": ",".join(tickers)},
                headers={"Authorization": f"Bearer {key}"}, retries=0, timeout=10.0,
            )
            err = pg_error(result)
            if err is not None:
                kind, detail = err
                if kind == "premium":
                    what = "acciones" if group == "stocks" else "divisas"
                    self._blocked.set(("polygon", group), f"Polygon: tu plan no incluye cotizaciones en vivo de {what}")
                    continue
                raise PollFailure(kind, f"Polygon: {detail}")
            data = result.data if isinstance(result.data, dict) else {}
            for item in data.get("tickers") or []:
                if isinstance(item, dict) and item.get("ticker") in tickers:
                    q = pg_quote(tickers[item["ticker"]], item, now, group)
                    if q is not None:
                        out[q["symbol"]] = q
        return out

    def _fail(self, provider: str, kind: str, detail: str, symbols: list[str]) -> None:
        now = float(self.clock())
        self.errors += 1
        self.last_error = detail[:300]
        b = self.budget(provider)
        if kind == "auth":
            self._down[provider] = (now + AUTH_BACKOFF_S, "clave API inválida o no autorizada")
        elif kind == "rate_limit":
            self._limited[provider] = self._limited.get(provider, 0) + 1
            # HTTP 429 does not say whether the minute or the day is exhausted: the second one in a row waits longer
            b.block(60.0 - (now % 60.0) + 2.0 if self._limited[provider] < 2 else RATE_LIMIT_LONG_S)
        elif kind == "day":
            b.block_day()
        elif kind == "premium":
            for s in symbols:
                self._blocked.set((provider, s), detail)
        else:
            self._down[provider] = (now + ERROR_BACKOFF_S, detail[:160])
        hub = self.hub
        if hasattr(hub, "_warn_once"):
            hub._warn_once(f"realtime:{provider}:{kind}",
                           f"Tiempo real ({LABELS.get(provider, provider)}): {detail}; mientras tanto se usa "
                           "TradingView", source="hub", every_s=600)

    # ------------------------------------------------------------------ status

    def provider_state(self, provider: str) -> tuple[str, str]:
        """(state, Spanish detail): sin_clave | sin_tiempo_real | en_espera | activo | limite | plan | error."""
        if provider == "alphavantage":
            if not self.key(provider):
                return "sin_clave", "Sin clave configurada."
            return "sin_tiempo_real", "Solo historial: su plan gratuito no da precios en vivo."
        if not self.key(provider):
            return "sin_clave", "Sin clave configurada: se usa TradingView (con el retraso que anuncie)."
        if not self.enabled():
            return "en_espera", "Desactivado en Ajustes o modo sin conexión."
        reason = self._down_reason(provider)
        if reason:
            state = "limite" if "límite" in reason or "créditos" in reason else "error"
            return state, reason[:1].upper() + reason[1:] + "; mientras tanto se usa TradingView."
        blocked = [str(self._blocked.get((provider, g)) or "") for g in ("stocks", "forex")]
        blocked = [b for b in blocked if b]
        if blocked and len(blocked) == 2:
            return "plan", "; ".join(dict.fromkeys(blocked)) + "."
        if any(p == provider for p in self._selected.values()):
            return "activo", "Consultando cotizaciones en vivo."
        return "en_espera", "Listo: se usa al abrir un gráfico o con posiciones demo abiertas de acciones o forex."

    def status(self) -> dict:
        now = float(self.clock())
        providers = []
        for prov in ("twelvedata", "polygon", "alphavantage"):
            state, detail = self.provider_state(prov)
            per_min, per_day = self.limits(prov) if prov != "alphavantage" else (0, 25)
            b = self._budgets.get(prov)
            providers.append({
                "id": prov, "label": LABELS[prov], "configured": bool(self.key(prov)),
                "live": prov in REALTIME_PROVIDERS, "plan": PLAN_NOTES[prov], "doc_url": DOC_URLS[prov],
                "state": state, "detail": detail, "credits_min": per_min, "used_min": b.used_minute() if b else 0,
                "credits_day": per_day, "used_day": b.used_day() if b else 0,
            })
        symbols = []
        for symbol, prov in sorted(self._selected.items()):
            q = self._quotes.get(symbol) or {}
            nxt = self._next.get(symbol)
            symbols.append({
                "symbol": symbol, "provider": prov, "interval_s": round(float(q.get("_interval") or 0.0), 1) or None,
                "last_at": int(q["_at"] * 1000) if q.get("_at") else None, "ts": q.get("ts"),
                "realtime": q.get("realtime"), "delay_s": q.get("delay_s"), "market_open": q.get("market_open"),
                "next_in_s": round(max(0.0, nxt - now), 1) if nxt is not None else None,
            })
        return {
            "enabled": self.enabled(), "offline": bool(config.OFFLINE), "providers": providers, "symbols": symbols,
            "polls": self.polls, "errors": self.errors, "last_error": self.last_error,
            "last_poll_at": self.last_poll_at,
        }


# ----------------------------------------------------------------------------- labels (terminal; the UI has its own)

SOURCE_LABELS = {"binance": "Binance", "okx": "OKX", "coinbase": "Coinbase", "kraken": "Kraken", "kucoin": "KuCoin",
                 "twelvedata": "Twelve Data", "polygon": "Polygon", "alphavantage": "Alpha Vantage",
                 "tradingview": "TradingView", "cache": "caché local"}
KEYED_LIVE_MAX_S = 150  # an API-key price older than this is shown with its age (freshness.ts KEYED_LIVE_MAX_S)


def ago_short(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s"
    if s < 90 * 60:
        return f"{round(s / 60)} min"
    if s < 48 * 3600:
        return f"{round(s / 3600)} h"
    n = round(s / 86400)
    return f"{n} día" if n == 1 else f"{n} días"


def freshness_label(q: dict | None, now_ms_: int | None = None, symbol: str = "") -> str:
    """Spanish freshness of a Quote: 'Tiempo real · Twelve Data', 'Retraso 15 min · TradingView',
    'Mercado cerrado · Twelve Data', 'Desactualizado · hace 3 h'."""
    if S.is_sim(symbol or str((q or {}).get("symbol") or "")):
        return "Sintético"
    if not q:
        return "Sin cotización"
    now = now_ms() if now_ms_ is None else int(now_ms_)
    source = SOURCE_LABELS.get(str(q.get("source") or ""), "")
    tail = f" · {source}" if source else ""
    if q.get("stale"):
        return f"Desactualizado · hace {ago_short((now - int(q.get('ts') or now)) / 1000.0)}"
    if q.get("market_open") is False:
        return f"Mercado cerrado{tail}"
    delay = safe_float(q.get("delay_s"))
    if delay and delay >= 60:
        return f"Retraso {round(delay / 60)} min{tail}"
    if q.get("realtime") is True:
        age = (now - int(q.get("ts") or now)) / 1000.0
        if q.get("source") in KEY_SETTINGS and age > KEYED_LIVE_MAX_S:
            # polled every few dozen seconds (plan limits): its age, not "real time" (same rule as the chart badge)
            return f"{source} · hace {ago_short(age)}"
        return f"Tiempo real{tail}"
    return source or "Origen desconocido"
