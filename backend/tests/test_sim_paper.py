"""BE-SIM: paper trading (level management, gate rejection, partials/BE/trailing, accounting, advice, auto-trade)."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.engine.levels import ev_hold
from app.sim import paper as pp
from tests.test_sim_fakes import FakeHub, FakeNexus, make_current

SYM = "BINANCE:BTCUSDT"


def _state(**kw) -> pp.LevelState:
    base = dict(side=1, entry=100.0, qty=3.0, qty_open=3.0, sl=98.0, tps=[102.0, 104.0, 106.0], atr=1.0,
                trail_mult=3.0, high_water=100.0, low_water=100.0)
    base.update(kw)
    return pp.LevelState(**base)


# ----------------------------------------------------------------------------- pure level logic


def test_process_bar_long_partials_break_even_and_trailing():
    st = _state()
    assert pp.process_bar(st, 101.0, 99.0, 100.5) == []
    fills = pp.process_bar(st, 102.5, 100.0, 102.0)
    assert [(f.kind, f.reason, f.price, f.qty) for f in fills] == [("partial", "TP1", 102.0, 1.0)]
    assert st.sl == 100.0 and st.qty_open == 2.0 and st.tp_hits == 1 and st.trail is None
    fills = pp.process_bar(st, 104.5, 101.0, 104.2)
    assert [(f.reason, f.price) for f in fills] == [("TP2", 104.0)]
    assert st.tp_hits == 2 and st.qty_open == pytest.approx(1.0)
    assert st.trail == pytest.approx(104.5 - 3.0)  # chandelier from the high-water mark
    pp.process_bar(st, 105.8, 104.0, 105.5)  # new high moves the trailing stop up, TP3 not reached
    assert st.trail == pytest.approx(105.8 - 3.0)
    fills = pp.process_bar(st, 103.0, 102.5, 102.6)
    assert [(f.kind, f.reason, f.price, f.qty) for f in fills] == [("close", "TRAIL", pytest.approx(102.8), 1.0)]
    assert st.qty_open == 0.0


def test_process_bar_stop_first_gap_and_break_even_label():
    st = _state()
    fills = pp.process_bar(st, 102.5, 97.0, 99.0)  # TP1 and SL inside the same bar -> stop first
    assert len(fills) == 1 and fills[0].reason == "SL" and fills[0].price == 98.0 and fills[0].market
    st = _state()
    fills = pp.process_bar(st, 97.0, 95.0, 95.5, open_=96.0)  # gap through the stop: filled at the open
    assert fills[0].price == 96.0
    st = _state(tp_hits=1, sl=100.0, qty_open=2.0)
    fills = pp.process_bar(st, 100.5, 99.5, 99.8)
    assert fills[0].reason == "SL" and fills[0].label == "Stop en punto de equilibrio"
    st = _state()
    fills = pp.process_bar(st, 0, 0, 97.5, use_hl=False)  # polled price beyond the stop: filled at that price
    assert fills[0].price == 97.5


def test_process_bar_short_single_target_and_no_partials():
    st = _state(side=-1, sl=102.0, tps=[98.0, 96.0, 94.0], high_water=100.0, low_water=100.0)
    fills = pp.process_bar(st, 100.0, 97.9, 98.5)
    assert fills[0].reason == "TP1" and st.sl == 100.0  # break-even for the short
    st = _state(tps=[103.0, None, None])  # single user target closes everything
    fills = pp.process_bar(st, 103.2, 100.0, 103.0)
    assert fills[0].kind == "close" and fills[0].reason == "TP1" and fills[0].qty == 3.0
    st = _state(partial=False)  # partials disabled: one target (TP2), all at once
    assert pp.process_bar(st, 103.0, 100.0, 102.5) == []
    fills = pp.process_bar(st, 104.1, 100.0, 104.0)
    assert fills[0].kind == "close" and fills[0].reason == "TP2" and fills[0].qty == 3.0


def test_assess_position_early_exit_and_hold():
    kw = {"sigma": 0.8, "horizon": 5}
    hold = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0, make_current("HOLD_LONG", prob_up=0.8, score=0.3), **kw)
    ev, p = ev_hold(1, 101.0, 98.0, 102.0, 2.0, 0.8, 0.8, 5)  # the engine's first-passage EV
    assert not hold["early_exit"] and hold["advice"].startswith("Mantener") and hold["warnings"] == []
    assert hold["ev_r"] == pytest.approx(ev) and hold["p_hit"] == pytest.approx(p) and ev > 0
    # a negative EV is an amber warning, never a close, unless the engine's own exit rule holds (I7): EV <= −0.10R
    # with the score against
    meh = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0, make_current("HOLD_LONG", prob_up=0.46, score=-0.3), **kw)
    assert -0.1 < meh["ev_r"] < 0 and not meh["early_exit"] and meh["exit_by"] is None
    assert meh["warnings"][0]["code"] == "WATCH_EXIT" and meh["warnings"][0]["severity"] == "warn"
    assert meh["advice"].startswith("Vigila la salida") and "score NEXUS va en contra" in meh["advice"]
    low = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0, make_current("HOLD_LONG", prob_up=0.3, score=0.1), **kw)
    assert low["ev_r"] < -0.1 and not low["early_exit"] and low["warnings"][0]["code"] == "WATCH_EXIT"
    assert low["advice"].endswith(", aunque el score NEXUS aún no gira en contra")
    rule = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0, make_current("HOLD_LONG", prob_up=0.3, score=-0.4), **kw)
    assert rule["early_exit"] and rule["exit_by"] == "ev" and rule["warnings"][0]["code"] == "EARLY_EXIT"
    assert rule["advice"] == ("Cerrar ya: valor esperado de mantener negativo (−0,48R) con el score NEXUS en contra "
                              "(−0,40)")
    bad = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0, make_current("EXIT_LONG", prob_up=0.3, score=-0.4), **kw)
    assert bad["early_exit"] and bad["warnings"][0]["code"] == "EARLY_EXIT" and bad["ev_r"] < 0
    assert bad["advice"] == "Cerrar ya: NEXUS recomienda cerrar el largo (CERRAR LARGO)" and bad["exit_by"] == "signal"
    rev = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0, make_current("SELL", prob_up=0.4, score=-0.5), **kw)
    assert rev["early_exit"] and "(VENDER)" in rev["advice"]
    short_ok = pp.assess_position("short", 99.0, 102.0, 98.0, 2.0, make_current("SELL", prob_up=0.35, score=-0.5), **kw)
    assert not short_ok["early_exit"]
    assert pp.assess_position("long", 100.0, 98.0, None, 0.0, None)["advice"] is None


# ----------------------------------------------------------------------------- engine


def _candle(t: int, o: float, h: float, l: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_paper_lifecycle_tp1_be_tp2_trail_and_accounting(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            published = []
            monkeypatch.setattr(rt.ws, "publish", lambda topic, msg: published.append((topic, msg)))
            await rt.settings.update({"risk.fee_pct": 0.1, "risk.slippage_pct": 0.0})
            rt.hub = FakeHub(prices={SYM: 100.0})
            rt.nexus = FakeNexus("TAKE")
            engine = pp.PaperEngine(rt)
            rt.paper = engine
            await engine.start()
            assert rt.hub.listeners == [engine._on_candle]
            res = await engine.open_position(1, SYM, "long", "1h")
            pos, gate = res["position"], res["gate"]
            assert gate["verdict"] == "TAKE" and rt.nexus.gate_calls[0][:3] == (SYM, "1h", "long")
            assert pos["sl"] == 98.0 and (pos["tp1"], pos["tp2"], pos["tp3"]) == (102.0, 104.0, 106.0)
            assert pos["qty"] == pytest.approx(50.0)  # 1 % of 10 000 / 2
            assert pos["gate_verdict"] == "TAKE" and pos["origin"] == "manual" and pos["status"] == "open"
            entry_fee = 50 * 100 * 0.001
            acct = await engine.account(1)
            assert acct["balance"] == pytest.approx(10000 - entry_fee) and acct["open_positions"] == 1
            assert (await rt.db.fetchval("SELECT COUNT(*) FROM gate_log WHERE source='paper' AND taken=1")) == 1
            t = int(pos["entry_time"]) + 60
            await engine._on_candle(SYM, "1h", _candle(t, 100, 103, 99, 101), False)  # wrong tf: ignored for levels
            assert engine.open_positions(1)[0]["qty_open"] == 50.0
            await engine._on_candle(SYM, "1m", _candle(t, 100, 102.3, 99.5, 102.0), False)
            p = engine.open_positions(1)[0]
            assert p["qty_open"] == pytest.approx(50 * 2 / 3) and p["sl"] == 100.0  # TP1 + break-even
            await engine._on_candle(SYM, "1m", _candle(t + 60, 102, 104.4, 101.5, 104.2), False)
            p = engine.open_positions(1)[0]
            raw = engine._open[p["id"]]
            atr = raw["meta"]["atr"]
            assert p["qty_open"] == pytest.approx(50 / 3) and p["trail"] == pytest.approx(104.4 - 3.0 * atr)
            assert p["pnl"] > 0 and p["r_multiple"] > 0
            stop = p["trail"]
            await engine._on_candle(SYM, "1m", _candle(t + 120, 104, 104.1, stop - 0.5, stop - 0.2), True)
            assert engine.open_positions(1) == []
            hist = await engine.history(1)
            closed = hist[0]
            assert closed["status"] == "closed" and closed["exit_reason"] == "TRAIL"
            assert closed["exit_label"] == "Stop dinámico" and closed["exit_price"] == pytest.approx(stop)
            gross = 50 / 3 * 2 + 50 / 3 * 4 + 50 / 3 * (stop - 100)
            fees = entry_fee + (50 / 3 * 102 + 50 / 3 * 104 + 50 / 3 * stop) * 0.001
            assert closed["pnl"] == pytest.approx(gross - fees, abs=0.02)
            assert closed["r_multiple"] == pytest.approx((gross - fees) / (50 * 2), abs=1e-3)  # net, like the engine
            acct = await engine.account(1)
            assert acct["balance"] == pytest.approx(10000 + gross - fees, abs=0.02)
            assert acct["trades"] == 1 and acct["wins"] == 1 and acct["win_rate"] == 1.0
            assert acct["realized_pnl"] == pytest.approx(gross - fees, abs=0.02)
            fills = await rt.db.fetchall("SELECT reason, qty FROM paper_fills ORDER BY id")
            assert [f["reason"] for f in fills] == ["Apertura", "Objetivo 1", "Objetivo 2", "Stop dinámico"]
            trade_msgs = [e["message"] for e in rt.bus.recent(level="trade")]
            assert trade_msgs[0].startswith(f"Abierto {SYM} largo")
            assert any("TP1 alcanzado" in m and "punto de equilibrio" in m for m in trade_msgs)
            assert trade_msgs[-1].startswith(f"Cerrado {SYM} largo +")
            assert published and published[-1][0] == "paper" and published[-1][1]["type"] == "paper"
            snap = await engine.snapshot(1)
            assert set(snap) == {"account", "positions", "history"}
            assert set(snap["account"]) == {"id", "name", "balance", "equity", "initial_balance", "realized_pnl",
                                            "unrealized_pnl", "return_pct", "trades", "wins", "losses", "win_rate",
                                            "open_positions", "auto_trade", "auto_symbols", "auto_tf"}
            assert set(closed) == {"id", "account_id", "symbol", "tf", "side", "qty", "qty_open", "entry_price",
                                   "entry_time", "sl", "tp1", "tp2", "tp3", "trail", "tp_hits", "status", "price", "exit_price",
                                   "exit_time", "pnl", "pnl_pct", "r_multiple", "exit_reason", "exit_label", "origin",
                                   "gate_verdict", "gate_score", "advice", "warnings", "instrument"}
            assert closed["instrument"] is None  # a plain position (a future or an option carries its contract)
            await engine.stop()

    asyncio.run(scenario())


def test_avoid_is_rejected_unless_forced_and_validation(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={SYM: 100.0})
            rt.nexus = FakeNexus("AVOID")
            engine = pp.PaperEngine(rt)
            await engine.start()
            with pytest.raises(pp.PaperRejected) as exc:
                await engine.open_position(1, SYM, "long", "1h")
            assert exc.value.gate["verdict"] == "AVOID" and "NO TOMAR" in exc.value.message
            assert engine.open_positions(1) == []
            res = await engine.open_position(1, SYM, "long", "1h", force=True, sl=97.0, tp=106.0, risk_pct=2.0)
            pos = res["position"]
            assert pos["gate_verdict"] == "AVOID" and pos["sl"] == 97.0
            assert (pos["tp1"], pos["tp2"], pos["tp3"]) == (106.0, None, None)
            assert pos["entry_price"] == pytest.approx(100.02)  # 0.02 % slippage on the market fill
            assert pos["qty"] == pytest.approx(10000 * 0.02 / (pos["entry_price"] - 97.0), rel=1e-6)
            assert any("FORZADA" in e["message"] for e in rt.bus.recent(level="trade"))
            with pytest.raises(pp.PaperError):
                await engine.open_position(1, SYM, "long", "1h", sl=101.0, force=True)  # stop above a long entry
            with pytest.raises(pp.PaperError):
                await engine.open_position(1, SYM, "flat", "1h")
            with pytest.raises(pp.PaperError):
                await engine.open_position(1, "NASDAQ:NOPRICE", "long", "1h", force=True)
            with pytest.raises(pp.PaperError):
                await engine.open_position(1, SYM, "short", "1h", qty=-1, force=True)
            rt.nexus = None  # without the engine: no gate, ATR-free fallback levels
            res = await engine.open_position(1, "BINANCE:ETHUSDT", "short", "1h", price=200.0)
            p = res["position"]
            assert res["gate"] is None and p["sl"] > p["entry_price"] > p["tp1"] > p["tp2"] > p["tp3"]
            closed = await engine.close_position(symbol=SYM)
            assert len(closed) == 1 and closed[0]["exit_reason"] == "MANUAL"
            with pytest.raises(pp.PaperError):
                await engine.close_position(symbol=SYM)
            with pytest.raises(pp.PaperError):
                await engine.close_position(999)
            with pytest.raises(pp.PaperError):
                await engine.close_position()
            all_closed = await engine.close_position(all=True, price=190.0)
            assert len(all_closed) == 1 and all_closed[0]["pnl"] > 0
            snap = await engine.reset(5000)
            assert snap["account"]["balance"] == 5000 and snap["account"]["initial_balance"] == 5000
            assert snap["positions"] == [] and snap["history"] == []
            with pytest.raises(pp.PaperError):
                await engine.reset(-1)
            await engine.stop()

    asyncio.run(scenario())


def test_advice_warns_and_the_auto_trader_exits_on_closed_bars(runtime_factory, no_background):
    eth = "BINANCE:ETHUSDT"

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={SYM: 100.0, eth: 100.0})
            rt.nexus = FakeNexus("TAKE")
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.set_auto(True, [eth], "1h")
            manual = (await engine.open_position(1, SYM, "long", "1h"))["position"]
            auto = (await engine.open_position(1, eth, "long", "1h", origin="auto"))["position"]
            rt.nexus.current_value = make_current("HOLD_LONG", prob_up=0.6, score=0.3)
            assert await engine.refresh_advice(force=True) == 0
            p = engine.open_positions(1)[0]
            assert p["advice"].startswith("Mantener") and p["warnings"] == []
            # live (forming-bar) advice never closes anything: the manual one is warned, the auto one waits for
            # the auto-trader's decision at the bar close
            rt.nexus.current_value = make_current("EXIT_LONG", prob_up=0.3, score=-0.5)
            assert await engine.refresh_advice(force=True) == 2
            remaining = {r["id"]: r for r in engine.open_positions(1)}
            assert set(remaining) == {manual["id"], auto["id"]}
            assert remaining[manual["id"]]["warnings"][0]["code"] == "EARLY_EXIT"
            assert remaining[auto["id"]]["advice"].endswith("el auto-trading decide al cierre de la vela")
            warns = [e["message"] for e in rt.bus.recent(level="warn")]
            assert [m for m in warns if "NEXUS recomienda CERRAR" in m] == [
                f"⚠ {SYM}: NEXUS recomienda CERRAR el largo (CERRAR LARGO)"]
            n_warn = len(warns)
            await engine.refresh_advice(force=True)  # repeated advice does not spam warnings
            assert len([e for e in rt.bus.recent(level="warn")]) == n_warn
            # the closed bar confirms NEXUS's exit: the auto-trader closes its own position, not the manual one
            t = 1_760_000_000
            await engine._on_candle(eth, "1h", _candle(t, 100, 101, 99, 100), True)
            await asyncio.gather(*list(engine._tasks))
            assert rt.nexus.upto_calls[-1] == t
            assert [r["id"] for r in engine.open_positions(1)] == [manual["id"]]
            hist = await engine.history(1)
            assert hist[0]["id"] == auto["id"] and hist[0]["exit_reason"] == "EARLY"
            assert hist[0]["exit_label"] == "Cierre anticipado"
            await engine.stop()

    asyncio.run(scenario())


def test_auto_trading_opens_only_take_signals(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={SYM: 100.0})
            rt.nexus = FakeNexus("TAKE", current=make_current("BUY", long_verdict="WAIT"))
            engine = pp.PaperEngine(rt)
            await engine.start()
            auto = await engine.set_auto(True, [SYM.lower()], "1h")
            assert auto == {"enabled": True, "symbols": [SYM], "tf": "1h"}
            t0 = 1_760_000_000
            await engine._on_candle(SYM, "1h", _candle(t0, 100, 101, 99, 100), True)
            await asyncio.gather(*list(engine._tasks))
            assert engine.open_positions(1) == []  # BUY but the gate said WAIT
            assert any("descartado por el filtro" in e["message"] for e in rt.bus.recent(level="info"))
            assert rt.nexus.upto_calls == [t0]  # NEXUS decides on the bar that just closed
            rt.nexus.current_value = make_current("BUY", long_verdict="TAKE")
            candle = _candle(t0 + 3600, 100, 101, 99, 100)
            await engine._on_candle(SYM, "1h", candle, False)  # not a closed bar: nothing
            await engine._on_candle(SYM, "4h", candle, True)  # other timeframe: nothing
            assert not engine._tasks and engine.open_positions(1) == []
            await engine._on_candle(SYM, "1h", candle, True)
            await asyncio.gather(*list(engine._tasks))
            pos = engine.open_positions(1)
            assert len(pos) == 1 and pos[0]["origin"] == "auto" and pos[0]["gate_verdict"] == "TAKE"
            assert rt.nexus.upto_calls == [t0, t0 + 3600]
            await engine._on_candle(SYM, "1h", candle, True)  # the same closed bar again: decided once
            assert not engine._tasks
            await engine._on_candle(SYM, "1h", _candle(t0 + 7200, 100, 101, 99, 100), True)
            await asyncio.gather(*list(engine._tasks))  # already in a position: no pyramiding
            assert len(engine.open_positions(1)) == 1
            acct = await engine.account(1)
            assert acct["auto_trade"] is True and acct["auto_symbols"] == [SYM]
            off = await engine.set_auto(False)
            assert off["enabled"] is False
            await engine.stop()

    asyncio.run(scenario())


def test_sim_positions_are_isolated_from_the_live_feed(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={SYM: 100.0})
            engine = pp.PaperEngine(rt)
            await engine.start()
            gate = {"verdict": "TAKE", "score": 80, "sl": 98.0, "tp1": 102.0, "tp2": 104.0, "tp3": 106.0, "checks": []}
            res = await engine.open_position(2, SYM, "long", "1h", origin="sim", price=100.0, gate=gate,
                                             check_gate=False, time_s=1_000_000)
            pid = res["position"]["id"]
            # a live 1m candle for the same real symbol must not touch the simulator's position
            await engine._on_candle(SYM, "1m", _candle(1_000_100, 100, 110, 90, 95), False)
            assert engine.sim_positions(SYM)[0]["qty_open"] == res["position"]["qty"]
            closed = await engine.sim_bar(SYM, "1h", _candle(1_000_000, 100, 102.5, 99.0, 101.0))
            assert closed == [] and engine.sim_positions(SYM)[0]["tp1"] == 102.0
            assert engine._open[pid]["tp_hits"] == 1 and engine._open[pid]["meta"]["last_price"] == 101.0
            closed = await engine.sim_bar(SYM, "1h", _candle(1_003_600, 101, 101.5, 99.5, 99.8))
            assert closed and closed[0]["exit_label"] == "Stop en punto de equilibrio"
            assert closed[0]["exit_time"] == 1_003_600 + 3600
            assert any(e["source"] == "sim" for e in rt.bus.recent(level="trade"))
            assert engine.status() == {"running": True, "open_positions": 0,
                                       "autopilot": {"enabled": False, "open_positions": 0, "watching": 0}}
            # a restart closes simulator positions left open by a previous session
            await engine.open_position(2, SYM, "long", "1h", origin="sim", price=100.0, gate=gate, check_gate=False)
            engine2 = pp.PaperEngine(rt)
            await engine2.start()
            assert engine2.sim_positions(None) == []
            await engine.stop()
            await engine2.stop()

    asyncio.run(scenario())
