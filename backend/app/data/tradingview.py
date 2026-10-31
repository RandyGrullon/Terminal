"""TradingView scanner client (public screener endpoint, plain JSON POSTs through app.util.http_client()).

No browser headers, cookies or Origin/Referer are sent (honest User-Agent only). Every request goes through one
RateLimiter (>= 1 s between calls, max 2 concurrent) and results are cached (quotes 5 s, universe 60 s, search 300 s,
market counts 1 h). `enabled()` (settings providers.tradingview_enabled) and config.OFFLINE are checked on every call:
when either blocks the network the client returns empty results without touching it. HTTP/network failures raise
DataProviderError (a ProviderError, Spanish message).

    TradingViewClient(enabled=lambda: True)
      scan(market, columns, *, filters=None, sort_by=None, order="desc", limit=300, offset=0, tickers=None,
           filter2=None) -> {"total", "rows": [{"symbol", <column>: value}]}
      search(q, market=None, limit=30) -> [SearchResult-like dicts]       quotes(symbols) -> {symbol: Quote}
      universe(market, offset, limit, sort, order, q, type_) -> {"total", "rows": [UniverseRow-like]}
      market_counts(wait_s=4.0) -> {market: int | None}                   market_counts_cached()
      symbol_details(symbol) -> SymbolInfo + "quote" + "technicals" | None
      current_bars(symbols, tfs) -> {symbol: {tf: Candle + asof}} (stamped in market time: now - announced delay)
      status() / close()
"""
from __future__ import annotations

import asyncio
import re
import time
import weakref
from typing import Any, Callable, Iterable

import httpx

from app import config
from app.data import tradingview_parse as P
from app.data.providers_http import DataProviderError, request_json, unexpected
from app.data.symbols import (
    _EXCHANGE_MARKET, FOREX_EXCHANGES, FUTURES_EXCHANGES, INDEX_EXCHANGES, MARKETS, PREFERRED_VENUES,
    US_STOCK_EXCHANGES, alias_targets, clean_symbol, is_sim, market_for, rank_results, valid_market,
)
from app.util import RateLimiter, TTLCache, normalize_tf, now_ms, now_s, parse_symbol

PROVIDER = "tradingview"
LABEL = "TradingView"
MAX_SCAN_ROWS = 1000
TICKER_CHUNK = 200
COUNT_TTL_S = 3600.0
COUNT_RETRY_S = 300.0
_TICKER_RE = re.compile(r"^[A-Z0-9_]{1,32}:[A-Z0-9][A-Z0-9_.!&+\-/^]{0,63}$")
# Venues allowed in free-text searches over global/crypto (keeps DEX pools with absurd volumes out of the top 60)
SEARCH_VENUES: tuple[str, ...] = tuple(sorted(
    set(PREFERRED_VENUES) | set(P.CEX_EXCHANGES) | set(_EXCHANGE_MARKET) | FOREX_EXCHANGES | FUTURES_EXCHANGES
    | US_STOCK_EXCHANGES | INDEX_EXCHANGES
))


def _clean_tickers(symbols: Iterable[Any], limit: int = 2000) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols or []:
        sym = "".join(str(s or "").split()).upper()
        if sym and sym not in seen and _TICKER_RE.match(sym) and not is_sim(sym):
            seen.add(sym)
            out.append(sym)
            if len(out) >= limit:
                break
    return out


class TradingViewClient:
    def __init__(
        self,
        enabled: Callable[[], bool] | bool = lambda: True,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        min_interval_s: float = 1.0,
        max_concurrency: int = 2,
    ) -> None:
        self._enabled = enabled if callable(enabled) else (lambda value=bool(enabled): value)
        self._factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self._min_interval = float(min_interval_s)
        self._max_concurrency = int(max_concurrency)
        self._limiters: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, RateLimiter]" = (
            weakref.WeakKeyDictionary()
        )
        self._search_cache = TTLCache(300.0, maxsize=512)
        self._quote_cache = TTLCache(5.0, maxsize=8192)
        self._universe_cache = TTLCache(60.0, maxsize=256)
        self._details_cache = TTLCache(30.0, maxsize=1024)
        self._where = TTLCache(6 * 3600.0, maxsize=8192)  # symbol -> scanner market where it was found
        self._missing = TTLCache(600.0, maxsize=8192)  # symbols the scanner does not list
        self._counts: dict[str, tuple[float, int | None]] = {}
        self._counts_task: asyncio.Task | None = None
        self.requests = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_error_at: int | None = None
        self.last_ok_at: int | None = None

    # ------------------------------------------------------------------ plumbing

    def active(self) -> bool:
        """Network allowed right now (not OFFLINE and the user did not disable TradingView)."""
        if config.OFFLINE:
            return False
        try:
            return bool(self._enabled())
        except Exception:
            return True

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._factory() if self._factory is not None else self._default_client()
        return self._client

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        from app.util import http_client

        return http_client(timeout=20.0)

    def _limiter(self) -> RateLimiter:
        loop = asyncio.get_running_loop()
        lim = self._limiters.get(loop)
        if lim is None:
            lim = RateLimiter(self._min_interval, self._max_concurrency)
            self._limiters[loop] = lim
        return lim

    def _fail(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = str(exc)[:300]
        self.last_error_at = now_ms()

    async def _post(self, market: str, body: dict) -> dict:
        self.requests += 1
        try:
            result = await request_json(
                self._http(), "POST", P.SCAN_URL.format(market=market), provider=PROVIDER, label=LABEL,
                json_body=body, limiter=self._limiter(),
            )
        except DataProviderError as exc:
            self._fail(exc)
            raise
        data = result.data if isinstance(result.data, dict) else None
        error = (data or {}).get("error")
        if result.status == 200 and data is not None and not error:
            self.last_ok_at = now_ms()
            return data
        if error or result.status in (400, 404, 422):
            detail = " ".join(str(error or result.text or f"HTTP {result.status}").split())[:200]
            kind = "not_found" if result.status == 404 else "invalid"
            exc = DataProviderError(f"{LABEL}: petición rechazada ({detail})", provider=PROVIDER, kind=kind)
        else:
            exc = unexpected(LABEL, PROVIDER, result)
        self._fail(exc)
        raise exc

    # ------------------------------------------------------------------ raw scan

    async def scan(
        self,
        market: str,
        columns: Iterable[str],
        *,
        filters: Any = None,
        sort_by: str | None = None,
        order: str = "desc",
        limit: int = 300,
        offset: int = 0,
        tickers: list[str] | None = None,
        filter2: dict | None = None,
    ) -> dict:
        """One screener request. `filters` are TradingView filter dicts passed through; with `tickers` the scan
        returns exactly those symbols (range/limit ignored)."""
        if not self.active():
            return {"total": 0, "rows": []}
        mkt = str(market or "").strip().lower()
        if not valid_market(mkt):
            raise DataProviderError(f"Mercado no válido: {market!r}", provider=PROVIDER, kind="invalid")
        try:
            cols = P.unique_columns(columns)
            sort_col = P.unique_columns([sort_by])[0] if sort_by else None
        except ValueError as exc:
            raise DataProviderError(str(exc), provider=PROVIDER, kind="invalid") from None
        tick = _clean_tickers(tickers) if tickers is not None else None
        if tickers is not None and not tick:
            return {"total": 0, "rows": []}
        n = max(1, min(MAX_SCAN_ROWS, int(limit or 1)))
        body = P.build_body(cols, filters=filters, filter2=filter2, sort_by=sort_col, order=order,
                            offset=max(0, int(offset or 0)), limit=n, tickers=tick)
        data = await self._post(mkt, body)
        try:
            return P.parse_response(data, cols)
        except ValueError as exc:
            err = DataProviderError(f"{LABEL}: {exc}", provider=PROVIDER, kind="invalid")
            self._fail(err)
            raise err from None

    async def _lookup(self, symbols: list[str], columns: list[str]) -> dict[str, dict]:
        """symbol -> row. Looks symbols up in the global market (or the market where they were last found), then
        retries the missing ones in their own market (TVC indices live in 'cfd', etc.). Unknown symbols are
        remembered for 10 minutes."""
        if not self.active():
            return {}
        groups: dict[str, list[str]] = {}
        for sym in symbols:
            if self._missing.get(sym) is None:
                groups.setdefault(self._where.get(sym) or "global", []).append(sym)
        found: dict[str, dict] = {}
        errors: list[DataProviderError] = []
        retry: dict[str, list[str]] = {}
        attempted = 0
        for market, syms in groups.items():
            for i in range(0, len(syms), TICKER_CHUNK):
                chunk = syms[i:i + TICKER_CHUNK]
                attempted += 1
                try:
                    data = await self.scan(market, columns, tickers=chunk)
                except DataProviderError as exc:
                    errors.append(exc)
                    continue
                for row in data["rows"]:
                    found[row["symbol"]] = row
                    self._where.set(row["symbol"], market)
                for sym in chunk:
                    if sym not in found:
                        exchange, ticker = parse_symbol(sym)
                        alt = market_for(exchange, ticker)
                        if alt != market and alt != "global":
                            retry.setdefault(alt, []).append(sym)
                        else:
                            self._missing.set(sym, True)
        for market, syms in retry.items():
            for i in range(0, len(syms), TICKER_CHUNK):
                chunk = syms[i:i + TICKER_CHUNK]
                try:
                    data = await self.scan(market, columns, tickers=chunk)
                except DataProviderError as exc:
                    errors.append(exc)
                    continue
                for row in data["rows"]:
                    found[row["symbol"]] = row
                    self._where.set(row["symbol"], market)
                for sym in chunk:
                    if sym not in found:
                        self._missing.set(sym, True)
        if errors and not found and attempted:
            raise errors[0]
        return found

    async def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Quote (types.ts) by symbol for the symbols TradingView lists (SIM:* and unknown ones are omitted)."""
        if not self.active():
            return {}
        out: dict[str, dict] = {}
        missing: list[str] = []
        for sym in _clean_tickers(symbols):
            cached = self._quote_cache.get(sym)
            if cached is not None:
                out[sym] = dict(cached)
            else:
                missing.append(sym)
        if missing:
            rows = await self._lookup(missing, list(P.QUOTE_COLUMNS))
            ts = now_ms()
            for sym, row in rows.items():
                quote = P.row_quote(row, ts)
                self._quote_cache.set(sym, quote)
                out[sym] = dict(quote)
        return out

    # ------------------------------------------------------------------ search

    def _rank(self, query: str, rows: list[dict], market: str | None) -> list[dict]:
        items = [P.search_item(r, market) for r in rows if isinstance(r, dict) and r.get("symbol")]
        return rank_results(query, items, boost=alias_targets())

    async def search(self, q: str, market: str | None = None, limit: int = 30) -> list[dict]:
        """SearchResult-like dicts (SymbolInfo + price + change_pct): exact names first, DEX pools removed."""
        query = " ".join(str(q or "").split())[:64]
        if not query or not self.active():
            return []
        mkt = str(market or "").strip().lower() or None
        if mkt is not None and not valid_market(mkt):
            raise DataProviderError(f"Mercado no válido: {market!r}", provider=PROVIDER, kind="invalid")
        n = max(1, min(100, int(limit or 30)))
        key = (P.norm_query(query), mkt, n)
        cached = self._search_cache.get(key)
        if cached is not None:
            return [dict(x) for x in cached]
        scan_market = mkt if mkt and mkt != "global" else "global"
        cols = list(P.SEARCH_COLUMNS)
        exact: str | None = None
        if ":" in query:
            try:
                exact = clean_symbol(query)
            except ValueError:
                exact = None
        text = parse_symbol(exact)[1] if exact else query
        names = P.search_variants(text)
        venue_filter: list[dict] = []
        if mkt in (None, "global"):
            venue_filter = [{"left": "exchange", "operation": "in_range", "right": list(SEARCH_VENUES)}]
        elif mkt == "crypto":
            venue_filter = [{"left": "exchange", "operation": "in_range", "right": list(P.CEX_EXCHANGES)}]
        jobs: list[Any] = []
        if exact:
            jobs.append(self._lookup([exact], cols))
        if names:
            name_filter = [{"left": "name", "operation": "in_range", "right": names}]
            jobs.append(self.scan(scan_market, cols, filters=name_filter, sort_by="volume", limit=100))
        if not exact:
            jobs.append(self.scan(scan_market, cols, filters=venue_filter, filter2=P.text_filter2(text),
                                  sort_by="volume", limit=60))
        rows: list[dict] = []
        errors: list[BaseException] = []
        for res in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                errors.append(res)
            elif isinstance(res, dict) and isinstance(res.get("rows"), list):
                rows.extend(res["rows"])
            elif isinstance(res, dict):
                rows.extend(res.values())
        if errors and not rows:
            err = errors[0]
            raise err if isinstance(err, DataProviderError) else DataProviderError(
                f"{LABEL}: error en la búsqueda ({type(err).__name__})", provider=PROVIDER)
        ranked = self._rank(text, rows, mkt)
        if len(ranked) < min(n, 8) and venue_filter and not exact:
            try:
                extra = await self.scan(scan_market, cols, filter2=P.text_filter2(text), sort_by="volume", limit=60)
                ranked = self._rank(text, rows + extra["rows"], mkt)
            except DataProviderError:
                pass
        if exact:
            ranked.sort(key=lambda item: item.get("symbol") != exact)  # stable: the exact symbol goes first
        out = [P.drop_private(item) for item in ranked[:n]]
        self._search_cache.set(key, out)
        return [dict(x) for x in out]

    # ------------------------------------------------------------------ universe & market sizes

    async def universe(self, market: str, offset: int = 0, limit: int = 100, sort: str = "volume",
                       order: str = "desc", q: str | None = None, type_: str | None = None) -> dict:
        """{"total", "rows": [UniverseRow-like]} (quick_score/quick_prob are added by the hub). Rows also carry
        the raw scanner columns of P.QUICK_COLUMNS (see P.public_universe_row to strip them)."""
        if not self.active():
            return {"total": 0, "rows": []}
        mkt = str(market or "crypto").strip().lower()
        if not valid_market(mkt):
            raise DataProviderError(f"Mercado no válido: {market!r}", provider=PROVIDER, kind="invalid")
        off = max(0, int(offset or 0))
        n = max(1, min(500, int(limit or 100)))
        direction = "asc" if str(order or "").lower() == "asc" else "desc"
        query = " ".join(str(q or "").split())[:64] or None
        kind = str(type_ or "").strip().lower() or None
        key = (mkt, off, n, str(sort or ""), direction, P.norm_query(query or ""), kind)
        cached = self._universe_cache.get(key)
        if cached is not None:
            return {"total": cached["total"], "rows": [dict(r) for r in cached["rows"]]}
        filters = P.universe_filters(mkt, kind)
        filter2 = P.text_filter2(query) if query else None
        data: dict | None = None
        last: DataProviderError | None = None
        used_sort: str | None = None
        for sort_col in P.universe_sorts(mkt, sort):
            try:
                data = await self.scan(mkt, P.universe_columns(), filters=filters, filter2=filter2, sort_by=sort_col,
                                       order=direction, limit=n, offset=off)
                used_sort = sort_col
                break
            except DataProviderError as exc:
                if exc.kind != "invalid":
                    raise
                last = exc
        if data is None:
            raise last or DataProviderError(f"{LABEL}: universo no disponible", provider=PROVIDER)
        rows = [P.row_universe(r, mkt) for r in data["rows"]
                if P.not_dex(r) and not P.absurd_liquidity(r, used_sort)]
        result = {"total": int(data["total"]), "rows": rows}
        self._universe_cache.set(key, result)
        return {"total": result["total"], "rows": [dict(r) for r in rows]}

    def market_counts_cached(self) -> dict[str, int | None]:
        """Counts known so far (no I/O); None = unknown."""
        return {mid: (self._counts.get(mid) or (0.0, None))[1] for mid, _ in MARKETS}

    def _stale_markets(self) -> list[str]:
        now = time.monotonic()
        out = []
        for mid, _ in MARKETS:
            entry = self._counts.get(mid)
            if entry is None or now - entry[0] > (COUNT_TTL_S if entry[1] is not None else COUNT_RETRY_S):
                out.append(mid)
        return out

    async def _refresh_counts(self, markets: list[str]) -> None:
        """Sequential (one request at a time, so interactive calls keep flowing through the limiter)."""
        for mid in markets:
            if not self.active():
                return
            try:
                data = await self.scan(mid, ["name"], filters=P.universe_filters(mid, None), limit=1)
                self._counts[mid] = (time.monotonic(), int(data["total"]))
            except DataProviderError:
                self._counts[mid] = (time.monotonic(), None)
            # low priority: leave limiter slots free for interactive requests (quotes, search, charts)
            await asyncio.sleep(self._min_interval * 1.5)

    async def market_counts(self, wait_s: float = 4.0) -> dict[str, int | None]:
        """Number of symbols per market (cached 1 h). A cold cache needs one request per market (~30 s): the
        refresh continues in the background and this returns what is known after `wait_s` seconds."""
        if not self.active():
            return {}
        stale = self._stale_markets()
        if stale:
            task = self._counts_task
            if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
                task = asyncio.get_running_loop().create_task(self._refresh_counts(stale))
                self._counts_task = task
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, float(wait_s)))
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # recorded per market; never fail /api/markets
                self._fail(exc)
        return self.market_counts_cached()

    # ------------------------------------------------------------------ details & current bars

    async def symbol_details(self, symbol: str) -> dict | None:
        """SymbolInfo fields + "quote" (Quote) + "technicals" ({rating, rating_ma, rating_osc, rsi, adx, ...})."""
        if not self.active():
            return None
        try:
            sym = clean_symbol(symbol)
        except ValueError:
            return None
        if is_sim(sym):
            return None
        cached = self._details_cache.get(sym)
        if cached is not None:
            return {**cached, "quote": dict(cached["quote"]), "technicals": dict(cached["technicals"])}
        row = (await self._lookup([sym], P.details_columns())).get(sym)
        if row is None:
            return None
        quote = P.row_quote(row, now_ms())
        self._quote_cache.set(sym, quote)
        out = {**P.row_info(row, self._where.get(sym)), "quote": quote, "technicals": P.row_technicals(row)}
        self._details_cache.set(sym, out)
        return {**out, "quote": dict(quote), "technicals": dict(out["technicals"])}

    async def current_bars(self, symbols: list[str], tfs: list[str]) -> dict[str, dict[str, dict]]:
        """{symbol: {tf: Candle}} for the bars forming now from TradingView's current-bar columns, stamped in market
        time (time = floor_time(now - delay, tf), `asof` = now - delay; delayed exchanges announce 600-900 s).
        Supported tfs: P.CURRENT_BAR_TFS (1m 5m 15m 1h 4h 1d 1w); others are ignored."""
        if not self.active():
            return {}
        wanted: list[str] = []
        for tf in tfs or []:
            try:
                t = normalize_tf(tf)
            except ValueError:
                continue
            if t in P.CURRENT_BAR_SUFFIX and t not in wanted:
                wanted.append(t)
        syms = _clean_tickers(symbols)
        if not wanted or not syms:
            return {}
        rows = await self._lookup(syms, P.current_bar_columns(wanted))
        now = now_s()
        out: dict[str, dict[str, dict]] = {}
        for sym, row in rows.items():
            bars = P.row_current_bars(row, wanted, now)
            if bars:
                out[sym] = bars
        return out

    # ------------------------------------------------------------------ lifecycle

    def status(self) -> dict:
        return {
            "running": True,
            "enabled": self.active(),
            "offline": bool(config.OFFLINE),
            "requests": self.requests,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_ok_at": self.last_ok_at,
            "cache": {
                "search": len(self._search_cache),
                "quotes": len(self._quote_cache),
                "universe": len(self._universe_cache),
                "details": len(self._details_cache),
                "market_counts": sum(1 for v in self._counts.values() if v[1] is not None),
            },
        }

    async def close(self) -> None:
        task, self._counts_task = self._counts_task, None
        if task is not None and not task.done():
            try:
                if task.get_loop() is asyncio.get_running_loop():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            except RuntimeError:
                pass
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:
                pass
