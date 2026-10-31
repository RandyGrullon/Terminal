"""Fakes for the alerts tests: nothing is ever sent. A scriptable channel, a stand-in for app.util.http_client()
(Telegram Bot API answers), a fake smtplib.SMTP / SMTP_SSL, a clock and real-format bus events."""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.alerts.channels import ChannelError, ChannelInfo

TOKEN = "123456789:AAHkQ_fake-token-for-tests-0123456789"
CHAT = "987654321"


class Clock:
    def __init__(self, now: float):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> float:
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp()


class FakeChannel:
    """Channel with the AlertService protocol (id, label, info(get), async send(get, text, subject))."""

    def __init__(self, cid: str = "telegram", label: str = "Telegram", *, enabled: bool = True,
                 configured: bool = True, fail: str | None = None, boom: Exception | None = None):
        self.id = cid
        self.label = label
        self.enabled = enabled
        self.configured = configured
        self.fail = fail
        self.boom = boom
        self.sent: list[tuple[str, str]] = []

    def info(self, get: Any) -> ChannelInfo:
        missing = [] if self.configured else ["el token del bot"]
        return ChannelInfo(self.id, self.label, self.enabled, self.configured, missing, None, "chat 1")

    async def send(self, get: Any, text: str, subject: str = "") -> None:
        self.sent.append((text, subject))
        if self.boom is not None:
            raise self.boom
        if self.fail:
            raise ChannelError(self.fail)


class FakeResponse:
    def __init__(self, status: int, payload: Any):
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHTTP:
    """Stand-in for app.util.http_client(): answers POSTs from a script, records url + json + client kwargs."""

    def __init__(self, *responses: FakeResponse | Exception):
        self.responses = list(responses)
        self.calls: list[tuple[str, Any]] = []
        self.client_kwargs: list[dict] = []

    def __call__(self, **kw: Any) -> "FakeHTTP":
        self.client_kwargs.append(kw)
        return self

    async def __aenter__(self) -> "FakeHTTP":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: Any = None, **_kw: Any) -> FakeResponse:
        self.calls.append((url, json))
        item = self.responses.pop(0) if self.responses else FakeResponse(200, {"ok": True, "result": {}})
        if isinstance(item, Exception):
            raise item
        return item


def tg_ok() -> FakeResponse:
    return FakeResponse(200, {"ok": True, "result": {"message_id": 1}})


def tg_error(code: int, description: str, **params: Any) -> FakeResponse:
    body: dict = {"ok": False, "error_code": code, "description": description}
    if params:
        body["parameters"] = params
    return FakeResponse(code, body)


class FakeSMTP:
    """Fake smtplib.SMTP / SMTP_SSL: records every step; `fail_at` raises `error` at that step."""

    instances: list["FakeSMTP"] = []
    fail_at: str | None = None
    error: BaseException | None = None
    ssl_mode = False

    def __init__(self, host: str, port: int, timeout: float | None = None, context: Any = None, **_kw: Any):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.steps: list[tuple] = [("connect", host, port, self.ssl_mode)]
        self.messages: list[Any] = []
        FakeSMTP.instances.append(self)
        self._maybe_fail("connect")

    def _maybe_fail(self, step: str) -> None:
        if FakeSMTP.fail_at == step and FakeSMTP.error is not None:
            raise FakeSMTP.error

    def ehlo(self) -> None:
        self.steps.append(("ehlo",))

    def starttls(self, context: Any = None) -> None:
        self.steps.append(("starttls", context))
        self._maybe_fail("starttls")

    def login(self, user: str, password: str) -> None:
        self.steps.append(("login", user, password))
        self._maybe_fail("login")

    def send_message(self, msg: Any, from_addr: str | None = None, to_addrs: Any = None) -> dict:
        self.steps.append(("send", from_addr, list(to_addrs or [])))
        self._maybe_fail("send")
        self.messages.append(msg)
        return {}

    def quit(self) -> None:
        self.steps.append(("quit",))

    def close(self) -> None:
        self.steps.append(("close",))

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.fail_at = None
        cls.error = None


class FakeSMTPSSL(FakeSMTP):
    ssl_mode = True


# ----------------------------------------------------------------------------- bus events in the real formats


def ev(source: str, level: str, message: str, data: Any, eid: int = 1, ts: int = 1_790_000_000_000) -> dict:
    return {"id": eid, "ts": ts, "level": level, "source": source, "message": message, "data": data}


def macro_event(level: str = "red", kind: str = "event_soon",
                message: str = "🔴 Decisión de tipos de la Fed en 15 min: no abras posiciones nuevas.") -> dict:
    return ev("macro", "success" if level == "green" else "warn", message,
              {"alert": {"kind": kind, "level": level, "message": message}, "level": level, "scope": "global"})


def signal_event(symbol: str = "BINANCE:BTCUSDT", tf: str = "1h", action: str = "BUY", eid: int = 2) -> dict:
    label = {"BUY": "COMPRAR", "SELL": "VENDER", "EXIT_LONG": "CERRAR LARGO", "EXIT_SHORT": "CERRAR CORTO"}[action]
    tail = "score +0,52 · p↑ 64 % · Filtro: TOMAR 78/100" if action in ("BUY", "SELL") else "Cerrar ahora: giro"
    return ev("nexus", "signal", f"{symbol} {tf} → {label} · {tail}", {"symbol": symbol, "tf": tf, "action": action},
              eid)


def early_exit_event(symbol: str = "BINANCE:ETHUSDT", tf: str = "1h") -> dict:
    return ev("nexus", "warn", f"⚠ {symbol} {tf}: Cerrar ahora: el valor esperado de mantener es negativo",
              {"symbol": symbol, "tf": tf, "code": "EARLY_EXIT"}, 3)


def paper_open_event(pid: int = 7, account: int = 1, symbol: str = "BINANCE:BTCUSDT") -> dict:
    return ev("paper", "trade", f"Abierto {symbol} largo 0,01 @ 65.000 · SL 64.000 · TP1 66.000",
              {"position_id": pid, "account_id": account, "symbol": symbol, "side": "long"}, 4)


def paper_close_event(pid: int = 7, reason: str = "SL", pnl: float = -98.12, account: int = 1) -> dict:
    label = {"SL": "Stop loss", "TRAIL": "Stop dinámico", "TP3": "Objetivo 3", "MANUAL": "Manual"}[reason]
    return ev("paper", "trade", f"Cerrado BINANCE:BTCUSDT largo -1,00 R (-0,98 % · {pnl}) · {label}",
              {"position_id": pid, "account_id": account, "pnl": pnl, "r_multiple": -1.0, "reason": reason}, 5)
