"""Deflated Sharpe Ratio and plateaus (app.validation.deflated, ported from the GenXAU research), and how the copilot's
strategy lab and the research report use them: how much of a good backtest the number of variants tried explains."""
from __future__ import annotations

import asyncio
import math
import random
from statistics import NormalDist
from types import SimpleNamespace

import pytest

from app.ai import lab
from app.ai import tools as tools_mod
from app.validation import deflated as D
from tests.test_imp_ai_lab import _candles, _result

ND = NormalDist()


def _noise(rng: random.Random, n: int, mean: float = 0.0) -> list[float]:
    return [rng.gauss(mean, 1.0) for _ in range(n)]


def test_trade_stats_are_the_sample_moments_without_excess_kurtosis():
    st = D.trade_stats([1.0, -1.0, 2.0, -1.0, 0.0, "x", None, float("nan")])
    assert st["n"] == 5 and st["sum"] == pytest.approx(1.0) and st["mean"] == pytest.approx(0.2)
    sd = math.sqrt(sum((x - 0.2) ** 2 for x in (1, -1, 2, -1, 0)) / 4)
    assert st["sr"] == pytest.approx(0.2 / sd)
    m2 = sum((x - 0.2) ** 2 for x in (1, -1, 2, -1, 0)) / 5
    assert st["skew"] == pytest.approx((sum((x - 0.2) ** 3 for x in (1, -1, 2, -1, 0)) / 5) / m2 ** 1.5)
    assert st["kurt"] == pytest.approx((sum((x - 0.2) ** 4 for x in (1, -1, 2, -1, 0)) / 5) / m2 ** 2)
    assert st["pf"] == pytest.approx(3.0 / 2.0)
    normal = D.trade_stats(_noise(random.Random(1), 20_000))
    assert normal["kurt"] == pytest.approx(3.0, abs=0.1) and normal["skew"] == pytest.approx(0.0, abs=0.05)
    assert D.trade_stats([])["sr"] is None and D.trade_stats([1.0, 1.0])["sr"] is None  # no spread, no Sharpe


def test_sr0_and_dsr_follow_bailey_and_lopez_de_prado_as_in_genxau():
    sigma, n = 0.05, 2418
    expected = sigma * ((1 - 0.5772156649) * ND.inv_cdf(1 - 1 / n) + 0.5772156649 * ND.inv_cdf(1 - 1 / (n * math.e)))
    assert D.expected_max_sr(sigma, n) == pytest.approx(expected)
    assert D.expected_max_sr(sigma, 10) < D.expected_max_sr(sigma, 1000)  # more ideas, higher bar
    assert D.expected_max_sr(sigma, 1) == D.expected_max_sr(sigma, 2)  # at least two
    sr, t, skew, kurt, sr0 = 0.28, 300, -0.2, 4.0, 0.31
    want = ND.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr * sr))
    assert D.deflated_sharpe(sr, t, skew, kurt, sr0) == pytest.approx(want)
    assert D.deflated_sharpe(sr, t, skew, kurt, sr0) < 0.5  # GenXAU's best (0.28) against luck (0.31): below
    assert D.deflated_sharpe(None, 300, 0, 3, 0.1) == 0.0 and D.deflated_sharpe(0.3, 2, 0, 3, 0.1) == 0.0
    assert D.deflated_sharpe(1.0, 300, 5.0, 3.0, 0.1) == 0.0  # a negative variance term: no DSR, as GenXAU


def test_the_best_of_many_noise_strategies_does_not_pass_but_a_real_edge_does():
    rng = random.Random(7)
    trials = [{"idea": f"idea{k // 5}", "n": 150, **D.trade_stats(_noise(rng, 150))} for k in range(400)]
    small = [{"idea": f"small{k}", "n": 40, **D.trade_stats(_noise(rng, 40))} for k in range(50)]
    fam = D.trial_family(trials + small)
    assert (fam["variants"], fam["eligible"], fam["ideas"]) == (450, 400, 80)  # N = ideas with 120+ trades
    assert fam["sigma_sr"] == pytest.approx(math.sqrt(1 / 150), rel=0.25)  # the spread of pure noise
    best = max(trials, key=lambda t: t["sr"])
    assert best["sr"] > 0.15  # it looks good on its own…
    psr0 = D.deflated_sharpe(best["sr"], best["n"], best["skew"], best["kurt"], 0.0)  # against zero, undeflated
    dsr = D.deflate(best, fam)["dsr"]
    assert psr0 > 0.95 and dsr < psr0 - 0.2  # …and the number of ideas tried takes most of it away
    assert (dsr < 0.5) == (best["sr"] < fam["sr0"])  # below luck's expected best: below the threshold
    typical = sorted(D.deflate(t, fam)["dsr"] for t in trials)[len(trials) // 2]
    assert typical < 0.05  # a typical noise strategy is nowhere near
    edge = {"idea": "edge", "n": 400, **D.trade_stats(_noise(rng, 400, mean=0.4))}
    out = D.deflate(edge, D.trial_family(trials + [edge]))
    assert out["passes"] and out["dsr"] > 0.9
    assert "Con 40 operaciones no se calcula" in D.deflate(small[0], fam)["reason"]
    alone = D.trial_family([edge])
    assert alone["sr0"] is None and "más de una variante" in D.deflate(edge, alone)["reason"]


def test_plateaus_judge_an_idea_by_its_median_variant():
    def v(idea, is_sum, oos_sum, score=0.1, pf=1.2, n=150):
        return {"idea": idea, "label": f"Idea {idea}", "n": n, "is": {"score": score, "pf": pf, "sum": is_sum},
                "oos": {"n": 30, "score": -score, "pf": 0.9, "sum": oos_sum}}
    variants = ([v("A", 5, -1, score=s) for s in (0.10, 0.12, 0.08, 0.2, 0.11)]
                + [v("B", 3, 2, score=0.5) for _ in range(3)]  # only 3 variants: no plateau
                + [v("C", -2, -1, score=-0.05, pf=0.8) for _ in range(4)]
                + [v("A", 1, 1, n=50)])  # too few trades: left out
    rows = D.plateaus(variants)
    assert [p["idea"] for p in rows] == ["A", "C"]
    a = rows[0]
    assert (a["variants"], a["is"]["score"], a["is"]["pf"], a["is"]["positive"]) == (5, 0.11, 1.2, 1.0)
    assert (a["oos"]["variants"], a["oos"]["positive"], a["oos"]["pf"]) == (5, 0.0, 0.9)
    assert rows[1]["is"]["positive"] == 0.0 and D.median([3, 1, 2, 4]) == 2.5 and D.median([]) is None


def _r_trades(candles: list[dict], rs: list[float], every: int = 2) -> list[dict]:
    out = []
    for k, r in enumerate(rs):
        i = 5 + k * every
        out.append({"side": "long", "entry_time": candles[i]["time"], "exit_time": candles[i + 1]["time"],
                    "exit_reason": "SIGNAL", "bars": 1, "pnl_pct": r * 0.5, "r_multiple": r})
    return out


def test_lab_idea_keys_trials_multiplicity_and_plateaus():
    a = "fast = input.int(9)\nema = ta.ema(close, fast)  # rápida\nstrategy('EMA 9/21')"
    b = "fast = input.int(12)\nema = ta.ema(close, fast)\nstrategy('EMA 12/26')"
    assert lab.idea_key(a) == lab.idea_key(b) and lab.variant_key(a) != lab.variant_key(b)
    assert lab.idea_key(a) != lab.idea_key(a.replace("ema", "sma"))
    candles = _candles(1200)
    rng = random.Random(3)
    trial = lab.trial_of(_result(candles, _r_trades(candles, _noise(rng, 500))), code=a, symbol="S", tf="1h")
    assert trial["n"] > 300 and trial["oos"]["n"] > 50 and trial["idea"] == lab.idea_key(a)
    assert lab.trial_of({"ok": False}, code=a, symbol="S", tf="1h") is None
    # the same variant run again replaces its previous run; another market is another family
    saved = lab.merge_trial([trial, {**trial, "symbol": "OTRO"}], {**trial, "n": 999})
    assert len(saved) == 2 and saved[-1]["n"] == 999 and len(lab.family_of(saved, "S", "1h")) == 1
    alone = lab.multiplicity(trial, [trial])
    assert alone["dsr"] is None and "más de una variante" in alone["veredicto"] and alone["variantes_probadas"] == 1
    family = [trial] + [lab.trial_from_trades(_r_trades(candles, _noise(rng, 500)), int(candles[840]["time"]),
                                              idea=f"i{k}", variant=f"v{k}", label=None, symbol="S", tf="1h")
                        for k in range(30)]
    out = lab.multiplicity(trial, family)
    assert out["ideas_con_120_operaciones"] == 31 and out["sharpe_del_azar_sr0"] > 0
    assert out["dsr"] is not None and out["veredicto"].startswith("DSR ")
    assert "Umbral orientativo: 0,5" in out["nota"] and "garantía" in out["nota"]
    variants = [lab.trial_from_trades(_r_trades(candles, _noise(rng, 400, mean=0.05)), int(candles[600]["time"]),
                                      idea="x", variant=f"x{k}", label="Cruce EMA", symbol="S", tf="1h") for k in range(5)]
    [row] = lab.plateau_rows(variants)
    assert row["idea"] == "Cruce EMA" and row["variantes"] == 5 and row["variantes_con_comprobacion"] == 5


def test_probar_estrategia_saves_its_trials_and_reports_the_deflation(runtime_factory):
    candles = _candles(1200)
    rng = random.Random(11)

    async def scenario():
        async with runtime_factory() as rt:
            calls = {"n": 0}

            async def run(code, symbol, tf, bars, inputs, *, name=None):
                calls["n"] += 1
                return _result(candles, _r_trades(candles, _noise(rng, 450)))

            rt.scripts = SimpleNamespace(run=run)
            cfg = {}
            first = await tools_mod.run_tool(rt, "probar_estrategia", {"codigo": "a = 1\nstrategy('A')",
                                                                     "simbolo": "SIM:NEXUS"}, cfg)
            assert first["ok"] and first["multiplicidad"]["variantes_probadas"] == 1
            assert first["multiplicidad"]["dsr"] is None  # one variant: nothing to deflate against yet
            for k in range(2, 6):
                res = await tools_mod.run_tool(rt, "probar_estrategia", {"codigo": f"a = {k}\nstrategy('A')",
                                                                       "simbolo": "SIM:NEXUS"}, cfg)
            assert res["multiplicidad"]["variantes_probadas"] == 5 and res["multiplicidad"]["ideas_distintas"] == 1
            assert res["multiplicidad"]["dsr"] is not None
            assert res["mesetas"]["ideas"][0]["variantes"] == 5 and "MEDIANA" in res["mesetas"]["nota"]
            saved = await rt.db.fetchval("SELECT value FROM meta WHERE key=?", (lab.TRIALS_KEY,))
            assert len(__import__("json").loads(saved)) == 5

    asyncio.run(scenario())
