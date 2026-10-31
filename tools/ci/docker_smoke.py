"""Smoke test of the production Docker image (CI job `docker`; needs a Docker daemon: Linux, or Docker Desktop).

1. Fails closed: a public bind without login, without a persistent database or without the e-mail allowlist refuses
   to start (exit code and Spanish reason, as a misconfigured Railway / Fly deploy would).
2. Starts on loopback: NEXUS_HOST=127.0.0.1 inside the container (nothing published, nothing reachable from outside),
   NEXUS_OFFLINE=1, PORT injected like Railway does. Every HTTP probe runs from inside the container's own network
   namespace (`docker exec ... python`, `docker run --network container:<name>`), so no host port is needed.
   /api/health answers in local mode: SQLite under /app/data, no login, every module loaded.
3. The server is PID 1 (it gets the platform's SIGTERM) and runs as a non-root user without kernel capabilities; its
   code is read-only for it (data/ and the sandbox scratch folder stay writable) and the image has no test tools.
4. No error events at start-up; a PyChart script runs in the sandbox through the API and a 4 GB allocation is stopped
   by the worker's POSIX memory limit (RLIMIT_AS).
5. tools/contract_check.py (every endpoint against frontend/src/api/types.ts) passes against the container.
6. docker stop (SIGTERM) stops it without SIGKILL; slower than Fly.io's default 5 s grace period is a warning.

    python3 tools/ci/docker_smoke.py nexus-terminal:ci [--port 18420] [--docker docker]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from typing import Any, Sequence

import ci_common as ci

# Obviously fake values, never contacted: the server stops before any request (sign-in is not even configured).
PLACEHOLDER_SUPABASE = {
    "NEXUS_AUTH": "supabase",
    "NEXUS_SUPABASE_URL": "https://supabase.ci.invalid",
    "NEXUS_SUPABASE_ANON_KEY": "ci-placeholder-not-a-key",
}
# (what is missing, extra environment, expected exit code or None for "any non-zero", text the reason must contain)
FAIL_CLOSED: list[tuple[str, dict[str, str], int | None, str]] = [
    ("sin login en una interfaz pública", {}, 2, "interfaz pública sin login"),
    ("sin base de datos persistente", {**PLACEHOLDER_SUPABASE, "NEXUS_ALLOWED_EMAILS": "ci@example.invalid"}, 2,
     "sin NEXUS_DATABASE_URL"),
    ("sin lista de correos permitidos", dict(PLACEHOLDER_SUPABASE), None, "NEXUS_ALLOWED_EMAILS"),
]
FAIL_CLOSED_TIMEOUT_S = 60
SCRIPT_OK = "indicator('ci: humo', overlay=True)\nplot(ta.sma(close, 10), 'sma')\n"
SCRIPT_MEMORY_BOMB = "indicator('ci: memoria')\nx = np.ones(500_000_000)\nplot(close)\n"  # 4 GB, far above RLIMIT_AS
# (test(1) flag, path, expected result, what it means)
FS_EXPECTATIONS: list[tuple[str, str, bool, str]] = [
    ("-w", "/app/data", True, "data/ escribible por el servidor (SQLite)"),
    ("-f", "/app/data/nexus.db", True, "base de datos creada al arrancar"),
    ("-w", "/app/backend/.tmp/sandbox", True, "carpeta de trabajo del sandbox escribible"),
    ("-w", "/app/backend/app", False, "código de solo lectura para el servidor (carpeta)"),
    ("-w", "/app/backend/app/main.py", False, "código de solo lectura para el servidor (archivo)"),
    # {tag} = sys.implementation.cache_tag of the image's Python (e.g. cpython-313)
    ("-f", "/app/backend/app/scripting/__pycache__/sandbox.{tag}.pyc", True, "bytecode precompilado en la imagen"),
]
CACHE_TAG = "import sys; print(sys.implementation.cache_tag)"
NO_PYTEST = "import importlib.util, sys; sys.exit(1 if importlib.util.find_spec('pytest') else 0)"
STOP_GRACE_S = 30
FLY_KILL_TIMEOUT_S = 5  # fly.toml keeps Fly's default kill_timeout
# tools/contract_check.py takes seconds; the cap keeps a hung endpoint inside the CI step's 10 minutes, so this script
# still prints the server log (docker logs) and its summary instead of being killed by the runner.
CONTRACT_TIMEOUT_S = 300
CONTRACT_SUMMARY = re.compile(r"(\d+) comprobaciones · (\d+) fallos · (\d+) avisos")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# HTTP request run by the image's own Python inside the server container: argv = url, method, JSON body ('' = none),
# timeout. Prints {"status", "body"}; a connection error exits non-zero (the server is not listening yet).
PROBE = """\
import json, sys, urllib.error, urllib.request
url, method, body, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
headers = {"User-Agent": "NexusTerminal-CI/1.0 (+tools/ci)", "Content-Type": "application/json"}
request = urllib.request.Request(url, data=body.encode() if body else None, method=method, headers=headers)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(request, timeout=timeout) as resp:
        status, raw = resp.status, resp.read()
except urllib.error.HTTPError as exc:
    status, raw = exc.code, exc.read()
sys.stdout.write(json.dumps({"status": status, "body": raw.decode("utf-8", "replace")}))
"""


class Docker:
    """The docker CLI (``command`` may be a wrapper, e.g. ``sudo docker``)."""

    def __init__(self, command: Sequence[str] = ("docker",)) -> None:
        self.command = list(command)

    def __call__(self, *args: str, timeout: float = 120, echo: bool = True):
        return ci.run([*self.command, *args], timeout=timeout, echo=echo)

    def running(self, name: str) -> bool:
        cp = self("inspect", "--format", "{{.State.Running}}", name, timeout=30, echo=False)
        return cp.returncode == 0 and cp.stdout.strip() == "true"


class ContainerHttp:
    """``http_json``-like requests made from inside the server container, on its loopback."""

    def __init__(self, docker: Docker, name: str) -> None:
        self.docker, self.name = docker, name

    def __call__(self, url: str, *, method: str = "GET", body: Any = None, timeout: float = 10.0) -> tuple[int, Any]:
        payload = json.dumps(body) if body is not None else ""
        cp = self.docker("exec", self.name, "python", "-c", PROBE, url, method, payload, str(timeout),
                         timeout=timeout + 30, echo=False)
        if cp.returncode != 0:
            raise ConnectionError(ci.tail(cp.stderr, 1) or f"docker exec terminó con código {cp.returncode}")
        envelope = json.loads(cp.stdout)
        try:
            return int(envelope["status"]), json.loads(envelope["body"])
        except ValueError:
            return int(envelope["status"]), None


def env_args(env: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key, value in env.items():
        out += ["-e", f"{key}={value}"]
    return out


def server_args(image: str, name: str, port: int) -> list[str]:
    """`docker run` arguments of the loopback server: it binds the container's loopback only (local mode) and no port
    is published, so it cannot be reached from outside the container."""
    env = {"NEXUS_HOST": "127.0.0.1", "PORT": str(port), "NEXUS_OFFLINE": "1"}
    return ["run", "--detach", "--name", name, *env_args(env), image]


def fail_closed_problems(returncode: int, output: str, expected: int | None, reason: str) -> list[str]:
    problems = []
    if expected is None:
        if returncode == 0:
            problems.append("terminó con código 0")
    elif returncode != expected:
        problems.append(f"código {returncode} (se esperaba {expected})")
    if reason not in output:
        problems.append(f"falta el motivo «{reason}» en la salida")
    return problems


def check_fail_closed(docker: Docker, image: str, report: ci.Report) -> None:
    for index, (label, extra, expected, reason) in enumerate(FAIL_CLOSED, 1):
        name = f"nexus-ci-closed-{index}-{os.getpid()}"
        check = f"falla cerrado {label}"
        args = ["run", "--rm", "--name", name, *env_args({"NEXUS_OFFLINE": "1", **extra}), image]
        try:
            cp = docker(*args, timeout=FAIL_CLOSED_TIMEOUT_S)
        except ci.CheckFailed:
            report.fail(check, f"siguió en marcha {FAIL_CLOSED_TIMEOUT_S} s: arrancó sin la protección")
            docker("rm", "--force", name, timeout=60, echo=False)
            continue
        output = (cp.stdout or "") + (cp.stderr or "")
        if not report.expect(check, fail_closed_problems(cp.returncode, output, expected, reason), f"código {cp.returncode}"):
            ci.log(ci.tail(output))


def check_identity(docker: Docker, image: str, name: str, report: ci.Report) -> None:
    cp = docker("image", "inspect", "--format", "{{.Config.User}}", image, timeout=30)
    user = cp.stdout.strip()
    report.expect("USER de la imagen", [] if user and user.split(":")[0] not in ("0", "root") else
                  [f"USER = {user!r}: la imagen arrancaría como root"], user)
    cp = docker("exec", name, "cat", "/proc/1/status", timeout=30)
    status = ci.parse_proc_status(cp.stdout if cp.returncode == 0 else "")
    detail = (f"Uid {' '.join(status.get('Uid', '?').split())} · CapEff {status.get('CapEff', '?')} · "
              f"NoNewPrivs {status.get('NoNewPrivs', '?')} · Seccomp {status.get('Seccomp', '?')}")
    report.expect("servidor sin root ni capacidades (PID 1)", ci.check_unprivileged(status), detail)
    cp = docker("exec", name, "cat", "/proc/1/cmdline", timeout=30)
    cmdline = (cp.stdout or "").replace("\x00", " ").strip()
    report.expect("PID 1 es el servidor (recibe SIGTERM)", [] if "app.main" in cmdline else [f"PID 1 = {cmdline!r}"],
                  cmdline)
    cp = docker("exec", name, "python", "-c", NO_PYTEST, timeout=60)
    problem = {0: [], 1: ["pytest está instalado"]}.get(cp.returncode, [f"docker exec terminó con código {cp.returncode}"])
    report.expect("sin herramientas de prueba en la imagen", problem)
    cp = docker("image", "inspect", "--format", "{{.Size}}", image, timeout=30)
    if cp.returncode == 0 and cp.stdout.strip().isdigit():
        report.ok("tamaño de la imagen", f"{int(cp.stdout.strip()) / 1e6:.0f} MB")


def check_filesystem(docker: Docker, name: str, report: ci.Report) -> None:
    cp = docker("exec", name, "python", "-c", CACHE_TAG, timeout=60, echo=False)
    tag = cp.stdout.strip() or "cpython-313"
    for flag, template, expected, label in FS_EXPECTATIONS:
        path = template.format(tag=tag)
        cp = docker("exec", name, "test", flag, path, timeout=30, echo=False)
        actual = cp.returncode == 0
        report.expect(label, [] if actual == expected else [f"test {flag} {path} dio {'sí' if actual else 'no'}"],
                      f"test {flag} {path}")


def first_error(result: Any) -> str:
    errors = result.get("errors") if isinstance(result, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "")
    return ""


def script_ok_problems(status: int, result: Any) -> list[str]:
    if status != 200 or not isinstance(result, dict):
        return [f"HTTP {status}"]
    problems = []
    if result.get("ok") is not True:
        problems.append(f"ok = {result.get('ok')!r}: {first_error(result)[:300]}")
    if not result.get("plots"):
        problems.append("sin plots en el resultado")
    return problems


def memory_bomb_problems(status: int, result: Any) -> list[str]:
    if status != 200 or not isinstance(result, dict):
        return [f"HTTP {status}"]
    if result.get("ok") is not False:
        return ["el script de 4 GB terminó bien: el límite de memoria del sandbox no actuó"]
    message = first_error(result)
    return [] if "Memoria insuficiente" in message else [f"error inesperado: {message[:300]}"]


def startup_error_problems(status: int, body: Any) -> tuple[list[str], int]:
    """(problems, number of events) of GET /api/events right after start-up: any 'error' event is a problem."""
    events = body.get("events") if isinstance(body, dict) else None
    if status != 200 or not isinstance(events, list):
        return [f"GET /api/events: HTTP {status}"], 0
    errors = [e for e in events if isinstance(e, dict) and e.get("level") == "error"]
    return [f"{e.get('source')}: {str(e.get('message'))[:200]}" for e in errors[:5]], len(events)


def check_api(fetch, base: str, report: ci.Report) -> None:
    """``fetch`` works like ci.http_json (ContainerHttp in the CI run)."""
    # read before running scripts: the memory bomb below is a script failure on purpose
    status, body = fetch(f"{base}/api/events?limit=500", timeout=30)
    problems, count = startup_error_problems(status, body)
    report.expect("sin eventos de error al arrancar", problems, f"{count} eventos")
    body = {"code": SCRIPT_OK, "symbol": "SIM:NEXUS", "tf": "1h", "limit": 300}
    status, result = fetch(f"{base}/api/scripts/run", method="POST", body=body, timeout=120)
    plots = len(result.get("plots") or []) if isinstance(result, dict) else 0
    report.expect("PyChart ejecuta un script en el sandbox", script_ok_problems(status, result), f"{plots} plot(s)")
    body = {"code": SCRIPT_MEMORY_BOMB, "symbol": "SIM:NEXUS", "tf": "1h", "limit": 300}
    status, result = fetch(f"{base}/api/scripts/run", method="POST", body=body, timeout=120)
    report.expect("PyChart: el límite de memoria detiene una reserva de 4 GB", memory_bomb_problems(status, result),
                  first_error(result)[:160])


def contract_args(image: str, name: str, base: str) -> list[str]:
    """Runs tools/contract_check.py from the image itself (it has httpx) in the server container's network namespace
    (its loopback is the server's), with the checker and frontend/src/api (the TypeScript contract) read-only."""
    return [
        "run", "--rm", "--network", f"container:{name}",
        "--mount", ci.bind_mount(ci.ROOT / "tools" / "contract_check.py", "/app/tools/contract_check.py"),
        "--mount", ci.bind_mount(ci.ROOT / "frontend" / "src" / "api", "/app/frontend/src/api"),
        image, "python", "/app/tools/contract_check.py", "--base", base, "--symbol", "SIM:NEXUS",
    ]


def contract_detail(output: str) -> str:
    match = CONTRACT_SUMMARY.search(ANSI.sub("", output or ""))
    return match.group(0) if match else ""


def check_contract(docker: Docker, image: str, name: str, base: str, report: ci.Report) -> None:
    cp = docker(*contract_args(image, name, base), timeout=CONTRACT_TIMEOUT_S)
    output = (cp.stdout or "") + (cp.stderr or "")
    ci.log(output)
    detail = contract_detail(output)
    report.expect("contrato de la API (types.ts)", [] if cp.returncode == 0 else [detail or ci.tail(output, 5)], detail)


def stop_verdict(exit_code: str, elapsed_s: float) -> tuple[list[str], str]:
    """(problems, warning) of a `docker stop`: SIGKILL (137) or an error exit are problems; a slow stop is a warning."""
    problems = []
    if exit_code == "137":
        problems.append(f"Docker lo mató con SIGKILL tras {STOP_GRACE_S} s: no se detuvo con SIGTERM")
    elif exit_code not in ("0", "143"):
        problems.append(f"código de salida {exit_code or '?'} al detenerse")
    warning = ""
    if not problems and elapsed_s > FLY_KILL_TIMEOUT_S:
        warning = f"tardó {elapsed_s:.1f} s en detenerse (Fly.io lo mataría a los {FLY_KILL_TIMEOUT_S} s por defecto)"
    return problems, warning


def check_stop(docker: Docker, name: str, report: ci.Report) -> None:
    started = time.monotonic()
    docker("stop", "-t", str(STOP_GRACE_S), name, timeout=STOP_GRACE_S + 60)
    elapsed = time.monotonic() - started
    cp = docker("inspect", "--format", "{{.State.ExitCode}}", name, timeout=30)
    problems, warning = stop_verdict(cp.stdout.strip(), elapsed)
    check = "parada limpia con SIGTERM"
    if warning:
        report.warn(check, warning)
    else:
        report.expect(check, problems, f"{elapsed:.1f} s, código {cp.stdout.strip()}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prueba de humo de la imagen Docker de NEXUS.")
    parser.add_argument("image", help="imagen ya construida, p. ej. nexus-terminal:ci")
    parser.add_argument("--port", type=int, default=18420, help="puerto del servidor dentro del contenedor (PORT)")
    parser.add_argument("--docker", default="docker", help="programa docker (por defecto: docker)")
    parser.add_argument("--health-timeout", type=float, default=120.0, help="segundos de espera de /api/health")
    args = parser.parse_args(argv)
    docker = Docker(shlex.split(args.docker))
    report = ci.Report("Imagen Docker: prueba de humo")
    base = f"http://127.0.0.1:{args.port}"
    name = f"nexus-ci-{os.getpid()}"
    fetch = ContainerHttp(docker, name)
    if not ci.require_docker(docker.command, report):
        return report.finish()
    try:
        with ci.group("1. Falla cerrado con una configuración pública incompleta"):
            check_fail_closed(docker, args.image, report)
        with ci.group("2. Arranque en loopback, sin red"):
            cp = docker(*server_args(args.image, name, args.port), timeout=120)
            if cp.returncode != 0:
                raise ci.CheckFailed("docker run falló: " + ci.tail(cp.stderr, 10))
            health = ci.wait_for_health(f"{base}/api/health", timeout_s=args.health_timeout, fetch=fetch,
                                        alive=lambda: docker.running(name))
            report.expect("/api/health en modo local", ci.check_health(health, db_path_prefix="/app/data/"),
                          ci.services_summary(health))
        with ci.group("3. Usuario, permisos y contenido de la imagen"):
            check_identity(docker, args.image, name, report)
            check_filesystem(docker, name, report)
        with ci.group("4. Arranque sin errores y sandbox de PyChart vía API"):
            check_api(fetch, base, report)
        with ci.group("5. Contrato de la API (tools/contract_check.py)"):
            check_contract(docker, args.image, name, base, report)
        with ci.group("6. Parada con SIGTERM"):
            check_stop(docker, name, report)
    except ci.CheckFailed as exc:
        report.fail("prueba interrumpida", str(exc))
    except ci.PROBE_ERRORS as exc:
        report.fail("prueba interrumpida", f"{type(exc).__name__}: {exc}")
    finally:
        with ci.group("Registro del servidor (docker logs)"):
            for step in (("logs", name), ("rm", "--force", name)):
                try:
                    cp = docker(*step, timeout=60, echo=False)
                    if step[0] == "logs":
                        ci.log((cp.stdout or "") + (cp.stderr or ""))
                except ci.CheckFailed as exc:
                    ci.log(str(exc))
    return report.finish()


if __name__ == "__main__":
    sys.exit(main())
