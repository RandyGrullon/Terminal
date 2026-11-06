"""NEXUS automática frente a entrar al azar (app.sim.autopilot_compare): each closed trade's reference = the
validation's random entries (long and short, NEXUS's default levels, mechanical exits) in the 48 bars up to its
decision; paired statistics with a block bootstrap by day; honest verdicts (nothing below 30 trades or without an
interval); the engine's cached comparison over real candles; the API and the terminal."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app import config as nexus_config
from app.engine.features import compute_features
from app.engine.nexus import NexusParams
from app.sim import autopilot_compare as C
from app.sim import paper as pp
from app.sim.synthetic import generate_candleset
from app.util import floor_time, now_s
from app.validation.mechanics import random_entry_table
from tests.test_sim_fakes import FakeHub

SYM = "BINANCE:ETHUSDT"
H = 3600


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_decision_bar_is_the_last_bar_closed_at_the_entry():
    t = 1_760_000_000
    assert C.decision_bar(t + 125, "1h") == floor_time(t + 125, "1h") - H
    assert C.decision_bar(floor_time(t, "4h") + 10, "4h") == floor_time(t, "4h") - 4 * H


def test_references_are_the_random_entries_of_the_window():
    cs = generate_candleset(n=700, seed=5, tf="1h", symbol=SYM)
    f = compute_features(cs)
    params = NexusParams.from_settings(None, gate_enabled=True)
    t = cs.time
    refs = C.references(f, t, [int(t[400]), int(t[690]), int(t[10]), 123], params, fee_pct=0.1, slippage_pct=0.02)
    table = random_entry_table(f, 400 - 48, 401, params, fee_pct=0.1, slippage_pct=0.02)
    vals = np.concatenate([table[1][352:401], table[-1][352:401]])
    vals = vals[np.isfinite(vals)]
    assert vals.size >= C.MIN_REF_ENTRIES
    assert refs[0] == (pytest.approx(float(vals.mean())), int((vals > 0).sum()), int(vals.size))
    assert refs[2] is None and refs[3] is None  # inside the warm-up · not a bar of the data
    # at the end of the data the latest entries have no known outcome yet: fewer of them, never invented
    assert refs[1] is None or refs[1][2] < vals.size


def _rows(n: int, r: float, ref: float, *, days: int, noise: float = 0.0, seed: int = 1) -> list[dict]:
    rng = np.random.default_rng(seed)
    return [{"r": r + float(rng.normal(0.0, noise)) if noise else r, "ref": (ref, 40, 96), "day": 20_000 + i % days}
            for i in range(n)]


def test_verdicts_follow_the_validation_rules():
    empty = C.summarize([])
    assert (empty["verdict"], empty["verdict_label"], empty["compared"]) == ("none", "Sin datos", 0)
    assert empty["text"] == "Aún no hay operaciones cerradas de NEXUS automática que comparar."
    assert empty["nexus"] == {"n": 0, "wins": 0, "mean_r": None} and empty["ci95"] is None
    unknown = C.summarize([{"r": 0.4, "ref": None, "day": 1}] * 3)
    assert unknown["verdict"] == "none" and unknown["missing"] == 3 and "Ninguna de sus 3" in unknown["text"]
    few = C.summarize(_rows(5, 0.5, -0.05, days=5))
    assert few["verdict"] == "insufficient" and "hacen falta 30" in few["text"]
    assert "sin intervalo: menos de 10 operaciones" in few["text"] and few["ci95"] is None
    some = C.summarize(_rows(20, 0.5, -0.05, days=10, noise=0.5))
    assert some["verdict"] == "insufficient" and "muy incierto" in some["text"] and some["ci95"] is not None
    crowded = C.summarize(_rows(40, 0.5, -0.05, days=3))  # 40 trades but on 3 days: no interval by days
    assert crowded["verdict"] == "insufficient" and "3 días" in crowded["text"] and crowded["ci95"] is None
    flat = C.summarize(_rows(40, 0.0, 0.0, days=12, noise=1.0), seed=3)
    assert flat["verdict"] == "no_edge" and flat["verdict_label"] == "Sin evidencia de ventaja"
    assert flat["ci95"][0] < 0 < flat["ci95"][1] and "incluye el cero" in flat["text"]
    good = C.summarize(_rows(40, 1.0, -0.05, days=12, noise=0.3), seed=3)
    assert good["verdict"] == "better" and good["verdict_label"] == "Por encima de entrar al azar"
    assert "En este periodo" in good["text"] and "No es una garantía" in good["text"]
    assert "La validación publicada no encontró ventaja" in good["text"]
    bad = C.summarize(_rows(40, -1.0, -0.05, days=12, noise=0.3), seed=3)
    assert bad["verdict"] == "worse" and bad["ci95"][1] < 0
    assert (good["nexus"]["n"], good["nexus"]["wins"], good["random"]["entries"], good["random"]["wins"]) == (
        40, 40, 3840, 1600)
    assert good["diff_r"] == pytest.approx(good["nexus"]["mean_r"] - good["random"]["mean_r"])
    for s in (empty, few, flat, good, bad):
        assert "no encontró ventaja" in s["note"] and s["window_bars"] == C.REF_WINDOW_BARS


def test_its_closed_trades_against_random_entries_on_real_candles(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "paper.daily_loss_limit_pct": 0})
            cs = generate_candleset(n=900, seed=7, tf="1h", symbol=SYM, end_time=now_s())
            rt.hub = FakeHub(prices={SYM: float(cs.close[-1]), "BINANCE:BTCUSDT": 100.0}, candles={(SYM, "1h"): cs})
            engine = pp.PaperEngine(rt)
            await engine.start()
            for k in range(5):  # closed trades of NEXUS's own trader, decided at bars of the data
                j = 500 + 40 * k
                entry = int(cs.time[j]) + 60
                res = await engine.open_position(3, SYM, "long" if k % 2 else "short", "1h", origin="autopilot",
                                                 price=float(cs.close[j - 1]), check_gate=False, time_s=entry)
                await engine.close_position(res["position"]["id"], account_id=3, price=float(cs.close[j + 4]),
                                            time_s=entry + 5 * H)
            other = await engine.open_position(3, "BINANCE:BTCUSDT", "long", "1h", origin="autopilot", price=100.0,
                                               check_gate=False, time_s=int(cs.time[600]) + 60)
            await engine.close_position(other["position"]["id"], account_id=3, price=101.0,
                                        time_s=int(cs.time[600]) + 3 * H)
            c = await engine.autopilot_comparison()
            assert (c["trades"], c["compared"], c["missing"]) == (6, 5, 1)  # no candles for BTC: no reference
            assert c["nexus"]["n"] == 5 and c["random"]["entries"] >= 5 * C.MIN_REF_ENTRIES
            assert np.isfinite(c["nexus"]["mean_r"]) and np.isfinite(c["random"]["mean_r"])
            assert c["verdict"] == "insufficient" and "hacen falta 30" in c["text"] and c["generated_at"] > 0
            assert await engine.autopilot_comparison() is c  # cached until a trade closes
            res = await engine.open_position(3, SYM, "long", "1h", origin="autopilot", price=float(cs.close[699]),
                                             check_gate=False, time_s=int(cs.time[700]) + 60)
            await engine.close_position(res["position"]["id"], account_id=3, price=float(cs.close[703]),
                                        time_s=int(cs.time[700]) + 4 * H)
            again = await engine.autopilot_comparison()
            assert again is not c and (again["trades"], again["compared"]) == (7, 6)
            assert (await engine.autopilot_comparison(fresh=True))["compared"] == 6
            # the user's own trades are never part of it
            mine = await engine.open_position(1, SYM, "long", "1h", price=float(cs.close[710]), check_gate=False,
                                              time_s=int(cs.time[711]) + 60)
            await engine.close_position(mine["position"]["id"], account_id=1, price=float(cs.close[712]),
                                        time_s=int(cs.time[711]) + 2 * H)
            assert (await engine.autopilot_comparison(fresh=True))["trades"] == 7
            await engine.stop()

    asyncio.run(scenario())


def test_api_and_terminal(api_client):
    c = api_client
    r = c.get("/api/paper/autopilot/compare")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["verdict"], body["trades"], body["compared"]) == ("none", 0, 0)
    assert body["text"] == "Aún no hay operaciones cerradas de NEXUS automática que comparar."
    assert c.get("/api/paper/autopilot/compare", params={"fresh": "true"}).status_code == 200
    res = c.post("/api/terminal", json={"text": "autopilot azar"}).json()
    assert res["ok"], res["lines"]
    text = "\n".join(line["text"] for line in res["lines"])
    assert "NEXUS automática frente a entrar al azar · Sin datos" in text and "Referencia de cada operación" in text
    assert c.post("/api/terminal", json={"text": "piloto comparar"}).json()["ok"]
