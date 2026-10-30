"""MarketDataHub (`rt.hub`): the single entry point for market data.

- Candles: `SIM:*` synthetic sets (fed live by the simulator through `push_candle`), crypto from official exchange
  APIs (`app.data.providers`), stocks / forex / indices / futures from API-key providers (`app.data.keyed`) or, when
  no key is configured, from the local history built by the recorder (TradingView current-bar snapshots).
- DB cache (`candles` table): incremental fetches, forming bar refreshed with a short TTL, concurrent identical
  requests deduplicated.
- Symbols: resolve / search / symbol_info (`symbols` table), quotes, universe and raw scans through TradingView.
- Live feed: ref-counted subscriptions, one poller task, bar-close detection, WS `market:SYMBOL:tf` + listeners.
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
from app.data.recorder import RECORDER_BASE, RECORDER_TFS, CandleRecorder, normalize_candle
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
FEED_CONCURRENCY = 4
FEED_DOWN_AFTER = 5  # consecutive live-feed failures before a crypto symbol falls back to TradingView
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
    return out


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
        elif key.startswith("providers."):
            self._remembered.clear()
            self._resolve_cache.clear()
            self._keyed_fresh.clear()
            self._crypto_down.clear()
            self._details_miss.clear()

    async def stop(self) -> None:
        self._running = False
        task, self._feed_task = self._feed_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.recorder.stop()
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
            venue = self._venue_ok.get(symbol) or route.venues[0]
            return venue.provider, "full"
        keyed = self.keyed_candidates(symbol, route.asset_class)
        if keyed:
            return keyed[0][0], "keyed"
        return "recorder", "recorder"

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
        except (TypeError, ValueError):
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
        cached = await self._read_cache(symbol, tf, limit)
        venue = self._venue_ok.get(symbol) or route.venues[0]
        if config.OFFLINE:
            note = (
                "Modo sin conexión: datos de la caché local" if cached
                else f"Modo sin conexión: no hay velas guardadas de {symbol} {tf}"
            )
            return self._build(cached, symbol, tf, venue.provider, "full", note)
        fkey = (symbol, tf)
        tf_s = tf_seconds(tf)
        now_bar = floor_time(now_s(), tf)
        reset = False
        if cached:
            gap = (now_bar - int(cached[-1]["time"])) // tf_s
            need = 0 if (gap <= 0 and self._fresh.get(fkey)) else max(int(gap), 0) + 1
            if need > max(2 * limit, 1000):  # cache far behind: refetch and drop the stale segment
                reset, need = True, limit
        else:
            need = limit
        error: str | None = None
        if need > 0:
            rows, used, error = await self._fetch_crypto(symbol, route, tf, min(max(need, 2), MAX_LIMIT))
            if rows and used is not None:
                if reset:
                    await self._delete_before(symbol, tf, rows[0]["time"])
                    self._history_start.pop(fkey, None)
                    cached = []
                await self._upsert(symbol, tf, rows, used.provider)
                self._fresh.set(fkey, True)
                if not cached and len(rows) < need:
                    self._history_start[fkey] = int(rows[0]["time"])  # the listing starts here
                cached = merge_candles(cached, rows)
                venue = used
        if need == 0:
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        if cached and len(cached) < limit and fkey not in self._history_start and error is None:
            cached = await self._backfill(symbol, tf, limit, route, venue, cached)
        if not cached:
            reason = error or f"ningún exchange devolvió velas de {symbol}"
            self._crypto_down.set(symbol, reason)
            self._emit("info", f"{symbol}: sin datos en los exchanges ({reason}); se usa el historial local")
            return await self._other_candles(symbol, tf, limit, route, reason=reason)
        note = None
        if not (route.native and route.venues and venue == route.venues[0]):
            note = f"Datos de {venue.tv_symbol} vía {provider_label(venue.provider)} (par equivalente)"
        if error:
            note = f"No se pudo actualizar ({error}); se muestran los datos guardados"
        return self._build(cached[-limit:], symbol, tf, venue.provider, "full", note)

    async def _backfill(
        self, symbol: str, tf: str, limit: int, route: S.Route, venue: S.Venue, cached: list[dict]
    ) -> list[dict]:
        """Fetch bars older than the cache until `limit` bars are available (or the listing start is reached)."""
        oldest = int(cached[0]["time"])
        want = limit - len(cached) + 1
        rows, used, error = await self._fetch_crypto(symbol, route, tf, min(want, MAX_LIMIT), end_time=oldest, only=venue)
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
    ) -> tuple[list[dict], S.Venue | None, str | None]:
        """(rows, venue that served them, error). Tries the working venue first, then equivalent pairs."""
        try:
            providers = self._providers()
        except Exception as exc:
            return [], None, f"módulo de proveedores no disponible ({type(exc).__name__}: {exc})"
        if only is not None:
            venues = [only]
        else:
            first = self._venue_ok.get(symbol)
            venues = list(dict.fromkeys([v for v in (first, *route.venues) if v is not None]))[:MAX_VENUES]
        last_error: str | None = None
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
                self._warn_once(f"fetch:{venue.provider}:{symbol}:{tf}", f"{symbol} {tf}: error de {label} → {exc}")
                continue
            self.fetches += 1
            rows = clean_rows(raw)
            if rows:
                if self._venue_ok.get(symbol) != venue:
                    if route.venues and venue != route.venues[0]:
                        self._emit("info", f"{symbol}: usando {venue.tv_symbol} ({label}) como fuente de velas")
                    self._venue_ok[symbol] = venue
                return rows, venue, None
            if end_time is not None:
                return [], venue, None  # no older history on this venue
            last_error = last_error or f"{label}: sin velas para {venue.tv_symbol}"
        return [], None, last_error

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
        cached = await self._read_cache(symbol, tf, limit)
        fkey = (symbol, tf, provider)
        enough = len(cached) >= min(limit, self._keyed_avail.get(fkey, limit))
        if cached and enough and self._keyed_fresh.get(fkey):
            self.cache_hits += 1
            return self._build(cached, symbol, tf, provider, "keyed", None)
        want = limit
        if cached and enough:
            gap = (floor_time(now_s(), tf) - int(cached[-1]["time"])) // tf_seconds(tf)
            if 0 <= gap < limit:
                want = int(max(gap + 2, 2))  # incremental refresh
        try:
            raw = await self._keyed_module().fetch_keyed_candles(
                provider, symbol, route.asset_class, tf, want, api_key=api_key, client=self._http()
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fetch_errors += 1
            errors.append(f"{label}: {exc}")
            self._warn_once(f"keyed:{provider}:{symbol}:{tf}", f"{symbol} {tf}: error de {label} → {exc}")
            if cached:
                note = f"No se pudo actualizar desde {label} ({exc}); se muestran los datos guardados"
                return self._build(cached, symbol, tf, provider, "keyed", note)
            return None
        self.fetches += 1
        self.cache_misses += 1
        rows = clean_rows(raw)
        if not rows:
            errors.append(f"{label}: sin velas para {symbol} {tf}")
            if cached:
                return self._build(cached, symbol, tf, provider, "keyed", f"{label} no devolvió velas nuevas")
            return None
        await self._upsert(symbol, tf, rows, provider)
        self._keyed_fresh.set(fkey, True, ttl=keyed_ttl(tf))
        if want >= limit:
            self._keyed_avail[fkey] = len(rows)
        return self._build(merge_candles(cached, rows)[-limit:], symbol, tf, provider, "keyed", None)

    async def _recorder_candles(
        self, symbol: str, tf: str, limit: int, route: S.Route, errors: list[str], reason: str | None
    ) -> CandleSet:
        """Local history (recorder snapshots or anything cached before); missing timeframes are resampled."""
        rows = await self._read_cache(symbol, tf, limit)
        base = RECORDER_BASE.get(tf)
        if base and len(rows) < limit:
            ratio = max(1, tf_seconds(tf) // tf_seconds(base))
            base_rows = await self._read_cache(symbol, base, min(limit * ratio + ratio, 50_000))
            if base_rows:
                resampled = resample(CandleSet.from_rows(base_rows, symbol, base), tf).to_list()
                first = int(base_rows[0]["time"])
                if resampled and floor_time(first, tf) != first:
                    resampled = resampled[1:]  # the first bucket is incomplete
                rows = merge_candles(resampled, rows)[-limit:]
        note = self._recorder_note(symbol, tf, route, len(rows), errors, reason)
        return self._build(rows, symbol, tf, "recorder", "recorder", note)

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

    async def _read_cache(self, symbol: str, tf: str, limit: int) -> list[dict]:
        rows = await self.rt.db.fetchall(
            "SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = ? "
            "ORDER BY time DESC LIMIT ?",
            (symbol, tf, int(limit)),
        )
        return [c for c in (normalize_candle(r) for r in reversed(rows)) if c is not None]

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
    def _build(rows: list[dict], symbol: str, tf: str, provider: str, history: str, note: str | None) -> CandleSet:
        return CandleSet.from_rows(rows, symbol, tf, provider=provider, history=history, note=note)

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
        limit = int(clamp(int(limit or 30), 1, 100))
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
        """Quote by symbol: SIM from the synthetic set, others from TradingView, then live feed / DB cache."""
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
            out.update(await self._tv_quotes(real))
        for sym in real:
            if sym not in out:
                q = await self._local_quote(sym)
                if q is not None:
                    out[sym] = q
        return out

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
            q = normalize_quote(sym, got.get(sym) if isinstance(got, dict) else None)
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
        """Quote from the live feed / DB cache (offline fallback; change from the last two daily bars)."""
        try:
            last = await self.rt.db.fetchone(
                "SELECT time, close FROM candles WHERE symbol = ? ORDER BY time DESC LIMIT 1", (symbol,)
            )
            days = await self.rt.db.fetchall(
                "SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = '1d' "
                "ORDER BY time DESC LIMIT 2",
                (symbol,),
            )
        except Exception:
            last, days = None, []
        live = self._prices.get(symbol)
        price = live[0] if live and time.monotonic() - live[1] <= 4 * LIVE_PRICE_TTL_S else None
        if price is None and last:
            price = safe_float(last.get("close"))
        if price is None:
            return None
        q = {"symbol": symbol, "price": price, "change_pct": None, "change": None, "volume": None, "high": None,
             "low": None, "open": None, "ts": now_ms()}
        if days:
            today = days[0]
            q.update(open=safe_float(today.get("open")), high=safe_float(today.get("high")),
                     low=safe_float(today.get("low")), volume=safe_float(today.get("volume")))
            prev = safe_float(days[1].get("close")) if len(days) > 1 else None
            if prev:
                q["change"] = price - prev
                q["change_pct"] = (price - prev) / prev * 100.0
        return q

    async def last_price(self, symbol: str) -> float | None:
        """Most recent price: SIM set, live feed, TradingView quote, exchange ticker, then the DB cache."""
        symbol = self._clean(symbol)
        if S.is_sim(symbol):
            cs = self._sim_reference(symbol)
            return float(cs.close[-1]) if len(cs) else None
        live = self._prices.get(symbol)
        if live and time.monotonic() - live[1] <= LIVE_PRICE_TTL_S:
            return live[0]
        q = (await self._tv_quotes([symbol])).get(symbol)
        if q and (q.get("price") or 0) > 0:
            return float(q["price"])
        if not config.OFFLINE:
            price = await self._exchange_price(symbol)
            if price:
                return price
        q = await self._local_quote(symbol)
        return q["price"] if q else None

    async def _exchange_price(self, symbol: str) -> float | None:
        await self._load_info(symbol)
        route = self.route(symbol)
        if route.kind != "crypto" or not route.venues or self._crypto_down.get(symbol) is not None:
            return None
        venue = self._venue_ok.get(symbol) or route.venues[0]
        try:
            providers = self._providers()
            raw = await providers.fetch_latest(venue.provider, self._native(providers, venue), "1m", client=self._http())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once(f"latest:{symbol}", f"{symbol}: precio de {provider_label(venue.provider)} no disponible ({exc})")
            return None
        rows = clean_rows(raw)
        return rows[-1]["close"] if rows else None

    # ================================================================== universe & scans

    async def universe(
        self, market: str, offset: int = 0, limit: int = 100, sort: str = "volume", order: str = "desc",
        q: str | None = None, type_: str | None = None,
    ) -> dict:
        """UniverseResponse: every asset of a TradingView market (local `symbols` table when offline)."""
        market = str(market or "crypto").strip().lower()
        if not S.valid_market(market):
            raise ProviderError(f"Mercado no válido: {market}")
        offset = max(0, int(offset or 0))
        limit = int(clamp(int(limit or 100), 1, 500))
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

    async def _dispatch(self, symbol: str, tf: str, candle: dict, closed: bool) -> None:
        """WS `market:SYMBOL:tf` message + every listener (errors isolated per listener)."""
        self._prices[symbol] = (float(candle["close"]), time.monotonic())
        self.feed_updates += 1
        ws = self.rt.ws
        if ws is not None:
            try:
                ws.publish(market_topic(symbol, tf), {
                    "type": "candle", "symbol": symbol, "tf": tf, "candle": dict(candle), "closed": bool(closed),
                })
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

    async def _feed_tick(self) -> None:
        now = time.monotonic()
        crypto: list[tuple[str, str]] = []
        snaps: list[tuple[str, str]] = []
        for key in list(self._subs):
            symbol = key[0]
            if S.is_sim(symbol) or now < self._next_poll.get(key, 0.0):
                continue  # SIM bars arrive through push_candle
            await self._load_info(symbol)
            history = self.history_kind(symbol)[1]
            if history == "full":
                crypto.append(key)
                self._next_poll[key] = now + CRYPTO_POLL_S
            elif history in ("recorder", "keyed"):
                snaps.append(key)
                self._next_poll[key] = now + SNAPSHOT_POLL_S
        if crypto:
            await self._poll_crypto(crypto)
        if snaps:
            await self._poll_snapshots(snaps)

    async def _poll_crypto(self, keys: list[tuple[str, str]]) -> None:
        try:
            providers = self._providers()
        except Exception as exc:
            self._warn_once("feed-providers", f"Feed en vivo: proveedores no disponibles ({exc})", every_s=600)
            return
        sem = asyncio.Semaphore(FEED_CONCURRENCY)

        async def poll(symbol: str, tf: str) -> None:
            route = self.route(symbol)
            if not route.venues:
                return
            venue = self._venue_ok.get(symbol) or route.venues[0]
            async with sem:
                try:
                    raw = await providers.fetch_latest(venue.provider, self._native(providers, venue), tf, client=self._http())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    label = provider_label(venue.provider)
                    self._warn_once(f"feed:{venue.provider}:{symbol}", f"Feed {symbol} {tf}: error de {label} → {exc}")
                    failures = self._feed_failures.get(symbol, 0) + 1
                    self._feed_failures[symbol] = failures
                    if failures >= FEED_DOWN_AFTER:  # switch this symbol to TradingView snapshots for a while
                        self._feed_failures.pop(symbol, None)
                        self._crypto_down.set(symbol, f"{label}: {exc}")
                        self._emit("info", f"{symbol}: {label} no responde; el feed en vivo usa TradingView temporalmente")
                    return
            self._feed_failures.pop(symbol, None)
            self.feed_polls += 1
            await self._apply_live(symbol, tf, clean_rows(raw), venue.provider)

        results = await asyncio.gather(*(poll(s, tf) for s, tf in keys), return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                self._warn_once("feed-apply", f"Feed en vivo: error procesando velas ({type(res).__name__}: {res})")

    async def _poll_snapshots(self, keys: list[tuple[str, str]]) -> None:
        """Recorder / keyed symbols: ONE TradingView current-bars request for every symbol and timeframe."""
        tv = self.tv() if self.tv_online() else None
        if tv is None:
            return
        symbols = list(dict.fromkeys(s for s, _ in keys))
        tfs = sorted({tf if tf in RECORDER_TFS else RECORDER_BASE.get(tf, "1m") for _, tf in keys}, key=tf_seconds)
        try:
            data = await tv.current_bars(symbols, tfs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn_once("feed-tv", f"Feed en vivo (TradingView): {exc}", "tradingview")
            return
        self.feed_polls += 1
        for symbol, tf in keys:
            bars = (data or {}).get(symbol) if isinstance(data, dict) else None
            if not isinstance(bars, dict):
                continue
            if tf in RECORDER_TFS:
                c = normalize_candle(bars.get(tf))
            else:
                c = await self._aggregate_current(symbol, tf, normalize_candle(bars.get(RECORDER_BASE[tf])))
            if c is not None:
                c["time"] = floor_time(c["time"], tf)
                await self._apply_live(symbol, tf, [c], "recorder")

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

    async def _apply_live(self, symbol: str, tf: str, rows: list[dict], source: str) -> None:
        """Bar-close detection: a newer bar emits the previous one closed=True, then the forming bar."""
        key = (symbol, tf)
        if not rows or key not in self._subs:
            return
        latest = rows[-1]
        prev = self._last.get(key)
        if prev is not None and latest["time"] < prev["time"]:
            return  # out-of-order answer
        closed_rows = rows[:-1]
        if prev is not None and latest["time"] > prev["time"]:
            final = next((r for r in rows if r["time"] == prev["time"]), prev)
            closed_rows = [r for r in closed_rows if r["time"] != final["time"]] + [final]
            await self._store_closed(symbol, tf, closed_rows, source)
            await self._dispatch(symbol, tf, final, True)
        elif closed_rows:
            await self._store_closed(symbol, tf, closed_rows, source)
        if prev is None or latest != prev:
            self._last[key] = latest
            await self._dispatch(symbol, tf, latest, False)

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
