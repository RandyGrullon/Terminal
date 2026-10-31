"""Open WebSockets are re-checked in cloud mode (finding ws-auth-4401-and-no-revalidation): closed with 4401 when the
token expires or Supabase stops accepting it, kept open during a Supabase outage, and extended by an
{"type": "auth"} message carrying the refreshed token of the same user. Driven through WSManager.endpoint with a
fake socket (every wait has a timeout, nothing touches the network)."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from app import ws as ws_mod
from app.auth import VerifierUnavailable
from app.ws import WSManager

OWNER = {"id": "u-0", "email": "owner@example.com"}


def make_jwt(exp: float, sub: str = "u-0") -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part({'exp': exp, 'sub': sub})}.signature"


class FakeSocket:
    def __init__(self, token: str, user: dict):
        self.headers: dict = {}
        self.query_params = {"token": token}
        self.scope = {"state": {"user": user}}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed: tuple[int, str | None] | None = None

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise WebSocketDisconnect(code=self.closed[0] if self.closed else 1000)
        return item

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self.closed is None:
            self.closed = (code, reason)
        self.inbox.put_nowait(None)


class FakeVerifier:
    def __init__(self, users: dict[str, dict]):
        self.users = users
        self.down = False
        self.calls: list[tuple[str, bool]] = []

    async def verify(self, token, *, fresh: bool = False):
        self.calls.append((token, fresh))
        if self.down:
            raise VerifierUnavailable("ConnectError")
        return self.users.get(token)


def _manager(verifier) -> WSManager:
    return WSManager(SimpleNamespace(auth=verifier, bus=None, hub=None))


async def _wait_closed(sock: FakeSocket, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while sock.closed is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def test_socket_closed_with_4401_when_token_expires():
    async def scenario():
        token = make_jwt(time.time() + 0.3)
        sock = FakeSocket(token, OWNER)
        task = asyncio.create_task(_manager(FakeVerifier({token: OWNER})).endpoint(sock))
        await asyncio.wait_for(task, timeout=5)  # the old code never closed it (this would time out)
        return sock

    sock = asyncio.run(scenario())
    assert sock.closed is not None and sock.closed[0] == 4401 and "caducada" in sock.closed[1]
    assert sock.sent and sock.sent[0]["type"] == "hello"


def test_socket_closed_with_4401_when_session_is_revoked(monkeypatch):
    monkeypatch.setattr(ws_mod, "AUTH_RECHECK_S", 0.05)

    async def scenario():
        verifier = FakeVerifier({"opaque-token": OWNER})
        sock = FakeSocket("opaque-token", OWNER)
        task = asyncio.create_task(_manager(verifier).endpoint(sock))
        await asyncio.sleep(0.2)
        assert sock.closed is None  # still valid: re-checks keep it open
        verifier.users.clear()  # revoked at Supabase
        await asyncio.wait_for(task, timeout=5)
        return sock, verifier

    sock, verifier = asyncio.run(scenario())
    assert sock.closed[0] == 4401 and "no válida" in sock.closed[1]
    assert verifier.calls and all(fresh for _, fresh in verifier.calls)  # bypasses the REST positive cache


def test_socket_survives_a_supabase_outage(monkeypatch):
    monkeypatch.setattr(ws_mod, "AUTH_RECHECK_S", 0.05)

    async def scenario():
        verifier = FakeVerifier({"opaque-token": OWNER})
        verifier.down = True
        manager = _manager(verifier)
        sock = FakeSocket("opaque-token", OWNER)
        task = asyncio.create_task(manager.endpoint(sock))
        await asyncio.sleep(0.4)
        still_open = sock.closed is None and len(verifier.calls) >= 2
        sock.inbox.put_nowait(None)  # the client leaves
        await asyncio.wait_for(task, timeout=5)
        return still_open, manager

    still_open, manager = asyncio.run(scenario())
    assert still_open and manager.clients == {}


def test_auth_message_extends_the_socket_session():
    async def scenario():
        old, new = make_jwt(time.time() + 0.3), make_jwt(time.time() + 3600)
        sock = FakeSocket(old, OWNER)
        task = asyncio.create_task(_manager(FakeVerifier({old: OWNER, new: OWNER})).endpoint(sock))
        sock.inbox.put_nowait(json.dumps({"type": "auth", "token": new}))
        await asyncio.sleep(0.6)  # past the old token's expiry
        alive = sock.closed is None
        sock.inbox.put_nowait(None)
        await asyncio.wait_for(task, timeout=5)
        return alive

    assert asyncio.run(scenario())


def test_auth_message_with_another_users_token_closes_the_socket():
    async def scenario():
        mine, theirs = make_jwt(time.time() + 3600), make_jwt(time.time() + 3600, sub="u-9")
        sock = FakeSocket(mine, OWNER)
        verifier = FakeVerifier({mine: OWNER, theirs: {"id": "u-9", "email": "x@example.com"}})
        task = asyncio.create_task(_manager(verifier).endpoint(sock))
        sock.inbox.put_nowait(json.dumps({"type": "auth", "token": theirs}))
        await asyncio.wait_for(task, timeout=5)
        return sock

    sock = asyncio.run(scenario())
    assert sock.closed[0] == 4401


def test_local_mode_has_no_session_watchdog():
    async def scenario():
        manager = WSManager(SimpleNamespace(auth=None, bus=None, hub=None))
        sock = FakeSocket("", {})
        task = asyncio.create_task(manager.endpoint(sock))
        await asyncio.sleep(0.05)
        watch = [c.auth_watch for c in manager.clients.values()]
        sock.inbox.put_nowait(None)
        await asyncio.wait_for(task, timeout=5)
        return watch

    assert asyncio.run(scenario()) == [None]


@pytest.mark.parametrize("code", [4401, 1013])
def test_close_all_disconnects_every_client(code):
    async def scenario():
        manager = WSManager(SimpleNamespace(auth=None, bus=None, hub=None))
        socks = [FakeSocket("", {}) for _ in range(2)]
        tasks = [asyncio.create_task(manager.endpoint(s)) for s in socks]
        await asyncio.sleep(0.05)
        await manager.close_all(code, "Instancia en espera")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        return socks, manager

    socks, manager = asyncio.run(scenario())
    assert [s.closed[0] for s in socks] == [code, code] and manager.clients == {}
