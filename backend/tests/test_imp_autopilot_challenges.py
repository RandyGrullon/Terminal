"""NEXUS automática's challenge accounts: extra accounts with their own balance, target, deadline and brake, traded by
the same brain as account 3 (its candidates and NEXUS's signals) in the aggressive mode, each sized on its own equity,
with its own log; the engine ends each one by itself (target, deadline, brake with nothing open)."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import config as nexus_config
from app.sim import autopilot_challenges as CH
from app.sim import paper as pp
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

PERP = "BINANCE:BTCUSDT.P"
NOW = 1_800_000_000


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_parse_and_names():
    rec = CH.parse_challenge(10, 100, None, None, "", now=NOW, taken=set())
    assert rec == {"name": "Reto 10 $ → 100 $", "initial": 10.0, "target": 100.0, "until": None, "brake_pct": 50.0,
                   "aggressive": True, "started_at": NOW, "status": "running", "ended_at": None, "result": None}
    assert CH.default_name(5000, 10000) == "Reto 5.000 $ → 10.000 $" and CH.money(12.5) == "12,50 $"
    for args, msg in (((0.5, 10, None, None, None), "saldo inicial"), ((100, 100, None, None, None), "mayor que el saldo"),
                      ((10, 2000, None, None, None), "100 veces"), ((10, 100, NOW + 10, None, None), "1 minuto"),
                      ((10, 100, None, 99, None), "freno"), ((10, 100, None, None, "x" * 41), "40 caracteres")):
        with pytest.raises(ValueError, match=msg):
            CH.parse_challenge(*args, now=NOW, taken=set())
    with pytest.raises(ValueError, match="Ya hay una cuenta"):
        CH.parse_challenge(10, 100, None, None, "Mi reto", now=NOW, taken={"mi reto"})
    saved = CH.load_records({"101": {"id": 101, **rec}, "7": {"id": 7, **rec}, "x": {}, "102": {"name": 3}})
    assert list(saved) == [101] and saved[101]["status"] == "running"


async def _engine(rt) -> pp.PaperEngine:
    await rt.settings.update({"risk.slippage_pct": 0.0})
    rt.hub = FakeHub(prices={PERP: 100.0})
    rt.nexus = BarNexus(default=_signal("BUY", 2.0))
    engine = pp.PaperEngine(rt)
    await engine.start()
    return engine


def test_each_account_trades_on_its_own_equity_and_ends_by_itself(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.set_autopilot(enabled=True, source="list", symbols=[PERP])
            await _settle(engine)
            [main] = engine.open_positions(3)
            assert main["qty"] * 2.0 == pytest.approx(10.0)  # account 3, normal mode: 1 % of 1 000
            snap = await engine.create_challenge(1000, 1100)
            await _settle(engine)
            small = await engine.create_challenge(500, 1000, None, 30, "Medio")
            await _settle(engine)
            assert [c["id"] for c in snap["challenges"]] == [101]
            [ch_a, ch_b] = sorted(small["challenges"], key=lambda c: c["id"])
            assert (ch_a["id"], ch_a["name"], ch_b["id"], ch_b["name"]) == (101, "Reto 1.000 $ → 1.100 $", 102, "Medio")
            # each one opened its own trade, aggressive: 4 % of its own equity at a 2-point stop
            [a_pos] = engine.open_positions(101)
            [b_pos] = engine.open_positions(102)
            assert a_pos["qty"] * 2.0 == pytest.approx(40.0) and b_pos["qty"] * 2.0 == pytest.approx(20.0)
            assert engine._autopilot_cfg(101)["tfs"] == ["5m", "15m", "1h"] and engine._autopilot_cfg()["tfs"] == ["1h"]
            assert engine._perp_leverage(101) == 20.0 and engine._perp_leverage() == 10.0
            assert (await engine.risk_state(102))["daily_loss_limit_pct"] == 30.0  # its brake
            assert (await engine.risk_state(3))["daily_loss_limit_pct"] == 3.0
            view = {c["id"]: c for c in (await engine.autopilot_snapshot())["challenges"]}
            assert view[101]["account"]["initial_balance"] == 1000.0 and view[101]["remaining"] == pytest.approx(100.0, abs=1)
            assert any("Empieza el reto: de 1.000 $ a 1.100 $" in e["text"] for e in view[101]["log"])
            assert any(e["kind"] == "open" for e in view[101]["log"])
            assert not any("Empieza el reto" in e["text"] for e in engine.autopilot_status()["log"])  # its own log
            # the price rises: account 101 reaches 1 100 (+20 × 5 = +100 $ on its 20 BTC); the others keep going
            rt.hub.prices[PERP] = 105.5
            engine._prices[PERP] = (105.5, time.monotonic())
            await engine._challenge_tick()
            done = engine._challenges[101]
            assert done["status"] == "reached" and done["result"]["closed"] == 1 and done["result"]["wins"] == 1
            assert engine.open_positions(101) == [] and len(engine.open_positions(102)) == 1
            assert engine._challenges[102]["status"] == "running"
            assert all(p.account_id != 101 for p in engine._autopilot_entry_profiles())  # it trades no more
            # stopped by hand without closing: its trade stays
            await engine.stop_challenge(102, close=False)
            assert engine._challenges[102]["status"] == "cancelled" and len(engine.open_positions(102)) == 1
            with pytest.raises(pp.PaperError, match="ya terminó"):
                await engine.stop_challenge(102)
            # deleted with its trades
            await engine.delete_challenge(102)
            assert 102 not in engine._challenges and engine.open_positions(102) == []
            assert await rt.db.fetchone("SELECT id FROM paper_accounts WHERE id=102") is None
            with pytest.raises(pp.PaperError, match="No hay ninguna cuenta de reto"):
                await engine.delete_challenge(102)
            # a restart keeps the accounts, their results and logs
            await engine.stop()
            again = pp.PaperEngine(rt)
            await again.start()
            assert again._challenges[101]["status"] == "reached" and again._ch_logs[101]
            await again.stop()

    asyncio.run(scenario())


def test_deadline_and_brake_end_a_challenge(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            until = int(time.time()) + 3600
            await engine.create_challenge(100, 200, until, 20)
            await engine.create_challenge(10, 100)
            await engine._challenge_tick(now=until + 1)
            assert engine._challenges[101]["status"] == "time" and engine._challenges[102]["status"] == "running"
            # the brake: equity 50 % under its 10 $ with nothing open ends it
            await rt.db.execute("UPDATE paper_accounts SET balance=4 WHERE id=102")
            await engine._challenge_tick()
            assert engine._challenges[102]["status"] == "brake"
            assert engine._challenges[102]["result"]["text"].startswith("Freno de pérdidas: meta 100,00")
            await engine.stop()

    asyncio.run(scenario())
