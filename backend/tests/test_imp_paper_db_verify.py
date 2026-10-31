"""paper-db · verifier regressions: pending orders and portfolio risk of the demo account.

- A final bar replayed after a feed stall never fills a pending entry: the fill would be dated in the past with the gate
  asked now (later prices: lookahead), and the new position would miss the rest of the replay (e.g. a stop crossed by
  the next replayed bar, so a trade that lost 1 R showed a gain). The order stays pending with one notice; bars that
  began before the feed came back count by close only. A short stall whose replayed bar is still live fills normally.
- Spanish messages with a count of 1 (group limit, validity in bars, expiry reason) use the singular.
- A pending order cancelled by a risk rule at its trigger (daily loss limit, full correlated group) leaves the page
  (WS 'paper' published) and the live feed (subscription dropped) like every other refusal; a full group refuses it
  before asking the gate; the daily block says that orders reaching their price meanwhile are cancelled.
- Cancelling an order whose fill is being booked never reports a cancellation that did not happen.
Runs on PostgreSQL too with NEXUS_TEST_PG_URL (runtime_factory).
"""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from tests.test_sim_fakes import FakeHub, FakeNexus

SYM = "BINANCE:BTCUSDT"
ETH = "BINANCE:ETHUSDT"
T0 = 1_800_000_000  # aligned to the minute


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


class SlowNexus(FakeNexus):
    """The gate answers only once `release` is set (a real gate takes seconds: the replay goes on meanwhile)."""

    def __init__(self, verdict: str = "TAKE"):
        super().__init__(verdict)
        self.release = asyncio.Event()

    async def gate(self, *args, **kwargs):
        await self.release.wait()
        return await super().gate(*args, **kwargs)


async def _engine(rt, **settings) -> pp.PaperEngine:
    await rt.settings.update({"risk.fee_pct": 0.1, "risk.slippage_pct": 0.02, **settings})
    rt.hub = FakeHub(prices={SYM: 100.0, ETH: 200.0})
    rt.nexus = FakeNexus("TAKE")
    engine = pp.PaperEngine(rt)
    rt.paper = engine
    await engine.start()
    return engine


async def _settle(engine: pp.PaperEngine) -> None:
    while engine._tasks:
        await asyncio.gather(*list(engine._tasks))


def test_a_replayed_bar_never_fills_a_pending_order_so_a_stop_crossed_later_in_the_replay_is_not_missed(
        runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h", sl=96.0))["order"]
            rt.nexus = SlowNexus()
            clock[0] = T0 + 900  # the feed stalled 15 min: the hub replays the missed final bars, oldest first
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 99.0, 99.0, 97.8, 98.5), True)  # reaches the limit
            await engine._on_candle(SYM, "1m", _bar(T0 + 120, 98.5, 98.6, 95.0, 97.0), True)  # through the SL (96)
            await engine._on_candle(SYM, "1m", _bar(T0 + 180, 97.0, 99.5, 97.0, 99.4), True)  # recovers
            rt.nexus.release.set()
            await _settle(engine)
            # before the fix: a position at 98 dated T0+120 (gate asked at T0+900) that never saw the 95 low, open in
            # profit although a real resting order would have been stopped out (-1 R)
            assert engine.open_positions(1) == [] and await rt.db.fetchval("SELECT COUNT(*) FROM paper_positions") == 0
            assert len(rt.nexus.gate_calls) == 0  # no decision taken with prices that came after the fill
            [pending] = engine.pending_orders(1)
            assert pending["id"] == order["id"] and pending["status"] == "pending" and not pending["executing"]
            notes = [e["message"] for e in rt.bus.recent(level="info") if "no llegó en vivo" in e["message"]]
            assert len(notes) == 1  # one notice per order, not one per replayed bar
            assert notes[0].startswith(f"Orden demo #{order['id']} (compra límite {SYM} a 98,00): el precio llegó")
            assert "sigue pendiente" in notes[0] and "solo se ejecutan con precios en vivo" in notes[0]
            assert engine._watch_from[order["id"]] == T0 + 900  # judged from the moment the feed came back
            # live again: the forming bar began before the feed came back -> its close only (99,5: not reached)
            await engine._on_candle(SYM, "1m", _bar(T0 + 840, 99.4, 99.6, 97.5, 99.5), False)
            await _settle(engine)
            assert engine.open_positions(1) == []
            await engine.stop()

    asyncio.run(scenario())


def test_a_short_stall_whose_replayed_bar_is_still_live_fills_now(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            await engine.place_order(1, SYM, "long", "limit", 98.0, "1h")
            clock[0] = T0 + 150  # the bar T0+60 ended 30 s ago: still a live price (QUOTE_STALE_S)
            await engine._on_candle(SYM, "1m", _bar(T0 + 60, 99.0, 99.0, 97.9, 98.4), True)
            await _settle(engine)
            [pos] = engine.open_positions(1)
            assert pos["entry_price"] == 98.0 and pos["entry_time"] == T0 + 150  # filled now, at the limit
            assert not [e for e in rt.bus.recent(level="info") if "no llegó en vivo" in e["message"]]
            await engine.stop()

    asyncio.run(scenario())


def test_delayed_bars_stamped_in_market_time_never_fill_a_pending_order(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            rt.hub.prices["NASDAQ:AAPL"] = 100.0
            order = (await engine.place_order(1, "NASDAQ:AAPL", "long", "limit", 98.0, "1h"))["order"]
            clock[0] = T0 + 1800  # a 15-min delayed feed: its final 1m bars are stamped 15 min in the past
            for t, low in ((T0 + 840, 97.5), (T0 + 900, 97.0)):
                await engine._on_candle("NASDAQ:AAPL", "1m", _bar(t, 99.0, 99.2, low, 98.5), True)
            await engine._on_candle("NASDAQ:AAPL", "1m", _bar(T0 + 960, 98.5, 98.6, 96.0, 96.5), False)  # forming
            await _settle(engine)
            assert engine.open_positions(1) == [] and [o["id"] for o in engine.pending_orders(1)] == [order["id"]]
            assert len(rt.nexus.gate_calls) == 1  # only at placement: no decision about a fill 15 min in the past
            notes = [e["message"] for e in rt.bus.recent(level="info") if "no llegó en vivo" in e["message"]]
            assert len(notes) == 1 and "datos retrasados" in notes[0]
            await engine.stop()

    asyncio.run(scenario())


def test_the_baseline_schema_files_stay_frozen():
    """Version 1 is schema.sql / schema_pg.sql as they were when versioning started. An existing database is only
    marked as baseline (its DDL never runs again), so any DDL edit there would silently diverge between fresh and
    upgraded databases: schema changes go into a new numbered migration (app/migrations). Comments may change."""
    import hashlib
    import re

    from app.db import SCHEMA_FILE
    from app.db_pg import SCHEMA_FILE as PG_SCHEMA_FILE

    def ddl_hash(path) -> str:
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()

    frozen = {
        SCHEMA_FILE.name: "7adfd2a1ef181aabbdc425c3f270741a824296bcd7ce1b48899b46e59d409d5e",
        PG_SCHEMA_FILE.name: "9d8d1d15fb05ae173c9e43a2b722bd502c75ba1715071328fbe745eb7cceb3bb",
    }
    for path in (SCHEMA_FILE, PG_SCHEMA_FILE):
        assert ddl_hash(path) == frozen[path.name], (
            f"{path.name} es el esquema base congelado (versión 1): no cambies su DDL; crea una migración numerada "
            "en app/migrations (NNNN_nombre.sqlite.sql + NNNN_nombre.pg.sql)")


def test_messages_with_a_count_of_one_use_the_singular(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, **{"paper.max_positions_per_group": 1})
            one = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h", expiry="bars", expire_bars=1))["order"]
            assert one["expiry_text"].startswith("válida 1 vela de 1h (hasta ")
            assert await engine._expire_orders(T0 + 3600) == 1
            row = await rt.db.fetchone("SELECT reason FROM paper_orders WHERE id=?", (one["id"],))
            assert row["reason"] == "Caducada: pasó 1 vela de 1h sin ejecutarse"
            three = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h", expiry="bars", expire_bars=3))["order"]
            assert three["expiry_text"].startswith("válida 3 velas de 1h (hasta ")
            await engine.open_position(1, SYM, "long", "1h")
            with pytest.raises(pp.PaperRiskBlocked) as blk:
                await engine.open_position(1, ETH, "long", "1h")
            assert "ya hay 1 abierta en Cripto (máximo 1)" in blk.value.message
            await engine.stop()

    asyncio.run(scenario())


def test_an_order_cancelled_by_the_daily_limit_at_its_trigger_leaves_the_page_and_the_live_feed(
        runtime_factory, clock, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            monkeypatch.setattr(nexus_config, "BACKGROUND", True)  # live subscriptions follow orders and positions
            engine = await _engine(rt, **{"paper.daily_loss_limit_pct": 3.0, "paper.max_open_risk_pct": 60.0})
            engine._loop_task.cancel()  # no monitor: only the publishes caused by the order itself
            published: list[tuple[str, dict]] = []
            monkeypatch.setattr(rt.ws, "publish", lambda topic, msg: published.append((topic, msg)))
            order = (await engine.place_order(1, ETH, "long", "limit", 196.0, "1h"))["order"]
            pos = (await engine.open_position(1, SYM, "long", "1h", qty=100.0, sl=95.0))["position"]
            await engine.close_position(pos["id"], price=96.5)  # about -3.7 % realized: the day is blocked
            assert (await engine.risk_state(1))["blocked"] and rt.hub.subs == [(ETH, "1m", "paper")]
            gates, sent = len(rt.nexus.gate_calls), len(published)
            clock[0] = T0 + 120
            await engine._on_candle(ETH, "1m", _bar(T0 + 60, 199.0, 199.5, 195.9, 196.4), False)  # reaches 196
            await _settle(engine)
            row = await rt.db.fetchone("SELECT status, cancelled_by, reason FROM paper_orders WHERE id=?", (order["id"],))
            assert (row["status"], row["cancelled_by"]) == ("cancelled", "risk")
            assert row["reason"].startswith("No se ejecutó al llegar a 196,00: Límite de pérdida diaria alcanzado")
            assert "ni ejecuta órdenes pendientes: las que lleguen a su precio antes se cancelan" in row["reason"]
            assert len(rt.nexus.gate_calls) == gates  # refused before asking the gate
            # before the fix: no 'paper' message (the page kept the order as pending) and ETH stayed subscribed
            after = [m for topic, m in published[sent:] if topic == "paper"]
            assert after and after[-1]["orders"] == [] and after[-1]["risk"]["pending_orders"] == 0
            assert rt.hub.subs == []
            await engine.stop()

    asyncio.run(scenario())


def test_a_full_correlated_group_at_the_trigger_cancels_the_order_without_asking_the_gate(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, **{"paper.max_positions_per_group": 1})
            order = (await engine.place_order(1, ETH, "long", "limit", 196.0, "1h"))["order"]  # the group is empty
            await engine.open_position(1, SYM, "long", "1h")  # now crypto is full (1 of 1)
            gates = len(rt.nexus.gate_calls)
            clock[0] = T0 + 120
            await engine._on_candle(ETH, "1m", _bar(T0 + 60, 199.0, 199.5, 195.9, 196.4), False)
            await _settle(engine)
            assert len(rt.nexus.gate_calls) == gates and [p["symbol"] for p in engine.open_positions(1)] == [SYM]
            row = await rt.db.fetchone("SELECT status, cancelled_by, reason FROM paper_orders WHERE id=?", (order["id"],))
            assert (row["status"], row["cancelled_by"]) == ("cancelled", "risk")
            assert row["reason"].startswith("No se ejecutó al llegar a 196,00: Límite de posiciones por grupo "
                                            "correlacionado: ya hay 1 abierta en Cripto (máximo 1)")
            await engine.stop()

    asyncio.run(scenario())


def test_cancelling_an_order_whose_fill_is_being_booked_never_reports_a_cancellation(runtime_factory, clock):
    from app.api.sim import _paper_error

    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h"))["order"]
            async with engine._lock:  # the fill holds the engine lock while it books the position
                task = asyncio.create_task(engine.cancel_orders(order["id"]))
                await asyncio.sleep(0.05)
                assert not task.done()
                engine._orders.pop(order["id"])  # booked: no longer pending (as open_position does under the lock)
                engine._watch_from.pop(order["id"], None)
            with pytest.raises(pp.PaperError) as err:  # before the fix: [] -> the page said "cancelada"
                await task
            assert str(err.value) == f"No hay una orden pendiente con id {order['id']}: se acaba de ejecutar, cancelar o caducar"
            assert _paper_error(err.value).status_code == 404
            assert not [e for e in rt.bus.recent() if f"Orden demo #{order['id']} cancelada" in e["message"]]
            await engine.stop()

    asyncio.run(scenario())
