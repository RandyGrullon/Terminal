"""Crypto exchange candle providers (official public REST APIs, no keys).

Public API (consumed by the hub):
    CEX_PROVIDERS = ("binance", "okx", "coinbase", "kraken", "kucoin")
    supports(provider, tf) -> bool
    await fetch_candles(provider, native, tf, limit, *, client, end_time=None) -> list[Candle dict]
    await fetch_latest(provider, native, tf, *, client) -> list[Candle dict]      # last 2 bars (live polling)
Extras: plan(provider, tf) -> (base_tf, factor), history_limit(provider, tf), to_native(provider, text),
        native_symbol(provider, base, quote) (same as Venue.native),
        await fetch_venues(venues, tf, limit, *, client, end_time=None) -> (candles, venue)  # first venue that works

Candles are ascending dicts {time (bar open, unix s), open, high, low, close, volume}; the last one may still be
forming. Timeframes the exchange lacks are rebuilt with app.data.base.resample from the largest native divisor
(e.g. Coinbase 4h <- 1h; Kraken/KuCoin 1w <- 1d so weeks start on Monday like everywhere else in the app).
Errors raise DataProviderError (a ProviderError) with a Spanish message and a `kind`.
"""
from __future__ import annotations

from typing import Any, Iterable

import httpx

from app.data.base import CandleSet, resample
from app.data.providers_cex import SPECS, ExchangeSpec
from app.data.providers_http import DataProviderError, ensure_online, request_json
from app.data.symbols import CEX_PROVIDERS, Venue, native_symbol
from app.util import TF_SECONDS, floor_time, normalize_tf

__all__ = [
    "CEX_PROVIDERS", "DataProviderError", "MAX_LIMIT", "fetch_candles", "fetch_latest", "fetch_venues",
    "history_limit", "native_symbol", "plan", "supports", "to_native",
]

MAX_LIMIT = 5000  # bars per call (the hub asks for <= 2000)
MAX_PAGES = 60  # hard cap of requests per call (resampled weekly bars from 1d can need several pages)


def _spec(provider: str) -> ExchangeSpec:
    spec = SPECS.get(str(provider or "").lower())
    if spec is None:
        raise DataProviderError(f"Proveedor desconocido: {provider!r}", provider=str(provider), kind="unsupported")
    return spec


def _norm_tf(tf: str, provider: str = "") -> str:
    try:
        return normalize_tf(tf)
    except ValueError as exc:
        raise DataProviderError(str(exc), provider=provider, kind="invalid") from None


def plan(provider: str, tf: str) -> tuple[str, int]:
    """(native timeframe to download, aggregation factor). factor 1 = served natively."""
    spec = _spec(provider)
    tf = _norm_tf(tf, spec.provider)
    if tf in spec.intervals:
        return tf, 1
    sec = TF_SECONDS[tf]
    best: str | None = None
    for base in spec.intervals:
        bsec = TF_SECONDS[base]
        if bsec < sec and sec % bsec == 0 and (best is None or bsec > TF_SECONDS[best]):
            best = base
    if best is None:
        raise DataProviderError(f"{spec.label}: temporalidad {tf} no disponible", provider=spec.provider,
                                kind="unsupported")
    return best, sec // TF_SECONDS[best]


def supports(provider: str, tf: str) -> bool:
    """True when `provider` can serve `tf` (natively or by resampling a smaller native timeframe)."""
    try:
        plan(provider, tf)
    except DataProviderError:
        return False
    return True


def history_limit(provider: str, tf: str) -> int | None:
    """Maximum number of `tf` bars the provider can return (None = deep history through pagination)."""
    spec = _spec(provider)
    if spec.max_bars is None:
        return None
    _, factor = plan(provider, tf)
    return max(1, -(-spec.max_bars // factor))  # ceil: the last (forming) bucket counts too


def to_native(provider: str, text: str) -> str:
    """'BTCUSDT' / 'BTC-USDT' / 'KRAKEN:XBTUSD' -> instrument id of the provider API."""
    return _spec(provider).to_native(text)


# ----------------------------------------------------------------------------- engine


async def _fetch_native(spec: ExchangeSpec, native: str, base_tf: str, need: int, end_time: int | None,
                        client: httpx.AsyncClient) -> list[dict]:
    """Page backwards from `end_time` (inclusive bar open time; None = now) until `need` bars are collected."""
    code = spec.intervals[base_tf]
    tf_sec = TF_SECONDS[base_tf]
    rows: dict[int, dict] = {}
    cursor = int(end_time) if end_time is not None else None
    page = 0
    max_pages = 1 if spec.single_page else min(MAX_PAGES, need // max(1, min(spec.page_size, 100)) + 3)
    while len(rows) < need and page < max_pages:
        url, params, requested = spec.request(native, code, tf_sec, max(2, need - len(rows)), cursor, page)
        result = await request_json(client, "GET", url, provider=spec.provider, label=spec.label, params=params)
        batch = spec.parse(result, native)
        page += 1
        if cursor is not None:
            batch = [c for c in batch if c["time"] <= cursor]
        added = 0
        for c in batch:
            if c["time"] not in rows:
                rows[c["time"]] = c
                added += 1
        if not batch or added == 0 or spec.single_page:
            break
        if spec.stop_on_short_page and len(batch) < requested:
            break
        cursor = min(c["time"] for c in batch) - 1
    out = sorted(rows.values(), key=lambda c: c["time"])
    return out[-need:] if need > 0 else out


def resample_rows(rows: list[dict], base_tf: str, tf: str) -> list[dict]:
    """Aggregate ascending base bars into `tf` buckets; the first bucket is dropped when it is incomplete."""
    if not rows:
        return []
    cs = CandleSet.from_rows(rows, "", base_tf)
    out = resample(cs, tf).to_list()
    first = int(rows[0]["time"])
    if out and floor_time(first, tf) < first:
        out = out[1:]
    return out


def _clamp_limit(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 500
    return max(1, min(MAX_LIMIT, n))


async def fetch_candles(provider: str, native: str, tf: str, limit: int, *, client: httpx.AsyncClient,
                        end_time: int | None = None) -> list[dict]:
    """Up to `limit` most recent bars with open time <= end_time (None = up to now), ascending."""
    spec = _spec(provider)
    ensure_online(spec.provider)
    tf = _norm_tf(tf, spec.provider)
    limit = _clamp_limit(limit)
    sym = spec.to_native(native)
    base_tf, factor = plan(spec.provider, tf)
    need = limit if factor == 1 else limit * factor + factor
    if spec.max_bars is not None:
        need = min(need, spec.max_bars)
    rows = await _fetch_native(spec, sym, base_tf, need, end_time, client)
    if factor > 1:
        rows = resample_rows(rows, base_tf, tf)
    if end_time is not None:
        rows = [c for c in rows if c["time"] <= end_time]
    return rows[-limit:]


async def fetch_latest(provider: str, native: str, tf: str, *, client: httpx.AsyncClient) -> list[dict]:
    """The last 2 bars (previous closed bar + the one forming) for live polling."""
    spec = _spec(provider)
    ensure_online(spec.provider)
    tf = _norm_tf(tf, spec.provider)
    sym = spec.to_native(native)
    base_tf, factor = plan(spec.provider, tf)
    need = 2 if factor == 1 else 3 * factor
    rows = await _fetch_native(spec, sym, base_tf, need, None, client)
    if factor > 1:
        rows = resample_rows(rows, base_tf, tf)
    return rows[-2:]


async def fetch_venues(venues: Iterable[Venue], tf: str, limit: int, *, client: httpx.AsyncClient,
                       end_time: int | None = None) -> tuple[list[dict], Venue]:
    """Try each venue (route order) and return the first non-empty history with the venue that served it.
    Raises the most informative DataProviderError when every venue fails."""
    ensure_online()
    errors: list[DataProviderError] = []
    tried = 0
    for venue in venues:
        if venue.provider not in SPECS:
            continue
        tried += 1
        try:
            rows = await fetch_candles(venue.provider, venue.native, tf, limit, client=client, end_time=end_time)
        except DataProviderError as exc:
            if exc.kind in ("offline", "invalid"):
                raise
            errors.append(exc)
            continue
        if rows:
            return rows, venue
        errors.append(DataProviderError(f"{venue.tv_symbol}: sin velas disponibles", provider=venue.provider,
                                        kind="not_found"))
    if not tried:
        raise DataProviderError("No hay un exchange con API pública para este símbolo", kind="unsupported")
    # prefer a transient error (worth retrying) over "not found" so the caller does not give up for good
    for kind in ("rate_limit", "network", "server"):
        for exc in errors:
            if exc.kind == kind:
                raise exc
    detail = "; ".join(str(e) for e in errors[:3])
    raise DataProviderError(f"Sin datos en los exchanges ({detail})", provider=errors[0].provider if errors else "",
                            kind="not_found")
