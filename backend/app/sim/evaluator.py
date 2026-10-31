"""PredictionEvaluator (`rt.evaluator`): closes the feedback loop.

Every 60 s it resolves due `predictions` (scanner + NEXUS) with real prices, writes the outcome, trains the scanner
model with the stored features (one structured update per resolved scan: app.sim.scanner.ScannerModel), resolves
`gate_log` decisions by walking the candles after the decision (TP1 or SL, whichever is touched first; SL first when
both in the same bar) and feeds the gate learner. This is what makes the hit rates honest: predictions are written
*before* the outcome is known and scored afterwards.

Predictions have three outcomes (up, down, neutral = |return| inside the flat band), so 50 % is not the chance
level: `skill_report` compares the hit rate with the reference (always predicting the most frequent outcome of the
period, and the chance level of the same mix of calls) and the Brier score with climatology (the period's up rate),
with 95 % intervals clustered by independent horizon windows (rows of one window move together; neighbouring windows
are correlated too: Newey–West term). The scanner's compact feature vectors stay `scanner.replay_days` after
resolution (default 7) for `replay_report`, an offline prequential replay that measures model designs on the same
data (streamed in chunks: its memory does not grow with the number of rows).

Outcomes that carry no information are resolved without a verdict (correct NULL) and never train anything: a move
too small to measure (closed session, frozen price), no bar traded between the prediction and its target, or only a
stale quote (those wait for the candles instead). Simulated gate decisions (simulator sessions, SIM:* symbols) are
resolved but never feed the global gate learner, and one decision logged by several sources is learned once.

Also provides the aggregations behind GET /api/predictions and GET /api/learning/summary. Totals, hit rates, Brier,
calibration and the daily charts are read from `prediction_daily` (per-day aggregates written in the same
transaction that resolves the rows), so they stay cheap and survive the pruning of old rows (app.maintenance).
"""
from __future__ import annotations

import asyncio
import importlib
import itertools
import math
import time
import weakref
from collections import deque
from typing import Any, Iterable

from app import config
from app.sim.common import fmt_num, fmt_prob, json_dumps, json_loads, to_plain
from app.sim.scanner import (
    CONTEXT_NAMES,
    FEATURE_NAMES,
    LearnSample,
    OnlineLogit,
    ScannerModel,
    action_for,
    context_regime,
    decide_direction,
    decode_features,
    evaluate_features,
    flat_band_pct,
    market_context,
    model_key,
    parse_model_key,
    train_scanner_models,
)
from app.util import RateLimitedLog, TTLCache, clamp, floor_time, normalize_tf, now_ms, safe_float, tf_seconds

LOOP_INTERVAL_S = 60.0
BATCH_LIMIT = 2000
QUOTE_BATCH = 50
QUOTE_MAX_AGE_MS = 120_000  # an older quote is not a live price: the row is resolved later from the candles
LATE_GROUPS_PER_CYCLE = 25
GATE_GROUPS_PER_CYCLE = 20
GATE_RECHECK_S = 600.0
GATE_FETCH_MAX = 1500  # bars fetched to walk gate decisions (older decisions cannot be judged)
EXPIRE_AFTER_MS = 7 * 86400 * 1000
GATE_STALE_GRACE_S = 2 * 86400
ROLLING_N = 200
CALIBRATION_BINS = 10
DAILY_META_KEY = "prediction_daily"  # meta flag: resolved rows that predate the aggregates table are folded in
DAILY_VERSION = "1"
# Resolved scanner rows keep their packed feature vector this long for the offline replay (0 = drop it at
# resolution). A week holds 42 independent 4 h windows, more than the MIN_WINDOWS a conclusion needs; at ≈ 0,2 KB per
# row the default background scan (≈ 16 k rows a day) keeps ≈ 3 MB a day, ≈ 20 MB for the week.
REPLAY_DAYS_DEFAULT = 7
FEATURE_PRUNE_EVERY_MS = 3_600_000


# ----------------------------------------------------------------------------- pure math
# flat_band_pct (imported from app.sim.scanner, the model needs it too): |return| (%) below which a neutral prediction
# counts as correct.


def dead_band_pct(horizon_s: float) -> float:
    """|return| (%) at or below which the move is not measurable (≈ 1 basis point at 4 h): a closed session, a frozen
    quote or tick noise. Such an outcome gets no verdict (correct NULL) and is not a training label."""
    return 0.02 * flat_band_pct(horizon_s)


def measurable(ret_pct: float, horizon_s: float) -> bool:
    return abs(ret_pct) > dead_band_pct(horizon_s)


def prob_bin(p: float, bins: int = CALIBRATION_BINS) -> int:
    """Calibration bin of a probability: the same thresholds as the SQL ladder of `_bin_expr`."""
    for i in range(bins - 1):
        if p < float(f"{(i + 1) / bins:.6f}"):
            return i
    return bins - 1


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
    ('NONE', None) = unknown: missing levels, or data that never reached the horizon (feed stopped) once the grace
    period is over.
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
    if covered_until < target_time_s:
        if now_s < target_time_s + GATE_STALE_GRACE_S:
            return None  # data does not reach the horizon yet; wait a bit for the feed
        return "NONE", None  # the feed never covered the horizon: what happened afterwards is unknown
    r = sign * (last_close - entry) / risk if last_close is not None else None
    return "NONE", r


def gate_target_s(decision_time: int, tf: str, horizon_bars: int, target_at_ms: Any = None,
                  created_at_ms: Any = None) -> int:
    """End of a gate decision's outcome window (unix s; bars opening before it count): the close of the
    `horizon_bars`-th bar after the decision bar, the engine's own candidate horizon (TP1 before the SL within
    time_stop_bars). It counts from the decision bar whatever clock or convention wrote target_at (a replayed decision
    logged now, a writer that stored the open of the last bar instead of its close): one window for every source.
    The stored target_at (else created_at + 48 bars) only serves a row without a decision time."""
    tf_s = tf_seconds(tf)
    if int(decision_time) > 0:
        return floor_time(int(decision_time), tf) + (int(horizon_bars) + 1) * tf_s
    stored = safe_float(target_at_ms)
    if stored is None:
        stored = (safe_float(created_at_ms) or 0.0) + 48 * tf_s * 1000
    return int(stored) // 1000


def gate_won(outcome: str | None, outcome_r: float | None) -> bool | None:
    """The engine's definition (shared with its in-run learner): won = TP1 reached before the SL within the time-stop
    horizon. A covered horizon without TP1 or SL ('NONE' with a mark-to-market R) is not a win, whatever its close;
    None = unknown."""
    if outcome == "TP1":
        return True
    if outcome == "SL":
        return False
    if outcome == "NONE" and outcome_r is not None:
        return False
    return None


def learnable_gate_row(source: Any, symbol: Any) -> bool:
    """Simulated decisions (simulator sessions, SIM:* markets) are resolved and shown but never train the global gate
    learner, which drives the live verdicts."""
    return str(source or "") != "sim" and not str(symbol or "").upper().startswith("SIM:")


# ----------------------------------------------------------------------------- gate learner adapter


def _gate_learner_api() -> tuple[str, Any] | None:
    """Locate the engine's gate learner. Supported shapes (first match wins):

    - class `GateLearner` with `from_state(dict|None)` (or a constructor taking the state), `update(checks, won)`
      and `to_state() -> dict`;
    - a function `learn_gate_outcome(state, checks, won) -> dict` (aliases: update_gate_state, gate_learn).
    `checks` is the gate_log JSON {check_id: 1 | 0 | None}; `won` = TP1 reached before SL.
    """
    try:
        gate_mod = importlib.import_module("app.engine.gate")
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


def _make_gate_learner(cls: Any, state: dict | None) -> Any:
    """GateLearner.from_state(state) (contract); a fresh learner when there is no stored state yet."""
    if hasattr(cls, "from_state"):
        try:
            return cls.from_state(state)
        except (TypeError, ValueError, KeyError, AttributeError):
            if state is not None:
                raise
        for build in (lambda: cls.from_state({}), cls):
            try:
                return build()
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
        raise TypeError("GateLearner no se puede crear sin estado")
    return cls(state) if state is not None else cls()


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
        self._last_feature_prune: int = 0
        self._log = RateLimitedLog()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._started = True
        try:
            await ensure_daily_aggregates(self.rt.db)
            self._resolved_total = int(await self.rt.db.fetchval(
                "SELECT SUM(scored) FROM prediction_daily", default=0))
            self._pending = int(await self.rt.db.fetchval(
                "SELECT COUNT(*) FROM predictions WHERE resolved=0", default=0))
        except Exception as exc:
            if self._log.should_log("start", 300):
                self._emit("warn", f"Evaluador: no se pudieron leer los contadores ({type(exc).__name__}: {exc})")
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
                await self._prune_features(now)
            except Exception as exc:
                if self._log.should_log("feature-prune", 3600):
                    self._emit("warn", f"No se pudieron recortar los vectores del escáner: {type(exc).__name__}: {exc}")
            try:
                self._pending = int(await self.rt.db.fetchval(
                    "SELECT COUNT(*) FROM predictions WHERE resolved=0", default=0))
            except Exception:
                pass
            self._last_run_ms = now
            return out

    # ------------------------------------------------------------------ predictions

    async def _price_now(self, symbols: list[str], now: int) -> dict[str, float]:
        """Live prices only: a quote flagged `stale` by the hub, or whose price is older than QUOTE_MAX_AGE_MS (e.g. the
        last cached close served while TradingView fails), is ignored and the row resolves later from the candles."""
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
                    if isinstance(q, dict):
                        ts = safe_float(q.get("ts"))
                        if q.get("stale") or (ts is not None and now - ts > QUOTE_MAX_AGE_MS):
                            continue
                        p = safe_float(q.get("price"))
                    else:
                        p = safe_float(q)
                    if p is not None and p > 0:
                        prices[s] = p
        return prices

    async def _price_at(self, symbol: str, tf: str, target_ms: int, now: int,
                        created_ms: int | None = None) -> tuple[float | None, bool]:
        """Late resolution: (close of the last bar that completed at or before the target, no_trading).

        no_trading=True when no bar completed between the prediction and its target (closed session, venue that
        stopped trading): the outcome must not be judged. (None, False) = no usable data yet."""
        hub = self.rt.hub
        if hub is None:
            return None, False
        try:
            tf_n = normalize_tf(tf)
        except ValueError:
            tf_n = "1h"
        tf_s = tf_seconds(tf_n)
        target_s = target_ms // 1000
        need = int((now // 1000 - target_s) / tf_s) + 5
        if need > 1000:
            return None, False
        try:
            cs = await asyncio.wait_for(hub.get_candles(symbol, tf_n, max(need, 10)), timeout=30)
        except Exception:
            return None, False
        if cs is None or len(cs) == 0:
            return None, False
        best: float | None = None
        best_close_s: int | None = None
        for t, c in zip(cs.time.tolist(), cs.close.tolist()):
            if t + tf_s <= target_s:
                best, best_close_s = c, int(t) + tf_s
            else:
                break
        if best is None:
            return None, False
        covers_target = int(cs.time[-1]) + tf_s > target_s
        if getattr(cs, "stale", False) and not covers_target:
            return None, True  # an outdated series that stops before the target: nothing to judge
        if created_ms is not None and best_close_s <= created_ms // 1000:
            return None, True
        return safe_float(best), False

    async def _resolve_predictions(self, now: int) -> dict:
        await ensure_daily_aggregates(self.rt.db)
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
        prices = await self._price_now(sorted({r["symbol"] for r in on_time}), now)
        for r in on_time:
            p = prices.get(r["symbol"])
            if p is not None:
                resolved.append((r, p))
        expired: list[dict] = []
        for i, ((symbol, tf), items) in enumerate(sorted(late.items(), key=lambda kv: kv[1][0]["target_at"])):
            if i >= LATE_GROUPS_PER_CYCLE:
                break
            for r in items:
                p, no_trading = await self._price_at(symbol, tf, int(r["target_at"]), now, int(r["created_at"]))
                if p is not None:
                    resolved.append((r, p))
                elif no_trading or now - int(r["target_at"]) > EXPIRE_AFTER_MS:
                    expired.append(r)

        judged: list[tuple] = []  # (outcome_price, ret, correct, resolved_at, id)
        unjudged: list[tuple] = []  # measured but too small to judge: (outcome_price, ret, resolved_at, id)
        daily = _DailyBatch()
        samples: list[LearnSample] = []
        per_key: dict[str, int] = {}
        nexus_n = 0
        for r, outcome_price in resolved:
            ret = outcome_return_pct(float(r["price"]), outcome_price)
            if ret is None:
                expired.append(r)
                continue
            horizon_s = (int(r["target_at"]) - int(r["created_at"])) / 1000.0
            if not measurable(ret, horizon_s):
                unjudged.append((outcome_price, round(ret, 6), now, int(r["id"])))
                daily.add(r, None, None)
                continue
            correct = judge_prediction(int(r["direction"] or 0), ret, horizon_s)
            judged.append((outcome_price, round(ret, 6), 1 if correct else 0, now, int(r["id"])))
            daily.add(r, ret, correct)
            if r["source"] == "scanner" and r.get("model_key"):
                x = decode_features(r.get("features"))
                if x:
                    samples.append(LearnSample(
                        key=r["model_key"], feats=x, up=1 if ret > 0 else 0, label_time=int(r["target_at"]) // 1000,
                        created_ms=int(r["created_at"]), ret_pct=ret, symbol=str(r["symbol"]),
                        direction=int(r["direction"] or 0), correct=1 if correct else 0,
                    ))
                    per_key[r["model_key"]] = per_key.get(r["model_key"], 0) + 1
            elif r["source"] == "nexus":
                nexus_n += 1
        for r in expired:
            daily.add(r, None, None)
        # Scanner feature vectors: kept `scanner.replay_days` for the offline replay (then _prune_features drops them);
        # rows that expired without a price never had an outcome and lose theirs now.
        drop = "features=CASE WHEN source='scanner' THEN NULL ELSE features END"
        keep = "features=features" if self._replay_days() > 0 else drop
        async with self.rt.db.transaction() as tx:  # rows and daily aggregates move together (never counted twice)
            await tx.executemany(
                "UPDATE predictions SET resolved=1, outcome_price=?, outcome_return_pct=?, correct=?, resolved_at=?, "
                f"{keep} WHERE id=? AND resolved=0",
                judged,
            )
            await tx.executemany(
                "UPDATE predictions SET resolved=1, outcome_price=?, outcome_return_pct=?, correct=NULL, "
                f"resolved_at=?, {keep} WHERE id=? AND resolved=0",
                unjudged,
            )
            await tx.executemany(
                f"UPDATE predictions SET resolved=1, correct=NULL, resolved_at=?, {drop} "
                "WHERE id=? AND resolved=0",
                [(now, int(r["id"])) for r in expired],
            )
            await tx.executemany(_DAILY_UPSERT, daily.rows())
        self._resolved_total += len(judged)
        if judged or unjudged or expired:
            invalidate_skill_cache(self.rt.db)
        if samples:
            await self._train_scanner(samples)
        for key, n in per_key.items():
            roll = await self._rolling(model_key_value=key)
            name = key.rsplit("|", 1)[0]
            self._emit("learn", f"Modelo escáner {name}: {n} predicciones resueltas" + _rolling_text(roll),
                       {"model_key": key, "resolved": n, **_rolling_data(roll)})
        if nexus_n:
            roll = await self._rolling(source="nexus")
            if roll is not None:
                self._emit("learn", f"NEXUS: {nexus_n} predicciones resueltas" + _rolling_text(roll),
                           {"resolved": nexus_n, **_rolling_data(roll)})
        return {"predictions": len(judged), "expired": len(expired) + len(unjudged), "scanner_updates": len(samples)}

    def _replay_days(self) -> float:
        v = safe_float(self._setting("scanner.replay_days", REPLAY_DAYS_DEFAULT), REPLAY_DAYS_DEFAULT)
        return float(clamp(v if v is not None else REPLAY_DAYS_DEFAULT, 0.0, 30.0))

    async def _prune_features(self, now: int) -> None:
        """Drop the feature vectors of scanner rows resolved more than `scanner.replay_days` ago (hourly)."""
        if now - self._last_feature_prune < FEATURE_PRUNE_EVERY_MS:
            return
        self._last_feature_prune = now
        cutoff = now - int(self._replay_days() * 86_400_000)
        await self.rt.db.execute(
            "UPDATE predictions SET features=NULL WHERE source='scanner' AND resolved=1 AND features IS NOT NULL "
            "AND target_at < ?",
            (cutoff,),
        )

    async def _train_scanner(self, samples: list[LearnSample]) -> None:
        if self.rt.scanner is not None and hasattr(self.rt.scanner, "learn_batch"):
            await self.rt.scanner.learn_batch(samples)
            return
        await train_scanner_models(self.rt.db, samples)

    async def _rolling(self, *, model_key_value: str | None = None, source: str | None = None) -> dict | None:
        """Hit rate and Brier of the last ROLLING_N scored rows, each with its reference over the same rows (the most
        frequent outcome; climatology for the Brier score) and the number of independent horizon windows."""
        where = "resolved=1 AND correct IS NOT NULL AND outcome_return_pct IS NOT NULL"
        params: list[Any] = []
        if model_key_value is not None:
            where += " AND model_key=?"
            params.append(model_key_value)
        if source is not None:
            where += " AND source=?"
            params.append(source)
        rows = await self.rt.db.fetchall(
            f"SELECT correct, prob_up, outcome_return_pct, created_at, target_at FROM predictions WHERE {where} "
            f"ORDER BY target_at DESC LIMIT {ROLLING_N}",
            params,
        )
        if not rows:
            return None
        n = len(rows)
        up = sum(1 for r in rows if r["outcome_return_pct"] > 0)
        flat = sum(1 for r in rows if abs(float(r["outcome_return_pct"])) <= flat_band_pct(
            (int(r["target_at"]) - int(r["created_at"])) / 1000.0))
        ref_kind, ref_hits = best_constant(up, n - up, flat)
        u = up / n
        windows = len({(int(r["target_at"]) - int(r["created_at"]),
                        int(r["target_at"]) // max(int(r["target_at"]) - int(r["created_at"]), 60_000)) for r in rows})
        return {
            "n": n, "hits": sum(int(r["correct"]) for r in rows), "ref_hits": ref_hits, "ref_kind": ref_kind,
            "brier": sum((float(r["prob_up"]) - (1.0 if r["outcome_return_pct"] > 0 else 0.0)) ** 2 for r in rows) / n,
            "brier_ref": u * (1.0 - u), "windows": windows,
        }

    # ------------------------------------------------------------------ gate decisions

    def _setting(self, key: str, default: Any) -> Any:
        if self.rt.settings is None:
            return default
        try:
            return self.rt.settings.get(key, default)
        except Exception:
            return default

    async def _learned_keys(self, symbol: str, tf: str, oldest: int) -> set[tuple[int, str]]:
        """(bar time, direction) of this market's decisions already learned (resolved with a known outcome by a
        non-simulated source): the same decision logged by the watcher, the paper account and the scanner is one
        sample, not three."""
        rows = await self.rt.db.fetchall(
            "SELECT time, direction FROM gate_log WHERE symbol=? AND tf=? AND time >= ? AND resolved=1 "
            f"AND (source IS NULL OR source <> 'sim') AND {_GATE_JUDGED_OUTCOME}",
            (symbol, tf, int(oldest)),
        )
        return {(int(r["time"]), str(r["direction"])) for r in rows}

    async def _resolve_gate(self, now: int) -> int:
        hub = self.rt.hub
        if hub is None:
            return 0
        rows = await self.rt.db.fetchall(
            "SELECT id, symbol, tf, time, direction, verdict, entry, sl, tp1, checks, source, target_at, created_at "
            "FROM gate_log WHERE resolved=0 ORDER BY time, id LIMIT 1000"  # id: the first logged copy is learned
        )
        if not rows:
            return 0
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            groups.setdefault((r["symbol"], r["tf"]), []).append(r)
        mono = time.monotonic()
        now_s = now // 1000
        horizon_bars = int(safe_float(self._setting("nexus.time_stop_bars", 48), 48) or 48)
        updates: list[tuple] = []
        samples: list[tuple[dict, bool]] = []
        checked = 0
        for (symbol, tf), items in sorted(groups.items(), key=lambda kv: kv[1][0]["time"]):
            if checked >= GATE_GROUPS_PER_CYCLE:
                break
            try:
                tf_n = normalize_tf(tf)
            except ValueError:
                tf_n = "1h"
            tf_s = tf_seconds(tf_n)
            targets = {int(r["id"]): gate_target_s(int(r["time"]), tf_n, horizon_bars, r.get("target_at"),
                                                   r.get("created_at")) for r in items}
            any_due = any(t <= now_s for t in targets.values())
            last = self._gate_checked.get((symbol, tf))
            if not any_due and last is not None and mono - last < GATE_RECHECK_S:
                continue
            oldest = min(int(r["time"]) for r in items)
            need = int(clamp((now_s - oldest) // tf_s + 3, 10, GATE_FETCH_MAX))
            try:
                cs = await asyncio.wait_for(hub.get_candles(symbol, tf_n, need), timeout=30)
            except Exception:
                continue
            checked += 1
            self._gate_checked[(symbol, tf)] = mono
            bars = []
            if cs is not None:
                bars = list(zip(cs.time.tolist(), cs.high.tolist(), cs.low.tolist(), cs.close.tolist()))
            learned = await self._learned_keys(symbol, tf, oldest)
            for r in items:
                decision = int(r["time"])
                # The horizon counts from the decision bar, whatever wrote target_at (a replayed or synthetic decision
                # logged now must not be judged over weeks of later bars; every source gets the engine's window)
                target_s = targets[int(r["id"])]
                if not bars or bars[0][0] > decision:
                    # No data from the decision bar on: the outcome is unknown. Wait for the feed unless the decision
                    # is older than anything that can be fetched.
                    reachable = (now_s - decision) // tf_s + 3 <= GATE_FETCH_MAX
                    if now_s >= target_s + (GATE_STALE_GRACE_S if reachable else 0):
                        updates.append(("NONE", None, now, int(r["id"])))
                    continue
                entry = safe_float(r["entry"])
                if entry is None:
                    entry = next((c for t, _, _, c in bars if t == decision), None)
                res = gate_outcome(r["direction"], entry, safe_float(r["sl"]), safe_float(r["tp1"]), bars,
                                   decision, target_s, now_s, tf_s)
                if res is None:
                    continue
                outcome, r_mult = res
                updates.append((outcome, r_mult, now, int(r["id"])))
                won = gate_won(outcome, r_mult)
                checks = json_loads(r["checks"], {}) or {}
                key = (decision, str(r["direction"]))
                if (won is not None and isinstance(checks, dict) and checks and key not in learned
                        and learnable_gate_row(r.get("source"), symbol)):
                    learned.add(key)
                    samples.append((checks, won))
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
                    learner = _make_gate_learner(obj, state if isinstance(state, dict) else None)
                    for checks, won in samples:
                        learner.update(checks, won)
                    new_state = learner.to_state() if hasattr(learner, "to_state") else to_plain(learner)
                else:
                    new_state = state
                    for checks, won in samples:
                        new_state = obj(new_state, checks, won)
                if isinstance(new_state, dict):
                    own = safe_float(new_state.get("updates"))  # the learner's own counter when it keeps one
                    updates = int(own) if own is not None and own >= 0 else prev + len(samples)
                    await db.execute(
                        "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                        "VALUES ('gate', 'global', ?, ?, NULL, ?) ON CONFLICT(kind, key) DO UPDATE SET "
                        "state=excluded.state, updates=excluded.updates, updated_at=excluded.updated_at",
                        (json_dumps(new_state), updates, now_ms()),
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
# gate_log outcome known: TP1, SL, or a covered horizon without either (time stop, mark-to-market R)
_GATE_JUDGED_OUTCOME = "(outcome='TP1' OR outcome='SL' OR (outcome='NONE' AND outcome_r IS NOT NULL))"


def _bin_expr(bins: int) -> str:
    """CASE ladder mapping prob_up to a calibration bin 0..bins-1 (same result in both SQL dialects)."""
    steps = " ".join(f"WHEN prob_up < {(i + 1) / bins:.6f} THEN {i}" for i in range(bins - 1))
    return f"CASE {steps} ELSE {bins - 1} END"


def _ratio(num: Any, den: Any) -> float | None:
    d = safe_float(den)
    return None if not d else float(safe_float(num) or 0.0) / d


# ----------------------------------------------------------------------------- daily aggregates (prediction_daily)

# One row per (UTC day of target_at, source, model key, calibration bin of prob_up). The model key is the scanner's
# "market|tf|horizon" or "SYMBOL|tf" for NEXUS predictions. n = resolved rows (with or without verdict); scored,
# correct, up, prob_sum and brier_sum only count rows with a verdict.
_DAILY_KEY_EXPR = "CASE WHEN source='nexus' THEN symbol || '|' || tf ELSE COALESCE(model_key, '') END"
_DAILY_UPSERT = (
    "INSERT INTO prediction_daily(day, source, model_key, bin, n, scored, correct, up, prob_sum, brier_sum) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(day, source, model_key, bin) DO UPDATE SET "
    "n=prediction_daily.n + excluded.n, scored=prediction_daily.scored + excluded.scored, "
    "correct=prediction_daily.correct + excluded.correct, up=prediction_daily.up + excluded.up, "
    "prob_sum=prediction_daily.prob_sum + excluded.prob_sum, brier_sum=prediction_daily.brier_sum + excluded.brier_sum"
)
_DAILY_BACKFILL = (
    "INSERT INTO prediction_daily(day, source, model_key, bin, n, scored, correct, up, prob_sum, brier_sum) "
    f"SELECT (target_at / 86400000) * 86400, source, {_DAILY_KEY_EXPR}, {_bin_expr(CALIBRATION_BINS)}, COUNT(*), "
    f"SUM(CASE WHEN {_SCORED} THEN 1 ELSE 0 END), SUM(CASE WHEN {_SCORED} THEN correct ELSE 0 END), "
    f"SUM(CASE WHEN {_SCORED} AND outcome_return_pct > 0 THEN 1 ELSE 0 END), "
    f"SUM(CASE WHEN {_SCORED} THEN prob_up ELSE 0 END), SUM(CASE WHEN {_SCORED} THEN {_BRIER_EXPR} ELSE 0 END) "
    "FROM predictions WHERE resolved=1 GROUP BY 1, 2, 3, 4"
)
_DAILY_READY: "weakref.WeakSet[Any]" = weakref.WeakSet()
_DAILY_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()


class _DailyBatch:
    """Aggregate increments of one resolution batch (parameter rows of `_DAILY_UPSERT`)."""

    def __init__(self) -> None:
        self._acc: dict[tuple[int, str, str, int], list[float]] = {}

    def add(self, r: dict, ret: float | None, correct: bool | None) -> None:
        source = str(r["source"])
        key_value = f"{r['symbol']}|{r['tf']}" if source == "nexus" else str(r.get("model_key") or "")
        p = float(r["prob_up"])
        acc = self._acc.setdefault(((int(r["target_at"]) // 86_400_000) * 86400, source, key_value, prob_bin(p)),
                                   [0, 0, 0, 0, 0.0, 0.0])
        acc[0] += 1
        if ret is not None and correct is not None:
            up = 1 if ret > 0 else 0
            acc[1] += 1
            acc[2] += 1 if correct else 0
            acc[3] += up
            acc[4] += p
            acc[5] += (p - up) ** 2

    def rows(self) -> list[tuple]:
        return [(*k, int(a[0]), int(a[1]), int(a[2]), int(a[3]), float(a[4]), float(a[5]))
                for k, a in self._acc.items()]


async def ensure_daily_aggregates(db: Any) -> None:
    """Fold the resolved rows that predate `prediction_daily` into it, once per database (meta flag). From then on the
    evaluator updates the aggregates in the transaction that resolves each row, so pruning old rows
    (app.maintenance) keeps totals, hit rates, calibration and the daily charts."""
    if db is None or db in _DAILY_READY:
        return
    lock = _DAILY_LOCKS.get(db)
    if lock is None:
        lock = _DAILY_LOCKS[db] = asyncio.Lock()
    async with lock:
        if db in _DAILY_READY:
            return
        if await db.fetchval("SELECT value FROM meta WHERE key=?", (DAILY_META_KEY,)) != DAILY_VERSION:
            async with db.transaction() as tx:
                await tx.execute("DELETE FROM prediction_daily")
                await tx.execute(_DAILY_BACKFILL)
                await tx.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (DAILY_META_KEY, DAILY_VERSION),
                )
        _DAILY_READY.add(db)


async def prediction_stats(db: Any, source: str | None = None, symbol: str | None = None) -> dict:
    """Totals, hit rate, Brier and the daily hit rate (90 days). Without a symbol they come from the daily aggregates
    (the whole retained history); for one symbol from the rows still stored (resolved scanner rows: ~30 days)."""
    if symbol:
        return await _row_prediction_stats(db, source, symbol)
    await ensure_daily_aggregates(db)
    where, params = ("source=?", [source]) if source else ("1=1", [])
    tot = await db.fetchone(
        f"SELECT SUM(n) AS n, SUM(scored) AS scored, SUM(correct) AS correct, SUM(brier_sum) AS brier "
        f"FROM prediction_daily WHERE {where}",
        params,
    ) or {}
    by_rows = await db.fetchall(
        f"SELECT source, SUM(scored) AS scored, SUM(correct) AS correct, SUM(brier_sum) AS brier "
        f"FROM prediction_daily WHERE {where} GROUP BY source",
        params,
    )
    pend = await db.fetchall(
        f"SELECT source, COUNT(*) AS n FROM predictions WHERE resolved=0 AND {where} GROUP BY source", params
    )
    daily = await db.fetchall(
        f"SELECT day, SUM(correct) AS correct, SUM(scored) AS scored FROM prediction_daily WHERE {where} "
        f"GROUP BY day HAVING SUM(scored) > 0 ORDER BY day DESC LIMIT 90",
        params,
    )
    pending = sum(int(r.get("n") or 0) for r in pend)
    scored = int(tot.get("scored") or 0)
    by_source: dict[str, dict] = {}
    for r in by_rows:
        n = int(r.get("scored") or 0)
        by_source[r["source"]] = {"resolved": n, "hit_rate": _ratio(r.get("correct"), n),
                                  "brier": _ratio(r.get("brier"), n)}
    for r in pend:
        by_source.setdefault(r["source"], {"resolved": 0, "hit_rate": None, "brier": None})
    return {
        "total": int(tot.get("n") or 0) + pending,
        "resolved": scored,
        "pending": pending,
        "hit_rate": _ratio(tot.get("correct"), scored),
        "brier": _ratio(tot.get("brier"), scored),
        "by_source": by_source,
        "rolling": [
            {"time": int(r["day"]), "hit_rate": float(_ratio(r["correct"], r["scored"]) or 0.0), "n": int(r["scored"])}
            for r in reversed(daily)
        ],
    }


async def _row_prediction_stats(db: Any, source: str | None, symbol: str | None) -> dict:
    """prediction_stats over the stored rows (used for one symbol)."""
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
    if bins == CALIBRATION_BINS:  # from the daily aggregates (whole retained history)
        await ensure_daily_aggregates(db)
        conds, args = ["scored > 0"], []
        if model_key_value is not None:
            conds.append("model_key=?")
            args.append(model_key_value)
        if source is not None:
            conds.append("source=?")
            args.append(source)
        agg = await db.fetchall(
            f"SELECT bin, SUM(prob_sum) AS p, SUM(up) AS up, SUM(scored) AS n FROM prediction_daily "
            f"WHERE {' AND '.join(conds)} GROUP BY bin ORDER BY bin",
            args,
        )
        return [
            {"bin": int(r["bin"]), "predicted": float(_ratio(r["p"], r["n"]) or 0.0),
             "observed": float(_ratio(r["up"], r["n"]) or 0.0), "count": int(r["n"])}
            for r in agg if int(r.get("n") or 0) > 0
        ]
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


_METRIC_CONTAINERS = (None, "learning", "stats", "perf", "metrics", "learner", "latest", "model")  # lookup order


def _state_metric(state: Any, name: str, _depth: int = 0) -> float | None:
    """A learner metric from a stored model_state JSON: top level, a common sub-object, or the latest snapshot
    (the NEXUS service may persist {"snapshots": [{"time", "state"}, ...]} instead of the bare learner state)."""
    if not isinstance(state, dict) or _depth > 2:
        return None
    for key in _METRIC_CONTAINERS:
        container = state if key is None else state.get(key)
        if isinstance(container, dict):
            v = safe_float(container.get(name))
            if v is not None:
                return v
    snaps = state.get("snapshots")
    if isinstance(snaps, list) and snaps:
        last = snaps[-1]
        inner = last.get("state") if isinstance(last, dict) and isinstance(last.get("state"), dict) else last
        return _state_metric(inner, name, _depth + 1)
    if isinstance(snaps, dict) and snaps:  # {time: state}
        try:
            last_key = max(snaps, key=lambda k: safe_float(k, 0.0) or 0.0)
        except ValueError:
            return None
        return _state_metric(snaps[last_key], name, _depth + 1)
    return None


# A stored NEXUS memory ('nexus' row) is large (≈ 0,6 MB of JSON at the default nexus.horizon, ≈ 3,5 MB at 200) and
# the summary needs only its hit rate and Brier: a row is read again only when its version (updated_at, updates,
# trained_until) changed since the previous summary of the same database, and then only its small top-level tail is
# decoded when that tail decides the answer (_nexus_row_metrics; otherwise the whole document). Decoding runs in a
# worker thread, so a first summary after a start does not stall the server either.
_NEXUS_TAIL_MARK = ',"learning":'
_NEXUS_METRICS: "weakref.WeakKeyDictionary[Any, dict[str, tuple[tuple, float | None, float | None]]]" = (
    weakref.WeakKeyDictionary())
_NEXUS_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _nexus_tail(text: str) -> dict | None:
    """The top-level members of a stored NEXUS memory from its last top-level "learning" key to the end, without
    decoding the snapshots before it (NexusService._save_model writes compact JSON {"version", "snapshots",
    "learning", "hit_rate", "brier", "updates"}). The text after the last ',"learning":' decodes as one object only
    when that key is a top-level one (a nested match leaves its enclosing brackets over). None otherwise."""
    pos = text.rfind(_NEXUS_TAIL_MARK)
    if pos < 0:
        return None
    tail = json_loads("{" + text[pos + 1:])  # None when it is not valid JSON
    return tail if isinstance(tail, dict) else None


def _tail_metric(tail: dict, name: str) -> tuple[bool, float | None]:
    """(decided, value): what _state_metric gives for the WHOLE document, read from its tail alone. Each container
    _state_metric looks at, in its order, must be in the tail (its last occurrence, the one the full decode keeps);
    the first one missing, or the snapshot fallback, leaves it undecided."""
    for key in _METRIC_CONTAINERS:
        if (name if key is None else key) not in tail:
            return False, None
        container = tail if key is None else tail.get(key)
        if isinstance(container, dict):
            v = safe_float(container.get(name))
            if v is not None:
                return True, v
    return False, None


def _nexus_row_metrics(text: Any) -> tuple[float | None, float | None]:
    """(hit_rate, brier) of one stored NEXUS memory, exactly as _state_metric reads them from the decoded document."""
    if isinstance(text, str):
        tail = _nexus_tail(text)
        if tail is not None:
            hit, brier = _tail_metric(tail, "hit_rate"), _tail_metric(tail, "brier")
            if hit[0] and brier[0]:
                return hit[1], brier[1]
    state = json_loads(text)
    return _state_metric(state, "hit_rate"), _state_metric(state, "brier")


async def _nexus_metrics(db: Any, rows: list[dict]) -> dict[str, tuple[float | None, float | None, Any, Any]]:
    """{key: (hit_rate, brier, updates, trained_until)} of the listed NEXUS memories (see the comment above). The
    four values always come from the same version of a row; a row deleted meanwhile is left out."""
    try:
        cache = _NEXUS_METRICS.setdefault(db, {})
        lock = _NEXUS_LOCKS.setdefault(db, asyncio.Lock())
    except TypeError:  # not weak-referenceable: nothing is remembered between calls
        cache, lock = {}, asyncio.Lock()
    listed = {str(r["key"]): (r.get("updated_at"), r.get("updates"), r.get("trained_until")) for r in rows}
    async with lock:  # concurrent summaries (several open pages) decode each changed row once
        for k in [k for k in cache if k not in listed]:  # deleted, or no longer among the newest listed
            cache.pop(k, None)
        for k, version in listed.items():
            entry = cache.get(k)
            if entry is not None and entry[0] == version:
                continue
            row = await db.fetchone("SELECT state, updates, trained_until, updated_at FROM model_state "
                                    "WHERE kind='nexus' AND key=?", (k,))
            if row is None:
                cache.pop(k, None)
                continue
            hit, brier = await asyncio.to_thread(_nexus_row_metrics, row.get("state"))
            cache[k] = ((row.get("updated_at"), row.get("updates"), row.get("trained_until")), hit, brier)
        return {k: (e[1], e[2], e[0][1], e[0][2]) for k, e in cache.items() if k in listed}


async def learning_summary(db: Any, settings: dict | None = None) -> dict:
    from app.config import DEFAULT_SETTINGS

    settings = settings or DEFAULT_SETTINGS
    markets = settings.get("scanner.markets") or ["crypto"]
    try:
        tf = normalize_tf(settings.get("scanner.tf") or "1h")
    except ValueError:
        tf = "1h"
    preferred = model_key(str(markets[0]).lower(), tf, int(settings.get("scanner.horizon_s") or 14400))
    await ensure_daily_aggregates(db)

    # --- scanner model
    scanner_model = None
    srow = await db.fetchone(
        "SELECT key, state, updates FROM model_state WHERE kind='scanner' AND key=?", (preferred,)
    ) or await db.fetchone(
        "SELECT key, state, updates FROM model_state WHERE kind='scanner' ORDER BY updates DESC LIMIT 1"
    )
    key = srow["key"] if srow else None
    if key is None and (
        await db.fetchone("SELECT 1 AS x FROM predictions WHERE model_key=? LIMIT 1", (preferred,))
        or await db.fetchone("SELECT 1 AS x FROM prediction_daily WHERE model_key=? LIMIT 1", (preferred,))
    ):
        key = preferred
    if key is not None:
        model = ScannerModel.from_state(json_loads(srow["state"]) if srow else None, key=key)
        stats = await db.fetchone(
            "SELECT SUM(scored) AS n, SUM(correct) AS correct, SUM(brier_sum) AS brier FROM prediction_daily "
            "WHERE model_key=? AND source='scanner'",
            (key,),
        ) or {}
        scanner_model = {
            "key": key,
            "updates": int(model.updates),
            "hit_rate": _ratio(stats.get("correct"), stats.get("n")),
            "brier": _ratio(stats.get("brier"), stats.get("n")),
            # direction weights: the asset's features (relative to its scan), volatility × market move, market context
            "features": model.weight_table(),
            "calibration": await calibration_bins(db, model_key_value=key, source="scanner"),
        }

    # --- NEXUS models (the large memory rows are decoded only when they changed: _nexus_metrics)
    nrows = await db.fetchall(
        "SELECT key, updates, trained_until, updated_at FROM model_state WHERE kind='nexus' "
        "ORDER BY updated_at DESC LIMIT 100"
    )
    metrics = await _nexus_metrics(db, nrows)
    pstats = await db.fetchall(
        "SELECT model_key, SUM(scored) AS n, SUM(correct) AS correct, SUM(brier_sum) AS brier FROM prediction_daily "
        "WHERE source='nexus' GROUP BY model_key"
    )
    pmap: dict[tuple[str, str], dict] = {}
    for r in pstats:
        psym, _, ptf = str(r["model_key"]).rpartition("|")
        pmap[(psym, ptf)] = {"hit_rate": _ratio(r.get("correct"), r.get("n")),
                             "brier": _ratio(r.get("brier"), r.get("n"))}
    nexus_models = []
    for r in nrows:
        k = str(r["key"])
        m = metrics.get(k)
        if m is None:  # deleted while the summary was being built
            continue
        symbol, _, ntf = k.rpartition("|")
        if not symbol:
            symbol, ntf = k, ""
        hit, brier, updates, trained_until = m
        p = pmap.get((symbol, ntf))
        if hit is None and p is not None:
            hit = safe_float(p.get("hit_rate"))
        if brier is None and p is not None:
            brier = safe_float(p.get("brier"))
        tu = safe_float(trained_until)
        nexus_models.append({
            "symbol": symbol,
            "tf": ntf,
            "updates": int(updates or 0),
            "hit_rate": hit,
            "brier": brier,
            "trained_until": int(tu) if tu is not None else None,
        })

    # --- gate decisions: the engine's target (TP1 before the SL within the time stop). Each decision counts once even
    # when several sources logged it, and simulated ones (simulator sessions, SIM:*) are left out like in the learner.
    # *_judged = decisions whose outcome is known: the denominators of the two rates.
    judged = f"resolved=1 AND {_GATE_JUDGED_OUTCOME}"
    won = "CASE WHEN outcome='TP1' THEN 1.0 ELSE 0.0 END"
    g = await db.fetchone(
        "SELECT COUNT(*) AS decisions, "
        "SUM(CASE WHEN verdict='TAKE' THEN 1 ELSE 0 END) AS taken, "
        "SUM(CASE WHEN verdict='AVOID' THEN 1 ELSE 0 END) AS avoided, "
        f"SUM(CASE WHEN verdict='AVOID' AND {judged} THEN 1 ELSE 0 END) AS avoided_judged, "
        f"SUM(CASE WHEN verdict='TAKE' AND {judged} THEN 1 ELSE 0 END) AS taken_judged, "
        f"AVG(CASE WHEN verdict='AVOID' AND {judged} THEN 1.0 - {won} END) AS avoided_lose, "
        f"AVG(CASE WHEN verdict='TAKE' AND {judged} THEN {won} END) AS taken_win "
        "FROM gate_log WHERE id IN (SELECT MIN(id) FROM gate_log WHERE (source IS NULL OR source <> 'sim') "
        "AND symbol NOT LIKE 'SIM:%' GROUP BY symbol, tf, time, direction)"
    ) or {}
    gate = {
        "decisions": int(g.get("decisions") or 0),
        "taken": int(g.get("taken") or 0),
        "avoided": int(g.get("avoided") or 0),
        "avoided_would_lose_rate": safe_float(g.get("avoided_lose")),
        "taken_win_rate": safe_float(g.get("taken_win")),
        "avoided_judged": int(g.get("avoided_judged") or 0),
        "taken_judged": int(g.get("taken_judged") or 0),
    }

    # --- timeline (all sources, per day)
    trows = await db.fetchall(
        "SELECT day, SUM(correct) AS correct, SUM(brier_sum) AS brier, SUM(scored) AS n FROM prediction_daily "
        "GROUP BY day HAVING SUM(scored) > 0 ORDER BY day DESC LIMIT 90"
    )
    timeline = [
        {"time": int(r["day"]), "hit_rate": float(_ratio(r["correct"], r["n"]) or 0.0),
         "brier": float(_ratio(r["brier"], r["n"]) or 0.0), "n": int(r["n"])}
        for r in reversed(trows)
    ]
    return {
        "scanner_model": scanner_model,
        "nexus_models": nexus_models,
        "gate": gate,
        "predictions": await prediction_stats(db),
        "timeline": timeline,
    }


# ----------------------------------------------------------------------------- skill versus the baseline
# A prediction has three outcomes (up = return > 0, down = return < 0, neutral = |return| inside the flat band; up/down
# and neutral overlap), so 50 % is not the chance level. Reference = what always predicting the most frequent outcome
# of the same rows would have hit (known only afterwards: a demanding baseline); chance = the hit rate expected if the
# model's calls (same mix of up / down / neutral) were unrelated to the outcomes. The Brier score is compared with
# climatology (always the period's up rate): Brier skill score BSS = 1 − Brier / climatology. Rows of one horizon
# window move together, so the 95 % intervals are clustered by horizon windows (target_at // horizon), with a
# Newey–West term for the correlation between neighbouring windows (cluster_ci), and no conclusion is drawn with
# fewer than MIN_WINDOWS windows.

MIN_WINDOWS = 30
SKILL_CACHE_TTL_S = 60.0  # windows up to 7 days (recomputed sooner when new outcomes arrive)
SKILL_CACHE_LONG_TTL_S = 600.0  # longer windows / every stored row
_T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
         2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)
OUTCOME_LABEL = {"up": "siempre «alcista»", "down": "siempre «bajista»", "flat": "siempre «neutral»"}
DIRECTION_LABEL = {1: "alcista", -1: "bajista", 0: "neutral"}
DIRECTION_OUTCOME = {1: "up", -1: "down", 0: "flat"}
_SKILL_CACHE = TTLCache(SKILL_CACHE_TTL_S, maxsize=64)
_SKILL_GEN: "weakref.WeakKeyDictionary[Any, int]" = weakref.WeakKeyDictionary()
_DB_TOKENS: "weakref.WeakKeyDictionary[Any, int]" = weakref.WeakKeyDictionary()
_TOKEN_SEQ = itertools.count(1)


def t975(df: int) -> float:
    """Two-sided 95 % Student t quantile (df ≥ 1)."""
    if df < 1:
        return float("inf")
    if df <= len(_T975):
        return _T975[df - 1]
    return 2.0 if df <= 60 else 1.98 if df <= 120 else 1.96


def best_constant(up: int, down: int, flat: int) -> tuple[str, int]:
    """(outcome, hits) of the best constant prediction for these outcome counts (ties: up, down, neutral)."""
    return max((("up", int(up)), ("down", int(down)), ("flat", int(flat))), key=lambda o: o[1])


def nw_lag(g: int) -> int:
    """Bartlett (Newey–West) bandwidth for g consecutive horizon windows: ⌊4 (g / 100)^(2/9)⌋, at least 1."""
    return max(1, int(4.0 * (max(int(g), 1) / 100.0) ** (2.0 / 9.0)))


def cluster_ci(sums: list[float], counts: list[int], blocks: list[tuple[int, int]] | None = None
               ) -> tuple[float, float, float] | None:
    """(mean, low, high): per-row mean of a quantity summed per cluster and its 95 % interval, robust to the
    correlation inside clusters (CR1 variance of the ratio estimator, t with G − 1 degrees of freedom).

    `blocks` = (horizon, window index) of each cluster: the variance then also counts the correlation between nearby
    windows (Bartlett kernel, Newey–West bandwidth `nw_lag`). A target near the start of a window was still moving
    during the previous one (the outcome windows of neighbouring blocks overlap) and market regimes last longer than
    one window, so neighbouring windows are not independent either; without it the interval would be too narrow."""
    g = len(counts)
    total = float(sum(counts))
    if g < 2 or total <= 0:
        return None
    mean = float(sum(sums)) / total
    resid = [s - mean * c for s, c in zip(sums, counts)]
    if blocks is not None and len(blocks) == g:
        by_block: dict[tuple[int, int], float] = {}
        for b, r in zip(blocks, resid):
            key = (int(b[0]), int(b[1]))
            by_block[key] = by_block.get(key, 0.0) + r
        acc = sum(r * r for r in by_block.values())
        lag = nw_lag(len(by_block))
        for (hz, idx), r in by_block.items():
            for k in range(1, lag + 1):
                other = by_block.get((hz, idx + k))
                if other is not None:
                    acc += 2.0 * (1.0 - k / (lag + 1.0)) * r * other
    else:
        acc = sum(r * r for r in resid)
    var = g / (g - 1.0) * max(acc, 0.0) / (total * total)
    half = t975(g - 1) * math.sqrt(var)
    return mean, mean - half, mean + half


def _bounded(ci: tuple[float, float, float] | None, lo: float, hi: float) -> tuple[float, float, float] | None:
    """An interval clipped to the natural range of its quantity (a hit-rate difference lies within ±1)."""
    if ci is None:
        return None
    return ci[0], max(ci[1], lo), min(ci[2], hi)


def invalidate_skill_cache(db: Any) -> None:
    """New outcomes were written: the next skill report is recomputed."""
    try:
        _SKILL_GEN[db] = _SKILL_GEN.get(db, 0) + 1
    except TypeError:
        _SKILL_CACHE.clear()


def _rolling_text(roll: dict | None) -> str:
    """' · acierto 38,0 % (76 de 200) · referencia 62,0 % (siempre «bajista») · Brier 0,357 · climatología 0,236 · …'"""
    if not roll or not roll.get("n"):
        return ""
    n = int(roll["n"])
    text = (f" · acierto {fmt_prob(roll['hits'] / n, 1)} ({fmt_num(roll['hits'], 0)} de {fmt_num(n, 0)}) · referencia "
            f"{fmt_prob(roll['ref_hits'] / n, 1)} ({OUTCOME_LABEL[roll['ref_kind']]}) · Brier {fmt_num(roll['brier'], 3)} "
            f"· climatología {fmt_num(roll['brier_ref'], 3)}")
    windows = int(roll.get("windows") or 0)
    if windows < MIN_WINDOWS:
        text += f" · {windows} {'ventana independiente' if windows == 1 else 'ventanas independientes'}: sin conclusión"
    return text


def _rolling_data(roll: dict | None) -> dict:
    if not roll or not roll.get("n"):
        return {"hit_rate": None, "brier": None}
    n = int(roll["n"])
    return {"hit_rate": roll["hits"] / n, "brier": roll["brier"], "n": n, "reference": roll["ref_hits"] / n,
            "reference_kind": roll["ref_kind"], "brier_ref": roll["brier_ref"], "windows": roll.get("windows")}


def _band_case(horizons: list[int]) -> str:
    """SQL expression: the flat band (%) of each row from its horizon (target_at − created_at, ms)."""
    default = f"{flat_band_pct(14400):.6f}"
    if not horizons:
        return default
    whens = " ".join(f"WHEN {int(h)} THEN {flat_band_pct(int(h) / 1000.0):.6f}" for h in horizons)
    return f"(CASE (target_at - created_at) {whens} ELSE {default} END)"


async def _skill_records(db: Any, since: int, source: str | None, model_key_value: str | None) -> list[dict]:
    """Scored rows since `since` (target time) aggregated per (source, scanner key, horizon, target hour, direction,
    calibration bin): everything the skill statistics need, without loading the rows."""
    where = [_SCORED, "target_at >= ?"]
    params: list[Any] = [int(since)]
    if source:
        where.append("source=?")
        params.append(source)
    if model_key_value:
        where.append("model_key=?")
        params.append(model_key_value)
    cond = " AND ".join(where)
    hz_rows = await db.fetchall(
        f"SELECT DISTINCT target_at - created_at AS hz FROM predictions WHERE {cond} LIMIT 200", params)
    horizons = sorted({int(r["hz"]) for r in hz_rows if safe_float(r.get("hz")) is not None and int(r["hz"]) > 0})
    if not horizons:
        return []
    band = _band_case(horizons)
    mkey = "CASE WHEN source='scanner' THEN COALESCE(model_key, '') ELSE '' END"
    rows = await db.fetchall(
        f"SELECT source, {mkey} AS mkey, target_at - created_at AS hz, target_at / 3600000 AS hr, direction AS d, "
        f"{_bin_expr(CALIBRATION_BINS)} AS bin, COUNT(*) AS n, SUM(correct) AS hits, "
        f"SUM(CASE WHEN outcome_return_pct > 0 THEN 1 ELSE 0 END) AS up, "
        f"SUM(CASE WHEN ABS(outcome_return_pct) <= {band} THEN 1 ELSE 0 END) AS flat, "
        f"SUM(prob_up) AS p_sum, SUM({_BRIER_EXPR}) AS brier_sum "
        f"FROM predictions WHERE {cond} GROUP BY 1, 2, 3, 4, 5, 6",
        params,
    )
    out = []
    for r in rows:
        hz = int(safe_float(r.get("hz"), 0) or 0)
        if hz <= 0:
            continue
        d = int(safe_float(r.get("d"), 0) or 0)
        out.append({
            "source": str(r.get("source") or ""), "mkey": str(r.get("mkey") or ""), "hz": hz,
            "hr": int(safe_float(r.get("hr"), 0) or 0), "d": 1 if d > 0 else -1 if d < 0 else 0,
            "bin": int(safe_float(r.get("bin"), 0) or 0), "n": int(r.get("n") or 0), "hits": int(r.get("hits") or 0),
            "up": int(r.get("up") or 0), "flat": int(r.get("flat") or 0), "p_sum": float(r.get("p_sum") or 0.0),
            "brier_sum": float(r.get("brier_sum") or 0.0),
        })
    return out


def _hz_text(hz_ms: int) -> str:
    h = hz_ms / 3_600_000
    if h >= 24 and abs(h / 24 - round(h / 24)) < 1e-9:
        return f"{round(h / 24)} d"
    if h >= 1:
        return f"{fmt_num(h, 0 if abs(h - round(h)) < 1e-9 else 1)} h"
    return f"{round(hz_ms / 60_000)} min"


def _pp(x: float | None, signed: bool = True) -> str:
    return "—" if x is None else f"{fmt_num(x * 100.0, 1, signed)} pp"


def skill_group(records: list[dict], *, gid: str, label: str, source: str | None = None,
                model_key_value: str | None = None, common_blocks: bool = False) -> dict:
    """Skill of one group of predictions versus the baseline. `records` are aggregates (see _skill_records); a
    cluster is one independent horizon window (target hour // horizon; `common_blocks` uses the longest horizon for
    every row, for groups that mix horizons)."""
    base: dict[str, Any] = {"id": gid, "label": label, "source": source, "model_key": model_key_value}
    n = sum(r["n"] for r in records)
    if n <= 0:
        return {**base, "n": 0, "hits": 0, "accuracy": None, "windows": 0, "horizon": None, "window": None,
                "outcomes": {"up": 0, "down": 0, "flat": 0}, "reference": None, "chance": None, "skill_pp": None,
                "skill_ci": None, "brier": None, "brier_ref": None, "bss": None, "bss_ci": None, "ece_pp": None,
                "reliability": None, "resolution": None, "by_direction": [], "daily": [], "verdict": "none",
                "verdict_text": "Aún no hay predicciones verificadas en este periodo.", "brier_text": None}
    hits = sum(r["hits"] for r in records)
    up = sum(r["up"] for r in records)
    flat = sum(r["flat"] for r in records)
    down = n - up
    ref_kind, ref_hits = best_constant(up, down, flat)
    rate = {"up": up / n, "down": down / n, "flat": flat / n}
    by_d: dict[int, list[int]] = {}
    for r in records:
        acc = by_d.setdefault(r["d"], [0, 0])
        acc[0] += r["n"]
        acc[1] += r["hits"]
    chance = sum(v[0] / n * rate[DIRECTION_OUTCOME[d]] for d, v in by_d.items())
    hz_counts: dict[int, int] = {}
    for r in records:
        hz_counts[r["hz"]] = hz_counts.get(r["hz"], 0) + r["n"]
    main_hz = max(hz_counts, key=lambda h: hz_counts[h])
    block_hz = max(hz_counts) if common_blocks else None
    # Length of one independent window as built below (the records are per target hour: never shorter than 1 h). A
    # group that mixes horizons uses its longest one for every row, so "N ventanas de 4 h" must not be said of 5-day
    # windows.
    window_hz = max(block_hz or max(hz_counts), 3_600_000)
    clusters: dict[tuple[int, int], list[float]] = {}
    for r in records:
        hz = block_hz or r["hz"]
        c = clusters.setdefault((hz, r["hr"] * 3_600_000 // hz), [0, 0, 0, 0, 0.0])
        c[0] += r["n"]
        c[1] += r["hits"]
        c[2] += r["up"]
        c[3] += r["flat"]
        c[4] += r["brier_sum"]
    windows = len(clusters)

    def ref_of(c: list[float]) -> float:
        return c[2] if ref_kind == "up" else (c[0] - c[2]) if ref_kind == "down" else c[3]

    blocks = list(clusters)
    cs = [clusters[b] for b in blocks]
    counts = [int(c[0]) for c in cs]
    diff = _bounded(cluster_ci([c[1] - ref_of(c) for c in cs], counts, blocks), -1.0, 1.0)
    u = up / n
    clim = u * (1.0 - u)
    brier = sum(r["brier_sum"] for r in records) / n
    bs = cluster_ci([c[4] - (c[0] * u * u - 2.0 * u * c[2] + c[2]) for c in cs], counts, blocks)
    bss = 1.0 - brier / clim if clim > 0 else None
    bss_ci = [-bs[2] / clim, min(-bs[1] / clim, 1.0)] if bs is not None and clim > 0 else None
    bins: dict[int, list[float]] = {}
    for r in records:
        b = bins.setdefault(r["bin"], [0, 0.0, 0])
        b[0] += r["n"]
        b[1] += r["p_sum"]
        b[2] += r["up"]
    ece = sum(abs(b[1] / b[0] - b[2] / b[0]) * b[0] for b in bins.values() if b[0]) / n
    reliability = sum((b[1] / b[0] - b[2] / b[0]) ** 2 * b[0] for b in bins.values() if b[0]) / n
    resolution = sum((b[2] / b[0] - u) ** 2 * b[0] for b in bins.values() if b[0]) / n
    days: dict[int, list[float]] = {}
    for r in records:
        dd = days.setdefault(r["hr"] * 3600 // 86400 * 86400, [0, 0, 0, 0, 0.0])
        dd[0] += r["n"]
        dd[1] += r["hits"]
        dd[2] += r["up"]
        dd[3] += r["flat"]
        dd[4] += r["brier_sum"]
    daily = []
    for day in sorted(days):
        dn, dh, du, df, db = days[day]
        kind, rh = best_constant(int(du), int(dn - du), int(df))
        ud = du / dn
        daily.append({"time": int(day), "n": int(dn), "hit_rate": dh / dn, "reference": rh / dn, "reference_kind": kind,
                      "brier": db / dn, "brier_ref": ud * (1.0 - ud)})
    by_direction = []
    for d in (1, -1, 0):
        dn, dh = by_d.get(d, [0, 0])
        by_direction.append({"direction": d, "label": DIRECTION_LABEL[d], "n": int(dn), "hits": int(dh),
                             "accuracy": dh / dn if dn else None, "base_rate": rate[DIRECTION_OUTCOME[d]]})
    group = {
        **base, "n": int(n), "hits": int(hits), "accuracy": hits / n, "windows": windows, "horizon": _hz_text(main_hz),
        "window": _hz_text(window_hz),
        "outcomes": {"up": int(up), "down": int(down), "flat": int(flat)},
        "reference": {"kind": ref_kind, "label": OUTCOME_LABEL[ref_kind], "hits": int(ref_hits),
                      "accuracy": ref_hits / n},
        "chance": chance, "skill_pp": (hits - ref_hits) / n * 100.0,
        "skill_ci": [diff[1] * 100.0, diff[2] * 100.0] if diff is not None else None,
        "brier": brier, "brier_ref": clim, "bss": bss, "bss_ci": bss_ci, "ece_pp": ece * 100.0,
        "reliability": reliability, "resolution": resolution, "by_direction": by_direction, "daily": daily,
    }
    group.update(_verdict(group))
    return group


def _verdict(g: dict) -> dict:
    """verdict (none | insufficient | no_edge | better | worse) and its Spanish explanation, always as "x de n"."""
    n, hits, ref = g["n"], g["hits"], g["reference"]
    acc = f"{fmt_num(hits, 0)} de {fmt_num(n, 0)} ({fmt_prob(hits / n, 1)})"
    ref_txt = f"{fmt_num(ref['hits'], 0)} de {fmt_num(n, 0)} ({fmt_prob(ref['accuracy'], 1)}, {ref['label']})"
    windows = int(g["windows"])
    noun = "ventana independiente" if windows == 1 else "ventanas independientes"
    win_txt = f"{windows} {noun} de {g.get('window') or g['horizon']}"
    ci = g.get("skill_ci")
    brier_text = None
    if g.get("brier") is not None and g.get("brier_ref") is not None:
        brier_text = f"Brier {fmt_num(g['brier'], 3)} frente a {fmt_num(g['brier_ref'], 3)} de la climatología"
        if g.get("bss") is not None:
            brier_text += f" (habilidad {fmt_num(g['bss'] * 100.0, 0, True)} %"
            if g.get("bss_ci") and windows >= MIN_WINDOWS:
                lo, hi = g["bss_ci"]
                brier_text += f"; IC 95 %: {fmt_num(lo * 100.0, 0, True)} a {fmt_num(hi * 100.0, 0, True)} %"
            brier_text += ")"
    if windows < MIN_WINDOWS or n < MIN_WINDOWS:
        return {"verdict": "insufficient", "brier_text": brier_text,
                "verdict_text": f"Muestra insuficiente: aciertos {acc} frente a {ref_txt} en solo {win_txt}; se "
                                f"necesitan al menos {MIN_WINDOWS} para sacar conclusiones."}
    diff = f"{fmt_num(g['skill_pp'], 1, True)} pp"
    ci_txt = f"IC 95 %: {fmt_num(ci[0], 1, True)} a {fmt_num(ci[1], 1, True)} pp" if ci else "sin intervalo"
    if ci and ci[0] > 0:
        return {"verdict": "better", "brier_text": brier_text,
                "verdict_text": f"Supera a la referencia: aciertos {acc} frente a {ref_txt}, diferencia {diff} ({ci_txt},"
                                f" {win_txt}). Es una medición del pasado, no una garantía."}
    if ci and ci[1] < 0:
        return {"verdict": "worse", "brier_text": brier_text,
                "verdict_text": f"Por debajo de la referencia: aciertos {acc} frente a {ref_txt}, diferencia {diff} "
                                f"({ci_txt}, {win_txt})."}
    return {"verdict": "no_edge", "brier_text": brier_text,
            "verdict_text": f"Sin ventaja demostrada: aciertos {acc} frente a {ref_txt}; la diferencia ({diff}) cabe en "
                            f"el margen de error ({ci_txt}, {win_txt})."}


def _scanner_label(key: str) -> str:
    market, tf, horizon = parse_model_key(key)
    return f"Escáner {market} {tf} · horizonte {_hz_text(horizon * 1000)}"


async def skill_report(db: Any, *, source: str | None = None, model_key_value: str | None = None,
                       days: float | None = 7.0, now: int | None = None) -> dict:
    """Out-of-sample skill of the recorded predictions versus the baseline (see the section comment): one group per
    scanner model key, the scanner as a whole (several keys), NEXUS and everything together. Cached for a minute
    (and recomputed after new outcomes). days=None: every stored row (resolved scanner rows: ~30 days)."""
    days = None if days is None or float(days) <= 0 else float(clamp(float(days), 1.0 / 24.0, 400.0))
    gen = _SKILL_GEN.get(db, 0) if _weakrefable(db) else 0
    key = (_db_token(db), source, model_key_value, days, gen)
    cached = _SKILL_CACHE.get(key) if now is None else None
    if cached is not None:
        return cached
    now_v = int(now if now is not None else now_ms())
    since = now_v - int(days * 86_400_000) if days else 0
    records = await _skill_records(db, since, source, model_key_value)
    groups: list[dict] = []
    scanner_keys = sorted({r["mkey"] for r in records if r["source"] == "scanner" and r["mkey"]})
    for k in scanner_keys:
        groups.append(skill_group([r for r in records if r["source"] == "scanner" and r["mkey"] == k],
                                  gid=f"scanner:{k}", label=_scanner_label(k), source="scanner", model_key_value=k))
    scanner = [r for r in records if r["source"] == "scanner"]
    if len(scanner_keys) != 1 and scanner:
        groups.append(skill_group(scanner, gid="scanner", label="Escáner (todos los modelos)", source="scanner",
                                  common_blocks=True))
    nexus = [r for r in records if r["source"] == "nexus"]
    if nexus and model_key_value is None:
        groups.append(skill_group(nexus, gid="nexus", label="NEXUS (señales)", source="nexus", common_blocks=True))
    if len(groups) > 1 or not groups:
        groups.append(skill_group(records, gid="all", label="Todas las predicciones", common_blocks=True))
    report = clean_report({
        "generated_at": now_v, "days": days, "since": since or None, "min_windows": MIN_WINDOWS,
        "groups": groups,
        "notes": [
            "Referencia: acertar siempre el resultado más frecuente del mismo periodo (alcista, bajista o neutral); se "
            "conoce solo a posteriori, por eso es exigente.",
            "Azar con la misma mezcla: el acierto esperado si las predicciones del modelo no tuvieran relación con el "
            "resultado.",
            "Brier frente a climatología: decir siempre la frecuencia de subida del periodo; habilidad > 0 % = mejor.",
            "Los intervalos del 95 % agrupan las predicciones por ventanas independientes del horizonte (las de una "
            "misma ventana se mueven juntas) y cuentan también la correlación entre ventanas vecinas; sin conclusiones "
            f"con menos de {MIN_WINDOWS} ventanas.",
        ],
    })
    if now is None:  # a long window scans every stored row (~0,5 M scanner rows a month): kept longer
        _SKILL_CACHE.set(key, report, ttl=SKILL_CACHE_TTL_S if days and days <= 7 else SKILL_CACHE_LONG_TTL_S)
    return report


def _weakrefable(db: Any) -> bool:
    try:
        weakref.ref(db)
        return True
    except TypeError:
        return False


def _db_token(db: Any) -> Any:
    """A process-unique cache token per database object (an id() is reused once its object is gone, so a cached
    report of a closed database could be served for a new one)."""
    try:
        token = _DB_TOKENS.get(db)
        if token is None:
            token = _DB_TOKENS[db] = next(_TOKEN_SEQ)
        return token
    except TypeError:
        return ("id", id(db))


def clean_report(obj: Any) -> Any:
    """Round floats (6 decimals) and replace NaN/inf with None, recursively."""
    if isinstance(obj, float):
        return round(obj, 6) if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean_report(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_report(v) for v in obj]
    return obj


# ----------------------------------------------------------------------------- offline replay of the scanner
# Resolved scanner rows keep their packed feature vector `scanner.replay_days` (default 7). The replay predicts every
# stored scan again, in order, with fresh models of each design that have learned only the scans whose horizon had
# already ended at that moment (what the live system could know), and compares them with the recorded live predictions
# and the baseline on exactly the same rows. It measures designs on past data: with few independent windows it says
# so. Cost: ≈ 0,13 ms per row (a week of the default background scan, ≈ 110 k rows: ≈ 15 s in a worker thread).

REPLAY_MAX_ROWS = 300_000  # rows replayed at most (the oldest of the period first)
REPLAY_CHUNK = 20_000  # rows per query: memory stays bounded whatever the period (see Replayer)
REPLAY_DESIGNS = ("v2", "v1", "recorded")
REPLAY_LABELS = {"v2": "Modelo v2 (actual, entrenado desde cero en la repetición)",
                 "v1": "Modelo v1 (anterior, entrenado desde cero en la repetición)",
                 "recorded": "Predicciones registradas en vivo"}
_AGG_FIELDS = ("n", "hits", "up", "flat", "p_sum", "brier_sum")


def _replay_record(hz_ms: int, target_ms: int, direction: int, p: float, ret: float) -> dict:
    """One row as a skill record (see _skill_records), judged with the live rule."""
    horizon_s = hz_ms / 1000.0
    up = 1 if ret > 0 else 0
    return {"source": "scanner", "mkey": "", "hz": hz_ms, "hr": target_ms // 3_600_000, "d": direction,
            "bin": prob_bin(p), "n": 1, "hits": 1 if judge_prediction(direction, ret, horizon_s) else 0, "up": up,
            "flat": 1 if abs(ret) <= flat_band_pct(horizon_s) else 0, "p_sum": p, "brier_sum": (p - up) ** 2}


class _DesignAgg:
    """Skill records of one replayed design, aggregated like `_skill_records` (per target hour, call and bin) as they
    come; the per-row records are kept only on request (tests)."""

    def __init__(self, keep: bool) -> None:
        self.groups: dict[tuple[int, int, int, int], dict] = {}
        self.rows: list[dict] | None = [] if keep else None
        self.n = 0

    def add(self, rec: dict) -> None:
        self.n += 1
        if self.rows is not None:
            self.rows.append(rec)
        key = (rec["hz"], rec["hr"], rec["d"], rec["bin"])
        agg = self.groups.get(key)
        if agg is None:
            self.groups[key] = dict(rec)
        else:
            for name in _AGG_FIELDS:
                agg[name] += rec[name]

    def records(self) -> list[dict]:
        return list(self.groups.values())


class Replayer:
    """Prequential replay of the stored scans of one model key (pure: no database).

    Rows arrive in created_at order, possibly in several chunks (a scan split between two chunks is completed by the
    next one). Each scan is predicted by fresh v2 and v1 models that have learned only the scans whose horizon had
    ended by then (what the live system could know), then waits in a queue until its own horizon ends. Vectors are
    decoded per scan and dropped once learned and the outcomes are aggregated as they come, so the memory does not
    grow with the number of rows (≈ one horizon of decoded scans)."""

    def __init__(self, key: str, *, keep_records: bool = False) -> None:
        self.key = key
        self.market, self.tf, horizon = parse_model_key(key)
        self.hz_ms = int(horizon) * 1000
        self.v2 = ScannerModel(key)
        self.v1 = OnlineLogit()
        self.aggs = {k: _DesignAgg(keep_records) for k in REPLAY_DESIGNS}
        self.paired: dict[tuple[int, int], list[float]] = {}  # (horizon, window) → [n, Δhits, ΔBrier] of v2 − v1
        self.scans = 0
        self.with_context = 0
        # scans waiting for their horizon: (created, [(row, features)], v2 inputs, v1 vectors) — the inputs built for
        # the prediction are the ones the models learn from, so nothing is computed twice
        self._queue: deque[tuple[int, list[tuple[dict, dict]], Any, list[Any]]] = deque()
        self._created: int | None = None
        self._pending: list[dict] = []

    def feed(self, rows: Iterable[dict]) -> None:
        for r in rows:
            if r.get("outcome_return_pct") is None or int(r["target_at"]) - int(r["created_at"]) != self.hz_ms:
                continue
            created = int(r["created_at"])
            if self._created is not None and created != self._created and self._pending:
                self._scan(self._created, self._pending)
                self._pending = []
            self._created = created
            self._pending.append(r)

    def finish(self) -> dict:
        if self._pending and self._created is not None:
            self._scan(self._created, self._pending)
            self._pending = []
        designs = [skill_group(self.aggs[k].records(), gid=k, label=REPLAY_LABELS[k], source="scanner",
                               model_key_value=self.key) for k in REPLAY_DESIGNS]
        out = {"key": self.key, "rows": self.aggs["v2"].n, "scans": self.scans, "with_context": self.with_context,
               "horizon": _hz_text(self.hz_ms), "designs": designs,
               "comparison": _paired_verdict(self.paired, self.hz_ms)}
        if self.aggs["v2"].rows is not None:
            out["records"] = {k: self.aggs[k].rows for k in REPLAY_DESIGNS}
        return out

    @staticmethod
    def _base(f: dict) -> dict:
        return {k: f[k] for k in FEATURE_NAMES if k in f}

    def _learn(self, created: int, items: list[tuple[dict, dict]], batch: Any, vectors: list[Any]) -> None:
        self.v2.learn_scan([LearnSample(key=self.key, feats=f, up=1 if float(r["outcome_return_pct"]) > 0 else 0,
                                        label_time=int(r["target_at"]) // 1000, created_ms=created,
                                        ret_pct=float(r["outcome_return_pct"]), symbol=str(r["symbol"]),
                                        direction=int(r["direction"] or 0), correct=int(r["correct"] or 0))
                            for r, f in items], batch=batch)
        for (r, _), x in zip(items, vectors):
            self.v1.update(x, 1 if float(r["outcome_return_pct"]) > 0 else 0, int(r["target_at"]) // 1000)

    def _scan(self, created: int, raw: list[dict]) -> None:
        while self._queue and self._queue[0][0] + self.hz_ms <= created:
            self._learn(*self._queue.popleft())
        items = [(r, f) for r in raw if (f := decode_features(r.get("features")))]
        if not items:
            return
        self.scans += 1
        feats_list = [f for _, f in items]
        ctx = {k: feats_list[0][k] for k in CONTEXT_NAMES if k in feats_list[0]}
        if context_regime(ctx) is None:  # stored without its context (older encoding): rebuilt from the rows
            ctx = market_context(feats_list, [str(r["symbol"]) for r, _ in items], self.market)
        else:
            self.with_context += len(items)
        batch = self.v2.batch(feats_list, ctx)
        _, p_up, p_move = self.v2.predict(batch)
        vectors = []
        for i, (r, f) in enumerate(items):
            ret, target = float(r["outcome_return_pct"]), int(r["target_at"])
            p2 = float(p_up[i])
            rec2 = _replay_record(self.hz_ms, target, decide_direction(p2, float(p_move[i])), p2, ret)
            base = self._base(f)
            x1 = self.v1.vector(base)
            vectors.append(x1)
            # the rule prior reads base features only: the one in the v2 inputs is the same value
            p1, s1, _, _ = evaluate_features(base, len(base) / len(FEATURE_NAMES), self.tf, self.v1, x1,
                                             float(batch.prior[i]))
            a1 = action_for(s1)
            rec1 = _replay_record(self.hz_ms, target, 1 if a1 == "BUY" else -1 if a1 == "SELL" else 0, p1, ret)
            self.aggs["v2"].add(rec2)
            self.aggs["v1"].add(rec1)
            self.aggs["recorded"].add(_replay_record(self.hz_ms, target, int(r["direction"] or 0),
                                                     float(r["prob_up"]), ret))
            c = self.paired.setdefault((self.hz_ms, rec2["hr"] * 3_600_000 // self.hz_ms), [0, 0.0, 0.0])
            c[0] += 1
            c[1] += rec2["hits"] - rec1["hits"]
            c[2] += rec2["brier_sum"] - rec1["brier_sum"]
        self._queue.append((created, items, batch, vectors))


def replay_compute(rows: list[dict], key: str, *, keep_records: bool = False) -> dict:
    """Prequential replay of the stored scans of one model key (pure: no database). `rows` = resolved, scored rows
    with features (symbol, created_at, target_at, direction, prob_up, correct, outcome_return_pct, features), in any
    order. `keep_records` adds the per-row records of every design (tests)."""
    replayer = Replayer(key, keep_records=keep_records)
    replayer.feed(sorted(rows, key=lambda r: int(r["created_at"])))
    return replayer.finish()


def _paired_verdict(clusters: dict[tuple[int, int], list[float]], hz_ms: int) -> dict:
    """v2 − v1 on the same rows (per window: [n, Δhits, ΔBrier]): hit-rate difference (pp) and Brier difference, 95 %
    intervals clustered by windows (with the Newey–West term for neighbouring ones), and an honest verdict."""
    n = int(sum(c[0] for c in clusters.values()))
    if not n:
        return {"n": 0, "windows": 0, "diff_pp": None, "diff_ci": None, "brier_diff": None, "brier_ci": None,
                "verdict": "none", "text": "No hay predicciones con su vector de características guardado en el "
                                           "periodo: todavía no se puede repetir."}
    blocks = list(clusters)
    cs = [clusters[b] for b in blocks]
    counts = [int(c[0]) for c in cs]
    windows = len(cs)
    acc = _bounded(cluster_ci([c[1] for c in cs], counts, blocks), -1.0, 1.0)
    bri = _bounded(cluster_ci([c[2] for c in cs], counts, blocks), -1.0, 1.0)
    diff = sum(c[1] for c in cs) / n
    bdiff = sum(c[2] for c in cs) / n
    out = {"n": n, "windows": windows, "diff_pp": diff * 100.0,
           "diff_ci": [acc[1] * 100.0, acc[2] * 100.0] if acc else None, "brier_diff": bdiff,
           "brier_ci": [bri[1], bri[2]] if bri else None}
    head = (f"Diferencia v2 − v1 en la repetición: {fmt_num(diff * 100.0, 1, True)} pp de acierto y "
            f"{fmt_num(bdiff, 3, True)} de Brier (negativo = mejor) sobre {fmt_num(n, 0)} predicciones")
    noun = "ventana independiente" if windows == 1 else "ventanas independientes"
    if windows < MIN_WINDOWS or n < MIN_WINDOWS:
        out.update(verdict="insufficient", text=f"{head}, pero solo {windows} {noun} de "
                                                 f"{_hz_text(max(hz_ms, 3_600_000))}: no se puede "
                                                 f"afirmar que un diseño sea mejor (se necesitan {MIN_WINDOWS}).")
    elif acc and acc[1] > 0:
        out.update(verdict="better", text=f"{head}: el v2 acierta más (IC 95 %: {fmt_num(acc[1] * 100, 1, True)} a "
                                          f"{fmt_num(acc[2] * 100, 1, True)} pp). Medición sobre datos pasados, no una "
                                          "garantía.")
    elif acc and acc[2] < 0:
        out.update(verdict="worse", text=f"{head}: el v2 acierta menos (IC 95 %: {fmt_num(acc[1] * 100, 1, True)} a "
                                         f"{fmt_num(acc[2] * 100, 1, True)} pp).")
    else:
        ci = f"IC 95 %: {fmt_num(acc[1] * 100, 1, True)} a {fmt_num(acc[2] * 100, 1, True)} pp" if acc else "sin IC"
        out.update(verdict="no_edge", text=f"{head}: sin diferencia demostrada ({ci}).")
    return out


class ReplayBusy(RuntimeError):
    """Another offline replay of the same database is still running."""


_REPLAY_RUNNING: set = set()


async def replay_report(db: Any, *, model_key_value: str | None = None, days: float = REPLAY_DAYS_DEFAULT,
                        now: int | None = None) -> dict:
    """Offline prequential replay of the scanner on the stored feature vectors (see the section comment). One at a
    time per database (it takes seconds of CPU): a second request while one runs raises ReplayBusy."""
    token = _db_token(db)
    if token in _REPLAY_RUNNING:
        raise ReplayBusy("Ya hay una repetición del escáner en curso: espera a que termine.")
    _REPLAY_RUNNING.add(token)
    try:
        return await _replay_report(db, model_key_value=model_key_value, days=days, now=now)
    finally:
        _REPLAY_RUNNING.discard(token)


async def _replay_report(db: Any, *, model_key_value: str | None, days: float, now: int | None) -> dict:
    now_v = int(now if now is not None else now_ms())
    days = float(clamp(float(days or REPLAY_DAYS_DEFAULT), 1.0 / 24.0, 30.0))
    since = now_v - int(days * 86_400_000)
    stored = "source='scanner' AND resolved=1 AND correct IS NOT NULL AND outcome_return_pct IS NOT NULL " \
             "AND features IS NOT NULL AND target_at >= ?"
    key = model_key_value
    if not key:
        top = await db.fetchone(
            f"SELECT model_key, COUNT(*) AS n FROM predictions WHERE {stored} AND model_key IS NOT NULL "
            "GROUP BY model_key ORDER BY 2 DESC LIMIT 1", (since,))
        key = top["model_key"] if top else None
    base = {"generated_at": now_v, "days": days, "since": since, "min_windows": MIN_WINDOWS,
            "note": "Repetición secuencial: cada predicción se evalúa antes de aprender de ella; cada escaneo se "
                    "predice solo con lo aprendido de los escaneos cuyo horizonte ya había vencido, así que nada se "
                    "aprende antes de tiempo."}
    if not key:
        return clean_report({**base, "key": None, "rows": 0, "scans": 0, "with_context": 0, "horizon": None,
                             "truncated": False, "designs": [], "comparison": _paired_verdict({}, 14_400_000)})
    replayer = Replayer(key)
    after_created, after_id, fetched = -1, -1, 0
    while fetched < REPLAY_MAX_ROWS:  # keyset pages in (created_at, id) order: a scan may span two pages
        limit = int(min(REPLAY_CHUNK, REPLAY_MAX_ROWS - fetched))
        rows = await db.fetchall(
            "SELECT id, symbol, created_at, target_at, direction, prob_up, correct, outcome_return_pct, features "
            f"FROM predictions WHERE {stored} AND model_key=? AND (created_at > ? OR (created_at = ? AND id > ?)) "
            f"ORDER BY created_at, id LIMIT {limit}",
            (since, key, after_created, after_created, after_id),
        )
        if not rows:
            break
        await asyncio.to_thread(replayer.feed, rows)
        fetched += len(rows)
        after_created, after_id = int(rows[-1]["created_at"]), int(rows[-1]["id"])
        if len(rows) < limit:
            break
    result = await asyncio.to_thread(replayer.finish)
    return clean_report({**base, **result, "truncated": fetched >= REPLAY_MAX_ROWS})
