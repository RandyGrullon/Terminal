"""MarketDataHub (`rt.hub`): the single entry point for market data.

- Candles: `SIM:*` synthetic sets (fed live by the simulator through `push_candle`), crypto from official exchange
  APIs (`app.data.providers`), stocks / forex / indices / futures from API-key providers (`app.data.keyed`) or, when
  no key is configured, from the local history built by the recorder (TradingView current-bar snapshots).
- DB cache (`candles` table): incremental fetches, forming bar refreshed with a short TTL, concurrent identical
  requests deduplicated, holes inside cached crypto windows (24/7 markets) refetched from the exchange.
- Freshness: a series whose newest bar is clearly too old (venue stopped trading, feed down) comes back with
  `stale=True` and a Spanish note ("Datos desactualizados: última vela hace 3 días"); quotes older than 2 minutes
  carry `stale: True` and `ts` = the real time of the price.
- Symbols: resolve / search / symbol_info (`symbols` table), quotes, universe and raw scans through TradingView.
- Quotes: source priority exchange (crypto followed live) > real-time quote of an API-key provider
  (app.data.realtime) > TradingView (with the delay it announces) > local cache (stale); optional fields `source`,
  `realtime`, `delay_s`, `market_open`.
- Live feed: ref-counted subscriptions, one task per polled key (a slow venue only delays its own keys) plus one for
  the TradingView snapshots (placed by app.data.stamping, shared with the recorder) and one per API-key provider
  for the real-time quotes of charts / demo positions, bar-close detection (missed bars after a stall are fetched
  and emitted in order), WS `market:SYMBOL:tf` (+ the `source` of the price) + listeners.
- Watchlist (`watchlist` table).
"""
from __future__ import annotations

import asyncio
import functools
import time
import weakref
import zlib
from typing import Any, Awaitable, Callable

import numpy as np

from app import config
from app.data import symbols as S
from app.data.base import CandleSet, ProviderError, make_symbol_info, merge_candles, resample
from app.data.recorder import (
    RECORDER_BASE, RECORDER_TFS, ROLLOVER_JITTER_S, CandleRecorder, normalize_candle, same_bar, signature,
)
from app.data.realtime import RealtimeQuotes, market_group, session_open
from app.data.stamping import PLACE_RANK
from app.util import (
    RateLimitedLog,
    TTLCache,
    clamp,
    floor_time,
    normalize_tf,
    now_ms,
    now_s,
    parse_symbol,
    safe_float,
    tf_seconds,
)
from app.ws import market_topic

Listener = Callable[[str, str, dict, bool], Awaitable[Any]]

MAX_LIMIT = 5000  # bars per request
SIM_MIN_BARS = 1500  # synthetic history generated per (symbol, tf)
SIM_MAX_BARS = 20000  # cap for simulator-extended sets (memory)
FORMING_TTL_S = 5.0  # the forming bar of a cached crypto series is refreshed after this
CRYPTO_DOWN_S = 600.0  # a crypto symbol no exchange could serve uses the recorder path for this long
MAX_VENUES = 4  # exchanges tried per crypto request
LIVE_PRICE_TTL_S = 15.0  # live feed prices younger than this answer last_price()
CRYPTO_POLL_S = 3.0  # live feed: crypto symbols (exchange API)
SNAPSHOT_POLL_S = 15.0  # live feed: recorder / keyed symbols (TradingView current bars)
FEED_TICK_S = 1.0
FEED_DOWN_AFTER = 5  # consecutive live-feed failures before a crypto symbol's live updates use TradingView
FEED_FALLBACK_S = 300.0  # ... for this long (live updates only: candles keep coming from the exchanges)
LIVE_HTTP_TIMEOUT_S = 5.0  # one live poll request (no retries: the next poll comes a few seconds later)
LIVE_POLL_TIMEOUT_S = 8.0  # whole live poll of one key (incl. waiting for the exchange's rate limiter)
CATCHUP_TIMEOUT_S = 20.0
MAX_CATCHUP_BARS = 360  # bars missed by a stalled feed that are fetched and emitted as closed (else: gap fill only)
GAP_FILL_LIMIT = 1000  # get_candles limit used to download a gap found by the live feed (large: no history reset)
TRANSIENT_BACKOFF_S = 30.0  # (symbol, tf) after a network / rate-limit / server error of every venue
DEFINITIVE_BACKOFF_S = 300.0  # (symbol, tf) after every venue answered "no data" (pair delisted...)
HOLE_RECHECK_S = 1800.0  # a cached crypto window with holes is re-verified with the exchange at most this often
# kinds of DataProviderError that are a definitive answer about the symbol (anything else is worth retrying soon)
DEFINITIVE_KINDS = frozenset({"not_found", "unsupported", "invalid"})
# Freshness (I2): a series is stale when its newest bar is more than STALE_BARS bars behind AND it ended more than
# STALE_MIN_S ago (24/7 crypto; quiet minutes of illiquid pairs are fine) or SESSION_STALE_S ago (markets with
# sessions: nights, weekends and holidays are normal).
STALE_BARS = 3
STALE_MIN_S = 3600
SESSION_STALE_S = 6 * 86400
QUOTE_STALE_S = 120  # a quote whose price is older than this is flagged stale (never a live price)
EXCHANGE_PRICE_MAX_AGE_S = 180  # last_price(): newest 1m exchange bar older than this is not a live price
# TradingView current-bar timeframes usable as they are for keyed series / crypto (recorder series use RECORDER_TFS)
SNAPSHOT_TFS = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")
SNAPSHOT_SOURCES = frozenset({"recorder", "keyed", "snapshot"})  # live rows that come from TradingView snapshots
UPSERT_CHUNK = 1000
INFO_FIELDS = ("ticker", "exchange", "description", "type", "market", "currency", "pricescale", "logoid")
UNIVERSE_NUMBERS = (
    "close", "change_pct", "volume", "rating", "rsi", "adx", "atr", "perf_w", "perf_1m", "volatility_d", "market_cap",
)
# UniverseRow key -> TradingView column (used to score universe rows with the scanner's quick model)
UNIVERSE_TV_COLUMNS = {
    "close": "close", "change_pct": "change", "volume": "volume", "rating": "Recommend.All", "rsi": "RSI",
    "adx": "ADX", "atr": "ATR", "volatility_d": "Volatility.D", "perf_w": "Perf.W", "perf_1m": "Perf.1M",
}
QUOTE_FIELDS = ("price", "change_pct", "change", "volume", "high", "low", "open")
# optional Quote fields (types-realtime.ts): where the price comes from and how fresh it is
QUOTE_SOURCES = frozenset({"binance", "okx", "coinbase", "kraken", "kucoin", "twelvedata", "polygon", "alphavantage",
                           "tradingview", "cache"})
KEYED_SETTINGS = (
    ("twelvedata", "providers.twelvedata_key", S.twelvedata_params),
    ("polygon", "providers.polygon_key", S.polygon_ticker),
    ("alphavantage", "providers.alphavantage_key", S.alphavantage_spec),
)
PROVIDER_LABELS = {
    "binance": "Binance", "okx": "OKX", "coinbase": "Coinbase", "kraken": "Kraken", "kucoin": "KuCoin",
    "twelvedata": "Twelve Data", "alphavantage": "Alpha Vantage", "polygon": "Polygon",
    "recorder": "grabador local", "synthetic": "mercado sintético", "tradingview": "TradingView",
}
SIM_DESCRIPTIONS = {"SIM:NEXUS": "Mercado sintético NEXUS", "SIM:REPLAY": "Repetición del simulador"}
SIM_NOTE = "Mercado sintético: datos simulados, no son precios reales"
_ASSET_TYPES = {
    "crypto": "spot", "stock": "stock", "fund": "fund", "forex": "forex", "commodity": "commodity",
    "index": "index", "futures": "futures", "cfd": "cfd", "bond": "bond",
}
# Native instrument ids of the official exchange APIs (used when app.data.providers has no helper for it).
_NATIVE_FORMATS = {
    "binance": "{base}{quote}", "okx": "{base}-{quote}", "coinbase": "{base}-{quote}",
    "kraken": "{base}{quote}", "kucoin": "{base}-{quote}",
}


def provider_label(provider: str | None) -> str:
    return PROVIDER_LABELS.get(str(provider or ""), str(provider or "?"))


def clean_rows(raw: Any) -> list[dict]:
    """Normalized candles, ascending, unique by time (the last occurrence wins)."""
    by_time: dict[int, dict] = {}
    for item in raw or []:
        c = normalize_candle(item)
        if c is not None:
            by_time[c["time"]] = c
    return [by_time[t] for t in sorted(by_time)]


def keyed_ttl(tf: str) -> float:
    """How long a keyed-provider series stays fresh (free tiers allow few requests per day)."""
    return float(clamp(tf_seconds(tf) / 4.0, 60.0, 6 * 3600.0))


def normalize_quote(symbol: str, q: Any) -> dict | None:
    """Quote dict exactly as types.ts (None when there is no usable price)."""
    if not isinstance(q, dict):
        return None
    out: dict[str, Any] = {"symbol": symbol}
    for k in QUOTE_FIELDS:
        out[k] = safe_float(q.get(k))
    if out["price"] is None:
        return None
    ts = safe_float(q.get("ts"))
    out["ts"] = int(ts) if ts is not None and ts > 0 else now_ms()
    if q.get("stale"):
        out["stale"] = True
    source = q.get("source")
    if isinstance(source, str) and source in QUOTE_SOURCES:
        out["source"] = source
    if isinstance(q.get("realtime"), bool):
        out["realtime"] = q["realtime"]
    delay = safe_float(q.get("delay_s"))
    if delay is not None and delay >= 1 and not isinstance(q.get("delay_s"), bool):
        out["delay_s"] = int(round(delay))
    if isinstance(q.get("market_open"), bool):
        out["market_open"] = q["market_open"]
    return out


def is_definitive(exc: BaseException) -> bool:
    """True for provider errors that settle whether a venue has the pair (not found / unsupported / invalid)."""
    return getattr(exc, "kind", None) in DEFINITIVE_KINDS


def ago_text(seconds: float) -> str:
    """'45 minutos' / '1 hora' / '3 días'."""
    s = max(0, int(seconds))
    if s < 90 * 60:
        n = max(1, round(s / 60))
        return f"{n} minuto" if n == 1 else f"{n} minutos"
    if s < 48 * 3600:
        n = round(s / 3600)
        return f"{n} hora" if n == 1 else f"{n} horas"
    n = round(s / 86400)
    return f"{n} día" if n == 1 else f"{n} días"


def stale_note(last_time: int, tf: str, now: int, continuous: bool) -> str | None:
    """Spanish note when the newest bar of a series is clearly older than it should be (None when fresh enough).
    `continuous` = the market trades 24/7 (crypto); other markets have nights, weekends and holidays."""
    tf_s = tf_seconds(tf)
    behind = (floor_time(now, tf) - int(last_time)) // tf_s
    quiet = int(now) - (int(last_time) + tf_s)
    if behind <= STALE_BARS or quiet <= (STALE_MIN_S if continuous else SESSION_STALE_S):
        return None
    return f"Datos desactualizados: última vela hace {ago_text(int(now) - int(last_time))}"


def first_hole(rows: list[dict], tf_s: int) -> int | None:
    """Open time of the first missing bar inside ascending `rows` (None when the window is contiguous)."""
    if len(rows) < 2:
        return None
    times = np.fromiter((int(r["time"]) for r in rows), dtype=np.int64, count=len(rows))
    gaps = np.flatnonzero(np.diff(times) > tf_s)
    return int(times[gaps[0]]) + int(tf_s) if len(gaps) else None


class MarketDataHub:
    """See module docstring. Service protocol: __init__ (no I/O) / start / stop / status."""

    def __init__(self, rt: Any):
        self.rt = rt
        self._running = False
        self._log = RateLimitedLog()
        self._tv: Any = None
        self._tv_error: str | None = None
        self._tv_error_at = 0.0
        self._client: Any = None
        # candles
        self._sim: dict[tuple[str, str], CandleSet] = {}
        self._sim_live: set[tuple[str, str]] = set()
        self._inflight: dict[tuple[str, str, int], asyncio.Future] = {}
        self._locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self._fresh = TTLCache(FORMING_TTL_S, maxsize=4096)
        self._keyed_fresh = TTLCache(3600.0, maxsize=4096)
        self._keyed_avail: dict[tuple[str, str, str], int] = {}
        self._history_start: dict[tuple[str, str], int] = {}
        self._venue_ok: dict[str, S.Venue] = {}
        self._crypto_down = TTLCache(CRYPTO_DOWN_S, maxsize=2048)
        self._backoff = TTLCache(TRANSIENT_BACKOFF_S, maxsize=4096)  # (symbol, tf) -> (error, transient)
        self._holes_checked = TTLCache(HOLE_RECHECK_S, maxsize=4096)
        self._keyed_offsets: dict[tuple[str, str], int] = {}  # (symbol, tf) -> session offset of the provider's bars
        self._keyed_clean: set[tuple[str, str, str]] = set()  # keyed series already purged of other sources
        # symbols
        self._info_mem = TTLCache(900.0, maxsize=4096)
        self._remembered: dict[str, tuple[str, str]] = {}
        self._resolve_cache = TTLCache(600.0, maxsize=2048)
        self._details_miss = TTLCache(600.0, maxsize=2048)
        # live feed
        self._subs: dict[tuple[str, str], set[str]] = {}
        self._listeners: list[Listener] = []
        self._last: dict[tuple[str, str], dict] = {}
        self._next_poll: dict[tuple[str, str], float] = {}
        self._prices: dict[str, tuple[float, float]] = {}
        self._feed_task: asyncio.Task | None = None
        self._feed_failures: dict[str, int] = {}
        self._feed_tv = TTLCache(FEED_FALLBACK_S, maxsize=2048)  # crypto symbols fed by TradingView snapshots
        self._jobs: dict[tuple[str, str], asyncio.Future] = {}  # in-flight live poll per (symbol, tf)
        self._snap_job: asyncio.Future | None = None
        self._gap_jobs: dict[tuple[str, str], asyncio.Future] = {}
        self._snap_seen: dict[tuple[str, str], tuple[int, Any]] = {}  # (symbol, TradingView tf) -> (time, OHLCV)
        self._closed_at: dict[tuple[str, str], int] = {}  # snapshot keys: bar already emitted closed (market shut)
        self._price_meta: dict[str, dict] = {}  # symbol -> source of the newest live price (see _dispatch)
        self._live_meta: dict[tuple[str, str], dict] = {}  # (symbol, tf) -> source of the newest live bar
        self._rt_vol: dict[str, tuple[int, float]] = {}  # symbol -> (UTC day, day volume) of the last keyed quote
        self.realtime = RealtimeQuotes(self)
        self.feed_polls = 0
        self.feed_updates = 0
        # stats
        self.cache_hits = 0
        self.cache_misses = 0
        self.fetches = 0
        self.fetch_errors = 0
        self.recorder = CandleRecorder(self)
        self._settings_hooked = False

    # ================================================================== service protocol

    async def start(self) -> None:
        self._running = True
        try:
            await self._seed_watchlist()
        except Exception as exc:
            self._emit("warn", f"No se pudo preparar la lista de seguimiento: {exc}")
        if self.rt.settings is not None and not self._settings_hooked:
            self.rt.settings.add_listener(self._on_setting)
            self._settings_hooked = True
        if config.BACKGROUND and not config.OFFLINE:
            self._feed_task = asyncio.create_task(self._feed_loop(), name="hub-feed")
            if self.recorder.enabled():
                self.recorder.start()
        self._emit("info", self._startup_message())

    def _on_setting(self, key: str, value: Any) -> None:
        """Settings listener: recorder on/off at runtime; provider changes invalidate routing caches."""
        if not self._running:
            return
        if key == "recorder.enabled":
            if value and config.BACKGROUND and not config.OFFLINE:
                self.recorder.start()
            elif not value and self.recorder.running:
                try:
                    asyncio.get_running_loop().create_task(self.recorder.stop())
                except RuntimeError:
                    pass
        elif key.startswith("realtime."):
            self.realtime.reset()
        elif key.startswith("providers."):
            self.realtime.reset()
            self._remembered.clear()
            self._resolve_cache.clear()
            self._keyed_fresh.clear()
            self._crypto_down.clear()
            self._details_miss.clear()
            self._backoff.clear()
            self._feed_tv.clear()
            self._keyed_offsets.clear()

    async def stop(self) -> None:
        self._running = False
        task, self._feed_task = self._feed_task, None
        jobs = [task, self._snap_job, *self._jobs.values(), *self._gap_jobs.values()]
        self._snap_job = None
        self._jobs.clear()
        self._gap_jobs.clear()
        for job in jobs:
            if job is not None and not job.done():
                job.cancel()
        for job in jobs:
            if job is not None:
                try:
                    await job
                except (asyncio.CancelledError, Exception):
                    pass
        await self.recorder.stop()
        await self.realtime.stop()
        for fut in list(self._inflight.values()):
            fut.cancel()
        self._inflight.clear()
        if self._tv is not None:
            try:
                await self._tv.close()
            except Exception:
                pass
            self._tv = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def status(self) -> dict:
        return {
            "running": self._running,
            "subscriptions": len(self._subs),
            "providers": self.providers_status(),
            "recorder": self.recorder.status(),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "fetches": self.fetches,
            "fetch_errors": self.fetch_errors,
            "listeners": len(self._listeners),
            "feed": self._feed_task is not None and not self._feed_task.done(),
            "realtime": {k: v for k, v in self.realtime.status().items() if k in ("enabled", "polls", "errors")},
            "offline": bool(config.OFFLINE),
        }

    def providers_status(self) -> dict:
        tv_status: dict[str, Any]
        if self._tv is not None:
            try:
                tv_status = dict(self._tv.status())
            except Exception as exc:
                tv_status = {"error": str(exc)}
        else:
            tv_status = {"loaded": False, "error": self._tv_error}
        tv_status["enabled"] = bool(self._setting("providers.tradingview_enabled", True))
        return {
            "crypto": list(S.CEX_PROVIDERS),
            "crypto_default": self._preferred(),
            "keyed": {name: bool(str(self._setting(key, "") or "").strip()) for name, key, _ in KEYED_SETTINGS},
            "tradingview": tv_status,
            "offline": bool(config.OFFLINE),
        }

    def _startup_message(self) -> str:
        keyed = [provider_label(n) for n, key, _ in KEYED_SETTINGS if str(self._setting(key, "") or "").strip()]
        parts = [f"Datos de mercado listos · cripto vía {provider_label(self._preferred())} (+ otros exchanges)"]
        parts.append(f"acciones/forex: {', '.join(keyed)}" if keyed else "acciones/forex: grabador local (sin clave API)")
        if config.OFFLINE:
            parts.append("modo sin conexión (solo caché y SIM)")
        elif not self._setting("providers.tradingview_enabled", True):
            parts.append("TradingView desactivado")
        return " · ".join(parts)

    # ================================================================== small helpers

    def _setting(self, key: str, default: Any = None) -> Any:
        store = self.rt.settings
        if store is None:
            return config.DEFAULT_SETTINGS.get(key, default)
        try:
            return store.get(key, default)
        except Exception:
            return default

    def _preferred(self) -> str:
        value = str(self._setting("providers.crypto_default", "binance") or "binance").strip().lower()
        return value if value in S.CEX_PROVIDERS else "binance"

    def _emit(self, level: str, message: str, data: Any = None, source: str = "hub") -> None:
        bus = self.rt.bus
        if bus is not None:
            try:
                bus.emit(level, source, message, data)
            except Exception:
                pass

    def _warn_once(self, key: str, message: str, source: str = "hub", every_s: float = 120.0) -> None:
        if self._log.should_log(key, every_s):
            self._emit("warn", message, source=source)

    @staticmethod
    def _clean(symbol: Any) -> str:
        try:
            return S.clean_symbol(str(symbol or ""))
        except ValueError as exc:
            raise ProviderError(str(exc)) from None

    @staticmethod
    def _tf(tf: Any) -> str:
        try:
            return normalize_tf(tf)
        except ValueError as exc:
            raise ProviderError(str(exc)) from None

    @staticmethod
    def _int_arg(value: Any, default: int, lo: int, hi: int, name: str) -> int:
        """Integer argument clamped to [lo, hi]; ProviderError (Spanish) for non-numbers, NaN and ±inf."""
        try:
            n = int(value) if value not in (None, "") else default
        except (TypeError, ValueError, OverflowError):
            raise ProviderError(f"El parámetro '{name}' debe ser un número entero") from None
        return int(clamp(n, lo, hi))

    # ================================================================== routing

    def route(self, symbol: str, info: dict | None = None) -> S.Route:
        """Where the candles of `symbol` come from (no I/O; uses the cached `symbols` row when loaded)."""
        meta = info if info is not None else (self._info_mem.get(symbol) or None)
        return S.route_symbol(symbol, meta, preferred=self._preferred())

    def keyed_candidates(self, symbol: str, asset_class: str) -> list[tuple[str, str]]:
        """(provider, api_key) of every API-key provider configured AND able to serve `symbol`."""
        out: list[tuple[str, str]] = []
        for provider, key_name, supports in KEYED_SETTINGS:
            api_key = str(self._setting(key_name, "") or "").strip()
            if not api_key:
                continue
            try:
                ok = supports(symbol, asset_class) is not None
            except Exception:
                ok = False
            if ok:
                out.append((provider, api_key))
        return out

    def history_kind(self, symbol: str, info: dict | None = None) -> tuple[str, str]:
        """(provider, history) for a symbol: history is full | keyed | recorder | synthetic."""
        route = self.route(symbol, info)
        if route.kind == "sim":
            return "synthetic", "synthetic"
        if route.kind == "crypto" and route.venues and self._crypto_down.get(symbol) is None:
            return self._known_venue(symbol, route, info).provider, "full"
        keyed = self.keyed_candidates(symbol, route.asset_class)
        if keyed:
            return keyed[0][0], "keyed"
        return "recorder", "recorder"

    @staticmethod
    def _venue_of(route: S.Route, provider: str | None) -> S.Venue | None:
        return next((v for v in route.venues if v.provider == provider), None) if provider else None

    def _known_venue(self, symbol: str, route: S.Route, info: dict | None = None) -> S.Venue:
        """Venue that serves `symbol`: the one that worked last in this process, else (equivalent pairs only) the
        provider stored in the `symbols` table (it survives restarts), else the first venue of the route. A symbol
        whose own instrument is listed starts on it again after a restart: an equivalent pair that served it during
        an outage is not kept for good."""
        venue = self._venue_ok.get(symbol)
        if venue is not None:
            return venue
        if route.native and route.venues:
            return route.venues[0]
        provider = (info or {}).get("provider") or (self._info_mem.get(symbol) or {}).get("provider")
        return self._venue_of(route, provider) or route.venues[0]

    def _http(self) -> Any:
        if self._client is None or getattr(self._client, "is_closed", False):
            from app.util import http_client

            self._client = http_client(timeout=20.0)
        return self._client

    @staticmethod
    def _providers() -> Any:
        from app.data import providers

        return providers

    @staticmethod
    def _keyed_module() -> Any:
        from app.data import keyed

        return keyed

    @staticmethod
    def _native(providers: Any, venue: S.Venue) -> str:
        """Native instrument id of `venue` for providers.fetch_candles / fetch_latest (`Venue.native`)."""
        native = getattr(venue, "native", None)
        if isinstance(native, str) and native:
            return native
        helper = getattr(providers, "native_symbol", None)
        if callable(helper):
            value = helper(venue.provider, venue.base, venue.quote)
            if value:
                return str(value)
        fmt = _NATIVE_FORMATS.get(venue.provider, "{base}{quote}")
        return fmt.format(base=venue.base, quote=venue.quote)

    # ================================================================== candles

    async def get_candles(self, symbol: str, tf: str = "1h", limit: int = 500) -> CandleSet:
        """Ascending CandleSet with up to `limit` bars. Empty set + note when there is no data (never raises for
        that); ProviderError only for invalid input."""
        symbol = self._clean(symbol)
        tf = self._tf(tf)
        try:
            n = int(limit if limit is not None else 500)
        except (TypeError, ValueError, OverflowError):  # OverflowError: limit=inf
            raise ProviderError("El límite de velas debe ser un número entero") from None
        n = int(clamp(n, 1, MAX_LIMIT))
        key = (symbol, tf, n)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._load_candles(symbol, tf, n))
            self._inflight[key] = task
            task.add_done_callback(functools.partial(self._inflight_done, key))
        return await asyncio.shield(task)

    def _inflight_done(self, key: tuple[str, str, int], task: asyncio.Future) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()  # mark retrieved (awaiters may all have been cancelled)

    def _lock_for(self, symbol: str, tf: str) -> asyncio.Lock:
        lock = self._locks.get((symbol, tf))
        if lock is None:
            lock = asyncio.Lock()
            self._locks[(symbol, tf)] = lock
        return lock

    async def _load_candles(self, symbol: str, tf: str, limit: int) -> CandleSet:
        await self._load_info(symbol)
        route = self.route(symbol)
        if route.kind == "sim":
            cs = self._sim_candles(symbol, tf, limit)
        else:
            async with self._lock_for(symbol, tf):
                if route.kind == "crypto" and route.venues and self._crypto_down.get(symbol) is None:
                    cs = await self._crypto_candles(symbol, tf, limit, route)
                else:
                    cs = await self._other_candles(symbol, tf, limit, route, reason=self._crypto_down.get(symbol))
        try:
            await self._remember(symbol, cs.provider or "none", cs.history if len(cs) else self.history_kind(symbol)[1])
        except Exception as exc:
            self._warn_once("remember", f"No se pudo guardar la ficha de {symbol}: {exc}")
        return cs

    # ------------------------------------------------------------------ SIM

    def _sim_candles(self, symbol: str, tf: str, limit: int) -> CandleSet:
        key = (symbol, tf)
        cs = self._sim.get(key)
        if cs is None or (len(cs) < limit and key not in self._sim_live):
            from app.sim.synthetic import generate_candleset

            cs = generate_candleset(
                n=max(limit, SIM_MIN_BARS), seed=zlib.crc32(symbol.encode("utf-8")), tf=tf, symbol=symbol,
                end_time=now_s(),
            )
            self._sim[key] = cs
        return cs.tail(limit).with_meta(provider="synthetic", history="synthetic", note=SIM_NOTE)

    # ------------------------------------------------------------------ crypto (official exchange APIs)

    async def _crypto_candles(self, symbol: str, tf: str, limit: int, route: S.Route) -> CandleSet:
        sources: list[str] = []
        cached = await self._read_cache(symbol, tf, limit, sources)
        venue = self._known_venue(symbol, route)
        if config.OFFLINE:
            note = (
                "Modo sin conexión: datos de la caché local" if cached
                else f"Modo sin conexión: no hay velas guardadas de {symbol} {tf}"
            )
            return self._build(cached, symbol, tf, sources[-1] if sources else venue.provider, "full", note, route)
        fkey = (symbol, tf)
        tf_s = tf_seconds(tf)
        now_bar = floor_time(now_s(), tf)
        reset = False
        hole: int | None = None
        if cached:
            gap = (now_bar - int(cached[-1]["time"])) // tf_s
            need = 0 if (gap <= 0 and self._fresh.get(fkey)) else max(int(gap), 0) + 1
            if need > min(max(2 * limit, 1000), MAX_LIMIT):  # cache far behind: refetch and drop the stale segment
                reset, need = True, limit
            elif self._holes_checked.get(fkey) is None:
                # crypto trades 24/7: a missing bar inside the cached window is a hole (app restart, feed stall)
                hole = first_hole(cached, tf_s)
                if hole is not None:
                    need = max(need, (now_bar - hole) // tf_s + 1)
                    if need > MAX_LIMIT:  # too old to download in one go: drop the segment before it
                        reset, need = True, limit
        else:
            need = limit
        error: str | None = None
        transient = False
        fetched = False
        if need > 0:
            backoff = self._backoff.get(fkey)
            if backoff is not None:  # every venue failed moments ago: serve what we have, retry after the backoff
                error, transient = backoff
            else:
                rows, used, error, transient = await self._fetch_crypto(
                    symbol, route, tf, min(max(need, 2), MAX_LIMIT)
                )
                if rows and used is not None:
                    if reset:
                        await self._delete_before(symbol, tf, rows[0]["time"])
                        self._history_start.pop(fkey, None)
                        cached, sources = [], []
                    await self._upsert(symbol, tf, rows, used.provider)
                    self._fresh.set(fkey, True)
                    if hole is not None or reset:
                        self._holes_checked.set(fkey, True)  # what is still missing is missing at the exchange
                    if not cached and len(rows) < need:
                        self._history_start[fkey] = int(rows[0]["time"])  # the listing starts here
                    cached = merge_candles(cached, rows)
                    venue, fetched = used, True
                else:
                    ttl = TRANSIENT_BACKOFF_S if transient else DEFINITIVE_BACKOFF_S
                    self._backoff.set(fkey, (error, transient), ttl=ttl)
        if need == 0:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        if cached and len(cached) < limit and fkey not in self._history_start and error is None:
            cached = await self._backfill(symbol, tf, limit, route, venue, cached)
        if not cached:
            reason = error or f"ningún exchange devolvió velas de {symbol}"
            if transient:  # network / rate limit / server: nothing is known about the symbol, retry soon
                # nothing reloads the chart by itself: the user (or the next NEXUS / scanner request) asks again
                note = f"Error temporal al consultar los exchanges ({reason}); vuelve a intentarlo en unos segundos"
                return self._build([], symbol, tf, venue.provider, "full", note, route)
            self._crypto_down.set(symbol, reason)
            self._emit("info", f"{symbol}: sin datos en los exchanges ({reason}); se usa el historial local")
            return await self._other_candles(symbol, tf, limit, route, reason=reason)
        # provider = real source of the newest bars (a restart must not relabel KuCoin bars as Binance)
        provider = venue.provider if fetched or not sources else (sources[-1] or venue.provider)
        shown = venue if venue.provider == provider else self._venue_of(route, provider)
        note = None
        if shown is not None and not (route.native and route.venues and shown == route.venues[0]):
            note = f"Datos de {shown.tv_symbol} vía {provider_label(shown.provider)} (par equivalente)"
        if error:
            note = f"No se pudo actualizar ({error}); se muestran los datos guardados"
        return self._build(cached[-limit:], symbol, tf, provider, "full", note, route)

    async def _backfill(
        self, symbol: str, tf: str, limit: int, route: S.Route, venue: S.Venue, cached: list[dict]
    ) -> list[dict]:
        """Fetch bars older than the cache until `limit` bars are available (or the listing start is reached)."""
        oldest = int(cached[0]["time"])
        want = limit - len(cached) + 1
        rows, used, error, _transient = await self._fetch_crypto(
            symbol, route, tf, min(want, MAX_LIMIT), end_time=oldest, only=venue
        )
        if error:
            return cached
        older = [r for r in rows if r["time"] < oldest]
        if older and used is not None:
            await self._upsert(symbol, tf, older, used.provider)
        if len(older) < want - 1:
            self._history_start[(symbol, tf)] = int(older[0]["time"]) if older else oldest
        return merge_candles(older, cached) if older else cached

    async def _fetch_crypto(
        self, symbol: str, route: S.Route, tf: str, n: int, end_time: int | None = None, only: S.Venue | None = None
    ) -> tuple[list[dict], S.Venue | None, str | None, bool]:
        """(rows, venue that served them, error, transient). Tries the known venue first, then equivalent pairs.
        `transient` is True when a venue failed for a reason worth retrying soon (network, rate limit, server...):
        only definitive answers of every venue (pair not found / unsupported / invalid / no bars) mean that the
        exchanges have no data for the symbol."""
        try:
            providers = self._providers()
        except Exception as exc:
            return [], None, f"módulo de proveedores no disponible ({type(exc).__name__}: {exc})", True
        if only is not None:
            venues = [only]
        else:
            first = self._known_venue(symbol, route)
            venues = list(dict.fromkeys([v for v in (first, *route.venues) if v is not None]))[:MAX_VENUES]
        last_error: str | None = None
        transient_error: str | None = None
        for venue in venues:
            label = provider_label(venue.provider)
            try:
                native = self._native(providers, venue)
                raw = await providers.fetch_candles(
                    venue.provider, native, tf, int(n), client=self._http(), end_time=end_time
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.fetch_errors += 1
                last_error = f"{label}: {exc}"
                if not is_definitive(exc):
                    transient_error = transient_error or last_error
                    self._warn_once(f"fetch:{venue.provider}:{symbol}:{tf}", f"{symbol} {tf}: error de {label} → {exc}")
                # A definitive answer ("par no encontrado" while trying equivalent pairs) is ordinary routing, not a
                # problem worth a warning per venue: the outcome is reported once when no venue has the symbol.
                continue
            self.fetches += 1
            rows = clean_rows(raw)
            if rows:
                if self._venue_ok.get(symbol) != venue:
                    if route.venues and venue != route.venues[0]:
                        self._emit("info", f"{symbol}: usando {venue.tv_symbol} ({label}) como fuente de velas")
                    self._venue_ok[symbol] = venue
                return rows, venue, None, False
            if end_time is not None:
                return [], venue, None, False  # no older history on this venue
            last_error = last_error or f"{label}: sin velas para {venue.tv_symbol}"
        if transient_error is not None:
            return [], None, transient_error, True
        return [], None, last_error, False

    # ------------------------------------------------------------------ stocks / forex / indices / futures

    async def _other_candles(
        self, symbol: str, tf: str, limit: int, route: S.Route, reason: str | None = None
    ) -> CandleSet:
        errors: list[str] = []
        if not config.OFFLINE:
            for provider, api_key in self.keyed_candidates(symbol, route.asset_class):
                cs = await self._keyed_candles(symbol, tf, limit, route, provider, api_key, errors)
                if cs is not None:
                    return cs
        return await self._recorder_candles(symbol, tf, limit, route, errors, reason)

    async def _keyed_candles(
        self, symbol: str, tf: str, limit: int, route: S.Route, provider: str, api_key: str, errors: list[str]
    ) -> CandleSet | None:
        label = provider_label(provider)
        sources: list[str] = []
        cached = await self._read_cache(symbol, tf, limit, sources)
        # bars of another source (TradingView snapshots stamped on the hour, another provider) are never mixed into
        # this series: the provider aligns intraday bars to the session (13:30 UTC for US stocks)
        foreign = any(src != provider for src in sources)
        if foreign:
            cached = [c for c, src in zip(cached, sources) if src == provider]
        fkey = (symbol, tf, provider)
        enough = len(cached) >= min(limit, self._keyed_avail.get(fkey, limit))
        if cached and enough and self._keyed_fresh.get(fkey):
            self.cache_hits += 1
            return self._keyed_set(cached, symbol, tf, provider, None, route)
        want = limit
        if cached and enough:
            gap = (floor_time(now_s(), tf) - int(cached[-1]["time"])) // tf_seconds(tf)
            if 0 <= gap < limit:
                want = int(max(gap + 2, 2))  # incremental refresh
        self.realtime.budget(provider).spend(1)
        try:
            raw = await self._keyed_module().fetch_keyed_candles(
                provider, symbol, route.asset_class, tf, want, api_key=api_key, client=self._http()
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if getattr(exc, "kind", None) == "rate_limit":
                self.realtime.budget(provider).block(60.0)
            self.fetch_errors += 1
            errors.append(f"{label}: {exc}")
            self._warn_once(f"keyed:{provider}:{symbol}:{tf}", f"{symbol} {tf}: error de {label} → {exc}")
            if cached:
                note = f"No se pudo actualizar desde {label} ({exc}); se muestran los datos guardados"
                return self._keyed_set(cached, symbol, tf, provider, note, route)
            return None
        self.fetches += 1
        self.cache_misses += 1
        rows = clean_rows(raw)
        if not rows:
            errors.append(f"{label}: sin velas para {symbol} {tf}")
            if cached:
                return self._keyed_set(cached, symbol, tf, provider, f"{label} no devolvió velas nuevas", route)
            return None
        await self._upsert(symbol, tf, rows, provider)
        if foreign or fkey not in self._keyed_clean:
            # leftovers of another source (live snapshots stored before, recorder history) leave the series
            await self.rt.db.execute(
                "DELETE FROM candles WHERE symbol = ? AND tf = ? AND source <> ?", (symbol, tf, provider)
            )
            self._keyed_clean.add(fkey)
        self._keyed_fresh.set(fkey, True, ttl=keyed_ttl(tf))
        if want >= limit:
            self._keyed_avail[fkey] = len(rows)
        return self._keyed_set(merge_candles(cached, rows)[-limit:], symbol, tf, provider, None, route)

    def _keyed_set(
        self, rows: list[dict], symbol: str, tf: str, provider: str, note: str | None, route: S.Route
    ) -> CandleSet:
        """CandleSet of a keyed series; remembers the provider's session offset (live snapshots are aligned to it)."""
        if rows and tf_seconds(tf) < 86400:
            self._keyed_offsets[(symbol, tf)] = int(rows[-1]["time"]) % tf_seconds(tf)
        return self._build(rows, symbol, tf, provider, "keyed", note, route)

    def _keyed_refresh(self, symbol: str, tf: str) -> None:
        """A keyed bar closed on the live feed: the next get_candles takes the final bar from the provider."""
        for provider, _key, _supports in KEYED_SETTINGS:
            self._keyed_fresh.pop((symbol, tf, provider))

    async def _recorder_candles(
        self, symbol: str, tf: str, limit: int, route: S.Route, errors: list[str], reason: str | None
    ) -> CandleSet:
        """Local history (recorder snapshots or anything cached before). Timeframes that are not recorded directly
        are rebuilt from the recorded base (UTC-aligned buckets); rebuilt bars win over bars stored for that
        timeframe (older versions recorded TradingView's session-aligned 1h/4h bars)."""
        rows = await self._read_cache(symbol, tf, limit)
        base = RECORDER_BASE.get(tf)
        if base:
            ratio = max(1, tf_seconds(tf) // tf_seconds(base))
            base_rows = await self._read_cache(symbol, base, min(limit * ratio + ratio, 50_000))
            if base_rows:
                resampled = resample(CandleSet.from_rows(base_rows, symbol, base), tf).to_list()
                first = int(base_rows[0]["time"])
                if resampled and floor_time(first, tf) != first:
                    resampled = resampled[1:]  # the first bucket is incomplete
                rows = merge_candles(rows, resampled)[-limit:]
        note = self._recorder_note(symbol, tf, route, len(rows), errors, reason)
        return self._build(rows, symbol, tf, "recorder", "recorder", note, route)

    def _recorder_note(
        self, symbol: str, tf: str, route: S.Route, n: int, errors: list[str], reason: str | None
    ) -> str:
        parts: list[str] = []
        if reason:
            parts.append(f"Sin datos de los exchanges ({reason}).")
        if errors:
            parts.append("Proveedores con clave: " + "; ".join(errors) + ".")
        parts.append(f"Historial grabado localmente: {n} velas." if n else f"Aún no hay historial local de {symbol} {tf}.")
        if config.OFFLINE:
            parts.append("Modo sin conexión: el grabador está en pausa.")
        elif not self.recorder.enabled():
            parts.append("El grabador local está desactivado (comando: recorder on).")
        else:
            parts.append("El grabador lo amplía cada pocos segundos mientras esté en la lista de seguimiento o con el gráfico abierto.")
        supported = []
        for provider, _key, supports in KEYED_SETTINGS:
            try:
                if supports(symbol, route.asset_class) is not None:
                    supported.append(provider_label(provider))
            except Exception:
                continue
        if supported and not errors:
            names = supported[0] if len(supported) == 1 else f"{', '.join(supported[:-1])} o {supported[-1]}"
            parts.append(f"Para el historial completo configura una clave de {names} en Ajustes.")
        return " ".join(parts)

    # ------------------------------------------------------------------ DB cache

    _UPSERT_SQL = (
        "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol, tf, time) DO UPDATE SET open = excluded.open, high = excluded.high, "
        "low = excluded.low, close = excluded.close, volume = excluded.volume, source = excluded.source"
    )

    async def _read_cache(self, symbol: str, tf: str, limit: int, sources: list[str] | None = None) -> list[dict]:
        """The newest `limit` cached bars, ascending (`sources`, when given, receives the source of each bar)."""
        rows = await self.rt.db.fetchall(
            "SELECT time, open, high, low, close, volume, source FROM candles WHERE symbol = ? AND tf = ? "
            "ORDER BY time DESC LIMIT ?",
            (symbol, tf, int(limit)),
        )
        out: list[dict] = []
        for r in reversed(rows):
            c = normalize_candle(r)
            if c is not None:
                out.append(c)
                if sources is not None:
                    sources.append(str(r.get("source") or ""))
        return out

    async def _upsert(self, symbol: str, tf: str, rows: list[dict], source: str) -> None:
        params = [
            (symbol, tf, int(c["time"]), c["open"], c["high"], c["low"], c["close"], c["volume"], source)
            for c in rows
        ]
        for i in range(0, len(params), UPSERT_CHUNK):
            await self.rt.db.executemany(self._UPSERT_SQL, params[i : i + UPSERT_CHUNK])

    async def _delete_before(self, symbol: str, tf: str, t: int) -> None:
        await self.rt.db.execute("DELETE FROM candles WHERE symbol = ? AND tf = ? AND time < ?", (symbol, tf, int(t)))

    @staticmethod
    def _build(
        rows: list[dict], symbol: str, tf: str, provider: str, history: str, note: str | None,
        route: S.Route | None = None,
    ) -> CandleSet:
        """CandleSet with the freshness check: a series whose newest bar is clearly too old is flagged `stale` and
        its note starts with "Datos desactualizados: última vela hace ..." (it must not be used as live data)."""
        stale = None
        if rows and route is not None and route.kind != "sim":
            continuous = route.kind == "crypto" or route.asset_class == "crypto"
            stale = stale_note(int(rows[-1]["time"]), tf, now_s(), continuous)
        if stale:
            note = f"{stale}. {note}" if note else stale
        return CandleSet.from_rows(rows, symbol, tf, provider=provider, history=history, note=note, stale=bool(stale))

    # ================================================================== symbols table

    _SYMBOL_UPSERT = (
        "INSERT INTO symbols(symbol, ticker, exchange, description, type, market, currency, pricescale, logoid, "
        "provider, history, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET ticker = excluded.ticker, exchange = excluded.exchange, "
        "description = excluded.description, type = excluded.type, market = excluded.market, "
        "currency = excluded.currency, pricescale = excluded.pricescale, logoid = excluded.logoid, "
        "provider = excluded.provider, history = excluded.history, updated_at = excluded.updated_at"
    )

    async def _load_info(self, symbol: str) -> dict:
        """The stored `symbols` row of `symbol` (memory-cached); {} when unknown."""
        cached = self._info_mem.get(symbol)
        if cached is not None:
            return cached
        try:
            row = await self.rt.db.fetchone(
                "SELECT symbol, ticker, exchange, description, type, market, currency, pricescale, logoid, "
                "provider, history FROM symbols WHERE symbol = ?",
                (symbol,),
            )
        except Exception:
            row = None
        info = {k: v for k, v in row.items() if v is not None} if row else {}
        self._info_mem.set(symbol, info)
        return info

    def default_info(self, symbol: str) -> dict:
        """SymbolInfo inferred from the symbol alone (no I/O)."""
        exchange, ticker = parse_symbol(symbol)
        if S.is_sim(symbol):
            return make_symbol_info(
                symbol, description=SIM_DESCRIPTIONS.get(symbol, "Mercado sintético"), type="synthetic",
                market="sim", currency="USD", pricescale=100,
            )
        route = S.route_symbol(symbol, None, preferred=self._preferred())
        currency = ""
        if route.kind == "crypto":
            pair = S.split_pair(S.strip_derivative(ticker)[0])
            currency = pair[1] if pair else ""
        else:
            fx = S.fx_pair(ticker)
            currency = fx[1] if fx else ""
        kind = "crypto" if route.kind == "crypto" else route.asset_class
        return make_symbol_info(
            symbol, type=_ASSET_TYPES.get(kind, ""), market=S.market_for(exchange, ticker), currency=currency
        )

    def compose_info(
        self, symbol: str, meta: dict | None = None, provider: str | None = None, history: str | None = None
    ) -> dict:
        """Complete SymbolInfo dict (types.ts) from stored/inferred metadata + routing."""
        info = self.default_info(symbol)
        src = meta if meta is not None else (self._info_mem.get(symbol) or {})
        for k in INFO_FIELDS:
            v = src.get(k)
            if v not in (None, ""):
                info[k] = v
        if provider is None or history is None:
            provider, history = self.history_kind(symbol, src or None)
        info["provider"], info["history"] = provider, history
        ps = safe_float(info.get("pricescale"))
        info["pricescale"] = int(ps) if ps else None
        info["logoid"] = str(info["logoid"]) if info.get("logoid") else None
        for k in ("description", "type", "market", "currency"):
            info[k] = str(info.get(k) or "")
        return info

    async def _remember(self, symbol: str, provider: str, history: str, meta: dict | None = None) -> None:
        """Upsert the `symbols` row of a served symbol (only when something changed)."""
        if not meta and self._remembered.get(symbol) == (provider, history):
            return
        current = await self._load_info(symbol)
        if not meta and current and current.get("provider") == provider and current.get("history") == history:
            self._remembered[symbol] = (provider, history)
            return
        merged = dict(current)
        for k, v in (meta or {}).items():
            if k in INFO_FIELDS and v not in (None, ""):
                merged[k] = v
        info = self.compose_info(symbol, merged, provider, history)
        await self._store_info(info)
        self._remembered[symbol] = (provider, history)

    async def _store_info(self, info: dict) -> None:
        await self.rt.db.execute(
            self._SYMBOL_UPSERT,
            (
                info["symbol"], info["ticker"], info["exchange"], info["description"], info["type"], info["market"],
                info["currency"], info["pricescale"], info["logoid"], info["provider"], info["history"], now_ms(),
            ),
        )
        self._info_mem.set(info["symbol"], {k: v for k, v in info.items() if v is not None})

    # ================================================================== TradingView

    def tv(self) -> Any:
        """Shared TradingViewClient (None when app.data.tradingview cannot be loaded; retried every 60 s)."""
        if self._tv is None and (self._tv_error is None or time.monotonic() - self._tv_error_at > 60.0):
            try:
                from app.data.tradingview import TradingViewClient

                self._tv = TradingViewClient(enabled=self._tv_enabled)
                self._tv_error = None
            except Exception as exc:
                self._tv_error = f"{type(exc).__name__}: {exc}"
                self._tv_error_at = time.monotonic()
                self._warn_once("tv-load", f"Cliente de TradingView no disponible: {self._tv_error}", "tradingview", 600)
        return self._tv

    def _tv_enabled(self) -> bool:
        return bool(self._setting("providers.tradingview_enabled", True)) and not config.OFFLINE

    def tv_online(self) -> bool:
        return self._tv_enabled() and self.tv() is not None

    async def _tv_details(self, symbol: str) -> dict | None:
        tv = self.tv()
        if tv is None:
            return None
        try:
            details = await tv.symbol_details(symbol)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once(f"details:{symbol}", f"TradingView: sin ficha de {symbol} ({exc})", "tradingview", 300)
            details = None
        if not isinstance(details, dict) or not details:
            self._details_miss.set(symbol, True)
            return None
        return details

    async def _absorb_details(self, symbol: str, details: dict) -> None:
        meta = {k: details.get(k) for k in INFO_FIELDS if details.get(k) not in (None, "")}
        current = await self._load_info(symbol)
        provider, history = self.history_kind(symbol, {**current, **meta})
        await self._remember(symbol, provider, history, meta=meta)

    def _needs_details(self, symbol: str, info: dict) -> bool:
        if S.is_sim(symbol) or self._details_miss.get(symbol) is not None:
            return False
        description = str(info.get("description") or "")
        return not info.get("type") or not description or description == str(info.get("ticker") or "")

    # ================================================================== symbols API

    async def symbol_info(self, symbol: str, fetch: bool = True) -> dict:
        """SymbolInfo (types.ts) incl. provider and history; enriched once from TradingView when online."""
        symbol = self._clean(symbol)
        info = await self._load_info(symbol)
        if fetch and self._needs_details(symbol, info) and self.tv_online():
            details = await self._tv_details(symbol)
            if details:
                await self._absorb_details(symbol, details)
        provider, history = self.history_kind(symbol)
        await self._remember(symbol, provider, history)
        return self.compose_info(symbol, None, provider, history)

    async def symbol_detail(self, symbol: str) -> dict:
        """SymbolDetail: SymbolInfo + quote + technicals (technicals only from TradingView)."""
        symbol = self._clean(symbol)
        details = None
        if not S.is_sim(symbol) and self.tv_online():
            details = await self._tv_details(symbol)
            if details:
                await self._absorb_details(symbol, details)
        info = await self.symbol_info(symbol, fetch=False)
        quote = normalize_quote(symbol, (details or {}).get("quote"))
        if quote is not None:
            quote = self._best_quote(symbol, {**quote, "source": "tradingview"})
        if quote is None:
            quote = (await self.quotes([symbol])).get(symbol)
        raw_tech = (details or {}).get("technicals")
        technicals = {str(k): safe_float(v) for k, v in raw_tech.items()} if isinstance(raw_tech, dict) else None
        return {**info, "quote": quote, "technicals": technicals}

    async def resolve(self, text: str) -> str | None:
        """'btc' -> 'BINANCE:BTCUSDT', 'aapl' -> 'NASDAQ:AAPL': aliases, then the symbols table, then the best
        TradingView search hit. Full symbols pass through uppercased. None when nothing matches."""
        raw = " ".join(str(text or "").split())
        if not raw:
            return None
        if ":" in raw:
            try:
                return S.clean_symbol(raw)
            except ValueError:
                return None
        key = raw.lower()
        hit = self._resolve_cache.get(key)
        if hit:
            return hit
        found = S.alias_lookup(raw, crypto_provider=self._preferred()) or await self._resolve_db(raw)
        if not found and self.tv_online():
            try:
                results = await self.search(raw, limit=8)
            except Exception:
                results = []
            found = results[0]["symbol"] if results else None
        if found:
            self._resolve_cache.set(key, found)
        return found

    async def _resolve_db(self, text: str) -> str | None:
        compact = "".join(text.split()).upper()
        try:
            rows = await self.rt.db.fetchall(
                "SELECT symbol, ticker, exchange, description, type FROM symbols "
                "WHERE ticker = ? OR UPPER(description) = ? LIMIT 50",
                (compact, text.upper()),
            )
        except Exception:
            return None
        ranked = S.rank_results(text, rows)
        return str(ranked[0]["symbol"]) if ranked else None

    def _market_of(self, symbol: str) -> str:
        info = self._info_mem.get(symbol) or {}
        if info.get("market"):
            return str(info["market"])
        exchange, ticker = parse_symbol(symbol)
        return "sim" if S.is_sim(symbol) else S.market_for(exchange, ticker)

    def _result_row(self, item: dict) -> dict | None:
        """SearchResult (SymbolInfo + price + change_pct) from a TradingView / local item."""
        try:
            symbol = S.clean_symbol(str(item.get("symbol") or ""))
        except ValueError:
            return None
        meta = {k: item.get(k) for k in INFO_FIELDS if item.get(k) not in (None, "")}
        provider, history = self.history_kind(symbol, meta or None)
        row = self.compose_info(symbol, meta, provider, history)
        row["price"] = safe_float(item.get("price"))
        row["change_pct"] = safe_float(item.get("change_pct"))
        return row

    async def search(self, q: str, market: str | None = None, limit: int = 30) -> list[dict]:
        """SearchResult list: alias hit first, then TradingView results, then local matches (offline too)."""
        query = " ".join(str(q or "").split())[:64]
        limit = self._int_arg(limit or 30, 30, 1, 100, "limit")
        market = (str(market or "").strip().lower()) or None
        if market and not S.valid_market(market):
            raise ProviderError(f"Mercado no válido: {market}")
        if not query:
            return []
        remote: list[dict] = []
        if self.tv_online():
            try:
                remote = [r for r in (await self.tv().search(query, market=market, limit=limit) or []) if isinstance(r, dict)]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn_once("search", f"Búsqueda en TradingView falló: {exc}", "tradingview")
        out: list[dict] = []
        seen: set[str] = set()

        def add(item: dict) -> None:
            row = self._result_row(item)
            if row is not None and row["symbol"] not in seen and len(out) < limit:
                seen.add(row["symbol"])
                out.append(row)

        alias = S.alias_lookup(query, crypto_provider=self._preferred())
        if alias and (not market or market == "global" or self._market_of(alias) == market):
            # the alias keeps first place but uses TradingView's richer row (price, logo...) when it has one
            remote_hit = next((r for r in remote if str(r.get("symbol") or "").upper() == alias), None)
            add(remote_hit or {"symbol": alias, **(await self._load_info(alias))})
        for item in remote:
            add(item)
        if len(out) < limit:
            for item in await self._local_search(query, market):
                add(item)
        await self._store_search_meta(out[:3])
        return out

    async def _local_search(self, query: str, market: str | None) -> list[dict]:
        like = f"%{query}%"
        try:
            rows = await self.rt.db.fetchall(
                "SELECT symbol, ticker, exchange, description, type, market, currency, pricescale, logoid "
                "FROM symbols WHERE symbol LIKE ? OR ticker LIKE ? OR description LIKE ? LIMIT 300",
                (like, like, like),
            )
        except Exception:
            rows = []
        known = {r["symbol"] for r in rows}
        extra = [self.default_info(s) for s in dict.fromkeys([*config.DEFAULT_WATCHLIST, *sorted(S.alias_targets())])
                 if s not in known]
        candidates = [*rows, *extra]
        if market and market != "global":
            candidates = [c for c in candidates if (c.get("market") or self._market_of(c["symbol"])) == market]
        return S.rank_results(query, candidates, boost=set(config.DEFAULT_WATCHLIST))

    async def _store_search_meta(self, rows: list[dict]) -> None:
        """Keep metadata of the top search hits so resolve() also works offline later."""
        for row in rows:
            if not row.get("description") or row["description"] == row.get("ticker"):
                continue  # nothing worth storing (inferred metadata only)
            try:
                current = await self._load_info(row["symbol"])
                description = str(current.get("description") or "")
                if current.get("type") and description and description != str(current.get("ticker") or ""):
                    continue  # already described
                meta = {k: row.get(k) for k in INFO_FIELDS if row.get(k) not in (None, "")}
                await self._remember(row["symbol"], row["provider"], row["history"], meta=meta)
            except Exception:
                continue

    # ================================================================== quotes & prices

    async def quotes(self, symbols: list[str] | str) -> dict[str, dict]:
        """Quote by symbol. Source priority (optional fields `source`, `realtime`, `delay_s`, `market_open`): SIM
        from the synthetic set; the exchange's live price (crypto followed live); a fresh real-time quote of an
        API-key provider (app.data.realtime); TradingView (with the delay it announces); the local live feed / DB
        cache (`stale` when older than 2 min)."""
        if isinstance(symbols, str):
            symbols = symbols.split(",")
        wanted: list[str] = []
        for s in symbols or []:
            try:
                sym = S.clean_symbol(str(s))
            except ValueError:
                continue
            if sym not in wanted:
                wanted.append(sym)
        wanted = wanted[:300]
        out: dict[str, dict] = {}
        real: list[str] = []
        for sym in wanted:
            if S.is_sim(sym):
                q = self._sim_quote(sym)
                if q is not None:
                    out[sym] = q
            else:
                real.append(sym)
        if real:
            tvq = await self._tv_quotes(real)
            for sym in real:
                q = self._best_quote(sym, tvq.get(sym))
                if q is not None:
                    out[sym] = q
        for sym in real:
            if sym not in out:
                q = await self._local_quote(sym)
                if q is not None:
                    out[sym] = q
        return out

    def _best_quote(self, symbol: str, tv: dict | None) -> dict | None:
        """Exchange live price (crypto) > fresh keyed real-time quote > TradingView quote (None: local cache)."""
        meta = self._price_meta.get(symbol)
        live = self._prices.get(symbol)
        if meta and live and meta.get("kind") == "exchange" and time.monotonic() - live[1] <= LIVE_PRICE_TTL_S:
            q = {"symbol": symbol, **{k: None for k in QUOTE_FIELDS}, **(tv or {})}
            price = float(live[0])
            ref = safe_float(q.get("price"))
            change = safe_float(q.get("change"))
            if ref is not None and change is not None and ref - change:  # previous close from TradingView's change
                prev = ref - change
                q["change"], q["change_pct"] = price - prev, (price - prev) / prev * 100.0
            q.update(price=price, ts=int(meta.get("at") or now_ms()), source=meta.get("source"), realtime=True)
            q.pop("delay_s", None)
            q.pop("stale", None)
            q.pop("market_open", None)
            return normalize_quote(symbol, q)
        keyed = self.realtime.quote(symbol)
        if keyed is not None:
            return normalize_quote(symbol, keyed)
        q = normalize_quote(symbol, tv) if tv else None
        if q is not None and "market_open" not in q:
            # TradingView does not say when a market is closed: outside the regular session (US stocks, forex; holidays
            # not known) the quote is the last price of a closed market, not a live (or 15-min-old) one
            group = market_group(symbol, self.route(symbol).asset_class)
            if session_open(group, float(now_s())) is False:
                q["market_open"] = False
        return q

    async def _tv_quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols or not self.tv_online():
            return {}
        try:
            got = await self.tv().quotes(list(symbols)) or {}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once("quotes", f"Cotizaciones de TradingView no disponibles: {exc}", "tradingview")
            return {}
        out: dict[str, dict] = {}
        for sym in symbols:
            raw = got.get(sym) if isinstance(got, dict) else None
            q = normalize_quote(sym, {**raw, "source": "tradingview"} if isinstance(raw, dict) else None)
            if q is not None:
                out[sym] = q
        return out

    def _sim_reference(self, symbol: str) -> CandleSet:
        """The SIM set that best represents `symbol` now (simulator-fed first, most recent bar)."""
        live = [cs for key, cs in self._sim.items() if key[0] == symbol and key in self._sim_live and len(cs)]
        sets = live or [cs for key, cs in self._sim.items() if key[0] == symbol and len(cs)]
        if sets:
            return max(sets, key=lambda c: int(c.time[-1]) + tf_seconds(c.tf))
        return self._sim_candles(symbol, "1h", SIM_MIN_BARS)

    def _sim_quote(self, symbol: str) -> dict | None:
        cs = self._sim_reference(symbol)
        if not len(cs):
            return None
        last = float(cs.close[-1])
        ref_idx = int(np.searchsorted(cs.time, int(cs.time[-1]) - 86400, side="right")) - 1
        if ref_idx < 0 or ref_idx >= len(cs) - 1:
            ref_idx = max(len(cs) - 2, 0)
        window = slice(ref_idx + 1, len(cs)) if len(cs) > 1 else slice(0, 1)
        ref = float(cs.close[ref_idx])
        change = last - ref
        return {
            "symbol": symbol,
            "price": last,
            "change_pct": (change / ref * 100.0) if ref else None,
            "change": change,
            "volume": float(cs.volume[window].sum()),
            "high": float(cs.high[window].max()),
            "low": float(cs.low[window].min()),
            "open": float(cs.open[window][0]),
            "ts": now_ms(),
        }

    async def _local_quote(self, symbol: str) -> dict | None:
        """Quote from the live feed / DB cache (fallback when TradingView has none; change from the last two daily
        bars). `ts` is the real time of the price (live update, or the end of the final cached bar it comes from;
        for the newest stored bar of a timeframe, maybe stored while still forming, only its open is certain) and
        prices older than QUOTE_STALE_S carry `stale: True`: they must never be used as live prices."""
        try:
            recent = await self.rt.db.fetchall(
                "SELECT tf, time, close FROM candles WHERE symbol = ? ORDER BY time DESC LIMIT 8", (symbol,)
            )
            days = await self.rt.db.fetchall(
                "SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = '1d' "
                "ORDER BY time DESC LIMIT 2",
                (symbol,),
            )
        except Exception:
            recent, days = [], []
        now = now_s()
        best: tuple[float, float] | None = None  # (time of the price, price)
        live = self._prices.get(symbol)
        if live is not None:
            best = (now - max(0.0, time.monotonic() - live[1]), live[0])
        newest_seen: set[str] = set()
        for r in recent:  # newest first
            t, close = safe_float(r.get("time")), safe_float(r.get("close"))
            tf = str(r.get("tf") or "")
            try:
                tf_s = tf_seconds(tf)
            except (KeyError, ValueError):
                continue
            if t is None or close is None:
                continue
            # the newest stored bar of a timeframe may have been stored while it was still forming (get_candles
            # stores the forming bar): its close is only known to be from after its open. An older bar of that
            # timeframe is final, its close is the price at its end.
            at = t + tf_s if tf in newest_seen and t + tf_s <= now else t
            newest_seen.add(tf)
            if best is None or at > best[0]:
                best = (at, close)
        if best is None:
            return None
        at, price = min(float(now), best[0]), best[1]
        q = {"symbol": symbol, "price": price, "change_pct": None, "change": None, "volume": None, "high": None,
             "low": None, "open": None, "ts": int(at) * 1000, "source": "cache"}
        if now - at > QUOTE_STALE_S:
            q["stale"] = True
        if days:
            today = days[0]
            q.update(open=safe_float(today.get("open")), high=safe_float(today.get("high")),
                     low=safe_float(today.get("low")), volume=safe_float(today.get("volume")))
            prev = safe_float(days[1].get("close")) if len(days) > 1 else None
            if prev:
                q["change"] = price - prev
                q["change_pct"] = (price - prev) / prev * 100.0
        return q

    async def last_price(self, symbol: str, *, allow_stale: bool = True) -> float | None:
        """Most recent price: SIM set, live feed, real-time keyed quote, TradingView quote, exchange ticker, then the
        DB cache. The DB cache may be old (see _local_quote): with allow_stale=False such a price gives None
        instead."""
        symbol = self._clean(symbol)
        if S.is_sim(symbol):
            cs = self._sim_reference(symbol)
            return float(cs.close[-1]) if len(cs) else None
        live = self._prices.get(symbol)
        if live and time.monotonic() - live[1] <= LIVE_PRICE_TTL_S:
            return live[0]
        keyed = self.realtime.quote(symbol)
        if keyed and keyed.get("realtime") and (keyed.get("price") or 0) > 0:
            return float(keyed["price"])
        q = (await self._tv_quotes([symbol])).get(symbol)
        if q and (q.get("price") or 0) > 0 and not q.get("stale"):
            return float(q["price"])
        if not config.OFFLINE:
            price = await self._exchange_price(symbol)
            if price:
                return price
        q = await self._local_quote(symbol)
        if q is None or (q.get("stale") and not allow_stale):
            return None
        return q["price"]

    async def _exchange_price(self, symbol: str) -> float | None:
        """Close of the newest 1m bar of the exchange (None when it is not recent: venue stopped trading)."""
        await self._load_info(symbol)
        route = self.route(symbol)
        if route.kind != "crypto" or not route.venues or self._crypto_down.get(symbol) is not None:
            return None
        venue = self._known_venue(symbol, route)
        try:
            providers = self._providers()
            raw = await providers.fetch_latest(venue.provider, self._native(providers, venue), "1m", client=self._http(),
                                               retries=1, timeout=LIVE_HTTP_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once(f"latest:{symbol}", f"{symbol}: precio de {provider_label(venue.provider)} no disponible ({exc})")
            return None
        rows = clean_rows(raw)
        if not rows or now_s() - int(rows[-1]["time"]) > EXCHANGE_PRICE_MAX_AGE_S:
            return None
        return rows[-1]["close"]

    # ================================================================== universe & scans

    async def universe(
        self, market: str, offset: int = 0, limit: int = 100, sort: str = "volume", order: str = "desc",
        q: str | None = None, type_: str | None = None,
    ) -> dict:
        """UniverseResponse: every asset of a TradingView market (local `symbols` table when offline)."""
        market = str(market or "crypto").strip().lower()
        if not S.valid_market(market):
            raise ProviderError(f"Mercado no válido: {market}")
        offset = self._int_arg(offset or 0, 0, 0, 10**9, "offset")
        limit = self._int_arg(limit or 100, 100, 1, 500, "limit")
        order = "asc" if str(order or "").lower() == "asc" else "desc"
        query = " ".join(str(q or "").split())[:64] or None
        kind = str(type_ or "").strip().lower() or None
        if self.tv_online():
            try:
                data = await self.tv().universe(
                    market, offset=offset, limit=limit, sort=sort or "volume", order=order, q=query, type_=kind
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn_once(f"universe:{market}", f"Universo de TradingView ({market}) no disponible: {exc}", "tradingview")
                raise ProviderError(f"TradingView no respondió: {exc}") from None
            raw = [r for r in (data or {}).get("rows") or [] if isinstance(r, dict) and r.get("symbol")]
            total = int(safe_float((data or {}).get("total"), 0) or 0)
        else:
            total, raw = await self._local_universe(market, offset, limit, query, kind)
        rows: list[dict] = []
        raws: list[dict] = []
        for item in raw:
            row = self._result_row(item)
            if row is None:
                continue
            row.pop("price", None)
            row.pop("change_pct", None)
            for k in UNIVERSE_NUMBERS:
                row[k] = safe_float(item.get(k))
            row["quick_score"] = None
            row["quick_prob"] = None
            rows.append(row)
            raws.append(item)
        self._score_universe(raws, rows, market)
        return {"total": max(total, offset + len(rows)), "rows": rows}

    def _score_universe(self, raws: list[dict], rows: list[dict], market: str) -> None:
        """quick_score / quick_prob from the scanner's in-memory model (when the scanner is loaded)."""
        scanner = self.rt.scanner
        fn = getattr(scanner, "quick_score_rows", None) if scanner is not None else None
        if not callable(fn) or not rows:
            return
        views: list[dict] = []
        for raw, row in zip(raws, rows):
            view = dict(raw)
            for key, column in UNIVERSE_TV_COLUMNS.items():
                if view.get(column) is None and row.get(key) is not None:
                    view[column] = row[key]
            views.append(view)
        try:
            try:
                scores = fn(views, market=market)
            except TypeError:
                scores = fn(views)
        except Exception as exc:
            self._warn_once("quick-score", f"Puntuación rápida del universo no disponible: {exc}")
            return
        for row, sc in zip(rows, list(scores or [])):
            if isinstance(sc, (tuple, list)) and len(sc) >= 2:
                row["quick_score"] = safe_float(sc[0])
                row["quick_prob"] = safe_float(sc[1])

    async def _local_universe(
        self, market: str, offset: int, limit: int, q: str | None, kind: str | None
    ) -> tuple[int, list[dict]]:
        clauses: list[str] = []
        params: list[Any] = []
        if market != "global":
            clauses.append("market = ?")
            params.append(market)
        if q:
            clauses.append("(symbol LIKE ? OR description LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if kind:
            clauses.append("type = ?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = await self.rt.db.fetchval(f"SELECT COUNT(*) FROM symbols{where}", tuple(params), default=0)
        rows = await self.rt.db.fetchall(
            "SELECT symbol, ticker, exchange, description, type, market, currency, pricescale, logoid "
            f"FROM symbols{where} ORDER BY symbol LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        )
        return int(total or 0), rows

    async def tv_scan(
        self, market: str, columns: list[str], *, filters: list[dict] | None = None, sort_by: str | None = None,
        order: str = "desc", limit: int = 300, offset: int = 0, tickers: list[str] | None = None,
    ) -> dict:
        """Raw TradingView scan: {"total", "rows": [{"symbol", <column>: value}]} (empty when offline)."""
        if not self.tv_online():
            return {"total": 0, "rows": []}
        try:
            data = await self.tv().scan(
                market, list(columns), filters=filters, sort_by=sort_by, order=order, limit=limit, offset=offset,
                tickers=tickers,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once(f"scan:{market}", f"Escaneo de TradingView ({market}) falló: {exc}", "tradingview")
            raise
        return data if isinstance(data, dict) else {"total": 0, "rows": []}

    async def market_counts(self) -> dict[str, int | None]:
        if not self.tv_online():
            return {}
        try:
            counts = await self.tv().market_counts() or {}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once("market-counts", f"Recuento de mercados no disponible: {exc}", "tradingview")
            return {}
        out: dict[str, int | None] = {}
        for k, v in (counts.items() if isinstance(counts, dict) else []):
            n = safe_float(v)
            out[str(k)] = int(n) if n is not None else None
        return out

    async def markets(self) -> list[dict]:
        """MarketInfo list with Spanish names (counts are null offline)."""
        counts = await self.market_counts()
        return [{"id": mid, "name": name, "count": counts.get(mid)} for mid, name in S.MARKETS]

    # ================================================================== live feed

    def subscribe(self, symbol: str, tf: str, owner: str) -> None:
        """Ref-counted live subscription (idempotent per owner). Raises ProviderError for invalid input."""
        key = (self._clean(symbol), self._tf(tf))
        self._subs.setdefault(key, set()).add(str(owner or "anon"))
        self._next_poll.setdefault(key, 0.0)
        self._ensure_feed()

    def unsubscribe(self, symbol: str, tf: str, owner: str) -> None:
        try:
            key = (self._clean(symbol), self._tf(tf))
        except ProviderError:
            return
        owners = self._subs.get(key)
        if owners is None:
            return
        owners.discard(str(owner or "anon"))
        if not owners:
            self._subs.pop(key, None)
            self._next_poll.pop(key, None)
            self._last.pop(key, None)
            self._closed_at.pop(key, None)
            self._live_meta.pop(key, None)

    def subscriptions(self) -> list[dict]:
        return [{"symbol": s, "tf": tf, "owners": sorted(o)} for (s, tf), o in sorted(self._subs.items())]

    def subscribed_symbols(self) -> list[str]:
        return list(dict.fromkeys(s for s, _ in self._subs))

    def add_listener(self, cb: Listener) -> None:
        """async cb(symbol, tf, candle, closed) is awaited on every live update."""
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb: Listener) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _ensure_feed(self) -> None:
        if not self._running or not config.BACKGROUND or config.OFFLINE:
            return
        if self._feed_task is not None and not self._feed_task.done():
            return
        try:
            self._feed_task = asyncio.get_running_loop().create_task(self._feed_loop(), name="hub-feed")
        except RuntimeError:
            pass  # no running loop (called from a thread); the next subscribe inside the loop starts it

    async def push_candle(self, symbol: str, tf: str, candle: dict, closed: bool = True) -> None:
        """External bar feed (the simulator feeds SIM:* bars): updates the SIM set / cache, then publishes."""
        symbol = self._clean(symbol)
        tf = self._tf(tf)
        c = normalize_candle(candle)
        if c is None:
            raise ProviderError("Vela no válida: se requieren time, open, high, low y close numéricos")
        if S.is_sim(symbol):
            self._sim_push(symbol, tf, c)
        elif closed:
            await self._upsert(symbol, tf, [c], "push")
        await self._dispatch(symbol, tf, c, bool(closed))

    def _sim_push(self, symbol: str, tf: str, c: dict) -> None:
        """The simulator is authoritative for a SIM series: its first bar (or a rewind) starts a fresh series."""
        key = (symbol, tf)
        cs = self._sim.get(key)
        if cs is None or key not in self._sim_live or not len(cs) or c["time"] < int(cs.time[-1]):
            cs = CandleSet.from_rows([c], symbol, tf, provider="synthetic", history="synthetic")
            self._sim_live.add(key)
        else:
            cs = cs.append(c)
            if len(cs) > SIM_MAX_BARS:
                cs = cs.tail(SIM_MAX_BARS)
        self._sim[key] = cs

    async def _dispatch(self, symbol: str, tf: str, candle: dict, closed: bool, meta: dict | None = None) -> None:
        """WS `market:SYMBOL:tf` message + every listener (errors isolated per listener). `meta` = where the live
        price comes from ({source, kind: exchange | keyed | tradingview, realtime, delay_s, at}); the WS message
        carries the optional `source`, `realtime` and `delay_s` of it (types-realtime.ts)."""
        self._prices[symbol] = (float(candle["close"]), time.monotonic())
        if meta:
            self._price_meta[symbol] = dict(meta)
        else:
            self._price_meta.pop(symbol, None)
        self.feed_updates += 1
        ws = self.rt.ws
        if ws is not None:
            message = {"type": "candle", "symbol": symbol, "tf": tf, "candle": dict(candle), "closed": bool(closed)}
            if meta and meta.get("source"):
                message["source"] = meta["source"]
                if isinstance(meta.get("realtime"), bool):
                    message["realtime"] = meta["realtime"]
                if meta.get("delay_s"):
                    message["delay_s"] = int(meta["delay_s"])
            try:
                ws.publish(market_topic(symbol, tf), message)
            except Exception as exc:
                self._warn_once("ws-publish", f"No se pudo publicar la vela de {symbol} {tf}: {exc}")
        for cb in list(self._listeners):
            try:
                await cb(symbol, tf, dict(candle), bool(closed))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                name = getattr(cb, "__qualname__", None) or repr(cb)
                self._warn_once(f"listener:{name}", f"Error en un oyente del feed en vivo ({name}): {exc}")

    async def _feed_loop(self) -> None:
        while True:
            try:
                if self._subs and not config.OFFLINE:
                    await self._feed_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn_once("feed", f"Feed en vivo: error ({type(exc).__name__}: {exc})")
            await asyncio.sleep(FEED_TICK_S)

    async def _feed_tick(self) -> list[asyncio.Future]:
        """Start the polls that are due: one task per crypto (symbol, tf), one task for every TradingView
        snapshot and one per API-key provider for the real-time quotes (app.data.realtime), so a slow venue only
        delays its own keys. Keys whose previous poll is still running are skipped. Keyed symbols served by
        real-time quotes skip the TradingView snapshots (their last bar is closed when its bucket ends). Returns
        the tasks started."""
        now = time.monotonic()
        started: list[asyncio.Future] = []
        snaps: list[tuple[str, str]] = []
        snap_busy = self._snap_job is not None and not self._snap_job.done()
        selected = self.realtime.select()
        # real-time polls first: a symbol is served by them (TradingView skipped) only while a fresh quote exists or a
        # poll of it is in flight; when the plan's limits leave nothing for it, TradingView keeps its chart live
        for provider, symbols in self.realtime.due():
            task = asyncio.ensure_future(self._poll_realtime(provider, symbols))
            self.realtime.track(provider, task)
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            started.append(task)
        for key in list(self._subs):
            symbol, tf = key
            if S.is_sim(symbol) or now < self._next_poll.get(key, 0.0):
                continue  # SIM bars arrive through push_candle
            job = self._jobs.get(key)
            if job is not None and not job.done():
                continue  # the previous poll of this key is still running
            await self._load_info(symbol)
            history = self.history_kind(symbol)[1]
            if history == "keyed" and symbol in selected and self.realtime.serves(symbol):
                self._next_poll[key] = now + FEED_TICK_S
                await self._close_if_over(symbol, tf, now_s(), "keyed", tf)
                continue
            if history == "full" and self._feed_tv.get(symbol) is None:
                self._next_poll[key] = now + CRYPTO_POLL_S
                task = asyncio.ensure_future(self._poll_key(symbol, tf))
                self._jobs[key] = task
                task.add_done_callback(functools.partial(self._job_done, key))
                started.append(task)
            elif history in ("full", "recorder", "keyed") and not snap_busy:
                snaps.append(key)
                self._next_poll[key] = now + SNAPSHOT_POLL_S
        if snaps:
            self._snap_job = asyncio.ensure_future(self._poll_snapshots(snaps))
            started.append(self._snap_job)
        return started

    async def _poll_realtime(self, provider: str, symbols: list[str]) -> None:
        """One real-time quote request; every fresh quote updates the live bars of its subscribed timeframes."""
        got = await self.realtime.poll(provider, symbols)
        for symbol, quote in got.items():
            try:
                await self._apply_rt_quote(symbol, quote)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn_once("feed-rt", f"Feed en vivo (tiempo real): error procesando {symbol} "
                                           f"({type(exc).__name__}: {exc})")

    async def _apply_rt_quote(self, symbol: str, q: dict) -> None:
        """Live bars from a real-time keyed quote (published only: the provider bars are downloaded at the bar
        close). A closed market or a delayed price never opens a bar."""
        if q.get("realtime") is not True or q.get("market_open") is False:
            return
        price = safe_float(q.get("price"))
        if price is None or price <= 0:
            return
        at = int(now_s())
        day = at // 86400
        volume = safe_float(q.get("volume"))
        seen = self._rt_vol.get(symbol)
        dv = max(0.0, volume - seen[1]) if volume is not None and seen and seen[0] == day else 0.0
        if volume is not None:
            self._rt_vol[symbol] = (day, volume)
        meta = {"source": q.get("source"), "kind": "keyed", "realtime": True, "at": int(q.get("ts") or now_ms())}
        for key in [k for k in self._subs if k[0] == symbol]:
            bar = await self._rt_bar(symbol, key[1], q, float(price), dv, at)
            if bar is not None:
                await self._apply_live(symbol, key[1], [bar], "keyed", store=False, meta=meta)

    async def _rt_bar(self, symbol: str, tf: str, q: dict, price: float, dv: float, at: int) -> dict | None:
        """Forming bar of (symbol, tf) in the alignment of the provider: the bar already shown, or the stored
        provider bar of that bucket, widened with the price (volume grows with the day volume of the quotes). The
        daily bar takes the day values of the quote."""
        if tf == "1d":
            o, h, lo = safe_float(q.get("open")), safe_float(q.get("high")), safe_float(q.get("low"))
            return {"time": floor_time(at, "1d"), "open": o or price, "high": max(h or price, price),
                    "low": min(lo or price, price), "close": price, "volume": safe_float(q.get("volume"), 0.0) or 0.0}
        bucket = await self._keyed_stamp(symbol, tf, at, floor_time(at, tf))
        prev = self._last.get((symbol, tf))
        base = prev if prev is not None and int(prev["time"]) == bucket else await self._keyed_bar_at(symbol, tf, bucket)
        if base is None:
            return {"time": bucket, "open": price, "high": price, "low": price, "close": price, "volume": dv}
        return {"time": bucket, "open": float(base["open"]), "high": max(float(base["high"]), price),
                "low": min(float(base["low"]), price), "close": price, "volume": float(base.get("volume") or 0.0) + dv}

    async def _keyed_bar_at(self, symbol: str, tf: str, bucket: int) -> dict | None:
        try:
            row = await self.rt.db.fetchone(
                "SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = ? AND time = ? "
                "AND source <> 'recorder'",
                (symbol, tf, int(bucket)),
            )
        except Exception:
            return None
        return normalize_candle(row) if row else None

    def _job_done(self, key: tuple[str, str], task: asyncio.Future) -> None:
        if self._jobs.get(key) is task:
            self._jobs.pop(key, None)
        if not task.cancelled():
            task.exception()  # mark retrieved (errors are reported inside the job)

    async def _poll_crypto(self, keys: list[tuple[str, str]]) -> None:
        """Poll these crypto keys now and wait for all of them (the feed loop starts one task per key instead)."""
        await asyncio.gather(*(self._poll_key(s, tf) for s, tf in keys))

    async def _poll_key(self, symbol: str, tf: str) -> None:
        """Live poll of one crypto key: the last 2 bars of its exchange (short timeout, no retries: the next poll
        comes a few seconds later)."""
        try:
            providers = self._providers()
        except Exception as exc:
            self._warn_once("feed-providers", f"Feed en vivo: proveedores no disponibles ({exc})", every_s=600)
            return
        route = self.route(symbol)
        if not route.venues:
            return
        venue = self._known_venue(symbol, route)
        try:
            raw = await asyncio.wait_for(
                providers.fetch_latest(venue.provider, self._native(providers, venue), tf, client=self._http(),
                                       retries=0, timeout=LIVE_HTTP_TIMEOUT_S),
                LIVE_POLL_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            label = provider_label(venue.provider)
            detail = str(exc) or "tiempo de espera agotado"
            self._warn_once(f"feed:{venue.provider}:{symbol}", f"Feed {symbol} {tf}: error de {label} → {detail}")
            failures = self._feed_failures.get(symbol, 0) + 1
            self._feed_failures[symbol] = failures
            if failures >= FEED_DOWN_AFTER:  # live updates from TradingView for a while; candles stay on exchanges
                self._feed_failures.pop(symbol, None)
                self._feed_tv.set(symbol, f"{label}: {detail}")
                self._emit("info", f"{symbol}: {label} no responde; el feed en vivo usa TradingView temporalmente")
            return
        self._feed_failures.pop(symbol, None)
        self.feed_polls += 1
        meta = {"source": venue.provider, "kind": "exchange", "realtime": True, "at": now_ms()}
        try:
            await self._apply_live(symbol, tf, clean_rows(raw), venue.provider, venue=venue, meta=meta)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once("feed-apply", f"Feed en vivo: error procesando velas ({type(exc).__name__}: {exc})")

    async def _poll_snapshots(self, keys: list[tuple[str, str]]) -> None:
        """Recorder / keyed symbols, and crypto symbols whose exchange feed keeps failing: ONE TradingView
        current-bars request for every symbol and timeframe. Bars are in market time (TradingView's announced
        delay). A frozen bar (market closed: the same bar re-stamped) creates no new bar; the last live bar is
        emitted closed once its bucket is over. Recorder bars are stored; keyed and crypto snapshots are only
        published (their history comes from the provider / the exchange)."""
        tv = self.tv() if self.tv_online() else None
        if tv is None:
            return
        plan: list[tuple[str, str, str, str]] = []  # (symbol, tf, TradingView tf, history)
        for symbol, tf in keys:
            history = self.history_kind(symbol)[1]
            if history not in ("recorder", "keyed", "full"):
                continue
            direct = RECORDER_TFS if history == "recorder" else SNAPSHOT_TFS
            plan.append((symbol, tf, tf if tf in direct else RECORDER_BASE.get(tf, "1m"), history))
        if not plan:
            return
        symbols = list(dict.fromkeys(p[0] for p in plan))
        tfs = sorted({p[2] for p in plan}, key=tf_seconds)
        try:
            data = await tv.current_bars(symbols, tfs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once("feed-tv", f"Feed en vivo (TradingView): {exc}", "tradingview")
            return
        self.feed_polls += 1
        stamper = self.recorder.stamper
        # the recorded timeframes are placed once per (symbol, TradingView tf) by the stamper shared with the
        # recorder (content first, lag-corrected clock; coarse timeframes first: they decide 1m corrections)
        placed: dict[tuple[str, str], tuple[dict, Any, int] | None] = {}
        for symbol, tv_tf in sorted({(p[0], p[2]) for p in plan}, key=lambda k: (k[0], PLACE_RANK.get(k[1], 9))):
            bars = (data or {}).get(symbol) if isinstance(data, dict) else None
            raw = bars.get(tv_tf) if isinstance(bars, dict) else None
            c = normalize_candle(raw)
            if c is None:
                placed[(symbol, tv_tf)] = None
                continue
            c["time"] = floor_time(c["time"], tv_tf)
            asof = int(safe_float(raw.get("asof"), c["time"]))
            try:
                where = await stamper.place(symbol, tv_tf, c, asof) if tv_tf in RECORDER_TFS else None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn_once("feed-stamp", f"Feed en vivo: no se pudo situar la vela ({type(exc).__name__}: {exc})")
                where = None if tv_tf not in RECORDER_TFS else False
            placed[(symbol, tv_tf)] = None if where is False else (c, where, asof)
        frozen: dict[tuple[str, str], dict | None] = {}
        for symbol, tf, tv_tf, history in plan:
            item = placed.get((symbol, tv_tf))
            if item is None:
                continue
            base, where, asof = item
            c = dict(base)
            try:
                if where is None:  # timeframes TradingView aligns to sessions (1h, 4h, 1w): clock + frozen check
                    if (symbol, tv_tf) not in frozen:
                        frozen[(symbol, tv_tf)] = await self._snapshot_frozen(symbol, tv_tf, c, asof)
                    if frozen[(symbol, tv_tf)] is not None:
                        await self._close_if_over(symbol, tf, asof, history, tv_tf, frozen[(symbol, tv_tf)])
                        continue
                else:
                    if where.bucket is None or where.status == "old":
                        continue  # a rollover inside our bucket late in it, or a late copy of an earlier bar
                    c["time"] = where.bucket
                    market = asof - stamper.lag(symbol)  # market time corrected by the estimated lag
                    if where.status in ("repeat", "same") and market >= where.bucket + tf_seconds(tv_tf):
                        # still the last known bar although its bucket is over (market closed, no trades, data
                        # still finishing): no new bar; the last live bar is emitted closed once
                        await self._close_if_over(symbol, tf, int(market), history, tv_tf, c)
                        continue
                if tv_tf != tf:
                    c = await self._aggregate_current(symbol, tf, c)
                elif history == "keyed":
                    c["time"] = await self._keyed_stamp(symbol, tf, asof, c["time"])
                if c is None:
                    continue
                delay = max(0, now_s() - int(asof))
                delay = delay if delay >= 5 else 0  # a streaming row stamped a second earlier is not delayed
                meta = {"source": "tradingview", "kind": "tradingview", "realtime": delay == 0,
                        "delay_s": delay or None, "at": int(asof) * 1000}
                if history == "recorder":
                    await self._apply_live(symbol, tf, [c], "recorder", store=tf in RECORDER_TFS, meta=meta)
                else:
                    await self._apply_live(symbol, tf, [c], "keyed" if history == "keyed" else "snapshot",
                                           store=False, meta=meta)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn_once("feed-apply", f"Feed en vivo: error procesando velas ({type(exc).__name__}: {exc})")

    def _stamp_shifted(self, symbol: str, tv_tf: str, d: int) -> None:
        """The stamper moved the recorded bars of (symbol, tv_tf) by d seconds: live bars built on the old buckets
        are forgotten (the next snapshot starts the forming bar again at the right bucket)."""
        for key in [k for k in self._last if k[0] == symbol and (k[1] == tv_tf or RECORDER_BASE.get(k[1]) == tv_tf)]:
            self._last.pop(key, None)
            self._closed_at.pop(key, None)

    async def _snapshot_frozen(self, symbol: str, tv_tf: str, c: dict, asof: int) -> dict | None:
        """When a TradingView snapshot with a newer stamp is still the last known bar (market closed, no trades,
        or the previous bar finishing right after our bucket rolled) it must not open a new bar: returns that bar
        with its real open time (None for a genuinely new or updated bar)."""
        key = (symbol, tv_tf)
        seen = self._snap_seen.get(key)
        from_db = seen is None  # first snapshot since (re)start: compared with the stored bar
        if seen is None:
            seen = await self.recorder.last_stored(symbol, tv_tf)
            self._snap_seen[key] = seen
        sig = signature(c)
        if seen[1] is not None and c["time"] > seen[0]:
            tf_s = tf_seconds(tv_tf)
            early = asof - c["time"] < min(tf_s / 2.0, ROLLOVER_JITTER_S)
            if sig == seen[1] or (same_bar(seen[1], sig) and (early or from_db or c["time"] - seen[0] > tf_s)):
                # remember its final values: later re-stamped copies of this bar must keep matching (else, once
                # the "early" window is over, the finished bar would open a phantom bar after the close)
                self._snap_seen[key] = (int(seen[0]), sig)
                return {**c, "time": int(seen[0])}
        self._snap_seen[key] = (int(c["time"]), sig)
        return None

    async def _close_if_over(
        self, symbol: str, tf: str, asof: int, history: str, tv_tf: str, base: dict | None = None
    ) -> None:
        """Frozen snapshots: emit the last live bar closed (once) when its bucket has ended in market time, with
        the final values of the frozen TradingView bar `base` when it belongs to that bucket."""
        key = (symbol, tf)
        prev = self._last.get(key)
        if prev is None or key not in self._subs or self._closed_at.get(key) == prev["time"]:
            return
        if asof < int(prev["time"]) + tf_seconds(tf):
            return
        final = prev
        if base is not None and history != "keyed":
            if tv_tf == tf and base["time"] == prev["time"]:
                final = {**base, "open": prev["open"]}
            elif tv_tf != tf and floor_time(base["time"], tf) == prev["time"]:
                final = await self._aggregate_current(symbol, tf, dict(base)) or prev
        self._closed_at[key] = int(prev["time"])
        self._last[key] = final
        if history == "recorder" and tf in RECORDER_TFS:
            await self._store_closed(symbol, tf, [final], "recorder")
        elif history == "keyed":
            self._keyed_refresh(symbol, tf)
        await self._dispatch(symbol, tf, final, True, self._live_meta.get(key))

    async def _keyed_stamp(self, symbol: str, tf: str, asof: int, utc_time: int) -> int:
        """Open time of the forming bar in the keyed provider's alignment (US stocks: 1h bars at :30 UTC)."""
        tf_s = tf_seconds(tf)
        if tf_s >= 86400:
            return utc_time
        offset = self._keyed_offsets.get((symbol, tf))
        if offset is None:
            try:
                row = await self.rt.db.fetchone(
                    "SELECT time FROM candles WHERE symbol = ? AND tf = ? AND source <> 'recorder' "
                    "ORDER BY time DESC LIMIT 1",
                    (symbol, tf),
                )
            except Exception:
                row = None
            offset = int(row["time"]) % tf_s if row else 0
            self._keyed_offsets[(symbol, tf)] = offset
        return int((asof - offset) // tf_s * tf_s + offset) if offset else utc_time

    async def _aggregate_current(self, symbol: str, tf: str, base_c: dict | None) -> dict | None:
        """Forming bar of a non-recorded timeframe: recorded base bars of the bucket + the current base bar."""
        if base_c is None:
            return None
        base = RECORDER_BASE[tf]
        base_c["time"] = floor_time(base_c["time"], base)
        start = floor_time(base_c["time"], tf)
        rows = await self.rt.db.fetchall(
            "SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = ? AND time >= ? "
            "AND time < ? ORDER BY time",
            (symbol, base, start, base_c["time"]),
        )
        parts = [c for c in (normalize_candle(r) for r in rows) if c is not None] + [base_c]
        return {
            "time": start,
            "open": parts[0]["open"],
            "high": max(p["high"] for p in parts),
            "low": min(p["low"] for p in parts),
            "close": base_c["close"],
            "volume": sum(p["volume"] for p in parts),
        }

    async def _apply_live(
        self, symbol: str, tf: str, rows: list[dict], source: str, *, store: bool = True,
        venue: S.Venue | None = None, meta: dict | None = None,
    ) -> None:
        """Bar-close detection. A newer bar emits every bar closed since the previous update (oldest first; after a
        feed stall the missed bars are downloaded from the exchange) and then the forming bar. Exchange bars are
        stored only when they continue the cached series: a gap (restart, long pause) is downloaded by get_candles
        instead, so a partial bar is never stored as final. `source`: exchange provider, "recorder" (merged by the
        recorder), "keyed" / "snapshot" (TradingView snapshots of keyed / crypto series, published only)."""
        key = (symbol, tf)
        if not rows or key not in self._subs:
            return
        latest = rows[-1]
        prev = self._last.get(key)
        if prev is not None and latest["time"] < prev["time"]:
            return  # out-of-order answer
        if meta:
            self._live_meta[key] = dict(meta)
        meta = self._live_meta.get(key) if meta is None else meta
        exchange = source not in SNAPSHOT_SOURCES
        if prev is None:
            if exchange and store and len(rows) > 1:
                if await self._continues_cache(symbol, tf, int(rows[0]["time"])):
                    await self._store_closed(symbol, tf, rows[:-1], source)
                else:
                    self._schedule_gap_fill(symbol, tf)
        elif latest["time"] > prev["time"]:
            closed: list[dict] = []
            if exchange:
                closed = [r for r in rows[:-1] if r["time"] >= prev["time"]]
                if not closed or closed[0]["time"] != prev["time"]:  # bars were missed (stall, laptop sleep)
                    caught = await self._catch_up(symbol, tf, venue, int(prev["time"]))
                    if caught:
                        latest, closed = caught[-1], caught[:-1]
                    else:
                        closed = []
                        if store:
                            self._schedule_gap_fill(symbol, tf)
            elif self._closed_at.get(key) != prev["time"]:
                closed = [prev]  # snapshots: the last one of the bucket is the best known final bar
            if closed and store:
                await self._store_closed(symbol, tf, closed, source)
            if source == "keyed":
                self._keyed_refresh(symbol, tf)
            for bar in closed:
                await self._dispatch(symbol, tf, bar, True, meta)
        if prev is None or latest != prev:
            self._last[key] = latest
            await self._dispatch(symbol, tf, latest, False, meta)

    async def _continues_cache(self, symbol: str, tf: str, first_time: int) -> bool:
        """True when bars starting at `first_time` continue the cached series (or nothing is cached yet). The cache
        must already reach `first_time`: its newest bar may have been stored while it was still forming (app
        closed during that bar), and only a download from it (get_candles) replaces it with its final version."""
        try:
            last = await self.rt.db.fetchval(
                "SELECT MAX(time) FROM candles WHERE symbol = ? AND tf = ?", (symbol, tf)
            )
        except Exception:
            return False
        return last is None or int(last) >= int(first_time)

    async def _catch_up(self, symbol: str, tf: str, venue: S.Venue | None, since: int) -> list[dict] | None:
        """Bars from `since` (the last bar seen live) to the forming one, downloaded after a feed stall. None when
        the gap is too long or the download fails (get_candles fills the gap then)."""
        tf_s = tf_seconds(tf)
        n = (floor_time(now_s(), tf) - int(since)) // tf_s + 1
        if venue is None or n < 2 or n > MAX_CATCHUP_BARS + 1:
            return None
        try:
            providers = self._providers()
            raw = await asyncio.wait_for(
                providers.fetch_candles(venue.provider, self._native(providers, venue), tf, int(n) + 1,
                                        client=self._http()),
                CATCHUP_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once(f"catchup:{symbol}:{tf}",
                            f"Feed {symbol} {tf}: no se pudieron recuperar las velas perdidas "
                            f"({str(exc) or 'tiempo de espera agotado'})")
            return None
        rows = [r for r in clean_rows(raw) if r["time"] >= since]
        return rows if len(rows) >= 2 and rows[0]["time"] == since else None

    def _schedule_gap_fill(self, symbol: str, tf: str) -> None:
        """Download the bars missing between the cache and now in the background (get_candles fills gaps)."""
        key = (symbol, tf)
        job = self._gap_jobs.get(key)
        if job is not None and not job.done():
            return
        self._fresh.pop(key)
        if self._log.should_log(f"gap:{symbol}:{tf}", 600):
            self._emit("debug", f"Feed en vivo: faltan velas de {symbol} {tf} (pausa o reinicio); se descargan")
        self._gap_jobs[key] = asyncio.ensure_future(self._gap_fill(symbol, tf))

    async def _gap_fill(self, symbol: str, tf: str) -> None:
        try:
            await self.get_candles(symbol, tf, GAP_FILL_LIMIT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once(f"gap-fill:{symbol}:{tf}", f"No se pudo completar el historial de {symbol} {tf}: {exc}")

    async def _store_closed(self, symbol: str, tf: str, rows: list[dict], source: str) -> None:
        try:
            if source == "recorder":
                await self.recorder.store([(symbol, tf, c) for c in rows])
            else:
                await self._upsert(symbol, tf, rows, source)
        except Exception as exc:
            self._warn_once(f"store-live:{symbol}", f"No se pudieron guardar velas cerradas de {symbol} {tf}: {exc}")

    # ================================================================== watchlist

    async def _seed_watchlist(self) -> None:
        """First start: seed config.DEFAULT_WATCHLIST when the table is empty (only once, ever)."""
        db = self.rt.db
        if await db.fetchval("SELECT value FROM meta WHERE key = 'watchlist_seeded'"):
            return
        count = int(await db.fetchval("SELECT COUNT(*) FROM watchlist", default=0) or 0)
        if count == 0:
            ts = now_ms()
            await db.executemany(
                "INSERT INTO watchlist(symbol, position, added_at) VALUES (?, ?, ?) ON CONFLICT(symbol) DO NOTHING",
                [(s, i, ts) for i, s in enumerate(config.DEFAULT_WATCHLIST)],
            )
        await db.execute(
            "INSERT INTO meta(key, value) VALUES ('watchlist_seeded', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )

    async def watchlist(self) -> list[str]:
        rows = await self.rt.db.fetchall("SELECT symbol FROM watchlist ORDER BY position, added_at, symbol")
        return [str(r["symbol"]) for r in rows]

    async def watch_add(self, symbol: str) -> list[str]:
        symbol = self._clean(symbol)
        db = self.rt.db
        if not int(await db.fetchval("SELECT COUNT(*) FROM watchlist WHERE symbol = ?", (symbol,), default=0) or 0):
            pos = await db.fetchval("SELECT MAX(position) FROM watchlist", default=-1)
            await db.execute(
                "INSERT INTO watchlist(symbol, position, added_at) VALUES (?, ?, ?) ON CONFLICT(symbol) DO NOTHING",
                (symbol, int(pos) + 1, now_ms()),
            )
            self._emit("info", f"Añadido a la lista de seguimiento: {symbol}")
        return await self.watchlist()

    async def watch_remove(self, symbol: str) -> list[str]:
        symbol = self._clean(symbol)
        db = self.rt.db
        if int(await db.fetchval("SELECT COUNT(*) FROM watchlist WHERE symbol = ?", (symbol,), default=0) or 0):
            await db.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
            self._emit("info", f"Quitado de la lista de seguimiento: {symbol}")
        return await self.watchlist()

    async def watch_items(self) -> list[dict]:
        """WatchItem list (SymbolInfo + quote) in watchlist order."""
        symbols = await self.watchlist()
        try:
            quotes = await self.quotes(symbols)
        except Exception as exc:
            self._warn_once("watch-quotes", f"Cotizaciones de la lista de seguimiento no disponibles: {exc}")
            quotes = {}
        sem = asyncio.Semaphore(4)

        async def info(sym: str) -> dict:
            async with sem:
                try:
                    return await self.symbol_info(sym)
                except Exception:
                    return self.compose_info(sym)

        infos = await asyncio.gather(*(info(s) for s in symbols))
        return [{**inf, "quote": quotes.get(sym)} for sym, inf in zip(symbols, infos)]
