"""NexusService (`rt.nexus`): NEXUS analysis with long memory (model_state snapshots), the Trade Gate for any idea
and the watchlist watcher (see app.engine.service_watch).

Memory: every run stores the learner snapshots of its bars (merge_snapshots) and starts right after a stored one
(anchor_window), so the history it shows is the replay of one causal path that later runs repeat instead of
recomputing it from a start that moves with every new bar."""
from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from app import config
from app.engine.costs import fee_pct_for
from app.engine.experts import EXPERTS
from app.engine.features import WARMUP_BARS
from app.engine.gate import GateLearner, evaluate_gate
from app.engine.learner import NexusLearner
from app.engine.macro_merge import merge_current, merge_gate, vetoed_side
from app.engine.nexus import NexusParams, run_nexus
from app.engine.nexus_loop import SNAPSHOT_EVERY
from app.engine.nexus_out import empty_stats
from app.engine.service_watch import WatchMixin
from app.util import RateLimitedLog, clean_json, normalize_tf, now_ms, now_s, tf_seconds

CACHE_TTL_S = 5.0
MAX_SNAPSHOTS = 60  # anchor snapshots kept (≈ one per SNAPSHOT_EVERY bars) + the newest 'last' one
SNAPSHOT_MIN_GAP_BARS = 50
MAX_LIMIT = 5000
# bars fetched beyond `limit` so the window can start at the anchor snapshot (anchor_window): the latest anchor
# that leaves `limit` bars lies at most about one snapshot bucket (+ the 50-bar gap after an origin) before that start
ANCHOR_SPAN = 2 * SNAPSHOT_EVERY
ANCHOR_KINDS = ("grid", "origin")
VETO_KIND = "nexus_veto"  # model_state kind of the entries the semáforo vetoed (key "SYMBOL|tf")
MAX_VETOES = 200  # per symbol/timeframe (newest kept)
# a plain live view (no `upto`) stores the veto of a CLOSED last bar only this soon after its close (the bar that just
# closed); later views of a market that sits on an old closed bar (session closed, weekend) veto that view only
VETO_RECORD_GRACE_S = 300


class NexusDataError(RuntimeError):
    """Not enough market data to run the engine (Spanish message)."""


@dataclass
class _Entry:
    created: float
    sig: tuple  # (last bar time, last close, bars fetched, anchor version): see NexusService._sig
    resp: dict
    ctx: Any
    gate_state: dict | None
    params: NexusParams
    upto: int | None = None  # last closed bar requested (None = live view, forming bar included)
    veto_ver: int = 0  # version of the stored vetoes the run used

    def fresh(self, params: NexusParams, upto: int | None, veto_ver: int) -> bool:
        return (time.monotonic() - self.created < CACHE_TTL_S and self.params == params and self.upto == upto
                and self.veto_ver == veto_ver)


def age_text(seconds: float) -> str:
    """Spanish age for messages: '3 días', '1 hora', '25 min'."""
    s = max(int(seconds), 0)
    if s >= 2 * 86400:
        return f"{s // 86400} días"
    if s >= 86400:
        return "1 día"
    if s >= 2 * 3600:
        return f"{s // 3600} horas"
    if s >= 3600:
        return "1 hora"
    return f"{max(s // 60, 1)} min"


def stale_head(cs: Any) -> str:
    """What is stale about a candle set: the hub's note when it says so, else the age of the last bar."""
    note = str(getattr(cs, "note", None) or "").strip()
    low = note.lower()
    if "desactualizad" in low:
        k = low.find("datos desactualizad")
        part = note[k:] if k >= 0 else note
        end = part.find(". ")
        return (part[:end] if end > 0 else part).rstrip(". ")
    if cs is not None and len(cs):
        return f"Datos desactualizados: última vela hace {age_text(now_s() - int(cs.time[-1]))}"
    return "Datos desactualizados"


def stale_message(head: str) -> str:
    return (f"{head}. NEXUS no analiza datos desactualizados: sin señal, niveles ni veredicto del filtro hasta que "
            "lleguen velas nuevas.")


def stale_quick(quick: dict | None, cs: Any, head: str) -> dict | None:
    """The scanner's quick read of a stale market, marked (stale=True + first reason) when it comes from those same
    old candles (flagged by the scanner, or its price is their last close), so it never reads as live."""
    if not isinstance(quick, dict):
        return quick
    price = quick.get("price")
    last = float(cs.close[-1]) if cs is not None and len(cs) else None
    same = (isinstance(price, (int, float)) and last is not None
            and abs(float(price) - last) <= 1e-9 * max(abs(last), 1e-12))
    if not (quick.get("stale") or same):
        return quick
    reasons = [r for r in quick.get("reasons") or [] if isinstance(r, str)]
    if quick.get("stale") and reasons and "desactualizad" in reasons[0].lower():
        return {**quick, "stale": True, "reasons": reasons}  # the scanner already says so first
    return {**quick, "stale": True, "reasons": [f"{head}: esta lectura usa esas velas antiguas"] + reasons}


def snapshot_kind(item: dict) -> str:
    """'grid' | 'origin' | 'last' of a stored snapshot (items stored before snapshot kinds existed count as 'grid')."""
    kind = item.get("kind")
    return kind if kind in ("grid", "origin", "last") else "grid"


def _valid_snapshot(item: Any) -> bool:
    return isinstance(item, dict) and isinstance(item.get("state"), dict) and isinstance(item.get("time"), (int, float))


def _anchor_times(items: list[dict]) -> set[int]:
    return {int(s["time"]) for s in items or [] if _valid_snapshot(s) and snapshot_kind(s) in ANCHOR_KINDS}


def merge_snapshots(old: list[dict], new: list[dict], tf_s: int, max_keep: int = MAX_SNAPSHOTS,
                    min_gap_bars: int = SNAPSHOT_MIN_GAP_BARS) -> list[dict]:
    """Stored NEXUS memory after a run, sorted by time.

    Anchor snapshots (kind 'grid' / 'origin', see `anchor_window`) are write-once: a stored one is never replaced or
    thinned out (only the oldest beyond `max_keep` age out), because later runs start their window right after it and
    must replay the same path; a new one is added only when it lies `min_gap_bars` bars or more from every kept one.
    Plus the newest 'last' snapshot (state at the newest bar, for the learning summary; never an anchor) when it is
    newer than all of them."""
    gap = int(min_gap_bars) * int(tf_s)
    anchors: dict[int, dict] = {}
    last: dict | None = None

    def take_last(t: int, item: dict) -> None:
        nonlocal last
        if last is None or t >= last["time"]:
            last = {"time": t, "state": item["state"], "kind": "last"}

    for item in old or []:
        if not _valid_snapshot(item):
            continue
        t, kind = int(item["time"]), snapshot_kind(item)
        if kind == "last":
            take_last(t, item)
        elif t not in anchors:
            anchors[t] = {"time": t, "state": item["state"], "kind": kind}
    for item in sorted((i for i in new or [] if _valid_snapshot(i)), key=lambda i: int(i["time"])):
        t, kind = int(item["time"]), snapshot_kind(item)
        if kind == "last":
            take_last(t, item)
        elif not any(abs(t - k) < max(gap, 1) for k in anchors):  # write-once, and not crowding a kept one
            anchors[t] = {"time": t, "state": item["state"], "kind": kind}
    kept = sorted(anchors.values(), key=lambda s: s["time"])[-max(int(max_keep), 1):]
    if last is not None and (not kept or last["time"] > kept[-1]["time"]):
        kept.append(last)
    return kept


def pick_prior(snapshots: list[dict], first_time: int) -> dict | None:
    """State of the latest snapshot whose time is <= the first bar of the window (zero lookahead)."""
    best = None
    for snap in snapshots or []:
        t = snap.get("time")
        if isinstance(t, (int, float)) and t <= first_time and (best is None or t > best["time"]):
            best = snap
    return best["state"] if best else None


def anchor_window(times: Any, ref: int, limit: int, snapshots: list[dict],
                  warmup: int = WARMUP_BARS) -> tuple[int, dict | None]:
    """(index of the first bar of the window, anchor snapshot or None) for an analysis of `limit` bars whose newest
    CLOSED bar is `times[ref]`.

    A snapshot holds the state AFTER processing its bar, and the engine processes from window index `warmup` on (the
    bars before only warm the indicators up). So a window starting `warmup − 1` bars before an anchor snapshot
    processes first the bar right after it, with its state as the prior: every later bar is processed exactly once
    (none twice, none skipped), zero lookahead, and runs anchored at the same snapshot replay the same causal path,
    so their past markers and trades do not move as the window slides. The anchor is the latest anchor snapshot
    (kind grid / origin, never newer than `times[ref]`) present in `times` whose window keeps at least `limit` bars
    up to `ref` (the window then has `limit` to `limit` + about one snapshot bucket of bars). None when no stored
    snapshot fits the fetched bars: the plain `limit`-bar window."""
    n = len(times)
    plain = max(int(ref) - int(limit) + 1, 0)
    if n == 0 or ref < 0:
        return plain, None
    newest = int(times[ref])
    for snap in sorted((s for s in snapshots or [] if _valid_snapshot(s) and snapshot_kind(s) in ANCHOR_KINDS),
                       key=lambda s: int(s["time"]), reverse=True):
        t = int(snap["time"])
        if t > newest:
            continue
        a = int(np.searchsorted(times, t))
        if a >= n - 1 or int(times[a]) != t:
            continue  # that bar is not in this data (or nothing would be left to process after it)
        start = a + 1 - int(warmup)
        if start < 0:
            break  # older snapshots start even earlier: not in the fetched bars
        if start <= plain:
            return start, snap
    return plain, None


def _empty_learning(gate_weights: dict) -> dict:
    return {"updates": 0, "hit_rate": None, "brier": None, "weights": {}, "gate_weights": gate_weights,
            "trained_until": None, "memory_bars": 0}


class NexusService(WatchMixin):
    def __init__(self, rt: Any):
        self.rt = rt
        # (symbol, tf, limit) = live view; + ("closed",) = up to a closed bar; + ("veto", time, side) = live view
        # whose forming-bar entry the semáforo vetoed
        self._cache: dict[tuple, _Entry] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        # entries vetoed by the market-conditions check: (symbol, tf) -> {(bar time, direction): veto}
        self._vetoes: dict[tuple[str, str], dict[tuple[int, str], dict]] = {}
        self._veto_ver: dict[tuple[str, str], int] = {}
        # bumped when the stored anchor snapshots of (symbol, tf) change: a cached result keyed by the same last bar
        # may have started its window elsewhere
        self._anchor_ver: dict[tuple[str, str], int] = {}
        self._gate_state: dict | None = None
        self._started = False
        self._analyses = 0
        self._last_error: str | None = None
        self._log = RateLimitedLog()
        self._init_watch()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._started = True
        await self.reload_gate_state()
        if config.BACKGROUND:
            await self.start_watch()

    async def stop(self) -> None:
        await self.stop_watch()
        self._started = False

    def status(self) -> dict:
        return {
            "running": self._started,
            "analyses": self._analyses,
            "cached": len(self._cache),
            "watching": len(self._watch),
            "gate_updates": int((self._gate_state or {}).get("updates") or 0),
            "last_error": self._last_error,
        }

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        if self.rt.bus is not None:
            self.rt.bus.emit(level, "nexus", message, data)

    # ------------------------------------------------------------------ settings / gate state

    def params(self, symbol: str | None = None, **overrides: Any) -> NexusParams:
        """Engine params from the settings; with `symbol`, the commission of its asset class
        (risk.fee_pct_by_class, see app.engine.costs) instead of the single risk.fee_pct."""
        settings = self.rt.settings.all() if self.rt.settings is not None else dict(config.DEFAULT_SETTINGS)
        if symbol and "fee_pct" not in overrides:
            overrides["fee_pct"] = fee_pct_for(symbol, settings)
        return NexusParams.from_settings(settings, **overrides)

    async def reload_gate_state(self) -> None:
        """Re-read the global gate learner (model_state kind='gate', key='global') written by the evaluator."""
        state = None
        if self.rt.db is not None:
            try:
                row = await self.rt.db.fetchone("SELECT state FROM model_state WHERE kind='gate' AND key='global'")
                state = json.loads(row["state"]) if row and row.get("state") else None
            except Exception as exc:
                if self._log.should_log("gate-state", 600):
                    self._emit("warn", f"No se pudo leer el estado del filtro: {exc}")
        self._gate_state = state if isinstance(state, dict) else None
        self._cache.clear()

    def gate_weights(self) -> dict:
        return GateLearner.from_state(self._gate_state).weights()

    # ------------------------------------------------------------------ model persistence

    @staticmethod
    def model_key(symbol: str, tf: str) -> str:
        return f"{symbol}|{tf}"

    async def _load_model(self, symbol: str, tf: str) -> dict:
        if self.rt.db is None:
            return {}
        row = await self.rt.db.fetchone(
            "SELECT state FROM model_state WHERE kind='nexus' AND key=?", (self.model_key(symbol, tf),)
        )
        if not row or not row.get("state"):
            return {}
        try:
            state = json.loads(row["state"])
        except (TypeError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    async def _save_model(self, symbol: str, tf: str, old: dict, res: Any, *, origin: dict | None = None,
                          closed: bool = True) -> None:
        """Persist the run's snapshots (merge_snapshots). `closed` = False when the last bar is still forming: its
        state is provisional, so it is stored as the 'last' snapshot only, never as an anchor. `origin` = the anchor
        to store when the run had none (its own window start, with the prior it used)."""
        if self.rt.db is None or not res.snapshots:
            return
        new = [dict(s) for s in res.snapshots]
        if not closed and new:
            new[-1]["kind"] = "last"
        if origin is not None:
            new.append(origin)
        old_snaps = old.get("snapshots") or []
        snaps = merge_snapshots(old_snaps, new, tf_seconds(tf))
        if _anchor_times(snaps) != _anchor_times(old_snaps):
            self._anchor_ver[(symbol, tf)] = self._anchor_ver.get((symbol, tf), 0) + 1
        lr = res.learning
        learning = {k: lr.get(k) for k in ("updates", "hit_rate", "brier", "trained_until", "memory_bars")}
        state = {"version": 1, "snapshots": snaps, "learning": learning, "hit_rate": lr.get("hit_rate"),
                 "brier": lr.get("brier"), "updates": lr.get("updates")}
        await self.rt.db.execute(
            "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES ('nexus', ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET state=excluded.state, updates=excluded.updates, "
            "trained_until=excluded.trained_until, updated_at=excluded.updated_at",
            (self.model_key(symbol, tf), json.dumps(clean_json(state), separators=(",", ":")),
             int(lr.get("updates") or 0), lr.get("trained_until"), now_ms()),
        )

    # ------------------------------------------------------------------ macro vetoes (persisted)

    async def _vetoes_for(self, symbol: str, tf: str) -> dict[tuple[int, str], dict]:
        """Vetoed entries of symbol/tf (loaded once from model_state kind 'nexus_veto')."""
        key = (symbol, tf)
        got = self._vetoes.get(key)
        if got is not None:
            return got
        entries: dict[tuple[int, str], dict] = {}
        if self.rt.db is not None:
            try:
                row = await self.rt.db.fetchone("SELECT state FROM model_state WHERE kind=? AND key=?",
                                                (VETO_KIND, self.model_key(symbol, tf)))
                state = json.loads(row["state"]) if row and row.get("state") else {}
            except Exception as exc:
                if self._log.should_log(f"veto-load:{symbol}", 600):
                    self._emit("warn", f"No se pudieron leer los vetos de {symbol} {tf}: {exc}")
                return entries  # not cached: retried on the next analysis
            for item in (state.get("entries") if isinstance(state, dict) else None) or []:
                if not isinstance(item, dict) or item.get("direction") not in ("long", "short"):
                    continue
                try:
                    t = int(item["time"])
                except (KeyError, TypeError, ValueError):
                    continue
                entries[(t, item["direction"])] = {
                    "time": t, "direction": item["direction"],
                    "level": "red" if item.get("level") == "red" else "amber",
                    "detail": str(item.get("detail") or ""), "at": item.get("at"),
                }
        return self._vetoes.setdefault(key, entries)

    async def _record_veto(self, symbol: str, tf: str, veto: dict) -> None:
        """Remember an entry the semáforo vetoed at a closed bar (bounded, persisted so a restart keeps it): every
        later run rejects it instead of opening it. Cached results computed without it are invalidated."""
        entries = await self._vetoes_for(symbol, tf)
        k = (int(veto["time"]), str(veto["direction"]))
        if k in entries:
            return
        entries[k] = {"time": k[0], "direction": k[1], "level": "red" if veto.get("level") == "red" else "amber",
                      "detail": str(veto.get("detail") or ""), "at": now_ms()}
        for old in sorted(entries)[: max(len(entries) - MAX_VETOES, 0)]:
            entries.pop(old, None)
        self._veto_ver[(symbol, tf)] = self._veto_ver.get((symbol, tf), 0) + 1
        if self.rt.db is None:
            return
        items = [entries[k2] for k2 in sorted(entries)]
        try:
            await self.rt.db.execute(
                "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, key) DO UPDATE SET state=excluded.state, updates=excluded.updates, "
                "trained_until=excluded.trained_until, updated_at=excluded.updated_at",
                (VETO_KIND, self.model_key(symbol, tf),
                 json.dumps(clean_json({"version": 1, "entries": items}), separators=(",", ":")), len(items), None,
                 now_ms()),
            )
        except Exception as exc:
            if self._log.should_log(f"veto-save:{symbol}", 600):
                self._emit("warn", f"No se pudo guardar el veto de {symbol} {tf}: {exc}")

    # ------------------------------------------------------------------ analysis

    @staticmethod
    def _clean_args(symbol: str, tf: str) -> tuple[str, str]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise ValueError("Falta el símbolo")
        return sym, normalize_tf(tf or "1h")

    async def _quick(self, symbol: str, tf: str) -> dict | None:
        scanner = self.rt.scanner
        if scanner is None or not hasattr(scanner, "quick_signal"):
            return None
        try:
            return await asyncio.wait_for(scanner.quick_signal(symbol, tf), timeout=25)
        except Exception as exc:
            if self._log.should_log(f"quick:{symbol}", 300):
                self._emit("debug", f"Lectura rápida no disponible para {symbol}: {type(exc).__name__}")
            return None

    async def _insufficient(self, cs: Any, symbol: str, tf: str, params: NexusParams) -> dict:
        quick = await self._quick(symbol, tf)
        n = len(cs) if cs is not None else 0
        msg = f"Historial insuficiente para NEXUS: {n} velas de {params.min_bars} necesarias."
        msg += " Se muestra una lectura rápida del escáner." if quick else " No hay lectura rápida disponible."
        return self._not_ok(cs, symbol, tf, params, msg, quick)

    async def _stale(self, cs: Any, symbol: str, tf: str, params: NexusParams) -> dict:
        """ok=false for a candle set whose newest bar is far older than it should be (venue stopped serving the
        pair, feed down): the engine never runs on it, so no signal, levels or gate verdict from old prices."""
        head = stale_head(cs)
        return self._not_ok(cs, symbol, tf, params, stale_message(head),
                            stale_quick(await self._quick(symbol, tf), cs, head))

    def _not_ok(self, cs: Any, symbol: str, tf: str, params: NexusParams, msg: str, quick: dict | None) -> dict:
        n = len(cs) if cs is not None else 0
        times = [int(t) for t in cs.time.tolist()] if n else []
        none = [None] * n
        bars = {"time": times, "score": none, "prob_up": none, "action": ["WAIT"] * n, "regime": ["unknown"] * n,
                "ema_fast": none, "ema_slow": none, "ema_trend": none, "pos_entry": none, "pos_sl": none,
                "pos_tp1": none, "pos_tp2": none, "pos_tp3": none}
        return clean_json({
            "symbol": symbol, "tf": tf, "provider": getattr(cs, "provider", "") or "",
            "history": getattr(cs, "history", "none") or "none", "note": getattr(cs, "note", None),
            "ok": False, "message": msg, "params": params.to_dict(), "bars": bars, "markers": [], "trades": [],
            "current": None, "prediction": None, "learning": _empty_learning(self.gate_weights()),
            "stats": empty_stats(), "quick": quick,
        })

    async def _fetch(self, symbol: str, tf: str, n: int) -> Any:
        hub = self.rt.hub
        if hub is None:
            raise NexusDataError("Los datos de mercado no están disponibles (servicio de datos detenido)")
        try:
            return await hub.get_candles(symbol, tf, n)
        except ValueError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "ProviderError":
                raise ValueError(str(exc)) from None
            raise NexusDataError(f"No se pudieron obtener velas de {symbol}: {exc}") from exc

    async def _candles(self, symbol: str, tf: str, lim: int, upto: int | None) -> Any:
        """Hub candles for an analysis of `lim` bars: ANCHOR_SPAN more (room for the window to start at its anchor
        snapshot) plus the forming bar. With `upto` the bars after it are dropped, re-fetching when more than the
        forming bar was dropped, so the closed bar has as many bars before it as in a closed-only fetch: the same
        window start and length as an analysis without the forming bar."""
        want = min(lim + ANCHOR_SPAN + 1, MAX_LIMIT)
        for _ in range(2):
            cs = await self._fetch(symbol, tf, want)
            n = len(cs) if cs is not None else 0
            if upto is None or not n:
                return cs
            last = int(np.searchsorted(cs.time, upto, side="right")) - 1
            dropped = n - 1 - last
            if dropped > 0:
                cs = cs.upto(last)
            if dropped <= 1 or n < want or want >= MAX_LIMIT:
                return cs
            want = min(want + dropped - 1, MAX_LIMIT)
        return cs

    async def _compute(self, symbol: str, tf: str, limit: int | None = None, force: bool = False, *,
                       upto: int | None = None, last_veto: dict | None = None) -> _Entry:
        """Engine result, cached CACHE_TTL_S and then by last bar.

        `upto` = open time (unix s) of the last CLOSED bar to use: later (still forming) bars are dropped before
        the engine runs, so a bar-close decision is taken on the bar that closed; that result is cached apart from
        the live view (which includes the forming bar). `last_veto` = a macro veto of the last bar's entry that
        applies to this view only (see `_resolve`); stored vetoes always apply.

        Window: the `limit` bars up to the newest closed bar (+ the forming bar in a live view), started instead
        right after the stored anchor snapshot that `anchor_window` picks, so consecutive runs replay the same path
        and history does not repaint as bars arrive (it only moves when the anchor steps to the next snapshot, about
        every SNAPSHOT_EVERY bars). A run without an anchor stores its own window start as one ('origin')."""
        symbol, tf = self._clean_args(symbol, tf)
        params = self.params(symbol)
        lim = int(limit) if limit else params.default_limit
        lim = max(min(lim, MAX_LIMIT), params.min_bars)
        upto = None if upto is None else int(upto)
        key: tuple = (symbol, tf, lim) + (("closed",) if upto is not None else ())
        if last_veto is not None:
            key += ("veto", int(last_veto["time"]), str(last_veto["direction"]))
        vetoes = await self._vetoes_for(symbol, tf)
        hit = self._cache.get(key)
        if hit is not None and not force and hit.fresh(params, upto, self._veto_ver.get((symbol, tf), 0)):
            return hit
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit is not None and not force and hit.fresh(params, upto, self._veto_ver.get((symbol, tf), 0)):
                return hit
            cs = await self._candles(symbol, tf, lim, upto)
            n = len(cs) if cs is not None else 0
            sig = self._sig(symbol, tf, cs)
            ver = self._veto_ver.get((symbol, tf), 0)
            if (hit is not None and hit.sig == sig and hit.params == params and hit.resp.get("ok")
                    and hit.upto == upto and hit.veto_ver == ver):
                hit.created = time.monotonic()  # same bars and anchors: the result cannot change
                return hit
            # the newest CLOSED bar sets the window (a live view adds the forming bar), so the live view and the
            # bar-close decision of the same moment share their window start and anchor
            closed = n > 0 and (upto is not None or int(cs.time[-1]) + tf_seconds(tf) <= now_s())
            ref = n - 1 if closed or n < 2 else n - 2
            plain = cs.slice(max(ref - lim + 1, 0)) if n else cs
            if n and getattr(cs, "stale", False):
                entry = _Entry(time.monotonic(), sig, await self._stale(plain, symbol, tf, params), None, None,
                               params, upto, ver)
            elif len(plain) < params.min_bars:
                entry = _Entry(time.monotonic(), sig, await self._insufficient(plain, symbol, tf, params), None, None,
                               params, upto, ver)
            else:
                old = await self._load_model(symbol, tf)
                snaps = old.get("snapshots") or []
                start, anchor = anchor_window(cs.time, ref, lim, snaps)
                win = cs.slice(start)
                prior, origin = (anchor["state"] if anchor is not None else pick_prior(snaps, int(win.time[0]))), None
                if anchor is None and len(win) > WARMUP_BARS:
                    # the bar before the first processed one: later runs anchored here start at this same bar
                    origin = {"time": int(win.time[WARMUP_BARS - 1]), "state": prior or {}, "kind": "origin"}
                t0, t1 = int(win.time[0]), int(win.time[-1])
                window = [dict(v) for v in vetoes.values() if t0 <= v["time"] <= t1]
                if last_veto is not None:
                    window.append(dict(last_veto))
                entry = await self._run(win, symbol, tf, params, window, old=old, prior=prior, origin=origin,
                                        closed=closed)
                entry.sig = self._sig(symbol, tf, cs)  # with the anchors this run stored
                entry.upto, entry.veto_ver = upto, ver
            self._cache[key] = entry
            if len(self._cache) > 200:
                oldest = min(self._cache, key=lambda k: self._cache[k].created)
                self._cache.pop(oldest, None)
                old_lock = self._locks.get(oldest)
                if old_lock is not None and not old_lock.locked():
                    self._locks.pop(oldest, None)
            return entry

    def _sig(self, symbol: str, tf: str, cs: Any) -> tuple:
        """What a cached result depends on besides params / upto / vetoes: the fetched bars and the stored anchors."""
        n = len(cs) if cs is not None else 0
        ver = self._anchor_ver.get((symbol, tf), 0)
        return (int(cs.time[-1]), float(cs.close[-1]), n, ver) if n else (0, 0.0, 0, ver)

    async def _run(self, cs: Any, symbol: str, tf: str, params: NexusParams, vetoed: list[dict] | None = None, *,
                   old: dict, prior: dict | None, origin: dict | None = None, closed: bool = True) -> _Entry:
        gate_state = copy.deepcopy(self._gate_state)
        try:
            res = await asyncio.to_thread(run_nexus, cs, params, prior, gate_state=gate_state, vetoed_entries=vetoed)
        except Exception as exc:
            if prior is None:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
            if self._log.should_log(f"prior:{symbol}|{tf}", 600):
                self._emit("warn", f"Memoria NEXUS de {symbol} {tf} incompatible; se reinicia ({type(exc).__name__})")
            res = await asyncio.to_thread(run_nexus, cs, params, None, gate_state=gate_state, vetoed_entries=vetoed)
        self._analyses += 1
        try:
            await self._save_model(symbol, tf, old, res, origin=origin, closed=closed)
        except Exception as exc:
            if self._log.should_log(f"save:{symbol}", 600):
                self._emit("warn", f"No se pudo guardar la memoria NEXUS de {symbol} {tf}: {exc}")
        resp = res.to_response(cs)
        quick = None if res.ok else await self._quick(symbol, tf)
        resp.update({"symbol": symbol, "tf": tf, "provider": cs.provider or "", "note": cs.note, "quick": quick})
        return _Entry(time.monotonic(), (), clean_json(resp), res.context, res.decision_gate, params)

    # ------------------------------------------------------------------ market conditions (semáforo)

    async def _macro_checks(self, symbol: str) -> dict[str, dict | None]:
        """rt.macro.gate_check for both sides (None when the macro service is missing or fails)."""
        macro = getattr(self.rt, "macro", None)
        out: dict[str, dict | None] = {"long": None, "short": None}
        if macro is None or not hasattr(macro, "gate_check") or not symbol:
            return out
        for side in ("long", "short"):
            try:
                out[side] = await asyncio.wait_for(macro.gate_check(symbol, side), timeout=5)
            except Exception:
                out[side] = None
        return out

    @staticmethod
    def _merge_macro(resp: dict, checks: dict[str, dict | None], take_score: float) -> dict:
        """Response with the macro check merged into current (and its gates). The cached dict is not modified."""
        if not resp.get("ok") or not resp.get("current") or not (checks["long"] or checks["short"]):
            return resp
        out = dict(resp)
        out["current"] = merge_current(resp["current"], checks, take_score)
        return out

    async def _resolve(self, symbol: str, tf: str, limit: int | None = None, *, force: bool = False,
                       upto: int | None = None) -> dict:
        """NexusResponse with the market-conditions check (semáforo) merged into `current`.

        When the semáforo vetoes the BUY/SELL of the last bar, the engine is re-run with that veto, so the vetoed
        entry opens no position: no position block or SL/TP lines now, and no MANTENER/CERRAR advice for it on later
        bars. The veto of a real closed-bar decision is stored (model_state kind 'nexus_veto', survives restarts) and
        applies to every later run: a run with `upto` (the bar-close watcher, the auto-trader), or a live view whose
        last bar is the bar that JUST closed (within VETO_RECORD_GRACE_S, at most one bar, of its close). Any other
        veto applies to this view only: a still-forming bar is decided again, with the semáforo of that moment, when
        it closes, and a market that sits on an old closed bar (session closed, weekend) keeps the decision taken at
        that close, which the semáforo of a later moment must not rewrite."""
        entry = await self._compute(symbol, tf, limit, force, upto=upto)
        resp = entry.resp
        if not resp.get("ok") or not resp.get("current"):
            return resp
        checks = await self._macro_checks(str(resp.get("symbol") or symbol))
        take_score = float(entry.params.take_score)
        vetoed = vetoed_side(resp["current"], checks, take_score)
        if vetoed is not None:
            direction, check = vetoed
            t = int(resp["current"]["time"])
            veto = {"time": t, "direction": direction, "level": "red" if check.get("critical") else "amber",
                    "detail": str(check.get("detail") or "Condiciones de mercado adversas")}
            tf_s = tf_seconds(str(resp.get("tf") or tf))
            closed_ago = now_s() - (t + tf_s)  # < 0 while the bar is still forming
            if upto is not None or 0 <= closed_ago <= min(tf_s, VETO_RECORD_GRACE_S):
                await self._record_veto(str(resp["symbol"]), str(resp["tf"]), veto)
                entry = await self._compute(symbol, tf, limit, upto=upto)
            else:
                entry = await self._compute(symbol, tf, limit, upto=upto, last_veto=veto)
            resp = entry.resp
        return self._merge_macro(resp, checks, take_score)

    async def analyze(self, symbol: str, tf: str, limit: int | None = None, *, upto: int | None = None) -> dict:
        """NexusResponse (cached 5 s and by last bar) with the market-conditions check merged into `current`.
        `upto` = open time of the last closed bar to analyse (forming bars dropped; see `_compute`).
        Treat it as read-only: unchanged parts are shared with the cache."""
        return await self._resolve(symbol, tf, limit, upto=upto)

    async def current(self, symbol: str, tf: str, *, upto: int | None = None) -> dict | None:
        """`current` of `analyze` (None without a usable result). On a bar close pass `upto` = that bar's open
        time, so the decision is the one taken at its close, not on the seconds-old forming bar."""
        try:
            resp = await self._resolve(symbol, tf, upto=upto)
        except (NexusDataError, ValueError):
            return None
        return resp.get("current") if resp.get("ok") else None

    async def gate(self, symbol: str, tf: str, direction: str, entry: float | None = None, sl: float | None = None,
                   tp: float | None = None) -> dict:
        """GateResult for `direction` at the last bar (entry = last close unless the user gives entry/SL/TP)."""
        direction = str(direction or "").strip().lower()
        if direction in ("buy", "compra", "largo", "l"):
            direction = "long"
        elif direction in ("sell", "venta", "corto", "s"):
            direction = "short"
        if direction not in ("long", "short"):
            raise ValueError("La dirección debe ser 'long' o 'short'")
        entry_obj = await self._compute(symbol, tf)
        if not entry_obj.resp.get("ok") or entry_obj.ctx is None:
            raise NexusDataError(entry_obj.resp.get("message") or "Historial insuficiente para evaluar el filtro")
        if entry is None and sl is None and tp is None:
            result = copy.deepcopy(entry_obj.resp["current"]["gate"][direction])
        else:
            result = clean_json(evaluate_gate(entry_obj.ctx, direction, entry_obj.params, entry=entry, sl=sl, tp=tp,
                                              gate_state=entry_obj.gate_state))
        checks = await self._macro_checks(str(entry_obj.resp.get("symbol") or symbol))
        return merge_gate(result, checks[direction], float(entry_obj.params.take_score)) or result

    # ------------------------------------------------------------------ model / reset

    async def model(self, symbol: str, tf: str) -> dict:
        """NexusModel: learning summary, calibration bins, rolling accuracy and the expert list."""
        symbol, tf = self._clean_args(symbol, tf)
        state = await self._load_model(symbol, tf)
        if not state.get("snapshots"):
            try:
                await self._compute(symbol, tf)
            except (NexusDataError, ValueError):
                pass
            state = await self._load_model(symbol, tf)
        snaps = state.get("snapshots") or []
        latest = max(snaps, key=lambda s: s["time"])["state"] if snaps else {}
        learner = NexusLearner.from_state(latest.get("learner") if isinstance(latest, dict) else None)
        gate_w = GateLearner.from_state(self._gate_state or latest.get("gate")).weights()
        learning = {
            "updates": int(learner.updates),
            "hit_rate": None if learner.hit_rate is None else round(learner.hit_rate, 4),
            "brier": None if learner.brier is None else round(learner.brier, 4),
            "weights": learner.weights_dict() if learner.updates else {},
            "gate_weights": gate_w,
            "trained_until": learner.trained_until,
            "memory_bars": int(learner.memory_bars),
        }
        rolling = [{"time": int(r[0]), "hit_rate": float(r[1]), "brier": float(r[2])} for r in learner.rolling]
        experts = [{"id": e["id"], "label": e["label"], "description": e["description"]} for e in EXPERTS]
        return clean_json({"symbol": symbol, "tf": tf, "learning": learning, "calibration": learner.calibration(),
                           "rolling": rolling, "experts": experts})

    async def reset(self, symbol: str, tf: str) -> None:
        symbol, tf = self._clean_args(symbol, tf)
        if self.rt.db is not None:
            await self.rt.db.execute("DELETE FROM model_state WHERE kind='nexus' AND key=?", (self.model_key(symbol, tf),))
        self._anchor_ver[(symbol, tf)] = self._anchor_ver.get((symbol, tf), 0) + 1
        for key in [k for k in self._cache if k[0] == symbol and k[1] == tf]:
            self._cache.pop(key, None)
        self._emit("learn", f"Memoria NEXUS de {symbol} {tf} reiniciada")
