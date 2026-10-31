"""Fix round (simlearn): prediction outcomes that carry no information are never judged nor trained.

- closed-market-zero-return-labels: a zero / near-zero move (closed session, frozen price) or no bar traded between
  the prediction and its target is resolved without verdict (correct NULL) and is not a scanner training label.
- stale-local-quote-as-live: a quote flagged stale or older than 2 minutes is not a live price; the row waits and is
  resolved later from the candles.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config as nexus_config
from app.data.base import CandleSet
from app.sim import evaluator as ev
from app.sim.scanner import ScannerService, encode_features

NOW = 1_760_000_400_000  # ms (aligned to the hour)
H = 3600
KEY = "crypto|1h|14400"


class _Hub:
    def __init__(self, quotes: dict[str, dict] | None = None, candles: dict[tuple[str, str], CandleSet] | None = None):
        self.quote_map = quotes or {}
        self.candles = candles or {}

    async def quotes(self, symbols):
        return {s: dict(self.quote_map[s], symbol=s) for s in symbols if s in self.quote_map}

    async def last_price(self, symbol):
        q = self.quote_map.get(symbol)
        return q["price"] if q else None

    async def get_candles(self, symbol, tf, limit=500):
        cs = self.candles.get((symbol, tf))
        return cs.tail(limit) if cs is not None else CandleSet.empty(symbol, tf)


async def _pred(db, symbol, *, created, target, price=100.0, direction=1, prob=0.6, source="scanner"):
    feats = encode_features({"rec_60": 0.5, "macd_60": 1.0})
    return await db.execute(
        "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, prob_up, "
        "score, features) VALUES (?, ?, '1h', ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, symbol, KEY if source == "scanner" else f"{symbol}|1h", created, target, price, direction, prob,
         2 * prob - 1, feats if source == "scanner" else None),
    )


def _hourly(symbol: str, closes: list[float], t0_s: int, *, stale: bool = False) -> CandleSet:
    rows = [{"time": t0_s + i * H, "open": c, "high": c, "low": c, "close": c, "volume": 1.0}
            for i, c in enumerate(closes)]
    return CandleSet.from_rows(rows, symbol, "1h", stale=stale)


async def _row(db, pid):
    return await db.fetchone("SELECT * FROM predictions WHERE id=?", (pid,))


def test_dead_band_scales_with_the_horizon():
    assert ev.dead_band_pct(4 * H) == pytest.approx(0.01)  # 1 basis point at 4 h
    assert ev.dead_band_pct(60) == pytest.approx(0.002)
    assert not ev.measurable(0.0, 4 * H) and not ev.measurable(-0.009, 4 * H)
    assert ev.measurable(0.011, 4 * H) and ev.measurable(-0.5, 4 * H)
    assert [ev.prob_bin(p) for p in (0.0, 0.0999, 0.1, 0.3, 0.55, 0.9, 0.99)] == [0, 0, 1, 3, 5, 9, 9]


def test_closed_session_zero_return_is_not_judged_nor_trained(runtime_factory, monkeypatch):
    """'scan america' on a Saturday: the outcome price equals the creation price."""
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            created, due = NOW - 4 * H * 1000, NOW - 60_000
            buy = await _pred(db, "NASDAQ:AAPL", created=created, target=due, price=187.42, direction=1, prob=0.7)
            sell = await _pred(db, "NASDAQ:MSFT", created=created, target=due, price=400.0, direction=-1, prob=0.3)
            flat = await _pred(db, "NASDAQ:NVDA", created=created, target=due, price=120.0, direction=0, prob=0.5)
            tiny = await _pred(db, "NASDAQ:AMZN", created=created, target=due, price=200.0, direction=1, prob=0.6)
            moved = await _pred(db, "NASDAQ:META", created=created, target=due, price=500.0, direction=1, prob=0.6)
            rt.hub = _Hub({"NASDAQ:AAPL": {"price": 187.42}, "NASDAQ:MSFT": {"price": 400.0},
                           "NASDAQ:NVDA": {"price": 120.0}, "NASDAQ:AMZN": {"price": 200.01},  # +0.005 %
                           "NASDAQ:META": {"price": 505.0}})
            rt.scanner = ScannerService(rt)
            await rt.scanner.start()
            out = await ev.PredictionEvaluator(rt).run_once(now=NOW)
            assert out == {"predictions": 1, "expired": 4, "scanner_updates": 1, "gate": 0}
            for pid in (buy, sell, flat, tiny):
                r = await _row(db, pid)
                assert r["resolved"] == 1 and r["correct"] is None  # no verdict: neither "down" nor "flat = right"
                assert r["outcome_return_pct"] is not None and abs(r["outcome_return_pct"]) < 0.01
                assert r["features"] is None
            assert (await _row(db, moved))["correct"] == 1
            state = await db.fetchone(f"SELECT updates FROM model_state WHERE kind='scanner' AND key='{KEY}'")
            assert state["updates"] == 1  # only the measurable outcome trained the model
            stats = await ev.prediction_stats(db)
            assert stats["resolved"] == 1 and stats["hit_rate"] == 1.0 and stats["total"] == 5

    asyncio.run(scenario())


def test_late_resolution_needs_a_bar_traded_after_the_prediction(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            target = NOW - 2 * 86400 * 1000  # late: resolved from the candles
            created = target - 4 * H * 1000
            created_s = created // 1000
            # market closed since before the prediction: the last bar ends 1 h before it was made
            closed = await _pred(db, "NYSE:KO", created=created, target=target, price=60.0)
            ko = _hourly("NYSE:KO", [61.0, 60.5, 60.0], created_s - 4 * H)
            # outdated series (venue stopped trading) that does not reach the target
            stale = await _pred(db, "HTX:BOBAUSDT", created=created, target=target, price=0.0206)
            boba = _hourly("HTX:BOBAUSDT", [0.0207, 0.0206, 0.0208, 0.0212], created_s - H, stale=True)
            # normal 24/7 market: bars from before the prediction up to now -> judged with the close at the target
            ok = await _pred(db, "BINANCE:SOLUSDT", created=created, target=target, price=100.0, direction=1)
            sol = _hourly("BINANCE:SOLUSDT", [100.0] * 2 + [101.0] * 52, created_s - H)
            rt.hub = _Hub(candles={("NYSE:KO", "1h"): ko, ("HTX:BOBAUSDT", "1h"): boba,
                                   ("BINANCE:SOLUSDT", "1h"): sol})
            out = await ev.PredictionEvaluator(rt).run_once(now=NOW)
            assert out["predictions"] == 1 and out["expired"] == 2 and out["scanner_updates"] == 1
            for pid in (closed, stale):
                r = await _row(db, pid)
                assert r["resolved"] == 1 and r["correct"] is None and r["outcome_price"] is None
            r = await _row(db, ok)
            assert r["correct"] == 1 and r["outcome_price"] == 101.0

    asyncio.run(scenario())


def test_stale_or_old_quotes_are_not_used_as_live_prices(runtime_factory, monkeypatch):
    """TradingView fails: the hub falls back to a 9-hour-old cached close (stale)."""
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            created, due = NOW - 4 * H * 1000, NOW - 60_000
            old_ts = await _pred(db, "BINANCE:ADAUSDT", created=created, target=due, price=151.0)
            flagged = await _pred(db, "BINANCE:DOTUSDT", created=created, target=due, price=151.0)
            live = await _pred(db, "BINANCE:XRPUSDT", created=created, target=due, price=151.0)
            rt.hub = _Hub({
                "BINANCE:ADAUSDT": {"price": 150.5, "ts": NOW - 9 * H * 1000},
                "BINANCE:DOTUSDT": {"price": 150.5, "ts": NOW, "stale": True},
                "BINANCE:XRPUSDT": {"price": 152.0, "ts": NOW - 30_000},
            })
            evaluator = ev.PredictionEvaluator(rt)
            out = await evaluator.run_once(now=NOW)
            assert out["predictions"] == 1 and out["expired"] == 0
            assert (await _row(db, live))["correct"] == 1
            for pid in (old_ts, flagged):
                assert (await _row(db, pid))["resolved"] == 0  # waits for the candles
            # later the candles cover the target: resolved with the close of the last bar completed by then
            t0 = NOW // 1000 - 7 * H  # bars up to the one forming 2 h later (index 5 closes just before the target)
            closes = [151.0, 151.0, 151.0, 150.0, 149.5, 149.0, 149.0, 149.0, 149.0]
            rt.hub.candles = {(s, "1h"): _hourly(s, closes, t0) for s in ("BINANCE:ADAUSDT", "BINANCE:DOTUSDT")}
            later = NOW + 2 * H * 1000
            out = await evaluator.run_once(now=later)
            assert out["predictions"] == 2
            for pid in (old_ts, flagged):
                r = await _row(db, pid)
                assert r["resolved"] == 1 and r["outcome_price"] == 149.0 and r["correct"] == 0
            assert json.loads(json.dumps(await ev.prediction_stats(db)))["resolved"] == 3

    asyncio.run(scenario())
