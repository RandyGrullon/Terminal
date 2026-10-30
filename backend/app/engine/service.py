"""NexusService (`rt.nexus`): NEXUS analysis with long memory (model_state snapshots), the Trade Gate for any idea
and the watchlist watcher (see app.engine.service_watch)."""
from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass
from typing import Any

from app import config
from app.engine.experts import EXPERTS
from app.engine.gate import GateLearner, evaluate_gate
from app.engine.learner import NexusLearner
from app.engine.macro_merge import merge_current, merge_gate
from app.engine.nexus import NexusParams, run_nexus
from app.engine.nexus_out import empty_stats
from app.engine.service_watch import WatchMixin
from app.util import RateLimitedLog, clean_json, normalize_tf, now_ms, tf_seconds

CACHE_TTL_S = 5.0
MAX_SNAPSHOTS = 60
SNAPSHOT_MIN_GAP_BARS = 50
MAX_LIMIT = 5000


class NexusDataError(RuntimeError):
    """Not enough market data to run the engine (Spanish message)."""


@dataclass
class _Entry:
    created: float
    sig: tuple
    resp: dict
    ctx: Any
    gate_state: dict | None
    params: NexusParams


def merge_snapshots(old: list[dict], new: list[dict], tf_s: int, max_keep: int = MAX_SNAPSHOTS,
                    min_gap_bars: int = SNAPSHOT_MIN_GAP_BARS) -> list[dict]:
    """Union by time (new wins), thinned to one snapshot per `min_gap_bars` bars, newest first, ≤ max_keep."""
    by_time: dict[int, dict] = {}
    for item in list(old or []) + list(new or []):
        if isinstance(item, dict) and isinstance(item.get("state"), dict) and isinstance(item.get("time"), (int, float)):
            by_time[int(item["time"])] = {"time": int(item["time"]), "state": item["state"]}
    kept: list[dict] = []
    for snap in sorted(by_time.values(), key=lambda s: s["time"], reverse=True):
        if not kept or kept[-1]["time"] - snap["time"] >= min_gap_bars * tf_s:
            kept.append(snap)
            if len(kept) >= max_keep:
                break
    return sorted(kept, key=lambda s: s["time"])


def pick_prior(snapshots: list[dict], first_time: int) -> dict | None:
    """State of the latest snapshot whose time is <= the first bar of the window (zero lookahead)."""
    best = None
    for snap in snapshots or []:
        t = snap.get("time")
        if isinstance(t, (int, float)) and t <= first_time and (best is None or t > best["time"]):
            best = snap
    return best["state"] if best else None


def _empty_learning(gate_weights: dict) -> dict:
    return {"updates": 0, "hit_rate": None, "brier": None, "weights": {}, "gate_weights": gate_weights,
            "trained_until": None, "memory_bars": 0}


class NexusService(WatchMixin):
    def __init__(self, rt: Any):
        self.rt = rt
        self._cache: dict[tuple[str, str, int], _Entry] = {}
        self._locks: dict[tuple[str, str, int], asyncio.Lock] = {}
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

    def params(self, **overrides: Any) -> NexusParams:
        settings = self.rt.settings.all() if self.rt.settings is not None else dict(config.DEFAULT_SETTINGS)
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

    async def _save_model(self, symbol: str, tf: str, old: dict, res: Any) -> None:
        if self.rt.db is None or not res.snapshots:
            return
        snaps = merge_snapshots(old.get("snapshots") or [], res.snapshots, tf_seconds(tf))
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

    async def _compute(self, symbol: str, tf: str, limit: int | None = None, force: bool = False) -> _Entry:
        symbol, tf = self._clean_args(symbol, tf)
        params = self.params()
        lim = int(limit) if limit else params.default_limit
        lim = max(min(lim, MAX_LIMIT), params.min_bars)
        key = (symbol, tf, lim)
        hit = self._cache.get(key)
        if hit is not None and not force and time.monotonic() - hit.created < CACHE_TTL_S and hit.params == params:
            return hit
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit is not None and not force and time.monotonic() - hit.created < CACHE_TTL_S and hit.params == params:
                return hit
            hub = self.rt.hub
            if hub is None:
                raise NexusDataError("Los datos de mercado no están disponibles (servicio de datos detenido)")
            try:
                cs = await hub.get_candles(symbol, tf, lim)
            except ValueError:
                raise
            except Exception as exc:
                if type(exc).__name__ == "ProviderError":
                    raise ValueError(str(exc)) from None
                raise NexusDataError(f"No se pudieron obtener velas de {symbol}: {exc}") from exc
            n = len(cs) if cs is not None else 0
            sig = (int(cs.time[-1]), float(cs.close[-1]), n) if n else (0, 0.0, 0)
            if hit is not None and hit.sig == sig and hit.params == params and hit.resp.get("ok"):
                hit.created = time.monotonic()  # same last bar: the result cannot change
                return hit
            if n < params.min_bars:
                entry = _Entry(time.monotonic(), sig, await self._insufficient(cs, symbol, tf, params), None, None, params)
                self._cache[key] = entry
                return entry
            entry = await self._run(cs, symbol, tf, params, sig)
            self._cache[key] = entry
            if len(self._cache) > 200:
                oldest = min(self._cache, key=lambda k: self._cache[k].created)
                self._cache.pop(oldest, None)
                old_lock = self._locks.get(oldest)
                if old_lock is not None and not old_lock.locked():
                    self._locks.pop(oldest, None)
            return entry

    async def _run(self, cs: Any, symbol: str, tf: str, params: NexusParams, sig: tuple) -> _Entry:
        old = await self._load_model(symbol, tf)
        prior = pick_prior(old.get("snapshots") or [], int(cs.time[0]))
        gate_state = copy.deepcopy(self._gate_state)
        try:
            res = await asyncio.to_thread(run_nexus, cs, params, prior, gate_state=gate_state)
        except Exception as exc:
            if prior is None:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
            if self._log.should_log(f"prior:{symbol}|{tf}", 600):
                self._emit("warn", f"Memoria NEXUS de {symbol} {tf} incompatible; se reinicia ({type(exc).__name__})")
            res = await asyncio.to_thread(run_nexus, cs, params, None, gate_state=gate_state)
        self._analyses += 1
        try:
            await self._save_model(symbol, tf, old, res)
        except Exception as exc:
            if self._log.should_log(f"save:{symbol}", 600):
                self._emit("warn", f"No se pudo guardar la memoria NEXUS de {symbol} {tf}: {exc}")
        resp = res.to_response(cs)
        quick = None if res.ok else await self._quick(symbol, tf)
        resp.update({"symbol": symbol, "tf": tf, "provider": cs.provider or "", "note": cs.note, "quick": quick})
        return _Entry(time.monotonic(), sig, clean_json(resp), res.context, res.decision_gate, params)

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

    async def _with_macro(self, resp: dict) -> dict:
        """Response with the macro check merged into current (and its gates). The cached dict is not modified."""
        if not resp.get("ok") or not resp.get("current"):
            return resp
        checks = await self._macro_checks(str(resp.get("symbol") or ""))
        if not (checks["long"] or checks["short"]):
            return resp
        out = dict(resp)
        out["current"] = merge_current(resp["current"], checks, float(self.params().take_score))
        return out

    async def analyze(self, symbol: str, tf: str, limit: int | None = None) -> dict:
        """NexusResponse (cached 5 s and by last bar) with the market-conditions check merged into `current`.
        Treat it as read-only: unchanged parts are shared with the cache."""
        return await self._with_macro((await self._compute(symbol, tf, limit)).resp)

    async def current(self, symbol: str, tf: str) -> dict | None:
        try:
            resp = await self._with_macro((await self._compute(symbol, tf)).resp)
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
        for key in [k for k in self._cache if k[0] == symbol and k[1] == tf]:
            self._cache.pop(key, None)
        self._emit("learn", f"Memoria NEXUS de {symbol} {tf} reiniciada")
