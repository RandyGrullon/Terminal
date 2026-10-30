"""NEXUS Terminal server: FastAPI app factory + lifespan that boots the DB and every service.

Run:  python -m app.main            (from backend/, uses NEXUS_HOST / NEXUS_PORT)
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import config
from app.db import open_database
from app.events import EventBus
from app.runtime import rt
from app.settings import SettingsStore
from app.ws import WSManager

# (attribute on rt, module, class) — constructed in this order, started in this order, stopped in reverse.
SERVICES: list[tuple[str, str, str]] = [
    ("hub", "app.data.hub", "MarketDataHub"),
    ("nexus", "app.engine.service", "NexusService"),
    ("scanner", "app.sim.scanner", "ScannerService"),
    ("paper", "app.sim.paper", "PaperEngine"),
    ("evaluator", "app.sim.evaluator", "PredictionEvaluator"),
    ("sim", "app.sim.simulator", "Simulator"),
    ("scripts", "app.scripting.service", "ScriptService"),
    ("maintenance", "app.maintenance", "Maintenance"),
]
ROUTERS = ["app.api.system", "app.api.market", "app.api.nexus", "app.api.sim", "app.api.scripts"]
COMMAND_MODULES = ["app.terminal", "app.data.commands", "app.engine.commands", "app.sim.commands", "app.scripting.commands"]


def _import(modname: str):
    try:
        return importlib.import_module(modname), None
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return None, f"{modname}: {detail}"


def _record_error(err: str) -> None:
    if err not in rt.load_errors:
        rt.load_errors.append(err)
    if rt.bus is not None:
        rt.bus.emit("error", "system", f"Módulo no disponible → {err}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    rt.started_at = time.time()
    rt.db = await open_database()
    await rt.db.init_schema()
    rt.settings = SettingsStore(rt.db)
    await rt.settings.load()
    rt.ws = WSManager(rt)
    rt.bus = EventBus(console=os.environ.get("NEXUS_CONSOLE_EVENTS", "1") != "0")
    await rt.bus.start(rt.db)
    db_kind = "PostgreSQL" if rt.db.dialect == "postgres" else "SQLite"
    auth = "login Supabase" if config.AUTH_MODE == "supabase" else "sin login (solo local)"
    rt.bus.emit("system", "system", f"Iniciando {config.APP_NAME} v{config.VERSION} · {db_kind} {rt.db.path} · {auth}")
    for err in list(rt.load_errors):  # router import errors collected in create_app()
        rt.bus.emit("error", "system", f"Módulo no disponible → {err}")

    for modname in COMMAND_MODULES:
        _, err = _import(modname)
        if err:
            _record_error(err)

    instances: list[tuple[str, object]] = []
    for attr, modname, clsname in SERVICES:
        module, err = _import(modname)
        if module is None:
            _record_error(err)
            continue
        try:
            inst = getattr(module, clsname)(rt)
        except Exception as exc:
            _record_error(f"{modname}.{clsname}: {type(exc).__name__}: {exc}")
            continue
        setattr(rt, attr, inst)
        instances.append((attr, inst))
    rt.services = instances

    for attr, inst in instances:
        try:
            await inst.start()
        except Exception as exc:
            rt.bus.emit("error", "system", f"El servicio '{attr}' no pudo iniciar: {exc}", {"trace": traceback.format_exc(limit=8)})

    mode = " · sin conexión" if config.OFFLINE else ""
    rt.bus.emit(
        "success",
        "system",
        f"NEXUS listo en http://{config.HOST}:{config.PORT} · {len(instances)}/{len(SERVICES)} servicios{mode}",
    )
    try:
        yield
    finally:
        for attr, inst in reversed(instances):
            try:
                await asyncio.wait_for(inst.stop(), timeout=8)
            except Exception:
                pass
        rt.bus.emit("system", "system", "NEXUS detenido")
        await rt.bus.stop()
        await rt.db.close()
        if rt.auth is not None:
            await rt.auth.close()


async def ws_endpoint(websocket: WebSocket) -> None:
    await rt.ws.endpoint(websocket)


_NOT_BUILT = """<!doctype html><html lang="es"><meta charset="utf-8"><title>NEXUS Terminal</title>
<body style="background:#0b0e14;color:#d1d4dc;font-family:system-ui;padding:40px">
<h1>NEXUS Terminal</h1><p>La API está funcionando, pero la interfaz no está compilada.</p>
<p>Ejecuta <code>start.ps1</code> (o <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>)
y recarga esta página. Documentación de la API: <a style="color:#2962ff" href="/api/docs">/api/docs</a></p></body></html>"""


def _mount_frontend(app: FastAPI) -> None:
    dist = config.FRONTEND_DIST.resolve()
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/") or full_path == "api" or full_path.startswith("ws"):
            raise HTTPException(status_code=404, detail="Ruta no encontrada")
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and dist in candidate.parents:
                return FileResponse(str(candidate))
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(str(index), headers={"Cache-Control": "no-cache"})
        return HTMLResponse(_NOT_BUILT)


def _allowed_hosts() -> list[str]:
    extra = [h.strip() for h in os.environ.get("NEXUS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if config.HOST not in config.LOOPBACK_HOSTS:
        # Public deployment (e.g. 0.0.0.0 in a container): authentication protects it, see main()/AuthMiddleware.
        return extra or ["*"]
    return ["127.0.0.1", "localhost", "::1", "[::1]", "testserver", *extra]


def create_app() -> FastAPI:
    if config.AUTH_MODE not in ("none", "supabase"):
        raise RuntimeError(f"NEXUS_AUTH no válido: {config.AUTH_MODE!r} (usa 'none' o 'supabase')")
    if config.AUTH_MODE == "supabase" and not (config.SUPABASE_URL and config.SUPABASE_ANON_KEY):
        raise RuntimeError("NEXUS_AUTH=supabase requiere NEXUS_SUPABASE_URL y NEXUS_SUPABASE_ANON_KEY")
    app = FastAPI(
        title=config.APP_NAME,
        version=config.VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    # Middleware order (last added = outermost): CORS -> Auth -> TrustedHost -> app.
    # TrustedHost protects the local API against DNS rebinding; Auth guards public (cloud) deployments.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())
    if config.AUTH_MODE == "supabase":
        from app.auth import AuthMiddleware, SupabaseVerifier

        rt.auth = SupabaseVerifier(config.SUPABASE_URL, config.SUPABASE_ANON_KEY, config.ALLOWED_EMAILS)
        app.add_middleware(AuthMiddleware, verifier=rt.auth)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", *config.CORS_ORIGINS],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    rt.load_errors.clear()
    for modname in ROUTERS:
        module, err = _import(modname)
        if module is None:
            rt.load_errors.append(err)
            continue
        router = getattr(module, "router", None)
        if router is None:
            rt.load_errors.append(f"{modname}: no define `router`")
            continue
        app.include_router(router)

    app.add_api_websocket_route("/ws", ws_endpoint)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        if rt.bus is not None:
            rt.bus.emit(
                "error",
                "system",
                f"Error interno en {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
                {"trace": traceback.format_exc(limit=8)},
            )
        return JSONResponse(status_code=500, content={"detail": f"Error interno: {type(exc).__name__}: {exc}"})

    _mount_frontend(app)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    try:  # UTF-8 console output on Windows (Spanish text, arrows, check marks)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import colorama

            colorama.just_fix_windows_console()
        except Exception:
            os.system("")
    if config.HOST not in config.LOOPBACK_HOSTS and config.AUTH_MODE == "none" and os.environ.get("NEXUS_ALLOW_INSECURE") != "1":
        print(
            "\n  ERROR: el servidor escucharía en una interfaz pública sin login.\n"
            "  Configura NEXUS_AUTH=supabase (ver docs/DEPLOY.md) o usa NEXUS_HOST=127.0.0.1.\n"
            "  (Solo si sabes lo que haces: NEXUS_ALLOW_INSECURE=1)\n",
            flush=True,
        )
        sys.exit(2)
    print(f"\n  NEXUS Terminal v{config.VERSION} → http://{config.HOST}:{config.PORT}\n", flush=True)
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="warning",
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("NEXUS_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
