"""Runs the backend test-suite inside the production image (CI job `docker`; needs a Docker daemon: Linux, or Docker
Desktop, where the container is still a Linux one).

The code under test is the image's own (/app/backend/app) with its Python, its exact dependencies
(backend/requirements.lock) and its non-root user, so the POSIX layers of the PyChart sandbox (RLIMIT_AS / FSIZE /
CORE / NPROC, oom_score_adj, no_new_privs, network namespace) and the server hardening run for real on Linux. Only
the test tooling is added, in a throw-away container (--rm):

- pytest, installed for the container's user (the range of backend/requirements-dev.txt; requirements.lock as
  constraints, so no package of the image changes);
- read-only, the repository files the tests read (backend/tests, backend/pytest.ini, docs, tools, frontend, ...).
  backend/app is never mounted over;
- NEXUS_CHECK_LOCK_ENV=1, so tests/test_fix_cloud_deps.py also checks that the image holds exactly the lock;
- the image's deployment variables (NEXUS_HOST=0.0.0.0, NEXUS_PORT, ...) unset: the tests run with the app's defaults.

A skip whose reason says the test is POSIX- or Linux-only fails the run (it must run here); the other skips are
listed, as warnings when they belong to the sandbox / hardening tests.

    python3 tools/ci/docker_tests.py nexus-terminal:ci [--docker docker] [-- <pytest arguments>]
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Sequence

import ci_common as ci

APP_ROOT = "/app"
SKIP_TOP = frozenset({".git", "backend", "data"})  # backend/ is handled entry by entry, data/ is never needed
SKIP_BACKEND = frozenset({"app", ".venv", ".tmp", ".pytest_cache", "__pycache__"})  # app = the image's code
ENV = {"NEXUS_OFFLINE": "1", "NEXUS_CHECK_LOCK_ENV": "1"}
# The image's deployment settings (Dockerfile ENV: public bind, Fly's port, data folder, trusted proxies) are not the
# test environment: they are unset in the container, so the suite runs with the app's defaults like the backend jobs
# (with NEXUS_HOST=0.0.0.0 the local-bind tests fail, because a public bind accepts any Host header by design).
IMAGE_DEPLOY_ENV = ("NEXUS_HOST", "NEXUS_PORT", "NEXUS_DATA_DIR", "NEXUS_FORWARDED_ALLOW_IPS")
DEFAULT_PYTEST_ARGS = ("-q", "-p", "no:cacheprovider", "-ra", "--durations=15")
TIMEOUT_S = 22 * 60  # below the step's timeout-minutes (25), so this script still reports why it stopped
PYTEST_REQUIREMENT = re.compile(r"^pytest(?![\w.-])")
SKIPPED_LINE = re.compile(r"^SKIPPED \[(?P<count>\d+)\] (?P<where>.+?): (?P<reason>.*)$")
PLATFORM_ONLY = re.compile(
    r"\b(posix|linux)\b[^.;]*\bonly\b|\bonly (on |in )?(posix|linux)\b|\bsolo (en )?(posix|linux)\b", re.IGNORECASE
)
# ... unless the reason says the environment refused something (e.g. a user namespace under Docker's seccomp profile):
# that skip is legitimate here and is only reported.
ENVIRONMENT_REFUSAL = re.compile(
    r"\bE(PERM|ACCES|NOSYS|INVAL)\b|seccomp|apparmor|namespace|permis|denied|deneg|refus|rechaz|"
    r"not available|unavailable|no disponible|no permit", re.IGNORECASE
)
# skipif(<condition>, reason="...") of the suite whose condition is true off POSIX / Linux (os.name == "nt", ...): a
# skip with one of those reasons inside the Linux image means the platform check itself is wrong, whatever the wording.
SKIPIF = re.compile(
    r"skipif\(\s*(?P<cond>[^()]*(?:\([^()]*\)[^()]*)*?)\s*,\s*reason\s*=\s*(?P<q>[\"'])(?P<reason>.+?)(?P=q)", re.S
)
NOT_POSIX_CONDITION = re.compile(
    r"os\.name\s*==\s*[\"']nt[\"']|os\.name\s*!=\s*[\"']posix[\"']|sys\.platform\s*==\s*[\"']win32[\"']|"
    r"sys\.platform\.startswith\(\s*[\"']win|not\s+sys\.platform\.startswith\(\s*[\"']linux|"
    r"sys\.platform\s*!=\s*[\"']linux|platform\.system\(\)\s*(?:==\s*[\"']Windows|!=\s*[\"']Linux)"
)
SECURITY_FILES = re.compile(r"sandbox|hardening|scripting", re.IGNORECASE)
RESULT_LINE = re.compile(r"\b\d+ (passed|failed|error|errors)\b.* in [\d.]+s")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def overlay_mounts(root: Path) -> list[tuple[Path, str]]:
    """(source, target) read-only mounts that lay the checkout over /app, except the code under test (backend/app),
    local environments and caches."""
    mounts: list[tuple[Path, str]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name not in SKIP_TOP:
            mounts.append((entry, f"{APP_ROOT}/{entry.name}"))
    for entry in sorted((root / "backend").iterdir(), key=lambda p: p.name):
        if entry.name not in SKIP_BACKEND:
            mounts.append((entry, f"{APP_ROOT}/backend/{entry.name}"))
    return mounts


def pytest_requirement(path: Path) -> str:
    """The pytest line of requirements-dev.txt (e.g. 'pytest>=8.3,<10')."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if PYTEST_REQUIREMENT.match(line):
            return line
    raise ci.CheckFailed(f"{path.name} no declara pytest")


def inner_command(pytest_requirement_line: str, pytest_args: Sequence[str]) -> str:
    """Shell command run in the container (cwd /app/backend, the image's WORKDIR)."""
    install = ["python", "-m", "pip", "install", "--user", "--quiet", "--no-cache-dir", "--disable-pip-version-check",
               "--no-warn-script-location", "--constraint", "requirements.lock", pytest_requirement_line]
    test = ["python", "-m", "pytest", *pytest_args]
    return " && ".join(["unset " + " ".join(IMAGE_DEPLOY_ENV), shlex.join(install), "id", "python -m pytest --version",
                        "exec " + shlex.join(test)])


def docker_args(image: str, root: Path, pytest_args: Sequence[str], *, name: str, requirement: str) -> list[str]:
    args = ["run", "--rm", "--name", name]
    for key, value in ENV.items():
        args += ["-e", f"{key}={value}"]
    for source, target in overlay_mounts(root):
        args += ["--mount", ci.bind_mount(source, target)]
    return [*args, image, "sh", "-c", inner_command(requirement, pytest_args)]


def skipped(lines: Sequence[str]) -> list[tuple[str, str]]:
    """(location, reason) of every SKIPPED line of pytest's short summary (-ra)."""
    out = []
    for line in lines:
        match = SKIPPED_LINE.match(ANSI.sub("", line).strip())
        if match:
            out.append((match.group("where"), match.group("reason")))
    return out


def skipped_total(lines: Sequence[str]) -> int:
    """Skipped tests in the short summary (a line 'SKIPPED [3] ...' groups 3 tests skipped for the same reason)."""
    total = 0
    for line in lines:
        match = SKIPPED_LINE.match(ANSI.sub("", line).strip())
        if match:
            total += int(match.group("count"))
    return total


def posix_gated_reasons(tests_dir: Path) -> frozenset[str]:
    """Reasons of the suite's skipif markers that skip a test everywhere but POSIX / Linux."""
    reasons = set()
    for path in sorted(tests_dir.glob("test_*.py")):
        for match in SKIPIF.finditer(path.read_text(encoding="utf-8", errors="replace")):
            if NOT_POSIX_CONDITION.search(match.group("cond")):
                reasons.add(match.group("reason"))
    return frozenset(reasons)


def platform_only(skips: Sequence[tuple[str, str]], gated: frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """Skips that say the test only runs on POSIX / Linux (a reason of ``gated``, or worded like it): on Linux they
    mean a broken platform check."""
    return [
        (where, reason) for where, reason in skips
        if reason in gated or (PLATFORM_ONLY.search(reason) and not ENVIRONMENT_REFUSAL.search(reason))
    ]


def result_line(lines: Sequence[str]) -> str:
    for line in reversed(lines):
        clean = ANSI.sub("", line).strip(" =")
        if RESULT_LINE.search(clean):
            return clean
    return ""


def stream(cmd: Sequence[str], *, timeout_s: float, on_timeout) -> tuple[int, list[str], bool]:
    """Runs ``cmd`` printing its output live; (exit code, output lines, timed out). ``on_timeout(proc)`` must make
    the process end (for `docker run`, kill the container: killing only the CLI would leave it running)."""
    ci.log("$ " + shlex.join(cmd))
    fired = threading.Event()
    proc = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")

    def expire() -> None:
        fired.set()
        on_timeout(proc)

    timer = threading.Timer(timeout_s, expire)
    timer.start()
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line.rstrip("\n"))
        code = proc.wait()
    finally:
        timer.cancel()
    return code, lines, fired.is_set()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Suite del backend dentro de la imagen Docker de NEXUS (Linux).")
    parser.add_argument("image", help="imagen ya construida, p. ej. nexus-terminal:ci")
    parser.add_argument("--docker", default="docker", help="programa docker (por defecto: docker)")
    parser.add_argument("pytest_args", nargs="*", help="argumentos de pytest (tras --); por defecto la suite completa")
    args = parser.parse_args(argv)
    docker = shlex.split(args.docker)
    report = ci.Report("Imagen Docker: suite del backend dentro de la imagen")
    name = f"nexus-ci-tests-{os.getpid()}"
    if not ci.require_docker(docker, report):
        return report.finish()
    try:
        requirement = pytest_requirement(ci.ROOT / "backend" / "requirements-dev.txt")
        cmd = [*docker, *docker_args(args.image, ci.ROOT, args.pytest_args or DEFAULT_PYTEST_ARGS,
                                     name=name, requirement=requirement)]

        def kill(proc: subprocess.Popen) -> None:
            try:
                subprocess.run([*docker, "kill", name], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
            proc.kill()

        code, lines, timed_out = stream(cmd, timeout_s=TIMEOUT_S, on_timeout=kill)
    except (ci.CheckFailed, OSError) as exc:
        report.fail("pytest dentro de la imagen", str(exc))
        return report.finish()
    summary = result_line(lines)
    if timed_out:
        report.fail("pytest dentro de la imagen", f"sin terminar tras {TIMEOUT_S // 60} min (contenedor detenido)")
    else:
        report.expect("pytest dentro de la imagen", [] if code == 0 else [f"código {code}: {summary or 'ver el registro'}"],
                      summary)
    if not summary:  # pytest never reached its summary (install failure, crash, timeout): the skips are unknown
        report.warn("pruebas POSIX/Linux ejecutadas en Linux", "sin resumen de pytest: no se revisaron las omitidas")
        return report.finish()
    skips = skipped(lines)
    wrong = platform_only(skips, posix_gated_reasons(ci.ROOT / "backend" / "tests"))
    report.expect("pruebas POSIX/Linux ejecutadas en Linux", [f"{where}: {reason}" for where, reason in wrong[:10]],
                  f"{skipped_total(lines)} omitidas en total, ninguna por plataforma")
    for where, reason in skips:
        if (where, reason) not in wrong and SECURITY_FILES.search(where):
            report.warn("prueba de seguridad omitida en la imagen", f"{where}: {reason}")
    return report.finish()


if __name__ == "__main__":
    sys.exit(main())
