"""Local candle recorder.

Assets without a free candle API (stocks, forex, indices, futures when no API key is configured) get their OHLCV
history built locally: every `recorder.interval_s` seconds ONE batched TradingView request returns the current bar of
1m/5m/15m/1d for every recorded symbol (watchlist + live subscriptions whose history is "recorder"), and the bars are
merged into the `candles` table (source "recorder"). Other timeframes are resampled from these by the hub: TradingView's
1h/4h bars follow each exchange's session (US stocks start at 13:30 UTC), so they are rebuilt from 15m bars to keep
the UTC-aligned buckets of the rest of the app. Bars are stamped in market time (TradingView announces a delay of
10-15 min for many exchanges, see tradingview_parse.row_current_bars) and placed by content with the lag estimated
from observed rollovers (app.data.stamping.BarStamper, shared with the hub's snapshot feed), so data arriving later
or earlier than announced neither lands under the wrong minute nor creates bars after the close.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app import config
from app.data.stamping import PLACE_RANK, BarStamper, same_bar, signature  # noqa: F401  (re-exported)
from app.util import RateLimitedLog, floor_time, now_ms, safe_float

RECORDER_TFS: tuple[str, ...] = ("1m", "5m", "15m", "1d")
# timeframes not recorded directly -> recorded timeframe they are resampled from (UTC-aligned buckets)
RECORDER_BASE: dict[str, str] = {
    "3m": "1m", "30m": "15m", "1h": "15m", "2h": "15m", "4h": "15m", "6h": "15m", "12h": "15m", "1w": "1d",
}
MIN_INTERVAL_S = 5.0
MAX_SYMBOLS = 200
START_DELAY_S = 3.0
# A TradingView bar that rolls over this soon after our bucket opened is the previous bar still finishing (feed
# jitter around the announced delay), not a session boundary (hub snapshots of the timeframes not recorded).
ROLLOVER_JITTER_S = 90.0
# recorded timeframes in placement order (coarse first: their rollovers decide corrections of the 1m run)
PLACE_ORDER: tuple[str, ...] = tuple(sorted(RECORDER_TFS, key=lambda t: PLACE_RANK.get(t, 9)))

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


class CandleRecorder:
    """Owned and started by the MarketDataHub (only when BACKGROUND and not OFFLINE)."""

    def __init__(self, hub: Any):
        self.hub = hub
        self._task: asyncio.Task | None = None
        self._log = RateLimitedLog()
        self._recording: set[str] = set()
        self.stamper = BarStamper(self.last_recorded, self._relocate, self._shifted)
        # TradingView bar followed per (symbol, tf); clearing it = restart semantics (re-seeded from the database)
        self._last_snap = self.stamper.tracks
        self._written: dict[tuple[str, str], tuple[int, tuple]] = {}  # (bucket, signature) last written per key
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
            "lag_s": dict(list(self.stamper.lags().items())[:50]),
            "corrections": self.stamper.corrections,
            "relocated": self.stamper.moved,
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
        keep = set(symbols) | set(self.hub.subscribed_symbols())
        self.stamper.forget(keep)
        for key in [k for k in self._written if k[0] not in keep]:
            self._written.pop(key, None)
        self.hub._emit("debug", f"Grabador: {len(rows)} velas de {len(live)}/{len(symbols)} símbolos", source="recorder")
        return {"symbols": len(symbols), "bars": len(rows)}

    async def collect(self, data: dict, symbols: list[str]) -> list[tuple[str, str, dict]]:
        """Rows to upsert from a current_bars() answer, placed by app.data.stamping.BarStamper: the bar already
        followed keeps its bucket however late it arrives (no phantom bars after the close), a new TradingView bar
        goes to the next bucket (clock corrected by the estimated lag after gaps), a rollover inside one of our
        buckets replaces the stored bar right after the bucket opened and is dropped later on (e.g. the forex day at
        17:00 New York), and late copies of an earlier bar merge into it. Unchanged bars and stale repeats (a closed
        market re-stamping its last bar) are skipped: bars of two different TradingView bars are never blended."""
        out: list[tuple[str, str, dict]] = []
        for symbol in symbols:
            bars = data.get(symbol)
            if not isinstance(bars, dict):
                continue
            for tf in PLACE_ORDER:
                raw = bars.get(tf)
                c = normalize_candle(raw)
                if c is None:
                    continue
                c["time"] = floor_time(c["time"], tf)
                asof = safe_float(raw.get("asof"), float(c["time"])) if isinstance(raw, dict) else float(c["time"])
                placed = await self.stamper.place(symbol, tf, c, asof)
                if placed.bucket is None:
                    if placed.reason == "rollover":
                        self.skipped_rollover += 1
                    continue
                key = (symbol, tf)
                sig = signature(c)
                stamp, c["time"] = c["time"], placed.bucket
                if placed.status == "old":  # a late copy of an earlier bar: merged into it (open kept)
                    out.append((symbol, tf, c))
                    continue
                if self._written.get(key) == (placed.bucket, sig):
                    if stamp > placed.bucket:
                        self.skipped_stale += 1
                    continue  # nothing new (same bar unchanged, or a stale repeat of the last bar)
                if placed.status == "replace":
                    c["_replace"] = True
                self._written[key] = (placed.bucket, sig)
                out.append((symbol, tf, c))
        return out

    async def last_recorded(self, symbol: str, tf: str) -> Snapshot:
        """(time, signature) of the newest bar the recorder stored for (symbol, tf) (seed of the stamper)."""
        return await self._db_last(symbol, tf, recorder_only=True)

    async def _relocate(self, symbol: str, tf: str, start: int, end: int, d: int) -> int:
        """Move the recorder bars of (symbol, tf) in [start, end] by d seconds in one transaction (a run the lag
        estimate showed to be whole buckets off). Returns the number of bars moved, or -1 when nothing was moved
        because another stored bar is in the way (it would be lost or blended) or the database failed: the stamper
        then keeps the run where it is."""
        db = self.rt.db
        if db is None or not d:
            return 0
        sel = ("SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = ? AND time >= ? "
               "AND time <= ? AND source = 'recorder' ORDER BY time")
        args = (symbol, tf, int(start), int(end))
        try:
            async with db.transaction() as tx:
                rows = await tx.fetchall(sel, args)
                if not rows:
                    return 0
                lo, hi = int(start) + int(d), int(end) + int(d)
                blocking = await tx.fetchone(
                    "SELECT time FROM candles WHERE symbol = ? AND tf = ? AND time >= ? AND time <= ? "
                    "AND NOT (time >= ? AND time <= ? AND source = 'recorder') LIMIT 1",
                    (symbol, tf, lo, hi, int(start), int(end)),
                )
                if blocking is not None:
                    return -1
                await tx.execute("DELETE FROM candles WHERE symbol = ? AND tf = ? AND time >= ? AND time <= ? "
                                 "AND source = 'recorder'", args)
                await tx.executemany(MERGE_UPSERT_SQL, [
                    (symbol, tf, int(r["time"]) + int(d), r["open"], r["high"], r["low"], r["close"], r["volume"],
                     "recorder") for r in rows
                ])
        except Exception as exc:
            if self._log.should_log(f"relocate:{symbol}:{tf}", 600):
                self.hub._emit("warn", f"Grabador local: no se pudieron corregir las velas de {symbol} {tf} ({exc})",
                               source="recorder")
            return -1
        self.hub._emit("info", f"Grabador local: {len(rows)} velas de {symbol} {tf} recolocadas "
                               f"{'+' if d > 0 else '−'}{abs(int(d))} s (TradingView llega con otro retraso del "
                               "anunciado)", source="recorder")
        return len(rows)

    def _shifted(self, symbol: str, tf: str, d: int) -> None:
        """The stamper moved the current run of (symbol, tf): forget what was written and tell the live feed."""
        self._written.pop((symbol, tf), None)
        hook = getattr(self.hub, "_stamp_shifted", None)
        if callable(hook):
            hook(symbol, tf, d)

    async def last_stored(self, symbol: str, tf: str) -> Snapshot:
        """(time, OHLCV signature) of the newest stored bar of (symbol, tf); (0, None) when there is none."""
        return await self._db_last(symbol, tf)

    async def _db_last(self, symbol: str, tf: str, recorder_only: bool = False) -> Snapshot:
        only = " AND source = 'recorder'" if recorder_only else ""
        try:
            row = await self.rt.db.fetchone(
                "SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = ?"
                f"{only} ORDER BY time DESC LIMIT 1",
                (symbol, tf),
            )
        except Exception:
            row = None
        if not row:
            return (0, None)
        c = normalize_candle(row)
        return (int(row["time"]), signature(c)) if c else (0, None)

    async def store(self, rows: list[tuple[str, str, dict]]) -> None:
        """Merge snapshots into the `candles` table. A bar the stamper placed over a stored bar of another
        TradingView bar ("replace": the stored one was the previous bar, stamped late) overwrites it on its first
        write, from the recorder cycle or from the live feed's closed bar (rows flagged `_replace` too)."""
        if not rows:
            return
        merge: list[tuple] = []
        replace: list[tuple] = []
        for s, tf, c in rows:
            params = (s, tf, int(c["time"]), c["open"], c["high"], c["low"], c["close"], c["volume"], "recorder")
            flagged = self.stamper.take_replace(s, tf, int(c["time"]), c["open"]) or c.get("_replace")
            (replace if flagged else merge).append(params)
        if merge:
            await self.rt.db.executemany(MERGE_UPSERT_SQL, merge)
        if replace:
            await self.rt.db.executemany(REPLACE_UPSERT_SQL, replace)
