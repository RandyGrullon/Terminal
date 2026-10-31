"""Improvement 5 (scanner-model): the measurement plumbing.

- the packed v4 feature vector: exact to 4 decimals, less than half the size of the JSON lists, every part guarded
  by the signature of its list, corrupt payloads ignored (never raise);
- 95 % intervals with the Newey–West term: neighbouring windows that move together widen the interval (a streak that
  lasts a regime is not proof of skill), alternating ones do not;
- the offline replay is streamed: chunks, a scan split between two pages and the row cap give the same result or
  say so;
- skill-report cache tokens are unique per database object (an id() can be reused).
"""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from app.sim import evaluator as ev
from app.sim import scanner as sc
from tests.test_imp_scanner_model_replay import H_MS, KEY, T0, _stored_rows

NOW = 1_760_000_400_000


def _rec(window: int, direction: int, p: float, ret: float) -> dict:
    return ev._replay_record(H_MS, NOW - 100 * H_MS + window * H_MS + H_MS // 2, direction, p, ret)


def test_packed_vector_is_exact_small_and_guarded():
    rng = np.random.default_rng(11)
    feats = {n: float(rng.uniform(-3, 3)) for n in sc.FEATURE_NAMES if n != "cci"}  # cci missing
    feats.update({"vol_d_log": 0.47, "atr_log": -1.25, "liq_log": 2.99999})  # bbw_log missing
    ctx = {n: float(rng.uniform(-1, 1)) for n in sc.CONTEXT_NAMES if not n.startswith("reg_")}
    ctx.update({"reg_trend_up": 1.0, "reg_trend_down": 0.0, "reg_range": 0.0, "reg_volatile": 0.0})
    raw = sc.encode_features(feats, ctx)
    data = json.loads(raw)
    assert data["v"] == 4 and set(data) == {"v", "f", "g", "s", "q"}
    expected = {k: round(v, 4) for k, v in {**feats, **ctx}.items()}
    out = sc.decode_features(raw)
    assert out == pytest.approx(expected, abs=1e-9) and "cci" not in out and "bbw_log" not in out
    # the same values as JSON lists (the v3 layout, still readable) take more than twice the space
    v3 = json.dumps({"v": 3, "f": sc.FEATURE_SIG, "x": [expected.get(n) for n in sc.FEATURE_NAMES],
                     "g": sc.EXTRA_SIG, "e": [expected.get(n) for n in sc.EXTRA_NAMES],
                     "s": sc.CONTEXT_SIG, "c": [expected.get(n) for n in sc.CONTEXT_NAMES]}, separators=(",", ":"))
    assert sc.decode_features(v3) == pytest.approx(expected)
    assert len(raw) < 220 and len(raw) * 2 < len(v3)
    # a part whose list changed is left out; another base list makes the whole row unusable
    assert set(sc.decode_features(json.dumps({**data, "s": "otra"}))) == set(expected) - set(ctx)
    base_only = {n for n in sc.FEATURE_NAMES if n in expected}
    assert set(sc.decode_features(json.dumps({**data, "g": "otra"}))) == base_only
    assert sc.decode_features(json.dumps({**data, "f": "otra"})) is None
    for bad in ("no es base64!", data["q"][:-3], "", 5, None):  # corrupt payloads: ignored
        assert sc.decode_features(json.dumps({**data, "q": bad})) is None
    # extras without a scan context still pack; base features alone keep the plain JSON list (v2)
    no_ctx = sc.encode_features({"rec_60": 0.5, "vol_d_log": 0.3})
    assert "s" not in json.loads(no_ctx) and sc.decode_features(no_ctx) == {"rec_60": 0.5, "vol_d_log": 0.3}
    assert json.loads(sc.encode_features({"rec_60": 0.5}))["v"] == 2


def test_newey_west_term_for_neighbouring_windows():
    assert [ev.nw_lag(g) for g in (2, 10, 30, 42, 180)] == [1, 2, 3, 3, 4]
    blocks = [(H_MS, w) for w in range(40)]
    counts = [10] * 40
    streak = [3.0] * 20 + [-1.0] * 20  # per window: +3 for 20 windows in a row, then −1
    plain, nw = ev.cluster_ci(streak, counts), ev.cluster_ci(streak, counts, blocks)
    assert plain[0] == nw[0] == pytest.approx(0.1)
    assert plain[1] > 0 > nw[1]  # as independent windows it would look proven; neighbours move together
    assert nw[2] - nw[1] > 1.8 * (plain[2] - plain[1])
    alternating = [3.0 if w % 2 else -1.0 for w in range(40)]  # no streaks: the interval does not widen
    a_plain, a_nw = ev.cluster_ci(alternating, counts), ev.cluster_ci(alternating, counts, blocks)
    assert a_nw[2] - a_nw[1] <= a_plain[2] - a_plain[1]
    order = list(range(40))[::-1]  # neighbours are found by window index, not by list position
    assert ev.cluster_ci([streak[i] for i in order], counts, [blocks[i] for i in order]) == pytest.approx(nw)
    assert ev.cluster_ci([5.0] * 3, [10] * 3, blocks[:3]) == pytest.approx((0.5, 0.5, 0.5))


def test_a_streak_that_lasts_a_regime_is_not_proof_of_skill():
    recs = []
    for w in range(40):  # 6 of 10 rows go up in every window: "always up" hits 6
        hits = 9 if w < 20 else 5  # the model shines for 20 windows in a row, then lags behind
        for i in range(10):
            up, right = i < 6, i < hits
            d = (1 if up else -1) if right else (-1 if up else 1)
            recs.append(_rec(w, d, 0.7 if d > 0 else 0.3, 1.0 if up else -1.0))
    g = ev.skill_group(recs, gid="s", label="s")
    assert g["windows"] == 40 and g["accuracy"] == pytest.approx(0.7)
    assert g["reference"]["accuracy"] == pytest.approx(0.6) and g["skill_pp"] == pytest.approx(10.0)
    assert g["skill_ci"][0] < 0 < g["skill_ci"][1] and g["verdict"] == "no_edge"
    assert g["verdict_text"].startswith("Sin ventaja demostrada: aciertos 280 de 400 (70,0 %)")


def test_replay_streams_in_chunks_with_the_same_result():
    rows = sorted(_stored_rows(40, 12), key=lambda r: r["created_at"])
    whole = ev.replay_compute(rows, KEY)
    replayer = ev.Replayer(KEY)
    for k in range(0, len(rows), 50):  # pages of 50 rows: most scans of 12 are split between two pages
        replayer.feed(rows[k:k + 50])
    assert replayer.finish() == whole
    assert whole["rows"] == 480 and whole["scans"] == 40 and whole["comparison"]["n"] == 480
    scans = [rows[k:k + 12] for k in range(0, len(rows), 12)]  # scans in any order (rows of a scan keep theirs)
    assert ev.replay_compute([r for scan in reversed(scans) for r in scan], KEY) == whole


async def _insert_rows(db, rows: list[dict]) -> None:
    for r in rows:
        await db.execute(
            "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, prob_up, "
            "score, features, resolved, outcome_price, outcome_return_pct, correct, resolved_at) "
            "VALUES ('scanner', ?, '1h', ?, ?, ?, 100, ?, ?, 0, ?, 1, ?, ?, ?, ?)",
            (r["symbol"], KEY, r["created_at"], r["target_at"], r["direction"], r["prob_up"], r["features"],
             100 + r["outcome_return_pct"], r["outcome_return_pct"], r["correct"], r["target_at"]),
        )


def test_replay_report_pages_through_the_rows_and_says_when_it_stops(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            now = T0 + 40 * 900_000 + H_MS + 60_000
            await _insert_rows(rt.db, _stored_rows(40, 6))
            full = await ev.replay_report(rt.db, days=3, now=now)
            assert full["rows"] == 240 and full["scans"] == 40 and full["truncated"] is False
            monkeypatch.setattr(ev, "REPLAY_CHUNK", 25)  # pages of 25 rows split scans of 6
            assert await ev.replay_report(rt.db, days=3, now=now) == full
            monkeypatch.setattr(ev, "REPLAY_MAX_ROWS", 100)
            capped = await ev.replay_report(rt.db, days=3, now=now)
            assert capped["truncated"] is True and capped["rows"] == 100 and capped["scans"] == 17

    asyncio.run(scenario())


def test_skill_replay_terminal_command_reports_the_designs_and_the_cap(api_client, monkeypatch):
    from app.runtime import rt
    from app.util import now_ms

    start = now_ms() - 40 * 900_000 - H_MS - 60_000
    api_client.portal.call(_insert_rows, rt.db, _stored_rows(40, 6, start=start))
    res = api_client.post("/api/terminal", json={"text": "skill replay --days 3"}).json()
    text = "\n".join(line["text"] for line in res["lines"])
    assert res["ok"] and f"Repetición del escáner {KEY} · últimos 3 días · 240 predicciones en 40 escaneos" in text
    assert "Modelo v2 (actual" in text and "Predicciones registradas en vivo" in text
    assert "no se puede afirmar que un diseño sea mejor" in text and "limitada" not in text
    monkeypatch.setattr(ev, "REPLAY_MAX_ROWS", 60)
    res = api_client.post("/api/terminal", json={"text": "skill replay --days 3"}).json()
    text = "\n".join(line["text"] for line in res["lines"])
    assert res["data"]["truncated"] is True and "Repetición limitada a las primeras 60 predicciones" in text
    token = ev._db_token(rt.db)  # one replay at a time per database (seconds of CPU each)
    ev._REPLAY_RUNNING.add(token)
    try:
        res = api_client.post("/api/terminal", json={"text": "skill replay"}).json()
    finally:
        ev._REPLAY_RUNNING.discard(token)
    assert res["ok"] is False and "Ya hay una repetición del escáner en curso" in str(res)


def test_the_replay_guard_is_released_after_a_failure(monkeypatch):
    class Db:
        pass

    db = Db()

    async def broken(*args, **kwargs):
        raise RuntimeError("fallo de prueba")

    monkeypatch.setattr(ev, "_replay_report", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(ev.replay_report(db))
    assert ev._db_token(db) not in ev._REPLAY_RUNNING
    ev._REPLAY_RUNNING.add(ev._db_token(db))
    try:
        with pytest.raises(ev.ReplayBusy):
            asyncio.run(ev.replay_report(db))
    finally:
        ev._REPLAY_RUNNING.discard(ev._db_token(db))


def test_cache_tokens_are_unique_per_database_object():
    class Db:
        pass

    a, b = Db(), Db()
    assert ev._db_token(a) == ev._db_token(a) != ev._db_token(b)
    old = ev._db_token(a)
    del a
    c = Db()  # may well get the id() of the deleted object: never its token
    assert ev._db_token(c) not in (old, ev._db_token(b))
