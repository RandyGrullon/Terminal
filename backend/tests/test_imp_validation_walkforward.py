"""Validation feature: walk-forward core (folds, variants, random baseline, causality), honest verdicts, the
cross-asset aggregation and the report shape."""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.data.base import CandleSet
from app.sim.synthetic import generate_candleset
from app.validation import verdict as V
from app.validation.aggregate import aggregate, block_seconds
from app.validation.report import build_report, round_floats
from app.validation.walkforward import ValidationDataError, WFConfig, validate_series

END = 1_760_000_400
CFG = WFConfig(folds=4, warmup_bars=400, random_runs=300, bootstrap_runs=400)


@pytest.fixture(scope="module")
def series_2500():
    cs = generate_candleset(n=2500, seed=11, tf="1h", end_time=END)
    return cs, validate_series(cs, None, CFG)


def test_series_shape_and_folds(series_2500):
    cs, res = series_2500
    assert res["symbol"] == cs.symbol and res["tf"] == "1h" and res["bars"] == 2500
    assert res["warmup_bars"] == 400 and res["fee_pct"] == pytest.approx(0.1)
    folds = res["folds"]
    assert [f["index"] for f in folds] == [1, 2, 3, 4]
    assert sum(f["bars"] for f in folds) == 2500 - 1 - 400
    assert all(folds[i]["end_time"] < folds[i + 1]["start_time"] for i in range(3))
    ov = res["overall"]
    assert set(ov) == {"nexus", "ungated", "mechanical", "ungated_mechanical", "buy_hold", "random",
                       "random_ungated"}
    assert sum(f["nexus"]["trades"] for f in folds) == ov["nexus"]["trades"] > 0
    assert sum(f["ungated"]["trades"] for f in folds) == ov["ungated"]["trades"] > 0
    rnd = ov["random"]
    assert rnd["runs"] == 300 and rnd["trades"] == ov["nexus"]["trades"]
    assert 0.0 <= rnd["percentile"] <= 100.0 and 0.0 < rnd["p_value"] <= 1.0
    lo, mid, hi = rnd["expectancy_band"]
    assert lo <= mid <= hi
    ru = ov["random_ungated"]  # the baseline matched to NEXUS without the gate (its own counts)
    assert ru["trades"] == ov["ungated"]["trades"] and 0.0 <= ru["percentile"] <= 100.0
    assert 0.0 <= ru["percentile_mechanical"] <= 100.0  # its entries through the same mechanical exits
    assert ov["ungated_mechanical"]["trades"] <= ov["ungated"]["trades"]
    rows = res["_trades_ungated"]
    known = [t for t in rows if t["mech"] is not None]
    assert len(known) >= 0.9 * len(rows)
    assert all(t["mech_excess"] is not None for t in known)
    assert res["verdict_ungated"]["code"] in {"insufficient", "negative", "no_edge", "edge"}
    assert res["folds_with_trades_ungated"] >= res["folds_positive_ungated"] >= 0
    assert len(res["_trades_ungated"]) == ov["ungated"]["trades"]
    assert res["side_mix"]["long"] + res["side_mix"]["short"] == ov["nexus"]["trades"]
    assert res["verdict"]["code"] in {"insufficient", "negative", "no_edge", "edge"}
    assert res["verdict"]["text"].startswith(res["verdict"]["label"])
    assert ov["buy_hold"]["return_pct"] == pytest.approx(
        (cs.close[-1] / cs.close[400 - 1] - 1) * 100, rel=1e-9)
    assert len(res["_trades"]) == ov["nexus"]["trades"]
    assert all(t["excess"] is not None for t in res["_trades"])


def test_validation_is_deterministic(series_2500):
    cs, res = series_2500
    again = validate_series(cs, None, CFG)
    clean = lambda r: json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, sort_keys=True,  # noqa: E731
                                 default=str)
    assert clean(again) == clean(res)


def test_no_lookahead_future_bars_do_not_change_earlier_folds(series_2500):
    """Replacing every bar after the start of fold 4 by a different market leaves folds 1-2 untouched."""
    cs, res = series_2500
    other = generate_candleset(n=2500, seed=99, tf="1h", end_time=END)
    cut = int(np.searchsorted(cs.time, res["folds"][3]["start_time"]))
    scale = cs.close[cut - 1] / other.open[cut]
    cols = [np.concatenate([getattr(cs, k)[:cut], getattr(other, k)[cut:] * (scale if k != "volume" else 1.0)])
            for k in ("open", "high", "low", "close", "volume")]
    mixed = CandleSet(cs.symbol, cs.tf, cs.time, *cols)
    res2 = validate_series(mixed, None, CFG)
    for k in (0, 1):
        for variant in ("nexus", "ungated", "mechanical", "buy_hold"):
            assert res2["folds"][k][variant] == res["folds"][k][variant], (k, variant)
    assert res2["folds"][3]["buy_hold"] != res["folds"][3]["buy_hold"]


def test_too_short_history_is_refused():
    with pytest.raises(ValidationDataError):
        validate_series(generate_candleset(n=250, seed=1, tf="1h", end_time=END), None, CFG)
    with pytest.raises(ValidationDataError):  # long enough for NEXUS, not for 2 folds after the warm-up
        validate_series(generate_candleset(n=550, seed=1, tf="1h", end_time=END), None, CFG)


def test_series_verdict_rules():
    assert V.series_verdict(29, 0.5, [0.2, 0.8], 99.0, 4, 4)["code"] == "insufficient"
    neg = V.series_verdict(80, -0.2, [-0.35, -0.05], 10.0, 1, 5)
    assert neg["code"] == "negative" and neg["label"] == V.NO_EDGE_LABEL and "pierde" in neg["text"]
    assert "menos que las entradas al azar" in V.series_verdict(80, -0.2, [-0.35, -0.05], 97.0, 1, 5)["text"]
    wide = V.series_verdict(80, 0.1, [-0.05, 0.25], 99.0, 4, 5)
    assert wide["code"] == "no_edge" and "incluye el 0" in wide["text"]
    weak = V.series_verdict(80, 0.2, [0.05, 0.35], 90.0, 4, 5)
    assert weak["code"] == "no_edge" and "azar" in weak["text"]
    one_period = V.series_verdict(80, 0.2, [0.05, 0.35], 99.0, 2, 5)
    assert one_period["code"] == "no_edge" and "2 de 5 periodos" in one_period["text"]
    edge = V.series_verdict(80, 0.2, [0.05, 0.35], 99.0, 3, 5)
    assert edge["code"] == "edge" and edge["label"] == V.EDGE_LABEL
    assert "No es una garantía" in edge["text"] and "prueba" not in edge["text"].lower()


def test_gate_effect_rules():
    a = np.linspace(0.3, 0.7, 40)
    b = np.linspace(-0.2, 0.2, 60)
    assert V.gate_effect(a, b, runs=500, seed=1)["code"] == "better"
    assert V.gate_effect(b, a, runs=500, seed=1)["code"] == "worse"
    assert V.gate_effect(a, a + 0.01, runs=500, seed=1)["code"] == "unclear"
    small = V.gate_effect(a[:10], b, runs=500, seed=1)
    assert small["code"] == "insufficient" and small["diff_r"] is not None
    assert V.gate_effect([], b, runs=500, seed=1)["code"] == "insufficient"


def _fake_series(symbol: str, tf: str, rs: list[float], times: list[int], excess: list[float], daily: dict,
                 code: str = "no_edge") -> dict:
    mean = float(np.mean(rs)) if rs else None
    return {
        "symbol": symbol, "tf": tf, "test_start_time": min(times), "end_time": max(times) + 3600,
        "overall": {"nexus": {"expectancy_r": mean, "return_pct": 1.0}, "ungated": {"expectancy_r": 0.0},
                    "buy_hold": {"return_pct": 5.0}, "random": {"expectancy_r": -0.1}},
        "verdict": {"code": code},
        # mechanical exits: 0.05R worse than the model's exits on every entry
        "_trades": [{"time": t, "r": r, "excess": e, "mech": r - 0.05, "mech_excess": e - 0.05}
                    for t, r, e in zip(times, rs, excess)],
        "_daily": daily,
    }


def test_aggregate_counts_correlated_assets_once():
    rng = np.random.default_rng(8)
    day = 86400
    times = [int(i * 2 * day) for i in range(60)]
    rs = list(rng.normal(0.05, 1.0, 60))
    daily = {d: float(x) for d, x in enumerate(rng.normal(0, 0.02, 200))}
    # five assets with the same trades at the same times and the same daily returns (fully correlated)
    group = [_fake_series(f"X:A{i}", "1h", rs, times, [r + 0.1 for r in rs], daily) for i in range(5)]
    for s in group:  # without the gate: every trade loses 0.4R more
        s["_trades_ungated"] = [{"time": t["time"], "r": t["r"] - 0.4, "excess": t["excess"] - 0.4}
                                for t in s["_trades"]]
    out = aggregate(group, runs=1000, seed=3)
    assert [g["key"] for g in out] == ["1h"]
    g = out[0]
    assert g["trades"] == 300 and g["assets"] == 5 and g["series"] == 5
    assert g["exit_effect_r"] == pytest.approx(0.05) and g["exit_effect_ci95"] == pytest.approx([0.05, 0.05])
    assert g["mechanical_r"] == pytest.approx(g["expectancy_r"] - 0.05)
    assert g["mechanical_excess_r"] == pytest.approx(g["excess_r"] - 0.05)
    u = g["ungated"]
    assert u["trades"] == 300 and u["expectancy_r"] == pytest.approx(g["expectancy_r"] - 0.4)
    assert u["mechanical_r"] is None and u["exit_effect_r"] is None  # unknown, never invented
    assert g["ungated_expectancy_r"] == u["expectancy_r"] and u["verdict"]["code"] in {"negative", "no_edge"}
    assert g["avg_correlation"] == pytest.approx(1.0)
    assert g["effective_assets"] == pytest.approx(1.0)
    w = lambda ci: ci[1] - ci[0]  # noqa: E731
    assert w(g["ci95_iid"]) < 0.6 * w(g["ci95_blocks"])
    assert g["verdict"]["code"] in {"no_edge", "insufficient", "negative", "edge"}
    assert "correlacionados" in g["verdict"]["text"]
    mixed = group + [_fake_series("X:A0", "4h", rs, times, rs, daily)]
    keys = [x["key"] for x in aggregate(mixed, runs=500, seed=3)]
    assert keys == ["1h", "4h", "all"]


def test_group_verdict_needs_blocks_excess_and_majority():
    edge = V.group_verdict(200, 0.2, [0.05, 0.3], 0.15, [0.02, 0.3], 4, 5, 5, 1.4, 0.8)
    assert edge["code"] == "edge" and "1,4 independientes" in edge["text"]
    assert V.group_verdict(200, 0.2, [0.05, 0.3], 0.15, [-0.02, 0.3], 4, 5, 5, 1.4, 0.8)["code"] == "no_edge"
    assert V.group_verdict(200, 0.2, [0.05, 0.3], 0.15, [0.02, 0.3], 2, 5, 5, 1.4, 0.8)["code"] == "no_edge"
    assert V.group_verdict(200, 0.2, None, 0.15, [0.02, 0.3], 4, 5, 5, 1.4, 0.8)["code"] == "insufficient"
    assert V.group_verdict(200, -0.2, [-0.3, -0.1], None, None, 0, 5, 5, 1.4, 0.8)["code"] == "negative"
    assert V.group_verdict(10, 0.2, [0.05, 0.3], 0.1, [0.01, 0.2], 1, 1, 1, 1.0, None)["code"] == "insufficient"


def test_block_seconds():
    assert block_seconds(0) == 86400
    assert block_seconds(30 * 7 * 86400) == 7 * 86400
    assert block_seconds(10_000 * 86400) == 30 * 86400


def test_report_drops_internals_rounds_and_summarizes(series_2500):
    _, res = series_2500
    rep = build_report([res], [{"symbol": "X:Y", "tf": "1h", "message": "Sin velas"}],
                       request={"symbols": ["SIM:NEXUS"], "tfs": ["1h"], "bars": 2500}, config=CFG,
                       started_at=1_000, finished_at=3_500)
    text = json.dumps(rep)  # JSON-safe
    assert '"_trades"' not in text and '"_daily"' not in text and '"_random_means"' not in text
    assert "NaN" not in text and "Infinity" not in text
    assert rep["version"] == 1 and rep["duration_s"] == 2.5 and rep["errors"][0]["message"] == "Sin velas"
    assert rep["summary"]["code"] == res["verdict"]["code"]  # a single series: its own verdict
    assert rep["series"][0]["overall"]["nexus"]["expectancy_r"] == round(res["overall"]["nexus"]["expectancy_r"], 4)
    assert len(rep["method"]) >= 5 and "no garantiza" in rep["disclaimer"]
    empty = build_report([], [{"symbol": "X:Y", "tf": "1h", "message": "Sin velas"}], request={}, config=CFG,
                         started_at=0, finished_at=0)
    assert empty["summary"]["code"] == "insufficient" and "Sin velas" in empty["summary"]["text"]
    assert round_floats({"a": [1.234567, float("nan")]}) == {"a": [1.2346, None]}
