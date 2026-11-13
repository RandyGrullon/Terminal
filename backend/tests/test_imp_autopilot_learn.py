"""NEXUS automática learns from its losses (autopilot.learn_guard, on by default): its trader opens nothing in a
pre-trade context (regime, session, news, filter score, direction, market) where its trades of the last 90 days lose
with strong evidence — at least 30 trades and the whole 99 % interval of their mean R below zero (many groups are
looked at at once). Too few trades, or results that only look bad, pause nothing; the user can switch it off."""
from __future__ import annotations

import asyncio

from app.macro import fomc
from app.sim import journal as J
from app.sim import paper as pp
from tests.test_imp_autopilot import SYM, H, BarNexus, _candle, _engine, _settle, no_background  # noqa: F401
from tests.test_sim_fakes import make_current

DAY = 86400


def _trade(k: int, r: float, *, regime: str = "trend_up", side: str = "long") -> dict:
    entry = fomc.ny_time("2026-06-01", 10, 0) + k * DAY
    return {"r": r, "side": side, "symbol": SYM, "entry_time": entry, "exit_time": entry + 3600, "exit_reason": "SL",
            "gate_score": 78.0, "context": {"regime": regime}, "journal": {"cause": "en_contra", "mfe_r": 0.1},
            "instrument": None}


def test_only_strong_evidence_pauses_a_context():
    losers = [_trade(k, -0.95 - 0.01 * (k % 5)) for k in range(35)]
    found = {(p["dimension"], p["key"]) for p in J.losing_contexts(losers, seed=1)}
    assert {("regime", "trend_up"), ("side", "long"), ("market", "crypto"), ("gate", "70")} <= found
    hit = next(p for p in J.losing_contexts(losers, seed=1) if p["dimension"] == "market")
    assert (hit["label"], hit["bucket_label"], hit["n"], hit["wins"]) == ("Mercado", "Cripto", 35, 0)
    assert hit["ci"][1] < 0 and hit["mean_r"] < -0.9
    assert J.losing_contexts(losers[:25], seed=1) == []  # fewer than 30 trades: nothing is concluded
    mixed = [_trade(k, -1.0 if k % 2 else 1.1) for k in range(40)]
    assert J.losing_contexts(mixed, seed=1) == []  # wins and losses: the interval includes zero
    # a context it only lost in, while the rest of its trades are fine
    shorts = [_trade(k, -1.0, side="short", regime="range") for k in range(32)]
    longs = [_trade(k, 1.2, regime="trend_up") for k in range(32)]
    keys = {(p["dimension"], p["key"]) for p in J.losing_contexts(shorts + longs, seed=1)}
    assert ("side", "short") in keys and ("regime", "range") in keys and ("side", "long") not in keys


def test_the_keys_of_a_decision_match_the_journal_groups():
    keys = J.context_keys(regime="trend_up", ts=fomc.ny_time("2025-01-15", 8, 50), gate_score=81, bet="short",
                          symbol="NASDAQ:AAPL")
    assert keys == {"regime": "trend_up", "session": "nueva_york", "news": "cerca", "gate": "80", "side": "short",
                    "market": "stock"}
    assert J.context_keys(regime="unknown", ts=fomc.ny_time("2026-01-17", 12, 0), gate_score=None, bet="long",
                          symbol=SYM)["regime"] is None


def _closed(rt, k: int, r: float):
    now = pp.now_s()
    meta = pp.json_dumps({"context": {"regime": "trend_up"}, "journal": {"cause": "en_contra", "mfe_r": 0.1}})
    return rt.db.execute(
        "INSERT INTO paper_positions(account_id, symbol, tf, side, qty, qty_open, entry_price, entry_time, sl, tp_hits, "
        "status, exit_price, exit_time, pnl, pnl_pct, r_multiple, exit_reason, origin, gate_score, meta) VALUES "
        "(3, ?, '1h', 'long', 1, 0, 100, ?, 98, 0, 'closed', 98, ?, -2, -2, ?, 'SL', 'autopilot', 78, ?)",
        (SYM, now - k * DAY - 3600, now - k * DAY, r, meta))


def test_its_trader_skips_where_it_learned_to_lose_until_switched_off(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"paper.daily_loss_limit_pct": 0})
            for k in range(1, 36):  # 35 losses in the last 90 days, with NEXUS in an uptrend
                await _closed(rt, k, -0.95 - 0.01 * (k % 5))
            await _closed(rt, 120, -1.0)  # older than 90 days: out of what it learns from
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("BUY")))
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            assert engine.open_positions(3) == []
            [note] = [e["text"] for e in engine.autopilot_status()["log"] if e["symbol"] == SYM]
            assert note.startswith("Largo descartado: aprendió de sus pérdidas que en «Régimen de NEXUS: Tendencia "
                                   "alcista» pierde de forma consistente (0 de 35 ganadoras en 90 días")
            assert "IC 99 %" in note and "desactiva «Aprender de sus pérdidas»" in note
            st = (await engine.autopilot_snapshot())["autopilot"]
            assert st["learn_guard"] is True
            assert {(p["dimension"], p["key"]) for p in st["paused_contexts"]} >= {("regime", "trend_up"),
                                                                                  ("market", "crypto")}
            # switched off: it opens on the next closed bar
            await engine.set_autopilot(learn_guard=False)
            assert engine.autopilot_status()["paused_contexts"] == []
            t = engine._auto_bar[(3, SYM, "1h")]
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await _settle(engine)
            assert [p["symbol"] for p in engine.open_positions(3)] == [SYM]
            await engine.stop()

    asyncio.run(scenario())


def test_learn_switch_from_the_api_and_the_terminal(api_client):
    r = api_client.post("/api/paper/autopilot", json={"learn_guard": False})
    assert r.status_code == 200 and r.json()["autopilot"]["learn_guard"] is False
    res = api_client.post("/api/terminal", json={"text": "autopilot aprender on"}).json()
    assert res["ok"] and res["data"]["autopilot"]["learn_guard"] is True
    text = "\n".join(line["text"] for line in res["lines"])
    assert "todavía no hay ningún contexto donde pierda con evidencia fuerte" in text
    assert api_client.post("/api/terminal", json={"text": "autopilot aprender quizas"}).json()["ok"] is False
