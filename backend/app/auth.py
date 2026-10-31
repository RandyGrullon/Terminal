"""Optional authentication for cloud deployments (Supabase Auth).

NEXUS_AUTH=supabase enables it. Every /api request (except /api/health, /api/auth/config and CORS preflights) and
every /ws connection must carry a valid Supabase access token: `Authorization: Bearer <jwt>` for HTTP and
`/ws?token=<jwt>` for the WebSocket (browsers cannot set headers on WebSockets). Tokens are validated against
Supabase itself (GET /auth/v1/user, so signing-key rotation and revocation just work), cached briefly, and
restricted to NEXUS_ALLOWED_EMAILS (app.main refuses to start without it unless NEXUS_ALLOW_ANY_SUPABASE_USER=1).

Outcomes:
- token definitively rejected by Supabase (malformed, expired, revoked) or account outside the allowlist
  -> HTTP 401 / WebSocket close 4401. The socket is accepted first: closing before accepting turns into an HTTP 403
  handshake failure and browsers would only see code 1006.
- Supabase unreachable (network error, 5xx, 429...) -> HTTP 503 / WebSocket close 1013 ("try again later"): a
  transient outage is never reported as an invalid session, so clients keep their session and retry.
Open sockets are re-checked by app.ws (closed with 4401 when the token expires or stops being valid).

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
# Supabase answers these for a token that can never become valid (malformed, expired, revoked, deleted user).
_REJECTED_STATUS = frozenset({400, 401, 403, 404, 422})
UNAUTHORIZED_DETAIL = "No autorizado: inicia sesión en NEXUS."
UNAVAILABLE_DETAIL = "No se pudo verificar tu sesión (Supabase no responde); reintenta en unos segundos."
WS_UNAUTHORIZED = 4401  # application code: the client must refresh its session (or log in again)
WS_TRY_AGAIN = 1013  # registered "Try Again Later": transient, keep the session and reconnect with backoff


class VerifierUnavailable(Exception):
    """Supabase could not judge the token (network error, 5xx, 429, unexpected answer). Never cached."""


def jwt_exp(token: str) -> float | None:
    """Expiry claim of a JWT (not verified here: only meaningful for a token Supabase accepted)."""
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

    async def verify(self, token: str | None, *, fresh: bool = False) -> dict | None:
        """{id, email} for a valid token of an allowed account; None when the token is definitively rejected.
        Raises VerifierUnavailable when Supabase cannot answer (not cached: the next call asks again).
        fresh=True skips the positive cache (periodic re-checks of long-lived WebSockets)."""
        if not token or len(token) > 8192:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        if self._bad.get(key):
            return None
        if not fresh:
            hit = self._ok.get(key)
            if hit is not None:
                return hit
        if self._client is None:
            self._client = http_client(timeout=10.0)
        try:
            r = await self._client.get(
                f"{self.url}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": self.anon_key},
            )
        except Exception as exc:
            raise VerifierUnavailable(type(exc).__name__) from None
        if r.status_code in _REJECTED_STATUS:
            self._reject(key)
            return None
        if r.status_code != 200:  # 429, 5xx, redirects...: Supabase did not judge the token
            raise VerifierUnavailable(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise VerifierUnavailable("respuesta no válida") from None
        if not isinstance(data, dict) or not data.get("id"):
            raise VerifierUnavailable("respuesta sin usuario")
        email = str(data.get("email") or "").lower()
        if self.allowed and email not in self.allowed:
            self._reject(key)
            return None
        user = {"id": data.get("id"), "email": email}
        exp = jwt_exp(token)
        ttl = self.ttl_s if exp is None else max(5.0, min(self.ttl_s, exp - time.time()))
        self._ok.set(key, user, ttl=ttl)
        return user

    def _reject(self, key: str) -> None:
        self._ok.pop(key)
        self._bad.set(key, True)

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


async def send_json_error(send: Callable, status: int, detail: str, retry_after: int | None = None) -> None:
    """Minimal ASGI JSON error response ({"detail": ...}, like FastAPI's HTTPException)."""
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if retry_after is not None:
        headers.append((b"retry-after", str(int(retry_after)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def refuse_websocket(receive: Callable, send: Callable, code: int, reason: str) -> None:
    """Accept, then close with `code`: a close sent before accepting becomes an HTTP 403 handshake failure and the
    browser would only see 1006. Keep `reason` short (the close frame allows 123 bytes)."""
    message = await receive()
    if message.get("type") != "websocket.connect":
        return  # the client already went away
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.close", "code": code, "reason": reason})


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
                public = path in self.public
                token = _bearer(scope)
                try:
                    user = await self.verifier.verify(token) if token else None
                except VerifierUnavailable:
                    if not public:
                        await send_json_error(send, 503, UNAVAILABLE_DETAIL, retry_after=5)
                        return
                    user = None  # public endpoints still answer (anonymous view)
                scope.setdefault("state", {})["user"] = user
                if user is None and not public:
                    await send_json_error(send, 401, UNAUTHORIZED_DETAIL)
                    return
        elif stype == "websocket":
            token = _query_token(scope)
            try:
                user = await self.verifier.verify(token) if token else None
            except VerifierUnavailable:
                await refuse_websocket(receive, send, WS_TRY_AGAIN, "No se pudo verificar la sesión; reintenta")
                return
            if user is None:
                await refuse_websocket(receive, send, WS_UNAUTHORIZED, "No autorizado")
                return
            state = scope.setdefault("state", {})
            state["user"] = user
            state["token"] = token  # the token checked here: app.ws re-checks this one, not another ?token= value
        await self.app(scope, receive, send)
