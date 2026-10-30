# NEXUS Terminal — backend image (FastAPI API + WebSocket + NEXUS engine + background services + PyChart sandbox).
# The web UI is deployed separately on Vercel (see docs/DEPLOY.md). Works on Railway, Fly.io, Render or any Docker host.
FROM python:3.13-slim

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
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt \
    && useradd --create-home --uid 10001 nexus

COPY backend/app backend/app
COPY docs docs
RUN mkdir -p /app/data /app/backend/.tmp/sandbox && chown -R nexus:nexus /app

USER nexus
WORKDIR /app/backend
EXPOSE 8080

# Railway/Render inject $PORT; Fly uses NEXUS_PORT (8080). The server refuses to listen publicly without
# NEXUS_AUTH=supabase (see app/main.py), so a misconfigured deploy fails closed.
CMD ["sh", "-c", "NEXUS_PORT=${PORT:-$NEXUS_PORT} exec python -m app.main"]
