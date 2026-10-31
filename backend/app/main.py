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
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import config
from app.auth import PUBLIC_PATHS, WS_TRY_AGAIN, refuse_websocket, send_json_error
from app.db import open_database
from app.events import EventBus
from app.runtime import rt
from app.settings import SettingsStore
from app.util import RateLimitedLog
from app.ws import WSManager

# (attribute on rt, module, class) — constructed in this order, started in this order, stopped in reverse.
SERVICES: list[tuple[str, str, str]] = [
    ("hub", "app.data.hub", "MarketDataHub"),
    ("macro", "app.macro.service", "MacroService"),
    ("nexus", "app.engine.service", "NexusService"),
    ("scanner", "app.sim.scanner", "ScannerService"),
    ("paper", "app.sim.paper", "PaperEngine"),
    ("evaluator", "app.sim.evaluator", "PredictionEvaluator"),
    ("sim", "app.sim.simulator", "Simulator"),
    ("scripts", "app.scripting.service", "ScriptService"),
    ("maintenance", "app.maintenance", "Maintenance"),
    ("alerts", "app.alerts.service", "AlertService"),
    ("validation", "app.validation.service", "ValidationService"),
]
ROUTERS = [
    "app.api.system",
    "app.api.market",
    "app.api.nexus",
    "app.api.sim",
    "app.api.scripts",
    "app.api.macro",
    "app.api.alerts",
    "app.api.validation",
]
COMMAND_MODULES = [
    "app.terminal",
    "app.data.commands",
    "app.engine.commands",
    "app.sim.commands",
    "app.scripting.commands",
    "app.macro.commands",
    "app.alerts.commands",
    "app.validation.commands",
]

# Single active instance on a shared PostgreSQL database (see Leadership)
LEADER_RETRY_S = 3.0  # standby: how often to try to take over
LEADER_CHECK_S = 5.0  # active: how often to verify that the lock connection is still alive
STANDBY_NOTICE_S = 600.0  # standby: how often to repeat the "en espera" notice in the log
STANDBY_DETAIL = (
    "Instancia en espera: otra instancia de NEXUS está activa con la misma base de datos (por ejemplo, durante un "
    "despliegue). Reintenta en unos segundos."
)
# Browser origins besides the server's own that may call the API (CORS) and send it changes (OriginCheckMiddleware):
# the Vite dev server (start.bat -Dev / start.sh --dev) and NEXUS_CORS_ORIGINS (e.g. the Vercel frontend).
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ORIGIN_LOG_EVERY_S = 300.0  # one warning per this many seconds about refused origins (never a flood of events)
# Vite names every file under dist/assets after a hash of its content: a browser may keep it without asking again. A
# new build has new names, and index.html (no-cache: revalidated on every load) always points to the current ones.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_CACHE = {"Cache-Control": "no-cache"}


def browser_origins() -> list[str]:
    return [*DEV_ORIGINS, *config.CORS_ORIGINS]


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


def _new_bus() -> EventBus:
    return EventBus(console=os.environ.get("NEXUS_CONSOLE_EVENTS", "1") != "0")


async def _start_services() -> int:
    """Construct every service (table order), then start them in the same order. Returns how many loaded."""
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
    return len(instances)


async def _stop_services(*, detach: bool = False) -> None:
    """Stop the services in reverse order. detach=True (stepping down to standby) also clears rt.<attr>, so the
    API never reaches a stopped service."""
    instances = list(rt.services)
    if detach:
        rt.services = []
    for attr, inst in reversed(instances):
        try:
            await asyncio.wait_for(inst.stop(), timeout=8)
        except Exception:
            pass
        if detach and getattr(rt, attr, None) is inst:
            setattr(rt, attr, None)


def _ready(count: int) -> None:
    mode = " · sin conexión" if config.OFFLINE else ""
    role = " · instancia activa" if rt.role == "leader" else ""
    rt.bus.emit(
        "success",
        "system",
        f"NEXUS listo en http://{config.HOST}:{config.PORT} · {count}/{len(SERVICES)} servicios{mode}{role}",
    )


def _auth_summary() -> str:
    if config.AUTH_MODE != "supabase":
        return "sin login (solo local)"
    n = len(config.ALLOWED_EMAILS)
    if not n:
        return "login Supabase (cualquier usuario del proyecto)"
    return f"login Supabase ({n} {'correo permitido' if n == 1 else 'correos permitidos'})"


def _schema_info() -> dict:
    info = getattr(rt.db, "schema_info", None)
    return info if isinstance(info, dict) else {}


def _report_schema(info: dict) -> None:
    """What init_schema did (it runs before the event bus exists): the migrations it applied now, or versions newer
    than this code (a database a newer NEXUS already migrated: it keeps working, nothing is undone)."""
    applied = ", ".join(str(label) for label in info.get("applied") or [])
    if applied:
        version = info.get("version")
        if info.get("baseline") == "created":  # a new database (or an old one missing part of the base schema)
            text = f"Base de datos preparada: esquema base y migraciones {applied} (esquema v{version})"
        else:
            text = f"Base de datos actualizada al esquema v{version}: {applied}"
        rt.bus.emit("system", "system", text, {"schema": info})
    unknown = [v for v in info.get("unknown") or [] if isinstance(v, int) and not isinstance(v, bool)]
    if unknown:
        rt.bus.emit(
            "warn",
            "system",
            f"La base de datos tiene migraciones más nuevas que esta versión de NEXUS (v{max(unknown)}): sigue "
            "funcionando, pero conviene actualizar NEXUS.",
            {"schema": info},
        )


def _server_protection(status: dict) -> str:
    """Linux only (elsewhere app.hardening is a no-op): the server's own protection, for the startup line."""
    if not str(status.get("platform", "")).startswith("linux"):
        return ""
    return f" · servidor no volcable: {'sí' if status.get('non_dumpable') is True else 'no'}"


def _report_hardening(status: dict) -> None:
    """harden_server_process() runs before the event bus exists: on Linux, a failure is logged once the bus is up."""
    if not str(status.get("platform", "")).startswith("linux") or status.get("non_dumpable") is True:
        return
    why = status.get("error") or "el sistema no aplicó el cambio"
    rt.bus.emit(
        "warn",
        "system",
        f"No se pudo marcar el proceso del servidor como no volcable ({why}): otro proceso del mismo usuario podría "
        "leer su entorno y su memoria (/proc/<pid>/environ y mem).",
        {"hardening": status},
    )


def _warn_risky_setup() -> None:
    if config.AUTH_MODE != "supabase":
        return
    if not config.ALLOWED_EMAILS:  # only reachable with NEXUS_ALLOW_ANY_SUPABASE_USER=1 (see create_app)
        rt.bus.emit(
            "warn",
            "system",
            "NEXUS_ALLOW_ANY_SUPABASE_USER=1: cualquier cuenta de tu proyecto Supabase puede entrar. Desactiva los "
            "registros en Supabase o define NEXUS_ALLOWED_EMAILS.",
        )
    if rt.db.dialect == "sqlite":
        rt.bus.emit(
            "warn",
            "system",
            "Modo nube con SQLite local (sin NEXUS_DATABASE_URL): en un contenedor los datos se pierden en cada "
            "despliegue salvo que la carpeta de datos sea un volumen persistente. Configura NEXUS_DATABASE_URL "
            "(docs/DEPLOY.md).",
        )


class Leadership:
    """Single active instance on a shared PostgreSQL database (cloud mode with background work).

    Two instances can run at once: Railway keeps the previous deployment until the new one is healthy, and Fly may
    start a second machine. Background loops, the demo account and the in-memory state must exist only once, so only
    the holder of the schema's advisory lock (app.db_pg.AdvisoryLock, dedicated connection) starts the services.
    The other instance stays in standby: /api/health answers (role "standby"), the rest gets 503 / WebSocket 1013
    (StandbyGate) and it retries the lock every LEADER_RETRY_S, taking over once the active instance stops. The
    services are never started on a standby: their start() does single-instance recovery (closing the simulator's
    positions, marking running scans as interrupted) that would hit the active instance. The active instance probes
    its lock connection every LEADER_CHECK_S and steps down when it is lost (another instance may take over)."""

    def __init__(self, lock: Any):
        self.lock = lock
        self.since = time.time()  # when the current role started
        self.takeovers = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._busy = asyncio.Lock()
        self._closing = False
        self._notice_at = time.monotonic()
        self._log = RateLimitedLog()

    async def acquire(self) -> bool:
        try:
            return await self.lock.try_acquire()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self._log.should_log("acquire", 300) and rt.bus is not None:
                rt.bus.emit("warn", "system", f"No se pudo comprobar qué instancia está activa: {self.last_error}")
            return False

    def run(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="leader-election")

    async def _loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(LEADER_CHECK_S if rt.role == "leader" else LEADER_RETRY_S)
            async with self._busy:
                if self._closing:
                    return
                try:
                    if rt.role == "leader":
                        if not await self.lock.alive():
                            await self._step_down()
                    elif await self.acquire():
                        await self._take_over()
                    else:
                        self._standby_notice()
                except Exception as exc:  # never let the election die
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if self._log.should_log("loop", 60) and rt.bus is not None:
                        rt.bus.emit("error", "system", f"Elección de instancia activa: {self.last_error}")

    def _standby_notice(self) -> None:
        if time.monotonic() - self._notice_at < STANDBY_NOTICE_S:
            return
        self._notice_at = time.monotonic()
        minutes = int((time.time() - self.since) // 60)
        rt.bus.emit(
            "warn",
            "system",
            f"Instancia en espera desde hace {minutes} min: otra instancia de NEXUS usa esta base de datos. Si no hay "
            "un despliegue en curso, deja una sola máquina (Fly: fly scale count 1; Railway: 1 réplica).",
        )

    async def _take_over(self) -> None:
        """Standby -> active. The previous instance stopped: its loops are gone and its events were flushed."""
        self.since = time.time()
        self.takeovers += 1
        # A fresh store with the values the previous instance saved while this one waited. Fresh, not reloaded: after
        # a step-down the old store still holds the settings listeners of the stopped services of the earlier term.
        settings = SettingsStore(rt.db)
        await settings.load()
        rt.settings = settings
        rt.bus = _new_bus()  # persisted from now on; event ids continue after the previous instance's last event
        await rt.bus.start(rt.db)
        rt.bus.emit("system", "system", "Esta instancia toma el relevo como instancia activa (la anterior se detuvo)")
        count = await _start_services()
        rt.role = "leader"
        _ready(count)

    async def _step_down(self) -> None:
        """Active -> standby: the lock connection was lost, so another instance may take over at any moment."""
        rt.role = "standby"  # StandbyGate refuses new work from now on
        self.since = time.time()
        self._notice_at = time.monotonic()
        rt.bus.emit(
            "error",
            "system",
            "Se perdió la conexión que garantiza que esta es la única instancia activa: se detienen los servicios "
            "de fondo y la instancia queda en espera hasta recuperarla",
        )
        await _stop_services(detach=True)
        if rt.ws is not None:
            await rt.ws.close_all(WS_TRY_AGAIN, "Instancia en espera")
        await self.lock.release()
        await rt.bus.stop()  # flush what can still be saved
        rt.bus = _new_bus()  # standby: console and memory only (its ids would clash with the active instance)

    async def close(self) -> None:
        """Stop the election loop (waits for an in-flight take-over or step-down to finish)."""
        self._closing = True
        async with self._busy:
            task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.hardening import harden_server_process, server_protections

    harden_server_process()  # OS-level protections of the server process (best effort; logged once the bus exists)
    rt.started_at = time.time()
    rt.role = "single"
    rt.db = await open_database()
    await rt.db.init_schema()
    rt.settings = SettingsStore(rt.db)
    await rt.settings.load()
    rt.ws = WSManager(rt)
    rt.bus = _new_bus()
    if config.BACKGROUND and hasattr(rt.db, "advisory_lock"):  # PostgreSQL: the database may be shared
        rt.leader = Leadership(rt.db.advisory_lock())
        rt.role = "leader" if await rt.leader.acquire() else "standby"
    if rt.role != "standby":
        await rt.bus.start(rt.db)  # a standby only logs to the console: its event ids would clash with the active one
    db_kind = "PostgreSQL" if rt.db.dialect == "postgres" else "SQLite"
    schema = _schema_info()
    protection = server_protections()
    version = f" (esquema v{schema['version']})" if schema.get("version") is not None else ""
    rt.bus.emit(
        "system",
        "system",
        f"Iniciando {config.APP_NAME} v{config.VERSION} · {db_kind} {rt.db.path}{version} · {_auth_summary()}"
        f"{_server_protection(protection)}",
    )
    _report_schema(schema)
    _report_hardening(protection)
    _warn_risky_setup()
    for err in list(rt.load_errors):  # router import errors collected in create_app()
        rt.bus.emit("error", "system", f"Módulo no disponible → {err}")

    for modname in COMMAND_MODULES:
        _, err = _import(modname)
        if err:
            _record_error(err)

    if rt.role == "standby":
        rt.bus.emit(
            "warn",
            "system",
            "Instancia en espera: otra instancia de NEXUS ya ejecuta los servicios con esta base de datos. Esta solo "
            "responde /api/health y tomará el relevo cuando la otra se detenga (p. ej. al terminar un despliegue).",
        )
    else:
        _ready(await _start_services())
    if rt.leader is not None:
        rt.leader.run()
    try:
        yield
    finally:
        if rt.leader is not None:
            await rt.leader.close()
        await _stop_services()
        rt.bus.emit("system", "system", "NEXUS detenido")
        await rt.bus.stop()
        if rt.leader is not None:
            await rt.leader.lock.release()  # only now may another instance start its services
        await rt.db.close()
        if rt.auth is not None:
            await rt.auth.close()


async def ws_endpoint(websocket: WebSocket) -> None:
    await rt.ws.endpoint(websocket)


_NOT_BUILT = """<!doctype html><html lang="es"><meta charset="utf-8"><title>NEXUS Terminal</title>
<body style="background:#0b0e14;color:#d1d4dc;font-family:system-ui;padding:40px">
<h1>NEXUS Terminal</h1><p>La API está funcionando, pero la interfaz no está compilada.</p>
<p>Ejecuta <code>start.bat</code> (o <code>bash start.sh</code>, o <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>)
y recarga esta página. Documentación de la API: <a style="color:#2962ff" href="/api/docs">/api/docs</a></p></body></html>"""


class HashedAssets(StaticFiles):
    """dist/assets of the built UI, cached by the browser for a year without revalidation (ASSET_CACHE_CONTROL; also
    on a 304). A missing file is a plain 404, never cached like that."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = ASSET_CACHE_CONTROL
        return response


def _mount_frontend(app: FastAPI) -> None:
    dist = config.FRONTEND_DIST.resolve()
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", HashedAssets(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/") or full_path == "api" or full_path.startswith("ws"):
            raise HTTPException(status_code=404, detail="Ruta no encontrada")
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and dist in candidate.parents:
                # index.html (and any page) revalidated on every load: it names the current hashed assets
                return FileResponse(str(candidate), headers=NO_CACHE if candidate.suffix == ".html" else None)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(str(index), headers=NO_CACHE)
        return HTMLResponse(_NOT_BUILT, headers=NO_CACHE)


_VALIDATION_ES = {
    "missing": "falta este campo",
    "int_parsing": "debe ser un número entero",
    "int_type": "debe ser un número entero",
    "int_from_float": "debe ser un número entero",
    "float_parsing": "debe ser un número",
    "float_type": "debe ser un número",
    "string_type": "debe ser texto",
    "bool_parsing": "debe ser verdadero o falso",
    "bool_type": "debe ser verdadero o falso",
    "list_type": "debe ser una lista",
    "dict_type": "debe ser un objeto",
    "json_invalid": "JSON no válido",
    "literal_error": "valor no permitido",
    "enum": "valor no permitido",
    "string_too_short": "texto demasiado corto",
    "string_too_long": "texto demasiado largo",
}


def _validation_es(err: dict) -> str:
    kind = str(err.get("type", ""))
    ctx = err.get("ctx") or {}
    if kind in ("greater_than_equal", "greater_than"):
        return f"debe ser ≥ {ctx.get('ge', ctx.get('gt'))}"
    if kind in ("less_than_equal", "less_than"):
        return f"debe ser ≤ {ctx.get('le', ctx.get('lt'))}"
    return _VALIDATION_ES.get(kind, str(err.get("msg", "valor no válido")))


def _allowed_hosts() -> list[str]:
    extra = [h.strip() for h in os.environ.get("NEXUS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if config.HOST not in config.LOOPBACK_HOSTS:
        # Public deployment (e.g. 0.0.0.0 in a container): authentication protects it, see main()/AuthMiddleware.
        return extra or ["*"]
    return ["127.0.0.1", "localhost", "::1", "[::1]", "testserver", *extra]


def _host_check_exempt() -> tuple[str, ...]:
    # Platform health checks use their own Host header (Railway: healthcheck.railway.app, Fly: the machine address),
    # so on a public bind /api/health (anonymous view: no data) skips the NEXUS_ALLOWED_HOSTS check. Locally every
    # path stays checked (DNS rebinding protection).
    return ("/api/health",) if config.HOST not in config.LOOPBACK_HOSTS else ()


class HostCheckMiddleware:
    """TrustedHostMiddleware except for `exempt_paths` (HTTP only)."""

    def __init__(self, app: Callable, allowed_hosts: list[str], exempt_paths: tuple[str, ...] = ()):
        self.app = app
        self.checked = TrustedHostMiddleware(app, allowed_hosts=allowed_hosts)
        self.exempt = frozenset(exempt_paths)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") == "http" and scope.get("path") in self.exempt:
            await self.app(scope, receive, send)
            return
        await self.checked(scope, receive, send)


class StandbyGate:
    """While this instance is in standby (another one holds the leader lock, see Leadership) only /api/health and
    /api/auth/config answer: the rest of the API gets 503 and WebSockets close with 1013, so clients retry (and reach
    the active instance) instead of using services that do not run here."""

    def __init__(self, app: Callable, open_paths: tuple[str, ...] = PUBLIC_PATHS):
        self.app = app
        self.open = frozenset(open_paths)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if rt.role == "standby":
            stype = scope.get("type")
            path = scope.get("path", "")
            if stype == "websocket":
                await refuse_websocket(receive, send, WS_TRY_AGAIN, "Instancia en espera; reintenta")
                return
            if stype == "http" and path.startswith("/api") and path not in self.open and scope.get("method") != "OPTIONS":
                await send_json_error(send, 503, STANDBY_DETAIL, retry_after=5)
                return
        await self.app(scope, receive, send)


def _scope_header(scope: dict, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def origin_key(origin: str) -> str | None:
    """'scheme://host[:port]' (lower case) of a browser Origin header; None when it is not one ("null" from a
    sandboxed frame or a file:// page, a path, credentials...)."""
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return None
    if (parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def _printable(text: str, limit: int = 120) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in text[:limit])


class OriginCheckMiddleware:
    """CSRF protection for the requests that change something (POST / PUT / PATCH / DELETE). A browser always sends
    Origin with a cross-site one, also with the "simple" requests that CORS never preflights (an HTML form, text/plain,
    no body), which would otherwise reach the endpoint even though the page cannot read the answer. Such a request
    whose Origin is neither this server's own (the Host it was sent to) nor one of `allowed_origins` (DEV_ORIGINS and
    NEXUS_CORS_ORIGINS) gets 403 before any endpoint runs. Requests without Origin (the terminal client, curl, scripts,
    tests) pass: they do not come from a web page. HTTP only (app.ws checks the WebSocket's Origin itself)."""

    def __init__(self, app: Callable, allowed_origins: Iterable[str] = ()):
        self.app = app
        self.allowed = frozenset(key for key in (origin_key(o) for o in allowed_origins) if key)
        self._log = RateLimitedLog()

    def allows(self, origin: str, host: str | None) -> bool:
        key = origin_key(origin)
        if key is None:
            return False
        if key in self.allowed:
            return True
        return bool(host) and key.split("://", 1)[1] == host.strip().lower()  # the page was served by this server

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") == "http" and scope.get("method") in UNSAFE_METHODS:
            origin = _scope_header(scope, b"origin")
            if origin is not None and not self.allows(origin, _scope_header(scope, b"host")):
                shown = _printable(origin)
                if rt.bus is not None and self._log.should_log("origin", ORIGIN_LOG_EVERY_S):
                    rt.bus.emit(
                        "warn",
                        "system",
                        f"Petición {scope.get('method')} {_printable(str(scope.get('path', '')), 80)} rechazada: "
                        f"origen no permitido ({shown}), protección CSRF. Si es tu interfaz, añade ese origen a "
                        "NEXUS_CORS_ORIGINS.",
                    )
                await send_json_error(
                    send,
                    403,
                    f"Origen no permitido ({shown}): por seguridad (CSRF), NEXUS solo acepta cambios desde su propia "
                    "interfaz o desde un origen de NEXUS_CORS_ORIGINS.",
                )
                return
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    if config.AUTH_MODE not in ("none", "supabase"):
        raise RuntimeError(f"NEXUS_AUTH no válido: {config.AUTH_MODE!r} (usa 'none' o 'supabase')")
    if config.AUTH_MODE == "supabase" and not (config.SUPABASE_URL and config.SUPABASE_ANON_KEY):
        raise RuntimeError("NEXUS_AUTH=supabase requiere NEXUS_SUPABASE_URL y NEXUS_SUPABASE_ANON_KEY")
    if config.AUTH_MODE == "supabase" and not config.ALLOWED_EMAILS and not config.ALLOW_ANY_SUPABASE_USER:
        # Fail closed: without the allowlist any account of the Supabase project (sign-ups are on by default) gets in.
        raise RuntimeError(
            "NEXUS_AUTH=supabase requiere NEXUS_ALLOWED_EMAILS (correos con acceso, separados por comas): sin esa "
            "lista cualquier cuenta de tu proyecto Supabase podría entrar. Si de verdad quieres aceptar a cualquier "
            "usuario del proyecto, define NEXUS_ALLOW_ANY_SUPABASE_USER=1 (ver docs/DEPLOY.md)."
        )
    app = FastAPI(
        title=config.APP_NAME,
        version=config.VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    # Middleware order (last added = outermost): CORS -> origin check -> Auth -> host check -> standby gate -> app.
    # CORS stays outermost, so preflights are answered there and every error below carries its headers for an allowed
    # origin. The origin check (CSRF) refuses a cross-site change before Auth asks Supabase about its token. The host
    # check protects the local API against DNS rebinding; Auth guards public (cloud) deployments; the standby gate
    # only acts while another instance is the active one (PostgreSQL leader lock).
    origins = browser_origins()
    app.add_middleware(StandbyGate)
    app.add_middleware(HostCheckMiddleware, allowed_hosts=_allowed_hosts(), exempt_paths=_host_check_exempt())
    if config.AUTH_MODE == "supabase":
        from app.auth import AuthMiddleware, SupabaseVerifier

        rt.auth = SupabaseVerifier(config.SUPABASE_URL, config.SUPABASE_ANON_KEY, config.ALLOWED_EMAILS)
        app.add_middleware(AuthMiddleware, verifier=rt.auth)
    app.add_middleware(OriginCheckMiddleware, allowed_origins=origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After"],  # readable by the browser: back-off hint of the 503s (auth outage, standby)
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

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        problems = []
        for err in exc.errors()[:6]:
            where = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path", "header"))
            problems.append(f"{where or 'petición'}: {_validation_es(err)}")
        return JSONResponse(status_code=422, content={"detail": "Parámetros no válidos — " + "; ".join(problems)})

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
    if config.HOST not in config.LOOPBACK_HOSTS and not config.DATABASE_URL and not config.ALLOW_LOCAL_DB:
        print(
            "\n  ERROR: servidor público sin NEXUS_DATABASE_URL: los datos irían a un archivo SQLite dentro del\n"
            "  contenedor y se perderían en cada despliegue o reinicio.\n"
            "  Configura NEXUS_DATABASE_URL con tu Postgres de Supabase (ver docs/DEPLOY.md).\n"
            "  (Solo si la carpeta de datos está en un disco persistente, p. ej. un volumen montado en /app/data:\n"
            "  NEXUS_ALLOW_LOCAL_DB=1)\n",
            flush=True,
        )
        sys.exit(2)
    print(f"\n  NEXUS Terminal v{config.VERSION} → http://{config.HOST}:{config.PORT}\n", flush=True)
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        # the standard asyncio loop everywhere: on Linux, start.sh installs uvicorn[standard], whose uvloop would be
        # picked automatically, while the image (its lock leaves uvloop out), Windows and the tests run on asyncio
        loop="asyncio",
        log_level="warning",
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("NEXUS_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
