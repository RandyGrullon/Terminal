"""Terminal command registry, parser and core commands.

Modules register commands at import time:

    from app.terminal import command, CommandContext

    @command("signal", help="Señal NEXUS de un símbolo", usage="signal <símbolo> [tf]", group="NEXUS")
    async def _signal(ctx: CommandContext) -> None:
        ...
"""
from __future__ import annotations

import difflib
import shlex
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.config import APP_NAME, VERSION
from app.runtime import rt
from app.util import clean_json

Handler = Callable[["CommandContext"], Awaitable[Any]]


@dataclass
class Command:
    name: str
    handler: Handler
    help: str = ""
    usage: str = ""
    group: str = "General"
    aliases: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: dict[str, Command] = {}
ALIASES: dict[str, str] = {}
# Flags that never take a value (so `scan crypto --deep 1h` keeps "1h" positional).
BOOL_FLAGS: set[str] = {
    "deep", "force", "all", "json", "auto", "quiet", "verbose", "yes", "reset", "live",
    "gate", "nogate", "long", "short", "paused", "full", "help",
}


class CommandError(Exception):
    pass


def command(name: str, help: str = "", usage: str = "", group: str = "General", aliases: tuple[str, ...] = ()):
    def deco(fn: Handler) -> Handler:
        REGISTRY[name] = Command(name=name, handler=fn, help=help, usage=usage or name, group=group, aliases=tuple(aliases))
        for alias in aliases:
            ALIASES[alias] = name
        return fn

    return deco


def register_bool_flags(*names: str) -> None:
    BOOL_FLAGS.update(names)


def list_commands() -> list[dict]:
    return [
        {"name": c.name, "help": c.help, "usage": c.usage, "group": c.group, "aliases": list(c.aliases)}
        for c in sorted(REGISTRY.values(), key=lambda c: (c.group, c.name))
    ]


class CommandContext:
    def __init__(self, raw: str, name: str, args: list[str], flags: dict[str, Any], client_id: int | None = None):
        self.rt = rt
        self.raw = raw
        self.name = name
        self.args = args
        self.flags = flags
        self.client_id = client_id
        self.lines: list[dict] = []
        self.actions: list[dict] = []
        self.data: Any = None

    # ---------------------------------------------------------------- input helpers
    def arg(self, i: int, default: Any = None) -> Any:
        return self.args[i] if i < len(self.args) else default

    def flag(self, name: str, default: Any = None) -> Any:
        return self.flags.get(name, default)

    def flag_int(self, name: str, default: int | None = None) -> int | None:
        try:
            return int(float(self.flags[name]))
        except (KeyError, TypeError, ValueError):
            return default

    def flag_float(self, name: str, default: float | None = None) -> float | None:
        try:
            return float(self.flags[name])
        except (KeyError, TypeError, ValueError):
            return default

    async def symbol(self, text: str | None) -> str:
        """Resolve user text ('btc', 'aapl', 'BINANCE:ETHUSDT') to a full symbol."""
        if not text:
            self.fail("Falta el símbolo. Ejemplo: btc, eth, aapl, BINANCE:SOLUSDT")
        if self.rt.hub is not None:
            try:
                resolved = await self.rt.hub.resolve(text)
            except Exception:
                resolved = None
            if resolved:
                return resolved
            if ":" not in text:
                self.fail(f"No encontré el símbolo '{text}'. Prueba: search {text}")
        return text.upper()

    # ---------------------------------------------------------------- output helpers
    def print(self, text: Any = "", level: str = "info") -> None:
        for line in (str(text).splitlines() or [""]):
            self.lines.append({"text": line, "level": level})

    def table(self, headers: list[str], rows: list[list[Any]], max_rows: int = 50, level: str = "info") -> None:
        cells = [[("" if v is None else str(v)) for v in row] for row in rows[:max_rows]]
        widths = [len(h) for h in headers]
        for row in cells:
            for i, v in enumerate(row[: len(widths)]):
                widths[i] = min(max(widths[i], len(v)), 48)
        fmt = lambda row: "  ".join(v[:48].ljust(widths[i]) for i, v in enumerate(row[: len(widths)]))  # noqa: E731
        self.print(fmt(headers), "muted")
        self.print("  ".join("─" * w for w in widths), "muted")
        for row in cells:
            self.print(fmt(row), level)
        if len(rows) > max_rows:
            self.print(f"… {len(rows) - max_rows} filas más", "muted")

    def action(self, name: str, **payload: Any) -> None:
        self.actions.append({"action": name, **payload})

    def fail(self, message: str) -> None:
        raise CommandError(message)


def parse(text: str) -> tuple[str, list[str], dict[str, Any]]:
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = text.split()
    if not tokens:
        return "", [], {}
    name = tokens[0].lower()
    args: list[str] = []
    flags: dict[str, Any] = {}
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and len(tok) > 2:
            key = tok[2:]
            if "=" in key:
                k, v = key.split("=", 1)
                flags[k.lower()] = v
            elif key.lower() in BOOL_FLAGS:
                flags[key.lower()] = True
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                flags[key.lower()] = tokens[i + 1]
                i += 1
            else:
                flags[key.lower()] = True
        else:
            args.append(tok)
        i += 1
    return name, args, flags


async def execute(text: str, client_id: int | None = None) -> dict:
    """Run one terminal line and return a CommandResult dict."""
    text = (text or "").strip()
    name, args, flags = parse(text)
    if not name:
        return {"ok": True, "command": "", "lines": [], "actions": [], "data": None}
    canonical = ALIASES.get(name, name)
    cmd = REGISTRY.get(canonical)
    if cmd is None:
        names = list(REGISTRY) + list(ALIASES)
        close = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
        hint = f" ¿Quisiste decir: {', '.join(close)}?" if close else " Escribe 'help' para ver los comandos."
        return {
            "ok": False,
            "command": name,
            "lines": [{"text": f"Comando desconocido: {name}.{hint}", "level": "error"}],
            "actions": [],
            "data": None,
        }
    ctx = CommandContext(text, canonical, args, flags, client_id)
    if flags.get("help"):
        _describe(ctx, cmd)
        return _result(ctx, True)
    started = time.perf_counter()
    try:
        await cmd.handler(ctx)
        ok = True
    except CommandError as exc:
        ctx.print(str(exc), "error")
        ok = False
    except Exception as exc:  # never let a command crash the socket
        ctx.print(f"Error ejecutando '{canonical}': {type(exc).__name__}: {exc}", "error")
        if rt.bus is not None:
            rt.bus.emit("error", "terminal", f"Fallo en comando '{text}': {exc}", {"trace": traceback.format_exc(limit=6)})
        ok = False
    elapsed = (time.perf_counter() - started) * 1000
    if rt.bus is not None and canonical not in ("clear", "help", "echo"):
        rt.bus.emit("debug", "terminal", f"> {text} ({elapsed:.0f} ms)")
    return _result(ctx, ok)


def _result(ctx: CommandContext, ok: bool) -> dict:
    return clean_json({"ok": ok, "command": ctx.name, "lines": ctx.lines, "actions": ctx.actions, "data": ctx.data})


def _describe(ctx: CommandContext, cmd: Command) -> None:
    ctx.print(f"{cmd.name} — {cmd.help}", "info")
    ctx.print(f"Uso: {cmd.usage}", "muted")
    if cmd.aliases:
        ctx.print(f"Alias: {', '.join(cmd.aliases)}", "muted")


# ============================================================================= core commands


@command("help", help="Lista los comandos o explica uno", usage="help [comando]", group="Sistema", aliases=("ayuda", "?"))
async def _help(ctx: CommandContext) -> None:
    target = ctx.arg(0)
    if target:
        cmd = REGISTRY.get(ALIASES.get(target.lower(), target.lower()))
        if not cmd:
            ctx.fail(f"No existe el comando '{target}'")
        _describe(ctx, cmd)
        return
    ctx.print(f"{APP_NAME} v{VERSION} — comandos disponibles (usa 'help <comando>' o '<comando> --help'):", "system")
    groups: dict[str, list[Command]] = {}
    for c in REGISTRY.values():
        groups.setdefault(c.group, []).append(c)
    for group in sorted(groups):
        ctx.print("")
        ctx.print(f"[{group}]", "signal")
        for c in sorted(groups[group], key=lambda c: c.name):
            ctx.print(f"  {c.usage:<46} {c.help}")
    ctx.data = list_commands()


@command("status", help="Estado general del sistema", group="Sistema", aliases=("estado",))
async def _status(ctx: CommandContext) -> None:
    uptime = int(time.time() - rt.started_at)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    ctx.print(f"{APP_NAME} v{VERSION} · activo {h:02d}:{m:02d}:{s:02d}", "system")
    if rt.db is not None:
        ctx.print(f"Base de datos: {rt.db.path} ({rt.db.size_bytes() / 1_048_576:.1f} MB)")
    if rt.ws is not None:
        ctx.print(f"Clientes conectados: {len(rt.ws.clients)}")
    rows = []
    for attr, svc in rt.services:
        try:
            st = svc.status()
        except Exception as exc:  # pragma: no cover
            st = {"running": False, "error": str(exc)}
        detail = ", ".join(f"{k}={v}" for k, v in st.items() if k != "running")[:70]
        rows.append([attr, "✔ activo" if st.get("running") else "✖ detenido", detail])
    if rows:
        ctx.table(["Servicio", "Estado", "Detalle"], rows)
    for err in rt.load_errors:
        ctx.print(f"Módulo con error: {err}", "error")


@command("services", help="Detalle de cada servicio", group="Sistema", aliases=("servicios",))
async def _services(ctx: CommandContext) -> None:
    data = {}
    for attr, svc in rt.services:
        try:
            data[attr] = svc.status()
        except Exception as exc:
            data[attr] = {"running": False, "error": str(exc)}
        ctx.print(f"{attr}: {data[attr]}", "info" if data[attr].get("running") else "warn")
    ctx.data = data


@command("events", help="Últimos eventos registrados", usage="events [n] [--level=signal,trade] [--source=nexus]", group="Sistema", aliases=("log",))
async def _events(ctx: CommandContext) -> None:
    n = int(ctx.arg(0, 30)) if str(ctx.arg(0, "30")).isdigit() else 30
    from datetime import datetime

    for ev in rt.bus.recent(n, level=ctx.flag("level"), source=ctx.flag("source")):
        stamp = datetime.fromtimestamp(ev["ts"] / 1000).strftime("%H:%M:%S")
        ctx.print(f"{stamp} [{ev['source']}] {ev['message']}", ev["level"])


@command("settings", help="Ver o cambiar ajustes", usage="settings [clave] [valor]", group="Sistema", aliases=("ajustes", "config"))
async def _settings(ctx: CommandContext) -> None:
    key, value = ctx.arg(0), ctx.arg(1)
    if key is None:
        for k, v in sorted(rt.settings.all().items()):
            if "key" in k and v:
                v = "••••••"
            ctx.print(f"{k:<32} {v}")
        return
    if value is None:
        ctx.print(f"{key} = {rt.settings.get(key)}")
        return
    try:
        new = await rt.settings.set(key, " ".join(ctx.args[1:]))
    except (KeyError, ValueError) as exc:
        ctx.fail(str(exc).strip("'\""))
    ctx.print(f"✔ {key} = {new}", "success")


@command("db", help="Estadísticas de la base de datos local", usage="db [vacuum]", group="Sistema")
async def _db(ctx: CommandContext) -> None:
    if ctx.arg(0) == "vacuum":
        await rt.db.vacuum()
        ctx.print(f"✔ Base de datos compactada ({rt.db.size_bytes() / 1_048_576:.1f} MB)", "success")
        return
    counts = await rt.db.table_counts()
    ctx.print(f"SQLite: {rt.db.path} · {rt.db.size_bytes() / 1_048_576:.2f} MB", "system")
    ctx.table(["Tabla", "Filas"], [[k, f"{v:,}"] for k, v in counts.items()])
    ctx.data = counts


@command("version", help="Versión de NEXUS", group="Sistema")
async def _version(ctx: CommandContext) -> None:
    ctx.print(f"{APP_NAME} v{VERSION}", "system")


@command("clear", help="Limpia la terminal", group="Sistema", aliases=("cls", "limpiar"))
async def _clear(ctx: CommandContext) -> None:
    ctx.action("clear")


@command("echo", help="Repite el texto", usage="echo <texto>", group="Sistema")
async def _echo(ctx: CommandContext) -> None:
    ctx.print(" ".join(ctx.args))
