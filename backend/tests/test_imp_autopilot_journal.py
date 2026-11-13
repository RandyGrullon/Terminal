"""Journal of NEXUS automática (app.sim.journal): the session and the distance to the principal US news of an entry,
how far each trade went against and in its favour and why it ended so, and its closed trades grouped as «x de n» with
the mean R and its 95 % interval — never a verdict below 30 trades or while the interval includes zero. The engine
keeps what NEXUS saw at the entry (meta.context) and the journal of the close (meta.journal); GET
/api/paper/autopilot/journal and the terminal (`autopilot diario`) show it."""
from __future__ import annotations

import asyncio

import pytest

from app.macro import fomc
from app.sim import journal as J
from app.sim import paper as pp
from tests.test_imp_autopilot import SYM, BarNexus, _engine, _settle, no_background  # noqa: F401 (fixture)
from tests.test_sim_fakes import make_current


def test_session_of_an_entry_in_new_york_time_with_its_summer_time():
    assert J.session_of(fomc.ny_time("2026-07-15", 10, 0)) == "nueva_york"  # EDT
    assert J.session_of(fomc.ny_time("2026-01-14", 10, 0)) == "nueva_york"  # EST: the same local hour
    assert J.session_of(fomc.ny_time("2026-01-14", 2, 0)) == "asia"
    assert J.session_of(fomc.ny_time("2026-01-14", 4, 30)) == "londres"
    assert J.session_of(fomc.ny_time("2026-01-14", 17, 30)) == "pausa"
    assert J.session_of(fomc.ny_time("2026-01-14", 19, 0)) == "asia"
    assert J.session_of(fomc.ny_time("2026-01-16", 17, 30)) == "fin_de_semana"  # Friday after the close
    assert J.session_of(fomc.ny_time("2026-01-17", 12, 0)) == "fin_de_semana"  # Saturday
    assert J.session_of(fomc.ny_time("2026-01-18", 19, 0)) == "asia"  # Sunday evening: Asia opens


def test_minutes_to_the_nearest_principal_us_release():
    assert J.news_minutes(fomc.ny_time("2025-01-15", 8, 50)) == pytest.approx(20.0)  # CPI at 08:30
    assert J.news_minutes(fomc.ny_time("2025-01-15", 8, 0)) == pytest.approx(-30.0)  # before it
    assert J.news_minutes(fomc.ny_time("2025-03-19", 14, 10)) == pytest.approx(10.0)  # FOMC decision at 14:00
    assert abs(J.news_minutes(fomc.ny_time("2025-01-22", 12, 0))) > 60 * 24  # a quiet week
    assert J.news_minutes(fomc.ny_time("2023-06-01", 10, 0)) is None  # before the calendar's coverage


def test_excursions_and_causes():
    # long at 100, 2 points of risk: it went 1.5R in favour, then back to its stop
    mae, mfe = J.excursions("long", 100, 2, 103, 99, exit_price=98)
    assert (mae, mfe) == (1.0, 1.5) and J.cause_of(-1.0, "SL", mfe) == "devolvio"
    mae, mfe = J.excursions("long", 100, 2, 100.2, 100, exit_price=98)
    assert (mae, mfe) == (1.0, 0.1) and J.cause_of(-1.02, "SL", mfe) == "en_contra"
    assert J.cause_of(-1.0, "SL", 0.5) == "stop_tras_favor"
    assert J.excursions("short", 100, 2, 101, 96) == (0.5, 2.0)
    assert J.excursions("long", 100, 0, 101, 99) == (None, None)
    for reason, r, cause in (("TP2", 1.9, "objetivo"), ("TRAIL", 2.4, "stop_dinamico"), ("SL", 0.3, "parcial_equilibrio"),
                             ("EARLY", 0.4, "salida_nexus_ganancia"), ("REVERSE", -0.4, "salida_nexus_perdida"),
                             ("EXPIRY", -0.8, "vencimiento"), ("MANUAL", 0.2, "manual")):
        assert J.cause_of(r, reason, 0.6) == cause, reason
    entry = J.close_entry(side="long", entry=100, risk_unit=2, high_water=100.4, low_water=99.5, exit_price=98, r=-1.03,
                          reason="SL", entry_time=1000, exit_time=4600)
    assert entry == {"mae_r": 1.0, "mfe_r": 0.2, "cause": "en_contra", "held_s": 3600}


def _trade(day: int, r: float, *, hour: int = 10, reason: str = "SL", mfe: float = 0.1, regime: str = "trend_up",
           gate: float = 75.0) -> dict:
    entry = fomc.ny_time("2026-03-02", hour, 0) + day * 86400
    return {"r": r, "side": "long", "symbol": "BINANCE:ETHUSDT", "entry_time": entry, "exit_time": entry + 3600,
            "exit_reason": reason, "gate_score": gate, "context": {"regime": regime},
            "journal": {"mae_r": 1.0 if r < 0 else 0.3, "mfe_r": mfe, "cause": J.cause_of(r, reason, mfe)},
            "instrument": None}


def test_the_journal_groups_its_trades_and_concludes_only_with_evidence():
    assert J.analyze([])["insights"] == ["Aún no hay operaciones cerradas de NEXUS automática que analizar."]
    few = J.analyze([_trade(0, -1.0), _trade(1, 1.5, reason="TP2", mfe=1.6), _trade(2, -1.0), _trade(3, -1.0)])
    assert (few["trades"], few["wins"], few["verdict"], few["verdict_label"]) == (4, 1, "small", "Muestra pequeña")
    assert few["insights"][0].startswith("Con 4 operaciones cerradas todavía no se puede concluir nada")
    assert "3 de 3 pérdidas: «En contra desde el principio»." in few["insights"]
    dims = [g["dimension"] for g in few["groups"]]
    assert dims == ["cause", "session", "regime", "news", "gate", "side", "market", "source"]
    # 40 London trades that lose and 40 New York trades that win, on 40 weekdays (2 March 2026 is a Monday): both
    # groups have evidence
    weekdays = [d for d in range(60) if d % 7 < 5][:40]
    many = [_trade(d, -0.9 - 0.01 * (d % 5), hour=5) for d in weekdays]
    many += [_trade(d, 0.92 + (0.3 if d % 2 == 0 else -0.3), hour=11, reason="TP2", mfe=1.4) for d in weekdays]
    out = J.analyze(many, seed=7)
    session = next(g for g in out["groups"] if g["dimension"] == "session")
    by_key = {b["key"]: b for b in session["buckets"]}
    assert (by_key["londres"]["n"], by_key["londres"]["wins"], by_key["londres"]["verdict"]) == (40, 0, "loses")
    assert by_key["nueva_york"]["verdict"] == "wins" and by_key["nueva_york"]["ci95"][0] > 0
    assert out["verdict"] == "unclear"  # overall they offset each other: the interval includes zero
    assert any(t.startswith("Sesión de entrada «Londres (3–8 h)»: R medio") and "(bajo cero) en 40 operaciones" in t
               for t in out["insights"])
    gate = next(g for g in out["groups"] if g["dimension"] == "gate")
    assert [b["key"] for b in gate["buckets"]] == ["70"]
    assert "no garantiza lo que pasará" in out["note"]


def test_the_engine_keeps_the_context_and_the_journal_and_serves_it(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("BUY")))
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            [pos] = engine.open_positions(3)
            ctx = engine._open[pos["id"]]["meta"]["context"]
            assert (ctx["regime"], ctx["gate_score"], ctx["score"], ctx["prob_up"]) == ("trend_up", 78.0, 0.5, 0.62)
            assert ctx["atr_pct"] is not None and ctx["atr_pct"] > 0
            # a 1m bar straight through its stop (98): it never went in its favour
            await engine._on_candle(SYM, "1m", {"time": pp.now_s() + 1, "open": 100.0, "high": 100.1, "low": 90.0,
                                                "close": 91.0, "volume": 1}, False)
            [h] = await engine.history(3)
            assert h["exit_reason"] == "SL"
            row = await rt.db.fetchone("SELECT meta FROM paper_positions WHERE id=?", (h["id"],))
            journal = pp.json_loads(row["meta"], {})["journal"]
            assert journal["cause"] == "en_contra" and journal["mae_r"] == pytest.approx(1.0) and journal["mfe_r"] == 0
            out = await engine.autopilot_journal()
            assert (out["trades"], out["wins"], out["verdict"]) == (1, 0, "small")
            cause = next(g for g in out["groups"] if g["dimension"] == "cause")
            assert [(b["key"], b["n"]) for b in cause["buckets"]] == [("en_contra", 1)]
            regime = next(g for g in out["groups"] if g["dimension"] == "regime")
            assert [b["label"] for b in regime["buckets"]] == ["Tendencia alcista"]
            assert (await engine.autopilot_journal()) is out  # cached until a trade closes
            await engine.stop()

    asyncio.run(scenario())


def test_journal_api_and_terminal(api_client):
    r = api_client.get("/api/paper/autopilot/journal")
    assert r.status_code == 200
    body = r.json()
    assert (body["trades"], body["verdict"], body["verdict_label"]) == (0, "none", "Sin operaciones")
    assert body["insights"] == ["Aún no hay operaciones cerradas de NEXUS automática que analizar."]
    assert isinstance(body["generated_at"], int) and len(body["groups"]) == 8
    res = api_client.post("/api/terminal", json={"text": "autopilot diario"}).json()
    assert res["ok"], res["lines"]
    text = "\n".join(line["text"] for line in res["lines"])
    assert "Diario de NEXUS automática · Sin operaciones" in text and "Aún no hay operaciones cerradas" in text
    assert "no garantiza lo que pasará" in text
