"""Regression tests from the adversarial review of the alerts feature (fakes only: nothing is ever sent).

- Dedupe groups: the semáforo going back to an earlier level (red -> amber -> red, red -> green -> red) and a market's
  signal flipping back (BUY -> EXIT -> BUY on a short timeframe) are announced again within the 10-min dedupe window,
  while plain repeats (a restart re-announcing the same state) are still deduped.
- alerts.signal_symbols entries without exchange ('BTCUSDT') match the watched market; entries NEXUS does not watch
  are reported in the status instead of silently never alerting.
- POST /api/alerts/test only accepts JSON (no bodyless / form posts from another site: CSRF).
- SMTP errors name the missing extension of the step that failed (STARTTLS, AUTH, SMTPUTF8) and non-ASCII credentials.
- The terminal does not promise that critical alerts are "always" sent (hard ceiling per hour).
"""
from __future__ import annotations

import asyncio
import smtplib

import pytest

from app import config, util
from app.alerts import channels as ch
from app.alerts.classify import classify
from app.alerts.policy import DEDUPE_S, Policy, symbol_matches
from app.alerts.service import AlertService
from app.config import DEFAULT_SETTINGS
from app.runtime import rt
from tests.test_imp_alerts_fakes import (CHAT, TOKEN, Clock, FakeChannel, FakeHTTP, FakeSMTP, FakeSMTPSSL,
                                         early_exit_event, macro_event, signal_event, tg_ok, utc)

NOON_NY = utc(2026, 7, 1, 16, 0)
RED = "🔴 No operar ahora: VIX 31. Espera a que baje la volatilidad."
AMBER = "🟡 Precaución: VIX 26. Reduce el tamaño de las posiciones."
GREEN = "🟢 Operar con normalidad: sin eventos de alto impacto cerca."


class StubNexus:
    def __init__(self, watching):
        self._watching = list(watching)

    def watching(self):
        return list(self._watching)


async def make(rt_, channel, *, tf: str = "1h", settings: dict | None = None, now: float = NOON_NY):
    await rt_.settings.update({"ui.timezone": "America/New_York", **(settings or {})})
    rt_.nexus = StubNexus([{"symbol": "BINANCE:BTCUSDT", "tf": tf}, {"symbol": "NASDAQ:AAPL", "tf": tf}])
    svc = AlertService(rt_, clock=Clock(now), channels=[channel])
    svc.batch_window_s = 0.05
    await svc.start()
    return svc


def level(color: str, text: str, kind: str = "level"):
    return classify(macro_event(color, kind, text))


def test_semaforo_back_to_an_earlier_level_is_announced_again(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt_:
            tg = FakeChannel()
            svc = await make(rt_, tg)
            assert (await svc.process([level("red", RED)]))["status"] == "sent"
            svc._time.advance(120)
            assert (await svc.process([level("amber", AMBER)]))["status"] == "sent"
            svc._time.advance(120)
            # the macro tracker re-announces an escalation to red even within its cooldown: so must the channels
            assert (await svc.process([level("red", RED)]))["status"] == "sent"
            assert await svc.process([level("red", RED)]) is None  # a plain repeat is still deduped
            svc._time.advance(60)
            # green is filtered at 'warn' but still replaces red: back to red is a new state
            assert await svc.process([level("green", GREEN)]) is None and svc.counters["filtered"] == 1
            svc._time.advance(60)
            assert (await svc.process([level("red", RED, "volatility")]))["status"] == "sent"
            assert [t.splitlines()[0] for t, _ in tg.sent] == [RED, AMBER, RED, RED]
            # event countdowns are not part of the group: their own repeats stay deduped
            soon = classify(macro_event("red", "event_soon"))
            assert (await svc.process([soon]))["status"] == "sent" and await svc.process([soon]) is None
            await svc.stop()
            # a restart re-announcing the current level (the tracker starts from scratch) is not sent twice
            again = AlertService(rt_, clock=Clock(svc._time() + 30), channels=[tg])
            again.batch_window_s = 0.05
            await again.start()
            assert await again.process([level("red", RED, "volatility")]) is None
            assert again.counters["deduped"] == svc.counters["deduped"] + 1
            assert (await again.process([level("amber", AMBER)]))["status"] == "sent"
            await again.stop()

    asyncio.run(scenario())


def test_signal_flip_back_on_a_short_timeframe_is_not_deduped(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt_:
            tg = FakeChannel()
            svc = await make(rt_, tg, tf="1m", settings={"watch.tf": "1m"})
            sym = "BINANCE:BTCUSDT"
            assert (await svc.process([classify(signal_event(sym, "1m", "BUY"))]))["status"] == "sent"
            svc._time.advance(60)
            assert (await svc.process([classify(signal_event(sym, "1m", "EXIT_LONG"))]))["status"] == "sent"
            svc._time.advance(60)
            assert (await svc.process([classify(signal_event(sym, "1m", "BUY"))]))["status"] == "sent"
            assert await svc.process([classify(signal_event(sym, "1m", "BUY"))]) is None  # same signal again
            # another market's signal does not reset this one
            svc._time.advance(60)
            assert (await svc.process([classify(signal_event("NASDAQ:AAPL", "1m", "SELL"))]))["status"] == "sent"
            assert await svc.process([classify(signal_event(sym, "1m", "BUY"))]) is None
            # an exit + early exit of one market: one critical alert, and the pair re-emitted is deduped
            pair = [classify(signal_event(sym, "1m", "EXIT_LONG")), classify(early_exit_event(sym, "1m"))]
            done = await svc.process(pair)
            assert done["priority"] == "critical" and "CERRAR LARGO" not in tg.sent[-1][0]
            pair = [classify(signal_event(sym, "1m", "EXIT_LONG")), classify(early_exit_event(sym, "1m"))]
            assert await svc.process(pair) is None
            await svc.stop()

    asyncio.run(scenario())


def test_policy_groups_persist_and_expire():
    pol = Policy()
    pol.supersede("g", "a", 1000.0)
    assert not pol.duplicate("a", 1000.0)
    pol.supersede("g", "b", 1010.0)
    assert "a" not in pol.seen and pol.latest["g"][0] == "b"
    assert not pol.duplicate("b", 1010.0)
    restored = Policy()
    restored.load(pol.dump(), 1100.0)
    assert restored.latest == {"g": ("b", 1010.0)} and "b" in restored.seen
    restored.supersede("g", "b", 1100.0)  # the same state again: kept, so its repeat is deduped
    assert restored.duplicate("b", 1100.0)
    restored.supersede(None, "x", 1100.0)  # no group: nothing changes
    assert set(restored.latest) == {"g"}
    restored.load({"latest": {"old": ["k", 1.0], "bad": "x", "neg": ["k", True]}}, 5000.0)
    assert restored.latest == {}
    restored.supersede("g", "c", 5000.0)
    restored._prune(5000.0 + DEDUPE_S)
    assert restored.latest == {}


def test_ticker_only_signal_symbols_and_unwatched_subset_warning(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt_:
            tg = FakeChannel()
            svc = await make(rt_, tg, settings={"alerts.signal_symbols": ["BTCUSDT"]})
            assert (await svc.process([classify(signal_event("BINANCE:BTCUSDT"))]))["status"] == "sent"
            assert await svc.process([classify(signal_event("NASDAQ:AAPL"))]) is None  # not in the subset
            assert not any("Señales solo de" in w for w in svc.full_status()["warnings"])
            await rt_.settings.update({"alerts.signal_symbols": ["BTCUSDT", "BINANCE:SOLUSDT", "MSFT"]})
            warn = [w for w in svc.full_status()["warnings"] if "Señales solo de" in w]
            assert warn and "BINANCE:SOLUSDT, MSFT no están en lo que vigila NEXUS" in warn[0]
            assert "BTCUSDT," not in warn[0]
            rt_.nexus = StubNexus([])  # watcher not running (e.g. NEXUS_BACKGROUND=0): unknown, no warning
            assert not any("Señales solo de" in w for w in svc.full_status()["warnings"])
            await svc.stop()

    asyncio.run(scenario())
    assert symbol_matches("NASDAQ:AAPL", "AAPL") and symbol_matches("NASDAQ:AAPL", "NASDAQ:AAPL")
    assert not symbol_matches("NASDAQ:AAPL", "NYSE:AAPL") and not symbol_matches("NASDAQ:AAPLX", "AAPL")


def test_test_endpoint_only_accepts_json(api_client, monkeypatch):
    body = {"alerts.telegram_enabled": True, "alerts.telegram_token": TOKEN, "alerts.telegram_chat_id": CHAT}
    assert api_client.put("/api/settings", json=body).status_code == 200
    http = FakeHTTP(tg_ok())
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(util, "http_client", http)
    rt.alerts._last_test_mono = float("-inf")
    # what a page of another site can send without a CORS preflight: nothing is sent
    for kw in ({}, {"data": {}}, {"content": b"{}", "headers": {"content-type": "text/plain"}},
               {"content": b'{"channel":"telegram"}'}):
        r = api_client.post("/api/alerts/test", **kw)
        assert r.status_code == 415 and "JSON" in r.json()["detail"], (kw, r.status_code)
    assert http.calls == [] and rt.alerts.last_test is None
    r = api_client.post("/api/alerts/test", json={})
    assert r.status_code == 200 and r.json()["ok"] is True and len(http.calls) == 1


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.reset()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setattr(config, "OFFLINE", False)
    yield FakeSMTP
    FakeSMTP.reset()


MAIL = {**DEFAULT_SETTINGS, "alerts.email_enabled": True, "alerts.smtp_host": "smtp.example.com",
        "alerts.smtp_port": 587, "alerts.smtp_security": "starttls", "alerts.smtp_user": "yo@example.com",
        "alerts.smtp_password": "contraseña-ñ", "alerts.email_to": "yo@example.com"}


@pytest.mark.parametrize("step, error, expected", [
    ("starttls", smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."), "no admite STARTTLS"),
    ("login", smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server."), "no ofrece autenticación"),
    ("send", smtplib.SMTPNotSupportedError("...require internationalized email support..."), "caracteres no ASCII"),
    ("login", UnicodeEncodeError("ascii", "contraseña-ñ", 7, 8, "ordinal not in range(128)"), "contraseña de aplicación"),
])
def test_smtp_errors_name_the_failing_step(fake_smtp, step, error, expected):
    fake_smtp.fail_at, fake_smtp.error = step, error
    get = lambda key, default=None: MAIL.get(key, default)  # noqa: E731
    with pytest.raises(ch.ChannelError) as err:
        asyncio.run(ch.EmailChannel().send(get, "x", "s"))
    assert expected in str(err.value) and "contraseña-ñ" not in str(err.value)


def test_test_message_does_not_promise_a_disabled_channel_will_get_alerts(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt_:
            tg = FakeChannel(enabled=False)  # configured, not enabled yet: it gets the test but no alerts
            svc = await make(rt_, tg)
            res = await svc.send_test()
            text = tg.sent[0][0]
            assert res["ok"] is True and "puede recibir tus alertas" in text and "recibirá" not in text
            assert "Con tus ajustes se envían: semáforo de mercado, señales NEXUS, cuenta demo" in text
            assert "canal desactivado: actívalo" in res["message"]
            await svc.stop()

    asyncio.run(scenario())


def test_terminal_does_not_promise_critical_alerts_always_go_out(api_client):
    out = api_client.post("/api/terminal", json={"text": "alertas"}).json()
    texts = " ".join(line["text"] for line in out["lines"])
    assert "siempre se envían" not in texts
    assert "máx. 20 mensajes por hora" in texts and "los críticos pueden superarlo, hasta 60" in texts
