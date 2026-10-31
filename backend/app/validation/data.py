"""Candle loading for the validation: through the app's market-data hub, or the exchanges' public APIs directly (CLI).

Only CLOSED bars are validated (the forming bar is dropped) and stale sets are refused, like everywhere else in the
app. Network access goes through the app's own providers and `app.util.http_client()` (honest User-Agent).
"""
from __future__ import annotations

from typing import Any

import numpy as np

from app.data.base import CandleSet
from app.data.symbols import CEX_PROVIDERS
from app.util import now_s, parse_symbol, tf_seconds
from app.validation.walkforward import ValidationDataError

MIN_BARS = 1000
MAX_BARS = 5000  # the hub's (and the providers') per-request cap


def closed_only(cs: CandleSet, now: int | None = None) -> CandleSet:
    """Drop the trailing bars that have not closed yet at `now` (unix s)."""
    if len(cs) == 0:
        return cs
    t_now = int(now if now is not None else now_s())
    closed = (np.asarray(cs.time, dtype=np.int64) + tf_seconds(cs.tf)) <= t_now
    last = int(np.nonzero(closed)[0][-1]) if closed.any() else -1
    return cs.slice(0, last + 1)


def _checked(cs: CandleSet | None, symbol: str, tf: str, bars: int) -> CandleSet:
    if cs is None or len(cs) == 0:
        note = f": {cs.note}" if cs is not None and cs.note else ""
        raise ValidationDataError(f"Sin velas de {symbol} {tf}{note}")
    if cs.stale:
        raise ValidationDataError(cs.note or f"Datos desactualizados de {symbol} {tf}")
    out = closed_only(cs).tail(bars)
    if len(out) == 0:
        raise ValidationDataError(f"Sin velas cerradas de {symbol} {tf}")
    return out


async def load_from_hub(hub: Any, symbol: str, tf: str, bars: int) -> CandleSet:
    """`bars` closed bars from rt.hub (cache + exchanges); one extra bar is requested for the forming one."""
    try:
        cs = await hub.get_candles(symbol, tf, min(int(bars) + 1, MAX_BARS))
    except Exception as exc:  # ProviderError (invalid input) or an unexpected provider failure
        raise ValidationDataError(f"No se pudieron obtener las velas de {symbol} {tf}: {exc}") from None
    return _checked(cs, symbol, tf, bars)


async def load_direct(symbol: str, tf: str, bars: int, client: Any) -> CandleSet:
    """`bars` closed bars from the exchange named by the symbol prefix (BINANCE:, OKX:, COINBASE:, KRAKEN:, KUCOIN:)."""
    from app.data import providers

    exchange, ticker = parse_symbol(symbol)
    provider = exchange.lower()
    if provider not in CEX_PROVIDERS:
        raise ValidationDataError(
            f"{symbol}: la línea de comandos solo descarga velas de los exchanges con API pública "
            f"({', '.join(p.upper() for p in CEX_PROVIDERS)}); valida otros mercados desde la app")
    try:
        rows = await providers.fetch_candles(provider, ticker, tf, min(int(bars) + 1, MAX_BARS), client=client)
    except Exception as exc:
        raise ValidationDataError(f"No se pudieron obtener las velas de {symbol} {tf}: {exc}") from None
    cs = CandleSet.from_rows(rows, symbol.upper(), tf, provider=provider, history="full")
    return _checked(cs, symbol, tf, bars)
