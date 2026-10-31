"""Paper NEXUS decisions: first-passage EV and honest advice wording, the auto-trader deciding on the bar that
just closed (never the forming bar), and gate_log horizons counted from the decision bar."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.engine.levels import ev_hold
from app.sim import paper as pp
from app.util import floor_time
from tests.test_sim_fakes import FakeHub, make_current, make_gate

SYM = "BINANCE:ETHUSDT"
H = 3600


def _candle(t: int, o: float = 100.0, h: float = 101.0, l: float = 99.0, c: float = 100.0) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


class BarNexus:
    """NexusService stand-in: `live` answers the forming-bar view (upto=None), `closed[t]` the closed bar t."""

    def __init__(self, live: dict | None = None):
        self.live = live
        self.closed: dict[int, dict] = {}
        self.calls: list[int | None] = []

    async def current(self, symbol: str, tf: str, *, upto: int | None = None) -> dict | None:
        self.calls.append(upto)
        return self.live if upto is None else self.closed.get(int(upto))

    async def gate(self, symbol, tf, direction, entry=None, sl=None, tp=None) -> dict:
        return make_gate(direction, "TAKE", entry if entry is not None else 100.0)


# ----------------------------------------------------------------------------- EV and advice wording


def test_winning_position_is_not_told_to_close_on_a_neutral_forecast():
    """Reviewer scenario: long @100, SL 98, TP1 102, price 101.8, p_up 0.50, score −0.15, MANTENER LARGO."""
    cur = make_current("HOLD_LONG", prob_up=0.5, score=-0.15)
    near_tp = pp.assess_position("long", 101.8, 98.0, 102.0, 2.0, cur, sigma=0.8, horizon=5)
    assert near_tp["ev_r"] == pytest.approx(0.0, abs=1e-9)  # old formula: −0.9R and "Cerrar ya"
    assert near_tp["p_hit"] == pytest.approx(0.95)  # first passage: 3.8 / (0.2 + 3.8)
    assert not near_tp["early_exit"] and near_tp["advice"].startswith("Mantener")
    assert "MANTENER" not in near_tp["advice"] and near_tp["warnings"] == []
    near_sl = pp.assess_position("long", 98.2, 98.0, 102.0, 2.0, cur, sigma=0.8, horizon=5)
    assert near_sl["ev_r"] == pytest.approx(0.0, abs=1e-9)  # old formula: +0.9R
    ev, p = ev_hold(-1, 95.0, 98.0, 93.0, 2.0, 0.7, 1.2, 8)
    short = pp.assess_position("short", 95.0, 98.0, 93.0, 2.0, make_current("HOLD_SHORT", prob_up=0.3, score=-0.3),
                               sigma=1.2, horizon=8)
    assert (short["ev_r"], short["p_hit"]) == (pytest.approx(ev), pytest.approx(p))


def test_engine_trade_warnings_are_not_copied_onto_the_position():
    cur = make_current("HOLD_LONG", prob_up=0.55, score=0.2, warnings=[
        {"code": "EARLY_EXIT", "severity": "critical", "message": "Salida anticipada (operación de NEXUS)"},
        {"code": "WATCH_EXIT", "severity": "warn", "message": "EV de la operación de NEXUS"},
        {"code": "NEAR_SL", "severity": "warn", "message": "Precio a 0,2R de tu stop (97,00)"},
        {"code": "TIME_STOP", "severity": "warn", "message": "La operación lleva 40 velas"},
        {"code": "DIVERGENCE", "severity": "warn", "message": "Divergencia alcista del RSI en contra de tu corto"},
        {"code": "MACRO", "severity": "critical", "message": "Mercado: evento de alto impacto"},
    ])
    cur["position"] = {"side": "short"}
    info = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0, cur, sigma=0.8)
    assert [w["code"] for w in info["warnings"]] == ["MACRO"]
    assert not info["early_exit"] and info["advice"] == "Mercado: evento de alto impacto"
    cur["position"] = {"side": "long"}
    info = pp.assess_position("long", 101.0, 98.0, 102.0, 2.0, cur, sigma=0.8)
    assert [w["code"] for w in info["warnings"]] == ["DIVERGENCE", "MACRO"]


def test_live_advice_never_closes_auto_positions(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0})
            rt.hub = FakeHub(prices={SYM: 100.0})
            rt.nexus = BarNexus()
            engine = pp.PaperEngine(rt)
            await engine.start()
            pos = (await engine.open_position(1, SYM, "long", "1h", origin="auto"))["position"]
            engine._prices[SYM] = (101.8, engine._prices[SYM][1])
            rt.nexus.live = make_current("HOLD_LONG", prob_up=0.5, score=-0.15)  # score mildly against
            assert await engine.refresh_advice(force=True) == 0
            p = engine.open_positions(1)[0]
            assert p["id"] == pos["id"] and p["advice"].startswith("Mantener")  # old: closed "Cierre anticipado"
            rt.nexus.live = make_current("HOLD_LONG", prob_up=0.4, score=-0.3)  # EV −0,06R: above the exit level
            await engine.refresh_advice(force=True)
            p = engine.open_positions(1)[0]
            assert p["advice"].startswith("Vigila la salida") and p["warnings"][0]["severity"] == "warn"
            # EV −0,25R with the score against: the engine's exit rule (I7) says close, yet live advice closes nothing
            rt.nexus.live = make_current("HOLD_LONG", prob_up=0.2, score=-0.6)
            assert await engine.refresh_advice(force=True) == 1
            p = engine.open_positions(1)[0]
            assert p["id"] == pos["id"] and p["warnings"][0]["code"] == "EARLY_EXIT"
            assert p["advice"].startswith("Cerrar ya: valor esperado de mantener negativo (−0,25R)")
            warns = [e["message"] for e in rt.bus.recent(level="warn")]
            assert f"⚠ {SYM}: NEXUS recomienda CERRAR el largo (valor esperado de mantener −0,25R con el score en contra)" \
                in warns
            rt.nexus.live = None  # no current analysis (e.g. outdated data): the old advice is not kept
            await engine.refresh_advice(force=True)
            p = engine.open_positions(1)[0]
            assert p["advice"].startswith("Sin consejo NEXUS") and p["warnings"] == []
            assert await engine.history(1) == [] and rt.nexus.calls == [None, None, None, None]
            await engine.stop()

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- auto-trading on closed bars


def test_auto_trader_decides_on_the_closed_bar(runtime_factory, no_background):
    """The forming-bar view already shows the entry decided at the close as HOLD_LONG: only the closed bar trades."""

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={SYM: 100.0})
            rt.nexus = BarNexus(live=make_current("HOLD_LONG"))
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.set_auto(True, [SYM], "1h")
            t = 1_760_000_000
            rt.nexus.closed[t] = make_current("BUY", time=t)
            await engine._on_candle(SYM, "1h", _candle(t), True)
            await asyncio.gather(*list(engine._tasks))
            assert rt.nexus.calls == [t]
            pos = engine.open_positions(1)
            assert len(pos) == 1 and pos[0]["origin"] == "auto" and pos[0]["side"] == "long"
            row = await rt.db.fetchone("SELECT time, target_at, source FROM gate_log")
            # target = close of the 48th bar after the decision bar (the evaluator's window, every writer)
            assert (row["time"], row["target_at"], row["source"]) == (t, (floor_time(t, "1h") + 49 * H) * 1000, "paper")
            # the next closed bar: NEXUS exits the long (its EARLY / SIGNAL / TIME rules) -> the auto-trader follows
            rt.nexus.closed[t + H] = make_current("EXIT_LONG", time=t + H, prob_up=0.4, score=-0.3)
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await asyncio.gather(*list(engine._tasks))
            assert engine.open_positions(1) == []
            hist = await engine.history(1)
            assert hist[0]["exit_reason"] == "EARLY" and hist[0]["exit_label"] == "Cierre anticipado"
            await engine.stop()

    asyncio.run(scenario())


def test_auto_trader_reverses_on_a_confirmed_opposite_signal(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={SYM: 100.0})
            rt.nexus = BarNexus()
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.set_auto(True, [SYM], "1h")
            t = 1_760_000_000
            rt.nexus.closed[t] = make_current("BUY", time=t)
            rt.nexus.closed[t + H] = make_current("HOLD_LONG", time=t + H, prob_up=0.48, score=-0.3)
            rt.nexus.closed[t + 2 * H] = make_current("SELL", time=t + 2 * H, prob_up=0.3, score=-0.5,
                                                      long_verdict="AVOID", short_verdict="TAKE")
            for k in range(3):
                await engine._on_candle(SYM, "1h", _candle(t + k * H), True)
                await asyncio.gather(*list(engine._tasks))
                if k == 1:  # EV −0,06R (above the −0,10R exit level) with the score against: a warning only
                    assert [p["side"] for p in engine.open_positions(1)] == ["long"]
                    assert engine.open_positions(1)[0]["warnings"][0]["code"] == "WATCH_EXIT"
            assert [p["side"] for p in engine.open_positions(1)] == ["short"]
            hist = await engine.history(1)
            assert hist[0]["exit_reason"] == "REVERSE" and hist[0]["exit_label"] == "Señal contraria"
            await engine.stop()

    asyncio.run(scenario())


def test_back_to_back_closed_bars_never_open_two_positions(runtime_factory, no_background):
    """After a feed gap the hub can close several bars at once: their checks overlap but trade one at a time."""

    class SlowNexus(BarNexus):
        async def current(self, symbol, tf, *, upto=None):
            await asyncio.sleep(0.05)
            return await super().current(symbol, tf, upto=upto)

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub(prices={SYM: 100.0, "BINANCE:BTCUSDT": 100.0})
            rt.nexus = SlowNexus(live=make_current("BUY"))
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.set_auto(True, [SYM, "BINANCE:BTCUSDT"], "1h")
            t = 1_760_000_000
            for k in range(2):
                rt.nexus.closed[t + k * H] = make_current("BUY", time=t + k * H)
                await engine._on_candle(SYM, "1h", _candle(t + k * H), True)
            await engine._on_candle("BINANCE:BTCUSDT", "1h", _candle(t), True)  # other symbols are not held up
            await asyncio.gather(*list(engine._tasks))
            assert sorted(p["symbol"] for p in engine.open_positions(1)) == ["BINANCE:BTCUSDT", SYM]
            await engine.stop()

    asyncio.run(scenario())


def test_a_burst_of_closed_bars_enters_only_on_the_newest(runtime_factory, no_background):
    """The feed catching up after a stall closes several bars at once: every bar's exit is followed, but an entry
    signal of an older bar is stale (old: a trade opened on it and closed at once on the next bar, paying 2 fees)."""

    class SlowNexus(BarNexus):
        async def current(self, symbol, tf, *, upto=None):
            await asyncio.sleep(0.02)
            return await super().current(symbol, tf, upto=upto)

    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0})
            rt.hub = FakeHub(prices={SYM: 100.0})
            rt.nexus = SlowNexus()
            engine = pp.PaperEngine(rt)
            await engine.start()
            await engine.set_auto(True, [SYM], "1h")
            t = 1_760_000_000
            rt.nexus.closed[t] = make_current("BUY", time=t)
            rt.nexus.closed[t + H] = make_current("EXIT_LONG", time=t + H, prob_up=0.4, score=-0.3)
            for k in range(2):
                await engine._on_candle(SYM, "1h", _candle(t + k * H), True)
            await asyncio.gather(*list(engine._tasks))
            assert engine.open_positions(1) == [] and await engine.history(1) == []
            assert rt.nexus.calls == [t + H]  # flat: the older bar is not even analysed
            # an auto long, then a burst with NEXUS's confirmed reversal followed by NEXUS holding its short
            pos = (await engine.open_position(1, SYM, "long", "1h", origin="auto"))["position"]
            rt.nexus.closed[t + 2 * H] = make_current("SELL", time=t + 2 * H, prob_up=0.3, score=-0.5,
                                                      long_verdict="AVOID", short_verdict="TAKE")
            rt.nexus.closed[t + 3 * H] = make_current("HOLD_SHORT", time=t + 3 * H, prob_up=0.35, score=-0.4)
            for k in (2, 3):
                await engine._on_candle(SYM, "1h", _candle(t + k * H), True)
            await asyncio.gather(*list(engine._tasks))
            hist = await engine.history(1)
            assert [h["id"] for h in hist] == [pos["id"]] and hist[0]["exit_reason"] == "REVERSE"  # exit followed
            assert engine.open_positions(1) == []  # no short opened late on the older bar's signal
            await engine.stop()

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- gate_log horizon and source


def test_gate_log_horizon_counts_from_the_decision_bar(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"nexus.time_stop_bars": 24})
            rt.hub = FakeHub(prices={SYM: 100.0, "SIM:NEXUS": 100.0})
            engine = pp.PaperEngine(rt)
            await engine.start()
            replay_t = 1_700_000_000  # a bar revealed by a replay session, months ago
            await engine.open_position(2, SYM, "long", "4h", origin="sim", price=100.0, check_gate=False,
                                       gate=make_gate("long", time=replay_t), time_s=replay_t + 4 * H)
            future_t = 1_900_000_000  # a synthetic bar ahead of the wall clock
            await engine.open_position(2, "SIM:NEXUS", "short", "1h", origin="sim", price=100.0, check_gate=False,
                                       gate=make_gate("short", time=future_t), time_s=future_t + H)
            await engine.open_position(1, "SIM:NEXUS", "long", "1h", price=100.0, check_gate=False,
                                       gate=make_gate("long", time=future_t + H))
            no_time = {k: v for k, v in make_gate("short").items() if k != "time"}
            await engine.open_position(1, SYM, "short", "1h", price=100.0, check_gate=False, gate=no_time,
                                       time_s=1_750_000_000)
            rows = await rt.db.fetchall("SELECT symbol, tf, time, target_at, source FROM gate_log ORDER BY id")
            assert [(r["symbol"], r["time"], r["target_at"], r["source"]) for r in rows] == [
                # close of the 24th bar after the decision bar (time_stop_bars = 24), never the wall clock
                (SYM, replay_t, (floor_time(replay_t, "4h") + 25 * 4 * H) * 1000, "sim"),
                ("SIM:NEXUS", future_t, (floor_time(future_t, "1h") + 25 * H) * 1000, "sim"),
                ("SIM:NEXUS", future_t + H, (floor_time(future_t + H, "1h") + 25 * H) * 1000, "sim"),  # simulated market
                (SYM, 1_750_000_000, (floor_time(1_750_000_000, "1h") + 25 * H) * 1000, "paper"),
            ]
            await engine.stop()

    asyncio.run(scenario())
