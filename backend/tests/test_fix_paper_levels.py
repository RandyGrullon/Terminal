"""Paper level management fixes: a stop moved by a fill takes effect from the next bar (live 1m feed, polled prices
and simulator bars), stale quotes never manage levels, R multiples net of fees, fees by asset class."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from app.util import now_ms
from tests.test_sim_fakes import FakeHub, FakeNexus

SYM = "BINANCE:BTCUSDT"


def _candle(t: int, o: float, h: float, l: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


async def _engine(rt, hub=None, nexus="TAKE") -> pp.PaperEngine:
    await rt.settings.update({"risk.fee_pct": 0.1, "risk.slippage_pct": 0.0})
    rt.hub = hub or FakeHub(prices={SYM: 100.0})
    rt.nexus = FakeNexus(nexus) if nexus else None
    engine = pp.PaperEngine(rt)
    await engine.start()
    return engine


# ----------------------------------------------------------------------------- same-bar stop moves


def test_pure_stop_close_only_keeps_targets_on_the_high():
    st = pp.LevelState(side=1, entry=100.0, qty=3.0, qty_open=2.0, sl=100.0, tps=[102.0, 104.0, 106.0], tp_hits=1,
                       atr=1.0, high_water=102.1, low_water=99.5)
    assert pp.process_bar(st, 102.1, 99.5, 101.5, open_=100.0, stop_hl=False) == []  # the dip predates the move
    fills = pp.process_bar(st, 104.2, 99.5, 103.0, stop_hl=False)  # a later new high still fills TP2
    assert [(f.reason, f.price) for f in fills] == [("TP2", 104.0)]
    st = pp.LevelState(side=1, entry=100.0, qty=3.0, qty_open=2.0, sl=100.0, tps=[102.0, 104.0, 106.0], tp_hits=1)
    fills = pp.process_bar(st, 102.1, 99.5, 99.8, open_=99.6, stop_hl=False)  # the close itself crossed the stop
    assert [(f.reason, f.price, f.label) for f in fills] == [("SL", 99.8, "Stop en punto de equilibrio")]


def test_live_feed_break_even_after_tp1_waits_for_the_next_bar(runtime_factory, no_background):
    """Reviewer scenario: three cumulative polls of one 1m bar; the 99.5 dip happened before TP1."""

    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            pos = (await engine.open_position(1, SYM, "long", "1h"))["position"]
            assert (pos["entry_price"], pos["sl"], pos["tp1"]) == (100.0, 98.0, 102.0)
            t = int(pos["entry_time"]) + 60
            await engine._on_candle(SYM, "1m", _candle(t, 100, 100.4, 99.5, 99.7), False)
            await engine._on_candle(SYM, "1m", _candle(t, 100, 102.1, 99.5, 102.0), False)
            p = engine.open_positions(1)[0]
            assert p["tp_hits"] == 1 and p["sl"] == 100.0 and p["qty_open"] == pytest.approx(pos["qty"] * 2 / 3)
            await engine._on_candle(SYM, "1m", _candle(t, 100, 102.1, 99.5, 101.5), False)
            await engine._on_candle(SYM, "1m", _candle(t, 100, 102.1, 99.5, 101.4), True)  # the bar closes
            p = engine.open_positions(1)[0]  # old behaviour: closed at break-even with the market at 101.5
            assert p["status"] == "open" and p["qty_open"] == pytest.approx(pos["qty"] * 2 / 3)
            await engine._on_candle(SYM, "1m", _candle(t + 60, 101.4, 101.8, 99.9, 100.2), False)  # next bar
            assert engine.open_positions(1) == []
            closed = (await engine.history(1))[0]
            assert closed["exit_reason"] == "SL" and closed["exit_label"] == "Stop en punto de equilibrio"
            assert closed["exit_price"] == pytest.approx(100.0)
            await engine.stop()

    asyncio.run(scenario())


def test_live_feed_trailing_stop_set_by_tp2_waits_for_the_next_bar(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            pos = (await engine.open_position(1, SYM, "long", "1h"))["position"]
            atr = engine._open[pos["id"]]["meta"]["atr"]
            assert atr == pytest.approx(1.0)  # no candles: risk / sl_atr
            t = int(pos["entry_time"]) + 60
            await engine._on_candle(SYM, "1m", _candle(t, 100, 102.2, 100.5, 102.1), True)  # TP1
            await engine._on_candle(SYM, "1m", _candle(t + 60, 102.1, 102.3, 101.2, 101.5), False)
            await engine._on_candle(SYM, "1m", _candle(t + 60, 102.1, 104.6, 101.2, 104.5), False)  # TP2
            p = engine.open_positions(1)[0]
            assert p["tp_hits"] == 2 and p["trail"] == pytest.approx(104.6 - 3 * atr)  # 101.6 > the bar's low
            await engine._on_candle(SYM, "1m", _candle(t + 60, 102.1, 104.6, 101.2, 104.0), True)
            assert engine.open_positions(1)[0]["qty_open"] == pytest.approx(pos["qty"] / 3)
            await engine._on_candle(SYM, "1m", _candle(t + 120, 104.0, 104.2, 101.5, 101.7), False)
            closed = (await engine.history(1))[0]
            assert closed["exit_reason"] == "TRAIL" and closed["exit_price"] == pytest.approx(101.6)
            await engine.stop()

    asyncio.run(scenario())


def test_simulator_bar_resent_after_tp1_checks_the_stop_on_its_close(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, nexus=None)
            gate = {"verdict": "TAKE", "score": 80, "sl": 98.0, "tp1": 102.0, "tp2": 104.0, "tp3": 106.0, "checks": []}
            t0 = 1_000_000
            res = await engine.open_position(2, SYM, "long", "1h", origin="sim", price=100.0, gate=gate,
                                             check_gate=False, time_s=t0)
            pid = res["position"]["id"]
            assert await engine.sim_bar(SYM, "1h", _candle(t0, 100, 102.5, 99.5, 101.0)) == []
            assert engine._open[pid]["tp_hits"] == 1 and engine._open[pid]["sl"] == 100.0
            assert await engine.sim_bar(SYM, "1h", _candle(t0, 100, 102.5, 99.5, 101.0)) == []  # same bar again
            closed = await engine.sim_bar(SYM, "1h", _candle(t0 + 3600, 101, 101.5, 99.8, 100.4))
            assert closed and closed[0]["exit_label"] == "Stop en punto de equilibrio"
            assert closed[0]["exit_price"] == pytest.approx(100.0)
            await engine.stop()

    asyncio.run(scenario())


def test_only_the_bar_of_the_move_is_restricted_even_after_a_simulator_rewind(runtime_factory, no_background):
    sim = "SIM:NEXUS"

    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, hub=FakeHub(prices={sim: 100.0}))
            pos = (await engine.open_position(1, sim, "long", "1h"))["position"]
            b = int(pos["entry_time"]) // 3600 * 3600 + 200 * 3600  # synthetic bars run ahead of the clock
            await engine._on_candle(sim, "1h", _candle(b, 100, 102.5, 99.5, 101.0), True)  # TP1 + break-even
            await engine._on_candle(sim, "1h", _candle(b, 100, 102.5, 99.5, 101.0), True)  # the same bar again
            assert engine.open_positions(1)[0]["tp_hits"] == 1
            # a new session restarts the series earlier: a different bar, its low counts again
            await engine._on_candle(sim, "1h", _candle(b - 100 * 3600, 101, 101.2, 99.7, 100.5), True)
            closed = (await engine.history(1))[0]
            assert closed["exit_label"] == "Stop en punto de equilibrio" and closed["exit_price"] == 100.0
            await engine.stop()

    asyncio.run(scenario())


def test_a_polled_move_restricts_the_live_bar_it_falls_in(runtime_factory, no_background, monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr(pp, "now_s", lambda: clock[0])

    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            pos = (await engine.open_position(1, SYM, "long", "1h"))["position"]
            assert pos["entry_time"] == clock[0]
            clock[0] += 600
            rt.hub.prices[SYM] = 102.1
            engine._prices[SYM] = (100.0, time.monotonic() - 60)  # no live candles for a while
            await engine._poll_stale_prices()
            assert engine.open_positions(1)[0]["tp_hits"] == 1  # TP1 + break-even from the polled price
            # the feed is back: this minute started before the polled move and its low (99.6) may predate it
            await engine._on_candle(SYM, "1m", _candle(clock[0] - 10, 100.2, 102.3, 99.6, 101.9), False)
            assert engine.open_positions(1)[0]["status"] == "open"
            await engine._on_candle(SYM, "1m", _candle(clock[0] + 50, 101.9, 102.0, 99.9, 100.3), False)
            assert engine.open_positions(1) == []
            await engine.stop()

    asyncio.run(scenario())


def test_pure_late_bar_checks_targets_but_not_the_stop():
    st = pp.LevelState(side=1, entry=100.0, qty=3.0, qty_open=2.0, sl=100.0, tps=[102.0, 104.0, 106.0], tp_hits=1,
                       atr=1.0, high_water=102.1, low_water=99.5)
    fills = pp.process_bar(st, 104.2, 99.0, 99.5, open_=99.2, check_stop=False)
    assert [(f.reason, f.price) for f in fills] == [("TP2", 104.0)] and st.qty_open == pytest.approx(1.0)


def test_a_late_bar_from_before_a_polled_move_never_checks_the_moved_stop(runtime_factory, no_background,
                                                                          monkeypatch):
    """The feed stalls, the poll fills TP1 (stop to break-even), the feed comes back: the hub first re-sends, closed,
    the bar that was forming when it stalled. Its low and even its close predate the move."""
    clock = [1_800_000_000]
    monkeypatch.setattr(pp, "now_s", lambda: clock[0])

    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.open_position(1, SYM, "long", "1h")
            b0 = clock[0] + 60
            clock[0] = b0 + 20
            await engine._on_candle(SYM, "1m", _candle(b0, 100.0, 100.2, 99.4, 99.6), False)  # dip, old SL 98
            clock[0] = b0 + 75  # the stall: the poll sees TP1 inside the next minute
            rt.hub.prices[SYM] = 102.1
            engine._prices[SYM] = (99.6, time.monotonic() - 60)
            await engine._poll_stale_prices()
            assert engine.open_positions(1)[0]["sl"] == 100.0
            clock[0] = b0 + 80
            await engine._on_candle(SYM, "1m", _candle(b0, 100.0, 100.2, 99.4, 99.6), True)  # late, closed
            assert engine.open_positions(1)[0]["status"] == "open"  # old: closed at break-even, market at 102.1
            await engine._on_candle(SYM, "1m", _candle(b0 + 60, 99.6, 102.3, 99.5, 101.9), False)  # bar of the move
            assert engine.open_positions(1)[0]["status"] == "open"
            clock[0] = b0 + 125
            await engine._on_candle(SYM, "1m", _candle(b0 + 120, 101.9, 102.0, 99.9, 100.3), False)
            closed = (await engine.history(1))[0]
            assert closed["exit_label"] == "Stop en punto de equilibrio" and closed["exit_price"] == 100.0
            await engine.stop()

    asyncio.run(scenario())


def test_a_late_bar_never_moves_the_moment_of_the_move_back(runtime_factory, no_background, monkeypatch):
    """A late bar that ratchets the trailing stop must not lift the restriction on the bar of an earlier polled
    move (that bar's low may predate it)."""
    clock = [1_800_000_000]
    monkeypatch.setattr(pp, "now_s", lambda: clock[0])

    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.open_position(1, SYM, "long", "1h")
            m1 = clock[0] + 60
            clock[0] = m1 + 10
            await engine._on_candle(SYM, "1m", _candle(m1, 100.0, 104.2, 100.1, 104.0), False)  # TP1 + TP2
            assert engine.open_positions(1)[0]["trail"] == pytest.approx(101.2)
            clock[0] = m1 + 150  # the stall: the poll ratchets the trail inside minute m1 + 120
            rt.hub.prices[SYM] = 104.5
            engine._prices[SYM] = (104.0, time.monotonic() - 60)
            await engine._poll_stale_prices()
            assert engine.open_positions(1)[0]["trail"] == pytest.approx(101.5)
            clock[0] = m1 + 160  # the feed is back: the late minute m1 + 60 lifts the trail to 101.8
            await engine._on_candle(SYM, "1m", _candle(m1 + 60, 104.0, 104.8, 101.6, 104.4), True)
            assert engine.open_positions(1)[0]["trail"] == pytest.approx(101.8)
            await engine._on_candle(SYM, "1m", _candle(m1 + 120, 104.4, 104.6, 101.7, 104.3), False)
            assert engine.open_positions(1)[0]["status"] == "open"  # its 101.7 low may predate the polled move
            clock[0] = m1 + 185
            await engine._on_candle(SYM, "1m", _candle(m1 + 180, 104.3, 104.4, 101.7, 102.0), False)
            closed = (await engine.history(1))[0]
            assert closed["exit_reason"] == "TRAIL" and closed["exit_price"] == pytest.approx(101.8)
            await engine.stop()

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- stale quotes


class QuoteHub(FakeHub):
    """Hub whose quotes carry the real time of the price (`ts`) and the `stale` flag, like the real hub."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.quote: dict | None = None
        self.last_price_calls = 0

    async def quotes(self, symbols):
        return {s: dict(self.quote, symbol=s) for s in symbols if self.quote is not None}

    async def last_price(self, symbol):
        self.last_price_calls += 1  # a bare float: its age is unknown
        return await super().last_price(symbol)


def test_stale_quotes_never_move_stops_or_fill_targets(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, hub=QuoteHub(prices={SYM: 100.0}))
            pos = (await engine.open_position(1, SYM, "long", "1h"))["position"]
            rt.hub.last_price_calls = 0
            engine._prices[SYM] = (100.0, time.monotonic() - 60)
            three_h = now_ms() - 3 * 3600 * 1000
            rt.hub.quote = {"price": 97.0, "ts": three_h, "stale": True}  # a 3-hour-old cached close below the SL
            await engine._poll_stale_prices()
            await engine._poll_stale_prices()
            assert engine.open_positions(1)[0]["status"] == "open"
            warns = [e["message"] for e in rt.bus.recent(level="warn") if "sin precio en vivo" in e["message"]]
            assert warns == [f"{SYM}: sin precio en vivo (el último es de hace 3 h); el stop y los objetivos se "
                             "vigilarán cuando vuelva a cotizar"]  # once per episode
            assert engine.cached_price(SYM, pp.PRICE_STALE_S) is None  # not cached as a live price either
            rt.hub.quote = {"price": 97.0, "ts": now_ms() - 10 * 60 * 1000}  # unflagged but 10 minutes old
            await engine._poll_stale_prices()
            assert engine.open_positions(1)[0]["status"] == "open"
            rt.hub.quote = {"price": 97.0, "ts": now_ms() - 2000}  # live
            await engine._poll_stale_prices()
            assert engine.open_positions(1) == []
            closed = (await engine.history(1))[0]
            assert closed["id"] == pos["id"] and closed["exit_reason"] == "SL" and closed["exit_price"] == 97.0
            assert rt.hub.last_price_calls == 0
            await engine.stop()

    asyncio.run(scenario())


def test_an_old_bar_from_the_feed_is_not_a_live_price(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            pos = (await engine.open_position(1, SYM, "long", "1h"))["position"]
            engine._prices[SYM] = (100.0, time.monotonic() - 60)
            old = int(pos["entry_time"]) // 60 * 60 - 3600  # e.g. the last session's bar of a closed market
            await engine._on_candle(SYM, "1m", _candle(old, 97.5, 97.6, 97.0, 97.2), False)
            assert engine.open_positions(1)[0]["status"] == "open"  # its 97.2 close is not the market now
            assert engine.cached_price(SYM, pp.PRICE_STALE_S) is None
            await engine._on_candle(SYM, "1m", _candle(int(pos["entry_time"]) + 60, 98.5, 98.6, 97.4, 97.6), False)
            assert engine.open_positions(1) == []
            await engine.stop()

    asyncio.run(scenario())


def test_quote_is_live():
    now = 1_800_000_000_000
    assert pp.quote_is_live({"price": 1.0}, now)  # no timestamp: as the hub sends it
    assert pp.quote_is_live({"price": 1.0, "ts": now - 119_000}, now)
    assert not pp.quote_is_live({"price": 1.0, "ts": now - 121_000}, now)
    assert not pp.quote_is_live({"price": 1.0, "ts": now, "stale": True}, now)
    assert not pp.quote_is_live(None, now)


# ----------------------------------------------------------------------------- R net of fees, fees by class


def test_r_multiple_is_net_of_fees_like_the_engine(runtime_factory, no_background):
    """Reviewer scenario: tight stop, TP1 then break-even: a money-losing trade must not read '+R'."""

    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, nexus=None)
            pos = (await engine.open_position(1, SYM, "long", "1h", sl=99.7))["position"]
            qty = pos["qty"]
            assert (pos["tp1"], pos["tp2"]) == (pytest.approx(100.3), pytest.approx(100.6))
            t = int(pos["entry_time"]) + 60
            await engine._on_candle(SYM, "1m", _candle(t, 100, 100.35, 100.0, 100.3), True)  # TP1, stop to 100
            p = engine.open_positions(1)[0]
            fees = engine._open[p["id"]]["meta"]["fees"]
            gross = qty / 3 * 0.3 + (100.3 - 100.0) * qty * 2 / 3
            assert p["r_multiple"] == pytest.approx((gross - fees) / (qty * 0.3), abs=1e-3)
            await engine._on_candle(SYM, "1m", _candle(t + 60, 100.3, 100.31, 99.9, 99.95), True)  # break-even
            closed = (await engine.history(1))[0]
            assert closed["pnl"] < 0 and closed["r_multiple"] < 0
            assert closed["r_multiple"] == pytest.approx(closed["pnl"] / (qty * 0.3), abs=1e-3)
            msg = [e["message"] for e in rt.bus.recent(level="trade")][-1]
            assert msg.startswith(f"Cerrado {SYM} largo −0,3R")
            acct = await engine.account(1)
            assert acct["wins"] == 0 and acct["losses"] == 1
            await engine.stop()

    asyncio.run(scenario())


def test_fees_follow_the_asset_class(runtime_factory, no_background):
    fx = "FX:EURUSD"

    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, hub=FakeHub(prices={SYM: 100.0, fx: 1.1}), nexus=None)
            p = (await engine.open_position(1, fx, "long", "1h", sl=1.098, tp=1.104))["position"]
            fee = engine._open[p["id"]]["meta"]["fees"]
            assert fee == pytest.approx(p["qty"] * 1.1 * 0.005 / 100)  # forex default, not the crypto 0.1 %
            closed = (await engine.close_position(p["id"], price=1.104))[0]
            exit_fee = p["qty"] * 1.104 * 0.005 / 100
            assert closed["pnl"] == pytest.approx(p["qty"] * 0.004 - fee - exit_fee, abs=0.01)  # rounded to cents
            c = (await engine.open_position(1, SYM, "long", "1h", sl=98.0))["position"]
            assert engine._open[c["id"]]["meta"]["fees"] == pytest.approx(c["qty"] * 100 * 0.1 / 100)
            await rt.settings.update({"risk.fee_pct_by_class": {"forex": 0.01}})
            p2 = (await engine.open_position(1, fx, "short", "1h", sl=1.102))["position"]
            assert engine._open[p2["id"]]["meta"]["fees"] == pytest.approx(p2["qty"] * 1.1 * 0.01 / 100)
            await engine.stop()

    asyncio.run(scenario())
