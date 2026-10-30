"""Optional authentication for cloud deployments (Supabase Auth).

NEXUS_AUTH=supabase enables it. Every /api request (except /api/health, /api/auth/config and CORS preflights) and
every /ws connection must carry a valid Supabase access token: `Authorization: Bearer <jwt>` for HTTP and
`/ws?token=<jwt>` for the WebSocket (browsers cannot set headers on WebSockets). Tokens are validated against
Supabase itself (GET /auth/v1/user, so signing-key rotation and revocation just work), cached briefly, and
restricted to NEXUS_ALLOWED_EMAILS when set.

Local mode (default, NEXUS_AUTH=none) has no authentication because the server only listens on 127.0.0.1.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

from app.util import TTLCache, http_client

PUBLIC_PATHS = ("/api/health", "/api/auth/config")


def _jwt_exp(token: str) -> float | None:
    """Expiry claim of a JWT (not verified — only used to bound the cache lifetime)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


class SupabaseVerifier:
    def __init__(self, url: str, anon_key: str, allowed_emails: list[str] | None = None, ttl_s: float = 300.0):
        self.url = url.rstrip("/")
        self.anon_key = anon_key
        self.allowed = {e.strip().lower() for e in (allowed_emails or []) if e.strip()}
        self.ttl_s = ttl_s
        self._ok = TTLCache(ttl_s, maxsize=256)
        self._bad = TTLCache(15, maxsize=2048)
        self._client: Any = None

    async def verify(self, token: str | None) -> dict | None:
        if not token or len(token) > 8192:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        hit = self._ok.get(key)
        if hit is not None:
            return hit
        if self._bad.get(key):
            return None
        if self._client is None:
            self._client = http_client(timeout=10.0)
        try:
            r = await self._client.get(
                f"{self.url}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": self.anon_key},
            )
        except Exception:
            return None  # network problems are not cached
        if r.status_code != 200:
            self._bad.set(key, True)
            return None
        data = r.json()
        email = str(data.get("email") or "").lower()
        if self.allowed and email not in self.allowed:
            self._bad.set(key, True)
            return None
        user = {"id": data.get("id"), "email": email}
        exp = _jwt_exp(token)
        ttl = self.ttl_s if exp is None else max(5.0, min(self.ttl_s, exp - time.time()))
        self._ok.set(key, user, ttl=ttl)
        return user

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _header(scope: dict, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def _bearer(scope: dict) -> str | None:
    value = _header(scope, b"authorization")
    if value and value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return None


def _query_token(scope: dict) -> str | None:
    qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    values = qs.get("token") or []
    return values[0] if values else None


class AuthMiddleware:
    """Pure ASGI middleware (covers HTTP and WebSocket). Sets scope['state']['user'] when authenticated."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        verifier: SupabaseVerifier,
        public_paths: tuple[str, ...] = PUBLIC_PATHS,
    ):
        self.app = app
        self.verifier = verifier
        self.public = set(public_paths)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        stype = scope.get("type")
        if stype == "http":
            path = scope.get("path", "")
            if path.startswith("/api") and scope.get("method") != "OPTIONS":
                token = _bearer(scope)
                user = await self.verifier.verify(token) if token else None
                scope.setdefault("state", {})["user"] = user
                if user is None and path not in self.public:
                    body = json.dumps({"detail": "No autorizado: inicia sesión en NEXUS."}).encode()
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return
        elif stype == "websocket":
            token = _query_token(scope)
            user = await self.verifier.verify(token) if token else None
            if user is None:
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 4401, "reason": "No autorizado"})
                return
            scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)
