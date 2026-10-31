"""Robust stamping of TradingView current-bar snapshots (shared by the recorder and the hub's snapshot feed).

TradingView announces a data delay (`update_mode`, e.g. 900 s for NASDAQ without a real-time feed) and snapshots are
stamped in market time, asof = now - announced delay (tradingview_parse.row_current_bars). The data can still run
later or earlier than announced (feed latency, clock skew): J = real lag beyond the announcement. Near a bucket
boundary the wall clock alone then puts bars under the wrong bucket, so placement is decided by CONTENT first:

- A snapshot of the bar already tracked (same open, volume never lower, high/low only widen) stays in that bar's
  bucket however late it arrives: a bar finishing after our bucket rolled, frozen bars after the close or a restart
  while the market is closed never open a new bar (no phantom bars).
- A new TradingView bar (open change, volume reset or a narrower range) goes to the next bucket for timeframes up to
  15 min (TradingView's intraday bars line up with UTC buckets: sessions open on a quarter hour); after a gap (no
  trades, overnight) the clock corrected by the estimated lag decides. The daily bar keeps the clock rule: forex and
  futures sessions start at 17:00/18:00 New York, inside our UTC day.
- J is estimated per symbol from observed rollovers: a rollover into bucket B first seen at asof `a` (the previous
  snapshot, at `a_prev`, still showed the old bar) says J in (a_prev - B, a - B]. The value covered by most of these
  intervals (max overlap: robust to outliers and to minutes without trades) is the estimate. 5m / 15m rollovers pin
  J without ambiguity (|J| < 150 s); 1m rollovers only modulo 60 s.
- A run of bars placed one after another after an uncertain start (first snapshot, gap) that the estimate shows to be
  whole buckets off (e.g. the first snapshot already showed the next minute because the data runs 30 s ahead) is
  moved to the right buckets in the database, and later bars follow. A new bar seen right after our bucket opened
  while the followed bar holds that bucket means the followed run is one bucket late: it moves back at once.
- Guards (adversarial verification): an EARLIER state of the followed bar arriving after a newer one (the recorder
  and the live feed ask independently, so answers come back out of order) is dropped, never taken for a new bar. A
  run whose own rollovers agreed with the lag is confirmed and never moved again (a later change of the lag, e.g. the
  PC clock corrected, must not move hours of good bars); runs placed by the lag-corrected clock are not moved because
  of a minute without trades or late 1m rollovers (sparse trading). A move onto another stored bar is refused.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from app.util import floor_time, tf_seconds

Sig = tuple[float, float, float, float, float]

SEQUENCED_MAX_S = 900  # timeframes that divide 15 min: TradingView's bars line up with our UTC buckets
LAG_TOLERANCE_S = 35.0  # data may run this far ahead of the estimate: a new bar still goes to the next bucket
LAG_TOLERANCE_KNOWN_S = 15.0  # ... once the lag is known (several rollovers agree)
EARLY_WINDOW_S = 90.0  # a rollover this soon after our bucket opened: the stored bar was the previous one (late)
OBS_MAX_GAP_S = 90.0  # a rollover seen after a longer pause between snapshots says little about the lag
LAG_WINDOW = 40  # rollover observations kept per (symbol, tf) run and per symbol (settled coarse ones)
LAG_MAX_S = 120.0  # estimates beyond this are not believed (the announced delay is assumed right)
RECENT_BARS = 3  # earlier TradingView bars remembered per (symbol, tf): late copies are recognised
MIN_CORRECTION_OBS = 3  # rollovers that must agree before a run is moved by whole buckets
RUNS_KEPT = 4  # runs kept per (symbol, tf): an earlier one can still be fixed once the lag is known
CORRECTION_SHARE = 0.6  # ... and the share of the run's rollovers they must be (sparse trading disagrees)
# an earlier state of the followed bar arriving this soon after a newer one is a stale answer (the recorder and the
# live feed ask TradingView independently: overlapping requests come back out of order), never a new bar
STALE_WINDOW_S = 15.0
# ... and within half a bucket (at most EARLY_WINDOW_S) when it still has this share of the followed bar's volume (the
# data went back a few seconds, e.g. a lagging server answered): a new bar opening at the same price starts near 0
STALE_VOLUME_SHARE = 0.5
# 1m runs: the lag read from their own rollovers must be this close (share of a bucket) to a whole number of buckets
# off the agreed lag before the run is moved or confirmed (about half a bucket: ambiguous, e.g. the lag is changing)
WHOLE_BUCKET_MARGIN = 0.25
# placement order inside one snapshot: the coarse timeframes first (they decide corrections of the 1m run)
PLACE_RANK: dict[str, int] = {"15m": 0, "5m": 1, "1m": 2, "1d": 3}


def signature(c: dict) -> Sig:
    return (c["open"], c["high"], c["low"], c["close"], c["volume"])


def same_bar(stored: tuple[float, ...], new: tuple[float, ...]) -> bool:
    """True when two snapshots belong to the same TradingView bar: its open never changes, its volume never drops
    and its high / low only widen."""
    if stored[0] != new[0] or new[4] < stored[4]:
        return False
    tol = 1e-9 * max(abs(stored[1]), abs(stored[2]), 1.0)
    return new[1] >= stored[1] - tol and new[2] <= stored[2] + tol


def older_state(stored: tuple[float, ...], new: tuple[float, ...]) -> bool:
    """True when `new` can be an EARLIER state of the bar `stored` (same open, volume not higher, range inside):
    with same_bar() false, a snapshot taken before the one already followed."""
    if stored[0] != new[0] or new[4] > stored[4]:
        return False
    tol = 1e-9 * max(abs(stored[1]), abs(stored[2]), 1.0)
    return new[1] <= stored[1] + tol and new[2] >= stored[2] - tol


def sequenced(tf_s: int) -> bool:
    return tf_s <= SEQUENCED_MAX_S and SEQUENCED_MAX_S % tf_s == 0


def coarse(tf_s: int) -> bool:
    """Bars long enough for the lag (|J| < LAG_MAX_S) to be read from their rollovers without ambiguity."""
    return sequenced(tf_s) and tf_s / 2.0 > LAG_MAX_S


def max_overlap(intervals: Iterable[tuple[float, float]]) -> tuple[float, int] | None:
    """(midpoint, depth) of the widest region covered by the most half-open intervals (lo, hi]; None when empty."""
    ivs = [(float(lo), float(hi)) for lo, hi in intervals if hi > lo]
    if not ivs:
        return None
    coords = sorted({x for iv in ivs for x in iv})
    opens = sorted(lo for lo, _ in ivs)
    closes = sorted(hi for _, hi in ivs)
    segments: list[tuple[float, float, int]] = []
    i = j = depth = 0
    for k in range(len(coords) - 1):
        x = coords[k]
        while i < len(opens) and opens[i] <= x:
            depth += 1
            i += 1
        while j < len(closes) and closes[j] <= x:
            depth -= 1
            j += 1
        segments.append((x, coords[k + 1], depth))
    top = max(d for _, _, d in segments)
    regions: list[list[float]] = []
    for a, b, d in segments:
        if d != top:
            continue
        if regions and regions[-1][1] == a:
            regions[-1][1] = b
        else:
            regions.append([a, b])
    a, b = max(regions, key=lambda r: (r[1] - r[0], r[0]))
    return (a + b) / 2.0, top


def consistent(obs: Iterable[tuple[float, float]], min_obs: int = MIN_CORRECTION_OBS) -> float | None:
    """Lag of one run when most of its rollovers agree (the max overlap covers at least `min_obs` and
    CORRECTION_SHARE of them); None otherwise (too few, or scattered by minutes without trades)."""
    items = list(obs)
    best = max_overlap(items)
    if best is None or best[1] < min_obs or best[1] < CORRECTION_SHARE * len(items):
        return None
    return best[0]


def _plausible(o: tuple[float, float]) -> bool:
    return abs((o[0] + o[1]) / 2.0) <= LAG_MAX_S


@dataclass
class Track:
    """The TradingView bar currently followed for one (symbol, tf)."""

    bucket: int
    sig: Sig
    seen_at: float | None  # asof of the newest snapshot that showed it (None: seeded from the database)
    fixed: bool = True  # False for an earlier bar that was moved away: late copies of it are dropped
    born_at: float | None = None  # asof of its first snapshot (None: seeded from the database)


@dataclass
class Run:
    """Bars of one (symbol, tf) placed one after another since an uncertain start (first snapshot, gap): they are
    moved together when the lag shows them whole buckets off. `obs` = lag intervals of its rollovers."""

    since: int
    end: int
    obs: deque = field(default_factory=lambda: deque(maxlen=LAG_WINDOW))
    # (asof, expected next bucket) when the run ended with the clock one or more buckets ahead of the next bar while
    # the lag was still unknown (1m): checked again once the lag is known (the run was early, or the data was late)
    jump: tuple[float, int] | None = None
    # its own rollovers agreed with the lag (it is in the right buckets): never moved again, and a new TradingView bar
    # after its last one is the next bucket (a later change of the lag, e.g. a clock correction, must not move hours
    # of correctly stamped bars)
    confirmed: bool = False
    # started with the lag already known (placed by the corrected clock): its 1m rollovers are not used to move it
    # (with sparse trading they come late - the first trade of a minute - and would move a correct run)
    placed_known: bool = False


@dataclass
class Placement:
    """Where a snapshot goes. status: first | repeat | same | new | replace | old | drop (bucket None)."""

    bucket: int | None
    status: str
    reason: str = ""  # drop: rollover (session boundary inside our bucket) | order (older than the tracked bar) | old


Seeder = Callable[[str, str], Awaitable[tuple[int, Sig | None]]]
Mover = Callable[[str, str, int, int, int], Awaitable[int]]


class BarStamper:
    """Shared placement state: `await place(symbol, tf, candle, asof)` (see module docstring). `seed(symbol, tf)`
    returns the newest stored recorder bar (time, signature); `move(symbol, tf, start, end, d)` moves the stored
    recorder bars of [start, end] by d seconds and returns how many; `on_shift(symbol, tf, d)` is told when the
    current run (the bar forming now included) moved."""

    def __init__(self, seed: Seeder, move: Mover, on_shift: Callable[[str, str, int], Any] | None = None):
        self._seed = seed
        self._move = move
        self.on_shift = on_shift
        self.tracks: dict[tuple[str, str], Track] = {}
        self._recent: dict[tuple[str, str], deque[Track]] = {}
        self._runs: dict[tuple[str, str], deque[Run]] = {}  # newest first: [current run, earlier runs...]
        self._settled: dict[tuple[str, str], deque] = {}  # (symbol, coarse tf) -> observations of runs no longer kept
        self._lag: dict[str, tuple[float, bool]] = {}  # symbol -> (lag, known)
        # (symbol, tf) -> (bucket, open): the stored daily bar of that bucket belongs to another TradingView bar and
        # the next write of the new one must replace it (whoever writes it: recorder cycle or a closed live bar)
        self._replace: dict[tuple[str, str], tuple[int, float]] = {}
        self._lock = asyncio.Lock()
        self.corrections = 0
        self.moved = 0

    # ------------------------------------------------------------------ lag

    def lag(self, symbol: str) -> float:
        """Estimated lag J of `symbol` in seconds (positive: data later than announced); 0 until several 5m / 15m
        rollovers agree (the announced delay is trusted until then)."""
        cached = self._lag.get(symbol)
        if cached is not None:
            return cached[0]
        ref = self._coarse_estimate(symbol, agreed=True)
        value = (0.0, False) if ref is None else (max(-LAG_MAX_S, min(LAG_MAX_S, ref)), True)
        self._lag[symbol] = value
        return value[0]

    def lag_known(self, symbol: str) -> bool:
        """True once several 5m / 15m rollovers agree on the lag of `symbol` (the clock is then trusted)."""
        self.lag(symbol)
        return self._lag[symbol][1]

    def lags(self) -> dict[str, float]:
        symbols = {k[0] for k in self._settled} | {k[0] for k in self._runs}
        return {s: round(self.lag(s), 1) for s in sorted(symbols) if self.lag_known(s)}

    def _coarse_estimate(self, symbol: str, agreed: bool = False) -> float | None:
        """Lag read from 5m / 15m rollovers (kept runs + settled ones; `agreed`: only when most of them agree).
        Observations of a run that is still whole buckets off (midpoint beyond LAG_MAX_S) are left out until that
        run is corrected. One coarse timeframe per symbol (the finest one followed): a 15m rollover is also a 5m
        rollover seen in the same snapshots, and counting both would make one observation look like two that agree."""
        tfs = sorted({k[1] for k in (*self._runs, *self._settled) if k[0] == symbol and coarse(tf_seconds(k[1]))},
                     key=tf_seconds)
        if not tfs:
            return None
        obs = [o for o in self._settled.get((symbol, tfs[0]), ()) if _plausible(o)]
        for run in self._runs.get((symbol, tfs[0]), ()):
            obs.extend(o for o in run.obs if _plausible(o))
        if agreed:
            return consistent(obs)
        best = max_overlap(obs)
        return best[0] if best else None

    def _start_run(self, key: tuple[str, str], bucket: int) -> None:
        """An uncertain placement starts a new run (the earlier ones are kept a while: they can still be fixed)."""
        runs = self._runs.setdefault(key, deque())
        runs.appendleft(Run(int(bucket), int(bucket), placed_known=self.lag_known(key[0])))
        while len(runs) > RUNS_KEPT:
            old = runs.pop()
            if coarse(tf_seconds(key[1])):
                self._settled.setdefault(key, deque(maxlen=LAG_WINDOW)).extend(o for o in old.obs if _plausible(o))
        self._lag.pop(key[0], None)

    def _current(self, key: tuple[str, str]) -> Run:
        runs = self._runs.get(key)
        if not runs:
            track = self.tracks.get(key)
            self._start_run(key, track.bucket if track is not None else 0)
            runs = self._runs[key]
        return runs[0]

    def forget(self, keep: set[str]) -> None:
        """Drop the state of symbols that are neither recorded nor followed live any more."""
        for key in [k for k in self.tracks if k[0] not in keep]:
            self.tracks.pop(key, None)
            self._recent.pop(key, None)
            self._replace.pop(key, None)
        for key in [k for k in (*self._runs, *self._settled) if k[0] not in keep]:
            self._runs.pop(key, None)
            self._settled.pop(key, None)
        for symbol in [s for s in self._lag if s not in keep]:
            self._lag.pop(symbol, None)

    # ------------------------------------------------------------------ placement

    async def place(self, symbol: str, tf: str, c: dict, asof: float) -> Placement:
        async with self._lock:
            return await self._place(symbol, tf, c, float(asof))

    async def _place(self, symbol: str, tf: str, c: dict, asof: float) -> Placement:
        key = (symbol, tf)
        tf_s = tf_seconds(tf)
        sig = signature(c)
        track = self.tracks.get(key)
        if track is None:
            stored_time, stored_sig = await self._seed(symbol, tf)
            if stored_sig is not None:
                track = Track(int(stored_time), stored_sig, None)
                self.tracks[key] = track
                self._start_run(key, int(stored_time))
        m = asof - self.lag(symbol)
        if track is None:
            bucket = floor_time(int(m), tf)
            self.tracks[key] = Track(bucket, sig, asof, born_at=asof)
            self._start_run(key, bucket)
            return Placement(bucket, "first")
        if sig == track.sig:
            track.seen_at = asof
            return Placement(track.bucket, "repeat")
        if same_bar(track.sig, sig):
            track.sig, track.seen_at = sig, asof
            return Placement(track.bucket, "same")
        since = asof - track.seen_at if track.seen_at is not None else None
        if since is not None and older_state(track.sig, sig) and (
                since <= min(STALE_WINDOW_S, tf_s / 4.0)
                or (since < min(tf_s / 2.0, EARLY_WINDOW_S) and track.sig[4] > 0
                    and sig[4] >= STALE_VOLUME_SHARE * track.sig[4])):
            # an earlier state of the followed bar right after a newer one: a stale answer (overlapping requests of the
            # recorder and the live feed came back out of order). Taken for a new bar, it would push the followed bar,
            # and its whole run with it, into the next bucket.
            return Placement(None, "drop", "stale")
        # a late copy of an earlier bar (lagging server) can only come shortly after the followed bar appeared: the
        # data would have to run later than when that bar was first seen. Later on, the open of an earlier bar is a
        # coincidence (prices move in ticks: a new bar often opens where one of the last ones did), not a copy.
        young = track.born_at is not None and asof - track.born_at <= min(EARLY_WINDOW_S, tf_s / 2.0 + 5.0)
        for old in self._recent.get(key, ()) if young else ():
            if old.sig[0] == sig[0]:  # the open of an earlier bar: a late copy of it (lagging server)
                if old.fixed and same_bar(old.sig, sig):
                    old.sig = sig
                    return Placement(old.bucket, "old")
                return Placement(None, "drop", "old")
        b_clock = floor_time(int(m), tf)
        nxt = track.bucket + tf_s
        if not sequenced(tf_s):  # daily bars: the clock decides (TradingView sessions start inside our UTC day)
            if b_clock >= nxt:
                bucket, status = b_clock, "new"
            elif b_clock == track.bucket and m - track.bucket < min(tf_s / 2.0, EARLY_WINDOW_S):
                bucket, status = track.bucket, "replace"  # the stored bar was the previous day, stamped late
                self._replace[key] = (bucket, sig[0])
            elif b_clock == track.bucket:
                return Placement(None, "drop", "rollover")  # e.g. the forex day starting at 17:00 New York
            else:
                return Placement(None, "drop", "order")
            self._remember(key, track, fixed=status == "new")
            self.tracks[key] = Track(bucket, sig, asof, born_at=asof)
            return Placement(bucket, status)
        known = self.lag_known(symbol)
        tol = LAG_TOLERANCE_KNOWN_S if known else LAG_TOLERANCE_S
        # the followed bar lasted about one bucket (seen from its start until now): the new one is the next bucket
        continuous = (track.seen_at is not None and track.born_at is not None
                      and asof - track.seen_at <= min(OBS_MAX_GAP_S, tf_s)
                      and asof - track.born_at <= tf_s + min(OBS_MAX_GAP_S, tf_s / 2.0))
        # a confirmed run is in the right buckets, and a run placed by the lag-corrected clock is too: a clock jump past
        # the next bucket is then a gap (a minute without trades), never a reason to move it
        current = self._current(key)
        confirmed = current.confirmed
        if b_clock > nxt and continuous and not confirmed and not current.placed_known:
            if known or coarse(tf_s):
                # the clock puts the next bar further on although the followed one lasted a single bucket: the run is
                # early, e.g. its first snapshot already showed the next bar because the data runs ahead of the
                # announced delay (5m / 15m: the lag can never explain a whole bucket; 1m: only once it is known)
                await self._shift_run(key, self._current(key), b_clock - nxt)
                track = self.tracks[key]
                nxt = track.bucket + tf_s
            else:
                self._current(key).jump = (asof, nxt)  # decided once the lag is known (see _reconcile)
        if b_clock >= nxt:
            bucket = b_clock  # next bucket, or a gap (no trades, pause, overnight)
        elif nxt <= floor_time(int(m + tol), tf):
            bucket = nxt  # the data runs a little ahead of the estimate
        elif b_clock == track.bucket and confirmed:
            # the followed bar is in its bucket (confirmed run): a new TradingView bar in the second half of it is the
            # next bucket's bar (the data now runs ahead of the estimate, e.g. the PC clock was corrected); earlier
            # than that it is not trusted yet (the next snapshot decides)
            if m - track.bucket < tf_s / 2.0:
                return Placement(None, "drop", "rollover")
            bucket = nxt
        elif b_clock == track.bucket and (m - track.bucket < min(tf_s / 2.0, EARLY_WINDOW_S)
                                          or (continuous and known and not current.placed_known)):
            # the followed bar was the previous TradingView bar, stamped one bucket late (first snapshot or gap
            # while the data runs late): its whole run moves back one bucket and the new bar takes its place
            await self._shift_run(key, self._current(key), -tf_s)
            track = self.tracks[key]
            nxt = track.bucket + tf_s
            bucket = nxt
        elif b_clock == track.bucket:
            return Placement(None, "drop", "rollover")
        else:
            return Placement(None, "drop", "order")
        self._remember(key, track, fixed=True)
        self.tracks[key] = Track(bucket, sig, asof, born_at=asof)
        if bucket != nxt:
            self._start_run(key, bucket)
            return Placement(bucket, "new")
        run = self._current(key)
        run.end = bucket
        if track.seen_at is not None and 0 < asof - track.seen_at <= OBS_MAX_GAP_S:
            run.obs.append((float(track.seen_at - bucket), float(asof - bucket)))
            self._lag.pop(symbol, None)
            await self._reconcile(symbol)
        return Placement(self.tracks[key].bucket, "new")

    def take_replace(self, symbol: str, tf: str, bucket: int, open_: float) -> bool:
        """True (once) when a bar about to be written at `bucket` must replace the stored one (see `place`)."""
        mark = self._replace.get((symbol, tf))
        if mark is None or mark[0] != int(bucket) or mark[1] != open_:
            return False
        self._replace.pop((symbol, tf), None)
        return True

    def _remember(self, key: tuple[str, str], track: Track, fixed: bool) -> None:
        recent = self._recent.setdefault(key, deque(maxlen=RECENT_BARS))
        recent.appendleft(Track(track.bucket, track.sig, track.seen_at, fixed))

    # ------------------------------------------------------------------ corrections

    async def _reconcile(self, symbol: str) -> None:
        """Whole-bucket errors of the kept runs: coarse runs against their own rollovers (|J| < LAG_MAX_S, so a
        lag of about a whole bucket means the run is off), then the 1m runs against the agreed coarse lag."""
        keys = [k for k in self._runs if k[0] == symbol]
        for key in [k for k in keys if coarse(tf_seconds(k[1]))]:
            tf_s = tf_seconds(key[1])
            for run in list(self._runs.get(key, ())):
                if run.confirmed:
                    continue
                est = consistent(run.obs, min_obs=2)
                if est is None:
                    continue
                n = round(est / tf_s)
                if n:
                    await self._shift_run(key, run, n * tf_s)
                else:
                    run.confirmed = True  # its rollovers agree with a plausible lag: in the right buckets
        ref = self._coarse_estimate(symbol, agreed=True)
        if ref is None:
            return
        for key in [k for k in keys if not coarse(tf_seconds(k[1])) and sequenced(tf_seconds(k[1]))]:
            tf_s = tf_seconds(key[1])
            for run in list(self._runs.get(key, ())):
                if run.confirmed:
                    continue
                n = 0
                if run.jump is not None:
                    # the run ended with a clock jump: with the lag known, was the next bar really further on?
                    at, expected = run.jump
                    run.jump = None
                    n = (floor_time(int(at - ref), key[1]) - expected) // tf_s
                    best = max_overlap(run.obs)
                    if n < 1 or (best is not None and round((best[0] - ref) / tf_s) != n):
                        n = 0  # the data was late (or its rollovers disagree): the run was right
                else:
                    est = consistent(run.obs)
                    if est is not None:
                        off = (est - ref) / tf_s  # buckets off the agreed lag
                        n = round(off)
                        if abs(off - n) > WHOLE_BUCKET_MARGIN:
                            n = 0  # about half a bucket off the agreed lag: ambiguous (the lag is changing), later
                        elif n == 0:
                            run.confirmed = True
                        elif run.placed_known:
                            n = 0  # placed by the lag-corrected clock: late 1m rollovers (sparse trades) do not move it
                if n:
                    await self._shift_run(key, run, n * tf_s)

    async def _shift_run(self, key: tuple[str, str], run: Run, d: int) -> None:
        """Move one run of (symbol, tf) by d seconds, stored bars included (never onto a neighbouring run, never a
        confirmed run; nothing moves when the database refuses: another stored bar in the way)."""
        runs = self._runs.get(key) or deque()
        if not d or run not in runs or run.confirmed:
            return
        i = list(runs).index(run)
        newer = runs[i - 1] if i > 0 else None
        older = runs[i + 1] if i + 1 < len(runs) else None
        if (newer is not None and run.end + d >= newer.since) or (older is not None and run.since + d <= older.end):
            return
        symbol, tf = key
        start, end = int(run.since), int(run.end)
        moved = await self._move(symbol, tf, start, end, int(d))
        if moved is not None and int(moved) < 0:
            return
        run.since, run.end = start + d, end + d
        run.obs = deque(((lo - d, hi - d) for lo, hi in run.obs), maxlen=LAG_WINDOW)
        for old in self._recent.get(key, ()):
            if start <= old.bucket <= end:
                old.bucket += d
        mark = self._replace.get(key)
        if mark is not None and start <= mark[0] <= end:
            self._replace[key] = (mark[0] + d, mark[1])
        current = i == 0
        track = self.tracks.get(key)
        if current and track is not None:
            track.bucket += d
        self._lag.pop(symbol, None)
        self.corrections += 1
        self.moved += int(moved or 0)
        if current and self.on_shift is not None:
            try:
                self.on_shift(symbol, tf, int(d))
            except Exception:
                pass
