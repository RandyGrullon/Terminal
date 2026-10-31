"""Shared helpers of the CI scripts in tools/ci (standard library only, Python 3.9+: they run with the runner's own
python3 as well as with backend/.venv).

- logs: collapsible groups and annotations on GitHub Actions (plain text elsewhere) and a Markdown table for the run
  page (``$GITHUB_STEP_SUMMARY``);
- ``Report``: the checks of one script (OK / AVISO / FALLO), exit code 1 when any check failed;
- ``http_request`` / ``http_json``: probes of a local NEXUS server (never through a proxy, honest User-Agent);
- ``wait_for_health`` / ``check_health``: ``/api/health`` of a server that was just started;
- ``parse_proc_status`` / ``check_unprivileged``: identity of a Linux process from ``/proc/<pid>/status``.
Messages are in Spanish, like the rest of the project's tools.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = "NexusTerminal-CI/1.0 (+tools/ci)"
MAX_BODY_BYTES = 64 * 1024 * 1024


class CheckFailed(Exception):
    """A step cannot go on (the message is for the developer, in Spanish)."""


# ----------------------------------------------------------------------------- logs


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def log(message: str = "") -> None:
    print(message, flush=True)


def _escape(text: str, *, prop: bool = False) -> str:
    """Workflow-command escaping (``::error title=<prop>::<data>``)."""
    out = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        out = out.replace(":", "%3A").replace(",", "%2C")
    return out


def annotate(level: str, message: str, title: str = "") -> None:
    """``level``: error | warning | notice. On GitHub Actions it shows on the run page; elsewhere it is a log line."""
    if in_actions():
        props = f" title={_escape(title, prop=True)}" if title else ""
        log(f"::{level}{props}::{_escape(message)}")
    else:
        label = {"error": "ERROR", "warning": "AVISO", "notice": "NOTA"}.get(level, level.upper())
        log(f"{label}: {title + ': ' if title else ''}{message}")


@contextlib.contextmanager
def group(title: str) -> Iterator[None]:
    log(f"::group::{title}" if in_actions() else f"\n== {title} ==")
    try:
        yield
    finally:
        if in_actions():
            log("::endgroup::")


def write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown.rstrip() + "\n\n")
    except OSError:
        pass


def tail(text: str | None, lines: int = 30) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


class Report:
    """Checks of one CI script. Every row is logged at once; ``finish()`` prints the totals, appends the table to the
    run summary and returns the exit code (1 when a check failed; warnings do not fail)."""

    OK, WARN, FAIL = "OK", "AVISO", "FALLO"

    def __init__(self, title: str) -> None:
        self.title = title
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, check: str, detail: str = "") -> None:
        self.rows.append((status, check, detail))
        log(f"{status:<5} {check}" + (f" · {detail}" if detail else ""))
        if in_actions() and status in (self.FAIL, self.WARN):
            annotate("error" if status == self.FAIL else "warning", detail or check, title=check)

    def ok(self, check: str, detail: str = "") -> None:
        self.add(self.OK, check, detail)

    def warn(self, check: str, detail: str = "") -> None:
        self.add(self.WARN, check, detail)

    def fail(self, check: str, detail: str = "") -> None:
        self.add(self.FAIL, check, detail)

    def expect(self, check: str, problems: Sequence[str], detail: str = "") -> bool:
        """OK (with ``detail``) when ``problems`` is empty, else FALLO listing them."""
        if problems:
            self.fail(check, "; ".join(problems))
            return False
        self.ok(check, detail)
        return True

    @property
    def failures(self) -> int:
        return sum(1 for status, _, _ in self.rows if status == self.FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for status, _, _ in self.rows if status == self.WARN)

    def totals(self) -> str:
        return " · ".join([
            count(len(self.rows), "comprobación", "comprobaciones"),
            count(self.failures, "fallo", "fallos"),
            count(self.warnings, "aviso", "avisos"),
        ])

    def finish(self) -> int:
        log("")
        log(f"{self.title}: {self.totals()}")

        def cell(text: str) -> str:
            return " ".join(str(text).split()).replace("|", "/")[:500]

        rows = "\n".join(f"| {s} | {cell(c)} | {cell(d)} |" for s, c, d in self.rows)
        write_summary(f"### {self.title}\n\n{self.totals()}\n\n| Estado | Comprobación | Detalle |\n|---|---|---|\n{rows}")
        return 1 if self.failures else 0


def count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


# ----------------------------------------------------------------------------- processes


def run(cmd: Sequence[str], *, timeout: float, echo: bool = True, cwd: str | Path | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Runs ``cmd`` (no shell) and returns its output as text. Raises CheckFailed on timeout or missing program."""
    cmd = [str(part) for part in cmd]
    if echo:
        log("$ " + shlex.join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise CheckFailed(f"«{shlex.join(cmd)[:200]}» no terminó en {timeout:g} s") from exc
    except FileNotFoundError as exc:
        raise CheckFailed(f"no se encontró el programa {cmd[0]!r}") from exc


def docker_server_version(command: Sequence[str]) -> str:
    """Version of the Docker daemon behind ``command`` ('' when it does not answer)."""
    try:
        cp = run([*command, "version", "--format", "{{.Server.Version}}"], timeout=60, echo=False)
    except CheckFailed:
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def require_docker(command: Sequence[str], report: Report) -> bool:
    """Records whether the Docker daemon answers (a stopped Docker Desktop, say, gives one clear failure)."""
    version = docker_server_version(command)
    if version:
        report.ok("Docker disponible", f"servidor {version}")
        return True
    report.fail("Docker disponible", "el daemon de Docker no responde (¿está en marcha?)")
    return False


def bind_mount(source: str | Path, target: str) -> str:
    """``docker run --mount`` value of a read-only bind mount."""
    src = str(source)
    if any(ch in src + target for ch in ',"'):
        raise CheckFailed(f"ruta no admitida en --mount (coma o comillas): {src}")
    return f"type=bind,source={src},target={target},readonly"


# ----------------------------------------------------------------------------- HTTP probes (local server only)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback probes never go through a proxy
PROBE_ERRORS = (OSError, ValueError, http.client.HTTPException)


def http_request(url: str, *, method: str = "GET", body: Any = None, timeout: float = 10.0,
                 accept: str = "application/json") -> tuple[int, dict[str, str], bytes]:
    """(status, lower-case headers, body) of one request; 4xx/5xx answers are returned, not raised."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(MAX_BODY_BYTES)
        finally:
            exc.close()
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, payload


def http_json(url: str, *, method: str = "GET", body: Any = None, timeout: float = 10.0) -> tuple[int, Any]:
    """(status, parsed JSON body or None when the body is not JSON)."""
    status, _headers, raw = http_request(url, method=method, body=body, timeout=timeout)
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return status, None


def wait_for_health(url: str, *, timeout_s: float, interval_s: float = 1.0, alive: Callable[[], bool] | None = None,
                    fetch: Callable[..., tuple[int, Any]] = http_json, clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> dict:
    """Polls ``url`` until it answers 200 with ``{"ok": true, ...}`` and returns that body. Raises CheckFailed when
    ``alive()`` says the server process is gone, or after ``timeout_s``."""
    deadline = clock() + timeout_s
    last = "sin respuesta"
    while True:
        if alive is not None and not alive():
            raise CheckFailed(f"el servidor terminó antes de responder {url} (último intento: {last})")
        try:
            status, data = fetch(url, timeout=5.0)
            if status == 200 and isinstance(data, dict) and data.get("ok") is True:
                return data
            last = f"HTTP {status}"
        except PROBE_ERRORS as exc:
            last = f"{type(exc).__name__}: {exc}"
        if clock() >= deadline:
            raise CheckFailed(f"{url} no respondió en {timeout_s:g} s (último intento: {last})")
        sleep(interval_s)


def check_health(data: Any, *, role: str = "single", auth: str = "none", dialect: str = "sqlite",
                 db_path_prefix: str | None = None) -> list[str]:
    """Problems of an /api/health answer of a single local instance (empty list = healthy)."""
    if not isinstance(data, dict):
        return ["/api/health no devolvió un objeto JSON"]
    problems = []
    if data.get("ok") is not True:
        problems.append(f"ok = {data.get('ok')!r}")
    for key, expected in (("role", role), ("auth", auth)):
        if data.get(key) != expected:
            problems.append(f"{key} = {data.get(key)!r} (se esperaba {expected!r})")
    db = data.get("db") if isinstance(data.get("db"), dict) else {}
    if db.get("dialect") != dialect:
        problems.append(f"db.dialect = {db.get('dialect')!r} (se esperaba {dialect!r})")
    if db_path_prefix and not str(db.get("path") or "").startswith(db_path_prefix):
        problems.append(f"db.path = {db.get('path')!r} (se esperaba dentro de {db_path_prefix})")
    errors = data.get("load_errors")
    if errors:
        items = errors if isinstance(errors, list) else [errors]
        word = "módulo" if len(items) == 1 else "módulos"
        problems.append(f"{len(items)} {word} sin cargar: " + " | ".join(str(e)[:300] for e in items[:5]))
    if not isinstance(data.get("services"), dict) or not data["services"]:
        problems.append("sin estado de servicios")
    return problems


def services_summary(data: Any) -> str:
    """'x de n servicios en marcha' from an /api/health answer ('' when it has no services)."""
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict) or not services:
        return ""
    running = sum(1 for value in services.values() if isinstance(value, dict) and value.get("running") is True)
    return f"{running} de {len(services)} servicios en marcha"


# ----------------------------------------------------------------------------- Linux process identity


def parse_proc_status(text: str) -> dict[str, str]:
    """``/proc/<pid>/status`` → {field: value} ('Uid' → '10001\\t10001\\t10001\\t10001', ...)."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def check_unprivileged(status: dict[str, str]) -> list[str]:
    """Problems when the process may act as root: any uid / gid 0 (real, effective, saved, filesystem) or any
    effective capability."""
    problems = []
    for key, who in (("Uid", "usuario"), ("Gid", "grupo")):
        ids = status.get(key, "").split()
        if not ids:
            problems.append(f"sin línea {key} en /proc/<pid>/status")
        elif "0" in ids:
            problems.append(f"{key} {' '.join(ids)}: el proceso corre con el {who} root")
    cap = status.get("CapEff", "")
    try:
        if int(cap, 16) != 0:
            problems.append(f"CapEff {cap}: el proceso conserva capacidades del kernel")
    except ValueError:
        problems.append("sin línea CapEff en /proc/<pid>/status")
    return problems
