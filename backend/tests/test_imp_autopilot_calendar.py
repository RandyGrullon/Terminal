"""The trading calendar (bitácora por día) of NEXUS automática: GET /api/paper/autopilot/calendar → its closed trades
in a time range, each with its cause (app.sim.journal) and a plain-Spanish lesson (app.sim.calendar_log)."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config as nexus_config
from app.sim import calendar_log as CAL
from app.sim import paper as pp

NOW = 1_800_000_000


def _row(**kw):
    base = {"id": 1, "account_id": 3, "symbol": "BINANCE:XAUUSDT.P", "tf": "15m", "side": "short", "qty": 1.0,
            "entry_time": NOW - 7200, "exit_time": NOW - 3600, "entry_price": 3600.0, "exit_price": 3630.0,
            "pnl": -30.0, "r_multiple": -1.0, "exit_reason": "SL", "status": "closed",
            "meta": json.dumps({"context": {"source": "nexus", "why": "NEXUS dijo VENDER", "gate_line": "TOMAR 70/100"},
                                "journal": {"mae_r": 1.0, "mfe_r": 0.1, "cause": "en_contra", "held_s": 3600}})}
    base.update(kw)
    return base


def test_entry_cause_and_lesson():
    e = CAL.entry(_row(), {3: "Cuenta 10k"})
    assert (e["result"], e["cause"], e["cause_label"]) == ("loss", "en_contra", "En contra desde el principio")
    assert "tocó el stop" in e["lesson"] and e["why"] == "NEXUS dijo VENDER" and e["account"] == "Cuenta 10k"
    # a strategy's early exit is labelled as the strategy's, with its name
    meta = json.dumps({"context": {"source": "script:9:Breakout de Donchian"}, "journal": {"mfe_r": 0.2, "mae_r": 0.4}})
    e = CAL.entry(_row(exit_reason="SIGNAL", r_multiple=-0.3, pnl=-9.0, meta=meta), {})
    assert e["cause"] == "salida_estrategia_perdida" and e["strategy"] == "Breakout de Donchian"
    e = CAL.entry(_row(exit_reason="TP1", r_multiple=1.5, pnl=45.0, meta="{}"), {})
    assert e["result"] == "win" and e["cause"] == "objetivo" and e["account"] == "Cuenta 3"


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_engine_calendar_range_and_accounts(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = pp.PaperEngine(rt)
            await engine.start()
            cols = ("account_id", "symbol", "tf", "side", "qty", "qty_open", "entry_price", "entry_time", "sl", "status",
                    "exit_price", "exit_time", "pnl", "r_multiple", "exit_reason", "origin", "meta")
            for aid, t, pnl in ((3, NOW - 3600, -30.0), (3, NOW - 90 * 86400, 10.0), (1, NOW - 3600, 99.0)):
                await rt.db.execute(f"INSERT INTO paper_positions({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                    (aid, "BINANCE:XAUUSDT.P", "15m", "short", 1.0, 0.0, 3600.0, t - 600, 3630.0, "closed",
                                     3630.0, t, pnl, -1.0, "SL", "autopilot", "{}"))
            cal = await engine.calendar(NOW - 86400, NOW)
            assert [t["pnl"] for t in cal["trades"]] == [-30.0]  # in range, NEXUS accounts only (not account 1)
            assert cal["trades"][0]["account"] == "Cuenta 10k" and cal["note"]
            with pytest.raises(pp.PaperError):
                await engine.calendar(NOW, NOW - 1)
            with pytest.raises(pp.PaperError):
                await engine.calendar(NOW - 500 * 86400, NOW)
            await engine.stop()

    asyncio.run(scenario())
