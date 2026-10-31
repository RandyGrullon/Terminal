"""Alert channels with fakes (nothing is ever sent): Telegram Bot API through a stand-in for app.util.http_client()
and SMTP through a fake smtplib. Official endpoint, plain text, Spanish errors, secrets never in a message, TLS with
the system trust store, no password over an unencrypted connection, NEXUS_OFFLINE=1 sends nothing."""
from __future__ import annotations

import asyncio
import smtplib
import socket
import ssl

import httpx
import pytest

from app import config, util
from app.alerts import channels as ch
from app.config import DEFAULT_SETTINGS
from tests.test_imp_alerts_fakes import (CHAT, TOKEN, FakeHTTP, FakeResponse, FakeSMTP, FakeSMTPSSL, tg_error,
                                         tg_ok)


def getter(**overrides):
    values = {**DEFAULT_SETTINGS, **{k.replace("__", "."): v for k, v in overrides.items()}}
    return lambda key, default=None: values.get(key, default)


TG = dict(alerts__telegram_enabled=True, alerts__telegram_token=TOKEN, alerts__telegram_chat_id=CHAT)
MAIL = dict(alerts__email_enabled=True, alerts__smtp_host="smtp.example.com", alerts__smtp_port=587,
            alerts__smtp_security="starttls", alerts__smtp_user="yo@example.com", alerts__smtp_password="s3cr3t-pass",
            alerts__email_to="yo@example.com, otra@example.org")


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE", False)


@pytest.fixture
def fast_sleep(monkeypatch):
    slept: list[float] = []

    async def no_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(ch.asyncio, "sleep", no_sleep)
    return slept


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.reset()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    yield FakeSMTP
    FakeSMTP.reset()


def send_tg(http: FakeHTTP, monkeypatch, text: str = "hola", **settings) -> None:
    monkeypatch.setattr(util, "http_client", http)
    asyncio.run(ch.TelegramChannel().send(getter(**{**TG, **settings}), text, "asunto"))


# ----------------------------------------------------------------------------- Telegram


def test_telegram_info_formats_and_missing_fields():
    tg = ch.TelegramChannel()
    info = tg.info(getter())
    assert not info.enabled and not info.configured and info.missing == ["el token del bot", "el ID del chat"]
    assert info.reason() == "Falta el token del bot y el ID del chat."
    info = tg.info(getter(**TG))
    assert info.active and info.target == f"chat {CHAT}" and TOKEN not in str(info.to_dict())
    assert tg.info(getter(**{**TG, "alerts__telegram_token": f"bot{TOKEN}"})).configured  # pasted from the URL
    bad = tg.info(getter(**{**TG, "alerts__telegram_token": "123/../evil?x=1"}))
    assert not bad.configured and "formato" in bad.problem
    assert not tg.info(getter(**{**TG, "alerts__telegram_chat_id": "mi chat"})).configured
    assert tg.info(getter(**{**TG, "alerts__telegram_chat_id": "-1001234567890"})).configured
    assert tg.info(getter(**{**TG, "alerts__telegram_chat_id": "@mi_canal"})).configured


def test_telegram_send_uses_official_api_plain_text(online, monkeypatch):
    http = FakeHTTP(tg_ok())
    send_tg(http, monkeypatch, "🔴 FOMC en 15 min\n— NEXUS", alerts__telegram_token=f"bot{TOKEN}")
    url, payload = http.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload == {"chat_id": CHAT, "text": "🔴 FOMC en 15 min\n— NEXUS", "disable_web_page_preview": True}
    assert "parse_mode" not in payload and http.client_kwargs == [{"timeout": ch.TELEGRAM_TIMEOUT_S}]
    long = FakeHTTP(tg_ok())
    send_tg(long, monkeypatch, "x" * 5000)
    assert len(long.calls[0][1]["text"]) == ch.TELEGRAM_MAX


@pytest.mark.parametrize("response, expected", [
    (tg_error(401, "Unauthorized"), "rechazó el token del bot"),
    (tg_error(404, "Not Found"), "rechazó el token del bot"),
    (tg_error(400, "Bad Request: chat not found"), "no encuentra el chat"),
    (tg_error(403, "Forbidden: bot was blocked by the user"), "pulsa Iniciar"),
    (tg_error(400, "Bad Request: message is too long"), "rechazó el mensaje (Bad Request: message is too long)"),
    (tg_error(502, "Bad Gateway"), "no está disponible ahora (HTTP 502)"),
])
def test_telegram_errors_are_spanish(online, monkeypatch, response, expected):
    with pytest.raises(ch.ChannelError) as err:
        send_tg(FakeHTTP(response), monkeypatch)
    assert expected in str(err.value) and TOKEN not in str(err.value)


def test_telegram_retries_one_short_429_only(online, monkeypatch, fast_sleep):
    http = FakeHTTP(tg_error(429, "Too Many Requests: retry after 3", retry_after=3), tg_ok())
    send_tg(http, monkeypatch)
    assert len(http.calls) == 2 and fast_sleep == [3.0]
    slow = FakeHTTP(tg_error(429, "Too Many Requests: retry after 60", retry_after=60))
    with pytest.raises(ch.ChannelError, match="vuelve a intentarlo en 60 s"):
        send_tg(slow, monkeypatch)
    assert len(slow.calls) == 1
    twice = FakeHTTP(tg_error(429, "x", retry_after=2), tg_error(429, "x", retry_after=2))
    with pytest.raises(ch.ChannelError, match="limita los envíos"):
        send_tg(twice, monkeypatch)
    assert len(twice.calls) == 2


def test_telegram_network_errors_never_leak_the_token(online, monkeypatch):
    boom = httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")
    with pytest.raises(ch.ChannelError) as err:
        send_tg(FakeHTTP(boom), monkeypatch)
    assert str(err.value) == "No se pudo conectar con api.telegram.org (ConnectError)." and TOKEN not in str(err.value)
    with pytest.raises(ch.ChannelError, match="no respondió a tiempo"):
        send_tg(FakeHTTP(httpx.ReadTimeout("timeout")), monkeypatch)
    leaky = tg_error(400, f"Bad Request: weird {TOKEN}")
    with pytest.raises(ch.ChannelError) as err:
        send_tg(FakeHTTP(leaky), monkeypatch)
    assert TOKEN not in str(err.value) and "•••" in str(err.value)
    with pytest.raises(ch.ChannelError, match="HTTP 200"):  # 200 without a JSON body is not a delivery
        send_tg(FakeHTTP(FakeResponse(200, ValueError("no json"))), monkeypatch)


def test_telegram_offline_and_incomplete_send_nothing(monkeypatch):
    http = FakeHTTP(tg_ok())
    monkeypatch.setattr(config, "OFFLINE", True)
    with pytest.raises(ch.ChannelError, match="NEXUS_OFFLINE=1"):
        send_tg(http, monkeypatch)
    monkeypatch.setattr(config, "OFFLINE", False)
    with pytest.raises(ch.ChannelError, match="Falta el ID del chat"):
        send_tg(http, monkeypatch, alerts__telegram_chat_id="")
    assert http.calls == []


# ----------------------------------------------------------------------------- email


def test_email_info_validation():
    em = ch.EmailChannel()
    info = em.info(getter())
    assert not info.configured and info.missing == ["el servidor SMTP", "el remitente", "el destinatario"]
    ok = em.info(getter(**MAIL))
    assert ok.active and ok.target == "yo@example.com, otra@example.org" and "s3cr3t" not in str(ok.to_dict())
    assert em.info(getter(**{**MAIL, "alerts__smtp_password": ""})).missing == ["la contraseña"]
    plain = em.info(getter(**{**MAIL, "alerts__smtp_security": "none"}))
    assert not plain.configured and "sin cifrar" in plain.problem
    local = em.info(getter(**{**MAIL, "alerts__smtp_security": "none", "alerts__smtp_user": "",
                              "alerts__smtp_password": "", "alerts__email_from": "nexus@localhost.local"}))
    assert local.configured
    assert "puerto" in em.info(getter(**{**MAIL, "alerts__smtp_port": 70000})).problem
    assert "Destinatario no válido" in em.info(getter(**{**MAIL, "alerts__email_to": "yo@example.com, nadie"})).problem
    assert "remitente" in em.info(getter(**{**MAIL, "alerts__email_from": "Yo <no-es-correo>"})).problem
    assert "Seguridad" in em.info(getter(**{**MAIL, "alerts__smtp_security": "tls13"})).problem
    assert "servidor SMTP" in em.info(getter(**{**MAIL, "alerts__smtp_host": "smtp.x.com/evil"})).problem
    good, bad = ch.parse_recipients("a@b.com; c@d.org  A@B.com,e@f.net")
    assert good == ["a@b.com", "c@d.org", "e@f.net"] and bad == []
    assert ch.parse_address("Yo <yo@example.com>") == ("Yo", "yo@example.com")
    assert ch.parse_address("yo@example.com\r\nBcc: x@y.z") is None


def test_email_starttls_login_and_message(online, fake_smtp):
    asyncio.run(ch.EmailChannel().send(getter(**MAIL), "🔴 FOMC\n— NEXUS", "NEXUS · 🔴 FOMC\r\nBcc: evil@x.com"))
    smtp = fake_smtp.instances[0]
    names = [s[0] for s in smtp.steps]
    assert names == ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert smtp.steps[0] == ("connect", "smtp.example.com", 587, False) and smtp.timeout == ch.SMTP_TIMEOUT_S
    assert isinstance(smtp.steps[2][1], ssl.SSLContext)  # verified TLS with the system trust store
    assert smtp.steps[4] == ("login", "yo@example.com", "s3cr3t-pass")
    assert smtp.steps[5] == ("send", "yo@example.com", ["yo@example.com", "otra@example.org"])
    msg = smtp.messages[0]
    assert msg["Subject"] == "NEXUS · 🔴 FOMC Bcc: evil@x.com" and msg["Bcc"] is None  # no header injection
    assert msg["From"] == "yo@example.com" and msg["To"] == "yo@example.com, otra@example.org"
    body = msg.get_content()
    assert body.startswith("🔴 FOMC\n— NEXUS") and "Ajustes → Alertas" in body and msg["Message-ID"].endswith(
        "@example.com>")


def test_email_ssl_and_unencrypted_local_relay(online, fake_smtp):
    asyncio.run(ch.EmailChannel().send(getter(**{**MAIL, "alerts__smtp_security": "ssl", "alerts__smtp_port": 465}),
                                       "x", "s"))
    smtp = fake_smtp.instances[0]
    assert smtp.steps[0] == ("connect", "smtp.example.com", 465, True) and isinstance(smtp.context, ssl.SSLContext)
    assert "starttls" not in [s[0] for s in smtp.steps]
    fake_smtp.reset()
    local = {**MAIL, "alerts__smtp_security": "none", "alerts__smtp_user": "", "alerts__smtp_password": "",
             "alerts__smtp_host": "localhost", "alerts__smtp_port": 25, "alerts__email_from": "Nexus <nexus@pc.lan>"}
    asyncio.run(ch.EmailChannel().send(getter(**local), "x", "s"))
    names = [s[0] for s in fake_smtp.instances[0].steps]
    assert names == ["connect", "ehlo", "send", "quit"]
    assert fake_smtp.instances[0].messages[0]["From"] == "Nexus <nexus@pc.lan>"


def test_email_refuses_password_without_encryption(online, fake_smtp):
    with pytest.raises(ch.ChannelError, match="sin cifrar"):
        asyncio.run(ch.EmailChannel().send(getter(**{**MAIL, "alerts__smtp_security": "none"}), "x", "s"))
    assert fake_smtp.instances == []


@pytest.mark.parametrize("step, error, expected", [
    ("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials s3cr3t-pass"), "contraseña de aplicación"),
    ("starttls", smtplib.SMTPNotSupportedError("STARTTLS extension not supported"), "no admite STARTTLS"),
    ("send", smtplib.SMTPRecipientsRefused({"otra@example.org": (550, b"no such user")}), "otra@example.org"),
    ("send", smtplib.SMTPSenderRefused(553, b"not allowed", "yo@example.com"), "rechazó el remitente"),
    ("connect", smtplib.SMTPServerDisconnected("closed"), "cerró la conexión"),
    ("connect", socket.gaierror(11001, "getaddrinfo failed"), "No se encuentra el servidor smtp.example.com"),
    ("connect", TimeoutError("timed out"), "no respondió a tiempo"),
    ("connect", ConnectionRefusedError(10061, "refused"), "rechazó la conexión"),
    ("starttls", ssl.SSLError(1, "WRONG_VERSION_NUMBER"), "Fallo TLS"),
    ("starttls", ssl.SSLCertVerificationError(1, "self signed"), "no es de confianza"),
    ("send", smtplib.SMTPDataError(554, b"spam"), "código 554"),
])
def test_email_errors_are_spanish_and_secret_free(online, fake_smtp, step, error, expected):
    fake_smtp.fail_at, fake_smtp.error = step, error
    with pytest.raises(ch.ChannelError) as err:
        asyncio.run(ch.EmailChannel().send(getter(**MAIL), "x", "s"))
    assert expected in str(err.value) and "s3cr3t" not in str(err.value)
    if step != "connect":  # a session was opened: closed without the polite QUIT (it would wait for the server)
        assert fake_smtp.instances[-1].steps[-1] == ("close",)


def test_email_offline_and_from_defaults(monkeypatch, fake_smtp):
    monkeypatch.setattr(config, "OFFLINE", True)
    with pytest.raises(ch.ChannelError, match="NEXUS_OFFLINE=1"):
        asyncio.run(ch.EmailChannel().send(getter(**MAIL), "x", "s"))
    assert fake_smtp.instances == []
    no_from = ch.EmailChannel().info(getter(**{**MAIL, "alerts__smtp_user": "usuario123"}))
    assert no_from.missing == ["el remitente"]  # the user is not an address, so it cannot be the sender
