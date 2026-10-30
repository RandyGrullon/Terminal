"""BE-SIM: prediction resolution, gate outcome walking (TP1 vs SL ordering), learner feedback, aggregations."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config as nexus_config
from app.data.base import CandleSet
from app.sim import evaluator as ev
from app.sim.scanner import FEATURE_NAMES, ScannerService

NOW = 1_760_000_400_000  # ms
H = 3600


def test_judge_and_flat_band():
    assert ev.flat_band_pct(4 * H) == pytest.approx(0.5)
    assert ev.flat_band_pct(60) == pytest.approx(0.1)  # floor
    assert ev.flat_band_pct(400 * H) == pytest.approx(1.5)  # cap
    assert ev.judge_prediction(1, 0.3, 4 * H) and not ev.judge_prediction(1, -0.3, 4 * H)
    assert ev.judge_prediction(-1, -0.01, 4 * H) and not ev.judge_prediction(-1, 0.0, 4 * H)
    assert ev.judge_prediction(0, 0.4, 4 * H) and not ev.judge_prediction(0, 0.8, 4 * H)
    assert ev.outcome_return_pct(100.0, 101.5) == pytest.approx(1.5)
    assert ev.outcome_return_pct(0.0, 1.0) is None


def _bars(*rows: tuple[float, float, float], t0: int = 1000 * H) -> list[tuple[int, float, float, float]]:
    """rows of (high, low, close) starting at bar t0 + H (the decision bar is t0)."""
    return [(t0 + (i + 1) * H, h, l, c) for i, (h, l, c) in enumerate(rows)]


def test_gate_outcome_tp1_before_sl_ordering():
    t0, target = 1000 * H, 1000 * H + 10 * H
    now = target // 1 + H
    # long: TP1 (102) on bar 2, SL (98) later -> TP1
    bars = _bars((101, 99.5, 100.5), (102.2, 100, 101), (100, 97.5, 98))
    assert ev.gate_outcome("long", 100, 98, 102, bars, t0, target, now, H) == ("TP1", pytest.approx(1.0))
    # both inside the same bar -> stop first (conservative)
    bars = _bars((102.5, 97.9, 100))
    assert ev.gate_outcome("long", 100, 98, 102, bars, t0, target, now, H) == ("SL", -1.0)
    # SL before TP1
    bars = _bars((100.5, 97.9, 98.5), (103, 99, 102))
    assert ev.gate_outcome("long", 100, 98, 102, bars, t0, target, now, H)[0] == "SL"
    # short mirrored: TP1 at 98 first
    bars = _bars((100.5, 99, 99.5), (100, 97.9, 98.2))
    assert ev.gate_outcome("short", 100, 102, 98, bars, t0, target, now, H) == ("TP1", pytest.approx(1.0))
    # the decision bar itself and bars after the target are ignored
    bars = [(t0, 110, 90, 100)] + _bars((101, 99, 100.6))
    assert ev.gate_outcome("long", 100, 98, 102, bars, t0, target, now, H) is None  # data does not reach target
    with_gap = bars + [(target, 120, 80, 100)]  # a later bar proves the data covers the horizon (gaps are ok)
    assert ev.gate_outcome("long", 100, 98, 102, with_gap, t0, target, now, H) == ("NONE", pytest.approx(0.3))
    full = [(t0 + (i + 1) * H, 101, 99, 100.6) for i in range(9)] + [(target, 120, 80, 100)]
    assert ev.gate_outcome("long", 100, 98, 102, full, t0, target, now, H) == ("NONE", pytest.approx(0.3))
    # still pending before the target time
    assert ev.gate_outcome("long", 100, 98, 102, full[:3], t0, target, t0 + 3 * H, H) is None
    # missing levels -> NONE without R once the horizon is over
    assert ev.gate_outcome("long", None, 98, 102, full, t0, target, now, H) == ("NONE", None)
    assert ev.gate_won("TP1", 1.0) is True and ev.gate_won("SL", -1) is False
    assert ev.gate_won("NONE", 0.2) is True and ev.gate_won("NONE", None) is None


class _QuoteHub:
    def __init__(self, prices: dict[str, float], candles: dict[tuple[str, str], CandleSet] | None = None):
        self.prices = prices
        self.candles = candles or {}

    async def quotes(self, symbols):
        return {s: {"symbol": s, "price": self.prices[s]} for s in symbols if s in self.prices}

    async def last_price(self, symbol):
        return self.prices.get(symbol)

    async def get_candles(self, symbol, tf, limit=500):
        cs = self.candles.get((symbol, tf))
        return cs.tail(limit) if cs is not None else CandleSet.empty(symbol, tf)


async def _insert_pred(db, *, source="scanner", symbol="BINANCE:BTCUSDT", created, target, price=100.0,
                       direction=1, prob=0.6, key="crypto|1h|14400", feats=None):
    return await db.execute(
        "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, prob_up, score, "
        "features) VALUES (?, ?, '1h', ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, symbol, key if source == "scanner" else None, created, target, price, direction, prob,
         2 * prob - 1, json.dumps({"v": 1, "x": feats or {"rec_60": 0.5, "macd_60": 1.0}}) if source == "scanner" else None),
    )


def _hourly(symbol: str, closes: list[float], t0_s: int, highs=None, lows=None) -> CandleSet:
    rows = [{"time": t0_s + i * H, "open": c, "high": (highs or {}).get(i, c), "low": (lows or {}).get(i, c),
             "close": c, "volume": 1.0} for i, c in enumerate(closes)]
    return CandleSet.from_rows(rows, symbol, "1h")


def test_run_once_resolves_predictions_and_trains_scanner(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            created = NOW - 4 * H * 1000
            due = NOW - 60_000  # one minute late -> current quotes
            await _insert_pred(db, created=created, target=due, direction=1, prob=0.7)  # up, right
            await _insert_pred(db, symbol="BINANCE:ETHUSDT", created=created, target=due, direction=-1, prob=0.3)
            await _insert_pred(db, source="nexus", symbol="SIM:NEXUS", created=created, target=due, direction=0,
                               prob=0.5)
            await _insert_pred(db, symbol="BINANCE:BNBUSDT", created=NOW, target=NOW + 4 * H * 1000)  # not due yet
            # 2 days late -> resolved with the candle at the target time
            late_target = NOW - 2 * 86400 * 1000
            t_bar = (late_target // 1000) - 5 * H
            closes = [100, 100, 100, 100, 96, 97, 98] + [120] * 46  # bars up to 'now' (53 h)
            await _insert_pred(db, symbol="BINANCE:SOLUSDT", created=late_target - 4 * H * 1000, target=late_target,
                               direction=1, prob=0.65)
            # very old and without any price -> expired
            await _insert_pred(db, symbol="BINANCE:NOPEUSDT", created=NOW - 9 * 86400 * 1000,
                               target=NOW - 8 * 86400 * 1000)
            rt.hub = _QuoteHub(
                {"BINANCE:BTCUSDT": 102.0, "BINANCE:ETHUSDT": 101.0, "SIM:NEXUS": 100.2},
                {("BINANCE:SOLUSDT", "1h"): _hourly("BINANCE:SOLUSDT", closes, t_bar)},
            )
            rt.scanner = ScannerService(rt)
            await rt.scanner.start()
            evaluator = ev.PredictionEvaluator(rt)
            await evaluator.start()
            out = await evaluator.run_once(now=NOW)
            assert out["predictions"] == 4 and out["expired"] == 1 and out["scanner_updates"] == 3
            rows = {r["symbol"]: r for r in await db.fetchall("SELECT * FROM predictions")}
            assert rows["BINANCE:BTCUSDT"]["correct"] == 1 and rows["BINANCE:BTCUSDT"]["outcome_return_pct"] == pytest.approx(2.0)
            assert rows["BINANCE:ETHUSDT"]["correct"] == 0
            assert rows["SIM:NEXUS"]["correct"] == 1  # neutral and the move stayed inside the band
            sol = rows["BINANCE:SOLUSDT"]
            assert sol["resolved"] == 1 and sol["outcome_price"] == 96.0 and sol["correct"] == 0
            assert rows["BINANCE:NOPEUSDT"]["resolved"] == 1 and rows["BINANCE:NOPEUSDT"]["correct"] is None
            state = await db.fetchone("SELECT updates FROM model_state WHERE kind='scanner' AND key='crypto|1h|14400'")
            assert state["updates"] == 3
            learn = [e["message"] for e in rt.bus.recent(level="learn")]
            assert any(m.startswith("Modelo escáner crypto|1h: 3 predicciones resueltas") for m in learn)
            st = evaluator.status()
            assert st == {"running": True, "pending": 1, "resolved_total": 4}
            stats = await ev.prediction_stats(db)
            assert stats["total"] == 6 and stats["resolved"] == 4 and stats["pending"] == 1
            assert stats["hit_rate"] == pytest.approx(0.5)
            assert set(stats["by_source"]) == {"scanner", "nexus"} and stats["by_source"]["nexus"]["resolved"] == 1
            assert stats["rolling"] and sum(r["n"] for r in stats["rolling"]) == 4
            assert stats["brier"] is not None and 0 <= stats["brier"] <= 1
            listed = await ev.list_predictions(db, source="scanner", resolved=True)
            assert {p["symbol"] for p in listed} == {"BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:SOLUSDT",
                                                     "BINANCE:NOPEUSDT"}
            p0 = listed[0]
            assert set(p0) == {"id", "source", "symbol", "tf", "created_at", "target_at", "price", "direction",
                               "prob_up", "score", "resolved", "outcome_price", "outcome_return_pct", "correct"}
            assert isinstance(p0["resolved"], bool)
            # a second run has nothing left to do
            again = await evaluator.run_once(now=NOW)
            assert again["predictions"] == 0
            await evaluator.stop()
            await rt.scanner.stop()

    asyncio.run(scenario())


async def _insert_gate(db, *, symbol="BINANCE:BTCUSDT", time_s, direction="long", verdict="TAKE", entry=100.0,
                       sl=98.0, tp1=102.0, target_ms, source="scanner"):
    await db.execute(
        "INSERT INTO gate_log(symbol, tf, time, direction, verdict, score, win_prob, entry, sl, tp1, tp2, checks, taken, "
        "source, created_at, target_at) VALUES (?, '1h', ?, ?, ?, 70, 0.6, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (symbol, time_s, direction, verdict, entry, sl, tp1, tp1 + 2, json.dumps({"trend_alignment": 1, "volume": 0,
                                                                                    "momentum": None}),
         source, time_s * 1000, target_ms),
    )


def test_gate_log_resolution_and_learning_summary(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(ev, "_gate_learner_api", lambda: None)  # engine learner absent -> per-check counts

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            t0 = (NOW // 1000) - 30 * H
            t0 -= t0 % H
            closes = [100.0] * 31  # bars up to 'now'
            highs = {3: 102.5}  # BTC long hits TP1 on bar 3
            cs_btc = _hourly("BINANCE:BTCUSDT", closes, t0, highs=highs)
            cs_eth = _hourly("BINANCE:ETHUSDT", closes, t0, lows={2: 97.0})  # ETH long hits SL on bar 2
            rt.hub = _QuoteHub({}, {("BINANCE:BTCUSDT", "1h"): cs_btc, ("BINANCE:ETHUSDT", "1h"): cs_eth})
            await _insert_gate(db, time_s=t0, target_ms=(t0 + 10 * H) * 1000)
            await _insert_gate(db, symbol="BINANCE:ETHUSDT", time_s=t0, verdict="AVOID", target_ms=(t0 + 10 * H) * 1000)
            await _insert_gate(db, symbol="BINANCE:ETHUSDT", time_s=t0 + 20 * H, verdict="WAIT",
                               target_ms=(t0 + 60 * H) * 1000)  # still pending (horizon not reached)
            evaluator = ev.PredictionEvaluator(rt)
            out = await evaluator.run_once(now=NOW)
            assert out["gate"] == 2
            rows = {(r["symbol"], r["verdict"]): r for r in await db.fetchall("SELECT * FROM gate_log")}
            assert rows[("BINANCE:BTCUSDT", "TAKE")]["outcome"] == "TP1"
            assert rows[("BINANCE:ETHUSDT", "AVOID")]["outcome"] == "SL"
            assert rows[("BINANCE:ETHUSDT", "WAIT")]["resolved"] == 0
            counts = json.loads((await db.fetchone(
                "SELECT state FROM model_state WHERE kind='gate' AND key='check_stats'"))["state"])
            assert counts["samples"] == 2
            assert counts["checks"]["trend_alignment"] == {"pass_win": 1, "pass_loss": 1, "fail_win": 0, "fail_loss": 0}
            assert counts["checks"]["volume"]["fail_win"] == 1 and "momentum" not in counts["checks"]
            assert await db.fetchone("SELECT 1 FROM model_state WHERE kind='gate' AND key='global'") is None
            summary = await ev.learning_summary(db, rt.settings.all())
            assert summary["gate"] == {"decisions": 3, "taken": 1, "avoided": 1, "avoided_would_lose_rate": 1.0,
                                       "taken_win_rate": 1.0}
            assert set(summary) == {"scanner_model", "nexus_models", "gate", "predictions", "timeline"}
            assert summary["scanner_model"] is None and summary["nexus_models"] == []

    asyncio.run(scenario())


def test_gate_learner_engine_api_is_used_when_present(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    class Learner:
        def __init__(self, state=None):
            self.state = dict(state or {"n": 0, "wins": 0})

        @classmethod
        def from_state(cls, state):
            return cls(state)

        def update(self, checks, won):
            assert isinstance(checks, dict)
            self.state["n"] += 1
            self.state["wins"] += int(won)

        def to_state(self):
            return self.state

    monkeypatch.setattr(ev, "_gate_learner_api", lambda: ("class", Learner))

    async def scenario():
        async with runtime_factory() as rt:
            evaluator = ev.PredictionEvaluator(rt)
            mode = await evaluator._update_gate_learner([({"a": 1}, True), ({"a": 0}, False)])
            assert mode == "engine"
            row = await rt.db.fetchone("SELECT state, updates FROM model_state WHERE kind='gate' AND key='global'")
            assert json.loads(row["state"]) == {"n": 2, "wins": 1} and row["updates"] == 2
            mode = await evaluator._update_gate_learner([({"a": 1}, True)])
            row = await rt.db.fetchone("SELECT state, updates FROM model_state WHERE kind='gate' AND key='global'")
            assert json.loads(row["state"]) == {"n": 3, "wins": 2} and row["updates"] == 3

    asyncio.run(scenario())


def test_learning_summary_with_models_and_calibration(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            svc = ScannerService(rt)
            await svc.learn_batch([("crypto|1h|14400", {"rec_60": 0.9}, 1, 100)] * 5)
            for i, (prob, ret) in enumerate([(0.15, -1.0), (0.12, 0.5), (0.55, 1.0), (0.58, -0.2), (0.91, 2.0)]):
                pid = await _insert_pred(db, created=NOW - (i + 10) * 86400 * 1000, target=NOW - (i + 9) * 86400 * 1000,
                                         prob=prob, direction=1 if prob > 0.5 else -1)
                await db.execute("UPDATE predictions SET resolved=1, outcome_price=?, outcome_return_pct=?, correct=?, "
                                 "resolved_at=? WHERE id=?",
                                 (100 + ret, ret, int((ret > 0) == (prob > 0.5)), NOW, pid))
            await db.execute(
                "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES "
                "('nexus', 'BINANCE:BTCUSDT|1h', ?, 640, 1759990000, ?)",
                (json.dumps({"learning": {"hit_rate": 0.57, "brier": 0.241}}), NOW),
            )
            s = await ev.learning_summary(db, rt.settings.all())
            sm = s["scanner_model"]
            assert sm["key"] == "crypto|1h|14400" and sm["updates"] == 5
            assert sm["hit_rate"] == pytest.approx(0.6) and 0 < sm["brier"] < 1
            assert len(sm["features"]) == len(FEATURE_NAMES) and sm["features"][0]["name"] == "rec_60"
            assert sm["features"][0]["label"] == "Rating técnico 1h"
            bins = {b["bin"]: b for b in sm["calibration"]}
            assert bins[1]["count"] == 2 and bins[1]["observed"] == pytest.approx(0.5)
            assert bins[5]["count"] == 2 and bins[9]["count"] == 1 and bins[9]["observed"] == 1.0
            nm = s["nexus_models"][0]
            assert nm == {"symbol": "BINANCE:BTCUSDT", "tf": "1h", "updates": 640, "hit_rate": 0.57, "brier": 0.241,
                          "trained_until": 1759990000}
            assert len(s["timeline"]) == 5 and all(set(t) == {"time", "hit_rate", "brier", "n"} for t in s["timeline"])

    asyncio.run(scenario())
