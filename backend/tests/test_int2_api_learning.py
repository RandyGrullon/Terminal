"""int2-api · learning endpoints and the cost of the learning summary.

- GET /api/learning/skill (SkillReport; days=0 = every stored row, as the UI asks for 'todo') and the optional GET
  /api/learning/replay (offline replay of the scanner; 409 while another one runs on the same database).
- learning_summary reads each large NEXUS memory row once per version (updated_at, updates, trained_until) and
  decodes only its top-level tail when that decides the answer: same output as decoding every row on every call.
Runs on PostgreSQL too with NEXUS_TEST_PG_URL (runtime_factory / api_client).
"""
from __future__ import annotations

import asyncio
import json
import random

import pytest

from app.sim import evaluator as ev
from app.sim.common import json_loads
from app.util import now_ms, safe_float
from tests.test_imp_scanner_model_replay import H_MS, KEY, _stored_rows
from tests.test_imp_scanner_model_skill import _insert as insert_skill_row

T = 1_790_000_000_000


# ----------------------------------------------------------------------------- GET /api/learning/skill · /replay


def test_skill_endpoint(api_client):
    from app.runtime import rt

    c = api_client
    empty = c.get("/api/learning/skill")
    assert empty.status_code == 200
    rep = empty.json()
    assert rep["days"] == 7 and rep["min_windows"] == 30 and rep["notes"]
    assert [g["id"] for g in rep["groups"]] == ["all"] and rep["groups"][0]["verdict"] == "none"
    bad = c.get("/api/learning/skill", params={"source": "otra"})
    assert bad.status_code == 400 and bad.json()["detail"] == "Fuente no válida: usa scanner o nexus"
    assert c.get("/api/learning/skill", params={"days": "nan"}).status_code == 400
    assert c.get("/api/learning/skill", params={"days": "abc"}).status_code == 422

    async def seed():
        for w in range(40):  # 40 windows of the past (fixed clock of the skill tests: older than 7 days)
            for i in range(5):
                await insert_skill_row(rt.db, window=w, direction=1 if i < 3 else -1, prob=0.7 if i < 3 else 0.3,
                                       ret=1.0 if i < 4 else -1.0)
        for w in range(3):
            await insert_skill_row(rt.db, source="nexus", window=w, direction=1, prob=0.6, ret=0.8, hz=5 * 3_600_000)
        ev.invalidate_skill_cache(rt.db)

    c.portal.call(seed)
    recent = c.get("/api/learning/skill", params={"days": 7}).json()
    assert recent["groups"][0]["n"] == 0  # those rows are older than 7 days
    every = c.get("/api/learning/skill", params={"days": 0}).json()  # the UI's 'Todo lo guardado'
    assert every["days"] is None and every["since"] is None
    groups = {g["id"]: g for g in every["groups"]}
    assert set(groups) == {f"scanner:{KEY}", "nexus", "all"}
    assert groups[f"scanner:{KEY}"]["n"] == 200 and groups[f"scanner:{KEY}"]["windows"] == 40
    assert groups["nexus"]["n"] == 3 and groups["nexus"]["verdict"] == "insufficient"
    only = c.get("/api/learning/skill", params={"days": 0, "model_key": f" {KEY} "}).json()
    assert [g["id"] for g in only["groups"]] == [f"scanner:{KEY}"]
    scanner = c.get("/api/learning/skill", params={"days": -1, "source": "scanner"}).json()
    assert {g["id"] for g in scanner["groups"]} == {f"scanner:{KEY}"} and scanner["days"] is None
    json.dumps(every, allow_nan=False)


def test_replay_endpoint(api_client):
    from app.runtime import rt

    c = api_client
    empty = c.get("/api/learning/replay").json()
    assert empty["key"] is None and empty["days"] == 7 and empty["comparison"]["verdict"] == "none"
    assert empty["note"].startswith("Repetición secuencial: cada predicción se evalúa antes de aprender de ella")
    assert c.get("/api/learning/replay", params={"days": "inf"}).status_code == 400

    start = now_ms() - 20 * 3_600_000  # 40 scans 15 min apart, resolved 4 h later: all within the last day

    async def seed():
        for r in _stored_rows(40, 6, start=start):
            await rt.db.execute(
                "INSERT INTO predictions(source, symbol, tf, model_key, created_at, target_at, price, direction, "
                "prob_up, score, features, resolved, outcome_price, outcome_return_pct, correct, resolved_at) "
                "VALUES ('scanner', ?, '1h', ?, ?, ?, 100, ?, ?, 0, ?, 1, ?, ?, ?, ?)",
                (r["symbol"], KEY, r["created_at"], r["target_at"], r["direction"], r["prob_up"], r["features"],
                 100 + r["outcome_return_pct"], r["outcome_return_pct"], r["correct"], r["target_at"]),
            )

    c.portal.call(seed)
    rep = c.get("/api/learning/replay", params={"days": 1}).json()
    assert rep["key"] == KEY and rep["days"] == 1 and rep["rows"] == 240 and rep["scans"] == 40
    assert [d["id"] for d in rep["designs"]] == ["v2", "v1", "recorded"] and rep["truncated"] is False
    assert rep["comparison"]["verdict"] == "insufficient" and rep["horizon"] == "4 h"
    assert c.get("/api/learning/replay", params={"model_key": "crypto|4h|14400"}).json()["rows"] == 0
    token = ev._db_token(rt.db)  # another replay of this database still running
    ev._REPLAY_RUNNING.add(token)
    try:
        busy = c.get("/api/learning/replay")
        assert busy.status_code == 409 and "Ya hay una repetición del escáner en curso" in busy.json()["detail"]
    finally:
        ev._REPLAY_RUNNING.discard(token)
    assert c.get("/api/learning/replay", params={"days": 1}).status_code == 200


# ----------------------------------------------------------------------------- learning_summary: NEXUS memories


def _full(text) -> tuple:
    state = json_loads(text)
    return ev._state_metric(state, "hit_rate"), ev._state_metric(state, "brier")


def _service_state(hit, brier, *, learner_hit=0.9, anchors: int = 40) -> dict:
    snaps = [{"time": 3600 * i, "kind": "grid", "state": {"resume": {"fp": "x", "q": [0.25] * 50},
                                                          "learner": {"hit_rate": learner_hit, "brier": 0.3}}}
             for i in range(anchors)]
    learning = {"updates": 10, "hit_rate": hit, "brier": brier, "trained_until": 99, "memory_bars": 5}
    return {"version": 1, "snapshots": snaps, "learning": learning, "hit_rate": hit, "brier": brier, "updates": 10}


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def test_tail_reading_gives_exactly_the_full_decode():
    cases = [
        _compact(_service_state(0.55, 0.24)),  # what NexusService writes: decided by the tail
        _compact(_service_state(None, None)),  # no metric yet: the snapshot fallback needs the whole document
        _compact(_service_state(0.5, None)),
        json.dumps({"learning": {"hit_rate": 0.57, "brier": 0.241}}),  # default separators: no tail mark
        '{"hit_rate":0.1,"snapshots":[],"learning":{"hit_rate":0.2},"hit_rate":0.9,"brier":0.3}',  # duplicates
        '{"hit_rate":0.5,"snapshots":[{"state":{"learner":{"hit_rate":0.4}}}],"learning":{"hit_rate":0.6,"brier":0.2}}',
        '{"learning":{"hit_rate":0.3},"snapshots":[{"state":{"x":1,"learning":{"hit_rate":0.1}}}],"hit_rate":null}',
        # the last mark belongs to a snapshot whose own members follow it: never read as the top level
        '{"hit_rate":0.7,"brier":0.2,"snapshots":[{"x":1,"learning":{},"hit_rate":0.1,"brier":0.9}]}',
        '{"hit_rate":0.7,"brier":0.2,"s":{"x":1,"learning":{},"hit_rate":0.1,"brier":0.9}}',
        '{"note":"a,\\"learning\\":{","hit_rate":0.4,"brier":0.2}',  # the mark inside a string is escaped
        '{"snapshots":[],"learning":null,"hit_rate":"0.61","brier":0.2}',
        '{"x":[1,{"y":2,"learning":{"hit_rate":0.8}}],"brier":0.1}',
        '[{"a":1,"learning":{"hit_rate":0.8}}]',
        "{oops", "", None, "null", '{"learning":', '{"a":1,"learning":{"hit_rate":0.2}}}',
    ]
    for text in cases:
        assert ev._nexus_row_metrics(text) == _full(text), text
    assert ev._nexus_row_metrics(cases[0]) == (0.55, 0.24) and ev._nexus_tail(cases[0]) is not None
    assert ev._nexus_row_metrics(cases[1]) == (0.9, 0.3)  # from the latest snapshot, like before


def test_tail_reading_fuzz():
    keys = ["learning", "hit_rate", "brier", "snapshots", "stats", "perf", "metrics", "learner", "latest", "model",
            "state", "time", "kind"]
    leaves = [None, 0.5, 0.25, 7, "x", ',"learning":{', True, "0.3", [], {}]
    rng = random.Random(20261031)

    def value(depth: int):
        r = rng.random()
        if depth > 3 or r < 0.35:
            return rng.choice(leaves + [round(rng.random(), 4)])
        if r < 0.75:
            return {rng.choice(keys): value(depth + 1) for _ in range(rng.randint(0, 4))}
        return [value(depth + 1) for _ in range(rng.randint(0, 3))]

    def metric():
        return rng.choice([None, round(rng.random(), 4), round(rng.random(), 4), "0.3", {}, value(3)])

    decided = 0
    for i in range(3000):
        doc = {rng.choice(keys): value(1) for _ in range(rng.randint(0, 7))}
        if rng.random() < 0.6:  # the layout of the service: learning and the metrics at the end
            for k in ("learning", "hit_rate", "brier"):
                if rng.random() < 0.85:
                    doc.pop(k, None)
                    doc[k] = ({"hit_rate": metric(), "brier": metric()} if rng.random() < 0.7 else value(2)) \
                        if k == "learning" else metric()
        text = _compact(doc) if i % 4 else json.dumps(doc)
        assert ev._nexus_row_metrics(text) == _full(text), text
        tail = ev._nexus_tail(text)
        decided += tail is not None and ev._tail_metric(tail, "hit_rate")[0] and ev._tail_metric(tail, "brier")[0]
    assert decided > 300  # the fast path is really exercised


async def _reference_models(db) -> list[dict]:
    """The NEXUS part of learning_summary as it was: every row decoded on every call."""
    nrows = await db.fetchall("SELECT key, state, updates, trained_until FROM model_state WHERE kind='nexus' "
                              "ORDER BY updated_at DESC LIMIT 100")
    pstats = await db.fetchall("SELECT model_key, SUM(scored) AS n, SUM(correct) AS correct, SUM(brier_sum) AS brier "
                               "FROM prediction_daily WHERE source='nexus' GROUP BY model_key")
    pmap = {str(r["model_key"]).rpartition("|")[::2]: r for r in pstats}
    out = []
    for r in nrows:
        symbol, _, ntf = str(r["key"]).rpartition("|")
        if not symbol:
            symbol, ntf = str(r["key"]), ""
        hit, brier = _full(r["state"])
        p = pmap.get((symbol, ntf))
        if hit is None and p is not None:
            hit = safe_float(ev._ratio(p.get("correct"), p.get("n")))
        if brier is None and p is not None:
            brier = safe_float(ev._ratio(p.get("brier"), p.get("n")))
        tu = safe_float(r.get("trained_until"))
        out.append({"symbol": symbol, "tf": ntf, "updates": int(r.get("updates") or 0), "hit_rate": hit,
                    "brier": brier, "trained_until": int(tu) if tu is not None else None})
    return out


def test_summary_reads_each_memory_once_per_version(runtime_factory, monkeypatch):
    rows = [("BINANCE:BTCUSDT|1h", _compact(_service_state(0.55, 0.24)), 640, 1_759_990_000),
            ("BINANCE:ETHUSDT|4h", _compact(_service_state(None, None, learner_hit=0.52)), 3, None),
            ("NASDAQ:AAPL|1d", json.dumps({"learning": {"hit_rate": 0.57, "brier": 0.241}}), 12, 1_759_000_000),
            ("SIM:ROTO|1h", "{oops", 0, None)]

    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            for i, (key, state, updates, until) in enumerate(rows):
                await db.execute("INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                                 "VALUES ('nexus', ?, ?, ?, ?, ?)", (key, state, updates, until, T + i))
            first = await ev.learning_summary(db, rt.settings.all())
            assert first["nexus_models"] == await _reference_models(db)
            assert [m["symbol"] for m in first["nexus_models"]] == ["SIM:ROTO", "NASDAQ:AAPL", "BINANCE:ETHUSDT",
                                                                     "BINANCE:BTCUSDT"]
            assert first["nexus_models"][-1]["hit_rate"] == 0.55 and first["nexus_models"][2]["hit_rate"] == 0.52

            reads: list[int] = []
            real = ev._nexus_row_metrics
            monkeypatch.setattr(ev, "_nexus_row_metrics", lambda text: reads.append(1) or real(text))
            again = await ev.learning_summary(db, rt.settings.all())
            assert again == first and reads == []  # nothing changed: no row is read again

            # a new analysis rewrote one memory: only that one is read again
            await db.execute("UPDATE model_state SET state=?, updates=641, updated_at=? WHERE kind='nexus' AND key=?",
                             (_compact(_service_state(0.6, 0.2)), T + 10, "BINANCE:BTCUSDT|1h"))
            third = await ev.learning_summary(db, rt.settings.all())
            assert len(reads) == 1 and third["nexus_models"] == await _reference_models(db)
            assert third["nexus_models"][0] == {"symbol": "BINANCE:BTCUSDT", "tf": "1h", "updates": 641,
                                                "hit_rate": 0.6, "brier": 0.2, "trained_until": 1_759_990_000}
            # a reset / retention deleted another one: it leaves the summary (and the cache)
            await db.execute("DELETE FROM model_state WHERE kind='nexus' AND key=?", ("NASDAQ:AAPL|1d",))
            fourth = await ev.learning_summary(db, rt.settings.all())
            assert fourth["nexus_models"] == await _reference_models(db) and len(fourth["nexus_models"]) == 3
            assert "NASDAQ:AAPL|1d" not in ev._NEXUS_METRICS[db] and len(reads) == 1

    asyncio.run(scenario())


def test_summary_falls_back_to_the_daily_aggregates_like_before(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            await db.execute("INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                             "VALUES ('nexus', 'BINANCE:BTCUSDT|1h', ?, 5, NULL, ?)",
                             (_compact({"version": 1, "snapshots": [], "learning": {}, "hit_rate": None,
                                        "brier": None}), T))
            await insert_skill_row(db, source="nexus", window=1, direction=1, prob=0.6, ret=0.8, hz=H_MS)
            s = await ev.learning_summary(db, rt.settings.all())  # folds the resolved row into prediction_daily
            m = s["nexus_models"][0]
            assert m == (await _reference_models(db))[0]
            assert m["symbol"] == "BINANCE:BTCUSDT" and m["hit_rate"] == 1.0 and m["brier"] == pytest.approx(0.16)

    asyncio.run(scenario())


@pytest.mark.parametrize("n_rows", [1, 30])
def test_concurrent_summaries_read_each_changed_row_once(runtime_factory, monkeypatch, n_rows):
    async def scenario():
        async with runtime_factory() as rt:
            db = rt.db
            for i in range(n_rows):
                await db.execute("INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                                 "VALUES ('nexus', ?, ?, 1, NULL, ?)",
                                 (f"SIM:C{i}|1h", _compact(_service_state(0.5, 0.25, anchors=5)), T + i))
            reads: list[int] = []
            real = ev._nexus_row_metrics
            monkeypatch.setattr(ev, "_nexus_row_metrics", lambda text: reads.append(1) or real(text))
            outs = await asyncio.gather(*(ev.learning_summary(db, rt.settings.all()) for _ in range(4)))
            assert len(reads) == n_rows and all(o["nexus_models"] == outs[0]["nexus_models"] for o in outs)
            assert len(outs[0]["nexus_models"]) == n_rows

    asyncio.run(scenario())
