"""Reproducible backend builds and launchers (findings unpinned-backend-deps, start-sh-not-executable,
readme-start-ps1-execution-policy): capped ranges, pytest only in requirements-dev.txt, an exact lock that matches the
tested environment and is what the Docker image installs, start.sh executable, Windows docs use start.bat."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent


def _lock_tool():
    spec = importlib.util.spec_from_file_location("lock_requirements", ROOT / "tools" / "lock_requirements.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lock_pins() -> dict[str, str]:
    pins = {}
    for line in (BACKEND / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.+\-]+)", line.strip())
        assert m, f"línea no exacta en requirements.lock: {line!r}"
        pins[m.group(1).lower().replace("_", "-").replace(".", "-")] = m.group(2)
    return pins


def test_runtime_requirements_are_capped_and_exclude_test_tools():
    tool = _lock_tool()
    reqs = tool.read_requirements()
    assert reqs, "requirements.txt vacío"
    for req in reqs:
        assert any(spec.operator == "<" for spec in req.specifier), f"{req} sin tope de versión mayor"
    names = {req.name.lower() for req in reqs}
    assert "pytest" not in names
    assert {"numpy", "pandas", "fastapi", "uvicorn"} <= names
    dev = (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in dev and re.search(r"^pytest\b", dev, re.M)


def test_lock_is_complete_and_within_requirement_ranges():
    """Deterministic (does not depend on the exact installed versions): every runtime requirement is pinned in the
    lock, every such pin satisfies its requirements.txt range, and the lock carries no test-only tool."""
    tool = _lock_tool()
    pins = _lock_pins()
    assert "pytest" not in pins
    for req in tool.read_requirements():
        name = req.name.lower()
        assert name in pins, f"{name} falta en requirements.lock"
        assert req.specifier.contains(pins[name], prereleases=True), f"{name}=={pins[name]} no cumple {req}"


@pytest.mark.skipif(
    os.environ.get("NEXUS_CHECK_LOCK_ENV") != "1",
    reason="set NEXUS_CHECK_LOCK_ENV=1 to assert the lock equals this venv (varies after any in-range upstream release)",
)
def test_lock_matches_this_exact_venv():
    """Opt-in drift alarm: the lock is exactly what tools/lock_requirements.py renders from the installed venv. Off by
    default because a fresh clone installs the requirements.txt ranges, so any in-range upstream release since the lock
    was generated would fail this without any code change; run it (or `python tools/lock_requirements.py --check`) when
    building the image or after bumping dependencies."""
    tool = _lock_tool()
    assert (BACKEND / "requirements.lock").read_text(encoding="utf-8") == tool.render()


def test_docker_image_installs_the_lock_only():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    installs = [line for line in dockerfile.splitlines() if "pip install" in line]
    assert installs and all("requirements.lock" in line for line in installs)
    assert "requirements-dev" not in dockerfile and "requirements.txt" not in dockerfile


def test_start_scripts_install_dev_requirements():
    for script in ("start.ps1", "start.sh"):
        text = (ROOT / script).read_text(encoding="utf-8-sig")
        assert "requirements-dev.txt" in text, f"{script} no instala requirements-dev.txt"


def _commands(markdown: str) -> list[str]:
    """Lines inside fenced code blocks (what readers copy and run)."""
    out, inside = [], False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
        elif inside:
            out.append(line.strip())
    return out


def test_docs_never_tell_windows_users_to_run_start_ps1_directly():
    for doc in ("README.md", "docs/DEPLOY.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        # the default Windows execution policy (Restricted) refuses `.\start.ps1`; start.bat passes Bypass
        assert not [c for c in _commands(text) if c.startswith(".\\start.ps1")], doc
        assert ".\\start.bat" in text
    assert "bash start.sh" in (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="sin repositorio git")
def test_start_sh_is_executable_in_git():
    # safe.directory=* for this read-only call only: a checkout owned by another user (a container with the checkout
    # mounted, a CI cache) makes git refuse it as "dubious ownership" otherwise
    out = subprocess.run(["git", "-c", "safe.directory=*", "ls-files", "-s", "start.sh"], cwd=ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip(f"git no puede leer este repositorio aquí: {out.stderr.strip()[:200]}")
    assert out.stdout.startswith("100755 "), out.stdout
