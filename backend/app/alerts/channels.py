"""Delivery channels of the external alerts, all opt-in and off by default:

- Telegram: the user's own bot (token from @BotFather + chat id) through the official Bot API,
  POST https://api.telegram.org/bot<token>/sendMessage with plain text, using app.util.http_client() (honest UA).
- Email: SMTP with smtplib in a worker thread (STARTTLS, SSL/TLS or, only without a password, no encryption),
  TLS verified with the system trust store (app.util.ssl_context()).

`info(get)` says whether a channel is complete without touching the network; `send(get, text, subject)` raises
ChannelError with a Spanish message. Secrets never appear in messages, events or the status. NEXUS_OFFLINE=1 sends
nothing. Tests replace app.util.http_client / smtplib.SMTP with fakes: nothing is ever sent during development."""
from __future__ import annotations

import asyncio
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Any, Callable

from app import config, util

Getter = Callable[[str, Any], Any]

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX = 4000  # Bot API limit is 4096 characters per message
TELEGRAM_TIMEOUT_S = 15.0
TELEGRAM_RETRY_MAX_S = 10  # a 429 asking to wait longer than this is reported, not waited for
TOKEN_RE = re.compile(r"^\d{3,20}:[A-Za-z0-9_-]{20,100}$")
CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$")
SMTP_TIMEOUT_S = 20.0
SECURITY = ("starttls", "ssl", "none")
SECURITY_LABEL = {"starttls": "STARTTLS", "ssl": "SSL/TLS", "none": "sin cifrado"}
ADDR_RE = re.compile(r"^[^@\s<>(),;:\"\[\]\\]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
EMAIL_TAIL = ("\n\nEnviado automáticamente por NEXUS Terminal (investigación y simulación). Para dejar de recibir "
              "estos avisos, desactiva el canal en Ajustes → Alertas.")


class ChannelError(Exception):
    """A delivery failed; the message is Spanish and safe to show (no secrets)."""


@dataclass
class ChannelInfo:
    id: str
    label: str
    enabled: bool
    configured: bool  # every required field present and valid
    missing: list[str] = field(default_factory=list)
    problem: str | None = None
    target: str | None = None  # where it sends (never a secret)

    @property
    def active(self) -> bool:
        return self.enabled and self.configured

    def reason(self) -> str:
        """Why the channel cannot send (Spanish), '' when it can."""
        if self.problem:
            return self.problem
        if self.missing:
            return "Falta " + join_es(self.missing) + "."
        return ""

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "enabled": self.enabled, "configured": self.configured,
                "active": self.active, "missing": list(self.missing), "problem": self.problem, "target": self.target}


def join_es(items: list[str]) -> str:
    """'a', 'a y b', 'a, b y c'."""
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


def _text(get: Getter, key: str) -> str:
    v = get(key, "")
    return v.strip() if isinstance(v, str) else ""


def _scrub(text: str, *secrets: str) -> str:
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "•••")
    return text


def offline_error() -> ChannelError:
    return ChannelError("Modo sin conexión (NEXUS_OFFLINE=1): no se envió nada.")


# ----------------------------------------------------------------------------- Telegram


def telegram_token(raw: str) -> str:
    """The token as typed, without a leading 'bot' copied from the Bot API URL."""
    s = raw.strip()
    if s.lower().startswith("bot") and s[3:4].isdigit():
        s = s[3:]
    return s


class TelegramChannel:
    id = "telegram"
    label = "Telegram"

    def __init__(self, client_factory: Callable[..., Any] | None = None):
        self._client_factory = client_factory

    def _client(self) -> Any:
        factory = self._client_factory or util.http_client  # looked up at call time (tests patch app.util)
        return factory(timeout=TELEGRAM_TIMEOUT_S)

    def info(self, get: Getter) -> ChannelInfo:
        token = telegram_token(_text(get, "alerts.telegram_token"))
        chat = _text(get, "alerts.telegram_chat_id")
        missing = [name for name, v in (("el token del bot", token), ("el ID del chat", chat)) if not v]
        problem = None
        if token and not TOKEN_RE.match(token):
            problem = "El token del bot no tiene el formato de Telegram (números:letras, como lo da @BotFather)."
        elif chat and not CHAT_RE.match(chat):
            problem = "El ID del chat no es válido: usa el número del chat (p. ej. 123456789 o -100…) o @canal."
        return ChannelInfo(self.id, self.label, bool(get("alerts.telegram_enabled", False)),
                           not missing and problem is None, missing, problem, f"chat {chat}" if chat else None)

    async def send(self, get: Getter, text: str, subject: str = "") -> None:
        info = self.info(get)
        if not info.configured:
            raise ChannelError(info.reason())
        if config.OFFLINE:
            raise offline_error()
        token = telegram_token(_text(get, "alerts.telegram_token"))
        chat = _text(get, "alerts.telegram_chat_id")
        body = text if len(text) <= TELEGRAM_MAX else text[: TELEGRAM_MAX - 1] + "…"
        payload = {"chat_id": chat, "text": body, "disable_web_page_preview": True}
        url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        try:
            for attempt in range(2):
                wait = await self._post(url, payload, token)
                if wait is None:
                    return
                if attempt == 0 and wait <= TELEGRAM_RETRY_MAX_S:
                    await asyncio.sleep(max(wait, 0.5))
                    continue
                raise ChannelError(f"Telegram limita los envíos: vuelve a intentarlo en {int(wait)} s.")
        except ChannelError as exc:
            raise ChannelError(_scrub(str(exc), token)) from None

    async def _post(self, url: str, payload: dict, token: str) -> float | None:
        """None when Telegram accepted the message; the wait in seconds when it asks to retry later (429)."""
        try:
            async with self._client() as client:
                resp = await client.post(url, json=payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the URL holds the token: only the exception type is reported
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "Timeout" in type(exc).__name__:
                raise ChannelError("Telegram no respondió a tiempo.") from None
            raise ChannelError(f"No se pudo conectar con api.telegram.org ({type(exc).__name__}).") from None
        try:
            data = resp.json()
        except Exception:
            data = None
        data = data if isinstance(data, dict) else {}
        status = int(getattr(resp, "status_code", 0) or 0)
        if status == 200 and data.get("ok") is True:
            return None
        code = data.get("error_code") if isinstance(data.get("error_code"), int) else status
        desc = _scrub(str(data.get("description") or ""), token)[:200]
        params = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
        if code == 429:
            retry = params.get("retry_after")
            return float(retry) if isinstance(retry, (int, float)) and not isinstance(retry, bool) else 5.0
        raise ChannelError(telegram_error(code, desc))


def telegram_error(code: int, desc: str) -> str:
    low = desc.lower()
    if code in (401, 404):
        return "Telegram rechazó el token del bot: cópialo de nuevo desde @BotFather."
    if code == 400 and "chat not found" in low:
        return ("Telegram no encuentra el chat: revisa el ID del chat y escribe primero a tu bot (por ejemplo, "
                "/start).")
    if code == 403:
        return ("Tu bot no puede escribir en ese chat: abre el chat con el bot y pulsa Iniciar (o desbloquéalo, o "
                "añádelo al grupo).")
    if code == 400:
        return f"Telegram rechazó el mensaje ({desc or 'petición no válida'})."
    if code >= 500:
        return f"Telegram no está disponible ahora (HTTP {code})."
    return f"Telegram respondió con un error (HTTP {code}{': ' + desc if desc else ''})."


# ----------------------------------------------------------------------------- email (SMTP)


def parse_address(raw: str) -> tuple[str, str] | None:
    """('Nombre', 'a@b.com') from 'a@b.com' or 'Nombre <a@b.com>'; None when it is not one valid address."""
    s = raw.strip()
    if not s or "\r" in s or "\n" in s:
        return None
    name, addr = parseaddr(s)
    if not addr or not ADDR_RE.match(addr):
        return None
    return name, addr


def parse_recipients(raw: str) -> tuple[list[str], list[str]]:
    """(valid addresses, invalid parts) from 'a@b.com, c@d.org; e@f.net'."""
    good: list[str] = []
    bad: list[str] = []
    for part in re.split(r"[,;\s]+", raw or ""):
        if not part:
            continue
        parsed = parse_address(part)
        if parsed is None:
            bad.append(part)
        elif parsed[1].lower() not in (g.lower() for g in good):
            good.append(parsed[1])
    return good, bad


@dataclass
class SmtpConfig:
    host: str
    port: int
    security: str
    user: str
    password: str
    sender: str  # formatted From header
    sender_addr: str
    recipients: list[str]


class EmailChannel:
    id = "email"
    label = "Correo"

    def _read(self, get: Getter) -> tuple[SmtpConfig | None, list[str], str | None]:
        host = _text(get, "alerts.smtp_host")
        security = _text(get, "alerts.smtp_security").lower() or "starttls"
        user = _text(get, "alerts.smtp_user")
        raw_password = get("alerts.smtp_password", "")
        password = raw_password if isinstance(raw_password, str) else ""
        sender_raw = _text(get, "alerts.email_from") or (user if "@" in user else "")
        to_raw = _text(get, "alerts.email_to")
        try:
            port = int(get("alerts.smtp_port", 587))
        except (TypeError, ValueError):
            port = 0
        missing = [name for name, v in (("el servidor SMTP", host), ("el remitente", sender_raw),
                                        ("el destinatario", to_raw)) if not v]
        if user and not password:
            missing.append("la contraseña")
        problem = None
        sender = parse_address(sender_raw) if sender_raw else None
        recipients, bad = parse_recipients(to_raw)
        if host and not re.match(r"^[A-Za-z0-9.-]+$", host):
            problem = "El servidor SMTP no es un nombre de servidor válido (p. ej. smtp.gmail.com)."
        elif not 1 <= port <= 65535:
            problem = "El puerto SMTP debe estar entre 1 y 65535 (587 = STARTTLS, 465 = SSL/TLS)."
        elif security not in SECURITY:
            problem = "Seguridad SMTP no válida: elige STARTTLS, SSL/TLS o sin cifrado."
        elif security == "none" and (user or password):
            problem = ("Por seguridad, NEXUS no envía la contraseña sin cifrar: elige STARTTLS o SSL/TLS (o deja "
                       "usuario y contraseña vacíos para un servidor local sin autenticación).")
        elif sender_raw and sender is None:
            problem = f"El remitente no es una dirección de correo válida: {sender_raw}"
        elif bad:
            problem = f"Destinatario no válido: {', '.join(bad[:3])}"
        if missing or problem or sender is None or not recipients:
            return None, missing, problem
        cfg = SmtpConfig(host, port, security, user, password, formataddr(sender), sender[1], recipients)
        return cfg, [], None

    def info(self, get: Getter) -> ChannelInfo:
        cfg, missing, problem = self._read(get)
        to_raw = _text(get, "alerts.email_to")
        target = ", ".join(parse_recipients(to_raw)[0]) or None
        return ChannelInfo(self.id, self.label, bool(get("alerts.email_enabled", False)), cfg is not None,
                           missing, problem, target)

    async def send(self, get: Getter, text: str, subject: str = "") -> None:
        cfg, missing, problem = self._read(get)
        if cfg is None:
            raise ChannelError(ChannelInfo(self.id, self.label, True, False, missing, problem).reason()
                               or "El correo no está configurado.")
        if config.OFFLINE:
            raise offline_error()
        msg = build_message(cfg, subject or "NEXUS · aviso", text)
        context = util.ssl_context() if cfg.security != "none" else None
        try:
            await asyncio.to_thread(smtp_send, cfg, msg, context)
        except ChannelError as exc:
            raise ChannelError(_scrub(str(exc), cfg.password)) from None


def build_message(cfg: SmtpConfig, subject: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = " ".join(subject.split())[:180]
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender_addr.rpartition("@")[2] or "nexus.local")
    msg["X-Mailer"] = f"NEXUS Terminal {config.VERSION}"
    msg.set_content(text + EMAIL_TAIL)
    return msg


def smtp_send(cfg: SmtpConfig, msg: EmailMessage, context: ssl.SSLContext | None) -> None:
    """Blocking SMTP delivery (runs in a worker thread). Raises ChannelError with a Spanish message."""
    where = f"{cfg.host}:{cfg.port}"
    server: Any = None
    delivered = False
    step = "connect"
    try:
        if cfg.security == "ssl":
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_S, context=context)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_S)
        server.ehlo()
        if cfg.security == "starttls":
            step = "starttls"
            server.starttls(context=context)
            server.ehlo()
        if cfg.user:
            step = "login"
            server.login(cfg.user, cfg.password)
        step = "send"
        server.send_message(msg, from_addr=cfg.sender_addr, to_addrs=cfg.recipients)
        delivered = True
    except ChannelError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise ChannelError("El servidor de correo rechazó el usuario o la contraseña (en Gmail u Outlook hace falta "
                           "una contraseña de aplicación).") from None
    except smtplib.SMTPNotSupportedError:
        # the same error class for a missing extension at each step: STARTTLS, AUTH or SMTPUTF8
        if step == "login":
            raise ChannelError(f"{cfg.host} no ofrece autenticación (AUTH) en esta conexión: revisa el puerto y la "
                               "seguridad, o deja usuario y contraseña vacíos si tu servidor no la pide.") from None
        if step == "send":
            raise ChannelError(f"{cfg.host} no acepta direcciones con caracteres no ASCII (acentos, eñes): usa "
                               "direcciones solo con letras sin tilde, números y . _ -.") from None
        raise ChannelError(f"{cfg.host} no admite STARTTLS: prueba Seguridad = SSL/TLS (puerto 465).") from None
    except UnicodeEncodeError:  # smtplib's AUTH encodes the user and password as ASCII
        if step != "login":
            raise ChannelError(f"Error SMTP con {where} (UnicodeEncodeError).") from None
        raise ChannelError("El usuario o la contraseña tienen caracteres no ASCII (ñ, tildes, €…) y el inicio de sesión "
                           "SMTP no los admite: usa una contraseña de aplicación o una sin esos caracteres.") from None
    except smtplib.SMTPSenderRefused as exc:
        raise ChannelError(f"El servidor rechazó el remitente {cfg.sender_addr} (código {exc.smtp_code}).") from None
    except smtplib.SMTPRecipientsRefused as exc:
        who = ", ".join(str(r) for r in list(exc.recipients)[:3]) or "los destinatarios"
        raise ChannelError(f"El servidor rechazó el destinatario: {who}.") from None
    except smtplib.SMTPServerDisconnected:
        raise ChannelError(f"{where} cerró la conexión: revisa el puerto y la seguridad (587 = STARTTLS, "
                           "465 = SSL/TLS).") from None
    except smtplib.SMTPResponseException as exc:
        raise ChannelError(f"El servidor de correo respondió con un error (código {exc.smtp_code}).") from None
    except smtplib.SMTPException as exc:
        raise ChannelError(f"Error SMTP con {where} ({type(exc).__name__}).") from None
    except ssl.SSLCertVerificationError:
        raise ChannelError(f"El certificado de {cfg.host} no es de confianza en este equipo.") from None
    except ssl.SSLError:
        raise ChannelError(f"Fallo TLS con {where}: revisa el puerto y la seguridad (587 = STARTTLS, "
                           "465 = SSL/TLS).") from None
    except socket.gaierror:
        raise ChannelError(f"No se encuentra el servidor {cfg.host}: revisa el nombre.") from None
    except TimeoutError:
        raise ChannelError(f"{where} no respondió a tiempo.") from None
    except ConnectionRefusedError:
        raise ChannelError(f"{where} rechazó la conexión: revisa el servidor y el puerto.") from None
    except OSError as exc:
        raise ChannelError(f"Error de red con {where} ({type(exc).__name__}).") from None
    finally:
        if server is not None:
            try:
                if delivered:
                    server.quit()  # polite goodbye only on a healthy session (it waits for the server's reply)
                else:
                    server.close()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass
