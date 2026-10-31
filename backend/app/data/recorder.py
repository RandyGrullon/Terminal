"""Local candle recorder.

Assets without a free candle API (stocks, forex, indices, futures when no API key is configured) get their OHLCV
history built locally: every `recorder.interval_s` seconds ONE batched TradingView request returns the current bar of
1m/5m/15m/1d for every recorded symbol (watchlist + live subscriptions whose history is "recorder"), and the bars are
merged into the `candles` table (source "recorder"). Other timeframes are resampled from these by the hub: TradingView's
1h/4h bars follow each exchange's session (US stocks start at 13:30 UTC), so they are rebuilt from 15m bars to keep
the UTC-aligned buckets of the rest of the app. Bars are stamped in market time (TradingView announces a delay of
10-15 min for many exchanges, see tradingview_parse.row_current_bars).
"""
from __future__ import annotations

import asyncio
from typing import Any

from app import config
from app.util import RateLimitedLog, floor_time, now_ms, safe_float, tf_seconds

RECORDER_TFS: tuple[str, ...] = ("1m", "5m", "15m", "1d")
# timeframes not recorded directly -> recorded timeframe they are resampled from (UTC-aligned buckets)
RECORDER_BASE: dict[str, str] = {
    "3m": "1m", "30m": "15m", "1h": "15m", "2h": "15m", "4h": "15m", "6h": "15m", "12h": "15m", "1w": "1d",
}
MIN_INTERVAL_S = 5.0
MAX_SYMBOLS = 200
START_DELAY_S = 3.0
# A TradingView bar that rolls over this soon after our bucket opened is the previous bar still finishing (feed
# jitter around the announced delay), not a session boundary.
ROLLOVER_JITTER_S = 90.0

# Snapshots of the same TradingView bar are merged: the stored open is kept, highs/lows only widen, close follows the
# source and volume only grows. A snapshot of ANOTHER TradingView bar (different open or lower volume: TradingView
# rolled over inside our bucket) never blends into the stored bar; CandleRecorder.collect decides whether it
# replaces the stored bar (REPLACE_UPSERT_SQL) or is dropped.
MERGE_UPSERT_SQL = (
    "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(symbol, tf, time) DO UPDATE SET "
    "high = MAX(candles.high, excluded.high), low = MIN(candles.low, excluded.low), close = excluded.close, "
    "volume = MAX(candles.volume, excluded.volume), source = excluded.source "
    "WHERE candles.open = excluded.open AND excluded.volume >= candles.volume"
)
REPLACE_UPSERT_SQL = (
    "INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(symbol, tf, time) DO UPDATE SET open = excluded.open, high = excluded.high, low = excluded.low, "
    "close = excluded.close, volume = excluded.volume, source = excluded.source"
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


def signature(c: dict) -> tuple[float, float, float, float, float]:
    return (c["open"], c["high"], c["low"], c["close"], c["volume"])


def same_bar(stored: tuple[float, ...], new: tuple[float, ...]) -> bool:
    """True when two snapshots belong to the same TradingView bar (its open never changes, its volume never
    drops)."""
    return stored[0] == new[0] and new[4] >= stored[4]


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
        self.skipped_rollover = 0
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
            "skipped_rollover": self.skipped_rollover,
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
        repeating the last bar with a newer timestamp). When TradingView's bar rolls over inside one of our buckets,
        the snapshot replaces the stored bar right after the bucket opened (the stored one was the previous bar,
        late because of feed jitter) and is dropped later on (TradingView's session starts before our bucket ends,
        e.g. the forex day at 17:00 New York): bars of two different TradingView bars are never blended."""
        out: list[tuple[str, str, dict]] = []
        for symbol in symbols:
            bars = data.get(symbol)
            if not isinstance(bars, dict):
                continue
            for tf in RECORDER_TFS:
                raw = bars.get(tf)
                c = normalize_candle(raw)
                if c is None:
                    continue
                c["time"] = floor_time(c["time"], tf)
                key = (symbol, tf)
                from_db = key not in self._last_snap  # first snapshot since (re)start: compared with the DB bar
                if from_db:
                    self._last_snap[key] = await self.last_stored(symbol, tf)
                prev_time, prev_sig = self._last_snap[key]
                sig = signature(c)
                if prev_sig is not None and sig == prev_sig:
                    if c["time"] > prev_time:
                        self.skipped_stale += 1
                    continue  # nothing new (same bar unchanged, or a stale repeat of the last bar)
                if c["time"] < prev_time:
                    continue  # out-of-order snapshot
                if prev_sig is not None:
                    tf_s = tf_seconds(tf)
                    asof = safe_float(raw.get("asof"))
                    early = asof is not None and asof - c["time"] < min(tf_s / 2.0, ROLLOVER_JITTER_S)
                    same = same_bar(prev_sig, sig)
                    if c["time"] == prev_time and not same:
                        if not early:
                            self.skipped_rollover += 1
                            continue
                        c["_replace"] = True
                    elif same and c["time"] > prev_time and (early or from_db or c["time"] - prev_time > tf_s):
                        # still the stored TradingView bar: finishing right after our bucket rolled (feed jitter),
                        # or frozen since (restart while the market is closed) -> complete it, no new bar
                        c["time"] = prev_time
                self._last_snap[key] = (c["time"], sig)
                out.append((symbol, tf, c))
        return out

    async def last_stored(self, symbol: str, tf: str) -> Snapshot:
        """(time, OHLCV signature) of the newest stored bar of (symbol, tf); (0, None) when there is none."""
        return await self._db_last(symbol, tf)

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
        return (int(row["time"]), signature(c)) if c else (0, None)

    async def store(self, rows: list[tuple[str, str, dict]]) -> None:
        """Merge snapshots into the `candles` table (rows flagged `_replace` by collect() overwrite the stored bar)."""
        if not rows:
            return
        merge: list[tuple] = []
        replace: list[tuple] = []
        for s, tf, c in rows:
            params = (s, tf, int(c["time"]), c["open"], c["high"], c["low"], c["close"], c["volume"], "recorder")
            (replace if c.get("_replace") else merge).append(params)
        if merge:
            await self.rt.db.executemany(MERGE_UPSERT_SQL, merge)
        if replace:
            await self.rt.db.executemany(REPLACE_UPSERT_SQL, replace)
