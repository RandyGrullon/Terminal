"""WebSocket hub (/ws): topic fan-out to browser clients + terminal command execution.

Cloud mode (rt.auth set): the token is checked by app.auth.AuthMiddleware at the handshake, then every open socket
is re-checked: it is closed with 4401 when its token expires or Supabase stops accepting it (revoked session).
A client may send {"type": "auth", "token": <refreshed access token>} to keep the socket past the old token's expiry.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import WebSocket, WebSocketDisconnect

from app.config import VERSION
from app.util import clean_json, now_ms

DEFAULT_TOPICS = ("events", "sim", "paper", "scanner", "ui", "macro")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
AUTH_RECHECK_S = 120.0  # cloud mode: re-verify the session of every open socket this often (revocations)


def parse_market_topic(topic: str) -> tuple[str, str] | None:
    """'market:BINANCE:BTCUSDT:1h' -> ('BINANCE:BTCUSDT', '1h')."""
    if not topic.startswith("market:"):
        return None
    rest = topic[len("market:") :]
    if ":" not in rest:
        return None
    symbol, tf = rest.rsplit(":", 1)
    if not symbol or not tf:
        return None
    return symbol.upper(), tf


def market_topic(symbol: str, tf: str) -> str:
    return f"market:{symbol.upper()}:{tf}"


def origin_allowed(origin: str | None) -> bool:
    """Block cross-site WebSocket hijacking: only pages served from this machine (or the configured cloud
    frontend origins, NEXUS_CORS_ORIGINS) may connect."""
    if not origin or origin == "null":
        return origin is None
    try:
        host = urlparse(origin).hostname or ""
    except ValueError:
        return False
    if host in _LOCAL_HOSTS:
        return True
    from app import config

    return origin.rstrip("/") in config.CORS_ORIGINS


class _Client:
    def __init__(self, ws: WebSocket, cid: int):
        self.ws = ws
        self.id = cid
        self.topics: set[str] = set(DEFAULT_TOPICS)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.sender: asyncio.Task | None = None
        # cloud mode only: the session behind the socket (token, its expiry and owner) and its watchdog
        self.token: str | None = None
        self.exp: float | None = None
        self.user_id: Any = None
        self.auth_watch: asyncio.Task | None = None

    def put(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:  # slow client: drop the oldest message
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(message)
            except Exception:
                pass


class WSManager:
    def __init__(self, rt: Any):
        self.rt = rt
        self.clients: dict[int, _Client] = {}
        self._next_id = 1

    # ------------------------------------------------------------------ publish

    def publish(self, topic: str, message: dict) -> None:
        if not self.clients:
            return
        targets = [c for c in self.clients.values() if topic in c.topics]
        if not targets:
            return
        payload = clean_json(message)
        for client in targets:
            client.put(payload)

    def subscribers(self, topic: str) -> int:
        return sum(1 for c in self.clients.values() if topic in c.topics)

    def status(self) -> dict:
        return {"running": True, "clients": len(self.clients)}

    async def close_all(self, code: int, reason: str) -> None:
        """Disconnect every client (e.g. this instance went to standby); browsers reconnect on their own."""
        for client in list(self.clients.values()):
            try:
                await client.ws.close(code=code, reason=reason)
            except Exception:
                pass

    # ------------------------------------------------------------------ connection

    async def endpoint(self, websocket: WebSocket) -> None:
        if not origin_allowed(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        cid = self._next_id
        self._next_id += 1
        client = _Client(websocket, cid)
        self.clients[cid] = client
        client.sender = asyncio.create_task(self._sender(client), name=f"ws-sender-{cid}")
        if getattr(self.rt, "auth", None) is not None:
            from app.auth import jwt_exp

            state = websocket.scope.get("state") or {}
            user = state.get("user") or {}
            # the token AuthMiddleware verified: with a repeated ?token= Starlette returns the last value, which would
            # let an unverified expiry replace the real one
            client.token = state.get("token") or websocket.query_params.get("token")
            client.exp = jwt_exp(client.token or "")
            client.user_id = user.get("id") if isinstance(user, dict) else None
            client.auth_watch = asyncio.create_task(self._auth_watch(client), name=f"ws-auth-{cid}")
        client.put({"type": "hello", "version": VERSION, "server_time": now_ms(), "topics": sorted(client.topics)})
        if self.rt.bus is not None:
            self.rt.bus.emit("debug", "ws", f"Cliente #{cid} conectado")
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await self._handle(client, msg)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            if self.rt.bus is not None:
                self.rt.bus.emit("debug", "ws", f"Cliente #{cid} desconectado: {type(exc).__name__}")
        finally:
            await self._cleanup(client)

    async def _cleanup(self, client: _Client) -> None:
        self.clients.pop(client.id, None)
        for topic in list(client.topics):
            self._leave_market(client, topic)
        if client.sender:
            client.sender.cancel()
        if client.auth_watch is not None and client.auth_watch is not asyncio.current_task():
            client.auth_watch.cancel()

    # ------------------------------------------------------------------ session checks (cloud mode)

    async def _auth_watch(self, client: _Client) -> None:
        """Close the socket with 4401 when its token expires or Supabase no longer accepts it. A Supabase outage
        (VerifierUnavailable) keeps the socket open: only a definitive rejection ends the session."""
        from app.auth import WS_UNAUTHORIZED

        last_check = time.monotonic()
        while True:
            until_check = AUTH_RECHECK_S - (time.monotonic() - last_check)
            wait = until_check if client.exp is None else min(until_check, client.exp - time.time())
            if wait > 0:
                await asyncio.sleep(wait)
            if client.exp is not None and time.time() >= client.exp:
                await self._close_client(client, WS_UNAUTHORIZED, "Sesión caducada")
                return
            if time.monotonic() - last_check < AUTH_RECHECK_S:
                continue  # woke up for an expiry that a refreshed token pushed back
            last_check = time.monotonic()
            verifier = getattr(self.rt, "auth", None)
            if verifier is None:
                return
            try:
                user = await verifier.verify(client.token, fresh=True)
            except Exception:  # VerifierUnavailable: Supabase unreachable, try again on the next round
                continue
            if user is None:
                await self._close_client(client, WS_UNAUTHORIZED, "Sesión no válida")
                return

    async def _reauth(self, client: _Client, token: str) -> None:
        """{"type": "auth", "token": ...}: adopt the client's refreshed access token (same user only)."""
        verifier = getattr(self.rt, "auth", None)
        if verifier is None or not token:
            return
        from app.auth import WS_UNAUTHORIZED, jwt_exp

        try:
            user = await verifier.verify(token)
        except Exception:  # VerifierUnavailable: keep the current token; the client sends the next one later
            return
        if user is None or (client.user_id is not None and user.get("id") != client.user_id):
            await self._close_client(client, WS_UNAUTHORIZED, "Sesión no válida")
            return
        client.token, client.exp = token, jwt_exp(token)

    async def _close_client(self, client: _Client, code: int, reason: str) -> None:
        if self.rt.bus is not None:
            self.rt.bus.emit("debug", "ws", f"Cliente #{client.id} desconectado: {reason.lower()}")
        try:
            await client.ws.close(code=code, reason=reason)
        except Exception:
            pass  # already closed; the receive loop cleans up

    async def _sender(self, client: _Client) -> None:
        try:
            while True:
                msg = await client.queue.get()
                await client.ws.send_text(json.dumps(msg, separators=(",", ":"), ensure_ascii=False, default=str))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # socket closed; receive loop will clean up

    # ------------------------------------------------------------------ messages

    async def _handle(self, client: _Client, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "ping":
            client.put({"type": "pong"})
        elif mtype == "subscribe":
            topic = str(msg.get("topic") or "")
            if topic and topic not in client.topics and len(client.topics) < 200:
                client.topics.add(topic)
                self._join_market(client, topic)
        elif mtype == "unsubscribe":
            topic = str(msg.get("topic") or "")
            if topic in client.topics:
                client.topics.discard(topic)
                self._leave_market(client, topic)
        elif mtype == "command":
            asyncio.create_task(self._run_command(client, str(msg.get("id") or ""), str(msg.get("text") or "")))
        elif mtype == "auth":
            await self._reauth(client, str(msg.get("token") or ""))

    def _join_market(self, client: _Client, topic: str) -> None:
        parsed = parse_market_topic(topic)
        if parsed and self.rt.hub is not None:
            try:
                self.rt.hub.subscribe(parsed[0], parsed[1], owner=f"ws:{client.id}")
            except Exception as exc:
                if self.rt.bus is not None:
                    self.rt.bus.emit("warn", "ws", f"No se pudo suscribir a {topic}: {exc}")

    def _leave_market(self, client: _Client, topic: str) -> None:
        parsed = parse_market_topic(topic)
        if parsed and self.rt.hub is not None:
            try:
                self.rt.hub.unsubscribe(parsed[0], parsed[1], owner=f"ws:{client.id}")
            except Exception:
                pass

    async def _run_command(self, client: _Client, cmd_id: str, text: str) -> None:
        from app.terminal import execute

        result = await execute(text, client_id=client.id)
        client.put(clean_json({"type": "command_result", "id": cmd_id, **result}))
