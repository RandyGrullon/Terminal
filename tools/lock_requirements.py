"""Regenerate backend/requirements.lock: the exact versions of the runtime dependencies installed in backend/.venv
(the set the test-suite ran against), for the Docker image (python:3.13-slim, Linux).

    backend/.venv/Scripts/python.exe tools/lock_requirements.py            (Windows; backend/.venv/bin/python elsewhere)
    backend/.venv/Scripts/python.exe tools/lock_requirements.py --check    (exit 1 when the lock is out of date)

No network: versions come from importlib.metadata of the running interpreter. Dependencies whose environment markers
do not apply to the image (colorama, tzdata on Windows...) are skipped. OPTIONAL dependencies are always left out and
named in the header, whether or not they are installed here, so the lock is the same when generated or checked on
Windows, Linux or macOS: uvloop, the optional speed-up of uvicorn[standard] that does not exist on Windows (the image
runs uvicorn on the standard asyncio loop, like the tests on Windows).
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import sys
from pathlib import Path

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:  # pragma: no cover - pip vendors packaging
    from pip._vendor.packaging.requirements import Requirement  # type: ignore[no-redef]
    from pip._vendor.packaging.utils import canonicalize_name  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
LOCK = ROOT / "backend" / "requirements.lock"
# Marker environment of the Docker image (python:3.13-slim on linux/amd64)
TARGET = {
    "implementation_name": "cpython",
    "implementation_version": "3.13.0",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.13.0",
    "python_version": "3.13",
    "sys_platform": "linux",
}
OPTIONAL = {"uvloop"}  # never pinned (Linux-only, optional): installed by start.sh on Linux/macOS, absent on Windows


def read_requirements(path: Path = REQUIREMENTS) -> list[Requirement]:
    reqs = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            reqs.append(Requirement(line))
    return reqs


def applies(req: Requirement, extras: set[str]) -> bool:
    if req.marker is None:
        return True
    return any(req.marker.evaluate({**TARGET, "extra": extra}) for extra in (extras or {""}))


def closure(top: list[Requirement]) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """{canonical name: (project name, installed version)} for the runtime closure, plus the missing ones."""
    pins: dict[str, tuple[str, str]] = {}
    missing: dict[str, str] = {}
    seen: set[tuple[str, frozenset]] = set()
    stack = [(req, set(req.extras)) for req in top if applies(req, set())]
    while stack:
        req, extras = stack.pop()
        name = canonicalize_name(req.name)
        if (name, frozenset(extras)) in seen:
            continue
        seen.add((name, frozenset(extras)))
        if name in OPTIONAL:  # left out on purpose, installed here or not
            missing[name] = str(req)
            continue
        try:
            dist = md.distribution(req.name)
        except md.PackageNotFoundError:
            missing[name] = str(req)
            continue
        if not req.specifier.contains(dist.version, prereleases=True):
            raise SystemExit(f"{dist.metadata['Name']} {dist.version} no cumple {req}")
        pins[name] = (dist.metadata["Name"], dist.version)
        for spec in dist.requires or []:
            sub = Requirement(spec)
            if applies(sub, extras):
                stack.append((sub, set(sub.extras)))
    return pins, missing


def render() -> str:
    pins, missing = closure(read_requirements())
    unexpected = sorted(set(missing) - OPTIONAL)
    if unexpected:
        raise SystemExit("Faltan en este entorno (instálalas antes de regenerar): " + ", ".join(unexpected))
    lines = [
        "# NEXUS Terminal - exact runtime versions for the Docker image (python:3.13-slim, Linux). Do not edit by hand:",
        "# regenerate with `python tools/lock_requirements.py` from backend/.venv (the versions the tests ran against).",
        "# Ranges live in requirements.txt; test tools in requirements-dev.txt.",
    ]
    if missing:
        lines.append("# Left out on purpose (optional; the tests run without it): " + ", ".join(sorted(missing)))
    lines += [f"{project}=={version}" for _, (project, version) in sorted(pins.items())]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only compare with the existing lock")
    args = parser.parse_args()
    text = render()
    current = LOCK.read_text(encoding="utf-8") if LOCK.exists() else ""
    if args.check:
        if current != text:
            print("backend/requirements.lock no coincide con backend/.venv: ejecuta tools/lock_requirements.py")
            return 1
        print("backend/requirements.lock al día")
        return 0
    LOCK.write_text(text, encoding="utf-8", newline="\n")
    print(f"Escrito {LOCK.relative_to(ROOT)} ({text.count('==')} paquetes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
