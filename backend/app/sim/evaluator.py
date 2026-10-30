"""PredictionEvaluator (`rt.evaluator`): closes the feedback loop.

Every 60 s it resolves due `predictions` (scanner + NEXUS) with real prices, writes the outcome, trains the scanner
model with the stored features (one SGD step per resolved scanner row), resolves `gate_log` decisions by walking
the candles after the decision (TP1 or SL, whichever is touched first; SL first when both in the same bar) and
feeds the gate learner. This is what makes the hit rates honest: predictions are written *before* the outcome is
known and scored afterwards.

Also provides the aggregations behind GET /api/predictions and GET /api/learning/summary.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Iterable

from app import config
from app.sim.common import fmt_prob, json_dumps, json_loads, to_plain
from app.sim.scanner import FEATURE_LABELS, FEATURE_NAMES, OnlineLogit, load_scanner_model, model_key, save_scanner_model
from app.util import RateLimitedLog, clamp, normalize_tf, now_ms, safe_float, tf_seconds

LOOP_INTERVAL_S = 60.0
BATCH_LIMIT = 2000
QUOTE_BATCH = 50
LATE_GROUPS_PER_CYCLE = 25
GATE_GROUPS_PER_CYCLE = 20
GATE_RECHECK_S = 600.0
EXPIRE_AFTER_MS = 7 * 86400 * 1000
GATE_STALE_GRACE_S = 2 * 86400
ROLLING_N = 200


# ----------------------------------------------------------------------------- pure math


def flat_band_pct(horizon_s: float) -> float:
    """|return| (%) below which a neutral prediction (direction 0) counts as correct."""
    hours = max(float(horizon_s), 60.0) / 3600.0
    return float(clamp(0.25 * math.sqrt(hours), 0.1, 1.5))


def outcome_return_pct(price: float, outcome_price: float) -> float | None:
    if price is None or outcome_price is None or price <= 0:
        return None
    return (outcome_price / price - 1.0) * 100.0


def judge_prediction(direction: int, ret_pct: float, horizon_s: float) -> bool:
    """Up/down predictions are right when the sign matches; neutral ones when the move stayed small."""
    if direction > 0:
        return ret_pct > 0
    if direction < 0:
        return ret_pct < 0
    return abs(ret_pct) <= flat_band_pct(horizon_s)


def gate_outcome(
    direction: str,
    entry: float | None,
    sl: float | None,
    tp1: float | None,
    bars: Iterable[tuple[int, float, float, float]],
    decision_time: int,
    target_time_s: int,
    now_s: int,
    tf_s: int,
) -> tuple[str, float | None] | None:
    """Walk the bars after the decision: ('TP1', r) / ('SL', -1) / ('NONE', mark-to-market r) or None (pending).

    Bars are (time, high, low, close) with `time` = bar open (unix s). Only bars opening after the decision bar
    and before `target_time_s` count. When SL and TP1 are both inside the same bar the stop is assumed first.
    """
    if entry is None or sl is None or tp1 is None:
        return ("NONE", None) if now_s >= target_time_s else None
    sign = 1 if direction == "long" else -1
    risk = abs(entry - sl)
    if risk <= 0 or sign * (tp1 - entry) <= 0:
        return ("NONE", None) if now_s >= target_time_s else None
    last_close: float | None = None
    covered_until = decision_time
    for t, high, low, close in bars:
        if t <= decision_time:
            continue
        if t >= target_time_s:
            covered_until = max(covered_until, target_time_s)
            break
        hit_sl = low <= sl if sign > 0 else high >= sl
        hit_tp = high >= tp1 if sign > 0 else low <= tp1
        if hit_sl:
            return "SL", -1.0
        if hit_tp:
            return "TP1", sign * (tp1 - entry) / risk
        last_close = close
        covered_until = max(covered_until, t + tf_s)
    if now_s < target_time_s:
        return None
    if covered_until < target_time_s and now_s < target_time_s + GATE_STALE_GRACE_S:
        return None  # data does not reach the horizon yet; wait a bit for the feed
    r = sign * (last_close - entry) / risk if last_close is not None else None
    return "NONE", r


def gate_won(outcome: str | None, outcome_r: float | None) -> bool | None:
    if outcome == "TP1":
        return True
    if outcome == "SL":
        return False
    if outcome == "NONE" and outcome_r is not None:
        return outcome_r > 0
    return None


# ----------------------------------------------------------------------------- gate learner adapter


def _gate_learner_api() -> tuple[str, Any] | None:
    """Locate the engine's gate learner. Supported shapes (first match wins):

    - class `GateLearner` with `from_state(dict|None)` (or a constructor taking the state), `update(checks, won)`
      and `to_state() -> dict`;
    - a function `learn_gate_outcome(state, checks, won) -> dict` (aliases: update_gate_state, gate_learn).
    `checks` is the gate_log JSON {check_id: 1 | 0 | None}; `won` = TP1 reached before SL.
    """
    try:
        import app.engine.gate as gate_mod
    except Exception:
        return None
    cls = getattr(gate_mod, "GateLearner", None)
    if cls is not None and callable(getattr(cls, "update", None)):
        return "class", cls
    for name in ("learn_gate_outcome", "update_gate_state", "gate_learn"):
        fn = getattr(gate_mod, name, None)
        if callable(fn):
            return "func", fn
    return None


# ----------------------------------------------------------------------------- service


class PredictionEvaluator:
    def __init__(self, rt: Any):
        self.rt = rt
        self._started = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._resolved_total = 0
        self._pending = 0
        self._last_run_ms: int | None = None
        self._gate_checked: dict[tuple[str, str], float] = {}
        self._log = RateLimitedLog()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._started = True
        try:
            self._resolved_total = int(await self.rt.db.fetchval(
                "SELECT COUNT(*) FROM predictions WHERE resolved=1 AND correct IS NOT NULL", default=0))
            self._pending = int(await self.rt.db.fetchval(
                "SELECT COUNT(*) FROM predictions WHERE resolved=0", default=0))
        except Exception:
            pass
        if config.BACKGROUND:
            self._task = asyncio.create_task(self._loop(), name="evaluator-loop")

    async def stop(self) -> None:
        self._started = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def status(self) -> dict:
        return {"running": self._started, "pending": self._pending, "resolved_total": self._resolved_total}

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        if self.rt.bus is not None:
            self.rt.bus.emit(level, "evaluator", message, data)

    async def _loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._log.should_log("loop", 300):
                    self._emit("error", f"Evaluador: {type(exc).__name__}: {exc}")
            await asyncio.sleep(LOOP_INTERVAL_S)

    # ------------------------------------------------------------------ one cycle

    async def run_once(self, now: int | None = None) -> dict:
        """Resolve everything that is due. Returns counters (used by tests and the terminal)."""
        async with self._lock:
            now = int(now if now is not None else now_ms())
            out = {"predictions": 0, "expired": 0, "scanner_updates": 0, "gate": 0}
            try:
                res = await self._resolve_predictions(now)
                out.update(res)
            except Exception as exc:
                if self._log.should_log("predictions", 300):
                    self._emit("warn", f"No se pudieron resolver predicciones: {type(exc).__name__}: {exc}")
            try:
                out["gate"] = await self._resolve_gate(now)
            except Exception as exc:
                if self._log.should_log("gate", 300):
                    self._emit("warn", f"No se pudieron resolver decisiones del filtro: {type(exc).__name__}: {exc}")
            try:
                self._pending = int(await self.rt.db.fetchval(
                    "SELECT COUNT(*) FROM predictions WHERE resolved=0", default=0))
            except Exception:
                pass
            self._last_run_ms = now
            return out

    # ------------------------------------------------------------------ predictions

    async def _price_now(self, symbols: list[str]) -> dict[str, float]:
        hub = self.rt.hub
        prices: dict[str, float] = {}
        if hub is None:
            return prices
        sim = [s for s in symbols if s.startswith("SIM:")]
        real = [s for s in symbols if not s.startswith("SIM:")]
        for s in sim:
            try:
                p = safe_float(await hub.last_price(s))
            except Exception:
                p = None
            if p is not None and p > 0:
                prices[s] = p
        if real and hasattr(hub, "quotes"):
            for i in range(0, len(real), QUOTE_BATCH):
                batch = real[i : i + QUOTE_BATCH]
                try:
                    quotes = await asyncio.wait_for(hub.quotes(batch), timeout=30) or {}
                except Exception as exc:
                    if self._log.should_log("quotes", 300):
                        self._emit("debug", f"Cotizaciones no disponibles para resolver predicciones: {exc}")
                    continue
                for s in batch:
                    q = quotes.get(s) or quotes.get(s.upper())
                    p = safe_float((q or {}).get("price")) if isinstance(q, dict) else safe_float(q)
                    if p is not None and p > 0:
                        prices[s] = p
        return prices

    async def _price_at(self, symbol: str, tf: str, target_ms: int, now: int) -> float | None:
        """Close of the last bar that completed at or before the target time (late resolution)."""
        hub = self.rt.hub
        if hub is None:
            return None
        try:
            tf_n = normalize_tf(tf)
        except ValueError:
            tf_n = "1h"
        tf_s = tf_seconds(tf_n)
        target_s = target_ms // 1000
        need = int((now // 1000 - target_s) / tf_s) + 5
        if need > 1000:
            return None
        try:
            cs = await asyncio.wait_for(hub.get_candles(symbol, tf_n, max(need, 10)), timeout=30)
        except Exception:
            return None
        if cs is None or len(cs) == 0:
            return None
        best = None
        for t, c in zip(cs.time.tolist(), cs.close.tolist()):
            if t + tf_s <= target_s:
                best = c
            else:
                break
        return safe_float(best)

    async def _resolve_predictions(self, now: int) -> dict:
        rows = await self.rt.db.fetchall(
            "SELECT id, source, symbol, tf, model_key, created_at, target_at, price, direction, prob_up, features "
            "FROM predictions WHERE resolved=0 AND target_at <= ? ORDER BY target_at LIMIT ?",
            (now, BATCH_LIMIT),
        )
        if not rows:
            return {"predictions": 0, "expired": 0, "scanner_updates": 0}
        on_time: list[dict] = []
        late: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            horizon_ms = max(int(r["target_at"]) - int(r["created_at"]), 60_000)
            tolerance = max(300_000, int(0.1 * horizon_ms))
            if now - int(r["target_at"]) <= tolerance:
                on_time.append(r)
            else:
                late.setdefault((r["symbol"], r["tf"]), []).append(r)

        resolved: list[tuple[dict, float]] = []
        prices = await self._price_now(sorted({r["symbol"] for r in on_time}))
        for r in on_time:
            p = prices.get(r["symbol"])
            if p is not None:
                resolved.append((r, p))
        expired: list[int] = []
        for i, ((symbol, tf), items) in enumerate(sorted(late.items(), key=lambda kv: kv[1][0]["target_at"])):
            if i >= LATE_GROUPS_PER_CYCLE:
                break
            for r in items:
                p = await self._price_at(symbol, tf, int(r["target_at"]), now)
                if p is not None:
                    resolved.append((r, p))
                elif now - int(r["target_at"]) > EXPIRE_AFTER_MS:
                    expired.append(int(r["id"]))

        updates: list[tuple] = []
        samples: list[tuple[str, dict, int, int | None]] = []
        per_key: dict[str, int] = {}
        nexus_n = 0
        for r, outcome_price in resolved:
            ret = outcome_return_pct(float(r["price"]), outcome_price)
            if ret is None:
                expired.append(int(r["id"]))
                continue
            horizon_s = (int(r["target_at"]) - int(r["created_at"])) / 1000.0
            correct = judge_prediction(int(r["direction"] or 0), ret, horizon_s)
            updates.append((outcome_price, round(ret, 6), 1 if correct else 0, now, int(r["id"])))
            if r["source"] == "scanner" and r.get("model_key"):
                feats = json_loads(r.get("features"), {}) or {}
                x = feats.get("x") if isinstance(feats, dict) and isinstance(feats.get("x"), dict) else feats
                if isinstance(x, dict) and x:
                    samples.append((r["model_key"], x, 1 if ret > 0 else 0, int(r["target_at"]) // 1000))
                    per_key[r["model_key"]] = per_key.get(r["model_key"], 0) + 1
            elif r["source"] == "nexus":
                nexus_n += 1
        if updates:
            await self.rt.db.executemany(
                "UPDATE predictions SET resolved=1, outcome_price=?, outcome_return_pct=?, correct=?, resolved_at=? "
                "WHERE id=?",
                updates,
            )
        if expired:
            await self.rt.db.executemany(
                "UPDATE predictions SET resolved=1, correct=NULL, resolved_at=? WHERE id=?",
                [(now, i) for i in expired],
            )
        self._resolved_total += len(updates)
        if samples:
            await self._train_scanner(samples)
        for key, n in per_key.items():
            hit, brier, total = await self._rolling(model_key_value=key)
            self._emit(
                "learn",
                f"Modelo escáner {key.rsplit('|', 1)[0]}: {n} predicciones resueltas · acierto {fmt_prob(hit, 1)} "
                f"(últimas {total}) · Brier {brier:.3f}" if hit is not None and brier is not None else
                f"Modelo escáner {key.rsplit('|', 1)[0]}: {n} predicciones resueltas",
                {"model_key": key, "resolved": n, "hit_rate": hit, "brier": brier},
            )
        if nexus_n:
            hit, brier, total = await self._rolling(source="nexus")
            if hit is not None:
                self._emit("learn", f"NEXUS: {nexus_n} predicciones resueltas · acierto {fmt_prob(hit, 1)} "
                                    f"(últimas {total})", {"resolved": nexus_n, "hit_rate": hit, "brier": brier})
        return {"predictions": len(updates), "expired": len(expired), "scanner_updates": len(samples)}

    async def _train_scanner(self, samples: list[tuple[str, dict, int, int | None]]) -> None:
        if self.rt.scanner is not None and hasattr(self.rt.scanner, "learn_batch"):
            await self.rt.scanner.learn_batch(samples)
            return
        models: dict[str, OnlineLogit] = {}
        for key, feats, label, label_time in samples:
            model = models.get(key)
            if model is None:
                model = models[key] = await load_scanner_model(self.rt.db, key)
            model.update(model.vector(feats), label, label_time)
        for key, model in models.items():
            await save_scanner_model(self.rt.db, key, model)

    async def _rolling(self, *, model_key_value: str | None = None, source: str | None = None) -> tuple[float | None, float | None, int]:
        where = "resolved=1 AND correct IS NOT NULL AND outcome_return_pct IS NOT NULL"
        params: list[Any] = []
        if model_key_value is not None:
            where += " AND model_key=?"
            params.append(model_key_value)
        if source is not None:
            where += " AND source=?"
            params.append(source)
        rows = await self.rt.db.fetchall(
            f"SELECT correct, prob_up, outcome_return_pct FROM predictions WHERE {where} "
            f"ORDER BY target_at DESC LIMIT {ROLLING_N}",
            params,
        )
        if not rows:
            return None, None, 0
        hit = sum(int(r["correct"]) for r in rows) / len(rows)
        brier = sum((float(r["prob_up"]) - (1.0 if r["outcome_return_pct"] > 0 else 0.0)) ** 2 for r in rows) / len(rows)
        return hit, brier, len(rows)

    # ------------------------------------------------------------------ gate decisions

    async def _resolve_gate(self, now: int) -> int:
        hub = self.rt.hub
        if hub is None:
            return 0
        rows = await self.rt.db.fetchall(
            "SELECT id, symbol, tf, time, direction, verdict, entry, sl, tp1, checks, target_at, created_at "
            "FROM gate_log WHERE resolved=0 ORDER BY time LIMIT 1000"
        )
        if not rows:
            return 0
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            groups.setdefault((r["symbol"], r["tf"]), []).append(r)
        mono = time.monotonic()
        now_s = now // 1000
        updates: list[tuple] = []
        samples: list[tuple[dict, bool]] = []
        checked = 0
        for (symbol, tf), items in sorted(groups.items(), key=lambda kv: kv[1][0]["time"]):
            if checked >= GATE_GROUPS_PER_CYCLE:
                break
            any_due = any(int(r["target_at"] or 0) <= now for r in items)
            last = self._gate_checked.get((symbol, tf))
            if not any_due and last is not None and mono - last < GATE_RECHECK_S:
                continue
            try:
                tf_n = normalize_tf(tf)
            except ValueError:
                tf_n = "1h"
            tf_s = tf_seconds(tf_n)
            oldest = min(int(r["time"]) for r in items)
            need = int(clamp((now_s - oldest) // tf_s + 3, 10, 1500))
            try:
                cs = await asyncio.wait_for(hub.get_candles(symbol, tf_n, need), timeout=30)
            except Exception:
                continue
            checked += 1
            self._gate_checked[(symbol, tf)] = mono
            if cs is None or len(cs) == 0:
                continue
            bars = list(zip(cs.time.tolist(), cs.high.tolist(), cs.low.tolist(), cs.close.tolist()))
            for r in items:
                target_s = int(r["target_at"] or (int(r["created_at"]) + 48 * tf_s * 1000)) // 1000
                entry = safe_float(r["entry"])
                if entry is None:
                    entry = next((c for t, _, _, c in bars if t == int(r["time"])), None)
                res = gate_outcome(r["direction"], entry, safe_float(r["sl"]), safe_float(r["tp1"]), bars,
                                   int(r["time"]), target_s, now_s, tf_s)
                if res is None:
                    continue
                outcome, r_mult = res
                updates.append((outcome, r_mult, now, int(r["id"])))
                won = gate_won(outcome, r_mult)
                checks = json_loads(r["checks"], {}) or {}
                if won is not None and isinstance(checks, dict) and checks and outcome in ("TP1", "SL", "NONE"):
                    samples.append((checks, outcome == "TP1"))
        if updates:
            await self.rt.db.executemany(
                "UPDATE gate_log SET resolved=1, outcome=?, outcome_r=?, resolved_at=? WHERE id=?", updates
            )
        if samples:
            mode = await self._update_gate_learner(samples)
            wins = sum(1 for _, w in samples if w)
            self._emit(
                "learn",
                f"Filtro: {len(updates)} decisiones resueltas · {wins} alcanzaron TP1 antes del SL "
                f"({'pesos del filtro actualizados' if mode == 'engine' else 'estadísticas por chequeo actualizadas'})",
                {"resolved": len(updates), "wins": wins, "mode": mode},
            )
        return len(updates)

    async def _update_gate_learner(self, samples: list[tuple[dict, bool]]) -> str:
        db = self.rt.db
        api = _gate_learner_api()
        if api is not None:
            row = await db.fetchone("SELECT state, updates FROM model_state WHERE kind='gate' AND key='global'")
            state = json_loads(row["state"]) if row else None
            prev = int(row["updates"]) if row and row.get("updates") is not None else 0
            try:
                kind, obj = api
                if kind == "class":
                    if hasattr(obj, "from_state"):
                        learner = obj.from_state(state)
                    else:
                        learner = obj(state) if state is not None else obj()
                    for checks, won in samples:
                        learner.update(checks, won)
                    new_state = learner.to_state() if hasattr(learner, "to_state") else to_plain(learner)
                else:
                    new_state = state
                    for checks, won in samples:
                        new_state = obj(new_state, checks, won)
                if isinstance(new_state, dict):
                    await db.execute(
                        "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                        "VALUES ('gate', 'global', ?, ?, NULL, ?) ON CONFLICT(kind, key) DO UPDATE SET "
                        "state=excluded.state, updates=excluded.updates, updated_at=excluded.updated_at",
                        (json_dumps(new_state), prev + len(samples), now_ms()),
                    )
                    reload_fn = getattr(self.rt.nexus, "reload_gate_state", None) if self.rt.nexus else None
                    if callable(reload_fn):
                        res = reload_fn()
                        if asyncio.iscoroutine(res):
                            await res
                    return "engine"
            except Exception as exc:
                if self._log.should_log("gate-learner", 3600):
                    self._emit("warn", f"Aprendizaje del filtro con la API del motor falló ({type(exc).__name__}: "
                                       f"{exc}); se usan estadísticas por chequeo")
        await self._update_check_counts(samples)
        return "counts"

    async def _update_check_counts(self, samples: list[tuple[dict, bool]]) -> None:
        db = self.rt.db
        row = await db.fetchone("SELECT state FROM model_state WHERE kind='gate' AND key='check_stats'")
        state = json_loads(row["state"], {}) if row else {}
        if not isinstance(state, dict):
            state = {}
        checks_state = state.setdefault("checks", {})
        for checks, won in samples:
            for cid, val in checks.items():
                if val is None:
                    continue
                st = checks_state.setdefault(str(cid), {"pass_win": 0, "pass_loss": 0, "fail_win": 0, "fail_loss": 0})
                k = ("pass_" if val else "fail_") + ("win" if won else "loss")
                st[k] = int(st.get(k, 0)) + 1
        state["samples"] = int(state.get("samples", 0)) + len(samples)
        state["version"] = 1
        await db.execute(
            "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
            "VALUES ('gate', 'check_stats', ?, ?, NULL, ?) ON CONFLICT(kind, key) DO UPDATE SET "
            "state=excluded.state, updates=excluded.updates, updated_at=excluded.updated_at",
            (json_dumps(state), state["samples"], now_ms()),
        )


# ----------------------------------------------------------------------------- aggregations (API)


def _filters(source: str | None, symbol: str | None, extra: str = "") -> tuple[str, list[Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if source:
        where.append("source=?")
        params.append(source)
    if symbol:
        where.append("symbol=?")
        params.append(symbol.upper())
    if extra:
        where.append(extra)
    return " AND ".join(where), params


# Portable SQL (SQLite + PostgreSQL): no arithmetic on boolean expressions, CASE instead.
_UP_EXPR = "CASE WHEN outcome_return_pct > 0 THEN 1.0 ELSE 0.0 END"
_BRIER_EXPR = f"(prob_up - {_UP_EXPR}) * (prob_up - {_UP_EXPR})"
_SCORED = "resolved=1 AND correct IS NOT NULL AND outcome_return_pct IS NOT NULL"


def _bin_expr(bins: int) -> str:
    """CASE ladder mapping prob_up to a calibration bin 0..bins-1 (same result in both SQL dialects)."""
    steps = " ".join(f"WHEN prob_up < {(i + 1) / bins:.6f} THEN {i}" for i in range(bins - 1))
    return f"CASE {steps} ELSE {bins - 1} END"


async def prediction_stats(db: Any, source: str | None = None, symbol: str | None = None) -> dict:
    where, params = _filters(source, symbol)
    row = await db.fetchone(
        f"SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN {_SCORED} THEN 1 ELSE 0 END) AS resolved, "
        f"SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) AS pending, "
        f"AVG(CASE WHEN {_SCORED} THEN correct END) AS hit_rate, "
        f"AVG(CASE WHEN {_SCORED} THEN {_BRIER_EXPR} END) AS brier "
        f"FROM predictions WHERE {where}",
        params,
    ) or {}
    by_rows = await db.fetchall(
        f"SELECT source, SUM(CASE WHEN {_SCORED} THEN 1 ELSE 0 END) AS resolved, "
        f"AVG(CASE WHEN {_SCORED} THEN correct END) AS hit_rate, "
        f"AVG(CASE WHEN {_SCORED} THEN {_BRIER_EXPR} END) AS brier "
        f"FROM predictions WHERE {where} GROUP BY source",
        params,
    )
    where_r, params_r = _filters(source, symbol, _SCORED)
    daily = await db.fetchall(
        f"SELECT (target_at / 86400000) * 86400 AS day, AVG(correct) AS hit_rate, COUNT(*) AS n "
        f"FROM predictions WHERE {where_r} GROUP BY day ORDER BY day DESC LIMIT 90",
        params_r,
    )
    return {
        "total": int(row.get("total") or 0),
        "resolved": int(row.get("resolved") or 0),
        "pending": int(row.get("pending") or 0),
        "hit_rate": safe_float(row.get("hit_rate")),
        "brier": safe_float(row.get("brier")),
        "by_source": {
            r["source"]: {
                "resolved": int(r.get("resolved") or 0),
                "hit_rate": safe_float(r.get("hit_rate")),
                "brier": safe_float(r.get("brier")),
            }
            for r in by_rows
        },
        "rolling": [
            {"time": int(r["day"]), "hit_rate": float(r["hit_rate"]), "n": int(r["n"])}
            for r in reversed(daily)
        ],
    }


def prediction_row(r: dict) -> dict:
    direction = int(r.get("direction") or 0)
    return {
        "id": int(r["id"]),
        "source": r["source"],
        "symbol": r["symbol"],
        "tf": r["tf"],
        "created_at": int(r["created_at"]),
        "target_at": int(r["target_at"]),
        "price": float(r["price"]),
        "direction": 1 if direction > 0 else -1 if direction < 0 else 0,
        "prob_up": float(r["prob_up"]),
        "score": float(r.get("score") or 0.0),
        "resolved": bool(r.get("resolved")),
        "outcome_price": safe_float(r.get("outcome_price")),
        "outcome_return_pct": safe_float(r.get("outcome_return_pct")),
        "correct": None if r.get("correct") is None else bool(r["correct"]),
    }


async def list_predictions(
    db: Any,
    source: str | None = None,
    symbol: str | None = None,
    resolved: bool | None = None,
    limit: int = 200,
) -> list[dict]:
    extra = "" if resolved is None else ("resolved=1" if resolved else "resolved=0")
    where, params = _filters(source, symbol, extra)
    rows = await db.fetchall(
        f"SELECT id, source, symbol, tf, created_at, target_at, price, direction, prob_up, score, resolved, "
        f"outcome_price, outcome_return_pct, correct FROM predictions WHERE {where} ORDER BY id DESC LIMIT ?",
        [*params, int(clamp(limit, 1, 5000))],
    )
    return [prediction_row(r) for r in rows]


async def calibration_bins(db: Any, *, model_key_value: str | None = None, source: str | None = None, bins: int = 10) -> list[dict]:
    where = _SCORED
    params: list[Any] = []
    if model_key_value is not None:
        where += " AND model_key=?"
        params.append(model_key_value)
    if source is not None:
        where += " AND source=?"
        params.append(source)
    rows = await db.fetchall(
        f"SELECT {_bin_expr(bins)} AS bin, AVG(prob_up) AS predicted, AVG({_UP_EXPR}) AS observed, "
        f"COUNT(*) AS n FROM predictions WHERE {where} GROUP BY bin ORDER BY bin",
        params,
    )
    return [
        {"bin": int(r["bin"]), "predicted": float(r["predicted"]), "observed": float(r["observed"]), "count": int(r["n"])}
        for r in rows
    ]


def _state_metric(state: Any, name: str) -> float | None:
    if not isinstance(state, dict):
        return None
    for container in (state, state.get("learning"), state.get("stats"), state.get("perf"), state.get("metrics")):
        if isinstance(container, dict):
            v = safe_float(container.get(name))
            if v is not None:
                return v
    return None


async def learning_summary(db: Any, settings: dict | None = None) -> dict:
    from app.config import DEFAULT_SETTINGS

    settings = settings or DEFAULT_SETTINGS
    markets = settings.get("scanner.markets") or ["crypto"]
    try:
        tf = normalize_tf(settings.get("scanner.tf") or "1h")
    except ValueError:
        tf = "1h"
    preferred = model_key(str(markets[0]).lower(), tf, int(settings.get("scanner.horizon_s") or 14400))

    # --- scanner model
    scanner_model = None
    srow = await db.fetchone(
        "SELECT key, state, updates FROM model_state WHERE kind='scanner' AND key=?", (preferred,)
    ) or await db.fetchone(
        "SELECT key, state, updates FROM model_state WHERE kind='scanner' ORDER BY updates DESC LIMIT 1"
    )
    key = srow["key"] if srow else None
    if key is None and await db.fetchval("SELECT COUNT(*) FROM predictions WHERE model_key=?", (preferred,), default=0):
        key = preferred
    if key is not None:
        model = OnlineLogit.from_state(json_loads(srow["state"]) if srow else None)
        stats = await db.fetchone(
            f"SELECT AVG(correct) AS hit_rate, AVG({_BRIER_EXPR}) AS brier FROM predictions "
            f"WHERE model_key=? AND {_SCORED}",
            (key,),
        ) or {}
        weights = {name: float(model.w[i]) for i, name in enumerate(model.names)}
        features = sorted(
            ({"name": n, "label": FEATURE_LABELS.get(n, n), "weight": round(weights.get(n, 0.0), 6)} for n in FEATURE_NAMES),
            key=lambda f: -abs(f["weight"]),
        )
        scanner_model = {
            "key": key,
            "updates": int(model.updates),
            "hit_rate": safe_float(stats.get("hit_rate")),
            "brier": safe_float(stats.get("brier")),
            "features": features,
            "calibration": await calibration_bins(db, model_key_value=key),
        }

    # --- NEXUS models
    nrows = await db.fetchall(
        "SELECT key, state, updates, trained_until FROM model_state WHERE kind='nexus' ORDER BY updated_at DESC LIMIT 100"
    )
    pstats = await db.fetchall(
        f"SELECT symbol, tf, AVG(correct) AS hit_rate, AVG({_BRIER_EXPR}) AS brier FROM predictions "
        f"WHERE source='nexus' AND {_SCORED} GROUP BY symbol, tf"
    )
    pmap = {(r["symbol"], r["tf"]): r for r in pstats}
    nexus_models = []
    for r in nrows:
        k = str(r["key"])
        symbol, _, ntf = k.rpartition("|")
        if not symbol:
            symbol, ntf = k, ""
        state = json_loads(r["state"])
        hit = _state_metric(state, "hit_rate")
        brier = _state_metric(state, "brier")
        p = pmap.get((symbol, ntf))
        if hit is None and p is not None:
            hit = safe_float(p.get("hit_rate"))
        if brier is None and p is not None:
            brier = safe_float(p.get("brier"))
        tu = safe_float(r.get("trained_until"))
        nexus_models.append({
            "symbol": symbol,
            "tf": ntf,
            "updates": int(r.get("updates") or 0),
            "hit_rate": hit,
            "brier": brier,
            "trained_until": int(tu) if tu is not None else None,
        })

    # --- gate decisions
    judged = "resolved=1 AND (outcome='SL' OR outcome='TP1' OR (outcome='NONE' AND outcome_r IS NOT NULL))"
    lost = "CASE WHEN outcome='SL' OR (outcome='NONE' AND outcome_r <= 0) THEN 1.0 ELSE 0.0 END"
    won = "CASE WHEN outcome='TP1' OR (outcome='NONE' AND outcome_r > 0) THEN 1.0 ELSE 0.0 END"
    g = await db.fetchone(
        "SELECT COUNT(*) AS decisions, "
        "SUM(CASE WHEN verdict='TAKE' THEN 1 ELSE 0 END) AS taken, "
        "SUM(CASE WHEN verdict='AVOID' THEN 1 ELSE 0 END) AS avoided, "
        f"AVG(CASE WHEN verdict='AVOID' AND {judged} THEN {lost} END) AS avoided_lose, "
        f"AVG(CASE WHEN verdict='TAKE' AND {judged} THEN {won} END) AS taken_win "
        "FROM gate_log"
    ) or {}
    gate = {
        "decisions": int(g.get("decisions") or 0),
        "taken": int(g.get("taken") or 0),
        "avoided": int(g.get("avoided") or 0),
        "avoided_would_lose_rate": safe_float(g.get("avoided_lose")),
        "taken_win_rate": safe_float(g.get("taken_win")),
    }

    # --- timeline (all sources, per day)
    trows = await db.fetchall(
        f"SELECT (target_at / 86400000) * 86400 AS day, AVG(correct) AS hit_rate, AVG({_BRIER_EXPR}) AS brier, "
        f"COUNT(*) AS n FROM predictions WHERE {_SCORED} GROUP BY day ORDER BY day DESC LIMIT 90"
    )
    timeline = [
        {"time": int(r["day"]), "hit_rate": float(r["hit_rate"]), "brier": float(r["brier"]), "n": int(r["n"])}
        for r in reversed(trows)
    ]
    return {
        "scanner_model": scanner_model,
        "nexus_models": nexus_models,
        "gate": gate,
        "predictions": await prediction_stats(db),
        "timeline": timeline,
    }
