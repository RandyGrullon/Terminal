"""Improvement ci: the logic of the CI scripts in tools/ci. They drive Docker and bash on the Linux runners (which a
development machine may not have), so every decision they take is checked here: health / identity checks, the
fail-closed expectations (against the real app entry point), the PyChart probes (against the real sandbox, so on
Linux, e.g. inside the image, they run under the real POSIX limits), the Docker smoke flow with a fake docker, the
in-image test run (mounts, skip analysis), the launcher and the portability checks.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
CI_DIR = ROOT / "tools" / "ci"

sys.path.insert(0, str(CI_DIR))
try:
    import ci_common as ci  # noqa: E402
    import docker_smoke  # noqa: E402
    import docker_tests  # noqa: E402
    import launcher_smoke  # noqa: E402
    import portability  # noqa: E402
finally:
    sys.path.remove(str(CI_DIR))

HEALTHY = {
    "ok": True, "version": "1.0.0", "auth": "none", "role": "single", "load_errors": [],
    "db": {"path": "/app/data/nexus.db", "size_bytes": 1, "dialect": "sqlite"},
    "services": {"hub": {"running": True}, "macro": {"running": True}, "scanner": {"running": False}},
}
NEXUS_STATUS = "Name:\tpython\nUid:\t10001\t10001\t10001\t10001\nGid:\t1000\t1000\t1000\t1000\nCapEff:\t0000000000000000\n"


@pytest.fixture(autouse=True)
def _not_on_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


# ----------------------------------------------------------------------------- ci_common


def test_check_health_accepts_a_single_local_instance():
    assert ci.check_health(HEALTHY, db_path_prefix="/app/data/") == []
    assert ci.services_summary(HEALTHY) == "2 de 3 servicios en marcha"


def test_check_health_lists_every_problem():
    bad = {**HEALTHY, "ok": False, "role": "standby", "auth": "required", "load_errors": ["app.api.x: ImportError"],
           "db": {"path": "/tmp/x.db", "dialect": "postgres"}, "services": {}}
    problems = " | ".join(ci.check_health(bad, db_path_prefix="/app/data/"))
    for part in ("ok = False", "role = 'standby'", "auth = 'required'", "db.dialect = 'postgres'", "/tmp/x.db",
                 "1 módulo sin cargar: app.api.x", "sin estado de servicios"):
        assert part in problems
    assert ci.check_health(["no", "dict"]) == ["/api/health no devolvió un objeto JSON"]
    two = ci.check_health({**HEALTHY, "load_errors": ["a", "b"]})
    assert any(p.startswith("2 módulos sin cargar") for p in two)


def test_wait_for_health_polls_until_the_server_is_ready():
    answers = iter([ConnectionRefusedError("rechazada"), (503, None), (200, {"ok": False}), (200, HEALTHY)])
    sleeps: list[float] = []

    def fetch(url, timeout):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    out = ci.wait_for_health("http://x/api/health", timeout_s=60, interval_s=0.5, fetch=fetch, clock=lambda: 0.0,
                             sleep=sleeps.append)
    assert out is HEALTHY and sleeps == [0.5, 0.5, 0.5]


def test_wait_for_health_stops_when_the_server_died_or_after_the_timeout():
    with pytest.raises(ci.CheckFailed, match="terminó antes de responder"):
        ci.wait_for_health("http://x", timeout_s=60, alive=lambda: False, fetch=lambda *a, **k: (200, HEALTHY))
    now = iter(range(0, 1000, 10))
    with pytest.raises(ci.CheckFailed, match=r"no respondió en 25 s \(último intento: HTTP 503\)"):
        ci.wait_for_health("http://x", timeout_s=25, fetch=lambda *a, **k: (503, None), clock=lambda: next(now),
                           sleep=lambda s: None)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            self._send(200, json.dumps({**HEALTHY, "ua": self.headers.get("User-Agent")}).encode())
        elif self.path == "/busy":
            self._send(503, b'{"detail": "Instancia en espera"}')
        else:
            self._send(200, b"<html>no json</html>", "text/html")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._send(200, json.dumps({"echo": json.loads(self.rfile.read(length))}).encode())


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_http_probes_against_a_real_loopback_server(local_server):
    status, body = ci.http_json(f"{local_server}/api/health")
    assert status == 200 and body["ok"] is True and body["ua"] == ci.USER_AGENT  # honest, identifiable client
    assert ci.http_json(f"{local_server}/busy") == (503, {"detail": "Instancia en espera"})  # 5xx returned, not raised
    assert ci.http_json(f"{local_server}/page") == (200, None)
    assert ci.http_json(f"{local_server}/run", method="POST", body={"a": 1}) == (200, {"echo": {"a": 1}})
    status, headers, raw = ci.http_request(f"{local_server}/page", accept="text/html")
    assert status == 200 and headers["content-type"] == "text/html" and raw.startswith(b"<html>")
    assert ci.wait_for_health(f"{local_server}/api/health", timeout_s=10)["role"] == "single"


def test_identity_checks_from_proc_status():
    assert ci.check_unprivileged(ci.parse_proc_status(NEXUS_STATUS)) == []
    root = ci.parse_proc_status("Uid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\nCapEff:\t00000000a80425fb\n")
    problems = " | ".join(ci.check_unprivileged(root))
    assert "usuario root" in problems and "grupo root" in problems and "capacidades" in problems
    assert len(ci.check_unprivileged({})) == 3  # nothing to prove it: not accepted
    setuid = ci.parse_proc_status(NEXUS_STATUS.replace("10001\t10001\t10001\t10001", "10001\t0\t10001\t10001"))
    assert any("usuario root" in p for p in ci.check_unprivileged(setuid))


def test_report_exit_code_annotations_and_run_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report = ci.Report("Prueba")
    report.ok("uno", "bien")
    report.warn("dos", "lento: 9 s")
    assert report.expect("tres", ["a", "b|c"]) is False
    assert report.finish() == 1
    out = capsys.readouterr().out
    assert "::warning title=dos::lento%3A 9 s" not in out and "::warning title=dos::lento: 9 s" in out
    assert "::error title=tres::a; b|c" in out
    assert "Prueba: 3 comprobaciones · 1 fallo · 1 aviso" in out
    table = summary.read_text(encoding="utf-8")
    assert "| FALLO | tres | a; b/c |" in table and "| OK | uno | bien |" in table
    clean = ci.Report("Limpio")
    clean.ok("x")
    assert clean.finish() == 0


def test_annotation_escaping(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    ci.annotate("error", "100% roto\nlínea 2", title="a: b, c")
    assert capsys.readouterr().out.strip() == "::error title=a%3A b%2C c::100%25 roto%0Alínea 2"


def test_run_and_bind_mount_helpers(tmp_path):
    cp = ci.run([sys.executable, "-c", "print('hola')"], timeout=60, echo=False)
    assert cp.returncode == 0 and cp.stdout.strip() == "hola"
    with pytest.raises(ci.CheckFailed, match="no terminó en 0.5 s"):
        ci.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5, echo=False)
    with pytest.raises(ci.CheckFailed, match="no se encontró el programa"):
        ci.run(["nexus-ci-programa-inexistente"], timeout=5, echo=False)
    assert ci.bind_mount(tmp_path, "/app/x") == f"type=bind,source={tmp_path},target=/app/x,readonly"
    with pytest.raises(ci.CheckFailed):
        ci.bind_mount("/a,b", "/app/x")


# ----------------------------------------------------------------------------- docker_smoke: expectations


def test_server_container_listens_on_its_own_loopback_only_with_the_platform_port():
    args = docker_smoke.server_args("img:ci", "nexus-ci-1", 18420)
    assert args[:2] == ["run", "--detach"] and args[-1] == "img:ci"
    env = {args[i + 1] for i, arg in enumerate(args) if arg == "-e"}
    assert env == {"NEXUS_HOST=127.0.0.1", "PORT=18420", "NEXUS_OFFLINE=1"}  # PORT: injected like Railway does
    # nothing published, no host network, no extra privileges: only the container itself can reach the server
    assert not {"-p", "--publish", "--network", "--privileged", "--user", "--cap-add"} & set(args)


def test_the_in_container_probe_speaks_http_like_http_json(local_server):
    """PROBE is the script `docker exec` runs with the image's Python: run here with this Python."""
    def probe(url, method="GET", body=""):
        cp = subprocess.run([sys.executable, "-c", docker_smoke.PROBE, url, method, body, "5"], capture_output=True,
                            text=True, timeout=60, stdin=subprocess.DEVNULL)
        return cp.returncode, cp.stdout

    code, out = probe(f"{local_server}/api/health")
    envelope = json.loads(out)
    assert code == 0 and envelope["status"] == 200 and json.loads(envelope["body"])["ua"] == ci.USER_AGENT
    assert json.loads(probe(f"{local_server}/busy")[1])["status"] == 503  # 5xx answered, not raised
    assert json.loads(json.loads(probe(f"{local_server}/run", "POST", '{"a": 1}')[1])["body"]) == {"echo": {"a": 1}}
    assert probe("http://127.0.0.1:9/api/health")[0] != 0  # nothing listening: non-zero exit, "not ready yet"


def test_container_http_turns_docker_exec_into_http_json(local_server):
    class LocalExec:  # stands in for `docker exec <name> python -c ...`: runs the probe with this Python
        def __call__(self, *args, timeout, echo=True):
            assert args[:4] == ("exec", "nexus-ci-9", "python", "-c")
            return subprocess.run([sys.executable, *args[3:]], capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)

    fetch = docker_smoke.ContainerHttp(LocalExec(), "nexus-ci-9")
    assert fetch(f"{local_server}/api/health")[1]["role"] == "single"
    assert fetch(f"{local_server}/page") == (200, None)
    assert fetch(f"{local_server}/run", method="POST", body={"b": 2}) == (200, {"echo": {"b": 2}})
    with pytest.raises(ConnectionError):
        fetch("http://127.0.0.1:9/api/health", timeout=2)
    assert ci.wait_for_health(f"{local_server}/api/health", timeout_s=10, fetch=fetch)["ok"] is True


def test_smoke_expectations_match_the_app_and_use_no_real_credentials():
    main_py = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    for _label, env, _code, reason in docker_smoke.FAIL_CLOSED:
        assert reason in main_py  # a reworded message fails here, not only on the Linux runner
        for key, value in env.items():
            if key in ("NEXUS_SUPABASE_URL", "NEXUS_ALLOWED_EMAILS"):
                assert value.endswith(".invalid")  # reserved TLD (RFC 2606): never resolves
            if key == "NEXUS_SUPABASE_ANON_KEY":
                assert "placeholder" in value
    assert "Memoria insuficiente" in (BACKEND / "app" / "scripting" / "worker.py").read_text(encoding="utf-8")
    contract = (ROOT / "tools" / "contract_check.py").read_text(encoding="utf-8")
    assert "comprobaciones · {bad} fallos · {warns} avisos" in contract


@pytest.mark.parametrize("case", docker_smoke.FAIL_CLOSED, ids=[c[0] for c in docker_smoke.FAIL_CLOSED])
def test_the_app_entry_point_really_fails_closed(case, tmp_path):
    """The image's CMD runs `python -m app.main` with NEXUS_HOST=0.0.0.0; same variables here, same expectation."""
    _label, extra, expected, reason = case
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
    env.update({"NEXUS_HOST": "0.0.0.0", "NEXUS_PORT": "8559", "NEXUS_OFFLINE": "1", "NEXUS_DATA_DIR": str(tmp_path),
                "NEXUS_CONSOLE_EVENTS": "0", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **extra})
    cp = subprocess.run([sys.executable, "-m", "app.main"], cwd=BACKEND, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL)
    output = cp.stdout + cp.stderr
    assert docker_smoke.fail_closed_problems(cp.returncode, output, expected, reason) == [], output[-3000:]


def test_fail_closed_verdicts():
    assert docker_smoke.fail_closed_problems(2, "... sin NEXUS_DATABASE_URL ...", 2, "sin NEXUS_DATABASE_URL") == []
    assert docker_smoke.fail_closed_problems(1, "RuntimeError: NEXUS_ALLOWED_EMAILS", None, "NEXUS_ALLOWED_EMAILS") == []
    assert docker_smoke.fail_closed_problems(0, "", None, "x") == ["terminó con código 0", "falta el motivo «x» en la salida"]
    assert docker_smoke.fail_closed_problems(1, "x", 2, "x") == ["código 1 (se esperaba 2)"]


@pytest.fixture(scope="module")
def sandbox_candles():
    from app.sim.synthetic import generate_candleset

    return generate_candleset(n=300, seed=4, tf="1h", end_time=1_760_000_400).to_list()


def test_the_smoke_scripts_behave_as_expected_in_the_real_sandbox(sandbox_candles):
    # On Linux (CI host, inside the image) this runs under the worker's real RLIMIT_AS; on Windows, its job object.
    from app.scripting import sandbox

    ok = sandbox.run_script(docker_smoke.SCRIPT_OK, sandbox_candles)
    assert docker_smoke.script_ok_problems(200, ok) == [], ok["errors"]
    bomb = sandbox.run_script(docker_smoke.SCRIPT_MEMORY_BOMB, sandbox_candles)
    assert docker_smoke.memory_bomb_problems(200, bomb) == [], bomb["errors"]


def test_api_probe_verdicts():
    assert docker_smoke.script_ok_problems(503, None) == ["HTTP 503"]
    failed = {"ok": False, "errors": [{"message": "boom"}], "plots": []}
    assert docker_smoke.script_ok_problems(200, failed) == ["ok = False: boom", "sin plots en el resultado"]
    assert docker_smoke.memory_bomb_problems(200, {"ok": True, "plots": [1]})[0].startswith("el script de 4 GB terminó bien")
    assert docker_smoke.memory_bomb_problems(200, failed) == ["error inesperado: boom"]
    events = {"events": [{"level": "info", "source": "system", "message": "listo"},
                         {"level": "error", "source": "scanner", "message": "falló"}]}
    assert docker_smoke.startup_error_problems(200, events) == (["scanner: falló"], 2)
    assert docker_smoke.startup_error_problems(500, None) == (["GET /api/events: HTTP 500"], 0)


@pytest.mark.parametrize("code, elapsed, problem, warning", [
    ("0", 1.2, False, False), ("143", 2.0, False, False), ("137", 30.5, True, False), ("1", 0.5, True, False),
    ("0", 8.0, False, True),
])
def test_stop_verdict(code, elapsed, problem, warning):
    problems, warn = docker_smoke.stop_verdict(code, elapsed)
    assert bool(problems) is problem and bool(warn) is warning
    if warning:
        assert "Fly.io" in warn


def test_contract_check_runs_from_the_image_with_read_only_mounts():
    args = docker_smoke.contract_args("img:ci", "nexus-ci-9", "http://127.0.0.1:18420")
    assert args[args.index("--network") + 1] == "container:nexus-ci-9"  # the server's own loopback
    mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    assert len(mounts) == 2 and all(m.endswith(",readonly") for m in mounts)
    assert any("target=/app/tools/contract_check.py" in m for m in mounts)
    assert any("target=/app/frontend/src/api" in m for m in mounts)
    assert args[args.index("img:ci") + 1:] == ["python", "/app/tools/contract_check.py", "--base",
                                                "http://127.0.0.1:18420", "--symbol", "SIM:NEXUS"]
    assert (ROOT / "frontend" / "src" / "api" / "types.ts").is_file()
    out = "\x1b[92mOK  \x1b[0m health Health\n\n99 comprobaciones · 0 fallos · 4 avisos\n"
    assert docker_smoke.contract_detail(out) == "99 comprobaciones · 0 fallos · 4 avisos"


# ----------------------------------------------------------------------------- docker_smoke: the whole flow, fake docker

ROOT_STATUS = "Uid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\nCapEff:\t00000000a80425fb\n"


class FakeDocker:
    """Answers the docker CLI like a healthy image would; ``overrides`` breaks one answer."""

    def __init__(self, **overrides):
        self.calls: list[list[str]] = []
        self.overrides = overrides

    def __call__(self, cmd, *, timeout, echo=True, cwd=None, env=None):
        args = [str(a) for a in cmd[1:]]
        self.calls.append(args)
        out = self.answer(args)
        code, stdout = out if isinstance(out, tuple) else (0, out)
        return subprocess.CompletedProcess(list(cmd), code, stdout, "")

    def answer(self, args):
        sub = args[0]
        env = {args[i + 1] for i, arg in enumerate(args) if arg == "-e"}
        if sub == "version":
            return self.overrides.get("version", (0, "29.0.0"))
        if sub == "run" and "--detach" in args:
            return "c0ffee"
        if sub == "run" and "--network" in args:  # contract check
            return "99 comprobaciones · 0 fallos · 4 avisos"
        if sub == "run":  # fail-closed cases
            if "NEXUS_ALLOWED_EMAILS=ci@example.invalid" in env:
                return 2, "ERROR: servidor público sin NEXUS_DATABASE_URL"
            if "NEXUS_AUTH=supabase" in env:
                return 1, "RuntimeError: NEXUS_AUTH=supabase requiere NEXUS_ALLOWED_EMAILS"
            return self.overrides.get("login", (2, "ERROR: el servidor escucharía en una interfaz pública sin login."))
        if sub == "inspect":
            if args[2] == "{{.State.Running}}":
                return "false" if self.overrides.get("down") else "true"
            return self.overrides.get("exit", "0")
        if sub == "image":
            return "nexus" if args[3] == "{{.Config.User}}" else "250000000"
        if sub == "exec":
            what = args[2:]
            if what[:3] == ["python", "-c", docker_smoke.PROBE]:  # HTTP from inside the container
                return self.http(*what[3:6])
            if what == ["cat", "/proc/1/status"]:
                return self.overrides.get("status", NEXUS_STATUS)
            if what == ["cat", "/proc/1/cmdline"]:
                return "python\x00-m\x00app.main\x00"
            if what[:2] == ["python", "-c"]:
                return "cpython-313" if "cache_tag" in what[2] else ""
            if what[0] == "test":
                expected = {p.format(tag="cpython-313"): e for _f, p, e, _l in docker_smoke.FS_EXPECTATIONS}
                return (0 if expected[what[2]] else 1), ""
        return ""  # stop, logs, rm

    def http(self, url, method, body):
        if self.overrides.get("down"):
            return 1, ""  # connection refused
        if url.endswith("/api/health"):
            status, payload = 200, HEALTHY
        elif "/api/events" in url:
            status, payload = 200, {"events": [{"level": "success", "source": "system", "message": "NEXUS listo"}]}
        elif "np.ones" in json.loads(body or "{}").get("code", ""):
            status, payload = 200, {"ok": False, "errors": [{"message": "Memoria insuficiente (MemoryError)"}]}
        else:
            status, payload = 200, {"ok": True, "errors": [], "plots": [{"title": "sma"}]}
        return 0, json.dumps({"status": status, "body": json.dumps(payload)})


@pytest.fixture
def fake_docker(monkeypatch):
    def install(**overrides):
        fake = FakeDocker(**overrides)
        monkeypatch.setattr(ci, "run", fake)
        return fake

    return install


def test_smoke_flow_with_a_healthy_image(fake_docker, capsys):
    fake = fake_docker()
    assert docker_smoke.main(["img:ci", "--port", "18999"]) == 0
    subs = [call[0] for call in fake.calls]
    assert subs.count("run") == 5  # 3 fail-closed starts + the server + the contract check
    probes = [call[5] for call in fake.calls if call[:5] == ["exec", call[1], "python", "-c", docker_smoke.PROBE]]
    assert probes[0] == "http://127.0.0.1:18999/api/health" and len(probes) == 4  # health, events, 2 scripts
    assert fake.calls[subs.index("stop")][:3] == ["stop", "-t", "30"]
    assert subs[-2:] == ["logs", "rm"]  # always cleaned up
    out = capsys.readouterr().out
    assert " · 0 fallos · 0 avisos" in out and "FALLO" not in out


def test_smoke_flow_reports_an_open_server_a_root_process_and_a_sigkill(fake_docker, capsys):
    fake = fake_docker(login=(0, ""), status=ROOT_STATUS, exit="137")
    assert docker_smoke.main(["img:ci"]) == 1
    out = capsys.readouterr().out
    for text in ("FALLO falla cerrado sin login", "usuario root", "capacidades", "SIGKILL"):
        assert text in out
    assert [call[0] for call in fake.calls][-2:] == ["logs", "rm"]


def test_both_docker_scripts_stop_at_once_without_a_docker_daemon(fake_docker, capsys):
    fake = fake_docker(version=(1, ""))  # e.g. Docker Desktop not running
    assert docker_smoke.main(["img:ci"]) == 1
    assert docker_tests.main(["img:ci"]) == 1
    assert [call[0] for call in fake.calls] == ["version", "version"]  # nothing else was attempted
    assert capsys.readouterr().out.count("FALLO Docker disponible · el daemon de Docker no responde") == 2


def test_smoke_flow_cleans_up_when_the_server_dies_before_answering(fake_docker, capsys):
    fake = fake_docker(down=True)  # the container exits at once: no HTTP answer, not running
    assert docker_smoke.main(["img:ci"]) == 1
    assert "terminó antes de responder" in capsys.readouterr().out
    subs = [call[0] for call in fake.calls]
    assert "stop" not in subs and subs[-2:] == ["logs", "rm"]


# ----------------------------------------------------------------------------- docker_tests (suite inside the image)


def _tree(root: Path, entries: list[str]) -> None:
    for rel in entries:
        path = root / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")


def test_overlay_lays_the_checkout_over_app_but_never_the_code(tmp_path):
    _tree(tmp_path, [".git/", "data/", "docs/a.md", "tools/x.py", "frontend/src/main.tsx", "README.md",
                     ".github/workflows/ci.yml", "backend/app/main.py", "backend/tests/test_x.py", "backend/.venv/",
                     "backend/.tmp/", "backend/__pycache__/", "backend/pytest.ini", "backend/requirements.lock"])
    targets = [target for _source, target in docker_tests.overlay_mounts(tmp_path)]
    assert targets == ["/app/.github", "/app/README.md", "/app/docs", "/app/frontend", "/app/tools",
                       "/app/backend/pytest.ini", "/app/backend/requirements.lock", "/app/backend/tests"]
    real = {target for _source, target in docker_tests.overlay_mounts(ROOT)}
    assert "/app/backend/app" not in real and not any(t.startswith("/app/backend/app/") for t in real)
    assert {"/app/backend/tests", "/app/backend/pytest.ini", "/app/tools", "/app/frontend", "/app/docs"} <= real


def test_container_command_adds_only_pytest_and_runs_the_whole_suite(tmp_path):
    line = docker_tests.pytest_requirement(BACKEND / "requirements-dev.txt")
    assert line.startswith("pytest") and "<" in line
    other = tmp_path / "req.txt"
    other.write_text("-r requirements.txt\npytest-asyncio>=1  # a plugin, not pytest itself\n", encoding="utf-8")
    with pytest.raises(ci.CheckFailed):
        docker_tests.pytest_requirement(other)
    command = docker_tests.inner_command("pytest>=8.3,<10", docker_tests.DEFAULT_PYTEST_ARGS)
    reset, install, who, version, run = command.split(" && ")
    assert reset == "unset " + " ".join(docker_tests.IMAGE_DEPLOY_ENV)  # the image's deployment variables go first
    assert install.startswith("python -m pip install --user") and "--constraint requirements.lock" in install
    assert install.endswith("'pytest>=8.3,<10'") and who == "id" and version == "python -m pytest --version"
    assert run == "exec python -m pytest -q -p no:cacheprovider -ra --durations=15"
    args = docker_tests.docker_args("img:ci", ROOT, ["-q"], name="t", requirement="pytest")
    assert args[:4] == ["run", "--rm", "--name", "t"] and args[-4:-1] == ["img:ci", "sh", "-c"]
    assert {"NEXUS_OFFLINE=1", "NEXUS_CHECK_LOCK_ENV=1"} <= {args[i + 1] for i, arg in enumerate(args) if arg == "-e"}
    assert all(args[i + 1].endswith(",readonly") for i, arg in enumerate(args) if arg == "--mount")


PYTEST_TAIL = [
    "\x1b[33mSKIPPED\x1b[0m [1] tests/test_fix_int_infra_sandbox.py:61: POSIX rlimits only (resource module)",
    "SKIPPED [2] tests/test_cloud.py:176: set NEXUS_TEST_PG_URL to run PostgreSQL integration tests",
    "SKIPPED [1] tests/test_imp_sandbox_os_linux.py:40: Linux only, but unshare was refused here (EPERM)",
    "SKIPPED [1] tests/test_x.py:3: solo Linux",
    "SKIPPED [1] tests/test_w.py:9: Windows job object only",
    "SKIPPED [1] tests/test_y.py:7: requiere el proceso 7 (gated)",
    "1450 passed, 7 skipped in 80.12s (0:01:20)",
]


def test_skip_analysis_flags_posix_only_tests_skipped_on_linux():
    skips = docker_tests.skipped(PYTEST_TAIL)
    assert len(skips) == 6 and skips[0] == ("tests/test_fix_int_infra_sandbox.py:61", "POSIX rlimits only (resource module)")
    wrong = docker_tests.platform_only(skips, frozenset({"requiere el proceso 7 (gated)"}))
    assert [where for where, _ in wrong] == ["tests/test_fix_int_infra_sandbox.py:61", "tests/test_x.py:3", "tests/test_y.py:7"]
    assert docker_tests.skipped_total(PYTEST_TAIL) == 7  # [2] counts two tests
    assert docker_tests.result_line(PYTEST_TAIL) == "1450 passed, 7 skipped in 80.12s (0:01:20)"
    assert docker_tests.result_line(["===== 2 failed, 3 passed in 1.00s ====="]) == "2 failed, 3 passed in 1.00s"


def test_suite_flow_judges_the_run_and_its_skips(fake_docker, monkeypatch, capsys):
    fake_docker()  # the daemon answers
    seen = {}

    def fake_stream(cmd, *, timeout_s, on_timeout):
        seen["cmd"] = cmd
        return 0, PYTEST_TAIL, False

    monkeypatch.setattr(docker_tests, "stream", fake_stream)
    assert docker_tests.main(["img:ci"]) == 1  # pytest passed, but POSIX-only tests were skipped on Linux
    out = capsys.readouterr().out
    assert "FALLO pruebas POSIX/Linux ejecutadas en Linux" in out and "test_fix_int_infra_sandbox.py:61" in out
    assert "AVISO prueba de seguridad omitida en la imagen" in out and "(EPERM)" in out  # reported, not fatal
    assert seen["cmd"][:3] == ["docker", "run", "--rm"] and seen["cmd"][-3:-1] == ["sh", "-c"]
    clean = [line for line in PYTEST_TAIL if "POSIX rlimits" not in line and "solo Linux" not in line]
    monkeypatch.setattr(docker_tests, "stream", lambda cmd, **kw: (0, clean, False))
    assert docker_tests.main(["img:ci"]) == 0
    monkeypatch.setattr(docker_tests, "stream", lambda cmd, **kw: (1, ["2 failed, 3 passed in 1.00s"], False))
    assert docker_tests.main(["img:ci"]) == 1
    monkeypatch.setattr(docker_tests, "stream", lambda cmd, **kw: (-9, ["x"], True))
    assert docker_tests.main(["img:ci"]) == 1
    assert "sin terminar tras 22 min" in capsys.readouterr().out


def test_the_suite_posix_only_skips_are_known_to_the_guard(tmp_path):
    reasons = docker_tests.posix_gated_reasons(BACKEND / "tests")
    assert "POSIX rlimits only (resource module)" in reasons  # tests/test_fix_int_infra_sandbox.py
    mark = "@pytest.mark." + "skip" + "if("  # spelled apart, so this file adds no reason to the scan above
    (tmp_path / "test_a.py").write_text(
        f'pytestmark = {mark[1:]}not sys.platform.startswith("linux"), reason="capas del kernel")\n'
        f'{mark}os.name == "nt", reason="rlimits")\n'
        f'{mark}not hasattr(os, "killpg"), reason="sin killpg")\n'
        f'{mark}\n    sys.platform == "win32",\n    reason="multilínea",\n)\n', encoding="utf-8")
    assert docker_tests.posix_gated_reasons(tmp_path) == {"capas del kernel", "rlimits", "multilínea"}
    assert not {"capas del kernel", "rlimits", "multilínea"} & reasons


def test_stream_prints_collects_and_times_out():
    code, lines, timed_out = docker_tests.stream([sys.executable, "-c", "print('a'); print('b')"], timeout_s=60,
                                                 on_timeout=lambda proc: proc.kill())
    assert (code, lines, timed_out) == (0, ["a", "b"], False)
    slow = "import time; print('x', flush=True); time.sleep(60)"
    code, lines, timed_out = docker_tests.stream([sys.executable, "-c", slow], timeout_s=1.0,
                                                 on_timeout=lambda proc: proc.kill())
    assert timed_out and code != 0 and lines == ["x"]


# ----------------------------------------------------------------------------- launcher_smoke

BUILT_INDEX = ('<!doctype html><html><head><script type="module" crossorigin src="/assets/index-AbC123.js"></script>'
               '<link rel="stylesheet" crossorigin href="/assets/index-Xy.css"></head><body><div id="root"></div></body>'
               '</html>')


def test_ui_check_accepts_the_built_index_and_rejects_the_others():
    assert launcher_smoke.ui_problems(BUILT_INDEX) == ([], ["/assets/index-AbC123.js", "/assets/index-Xy.css"])
    dev, _ = launcher_smoke.ui_problems((ROOT / "frontend" / "index.html").read_text(encoding="utf-8"))
    assert any("desarrollo" in p for p in dev) and any("/assets/" in p for p in dev)
    import app.main as app_main

    not_built = getattr(app_main, "_NOT_BUILT", "Ejecuta npm run build")
    problems, _ = launcher_smoke.ui_problems(not_built)
    assert any("no está compilada" in p for p in problems)


def test_launcher_refuses_to_run_on_windows(monkeypatch, capsys):
    monkeypatch.setattr(launcher_smoke, "os", types.SimpleNamespace(name="nt"))
    assert launcher_smoke.main([]) == 2
    assert "solo para Linux/macOS" in capsys.readouterr().out


needs_killpg = pytest.mark.skipif(not hasattr(os, "killpg"), reason="sin grupos de procesos (os.killpg) aquí")


def _session(code: str) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "listo"
    return proc


@needs_killpg
def test_stop_sends_sigterm_to_the_process_group():
    proc = _session("import signal, sys, time\nsignal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
                    "print('listo', flush=True)\ntime.sleep(60)\n")
    report = ci.Report("t")
    launcher_smoke.stop(proc, report)
    assert report.rows[-1][0] == ci.Report.OK and "código 0" in report.rows[-1][2]


@needs_killpg
def test_stop_kills_a_server_that_ignores_sigterm(monkeypatch):
    monkeypatch.setattr(launcher_smoke, "STOP_GRACE_S", 0.5)
    proc = _session("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "print('listo', flush=True)\ntime.sleep(60)\n")
    report = ci.Report("t")
    launcher_smoke.stop(proc, report)
    assert report.rows[-1][0] == ci.Report.FAIL and "SIGKILL" in report.rows[-1][2]
    assert proc.poll() is not None


# ----------------------------------------------------------------------------- own limits under the CI step limits


def test_scripts_stop_on_their_own_before_their_ci_step_is_killed():
    """Each script ends by itself (and still prints why, the server log and its summary, and cleans up) before GitHub
    kills its step: a step killed at its timeout-minutes prints nothing more and leaves no summary."""
    yaml = pytest.importorskip("yaml")
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]

    def step_seconds(job: str, script: str) -> tuple[str, int]:
        [step] = [s for s in jobs[job]["steps"] if script in str(s.get("run", ""))]
        return str(step["run"]), int(step["timeout-minutes"]) * 60

    _run, suite_s = step_seconds("docker", "tools/ci/docker_tests.py")
    assert docker_tests.TIMEOUT_S + 60 <= suite_s  # + `docker kill` and the skip report
    run, launcher_s = step_seconds("launcher", "tools/ci/launcher_smoke.py")
    budget = float(run.split("--timeout", 1)[1].split()[0])
    assert budget + 2 * launcher_smoke.STOP_GRACE_S + 60 <= launcher_s  # start-up budget, SIGTERM grace, SIGKILL wait
    _run, smoke_s = step_seconds("docker", "tools/ci/docker_smoke.py")
    # a hung contract run is the longest wait; the other checks take well under 2 min together
    assert docker_smoke.CONTRACT_TIMEOUT_S + docker_smoke.STOP_GRACE_S + 120 <= smoke_s
