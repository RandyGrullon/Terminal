"""Regression tests (engine fixes): NEXUS never runs on a stale candle set (venue stopped serving the pair, feed
down): ok=false with the Spanish note, no signal / levels / gate verdict / watcher rows, and a quick read taken
from those same old candles says so (stale-delisted-pairs-served-live, contract I2)."""
from __future__ import annotations

import asyncio

import pytest

from app.engine import service as service_mod
from app.engine.service import NexusDataError, NexusService, age_text
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END
from tests.test_fix_engine_watch import SeriesHub, WSRecorder

SYM = "HTX:BOBAUSDT"
NOTE = ("No se pudo actualizar (OKX: HTTP 404); se muestran los datos guardados. "
        "Datos desactualizados: última vela hace 22 días.")


class FakeScanner:
    def __init__(self, quick: dict | None) -> None:
        self.quick = quick

    async def quick_signal(self, symbol: str, tf: str = "1h") -> dict | None:
        return dict(self.quick) if self.quick else None


def stale_set(note: str | None):
    cs = generate_candleset(n=600, seed=4, tf="1h", end_time=FIXED_END, symbol=SYM)
    return cs.with_meta(stale=True, note=note, provider="kucoin")


def quick_at(price: float) -> dict:
    return {"symbol": SYM, "tf": "1h", "score": 0.3, "prob_up": 0.65, "action": "BUY", "confidence": 0.4,
            "rating": None, "price": price, "reasons": ["Lectura local con 300 velas 1h"], "model_updates": 0,
            "time": FIXED_END}


def test_stale_candles_never_reach_the_engine(runtime_factory):
    cs = stale_set(NOTE)
    last = float(cs.close[-1])

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub, rt.ws, rt.scanner = SeriesHub(cs), WSRecorder(), FakeScanner(quick_at(last))
            svc = rt.nexus = NexusService(rt)
            resp = await svc.analyze(SYM, "1h")
            assert resp["ok"] is False and resp["current"] is None and resp["markers"] == []
            assert resp["message"].startswith("Datos desactualizados: última vela hace 22 días. NEXUS no analiza")
            assert resp["note"] == NOTE and resp["stats"]["trades"] == 0 and svc._analyses == 0
            quick = resp["quick"]  # computed from the same old candles: flagged as such
            assert quick["stale"] is True
            assert quick["reasons"][0] == ("Datos desactualizados: última vela hace 22 días: "
                                           "esta lectura usa esas velas antiguas")
            assert not await rt.db.fetchone("SELECT key FROM model_state WHERE kind='nexus'")
            with pytest.raises(NexusDataError, match="desactualizados"):
                await svc.gate(SYM, "1h", "short")
            assert await svc.current(SYM, "1h") is None
            assert await svc.process_closed_bar(SYM, "1h", upto=int(cs.time[-1])) is None
            for table in ("signals", "predictions", "gate_log"):
                assert not await rt.db.fetchall(f"SELECT * FROM {table}")
            # a quick read from another source (a different, current price) is left as it is
            rt.scanner = FakeScanner(quick_at(last * 0.86))
            svc._cache.clear()
            other = (await svc.analyze(SYM, "1h"))["quick"]
            assert "stale" not in other and other["reasons"] == ["Lectura local con 300 velas 1h"]

    asyncio.run(scenario())


def test_stale_message_without_a_hub_note_gives_the_age(runtime_factory, monkeypatch):
    cs = stale_set(None)
    monkeypatch.setattr(service_mod, "now_s", lambda: int(cs.time[-1]) + 3 * 86400 + 600)

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub, rt.ws = SeriesHub(cs), WSRecorder()
            resp = await NexusService(rt).analyze(SYM, "1h")
            assert resp["ok"] is False and resp["quick"] is None
            assert resp["message"].startswith("Datos desactualizados: última vela hace 3 días.")

    asyncio.run(scenario())
    assert [age_text(s) for s in (90, 3600, 5 * 3600, 86400, 9 * 86400)] == ["1 min", "1 hora", "5 horas", "1 día",
                                                                          "9 días"]
