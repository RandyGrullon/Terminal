"""Validation feature, second verifier: the walk-forward machinery is honest in BOTH directions. With the engine
replaced by fakes whose trades follow the engine's own mechanical risk rules:
- an oracle that peeks at the future (picks only winning entries) reads "Indicios de ventaja" (with the caveat), per
  series and pooled, so a real edge would be detected;
- entries without skill (seeded coin flips) never read as an edge and their percentile against the random baseline
  is spread out, not stuck high or low;
- entries chosen to lose read "pierde después de comisiones".
Plus: missing bars are only counted for 24/7 markets, the method says when a short series has fewer periods, and a
report saved while the first read of model_state is in flight is not replaced by the older stored one."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.config import DEFAULT_SETTINGS
from app.engine.features import compute_features
from app.engine.nexus import NexusParams
from app.sim.synthetic import generate_candleset
from app.validation import walkforward as W
from app.validation.aggregate import aggregate
from app.validation.markdown import report_markdown
from app.validation.mechanics import plan_for, simulate_exit
from app.validation.report import build_report, method_lines
from app.validation import verdict as V
from app.validation.service import ValidationService
from app.validation.verdict import EDGE_LABEL, NOT_A_GUARANTEE

END = 1_760_000_400
CFG = W.WFConfig(folds=4, warmup_bars=400, random_runs=300, bootstrap_runs=400)
PARAMS = NexusParams.from_settings(DEFAULT_SETTINGS)


class FakeRun:
    """What validate_series reads from a NexusResult."""

    def __init__(self, trades: list[dict]):
        self.ok, self.message, self.trades, self.stats = True, None, trades, {"rejected_signals": 0}


def trades_by(cs, pick) -> list[dict]:
    """One position at a time from bar 400: `pick(d)` -> +1 / −1 / 0 at the close of bar d; NEXUS' default levels and
    the mechanical exits (what the engine itself would book for that entry without its model exits)."""
    f = compute_features(cs)
    out, d, n = [], 400, len(cs)
    while d < n - 2:
        side = pick(d)
        if side:
            lv = plan_for(f, d, side, PARAMS)
            res = simulate_exit(f, d, lv, fee_pct=PARAMS.fee_pct, time_stop_bars=PARAMS.time_stop_bars,
                                trail_atr=PARAMS.trail_atr)
            if res is not None:
                r, x, e = res
                out.append({"id": len(out) + 1, "side": "long" if side > 0 else "short",
                            "entry_time": int(f.time[e]), "entry_price": float(f.open[e]),
                            "exit_time": int(f.time[x]), "exit_price": float(f.close[x]), "sl_initial": lv.sl,
                            "tp1": lv.tp1, "tp2": lv.tp2, "tp3": lv.tp3, "r_multiple": r,
                            "exit_reason": "TP3" if r > 0 else "SL", "bars": x - e + 1})
                d = x + 1
                continue
        d += 1
    return out


def validate_with(monkeypatch, cs, gated: list[dict], ungated: list[dict]) -> dict:
    monkeypatch.setattr(W, "run_nexus", lambda _cs, params, *a, **k: FakeRun(gated if params.gate_enabled else ungated))
    return W.validate_series(cs, None, CFG)


def future_r(cs) -> dict[int, np.ndarray]:
    """R of a random entry at every bar, known only AFTER the fact (the oracle's lookahead)."""
    return W.random_entry_table(compute_features(cs), 400, len(cs) - 1, PARAMS, fee_pct=PARAMS.fee_pct,
                                slippage_pct=0.0)


def coin_flips(seed: int):
    rng = np.random.default_rng(seed)
    return lambda d: int(rng.choice([1, -1])) if rng.random() < 0.05 else 0


@pytest.fixture(scope="module")
def market():
    return generate_candleset(n=2500, seed=11, tf="1h", end_time=END)


def test_an_oracle_with_lookahead_reads_as_an_edge_per_series_and_pooled(monkeypatch, market):
    fut = future_r(market)
    oracle = trades_by(market, lambda d: 1 if fut[1][d] > 0.5 else (-1 if fut[-1][d] > 0.5 else 0))
    noise = trades_by(market, coin_flips(3))
    assert len(oracle) >= 30
    res = validate_with(monkeypatch, market, oracle, noise)
    v = res["verdict"]
    assert v["code"] == "edge" and v["label"] == EDGE_LABEL and NOT_A_GUARANTEE in v["text"]
    assert res["overall"]["random"]["percentile"] >= 95 and res["overall"]["nexus"]["ci95"][0] > 0
    assert res["folds_positive"] == res["folds_with_trades"] >= 2
    assert res["gate_effect"]["code"] == "better"  # the oracle "filter" beats the coin flips it replaced

    other = generate_candleset(n=2500, seed=12, tf="1h", end_time=END, symbol="SIM:OTRO")
    fut2 = future_r(other)
    res2 = validate_with(monkeypatch, other, trades_by(other, lambda d: 1 if fut2[1][d] > 0.5 else 0), noise)
    groups = aggregate([res, res2], runs=400, seed=7)
    assert [g["key"] for g in groups] == ["1h"]
    assert groups[0]["verdict"]["code"] == "edge" and groups[0]["excess_ci95"][0] > 0
    rep = build_report([res, res2], [], request={}, config=CFG, started_at=0, finished_at=1)
    assert rep["summary"]["code"] == "edge" and NOT_A_GUARANTEE in rep["summary"]["text"]


def test_entries_without_skill_never_read_as_an_edge(monkeypatch, market):
    pcts = []
    for seed in range(100, 108):
        flips = trades_by(market, coin_flips(seed))
        res = validate_with(monkeypatch, market, flips, flips)
        assert res["verdict"]["code"] != "edge", seed
        assert res["verdict_ungated"]["code"] != "edge", seed
        pcts.append(res["overall"]["random"]["percentile"])
    # no skill = somewhere inside the random distribution: neither always beaten nor always beating it
    assert min(pcts) < 50 < max(pcts), pcts


def test_entries_picked_to_lose_read_as_a_loss_after_costs(monkeypatch, market):
    fut = future_r(market)
    losers = trades_by(market, lambda d: 1 if fut[1][d] < -0.5 else 0)
    res = validate_with(monkeypatch, market, losers, losers)
    assert res["verdict"]["code"] == "negative" and "pierde después de comisiones" in res["verdict"]["text"]
    assert res["overall"]["random"]["percentile"] <= 5


def test_a_single_period_never_carries_an_edge():
    """All the trades inside one walk-forward period: one stretch of the market, so never "Indicios de ventaja"."""
    one = V.series_verdict(80, 0.2, [0.05, 0.35], 99.0, 1, 1)
    assert one["code"] == "no_edge" and "todas las operaciones caen en un solo periodo" in one["text"]
    assert V.series_verdict(80, 0.2, [0.05, 0.35], 99.0, 2, 2)["code"] == "edge"
    assert "1 de 2 periodos" in V.series_verdict(80, 0.2, [0.05, 0.35], 99.0, 1, 2)["text"]
    assert "mayoría de los periodos (con operaciones en al menos dos)" in " ".join(method_lines(W.WFConfig()))


def test_missing_bars_are_counted_only_for_24_7_markets(monkeypatch):
    cs = generate_candleset(n=1300, seed=4, tf="1h", end_time=END)
    times = np.asarray(cs.time, dtype=np.int64).copy()
    times[700:] += 3 * 3600  # three missing hours in the middle of the series
    assert W.series_gaps("BINANCE:BTCUSDT", times, 3600) == 3
    assert W.series_gaps("SIM:NEXUS", times, 3600) == 3
    assert W.series_gaps("NASDAQ:AAPL", times, 3600) is None  # nights and weekends are not missing data
    assert W.series_gaps("FX:EURUSD", times, 3600) is None
    stock = generate_candleset(n=1300, seed=4, tf="1h", end_time=END, symbol="NASDAQ:AAPL")
    res = validate_with(monkeypatch, stock, [], [])
    assert res["gaps"] is None and res["verdict"]["code"] == "insufficient"
    rep = build_report([res], [], request={}, config=CFG, started_at=0, finished_at=1)
    assert rep["series"][0]["gaps"] is None
    row = next(line for line in report_markdown(rep).splitlines() if line.startswith("| NASDAQ:AAPL 1h | 1300 |"))
    assert row.split(" | ")[5] == "—"  # the "Huecos" column of the data table


def test_the_method_says_short_series_get_fewer_periods(monkeypatch):
    lines = " ".join(method_lines(W.WFConfig()))
    assert "5 periodos consecutivos" in lines and "menos si la serie es corta" in lines
    assert f"al menos {W.MIN_FOLD_BARS} velas de decisión" in lines
    res = validate_with(monkeypatch, generate_candleset(n=1000, seed=4, tf="1h", end_time=END), [], [])
    assert len(res["folds"]) == 4  # 1000 bars (the API minimum) - 500 of warm-up: 4 periods of ≥ 100 bars


def test_a_report_saved_during_the_first_read_is_not_replaced_by_the_stored_one(runtime_factory):
    """Same result on SQLite and PostgreSQL (checked 15 of 15 runs on each; the pre-fix latest() reads 'SIM:VIEJO' on
    both). A PostgreSQL failure seen once in a full-suite run came from a run that had imported the pre-fix service
    while it was being edited (its traceback line numbers did not match the file), not from the database."""
    async def scenario():
        async with runtime_factory() as rt:
            old = {"version": 1, "series": [{"symbol": "SIM:VIEJO", "tf": "1h"}], "generated_at": 1}
            await ValidationService(rt)._save(old)
            svc = ValidationService(rt)  # a restarted server: nothing loaded yet
            new = {"version": 1, "series": [{"symbol": "SIM:NUEVO", "tf": "1h"}], "generated_at": 2}
            real = rt.db.fetchone
            reads = []

            async def slow_fetchone(sql, params=()):
                row = await real(sql, params)  # the old row is read...
                if "model_state" in sql:
                    reads.append(sql)
                    await svc._save(new)  # ...while a job finishes and saves its report
                return row

            rt.db.fetchone = slow_fetchone
            try:
                got = await svc.latest()
            finally:
                rt.db.fetchone = real
            assert len(reads) == 1, "latest() must read model_state through db.fetchone (the race path ran)"
            assert got["series"][0]["symbol"] == "SIM:NUEVO"
            assert (await svc.latest())["series"][0]["symbol"] == "SIM:NUEVO"
            assert (await ValidationService(rt).latest())["series"][0]["symbol"] == "SIM:NUEVO"

    asyncio.run(scenario())
