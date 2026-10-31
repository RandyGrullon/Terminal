"""paper-db · a pending order whose fill cannot get the filter's verdict is cancelled, never opened blind.

The user's decision: the filter is asked again when a limit / stop order reaches its price; when it cannot answer
(engine not loaded, error, timeout) the order is cancelled with the reason in sight (cancelled_by 'gate'), like a
NO TOMAR. Market orders keep their own behaviour. Runs on PostgreSQL too with NEXUS_TEST_PG_URL.
"""
from __future__ import annotations

import asyncio

from tests.test_imp_paper_db_orders import SYM, T0, _bar, _engine, _settle, clock  # noqa: F401 (fixture)
from tests.test_sim_fakes import FakeNexus


class FailingNexus(FakeNexus):
    """Answers the gate at placement, then fails at the fill (data error inside the engine)."""

    def __init__(self) -> None:
        super().__init__("TAKE")
        self.fail = False

    async def gate(self, symbol, tf, direction, entry=None, sl=None, tp=None):
        if self.fail:
            raise RuntimeError("sin velas para el filtro")
        return await super().gate(symbol, tf, direction, entry=entry, sl=sl, tp=tp)


async def _trigger(engine, clock) -> None:
    clock[0] = T0 + 120
    await engine._on_candle(SYM, "1m", _bar(T0 + 60, 98.5, 98.6, 97.9, 98.1), False)  # touches the 98 limit
    await _settle(engine)


async def _order_row(rt, oid: int):
    return await rt.db.fetchone("SELECT status, reason, cancelled_by, position_id FROM paper_orders WHERE id=?", (oid,))


def test_no_engine_at_the_fill_cancels_the_pending_order(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h"))["order"]
            rt.nexus = None  # the engine went away before the price came down
            await _trigger(engine, clock)
            assert engine.open_positions(1) == [] and engine.pending_orders(1) == []
            row = await _order_row(rt, order["id"])
            assert row["status"] == "cancelled" and row["cancelled_by"] == "gate" and row["position_id"] is None
            assert row["reason"] == ("Cancelada al llegar a 98,00: no se pudo consultar el filtro (el motor NEXUS no "
                                     "está cargado); una orden pendiente nunca se abre sin su veredicto")
            warns = [e["message"] for e in rt.bus.recent(level="warn")]
            assert any(f"Orden demo #{order['id']} cancelada" in m and "no se pudo consultar el filtro" in m
                       for m in warns)
            assert await rt.db.fetchval("SELECT COUNT(*) FROM paper_positions") == 0
            await engine.stop()

    asyncio.run(scenario())


def test_a_filter_error_at_the_fill_cancels_the_pending_order_with_the_cause(runtime_factory, clock):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)
            rt.nexus = FailingNexus()
            order = (await engine.place_order(1, SYM, "long", "limit", 98.0, "1h"))["order"]
            rt.nexus.fail = True
            await _trigger(engine, clock)
            assert engine.open_positions(1) == [] and engine.pending_orders(1) == []
            row = await _order_row(rt, order["id"])
            assert row["status"] == "cancelled" and row["cancelled_by"] == "gate"
            assert "no se pudo consultar el filtro (error al evaluarlo: sin velas para el filtro)" in row["reason"]
            await engine.stop()

    asyncio.run(scenario())


def test_a_filter_timeout_reason_is_spanish(runtime_factory, clock, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt)

            async def no_answer(*_args, **_kwargs):
                raise asyncio.TimeoutError

            monkeypatch.setattr(rt.nexus, "gate", no_answer)
            gate, why = await engine._gate_eval(SYM, "1h", "long", entry=98.0, sl=None, tp=None, origin="manual")
            assert gate is None and why == "no respondió en 90 s"
            await engine.stop()

    asyncio.run(scenario())
