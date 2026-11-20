"""The 1k account and the vault of NEXUS automática.

The 1k account (kind "twin"): a second account with its own balance, traded by the same brain (account 3's candidates
and signals) with the small-account mindset (app.sim.autopilot_challenges.TWIN_PROFILE / TWIN_RISK) whatever the mode,
never aggressive, never ended by the engine; it travels as `twin` in the snapshot, not among the challenge accounts.

The vault (app.sim.autopilot_vault): with autopilot.vault on, the realized balance of the 10k account (3) and of the
1k account above their initial balance moves there; a later loss never takes anything back; kept across restarts."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import autopilot_challenges as CH
from app.sim import autopilot_vault as V
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


async def _engine(rt) -> pp.PaperEngine:
    await rt.settings.update({"risk.slippage_pct": 0.0})
    rt.hub = FakeHub(prices={PERP: 100.0})
    rt.nexus = BarNexus(default=_signal("BUY", 2.0))
    engine = pp.PaperEngine(rt)
    await engine.start()
    return engine


async def _set_balance(rt, aid: int, balance: float) -> None:
    await rt.db.execute("UPDATE paper_accounts SET balance=? WHERE id=?", (balance, aid))


def test_twin_record_and_view():
    rec = CH.parse_twin(None, "", now=NOW, taken=set())
    assert (rec["name"], rec["initial"], rec["target"], rec["kind"], rec["aggressive"], rec["brake_pct"]) == (
        "NEXUS 1k", 1000.0, 10000.0, "twin", False, 25.0)  # its challenge: 1.000 $ -> 10.000 $
    saved = CH.load_records({"106": {"id": 106, **rec}})
    assert saved[106]["kind"] == "twin" and saved[106]["target"] == 10000.0 and CH.is_twin(saved[106])
    view = CH.challenge_view(saved[106], account={"equity": 1900.0}, positions=[], history=[], log=[],
                             brake={"active": False}, now=NOW)
    assert view["progress_pct"] == 10.0 and view["remaining"] == 8100.0 and view["kind"] == "twin"
    # an old record without a goal still loads
    old = CH.load_records({"107": {"id": 107, **{**rec, "target": None}}})
    assert old[107]["target"] is None
    for bad, msg in ((900, "mayor que el saldo"), (200_000, "100 veces")):
        with pytest.raises(ValueError, match=msg):
            CH.parse_twin_target(bad, 1000.0)
    assert "Mentalidad 1k" in view["mindset"] and "2 operaciones" in view["mindset"]
    with pytest.raises(ValueError, match="Ya hay una cuenta"):
        CH.parse_twin(1000, "nexus 1k", now=NOW, taken={"NEXUS 1k"})


def test_the_1k_account_has_its_own_mindset(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.set_autopilot(enabled=True, source="list", symbols=[PERP])
            snap = await engine.create_twin()
            twin = snap["twin"]
            aid = twin["id"]
            assert (twin["name"], twin["initial"], twin["kind"]) == ("NEXUS 1k", 1000.0, "twin")
            assert all(c["id"] != aid for c in snap["challenges"])  # not a challenge account
            with pytest.raises(pp.PaperError, match="Ya hay una cuenta llamada"):
                await engine.create_twin()  # a second «NEXUS 1k»: the name is taken
            # its settings: the 1k mindset, never the aggressive mode, even in the New York session mode
            await rt.settings.update({"autopilot.session_aggressive": True})
            engine._ny_cache = (pp.now_s(), {**engine._ny(), "open": True})
            engine._ny_cache = (pp.now_s(), {**engine._ny(), "open": True})
            assert engine._aggressive_now(3) is True and engine._aggressive_now(aid) is False
            cfg = engine._autopilot_cfg(aid)
            assert (cfg["max_positions"], cfg["risk_pct"], cfg["lot_max_risk_pct"], cfg["max_drawdown_pct"]) == (
                2, 2.0, 3.0, 25.0)
            assert engine._perp_leverage(aid) == 10.0
            limits = engine._risk_limits(aid)
            assert (limits["daily_loss_limit_pct"], limits["max_open_risk_pct"], limits["max_positions_per_group"]) == (
                4.0, 6.0, 2)
            # it trades account 3's candidates, sized on its own 1 000 $ (2 % = 20 $ of risk)
            await _settle(engine)
            [pos] = engine.open_positions(aid)
            assert pos["symbol"] == PERP
            # the engine never ends it, even with its brake on and nothing open
            await engine.close_position(pos["id"], None, False, account_id=aid)
            await _set_balance(rt, aid, 600.0)
            await engine._challenge_tick()
            assert engine.twin_id() == aid
            await engine.stop()

    asyncio.run(scenario())


def test_the_vault_keeps_the_excess_and_never_gives_it_back(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            snap = await engine.create_twin()
            aid = snap["twin"]["id"]
            base3 = (await engine.account(3))["initial_balance"]
            await _set_balance(rt, 3, base3 + 500.25)
            await _set_balance(rt, aid, 1100.0)
            await engine._vault_tick()  # off by default: nothing moves
            assert (await engine.account(3))["balance"] == pytest.approx(base3 + 500.25)
            await rt.settings.update({"autopilot.vault": True})
            await engine._vault_tick()
            assert (await engine.account(3))["balance"] == pytest.approx(base3)
            # the 1k account keeps its gains: it compounds toward its 10k goal
            assert (await engine.account(aid))["balance"] == pytest.approx(1100.0)
            view = (await engine.autopilot_snapshot())["vault"]
            assert view["enabled"] is True and view["total"] == pytest.approx(500.25)
            assert {r["account_id"]: r["amount"] for r in view["by_account"]} == {3: 500.25}
            assert [e["amount"] for e in view["entries"]] == [500.25]
            # a loss afterwards takes nothing back
            await _set_balance(rt, 3, base3 - 300.0)
            await engine._vault_tick()
            assert (await engine.account(3))["balance"] == pytest.approx(base3 - 300.0)
            assert (await engine.autopilot_snapshot())["vault"]["total"] == pytest.approx(500.25)
            # kept across a restart
            await engine.stop()
            again = pp.PaperEngine(rt)
            await again.start()
            assert again._vault_view()["total"] == pytest.approx(500.25)
            await again.stop()

    asyncio.run(scenario())


def test_vault_record_helpers():
    v = V.load({"total": "x", "by_account": {"3": 5, "bad": "y"}, "entries": [{"ts": 1, "account_id": 3, "amount": 5}]})
    assert v["total"] == 0.0 and v["by_account"] == {"3": 5.0} and len(v["entries"]) == 1
    V.add(v, 3, 2.5, 2, 10_000)
    assert v["by_account"]["3"] == 7.5 and v["entries"][-1]["base"] == 10_000.0
    view = V.view(v, enabled=False, names={3: "NEXUS automática", 106: "NEXUS 1k"})
    assert [r["account_id"] for r in view["by_account"]] == [3, 106] and view["by_account"][1]["amount"] == 0.0
    assert "no le quita nada" in view["note"]


def test_the_1k_goal_is_noted_once_and_it_keeps_trading(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            aid = (await engine.create_twin())["twin"]["id"]
            snap = await engine.set_twin_target(5000)
            assert snap["twin"]["target"] == 5000.0 and snap["twin"]["progress_pct"] == 0.0
            with pytest.raises(pp.PaperError, match="mayor que el saldo"):
                await engine.set_twin_target(500)
            await _set_balance(rt, aid, 5200.0)
            await engine._challenge_tick()
            await engine._challenge_tick()
            twin = (await engine.autopilot_snapshot())["twin"]
            assert twin["reached_at"] and twin["progress_pct"] == 100.0 and engine.twin_id() == aid
            notes = [e for e in twin["log"] if "Meta alcanzada" in e["text"]]
            assert len(notes) == 1  # once
            await engine.stop()

    asyncio.run(scenario())


def test_the_10k_goal_counts_the_account_plus_its_vault():
    acct = {"id": 3, "equity": 9998.23, "initial_balance": 10000.0}
    vault = {"by_account": {"3": 6008.95}}
    g = V.main_goal_view(acct, vault, 100000)
    assert g["total"] == pytest.approx(16007.18) and g["saved"] == 6008.95 and g["reached"] is False
    assert g["progress_pct"] == pytest.approx((16007.18 - 10000) / 90000 * 100, abs=0.01)
    assert g["remaining"] == pytest.approx(83992.82)
    assert V.main_goal_view(acct, vault, 0) is None and V.main_goal_view(acct, {"by_account": {}}, None) is None


def test_several_small_accounts_500_and_100(runtime_factory, no_background):
    """500 $ -> 5.000 $ and 100 $ -> 1.000 $ next to the 1k account: each its own balance and goal; below 1.000 $ the
    minimum lot would not fit, so they size in fractions (no MT5 lots), with the same 2 % risk."""
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            a1k = (await engine.create_twin())["twin"]["id"]
            await engine.create_twin(500)
            snap = await engine.create_twin(100)
            names = [(t["name"], t["initial"], t["target"]) for t in snap["twins"]]
            assert names == [("NEXUS 1k", 1000.0, 10000.0), ("NEXUS 500", 500.0, 5000.0), ("NEXUS 100", 100.0, 1000.0)]
            assert snap["twin"]["id"] == a1k == engine.twin_id()  # the first one stays the 1k (MT5 source «1k»)
            a500, a100 = snap["twins"][1]["id"], snap["twins"][2]["id"]
            assert engine._autopilot_cfg(a1k)["lots"] is True
            assert engine._autopilot_cfg(a500)["lots"] is False and engine._autopilot_cfg(a100)["lots"] is False
            assert engine._autopilot_cfg(a100)["risk_pct"] == 2.0
            assert "fraccionario" in snap["twins"][2]["mindset"] and "Mentalidad 1k" in snap["twins"][0]["mindset"]
            # a goal per account
            snap = await engine.set_twin_target(2000, a100)
            assert [t["target"] for t in snap["twins"]] == [10000.0, 5000.0, 2000.0]
            with pytest.raises(pp.PaperError, match="No hay una cuenta pequeña"):
                await engine.set_twin_target(2000, 3)
            # all three trade account 3's candidates on their own balance (an account joins from the next decision)
            await engine.set_autopilot(enabled=True, source="list", symbols=[PERP])
            await _settle(engine)
            for aid in (a1k, a500, a100):
                assert [p["symbol"] for p in engine.open_positions(aid)] == [PERP], aid
            await engine.stop()

    asyncio.run(scenario())
