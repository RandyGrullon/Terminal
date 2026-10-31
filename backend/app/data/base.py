"""Core market-data types shared by every module: CandleSet, resampling, SymbolInfo helpers."""
from __future__ import annotations

from typing import Any, Iterable, TypedDict

import numpy as np

from app.util import floor_times, tf_seconds

_FIELDS = ("time", "open", "high", "low", "close", "volume")


class ProviderError(Exception):
    """Raised by providers for invalid input or hard failures (the hub converts it into a note/event)."""


class SymbolInfo(TypedDict, total=False):
    symbol: str
    ticker: str
    exchange: str
    description: str
    type: str
    market: str
    currency: str
    pricescale: int | None
    logoid: str | None
    provider: str
    history: str


def make_symbol_info(symbol: str, **kw: Any) -> dict:
    """SymbolInfo dict with every key present (defaults filled)."""
    exchange, ticker = (symbol.split(":", 1) + [""])[:2] if ":" in symbol else ("", symbol)
    info = {
        "symbol": symbol,
        "ticker": ticker,
        "exchange": exchange,
        "description": ticker,
        "type": "",
        "market": "",
        "currency": "",
        "pricescale": None,
        "logoid": None,
        "provider": "",
        "history": "none",
    }
    info.update({k: v for k, v in kw.items() if v is not None or k in ("pricescale", "logoid")})
    return info


class CandleSet:
    """OHLCV bars as numpy arrays (ascending, unique times). `time` = bar open in unix seconds (UTC)."""

    __slots__ = ("symbol", "tf", "provider", "history", "note", "stale", "time", "open", "high", "low", "close", "volume")

    def __init__(
        self,
        symbol: str,
        tf: str,
        time: Any,
        open: Any,
        high: Any,
        low: Any,
        close: Any,
        volume: Any,
        provider: str = "",
        history: str = "full",
        note: str | None = None,
        stale: bool = False,
    ):
        self.symbol = symbol
        self.tf = tf
        self.provider = provider
        self.history = history
        self.note = note
        # True when the newest bar is far older than it should be (venue stopped trading / feed down): the data must
        # not be presented or used as live (NEXUS answers ok=false, the scanner skips it).
        self.stale = bool(stale)
        self.time = np.asarray(time, dtype=np.int64)
        self.open = np.asarray(open, dtype=np.float64)
        self.high = np.asarray(high, dtype=np.float64)
        self.low = np.asarray(low, dtype=np.float64)
        self.close = np.asarray(close, dtype=np.float64)
        self.volume = np.asarray(volume, dtype=np.float64)

    # ---------------------------------------------------------------- constructors
    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Any],
        symbol: str,
        tf: str,
        provider: str = "",
        history: str = "full",
        note: str | None = None,
        stale: bool = False,
    ) -> "CandleSet":
        rows = list(rows)
        if not rows:
            return cls.empty(symbol, tf, note=note, provider=provider, history="none" if history == "full" else history)
        if isinstance(rows[0], dict):
            cols = [[r.get(f, 0.0 if f == "volume" else None) for r in rows] for f in _FIELDS]
        else:
            cols = [list(c) for c in zip(*[tuple(r)[:6] if len(r) >= 6 else (*tuple(r)[:5], 0.0) for r in rows])]
        t = np.asarray(cols[0], dtype=np.float64).astype(np.int64)
        o, h, lo, c = (np.asarray(cols[i], dtype=np.float64) for i in range(1, 5))
        v = np.nan_to_num(np.asarray(cols[5], dtype=np.float64), nan=0.0)
        ok = np.isfinite(o) & np.isfinite(h) & np.isfinite(lo) & np.isfinite(c)
        t, o, h, lo, c, v = t[ok], o[ok], h[ok], lo[ok], c[ok], v[ok]
        # sort by time and keep the LAST occurrence of duplicated timestamps
        order = np.argsort(t, kind="stable")
        t, o, h, lo, c, v = t[order], o[order], h[order], lo[order], c[order], v[order]
        if len(t) > 1:
            keep = np.ones(len(t), dtype=bool)
            keep[:-1] = t[1:] != t[:-1]
            t, o, h, lo, c, v = t[keep], o[keep], h[keep], lo[keep], c[keep], v[keep]
        # repair inconsistent highs/lows defensively
        h = np.maximum.reduce([h, o, c])
        lo = np.minimum.reduce([lo, o, c])
        return cls(symbol, tf, t, o, h, lo, c, v, provider=provider, history=history, note=note, stale=stale)

    @classmethod
    def empty(cls, symbol: str, tf: str, note: str | None = None, provider: str = "", history: str = "none") -> "CandleSet":
        z = np.zeros(0)
        return cls(symbol, tf, np.zeros(0, dtype=np.int64), z, z, z, z, z, provider=provider, history=history, note=note)

    # ---------------------------------------------------------------- basics
    def __len__(self) -> int:
        return int(self.time.shape[0])

    def __repr__(self) -> str:
        stale = " stale" if self.stale else ""
        return f"CandleSet({self.symbol} {self.tf} n={len(self)} provider={self.provider} history={self.history}{stale})"

    @property
    def is_empty(self) -> bool:
        return len(self) == 0

    @property
    def last_time(self) -> int | None:
        return int(self.time[-1]) if len(self) else None

    @property
    def last_close(self) -> float | None:
        return float(self.close[-1]) if len(self) else None

    def with_meta(self, **kw: Any) -> "CandleSet":
        out = self._sub(slice(None))
        for k, v in kw.items():
            setattr(out, k, v)
        return out

    def _sub(self, sl: slice) -> "CandleSet":
        return CandleSet(
            self.symbol,
            self.tf,
            self.time[sl],
            self.open[sl],
            self.high[sl],
            self.low[sl],
            self.close[sl],
            self.volume[sl],
            provider=self.provider,
            history=self.history,
            note=self.note,
            stale=self.stale,
        )

    def tail(self, n: int) -> "CandleSet":
        return self if n >= len(self) else self._sub(slice(len(self) - max(0, n), None))

    def slice(self, i0: int, i1: int | None = None) -> "CandleSet":
        return self._sub(slice(i0, i1))

    def upto(self, index: int) -> "CandleSet":
        """Bars 0..index inclusive (what was known at the close of bar `index`)."""
        return self._sub(slice(0, index + 1))

    def append(self, candle: dict) -> "CandleSet":
        """New CandleSet with `candle` appended (or replacing the last bar when it has the same time)."""
        t = int(candle["time"])
        vals = [float(candle[f]) for f in ("open", "high", "low", "close")] + [float(candle.get("volume") or 0.0)]
        if len(self) and t < int(self.time[-1]):
            return self
        base = self._sub(slice(0, len(self) - 1)) if len(self) and t == int(self.time[-1]) else self
        return CandleSet(
            self.symbol,
            self.tf,
            np.append(base.time, t),
            np.append(base.open, vals[0]),
            np.append(base.high, vals[1]),
            np.append(base.low, vals[2]),
            np.append(base.close, vals[3]),
            np.append(base.volume, vals[4]),
            provider=self.provider,
            history=self.history,
            note=self.note,
            stale=self.stale,
        )

    def to_list(self) -> list[dict]:
        t = self.time.tolist()
        o, h, lo, c, v = (a.tolist() for a in (self.open, self.high, self.low, self.close, self.volume))
        return [
            {"time": t[i], "open": o[i], "high": h[i], "low": lo[i], "close": c[i], "volume": v[i]}
            for i in range(len(t))
        ]

    def to_frame(self):
        import pandas as pd

        return pd.DataFrame(
            {"open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume},
            index=pd.Index(self.time, name="time"),
        )


def resample(cs: CandleSet, tf: str) -> CandleSet:
    """Aggregate to a higher timeframe. The last bucket may be partial (still forming)."""
    if not len(cs):
        return CandleSet.empty(cs.symbol, tf, provider=cs.provider, history=cs.history)
    if tf_seconds(tf) < tf_seconds(cs.tf):
        raise ValueError(f"No se puede re-muestrear {cs.tf} a una temporalidad menor ({tf})")
    buckets = floor_times(cs.time, tf)
    uniq, starts = np.unique(buckets, return_index=True)
    ends = np.append(starts[1:], len(cs))
    return CandleSet(
        cs.symbol,
        tf,
        uniq,
        cs.open[starts],
        np.maximum.reduceat(cs.high, starts),
        np.minimum.reduceat(cs.low, starts),
        cs.close[ends - 1],
        np.add.reduceat(cs.volume, starts),
        provider=cs.provider,
        history=cs.history,
        note=cs.note,
        stale=cs.stale,
    )


def merge_candles(old: list[dict], new: list[dict]) -> list[dict]:
    """Merge two candle lists by time; entries in `new` win."""
    by_time = {int(c["time"]): c for c in old}
    by_time.update({int(c["time"]): c for c in new})
    return [by_time[k] for k in sorted(by_time)]
