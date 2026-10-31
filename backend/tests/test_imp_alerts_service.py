"""AlertService with fake channels (nothing is sent): bus listener -> one batched message, the user's filters
(categories, minimum level, watched symbols), dedupe, quiet hours, hourly limit with critical bypass and the
"omitted" notes, failures, persistence across restarts, the test message and the delayed-data label."""
from __future__ import annotations

import asyncio

import pytest

from app import config
from app.alerts.classify import classify
from app.alerts.service import AlertBusy, AlertService
from tests.test_imp_alerts_fakes import (Clock, FakeChannel, early_exit_event, macro_event, paper_close_event,
                                         paper_open_event, signal_event, utc)

WATCHED = [{"symbol": "BINANCE:BTCUSDT", "tf": "1h"}, {"symbol": "BINANCE:ETHUSDT", "tf": "1h"},
           {"symbol": "NASDAQ:AAPL", "tf": "1h"}]
NOON_NY = utc(2026, 7, 1, 16, 0)  # 12:00 in New York (outside 23:00–07:00)
NIGHT_NY = utc(2026, 7, 1, 5, 30)  # 01:30 in New York


class StubNexus:
    def __init__(self, watching=None):
        self._watching = list(WATCHED if watching is None else watching)

    def watching(self):
        return list(self._watching)


class StubHub:
    def __init__(self, delays=None):
        self.delays = dict(delays or {})
        self.quote_calls: list[list[str]] = []

    async def quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        return {s: {"symbol": s, "price": 1.0, **({"delay_s": self.delays[s]} if s in self.delays else {})}
                for s in symbols}

    async def watchlist(self):
        return [w["symbol"] for w in WATCHED]


async def until(pred, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("timeout waiting for the alert worker")
        await asyncio.sleep(0.01)


async def make(rt, *channels, now: float = NOON_NY, settings: dict | None = None):
    await rt.settings.update({"ui.timezone": "America/New_York", **(settings or {})})
    rt.nexus = rt.nexus or StubNexus()
    svc = AlertService(rt, clock=Clock(now), channels=list(channels) or [FakeChannel()])
    svc.batch_window_s = 0.05
    await svc.start()
    return svc


def alerts(*events):
    return [classify(e) for e in events]


def test_bus_events_reach_the_channel_in_one_message(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            tg = FakeChannel()
            svc = await make(rt, tg)
            rt.bus.emit(**{k: macro_event("red")[k] for k in ("level", "source", "message", "data")})
            e = signal_event()
            rt.bus.emit(e["level"], e["source"], e["message"], e["data"])
            e = paper_open_event()
            rt.bus.emit(e["level"], e["source"], e["message"], e["data"])
            rt.bus.emit("info", "nexus", "algo que no es un aviso", {"symbol": "BINANCE:BTCUSDT"})
            await until(lambda: svc.counters["sent"] == 3)
            assert len(tg.sent) == 1
            text, subject = tg.sent[0]
            lines = text.splitlines()
            assert lines[0] == "NEXUS · 3 avisos" and lines[1].startswith("🔴 Decisión de tipos de la Fed")
            assert any(line.startswith("📈 BINANCE:BTCUSDT 1h → COMPRAR") for line in lines)
            assert any("Cuenta demo (simulado): Abierto" in line for line in lines)
            assert subject.startswith("NEXUS · 3 avisos: 🔴")
            st = svc.full_status()
            assert st["counters"]["received"] == 3 and st["sent_last_hour"] == 1 and st["active_channels"] == 1
            assert [r["status"] for r in st["recent"]] == ["sent"] * 3 and st["channels"][0]["last"]["ok"]
            assert svc.status() == {"running": True, "channels": "Telegram", "sent": 3, "failed": 0, "queued": 0}
            await svc.stop()
            assert svc._on_event not in rt.bus._listeners  # detached from the bus
            e = signal_event("BINANCE:ETHUSDT")
            rt.bus.emit(e["level"], e["source"], e["message"], e["data"])
            await asyncio.sleep(0.1)
            assert len(tg.sent) == 1 and svc.counters["received"] == 3

    asyncio.run(scenario())


def test_filters_categories_level_and_watched_symbols(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            tg = FakeChannel()
            svc = await make(rt, tg, settings={"alerts.paper": False})
            assert await svc.process(alerts(paper_open_event())) is None  # category off
            assert await svc.process(alerts(signal_event("BINANCE:SOLUSDT"))) is None  # not in the watchlist
            assert await svc.process(alerts(signal_event("BINANCE:BTCUSDT", tf="15m"))) is None  # chart, not watch.tf
            assert svc.counters["filtered"] == 3 and tg.sent == []
            await rt.settings.update({"alerts.min_level": "critical"})
            assert await svc.process(alerts(signal_event())) is None  # entries are 'warn'
            done = await svc.process(alerts(early_exit_event()))
            assert done["status"] == "sent" and done["priority"] == "critical" and len(tg.sent) == 1
            await rt.settings.update({"alerts.min_level": "warn", "alerts.signal_symbols": ["BINANCE:ETHUSDT"]})
            assert await svc.process(alerts(signal_event("BINANCE:BTCUSDT"))) is None
            assert (await svc.process(alerts(signal_event("BINANCE:ETHUSDT", action="SELL"))))["status"] == "sent"
            await rt.settings.update({"alerts.signal_symbols": [], "watch.enabled": False})
            assert await svc.process(alerts(signal_event("BINANCE:BTCUSDT", action="EXIT_LONG"))) is None
            assert any("vigilancia NEXUS está desactivada" in w for w in svc.full_status()["warnings"])
            rt.nexus = StubNexus(watching=[])  # watcher not running: falls back to the watchlist at watch.tf
            rt.hub = StubHub()
            await rt.settings.update({"watch.enabled": True})
            assert (await svc.process(alerts(signal_event("BINANCE:BTCUSDT", action="SELL"))))["status"] == "sent"
            await svc.stop()

    asyncio.run(scenario())


def test_dedupe_and_exit_collapsed_into_early_exit(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            tg = FakeChannel()
            svc = await make(rt, tg)
            assert (await svc.process(alerts(signal_event())))["status"] == "sent"
            assert await svc.process(alerts(signal_event())) is None and svc.counters["deduped"] == 1
            svc._time.advance(601)
            assert (await svc.process(alerts(signal_event())))["status"] == "sent"
            done = await svc.process(alerts(signal_event("BINANCE:ETHUSDT", action="EXIT_LONG"), early_exit_event()))
            assert done["priority"] == "critical" and tg.sent[-1][0].splitlines()[0].startswith("⚠ BINANCE:ETHUSDT")
            assert "CERRAR LARGO" not in tg.sent[-1][0]
            await svc.stop()

    asyncio.run(scenario())


def test_quiet_hours_hold_alerts_and_announce_them_later(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            tg = FakeChannel()
            svc = await make(rt, tg, now=NIGHT_NY, settings={"alerts.quiet_enabled": True})
            assert svc.full_status()["quiet"]["active"] is True
            assert await svc.process(alerts(signal_event(), macro_event("red"))) is None  # critical too (default)
            assert svc.counters["quiet"] == 2 and tg.sent == []
            assert [r["status"] for r in svc.recent] == ["quiet", "quiet"]
            await rt.settings.update({"alerts.quiet_critical": True})
            done = await svc.process(alerts(paper_close_event(pnl=-50.0), signal_event("BINANCE:ETHUSDT")))
            assert done["status"] == "sent" and len(tg.sent) == 1
            text = tg.sent[0][0]
            assert text.startswith("🛑 Cuenta demo (simulado)") and "horas de silencio" not in text  # still quiet
            assert svc.counters["quiet"] == 3
            svc._time.now = utc(2026, 7, 1, 13, 0)  # 09:00 in New York: quiet hours over
            done = await svc.process(alerts(signal_event("NASDAQ:AAPL")))
            assert "Durante las horas de silencio se omitieron 3 avisos." in tg.sent[-1][0]
            assert svc.policy.omitted == {"rate": 0, "quiet": 0}
            await svc.stop()

    asyncio.run(scenario())


def test_hourly_limit_spares_critical_alerts_and_reports_omitted(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            tg = FakeChannel()
            svc = await make(rt, tg, settings={"alerts.max_per_hour": 2})
            await svc.process(alerts(signal_event()))
            await svc.process(alerts(signal_event("BINANCE:ETHUSDT")))
            assert await svc.process(alerts(paper_open_event())) is None and svc.counters["rate_limited"] == 1
            done = await svc.process(alerts(macro_event("red")))  # critical: sent despite the limit
            assert done["status"] == "sent" and len(tg.sent) == 3
            assert "Además se omitió 1 aviso por el límite de 2 por hora." in tg.sent[-1][0]
            for i in range(60):  # a flood of critical messages: hard ceiling max(2 × limit, 60) per hour
                svc.policy.record_sent(svc._time())
            assert await svc.process(alerts(early_exit_event())) is None and svc.counters["rate_limited"] == 2
            svc._time.advance(3601)
            assert (await svc.process(alerts(paper_close_event(reason="TP3", pnl=10.0))))["status"] == "sent"
            assert "Además se omitió 1 aviso por el límite" in tg.sent[-1][0]  # the first one was announced already
            await svc.stop()

    asyncio.run(scenario())


def test_full_batch_fits_one_telegram_message(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            tg = FakeChannel()
            svc = await make(rt, tg, settings={"alerts.min_level": "info"})
            long = macro_event("red", message="🔴 " + "muy largo " * 120)
            batch = [classify(long)] + [classify(paper_close_event(pid=i, reason="TP3", pnl=1.0)) for i in range(11)]
            await svc.process(batch)
            text = tg.sent[0][0]
            assert text.startswith("NEXUS · 12 avisos") and len(text) < 4000
            assert all(len(line) <= 280 for line in text.splitlines())
            await svc.stop()

    asyncio.run(scenario())


def test_failures_partial_delivery_and_no_channel(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            bad = FakeChannel(fail="Telegram rechazó el token del bot: cópialo de nuevo desde @BotFather.")
            svc = await make(rt, bad, settings={"alerts.max_per_hour": 1})
            await svc.process(alerts(signal_event()))
            assert svc.counters["failed"] == 1 and svc.last["telegram"]["ok"] is False
            assert svc.recent[0]["status"] == "failed" and "BotFather" in svc.recent[0]["results"][0]["error"]
            warn = [e for e in rt.bus.recent(20, source="alerts") if e["level"] == "warn"]
            assert warn and "Telegram no entregó un aviso" in warn[-1]["message"]
            assert svc.policy.used(svc._time()) == 0  # a failed message does not use the hourly quota
            mail = FakeChannel("email", "Correo", boom=RuntimeError("smtp password=hunter2"))
            ok = FakeChannel()
            svc.channels = [ok, mail]
            done = await svc.process(alerts(signal_event("BINANCE:ETHUSDT")))
            assert done["status"] == "partial" and svc.counters["sent"] == 1
            err = svc.last["email"]["error"]
            assert err == "Error inesperado al enviar por Correo (RuntimeError)." and "hunter2" not in err
            svc.channels = [FakeChannel(enabled=False), FakeChannel("email", "Correo", configured=False)]
            assert await svc.process(alerts(macro_event("red"))) is None
            assert svc.counters["no_channel"] == 1 and svc.recent[0]["status"] == "no_channel"
            await svc.stop()

    asyncio.run(scenario())


def test_state_survives_a_restart(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            tg = FakeChannel()
            svc = await make(rt, tg)
            await svc.process(alerts(signal_event()))
            await svc.send_test()
            await svc.stop()  # saves the state
            again = AlertService(rt, clock=Clock(NOON_NY + 60), channels=[tg])
            await again.start()
            assert again.counters["sent"] == 1 and again.last["telegram"]["ok"] and again.recent[0]["status"] == "sent"
            assert again.last_test is not None and again.last_test["ok"] is True
            assert again.since == svc.since
            assert await again.process(alerts(signal_event())) is None  # deduped across the restart
            assert again.counters["deduped"] == 1 and again.policy.used(NOON_NY + 60) == 1
            await again.stop()
            row = await rt.db.fetchone("SELECT state FROM model_state WHERE kind='alerts' AND key='state'")
            assert row is not None and "hunter2" not in row["state"]

    asyncio.run(scenario())


def test_send_test_reports_each_channel(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            tg = FakeChannel(enabled=False)
            mail = FakeChannel("email", "Correo", enabled=True, configured=False)
            svc = await make(rt, tg, mail, settings={"alerts.quiet_enabled": True})
            res = await svc.send_test()
            assert res["ok"] is True and len(tg.sent) == 1 and mail.sent == []
            text = tg.sent[0][0]
            assert text.startswith("✅ Prueba de NEXUS Terminal") and "silencio de 23:00 a 07:00" in text
            assert "semáforo de mercado, señales NEXUS, cuenta demo" in text
            by = {r["channel"]: r for r in res["results"]}
            assert by["telegram"]["ok"] and by["telegram"]["enabled"] is False and not by["telegram"]["skipped"]
            assert by["email"]["skipped"] and by["email"]["error"] == "Falta el token del bot."
            assert "canal desactivado" in res["message"]
            with pytest.raises(AlertBusy) as busy:
                await svc.send_test()
            assert busy.value.status == 429 and "antes de otra prueba" in str(busy.value)
            svc._last_test_mono = float("-inf")
            with pytest.raises(ValueError):
                await svc.send_test("sms")
            svc.channels = [FakeChannel(fail="Telegram no encuentra el chat")]
            res = await svc.send_test("telegram")
            assert res["ok"] is False and res["message"] == "Telegram: Telegram no encuentra el chat"
            assert svc.counters["sent"] == 0 and "telegram" not in svc.last  # tests are not alert deliveries
            svc._last_test_mono = float("-inf")
            svc.channels = [FakeChannel(configured=False)]
            res = await svc.send_test()
            assert res["ok"] is False and "No hay ningún canal configurado" in res["message"]
            await svc.stop()

    asyncio.run(scenario())


def test_delayed_quotes_are_labelled(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            monkeypatch.setattr(config, "OFFLINE", False)
            rt.hub = StubHub(delays={"NASDAQ:AAPL": 900})
            tg = FakeChannel()
            svc = await make(rt, tg)
            await svc.process(alerts(signal_event("NASDAQ:AAPL"), signal_event("BINANCE:BTCUSDT")))
            text = tg.sent[0][0]
            assert "NASDAQ:AAPL 1h → COMPRAR · score +0,52 · p↑ 64 % · Filtro: TOMAR 78/100 · datos con ~15 min de " \
                   "retraso" in text
            assert rt.hub.quote_calls == [["NASDAQ:AAPL"]]  # crypto is real time: no lookup
            assert "retraso" not in [line for line in text.splitlines() if "BTCUSDT" in line][0]
            await svc.stop()

    asyncio.run(scenario())
