"""paper-db · pending limit / stop orders of the demo account (improvement 6).

Placement checks (side against the market, levels, expiry, gate at the order price), fills from the live feed (limit
touched, stop crossed, gaps at the open, bars older than the order judged by their close, replayed bars never fill),
the gate asked again at the fill (NO TOMAR cancels), positions managed like market ones, cancel / expiry / restart /
reset, WS and the API.
Runs on PostgreSQL too with NEXUS_TEST_PG_URL (runtime_factory / api_client).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from tests.test_sim_fakes import FakeHub, FakeNexus

SYM = "BINANCE:BTCUSDT"
ETH = "BINANCE:ETHUSDT"
T0 = 1_800_000_000  # fixed clock (aligned to the minute)


def _bar(t: int, o: float, h: float, l: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)
    monkeypatch.setattr(pp, "now_s", lambda: now[0])
    monkeypatch.setattr(pp, "now_ms", lambda: now[0] * 1000)
    return now


async def _engine(rt, verdict: str = "TAKE", prices: dict | None = None) -> pp.PaperEngine:
    await rt.settings.update({"risk.fee_pct": 0.1, "risk.slippage_pct": 0.02})
    rt.hub = FakeHub(prices=prices or {SYM: 100.0, ETH: 200.0})
    rt.nexus = FakeNexus(verdict)
    engine = pp.PaperEngine(rt)
    rt.paper = engine
    await engine.start()
    return engine


async def _settle(engine: pp.PaperEngine) -> None:
    while engine._tasks:
        await asyncio.gather(*list(engine._tasks))


# ----------------------------------------------------------------------------- pure trigger rules


def test_limit_triggers_when_touched_at_its_price_or_better():
    trig = pp.order_trigger
    assert trig(1, "limit", 95.0, 97.0, 95.01, 96.0) is None
    assert trig(1, "limit", 95.0, 97.0, 95.0, 96.0) == 95.0  # touched
    assert trig(1, "limit", 95.0, 95.5, 93.0, 94.0, open_=94.5) == 94.5  # gap below the limit: the (better) open
    assert trig(1, "limit", 95.0, 0, 0, 94.0, use_hl=False) == 95.0  # close only: never better than the limit
    assert trig(1, "limit", 95.0, 0, 0, 95.2, use_hl=False) is None
    assert trig(-1, "limit", 105.0, 104.99, 103.0, 104.0) is None
    assert trig(-1, "limit", 105.0, 105.0, 103.0, 104.0) == 105.0
    assert trig(-1, "limit", 105.0, 107.0, 105.5, 106.0, open_=106.5) == 106.5


def test_stop_triggers_when_crossed_and_fills_at_market():
    trig = pp.order_trigger
    assert trig(1, "stop", 105.0, 104.99, 103.0, 104.0) is None
    assert trig(1, "stop", 105.0, 105.3, 104.0, 105.1, open_=104.2) == 105.0
    assert trig(1, "stop", 105.0, 107.0, 105.6, 106.0, open_=106.0) == 106.0  # gap above: the (worse) open
    assert trig(1, "stop", 105.0, 0, 0, 106.0, use_hl=False) == 106.0  # close only: the observed (worse) price
    assert trig(-1, "stop", 95.0, 96.0, 95.01, 95.5) is None
    assert trig(-1, "stop", 95.0, 96.0, 94.9, 95.2, open_=95.8) == 95.0
    assert trig(-1, "stop", 95.0, 94.5, 93.0, 94.0, open_=94.2) == 94.2
    assert trig(-1, "stop", 95.0, 0, 0, 94.0, use_hl=False) == 94.0


# ----------------------------------------------------------------------------- placement


def test_placement_validation_messages(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            cases = [
                (dict(side="long", kind="limit", price=101.0), "compra stop"),
                (dict(side="long", kind="stop", price=99.0), "compra límite"),
                (dict(side="short", kind="limit", price=99.0), "venta stop"),
                (dict(side="short", kind="stop", price=101.0), "venta límite"),
                (dict(side="long", kind="limit", price=95.0, sl=96.0), "por debajo del precio de la orden"),
                (dict(side="long", kind="limit", price=95.0, tp=94.0), "por encima del precio de la orden"),
                (dict(side="long", kind="limit", price=float("nan")), "número finito"),
                (dict(side="long", kind="limit", price=95.0, sl=float("inf")), "número finito"),
                (dict(side="long", kind="limit", price=None), "precio de la orden"),
                (dict(side="long", kind="market", price=95.0), "Tipo de orden no válido"),
                (dict(side="long", kind="limit", price=95.0, expiry="bars", expire_bars=0), "velas"),
                (dict(side="long", kind="limit", price=95.0, expiry="bars", expire_bars=2.5), "velas"),
                (dict(side="long", kind="limit", price=95.0, expiry="date", expire_time=T0 - 10), "futuro"),
                (dict(side="long", kind="limit", price=95.0, expiry="date", expire_time=T0 + 400 * 86400), "365"),
                (dict(side="long", kind="limit", price=95.0, expiry="semana"), "Caducidad no válida"),
                (dict(side="long", kind="limit", price=95.0, risk_pct=0), "riesgo"),
            ]
            for kwargs, needle in cases:
                with pytest.raises(pp.PaperError) as exc:
                    await engine.place_order(1, SYM, tf="1h", **kwargs)
                assert needle in str(exc.value), (kwargs, str(exc.value))
            with pytest.raises(pp.PaperError, match="cuenta demo Principal"):
                await engine.place_order(2, SYM, "long", "limit", 95.0)
            with pytest.raises(pp.PaperError, match="No hay precio"):
                await engine.place_order(1, "NASDAQ:NOPRICE", "long", "limit", 95.0)
            assert engine.pending_orders(1) == [] and rt.nexus.gate_calls == []  # nothing reached the gate
            await engine.stop()

    asyncio.run(scenario())


def test_placement_asks_the_gate_at_the_order_price_and_needs_force_for_no_tomar(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, "AVOID")
            with pytest.raises(pp.PaperRejected) as rej:
                await engine.place_order(1, SYM, "long", "limit", 95.0, "1h", sl=93.0)
            assert rej.value.gate["verdict"] == "AVOID" and "a 95,00" in rej.value.message
            assert rt.nexus.gate_calls[-1] == (SYM, "1h", "long", 95.0, 93.0, None)
            res = await engine.place_order(1, SYM, "long", "limit", 95.0, "1h", sl=93.0, force=True)
            order = res["order"]
            assert order["status"] == "pending" and order["forced"] and order["gate_verdict"] == "AVOID"
            assert order["label"] == "Compra límite" and order["expiry"] == "gtc" and order["expire_time"] is None
            assert order["market_price"] == 100.0 and order["distance_pct"] == pytest.approx(-5.0)
            assert order["expiry_text"] == "válida hasta cancelarla"
            row = await rt.db.fetchone("SELECT * FROM paper_orders WHERE id=?", (order["id"],))
            assert row["status"] == "pending" and row["kind"] == "limit" and row["sl"] == 93.0
            infos = [e["message"] for e in rt.bus.recent(level="info")]
            assert any(m.startswith(f"Orden demo #{order['id']}: compra límite {SYM} a 95,00") and "FORZADA" in m
                       for m in infos)
            await engine.stop()

    asyncio.run(scenario())


def test_expiry_in_bars_and_by_date(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            bars = (await engine.place_order(1, SYM, "long", "limit", 95.0, "1h", expiry="bars", expire_bars=3))["order"]
            assert bars["expire_time"] == T0 + 3 * 3600 and bars["expire_bars"] == 3
            assert bars["expiry_text"].startswith("válida 3 velas de 1h (hasta ")
            dated = (await engine.place_order(1, SYM, "short", "stop", 90.0, "4h", expire_time=(T0 + 86400) * 1000))
            assert dated["order"]["expiry"] == "date" and dated["order"]["expire_time"] == T0 + 86400  # ms accepted
            assert await engine._expire_orders(T0 + 3 * 3600 - 1) == 0
            assert await engine._expire_orders(T0 + 3 * 3600) == 1
            assert [o["id"] for o in engine.pending_orders(1)] == [dated["order"]["id"]]
            row = await rt.db.fetchone("SELECT status, reason, cancelled_by FROM paper_orders WHERE id=?", (bars["id"],))
            assert row["status"] == "expired" and row["cancelled_by"] is None
            assert row["reason"] == "Caducada: pasaron 3 velas de 1h sin ejecutarse"
            clock[0] = T0 + 86400
            listed = await engine.list_orders(1, "all")  # the listing expires what is due first
            assert [o["status"] for o in listed] == ["expired", "expired"]
            assert listed[0]["reason"] == "Caducada: llegó su fecha límite sin ejecutarse"
            await engine.stop()

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- fills


def test_limit_fills_when_touched_without_slippage_and_is_managed_like_a_market_position(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            published = []
            rt_ws_publish = rt.ws.publish
            engine = await _engine(rt)
            rt.ws.publish = lambda topic, msg: published.append((topic, msg))
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h"))["order"]
            assert published[-1][1]["orders"][0]["id"] == order["id"]
            clock[0] = T0 + 120
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 99.0, 99.5, 98.4, 98.6), False)  # not reached
            await _settle(engine)
            assert engine.pending_orders(1)[0]["status"] == "pending" and engine.open_positions(1) == []
            await engine._on_candle(SYM, "1m", _bar(T0 + 120, 98.6, 98.7, 97.9, 98.2), False)  # touched 98
            await _settle(engine)
            assert engine.pending_orders(1) == []
            [pos] = engine.open_positions(1)
            # exactly the limit price (no slippage); NEXUS levels from the gate asked at the FILL price (98)
            assert pos["entry_price"] == 98.0 and pos["entry_time"] == T0 + 120
            assert rt.nexus.gate_calls[-1] == (SYM, "1h", "long", 98.0, None, None)
            assert (pos["sl"], pos["tp1"], pos["tp2"], pos["tp3"]) == (96.0, 100.0, 102.0, 104.0)
            assert pos["qty"] == pytest.approx(10000 * 0.01 / 2.0, rel=1e-3) and pos["gate_verdict"] == "TAKE"
            row = await rt.db.fetchone("SELECT * FROM paper_orders WHERE id=?", (order["id"],))
            assert row["status"] == "filled" and row["position_id"] == pos["id"] and row["fill_price"] == 98.0
            assert row["fill_time"] == T0 + 120
            meta = json.loads(row["meta"])
            assert meta["fill_gate"] == "TAKE" and meta["fill_slippage"] is False
            assert await rt.db.fetchval("SELECT COUNT(*) FROM gate_log WHERE taken=1 AND source='paper'") == 1
            trade = [e["message"] for e in rt.bus.recent(level="trade")][-1]
            assert trade.startswith(f"Abierto {SYM} largo") and f"compra límite #{order['id']} a 98,00 ejecutada" in trade
            # managed like any position: TP1 closes a third and moves the stop to break-even, R net of fees
            clock[0] = T0 + 240
            await engine._on_candle(SYM, "1m", _bar(T0 + 180, 98.5, 100.2, 98.4, 100.1), False)
            p = engine.open_positions(1)[0]
            assert p["tp_hits"] == 1 and p["sl"] == 98.0 and p["qty_open"] == pytest.approx(pos["qty"] * 2 / 3)
            listed = await engine.list_orders(1, "all")
            assert listed[0]["status"] == "filled" and listed[0]["status_label"] == "Ejecutada"
            rt.ws.publish = rt_ws_publish
            await engine.stop()

    asyncio.run(scenario())


def test_stop_fills_at_market_with_slippage_and_gaps_at_the_open(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            buy = (await engine.place_order(1, SYM, "long", "stop", 102.0, "1h", sl=99.0))["order"]
            sell = (await engine.place_order(1, ETH, "short", "stop", 190.0, "1h"))["order"]
            clock[0] = T0 + 120
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 103.0, 104.0, 102.5, 103.5), False)  # gap over 102
            await engine._on_candle(ETH, "1m", _bar(T0 + 60, 195.0, 195.5, 189.5, 190.5), False)  # crosses 190
            await _settle(engine)
            by_sym = {p["symbol"]: p for p in engine.open_positions(1)}
            assert by_sym[SYM]["entry_price"] == pytest.approx(103.0 * 1.0002)  # the open, plus slippage
            assert by_sym[SYM]["sl"] == 99.0  # the user's stop, kept as given
            assert by_sym[ETH]["entry_price"] == pytest.approx(190.0 * 0.9998)  # short: sells lower
            assert by_sym[ETH]["sl"] == pytest.approx(192.0) and by_sym[ETH]["tp1"] == pytest.approx(188.0)
            rows = await rt.db.fetchall("SELECT id, status FROM paper_orders ORDER BY id")
            assert [(r["id"], r["status"]) for r in rows] == [(buy["id"], "filled"), (sell["id"], "filled")]
            await engine.stop()

    asyncio.run(scenario())


def test_bars_older_than_the_order_count_by_their_close_and_replayed_bars_never_fill(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h"))["order"]
            # the forming 1m bar began 30 s before the order: its low (97.5) may predate it -> close only (99)
            await engine._on_candle(SYM, "1m", _bar(T0 - 30, 99.0, 99.2, 97.5, 99.0), False)
            await _settle(engine)
            assert engine.pending_orders(1)[0]["id"] == order["id"]
            # the feed stalls; 15 min later the hub replays the missed final bars (closed=True, not live)
            clock[0] = T0 + 900
            await engine._on_candle(SYM, "1m", _bar(T0 - 60, 99.0, 99.0, 97.0, 98.9), True)  # ended before: ignored
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 99.0, 99.1, 97.8, 98.5), True)  # touched meanwhile
            await _settle(engine)
            # a replayed bar is not a live price: no fill dated in the past with a gate asked now; one notice
            assert engine.open_positions(1) == [] and engine.pending_orders(1)[0]["status"] == "pending"
            assert rt.nexus.gate_calls[-1][3] == 98.0 and len(rt.nexus.gate_calls) == 1  # only at placement
            notes = [e["message"] for e in rt.bus.recent(level="info") if "no llegó en vivo" in e["message"]]
            assert len(notes) == 1 and notes[0].startswith(f"Orden demo #{order['id']} (compra límite {SYM} a 98,00)")
            assert engine._prices[SYM][0] == 99.0  # the last LIVE price: a replayed bar never refreshes it
            # live again: the forming bar began before the feed came back -> close only (98,6: not reached)
            await engine._on_candle(SYM, "1m", _bar(T0 + 840, 98.5, 98.7, 97.9, 98.6), False)
            await _settle(engine)
            assert engine.open_positions(1) == []
            # a bar that begins after the feed came back counts with its high / low
            clock[0] = T0 + 960
            await engine._on_candle(SYM, "1m", _bar(T0 + 960, 98.6, 98.7, 97.95, 98.3), False)
            await _settle(engine)
            [pos] = engine.open_positions(1)
            assert pos["entry_price"] == 98.0 and pos["entry_time"] == T0 + 960
            await engine.stop()

    asyncio.run(scenario())


def test_polled_live_quotes_trigger_orders_by_close(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.place_order(1, SYM, "short", "limit", 103.0, "1h")
            engine._prices.clear()  # no live candle: the monitor polls the hub quotes
            rt.hub.prices[SYM] = 104.0
            await engine._poll_stale_prices()
            await _settle(engine)
            [pos] = engine.open_positions(1)
            assert pos["side"] == "short" and pos["entry_price"] == 103.0  # never better than the limit by close
            await engine.stop()

    asyncio.run(scenario())


def test_no_tomar_at_the_fill_cancels_the_order_with_the_reason(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h"))["order"]
            rt.nexus.verdict = "AVOID"  # conditions changed by the time the price comes down
            clock[0] = T0 + 120
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 98.5, 98.6, 97.9, 98.1), False)
            await _settle(engine)
            assert engine.open_positions(1) == [] and engine.pending_orders(1) == []
            row = await rt.db.fetchone("SELECT status, reason, cancelled_by, position_id FROM paper_orders WHERE id=?",
                                       (order["id"],))
            assert row["status"] == "cancelled" and row["cancelled_by"] == "gate" and row["position_id"] is None
            assert row["reason"].startswith("Cancelada al ejecutarse a 98,00: el filtro dice NO TOMAR 31/100")
            warns = [e["message"] for e in rt.bus.recent(level="warn")]
            assert any(f"Orden demo #{order['id']} cancelada" in m and "NO TOMAR" in m for m in warns)
            assert await rt.db.fetchval("SELECT COUNT(*) FROM paper_positions") == 0
            await engine.stop()

    asyncio.run(scenario())


def test_a_gap_through_the_users_stop_cancels_instead_of_opening_a_broken_trade(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h", sl=97.0))["order"]
            clock[0] = T0 + 120
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 96.5, 96.8, 96.0, 96.4), False)  # opens below the stop
            await _settle(engine)
            assert engine.open_positions(1) == []
            row = await rt.db.fetchone("SELECT status, reason, cancelled_by FROM paper_orders WHERE id=?", (order["id"],))
            assert row["status"] == "cancelled" and row["cancelled_by"] == "levels"
            assert row["reason"].startswith("No se ejecutó al llegar a 96,50: El stop (97,00) debe estar por debajo")
            await engine.stop()

    asyncio.run(scenario())


class SlowNexus(FakeNexus):
    """The gate answers only when `release` is set (a fill whose decision is still pending)."""

    def __init__(self, verdict: str = "TAKE"):
        super().__init__(verdict)
        self.release = asyncio.Event()

    async def gate(self, *args, **kwargs):
        await self.release.wait()
        return await super().gate(*args, **kwargs)


def test_cancelling_while_the_fill_is_decided_wins_and_triggers_are_not_repeated(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h"))["order"]
            rt.nexus = SlowNexus()
            clock[0] = T0 + 120
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 98.5, 98.6, 97.9, 98.1), False)
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 98.5, 98.6, 97.7, 97.8), False)  # same bar resent
            await asyncio.sleep(0)
            assert len(engine._tasks) == 1 and engine.pending_orders(1)[0]["executing"] is True
            assert await engine._expire_orders(T0 + 10**6) == 0  # never expired while its fill is decided
            cancelled = await engine.cancel_orders(order["id"])
            assert cancelled[0]["status"] == "cancelled" and cancelled[0]["reason"] == "Cancelada por el usuario"
            rt.nexus.release.set()
            await _settle(engine)
            assert engine.open_positions(1) == [] and await rt.db.fetchval("SELECT COUNT(*) FROM paper_positions") == 0
            row = await rt.db.fetchone("SELECT status, cancelled_by FROM paper_orders WHERE id=?", (order["id"],))
            assert (row["status"], row["cancelled_by"]) == ("cancelled", "user")
            with pytest.raises(pp.PaperError, match="No hay una orden pendiente"):
                await engine.cancel_orders(order["id"])
            with pytest.raises(pp.PaperError, match="No hay órdenes pendientes"):
                await engine.cancel_orders(symbol=SYM)
            with pytest.raises(pp.PaperError, match="Indica qué cancelar"):
                await engine.cancel_orders()
            await engine.stop()

    asyncio.run(scenario())


def test_orders_survive_a_restart_judged_from_the_restart_and_reset_deletes_them(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            keep = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h"))["order"]
            gone = (await engine.place_order(1, SYM, "long", "limit", 97.0, "1h", expiry="bars", expire_bars=1))["order"]
            await engine.stop()
            clock[0] = T0 + 7200  # NEXUS was stopped for 2 h: the 1-bar order expired meanwhile
            engine2 = pp.PaperEngine(rt)
            rt.paper = engine2
            await engine2.start()
            assert [o["id"] for o in engine2.pending_orders(1)] == [keep["id"]]
            assert engine2._watch_from[keep["id"]] == T0 + 7200
            row = await rt.db.fetchone("SELECT status FROM paper_orders WHERE id=?", (gone["id"],))
            assert row["status"] == "expired"
            # the first bar after the restart began before it: judged by its close only
            await engine2._on_candle(SYM, "1m", _bar(T0 + 7200 - 20, 98.4, 98.5, 97.5, 98.3), False)
            await _settle(engine2)
            assert engine2.open_positions(1) == []
            snap = await engine2.reset(5000)
            assert snap["positions"] == [] and engine2.pending_orders(1) == []
            assert await rt.db.fetchval("SELECT COUNT(*) FROM paper_orders") == 0
            await engine2.stop()

    asyncio.run(scenario())


def test_subscriptions_follow_pending_orders(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            monkeypatch.setattr(nexus_config, "BACKGROUND", True)
            monkeypatch.setattr(nexus_config, "OFFLINE", True)
            rt.hub = FakeHub(prices={SYM: 100.0, "SIM:NEXUS": 50.0})
            rt.nexus = FakeNexus("TAKE")
            engine = pp.PaperEngine(rt)
            await engine.start()
            o1 = (await engine.place_order(1, SYM, "long", "limit", 95.0, "1h"))["order"]
            await engine.place_order(1, "SIM:NEXUS", "long", "stop", 55.0, "4h")
            assert set(rt.hub.subs) == {(SYM, "1m", "paper"), ("SIM:NEXUS", "4h", "paper")}
            await engine.cancel_orders(o1["id"])
            assert rt.hub.subs == [("SIM:NEXUS", "4h", "paper")]
            await engine.cancel_orders(all=True)
            assert rt.hub.subs == []
            await engine.stop()

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- REST API


@pytest.fixture
def client(api_client):
    from app.runtime import rt

    rt.hub = FakeHub(prices={SYM: 100.0, ETH: 200.0})
    rt.nexus = FakeNexus("TAKE")
    return api_client


def test_pending_order_endpoints(client):
    from app.runtime import rt

    r = client.post("/api/paper/orders", json={"symbol": "btc", "side": "long", "kind": "limit", "price": 95,
                                                "tf": "1h", "sl": 93, "expiry": "bars", "expire_bars": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"order", "gate"} and body["gate"]["verdict"] == "TAKE"
    order = body["order"]
    assert order["symbol"] == SYM and order["kind"] == "limit" and order["price"] == 95 and order["expire_bars"] == 4
    r = client.post("/api/paper/orders", json={"symbol": SYM, "side": "long", "kind": "limit", "price": 101})
    assert r.status_code == 400 and "compra stop" in r.json()["detail"]
    raw = b'{"symbol": "BINANCE:BTCUSDT", "side": "long", "kind": "limit", "price": NaN}'
    r = client.post("/api/paper/orders", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "finito" in r.json()["detail"]
    assert client.post("/api/paper/orders", json={"symbol": SYM, "kind": "limit", "price": 95}).status_code == 400
    assert client.post("/api/paper/orders", json={"symbol": SYM, "side": "long", "price": 95}).status_code == 400
    assert client.post("/api/paper/orders", json={"symbol": "zzz", "side": "long", "kind": "limit",
                                                  "price": 95}).status_code == 404
    rt.nexus.verdict = "AVOID"
    r = client.post("/api/paper/orders", json={"symbol": SYM, "side": "short", "kind": "limit", "price": 104})
    assert r.status_code == 409 and r.json()["detail"]["gate"]["verdict"] == "AVOID"
    r = client.post("/api/paper/orders", json={"symbol": SYM, "side": "short", "kind": "limit", "price": 104,
                                                "force": True})
    assert r.status_code == 200 and r.json()["order"]["forced"] is True

    listed = client.get("/api/paper/orders").json()["orders"]
    assert [o["kind"] for o in listed] == ["limit", "limit"] and all(o["status"] == "pending" for o in listed)
    assert client.get("/api/paper/orders", params={"status": "nope"}).status_code == 400
    r = client.post("/api/paper/orders/cancel", json={"order_id": order["id"]})
    assert r.status_code == 200 and [o["id"] for o in r.json()["cancelled"]] == [order["id"]]
    assert client.post("/api/paper/orders/cancel", json={"order_id": 999}).status_code == 404
    assert client.post("/api/paper/orders/cancel", json={}).status_code == 400
    everything = client.get("/api/paper/orders", params={"status": "all"}).json()["orders"]
    assert [o["status"] for o in everything] == ["pending", "cancelled"]
    r = client.post("/api/paper/orders/cancel", json={"all": True})
    assert r.status_code == 200 and len(r.json()["cancelled"]) == 1
    assert client.get("/api/paper/orders").json()["orders"] == []
    # the account snapshot keeps its contract (orders and risk have their own endpoints)
    assert set(client.get("/api/paper/account").json()) == {"account", "positions", "history"}
