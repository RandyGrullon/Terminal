"""Local candle recorder.

Assets without a free candle API (stocks, forex, indices, futures when no API key is configured) get their OHLCV
history built locally: every `recorder.interval_s` seconds ONE batched TradingView request returns the current bar of
1m/5m/15m/1h/4h/1d for every recorded symbol (watchlist + live subscriptions whose history is "recorder"), and the
bars are merged into the `candles` table (source "recorder"). Other timeframes are resampled from these by the hub.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app import config
from app.util import RateLimitedLog, floor_time, now_ms, safe_float

RECORDER_TFS: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d")
# timeframes not recorded directly -> recorded timeframe they are resampled from (aligned buckets)
RECORDER_BASE: dict[str, str] = {"3m": "1m", "30m": "15m", "2h": "1h", "6h": "1h", "12h": "4h", "1w": "1d"}
MIN_INTERVAL_S = 5.0
MAX_SYMBOLS = 200
START_DELAY_S = 3.0

# Snapshots of the same bar are merged: highs/lows only widen, volume only grows, open/close follow the source.
MERGE_UPSERT_SQL = (
    "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(symbol, tf, time) DO UPDATE SET open = excluded.open, "
    "high = MAX(candles.high, excluded.high), low = MIN(candles.low, excluded.low), close = excluded.close, "
    "volume = MAX(candles.volume, excluded.volume), source = excluded.source"
)

Snapshot = tuple[int, tuple[float, float, float, float, float] | None]


def normalize_candle(c: Any) -> dict | None:
    """Candle dict with int `time` (unix s) and finite floats (high/low repaired); None when unusable."""
    if not isinstance(c, dict):
        return None
    t = safe_float(c.get("time"))
    close = safe_float(c.get("close"))
    if t is None or close is None or t <= 0:
        return None
    if t > 1e11:  # milliseconds by mistake
        t = t / 1000.0
    o = safe_float(c.get("open"), close)
    h = safe_float(c.get("high"), max(o, close))
    lo = safe_float(c.get("low"), min(o, close))
    v = safe_float(c.get("volume"), 0.0) or 0.0
    return {
        "time": int(t),
        "open": float(o),
        "high": float(max(h, o, close)),
        "low": float(min(lo, o, close)),
        "close": float(close),
        "volume": float(max(v, 0.0)),
    }


def _signature(c: dict) -> tuple[float, float, float, float, float]:
    return (c["open"], c["high"], c["low"], c["close"], c["volume"])


class CandleRecorder:
    """Owned and started by the MarketDataHub (only when BACKGROUND and not OFFLINE)."""

    def __init__(self, hub: Any):
        self.hub = hub
        self._task: asyncio.Task | None = None
        self._log = RateLimitedLog()
        self._recording: set[str] = set()
        self._last_snap: dict[tuple[str, str], Snapshot] = {}
        self.cycles = 0
        self.bars = 0
        self.skipped_stale = 0
        self.last_symbols = 0
        self.last_cycle_at: int | None = None
        self.last_error: str | None = None

    @property
    def rt(self) -> Any:
        return self.hub.rt

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def enabled(self) -> bool:
        return bool(self.hub._setting("recorder.enabled", True))

    def interval_s(self) -> float:
        value = safe_float(self.hub._setting("recorder.interval_s", 20), 20.0) or 20.0
        return max(MIN_INTERVAL_S, value)

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name="hub-recorder")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def status(self) -> dict:
        return {
            "running": self.running,
            "enabled": self.enabled(),
            "interval_s": self.interval_s(),
            "symbols": len(self._recording),
            "recording": sorted(self._recording)[:50],
            "cycles": self.cycles,
            "bars": self.bars,
            "skipped_stale": self.skipped_stale,
            "last_symbols": self.last_symbols,
            "last_cycle_at": self.last_cycle_at,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ loop

    async def _loop(self) -> None:
        await asyncio.sleep(START_DELAY_S)
        while True:
            try:
                if self.enabled() and not config.OFFLINE:
                    await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self._log.should_log("cycle", 300):
                    self.hub._emit("warn", f"Grabador local: fallo en el ciclo ({self.last_error})", source="recorder")
            await asyncio.sleep(self.interval_s())

    async def symbols_to_record(self) -> list[str]:
        """Watchlist + subscribed symbols whose candles come from the local recorder."""
        try:
            watch = await self.hub.watchlist()
        except Exception:
            watch = []
        candidates = list(dict.fromkeys([*watch, *self.hub.subscribed_symbols()]))
        out: list[str] = []
        for symbol in candidates:
            try:
                await self.hub._load_info(symbol)
                if self.hub.history_kind(symbol)[1] == "recorder":
                    out.append(symbol)
            except Exception:
                continue
        return out[:MAX_SYMBOLS]

    async def run_once(self, symbols: list[str] | None = None) -> dict:
        """One recording cycle (one TradingView request). Returns {"symbols", "bars"[, "error"]}."""
        if symbols is None:
            symbols = await self.symbols_to_record()
        self.last_symbols = len(symbols)
        if not symbols:
            return {"symbols": 0, "bars": 0}
        tv = self.hub.tv() if self.hub.tv_online() else None
        if tv is None:
            return {"symbols": len(symbols), "bars": 0, "error": "TradingView no disponible"}
        data = await tv.current_bars(list(symbols), list(RECORDER_TFS))
        rows = await self.collect(data if isinstance(data, dict) else {}, symbols)
        await self.store(rows)
        self.cycles += 1
        self.bars += len(rows)
        self.last_cycle_at = now_ms()
        self.last_error = None
        live = {s for s, _, _ in rows}
        for symbol in symbols:
            if symbol in live and symbol not in self._recording:
                self._recording.add(symbol)
                self.hub._emit(
                    "info",
                    f"Grabador local: empezando a grabar {symbol} ({', '.join(RECORDER_TFS)}) desde TradingView",
                    source="recorder",
                )
        keep = set(symbols)
        for key in [k for k in self._last_snap if k[0] not in keep]:
            self._last_snap.pop(key, None)
        self.hub._emit("debug", f"Grabador: {len(rows)} velas de {len(live)}/{len(symbols)} símbolos", source="recorder")
        return {"symbols": len(symbols), "bars": len(rows)}

    async def collect(self, data: dict, symbols: list[str]) -> list[tuple[str, str, dict]]:
        """Rows to upsert from a current_bars() answer. Skips unchanged bars and stale snapshots (a closed market
        repeating the last bar with a newer timestamp)."""
        out: list[tuple[str, str, dict]] = []
        for symbol in symbols:
            bars = data.get(symbol)
            if not isinstance(bars, dict):
                continue
            for tf in RECORDER_TFS:
                c = normalize_candle(bars.get(tf))
                if c is None:
                    continue
                c["time"] = floor_time(c["time"], tf)
                key = (symbol, tf)
                if key not in self._last_snap:
                    self._last_snap[key] = await self._db_last(symbol, tf)
                prev_time, prev_sig = self._last_snap[key]
                sig = _signature(c)
                if prev_sig is not None and sig == prev_sig:
                    if c["time"] > prev_time:
                        self.skipped_stale += 1
                    continue  # nothing new (same bar unchanged, or a stale repeat of the last bar)
                if c["time"] < prev_time:
                    continue  # out-of-order snapshot
                self._last_snap[key] = (c["time"], sig)
                out.append((symbol, tf, c))
        return out

    async def _db_last(self, symbol: str, tf: str) -> Snapshot:
        try:
            row = await self.rt.db.fetchone(
                "SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = ? "
                "ORDER BY time DESC LIMIT 1",
                (symbol, tf),
            )
        except Exception:
            row = None
        if not row:
            return (0, None)
        c = normalize_candle(row)
        return (int(row["time"]), _signature(c)) if c else (0, None)

    async def store(self, rows: list[tuple[str, str, dict]]) -> None:
        if not rows:
            return
        params = [
            (s, tf, int(c["time"]), c["open"], c["high"], c["low"], c["close"], c["volume"], "recorder")
            for s, tf, c in rows
        ]
        await self.rt.db.executemany(MERGE_UPSERT_SQL, params)
