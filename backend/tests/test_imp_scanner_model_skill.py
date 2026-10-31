"""Improvement 5 (scanner-model): honest measurement of skill versus the baseline.

Three outcomes (up / down / neutral inside the flat band): the reference is always predicting the most frequent
outcome of the same rows, chance is the hit rate of the same mix of calls if they were unrelated to the outcomes, the
Brier score is compared with climatology, and the 95 % intervals are clustered by independent horizon windows (no
conclusion below 30 windows).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config as nexus_config
from app.sim import evaluator as ev
from app.sim.scanner import ScannerService

H_MS = 14_400_000
NOW = 1_760_000_400_000
KEY = "crypto|1h|14400"


def _rec(window: int, direction: int, p: float, ret: float, hz: int = H_MS) -> dict:
    return ev._replay_record(hz, NOW - 100 * hz + window * hz + hz // 2, direction, p, ret)


def test_reference_is_the_most_frequent_outcome_and_chance_uses_the_same_mix():
    # 100 rows: 70 up (20 of them inside the ±0,5 % band), 30 down; the model always says "up"
    recs = [_rec(i % 40, 1, 0.7, 2.0 if i < 50 else 0.3 if i < 70 else -1.0) for i in range(100)]
    g = ev.skill_group(recs, gid="x", label="x")
    assert g["n"] == 100 and g["hits"] == 70 and g["outcomes"] == {"up": 70, "down": 30, "flat": 20}
    assert g["reference"] == {"kind": "up", "label": "siempre «alcista»", "hits": 70, "accuracy": 0.7}
    assert g["chance"] == pytest.approx(0.7) and g["skill_pp"] == pytest.approx(0.0)
    assert g["verdict"] == "no_edge" and "Sin ventaja demostrada" in g["verdict_text"]
    assert "70 de 100 (70,0 %)" in g["verdict_text"]
    # mostly quiet moves: the reference is "always neutral", and a neutral call is judged against the band
    # (+0,2 % is both "up" and "inside the band": the outcomes overlap)
    quiet = [_rec(i % 40, 0, 0.5, 0.2 if i < 40 else -0.2 if i < 80 else 2.0) for i in range(100)]
    q = ev.skill_group(quiet, gid="q", label="q")
    assert q["outcomes"] == {"up": 60, "down": 40, "flat": 80}
    assert q["reference"]["kind"] == "flat" and q["reference"]["accuracy"] == pytest.approx(0.8)
    assert q["accuracy"] == pytest.approx(0.8)
    by = {d["direction"]: d for d in q["by_direction"]}
    assert by[0]["n"] == 100 and by[0]["base_rate"] == pytest.approx(0.8) and by[1]["n"] == 0


def test_correlated_rows_of_one_window_are_not_independent_evidence():
    # 1 000 right calls inside ONE 4 h window: impressive hit rate, no evidence
    one = [_rec(0, 1, 0.8, 1.0) for _ in range(1000)]
    g = ev.skill_group(one + [_rec(0, 1, 0.8, -1.0) for _ in range(500)], gid="one", label="one")
    assert g["windows"] == 1 and g["skill_ci"] is None and g["verdict"] == "insufficient"
    assert "en solo 1 ventana independiente de 4 h" in g["verdict_text"]
    # the same edge spread over 40 independent windows: now it is measurable
    spread = []
    for w in range(40):
        spread += [_rec(w, 1, 0.8, 1.0) for _ in range(8)] + [_rec(w, -1, 0.2, -1.0) for _ in range(6)]
        spread += [_rec(w, 1, 0.8, -1.0) for _ in range(3)] + [_rec(w, -1, 0.2, 1.0) for _ in range(3)]
    s = ev.skill_group(spread, gid="s", label="s")
    assert s["windows"] == 40 and s["accuracy"] == pytest.approx(0.7) and s["reference"]["accuracy"] == pytest.approx(0.55)
    assert s["verdict"] == "better" and s["skill_ci"][0] > 0
    assert s["verdict_text"].startswith("Supera a la referencia") and "no una garantía" in s["verdict_text"]
    assert s["bss"] > 0 and s["chance"] == pytest.approx(0.55 * 0.55 + 0.45 * 0.45)


def test_cluster_interval_and_t_quantiles():
    assert ev.cluster_ci([1.0], [10]) is None
    mean, lo, hi = ev.cluster_ci([5.0, 5.0, 5.0], [10, 10, 10])
    assert mean == pytest.approx(0.5) and lo == pytest.approx(0.5) and hi == pytest.approx(0.5)
    wide = ev.cluster_ci([0.0, 10.0], [10, 10])
    assert wide[1] < 0 < wide[2]  # two clusters that disagree: an interval that includes zero
    assert ev.t975(1) == pytest.approx(12.706) and ev.t975(30) == pytest.approx(2.042) and ev.t975(500) == 1.96
    assert ev.best_constant(5, 5, 9) == ("flat", 9) and ev.best_constant(5, 5, 1) == ("up", 5)


async def _insert(db, *, source="scanner", key=KEY, symbol="BINANCE:BTCUSDT", window: int, direction: int, prob: float,
                  ret: float, hz: int = H_MS, correct: int | None = None):
    target = NOW - 100 * hz + window * hz + hz // 2
    created = target - hz
    if correct is None:
        correct = int(ev.judge_prediction(direction, ret, hz / 1000))
    await db.execute(
        "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, prob_up, score, "
        "resolved, outcome_price, outcome_return_pct, correct, resolved_at) VALUES (?, ?, '1h', ?, ?, ?, 100, ?, ?, ?, "
        "1, ?, ?, ?, ?)",
        (source, symbol, key if source == "scanner" else f"{symbol}|1h", created, target, direction, prob, 2 * prob - 1,
         100 + ret, ret, correct, target),
    )


def test_skill_report_from_the_database(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            for w in range(40):  # 40 independent 4 h windows: 7 right, 3 wrong; 6 of 10 went up
                for i in range(10):
                    up = i < 6
                    right = i < 7
                    d = (1 if up else -1) if right else (-1 if up else 1)
                    await _insert(db, window=w, direction=d, prob=0.7 if d > 0 else 0.3, ret=1.0 if up else -1.0)
            for w in range(3):  # NEXUS: few windows
                await _insert(db, source="nexus", window=w, direction=1, prob=0.6, ret=0.8, hz=5 * 3_600_000)
            rep = await ev.skill_report(db, days=None, now=NOW)
            groups = {g["id"]: g for g in rep["groups"]}
            assert set(groups) == {f"scanner:{KEY}", "nexus", "all"}
            sc_g = groups[f"scanner:{KEY}"]
            assert sc_g["label"] == "Escáner crypto 1h · horizonte 4 h" and sc_g["n"] == 400 and sc_g["windows"] == 40
            assert sc_g["accuracy"] == pytest.approx(0.7) and sc_g["reference"]["accuracy"] == pytest.approx(0.6)
            assert sc_g["verdict"] == "better" and sc_g["skill_ci"][0] > 0
            # Brier against climatology (u = 0,6 → 0,24), computed by hand
            brier = (280 * 0.09 + 120 * 0.49) / 400
            assert sc_g["brier"] == pytest.approx(brier) and sc_g["brier_ref"] == pytest.approx(0.24)
            assert sc_g["bss"] == pytest.approx(1 - brier / 0.24)
            assert sc_g["brier_text"].startswith("Brier 0,210 frente a 0,240 de la climatología (habilidad +13 %")
            assert groups["nexus"]["verdict"] == "insufficient" and groups["nexus"]["horizon"] == "5 h"
            assert len(sc_g["daily"]) >= 1 and set(sc_g["daily"][0]) == {"time", "n", "hit_rate", "reference",
                                                                       "reference_kind", "brier", "brier_ref"}
            json.dumps(rep, allow_nan=False)
            # the window (by target time): the last 12 days hold the 12 newest scanner windows, no NEXUS row
            recent = await ev.skill_report(db, days=12, now=NOW)
            assert [g["id"] for g in recent["groups"]] == [f"scanner:{KEY}"] and recent["groups"][0]["n"] == 120
            only_nexus = await ev.skill_report(db, source="nexus", days=None, now=NOW)
            assert [g["id"] for g in only_nexus["groups"]] == ["nexus", "all"] or \
                [g["id"] for g in only_nexus["groups"]] == ["nexus"]

    asyncio.run(scenario())


def test_skill_report_is_cached_until_new_outcomes(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            empty = await ev.skill_report(db, days=7)
            assert empty["groups"][0]["verdict"] == "none" and empty["groups"][0]["n"] == 0
            await _insert(db, window=99, direction=1, prob=0.7, ret=1.0)
            assert (await ev.skill_report(db, days=None)) is (await ev.skill_report(db, days=None))  # cached
            before = await ev.skill_report(db, days=None)
            await _insert(db, window=98, direction=1, prob=0.7, ret=1.0)
            ev.invalidate_skill_cache(db)
            after = await ev.skill_report(db, days=None)
            assert after["groups"][0]["n"] == before["groups"][0]["n"] + 1

    asyncio.run(scenario())


def test_learn_events_name_the_reference_and_climatology(runtime_factory, monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)

    class Hub:
        async def quotes(self, symbols):
            return {s: {"symbol": s, "price": 101.0 if s.endswith("0USDT") else 99.0} for s in symbols}

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            created, due = NOW - H_MS, NOW - 60_000
            for i in range(6):
                await db.execute(
                    "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, "
                    "prob_up, score, features) VALUES ('scanner', ?, '1h', ?, ?, ?, 100, -1, 0.3, -0.4, ?)",
                    (f"BINANCE:A{i}0USDT" if i < 4 else f"BINANCE:B{i}USDT", KEY, created, due,
                     json.dumps({"v": 1, "x": {"rec_60": 0.1 * i}})),
                )
            rt.hub = Hub()
            rt.scanner = ScannerService(rt)
            await rt.scanner.start()
            out = await ev.PredictionEvaluator(rt).run_once(now=NOW)
            assert out["predictions"] == 6 and out["scanner_updates"] == 6
            msg = next(e for e in rt.bus.recent(level="learn") if e["message"].startswith("Modelo escáner"))
            assert msg["message"] == ("Modelo escáner crypto|1h: 6 predicciones resueltas · acierto 33,3 % (2 de 6) · "
                                      "referencia 66,7 % (siempre «alcista») · Brier 0,357 · climatología 0,222 · "
                                      "1 ventana independiente: sin conclusión")
            assert msg["data"]["reference"] == pytest.approx(4 / 6) and msg["data"]["windows"] == 1
            await rt.scanner.stop()

    asyncio.run(scenario())


def _term(client, text: str) -> dict:
    r = client.post("/api/terminal", json={"text": text})
    assert r.status_code == 200
    return r.json()


def _text(res: dict) -> str:
    return "\n".join(line["text"] for line in res["lines"])


def test_skill_terminal_command(api_client):
    from app.runtime import rt

    res = _term(api_client, "skill")
    assert res["ok"] and "Aún no hay predicciones verificadas" in _text(res)
    assert _term(api_client, "skill bogus")["ok"] is False

    async def seed():
        for w in range(5):
            for i in range(4):
                await _insert(rt.db, window=w, direction=1, prob=0.7, ret=1.0 if i < 3 else -1.0)
        ev.invalidate_skill_cache(rt.db)

    api_client.portal.call(seed)  # the app's database lives in the TestClient's event loop
    res = _term(api_client, "habilidad escáner --days 0")
    text = _text(res)
    assert res["ok"] and "Habilidad frente a la referencia · todo lo guardado" in text
    assert "Muestra insuficiente: aciertos 15 de 20 (75,0 %) frente a 15 de 20 (75,0 %, siempre «alcista»)" in text
    assert "Predicción alcista: 15 de 20 (75,0 %)" in text
    assert res["data"]["groups"][0]["windows"] == 5
    res = _term(api_client, "skill replay")
    assert res["ok"] and "repetición necesita escaneos resueltos" in _text(res)
    res = _term(api_client, "learning")
    assert res["ok"] and "el 50 % no es el azar" in _text(res)
    # the model starts from zero (v2): the historical hit rate is not presented as its own
    assert "El modelo actual aún no ha aprendido de ningún resultado" in _text(res)
    res = _term(api_client, "predictions 5")
    assert res["ok"] and "Frente a la referencia (filas guardadas): Muestra insuficiente" in _text(res)
