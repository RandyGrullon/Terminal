# NEXUS Terminal — backend image (FastAPI API + WebSocket + NEXUS engine + background services + PyChart sandbox).
# The web UI is deployed separately on Vercel (see docs/DEPLOY.md). Works on Railway, Fly.io, Render or any Docker host.
# CI (.github/workflows/ci.yml, job "docker") builds it on every push and pull request and checks it on Linux:
# fail-closed start-up, /api/health on loopback, non-root user, read-only code, PyChart limits, the API contract and
# the backend test-suite inside the image (tools/ci/docker_smoke.py, tools/ci/docker_tests.py).
FROM python:3.13-slim

LABEL org.opencontainers.image.title="NEXUS Terminal (backend)" \
      org.opencontainers.image.source="https://github.com/RandyGrullon/Terminal"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PORT=8080 \
    NEXUS_DATA_DIR=/app/data \
    NEXUS_FORWARDED_ALLOW_IPS=*

WORKDIR /app
# Exact runtime versions (the ones the tests ran against; tools/lock_requirements.py) and nothing else: with --no-deps a
# package missing from the lock makes `pip check` fail the build instead of pulling an untested version. No test tools.
COPY backend/requirements.lock backend/requirements.lock
RUN pip install --no-deps -r backend/requirements.lock \
    && pip check \
    && useradd --create-home --uid 10001 nexus

COPY backend/app backend/app
COPY docs docs
# The code stays owned by root: read-only for the server and for its PyChart workers. It is compiled here because the
# workers start with `python -I` (which ignores PYTHONDONTWRITEBYTECODE) and must not write next to the code. Only the
# data folder and the sandbox scratch folder belong to the nexus user.
RUN python -m compileall -q backend/app \
    && mkdir -p /app/data /app/backend/.tmp/sandbox \
    && chown -R nexus:nexus /app/data /app/backend/.tmp

USER nexus
WORKDIR /app/backend
EXPOSE 8080

# Railway/Render inject $PORT; Fly uses NEXUS_PORT (8080). The server refuses to listen publicly without
# NEXUS_AUTH=supabase + NEXUS_ALLOWED_EMAILS, or without NEXUS_DATABASE_URL (this image has no volume: a local SQLite
# file would be lost on every deploy), so a misconfigured deploy fails closed (see app/main.py, docs/DEPLOY.md).
# `exec` makes the server PID 1, so the platform's SIGTERM reaches it and it shuts down cleanly.
CMD ["sh", "-c", "NEXUS_PORT=${PORT:-$NEXUS_PORT} exec python -m app.main"]
