"""Validation feature, integration round: tiny samples never get a bootstrap interval (below MIN_CI_N trades), samples
below 30 are flagged «muy incierto» wherever an interval is shown (terminal line, Markdown tables, old stored reports
included), and the drawdown next to the random baseline is the same measure for NEXUS and for random entries
(closed-trade equity), with the per-bar account drawdown labelled as such."""
from __future__ import annotations

import copy

import pytest

from app.sim.common import fmt_pct
from app.sim.synthetic import generate_candleset
from app.validation import walkforward as W
from app.validation.markdown import CI_LEGEND, DD_LEGEND, report_markdown
from app.validation.report import build_report, method_lines
from app.validation.runner import series_line
from app.validation.stats import MIN_CI_N
from app.validation.verdict import UNCERTAIN_TEXT, ci_for, gate_effect

END = 1_760_000_400
CFG = W.WFConfig(folds=3, warmup_bars=400, random_runs=200, bootstrap_runs=300)


@pytest.fixture(scope="module")
def report():
    cs = generate_candleset(n=1500, seed=21, tf="1h", end_time=END)
    res = W.validate_series(cs, None, CFG)
    return build_report([res], [], request={}, config=CFG, started_at=0, finished_at=1)


def test_interval_texts_by_sample_size():
    ci = [-0.57, 0.85]
    assert ci_for(ci, 0) == "sin intervalo"
    assert ci_for(ci, 6) == f"sin intervalo: menos de {MIN_CI_N} operaciones"  # an older report's interval is hidden
    assert ci_for(None, 9) == f"sin intervalo: menos de {MIN_CI_N} operaciones"
    assert ci_for(ci, 12) == f"−0,57R a +0,85R · {UNCERTAIN_TEXT}"
    assert ci_for(ci, 29).endswith(UNCERTAIN_TEXT) and ci_for(ci, 30) == "−0,57R a +0,85R"
    assert ci_for(None, 40) == "sin intervalo"


def test_series_line_never_shows_an_interval_for_a_handful_of_trades():
    res = {"symbol": "X:Y", "tf": "1h", "verdict": {"label": "Sin evidencia de ventaja"},
           "overall": {"nexus": {"trades": 6, "expectancy_r": 0.15, "ci95": [-0.57, 0.85]},
                       "random": {"percentile": 77.0}}}
    line = series_line(res)
    assert "expectativa +0,15R (sin intervalo: menos de 10 operaciones)" in line and "IC 95 %" not in line
    res["overall"]["nexus"]["trades"] = 12
    assert "(IC 95 % −0,57R a +0,85R · muy incierto)" in series_line(res)
    res["overall"]["nexus"]["trades"] = 44
    assert "(IC 95 % −0,57R a +0,85R) · percentil frente al azar 77" in series_line(res)


def test_gate_effect_has_no_interval_with_fewer_than_ten_trades_on_a_side():
    few = gate_effect([0.4, -0.2, 0.1, -1.0, 0.3, 0.2], [0.1, -0.3] * 40, runs=300, seed=1)
    assert few["ci95"] is None and few["diff_r"] is not None and few["code"] == "insufficient"
    ten = gate_effect([0.4, -0.2, 0.1, -1.0, 0.3, 0.2, 0.5, -0.6, 0.0, 0.7], [0.1, -0.3] * 40, runs=300, seed=1)
    assert ten["ci95"] is not None and ten["code"] == "insufficient"  # an interval, still no conclusion below 30


def test_every_interval_of_a_real_report_follows_the_rule(report):
    """Series overall, every period and every variant: an interval only with at least MIN_CI_N trades."""
    seen = 0
    s = report["series"][0]
    sections = [s["overall"], *s["folds"]]
    for sec in sections:
        for name in ("nexus", "ungated", "mechanical", "ungated_mechanical"):
            v = sec[name]
            seen += 1
            if v["trades"] < MIN_CI_N:
                assert v["ci95"] is None, (name, v["trades"], v["ci95"])
    assert seen == 4 * len(sections)
    ge = s["gate_effect"]
    if min(ge["trades_gated"], ge["trades_ungated"]) < MIN_CI_N:
        assert ge["ci95"] is None


def test_markdown_hides_tiny_sample_intervals_and_labels_the_drawdowns(report):
    s = report["series"][0]
    o, rnd = s["overall"]["nexus"], s["overall"]["random"]
    md = report_markdown(report)
    assert CI_LEGEND in md and DD_LEGEND in md
    head = next(line for line in md.splitlines() if line.startswith("| Serie | Retorno de la cuenta |"))
    cols = [c.strip() for c in head.strip("|").split("|")]
    assert cols[1:6] == ["Retorno de la cuenta", "DD de la cuenta (vela a vela)", "Sharpe de la cuenta",
                         "DD por operaciones NEXUS", "DD por operaciones azar (mediana)"]
    prefix = f"| {s['symbol']} {s['tf']} | {fmt_pct(o['return_pct'], 1)}"
    row = next(line for line in md.splitlines() if line.startswith(prefix))
    cells = [c.strip() for c in row.strip("|").split("|")]
    if o["trades"]:  # the closed-trade drawdown sits next to the random one: the same measure
        assert cells[4] == fmt_pct(o["trade_max_dd_pct"], 1, signed=False)
        assert cells[5] == fmt_pct(rnd["trade_max_dd_p50"], 1, signed=False)
    assert cells[2] == fmt_pct(o["max_dd_pct"], 1, signed=False)  # the per-bar account, labelled as such
    old = copy.deepcopy(report)  # a report stored before this rule: 6 trades with a (far too narrow) interval
    old["series"][0]["overall"]["nexus"].update(trades=6, wins=2, ci95=[-1.14, -0.69])
    md_old = report_markdown(old)
    assert "−1,14R a −0,69R" not in md_old and "sin intervalo: menos de 10 operaciones" in md_old


def test_the_method_states_the_interval_and_drawdown_rules():
    text = " ".join(method_lines(W.WFConfig()))
    assert f"sin intervalo con menos de {MIN_CI_N} operaciones" in text and "muy incierto" in text
    assert "la misma medida para NEXUS y para el azar" in text
