#!/usr/bin/env bash
# NEXUS Terminal launcher for Linux/macOS: prepares the environment (once), builds the UI when it changes,
# then starts the server (local SQLite DB + every service). Usage: ./start.sh [--dev] [--port N] [--no-browser]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$BACKEND/.venv"
PY="$VENV/bin/python"
PORT=8420
DEV=0
OPEN=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV=1 ;;
    --port) PORT="$2"; shift ;;
    --no-browser) OPEN=0 ;;
    *) echo "Opción desconocida: $1"; exit 1 ;;
  esac
  shift
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$1"; exit 1; }
sha() { (command -v sha256sum >/dev/null && sha256sum "$@" || shasum -a 256 "$@") | awk '{print $1}'; }

if [[ ! -x "$PY" ]]; then
  step "Creando entorno virtual de Python"
  if command -v uv >/dev/null; then uv venv "$VENV" --python 3.13 || uv venv "$VENV";
  elif command -v python3 >/dev/null; then python3 -m venv "$VENV";
  else fail "Python 3.11+ no encontrado"; fi
fi

REQ_HASH="$(sha "$BACKEND/requirements.txt")"
if [[ "$(cat "$VENV/.req-hash" 2>/dev/null || true)" != "$REQ_HASH" ]]; then
  step "Instalando dependencias de Python"
  if command -v uv >/dev/null; then
    uv pip install --python "$PY" -r "$BACKEND/requirements.txt"
  else
    "$PY" -m pip install --disable-pip-version-check -r "$BACKEND/requirements.txt"
  fi
  echo "$REQ_HASH" > "$VENV/.req-hash"
fi

if command -v node >/dev/null; then
  LOCK_HASH="$(sha "$FRONTEND/package-lock.json" 2>/dev/null || echo none)"
  if [[ ! -d "$FRONTEND/node_modules" || "$(cat "$FRONTEND/node_modules/.lock-hash" 2>/dev/null || true)" != "$LOCK_HASH" ]]; then
    step "Instalando dependencias de la interfaz"
    (cd "$FRONTEND" && npm ci --no-audit --no-fund)
    echo "$LOCK_HASH" > "$FRONTEND/node_modules/.lock-hash"
  fi
  if [[ $DEV -eq 0 ]]; then
    BUILD_HASH="$(cd "$FRONTEND" && find src index.html vite.config.ts package.json tsconfig.json -type f -exec cksum {} + | sort | sha /dev/stdin)"
    if [[ ! -f "$FRONTEND/dist/index.html" || "$(cat "$FRONTEND/dist/.build-hash" 2>/dev/null || true)" != "$BUILD_HASH" ]]; then
      step "Compilando la interfaz web"
      (cd "$FRONTEND" && npx vite build)
      echo "$BUILD_HASH" > "$FRONTEND/dist/.build-hash"
    fi
  fi
elif [[ ! -f "$FRONTEND/dist/index.html" ]]; then
  fail "Node.js 18+ no encontrado y la interfaz no está compilada"
fi

export NEXUS_PORT="$PORT" PYTHONUTF8=1
URL="http://127.0.0.1:$PORT"
if [[ $DEV -eq 1 ]]; then
  step "Modo desarrollo: Vite en http://127.0.0.1:5173"
  (cd "$FRONTEND" && NEXUS_BACKEND="$URL" npm run dev) &
  URL="http://127.0.0.1:5173"
fi

if [[ $OPEN -eq 1 ]]; then
  (
    for _ in $(seq 1 120); do
      if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        (command -v xdg-open >/dev/null && xdg-open "$URL") || (command -v open >/dev/null && open "$URL") || true
        break
      fi
      sleep 0.5
    done
  ) &
fi

step "Iniciando NEXUS en $URL (Ctrl+C para detener)"
cd "$BACKEND"
exec "$PY" -m app.main
