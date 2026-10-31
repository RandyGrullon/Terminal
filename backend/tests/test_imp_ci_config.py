"""Improvement ci (automatic tests on GitHub, the Linux / Docker deployment tested for real): static checks of the CI
configuration, so a broken edit also fails the local test run, before a push.

- .github/workflows/ci.yml: push to main + pull requests, read-only token and no secrets, cancellation of superseded
  runs, a timeout on every job, only first-party actions, and the jobs the project relies on (SQLite, PostgreSQL 16
  service with a health check, frontend on Node 20, Docker image, Linux launcher, portability);
- Dockerfile: installs only the lock (--no-deps + pip check), non-root user, code read-only for it, PORT fallback;
- .dockerignore keeps secrets, tests and local data out without hiding what the Dockerfile copies;
- .gitattributes keeps the files read by Linux tools in LF.
"""
from __future__ import annotations

import ast
import fnmatch
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CORE_JOBS = {"backend", "backend-postgres", "frontend", "docker"}


def _workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _triggers(data: dict) -> dict:
    return data.get("on", data.get(True))  # YAML 1.1 (PyYAML) reads the key `on` as the boolean True


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _runs(job: dict) -> list[str]:
    return [str(step["run"]) for step in _steps(job) if "run" in step]


def _uses(job: dict, action: str) -> dict:
    found = [step for step in _steps(job) if str(step.get("uses", "")).startswith(action + "@")]
    assert found, f"falta {action}"
    return found[0].get("with") or {}


# ----------------------------------------------------------------------------- workflow


WORKFLOW_KEYS = {"name", "on", True, "permissions", "concurrency", "env", "jobs", "defaults", "run-name"}
JOB_KEYS = {"name", "runs-on", "timeout-minutes", "defaults", "services", "env", "steps", "needs", "if", "permissions",
            "strategy", "container", "outputs", "environment", "concurrency", "continue-on-error"}
STEP_KEYS = {"name", "uses", "with", "run", "env", "id", "if", "timeout-minutes", "working-directory", "shell",
             "continue-on-error"}
SERVICE_KEYS = {"image", "env", "ports", "options", "credentials", "volumes"}


def test_workflow_uses_only_known_keys():
    """Catches typos that YAML accepts but GitHub rejects or ignores (timeout_minutes, working_directory, ...)."""
    data = _workflow()
    assert set(data) <= WORKFLOW_KEYS
    for name, job in data["jobs"].items():
        assert set(job) <= JOB_KEYS, (name, set(job) - JOB_KEYS)
        assert set(job.get("defaults", {}).get("run", {})) <= {"working-directory", "shell"}, name
        for service in (job.get("services") or {}).values():
            assert set(service) <= SERVICE_KEYS, name
        for step in _steps(job):
            assert set(step) <= STEP_KEYS, (name, set(step) - STEP_KEYS)
            assert ("uses" in step) != ("run" in step), (name, step)  # exactly one of them
            assert step.get("name") or "uses" in step, (name, step)  # run steps are named in the log


def test_workflow_runs_on_push_to_main_and_on_pull_requests():
    triggers = _triggers(_workflow())
    assert triggers["push"]["branches"] == ["main"]
    assert "pull_request" in triggers
    assert "pull_request_target" not in triggers and "schedule" not in triggers


def test_workflow_has_a_read_only_token_and_needs_no_secrets():
    data = _workflow()
    text = WORKFLOW.read_text(encoding="utf-8")
    assert data["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "pull_request_target" not in text
    for name, job in data["jobs"].items():
        assert "permissions" not in job, name  # nothing widens the token
        checkout = _uses(job, "actions/checkout")
        assert checkout.get("persist-credentials") is False, name  # the token is not left in .git/config


def test_superseded_runs_are_cancelled_and_every_job_has_a_timeout():
    data = _workflow()
    assert data["concurrency"]["cancel-in-progress"] is True
    assert "github.ref" in data["concurrency"]["group"]
    for name, job in data["jobs"].items():
        assert job["runs-on"] == "ubuntu-latest", name
        assert 0 < job["timeout-minutes"] <= 60, name
        for step in _steps(job):
            assert step.get("timeout-minutes", 0) <= job["timeout-minutes"], (name, step.get("name"))
        # steps run one after the other: the job limit must not cut the last one short before its own limit (the
        # docker scripts stop themselves under their step's limit so they can still print why and clean up)
        assert sum(step.get("timeout-minutes", 0) for step in _steps(job)) <= job["timeout-minutes"], name


def test_only_first_party_actions_pinned_to_a_major_version():
    for name, job in _workflow()["jobs"].items():
        for step in _steps(job):
            if "uses" in step:
                assert re.fullmatch(r"actions/(checkout|setup-python|setup-node)@v\d+", step["uses"]), (name, step)


def test_the_expected_jobs_exist():
    jobs = set(_workflow()["jobs"])
    assert CORE_JOBS <= jobs
    assert {"launcher", "portability"} <= jobs


def test_the_app_runs_offline_with_utf8_in_every_job():
    env = _workflow()["env"]
    assert env["NEXUS_OFFLINE"] == "1" and env["PYTHONUTF8"] == "1"


@pytest.mark.parametrize("job_name", ["backend", "backend-postgres"])
def test_backend_jobs_install_the_dev_requirements_and_run_pytest(job_name):
    job = _workflow()["jobs"][job_name]
    assert job["defaults"]["run"]["working-directory"] == "backend"
    python = _uses(job, "actions/setup-python")
    assert str(python["python-version"]) == "3.13" and python["cache"] == "pip"
    assert "backend/requirements-dev.txt" in python["cache-dependency-path"]
    runs = _runs(job)
    assert any("pip install -r requirements-dev.txt" in run for run in runs)
    assert any(run.startswith("python -m pytest") and "-p no:cacheprovider" in run for run in runs)


def test_postgres_job_uses_a_healthy_postgres_16_service():
    job = _workflow()["jobs"]["backend-postgres"]
    service = job["services"]["postgres"]
    assert service["image"].startswith("postgres:16")
    assert "--health-cmd" in service["options"] and "pg_isready" in service["options"]
    assert "5432:5432" in service["ports"]
    assert service["env"]["POSTGRES_PASSWORD"] == "postgres"
    # localhost on purpose: app/db_pg.py only skips TLS for loopback hosts, and the service container has no TLS
    assert job["env"]["NEXUS_TEST_PG_URL"] == "postgresql://postgres:postgres@localhost:5432/postgres"


def test_frontend_job_installs_typechecks_tests_and_builds_on_node_20():
    job = _workflow()["jobs"]["frontend"]
    assert job["defaults"]["run"]["working-directory"] == "frontend"
    node = _uses(job, "actions/setup-node")
    assert str(node["node-version"]) == "20" and node["cache"] == "npm"
    assert node["cache-dependency-path"] == "frontend/package-lock.json"
    order = ["npm ci", "npm run typecheck", "npm test", "npm run build"]
    runs = _runs(job)
    positions = [next(i for i, run in enumerate(runs) if run.startswith(cmd)) for cmd in order]
    assert positions == sorted(positions)


def test_docker_job_builds_the_image_then_runs_the_linux_checks():
    runs = _runs(_workflow()["jobs"]["docker"])
    build = next(i for i, run in enumerate(runs) if run.startswith("docker build"))
    smoke = next(i for i, run in enumerate(runs) if "tools/ci/docker_smoke.py" in run)
    suite = next(i for i, run in enumerate(runs) if "tools/ci/docker_tests.py" in run)
    assert build < smoke < suite
    assert "--pull" in runs[build]


def _step(job: dict, needle: str) -> dict:
    found = [step for step in _steps(job) if needle in str(step.get("run", ""))]
    assert len(found) == 1, needle
    return found[0]


def test_later_checks_still_run_after_an_earlier_one_failed():
    """One run reports every problem: the in-image suite runs after a failed smoke test (only a failed build stops
    it), and the frontend checks run after one another failed (only a failed npm ci stops them)."""
    jobs = _workflow()["jobs"]
    build = next(step for step in _steps(jobs["docker"]) if str(step.get("run", "")).startswith("docker build"))
    suite_if = " ".join(str(_step(jobs["docker"], "tools/ci/docker_tests.py").get("if", "")).split())
    assert "!cancelled()" in suite_if and f"steps.{build['id']}.outcome == 'success'" in suite_if
    assert "if" not in _step(jobs["docker"], "tools/ci/docker_smoke.py")  # skipped when the build failed
    install = next(step for step in _steps(jobs["frontend"]) if str(step.get("run", "")).startswith("npm ci"))
    for cmd in ("npm run typecheck", "npm test", "npm run build"):
        cond = " ".join(str(_step(jobs["frontend"], cmd).get("if", "")).split())
        assert "!cancelled()" in cond and f"steps.{install['id']}.outcome == 'success'" in cond, cmd


def test_launcher_job_runs_start_sh_with_python_and_node():
    job = _workflow()["jobs"]["launcher"]
    assert str(_uses(job, "actions/setup-python")["python-version"]) == "3.13"
    assert str(_uses(job, "actions/setup-node")["node-version"]) == "20"
    assert any("tools/ci/launcher_smoke.py" in run for run in _runs(job))


def test_every_ci_script_used_by_the_workflow_exists_and_parses_on_python_3_9():
    scripts = set(re.findall(r"tools/ci/[\w.-]+\.py", WORKFLOW.read_text(encoding="utf-8")))
    assert scripts
    for rel in scripts:
        assert (ROOT / rel).is_file(), rel
    for path in sorted((ROOT / "tools" / "ci").glob("*.py")):
        # the docker / portability jobs run them with the runner's own python3, which may be older than 3.13
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))


# ----------------------------------------------------------------------------- Dockerfile / .dockerignore


def _instructions(text: str) -> list[tuple[str, str]]:
    joined = re.sub(r"\\\n", " ", text)
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            word, _, rest = line.partition(" ")
            out.append((word.upper(), rest.strip()))
    return out


def test_dockerfile_installs_exactly_the_lock():
    steps = _instructions((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    runs = " ".join(rest for word, rest in steps if word == "RUN")
    assert "pip install --no-deps -r backend/requirements.lock" in runs and "pip check" in runs
    assert [rest for word, rest in steps if word == "FROM"] == ["python:3.13-slim"]


def test_dockerfile_runs_as_a_non_root_user_with_read_only_code():
    steps = _instructions((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    users = [rest for word, rest in steps if word == "USER"]
    assert users and users[-1] == "nexus"
    runs = " ".join(rest for word, rest in steps if word == "RUN")
    chowned = re.findall(r"chown -R nexus:nexus ((?:/\S+\s*)+)", runs)
    assert chowned and set(" ".join(chowned).split()) == {"/app/data", "/app/backend/.tmp"}
    assert "python -m compileall" in runs  # the workers (python -I) cannot write __pycache__ next to root-owned code
    assert "--chown" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_keeps_the_platform_port_and_execs_the_server():
    steps = _instructions((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    cmd = [rest for word, rest in steps if word == "CMD"][-1]
    assert "${PORT:-$NEXUS_PORT}" in cmd and "exec python -m app.main" in cmd


def _dockerignore() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _ignored(path: str, patterns: list[str]) -> bool:
    """Approximation of Docker's matcher for the patterns used here: a path is excluded when it, or a parent, matches
    a pattern; '**/x' matches x in any folder."""
    parts = path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in patterns:
        for prefix in prefixes:
            if fnmatch.fnmatchcase(prefix, pattern):
                return True
            if pattern.startswith("**/") and fnmatch.fnmatchcase(prefix.rsplit("/", 1)[-1], pattern[3:]):
                return True
    return False


def test_dockerignore_keeps_secrets_tests_and_local_data_out():
    patterns = _dockerignore()
    for path in (".env", "backend/.env", ".env.local", "data/nexus.db", "backend/tests/test_core.py", ".git/config",
                 "frontend/src/main.tsx", "tools/ci/docker_smoke.py", "backend/.venv/bin/python", ".claude/settings.json",
                 "backend/app/__pycache__/main.cpython-313.pyc", "backend/.tmp/sandbox/x.txt", "certs/server.key"):
        assert _ignored(path, patterns), path


def test_dockerignore_does_not_hide_what_the_dockerfile_copies():
    patterns = _dockerignore()
    steps = _instructions((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    sources = [rest.split()[0] for word, rest in steps if word == "COPY"]
    assert {"backend/requirements.lock", "backend/app", "docs"} <= set(sources)
    for source in sources + ["backend/app/main.py", "backend/app/data/hub.py", "backend/app/scripting/worker.py"]:
        assert not _ignored(source, patterns), source


# ----------------------------------------------------------------------------- line endings


def test_gitattributes_keeps_linux_files_lf():
    rules = {line.strip() for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()}
    assert {"*.sh text eol=lf", "Dockerfile text eol=lf", ".dockerignore text eol=lf"} <= rules


def test_linux_files_have_no_cr_in_this_checkout():
    for rel in ("Dockerfile", ".dockerignore", "start.sh"):
        assert b"\r" not in (ROOT / rel).read_bytes(), rel


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="sin repositorio git")
def test_linux_files_are_lf_in_git():
    proc = subprocess.run(["git", "-c", "safe.directory=*", "ls-files", "--eol", "--", "Dockerfile", ".dockerignore",
                           "start.sh"], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:  # another user's checkout or a transient failure of git: nothing to compare
        pytest.skip(f"git ls-files no disponible: {proc.stderr.strip()[:120]}")
    out = proc.stdout
    rows = [line.split("\t", 1) for line in out.splitlines() if "\t" in line]
    assert rows
    for info, path in rows:
        assert info.split()[0] in ("i/lf", "i/none"), (path, info)
