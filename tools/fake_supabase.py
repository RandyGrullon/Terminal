"""Minimal fake of Supabase Auth (GoTrue REST) for end-to-end tests of cloud mode without a real project.

    backend/.venv/Scripts/python.exe tools/fake_supabase.py --port 8455 --user demo@nexus.local:demo1234 --user otro@nexus.local:otro1234

Then run the backend with:
    NEXUS_AUTH=supabase NEXUS_SUPABASE_URL=http://127.0.0.1:8455 NEXUS_SUPABASE_ANON_KEY=fake-anon
    NEXUS_ALLOWED_EMAILS=demo@nexus.local
Implements: POST /auth/v1/token?grant_type=password|refresh_token, GET /auth/v1/user, POST /auth/v1/logout.
Tokens are random opaque strings (not JWTs) that expire after --ttl seconds.
"""
from __future__ import annotations

import argparse
import secrets
import time

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

USERS: dict[str, str] = {}
ACCESS: dict[str, tuple[str, float]] = {}  # token -> (email, expires_at)
REFRESH: dict[str, str] = {}  # refresh token -> email
TTL = 3600

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _session(email: str) -> dict:
    access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    expires_at = time.time() + TTL
    ACCESS[access] = (email, expires_at)
    REFRESH[refresh] = email
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": TTL,
        "expires_at": int(expires_at),
        "refresh_token": refresh,
        "user": {"id": f"user-{email}", "email": email},
    }


@app.post("/auth/v1/token")
async def token(request: Request, grant_type: str, apikey: str | None = Header(default=None)):
    if not apikey:
        return JSONResponse({"msg": "No API key found in request"}, status_code=401)
    body = await request.json()
    if grant_type == "password":
        email = str(body.get("email", "")).lower()
        if USERS.get(email) != body.get("password"):
            return JSONResponse({"code": 400, "error_code": "invalid_credentials", "msg": "Invalid login credentials"}, status_code=400)
        return _session(email)
    if grant_type == "refresh_token":
        email = REFRESH.pop(str(body.get("refresh_token", "")), None)
        if not email:
            return JSONResponse({"code": 400, "error_code": "refresh_token_not_found", "msg": "Invalid Refresh Token"}, status_code=400)
        return _session(email)
    return JSONResponse({"msg": "unsupported grant_type"}, status_code=400)


@app.get("/auth/v1/user")
async def user(authorization: str | None = Header(default=None)):
    token = (authorization or "")[7:] if (authorization or "").lower().startswith("bearer ") else ""
    item = ACCESS.get(token)
    if not item or item[1] < time.time():
        return JSONResponse({"code": 401, "msg": "invalid JWT"}, status_code=401)
    return {"id": f"user-{item[0]}", "email": item[0]}


@app.post("/auth/v1/logout")
async def logout(authorization: str | None = Header(default=None)):
    token = (authorization or "")[7:]
    ACCESS.pop(token, None)
    return Response(status_code=204)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8455)
    ap.add_argument("--ttl", type=int, default=3600)
    ap.add_argument("--user", action="append", default=[], help="email:password (repeatable)")
    args = ap.parse_args()
    TTL = args.ttl
    for spec in args.user or ["demo@nexus.local:demo1234"]:
        email, _, password = spec.partition(":")
        USERS[email.lower()] = password
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
