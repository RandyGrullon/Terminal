"""Improvement 12 (sandbox-os), every platform: the OS hardening layer never raises and reports honestly.

- ``app.hardening.harden_server_process`` (server non-dumpable on Linux) is a safe no-op elsewhere and survives any
  failure of the platform calls;
- ``worker.harden_process`` / ``worker._isolate_linux`` (no_new_privs, empty network namespace, inherited
  descriptors) record each protection, keep going when one is refused, and never raise;
- the launcher removes the internal report from the result (ScriptRunResult unchanged), keeps the latest one and
  logs it once, never claiming a protection that is not active.

All OS calls are faked here (nothing is applied to the pytest process); the real effects are checked from separate
processes in ``test_imp_sandbox_os_linux.py`` (Linux only).
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import hardening
from app.runtime import rt
from app.scripting import sandbox, worker
from app.scripting.runtime_base import RESULT_MARKER
from app.sim.synthetic import generate_candleset

BACKEND = Path(__file__).resolve().parents[1]
RESULT_KEYS = {
    "ok", "errors", "stdout", "duration_ms", "meta", "time", "plots", "markers", "hlines", "bgcolors", "alerts",
    "strategy", "candles",
}


@pytest.fixture
def server_status():
    """Restores the module status of app.hardening after a test that changes it."""
    saved = hardening.server_protections()
    yield
    with hardening._lock:
        hardening._status.clear()
        hardening._status.update(saved)


@pytest.fixture
def isolation_state(monkeypatch):
    """Fresh launcher bookkeeping (latest report / last message logged) for one test."""
    monkeypatch.setattr(sandbox, "_isolation_last", None)
    monkeypatch.setattr(sandbox, "_isolation_announced", None)


# ----------------------------------------------------------------------------- server process (app.hardening)


def test_harden_server_process_in_a_real_process_never_raises():
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from app import hardening\n"
        "first = hardening.harden_server_process()\n"
        "hardening.harden_server_process()\n"  # twice: idempotent
        "out = {'returned': first, 'status': hardening.server_protections()}\n"
        "if sys.platform.startswith('linux'):\n"
        "    out['dumpable'] = hardening._prctl(hardening._libc(), hardening.PR_GET_DUMPABLE)\n"
        "print('@@' + json.dumps(out))\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=60,
                          stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "")})
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-2000:]
    out = json.loads([ln for ln in proc.stdout.decode().splitlines() if ln.startswith("@@")][-1][2:])
    assert out["returned"] is None
    status = out["status"]
    if sys.platform.startswith("linux"):
        assert status == {"platform": sys.platform, "applied": True, "non_dumpable": True}
        assert out["dumpable"] == 0
    else:
        assert status["applied"] is False and status["non_dumpable"] is False and status["reason"] == "solo Linux"


def test_harden_server_process_is_a_no_op_off_linux(monkeypatch, server_status):
    def no_libc():  # pragma: no cover - must not be reached
        raise AssertionError("prctl called off Linux")

    monkeypatch.setattr(hardening, "_libc", no_libc)
    for platform in ("win32", "darwin", "cygwin"):
        monkeypatch.setattr(hardening, "_platform", lambda p=platform: p)
        assert hardening.harden_server_process() is None
        assert hardening.server_protections() == {"platform": platform, "applied": False, "non_dumpable": False,
                                                  "reason": "solo Linux"}


class _FakeLibc:
    """Stands in for libc: records prctl calls, never reaches the kernel."""

    def __init__(self, dumpable_after: int = 0) -> None:
        self.calls: list[tuple[int, int]] = []
        self.dumpable_after = dumpable_after


def _fake_prctl(libc: _FakeLibc, option: int, arg2: int = 0) -> int:
    libc.calls.append((option, arg2))
    return libc.dumpable_after if option == hardening.PR_GET_DUMPABLE else 0


def test_harden_server_process_sets_and_checks_the_flag_on_linux(monkeypatch, server_status):
    libc = _FakeLibc()
    monkeypatch.setattr(hardening, "_platform", lambda: "linux")
    monkeypatch.setattr(hardening, "_libc", lambda: libc)
    monkeypatch.setattr(hardening, "_prctl", _fake_prctl)
    hardening.harden_server_process()
    assert libc.calls == [(hardening.PR_SET_DUMPABLE, 0), (hardening.PR_GET_DUMPABLE, 0)]
    assert hardening.server_protections() == {"platform": "linux", "applied": True, "non_dumpable": True}
    libc.dumpable_after = 1  # the call "worked" but the flag did not change: reported as not protected
    hardening.harden_server_process()
    assert hardening.server_protections()["non_dumpable"] is False


@pytest.mark.parametrize(
    "failure, name",
    [
        (OSError(errno.EPERM, "denegado"), "EPERM"),
        (OSError(errno.EINVAL, "no soportado"), "EINVAL"),
        (AttributeError("libc sin prctl"), "AttributeError"),
        (TypeError("CDLL"), "TypeError"),
        (RuntimeError("inesperado"), "RuntimeError"),
    ],
)
def test_harden_server_process_survives_any_failure(monkeypatch, server_status, failure, name):
    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(hardening, "_platform", lambda: "linux")
    monkeypatch.setattr(hardening, "_libc", lambda: _FakeLibc())
    monkeypatch.setattr(hardening, "_prctl", broken)
    assert hardening.harden_server_process() is None
    assert hardening.server_protections() == {"platform": "linux", "applied": False, "non_dumpable": False,
                                              "error": name}
    monkeypatch.setattr(hardening, "_libc", broken)  # failing before prctl (no libc at all) too
    assert hardening.harden_server_process() is None
    assert hardening.server_protections()["error"] == name


def test_harden_server_process_survives_a_broken_platform_probe(monkeypatch, server_status):
    def broken():
        raise RuntimeError("sin plataforma")

    monkeypatch.setattr(hardening, "_platform", broken)
    assert hardening.harden_server_process() is None
    assert hardening.server_protections() == {"platform": "desconocida", "applied": False, "non_dumpable": False,
                                              "error": "RuntimeError"}


def test_hardening_does_not_import_the_app_config():
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "import app.hardening\n"
        "print('app.config' in sys.modules)\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=60,
                          stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "")})
    assert proc.returncode == 0 and proc.stdout.decode().strip() == "False", proc.stderr.decode(errors="replace")


# ----------------------------------------------------------------------------- worker: Linux isolation (faked)


class FakeOps:
    """Fakes for every effectful step of worker._isolate_linux (nothing reaches the pytest process)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.prctl_error: BaseException | None = None
        self.unshare_error: BaseException | None = None
        self.fds_error: BaseException | None = None
        self.no_new_privs = 1
        self.ns_ids = ["net:[4026531840]", "net:[4026532999]"]  # before / after unshare
        self.fds = 2

    def prctl(self, option: int, arg2: int = 0) -> int:
        self.calls.append(("prctl", option, arg2))
        if self.prctl_error is not None:
            raise self.prctl_error
        return self.no_new_privs if option == worker._PR_GET_NO_NEW_PRIVS else 0

    def unshare(self, flags: int) -> None:
        self.calls.append(("unshare", flags))
        if self.unshare_error is not None:
            raise self.unshare_error

    def ns_id(self) -> str | None:
        self.calls.append(("ns_id",))
        return self.ns_ids.pop(0) if self.ns_ids else None

    def close_fds(self) -> int:
        self.calls.append(("close_fds",))
        if self.fds_error is not None:
            raise self.fds_error
        return self.fds


@pytest.fixture
def ops(monkeypatch):
    fake = FakeOps()
    monkeypatch.setattr(worker, "_prctl", fake.prctl)
    monkeypatch.setattr(worker, "_unshare", fake.unshare)
    monkeypatch.setattr(worker, "_net_namespace_id", fake.ns_id)
    monkeypatch.setattr(worker, "_close_inherited_fds", fake.close_fds)
    return fake


def test_isolate_linux_reports_every_active_protection(ops):
    status = worker._isolate_linux()
    assert status == {"no_new_privs": True, "net_namespace": True, "fds_closed": 2}
    assert ("prctl", worker._PR_SET_NO_NEW_PRIVS, 1) in ops.calls
    assert ("prctl", worker._PR_GET_NO_NEW_PRIVS, 0) in ops.calls
    assert ("unshare", 0x10000000 | 0x40000000) in ops.calls  # CLONE_NEWUSER | CLONE_NEWNET, one call
    names = [c[0] for c in ops.calls]
    # the namespace first (an LSM may refuse the user-namespace profile transition to a no_new_privs task)
    assert names.index("unshare") < names.index("prctl") < names.index("close_fds")


@pytest.mark.parametrize("code", [errno.EPERM, errno.EINVAL, errno.ENOSPC, errno.EUSERS, errno.ENOSYS])
def test_refused_namespace_is_recorded_and_the_other_steps_still_run(ops, code):
    ops.unshare_error = OSError(code, os.strerror(code))
    status = worker._isolate_linux()
    assert status == {"no_new_privs": True, "net_namespace": False,
                      "net_namespace_error": errno.errorcode[code], "fds_closed": 2}


def test_namespace_that_did_not_change_is_not_reported_as_active(ops):
    ops.ns_ids = ["net:[4026531840]", "net:[4026531840]"]
    status = worker._isolate_linux()
    assert status["net_namespace"] is False and status["net_namespace_error"] == "sin cambio"


def test_isolate_linux_never_raises(ops):
    ops.prctl_error = AttributeError("libc sin prctl")
    ops.unshare_error = RuntimeError("inesperado")  # not even an OSError
    ops.fds_error = OSError(errno.ENOENT, "sin /proc")
    status = worker._isolate_linux()
    assert status == {"no_new_privs": False, "no_new_privs_error": "AttributeError", "net_namespace": False,
                      "net_namespace_error": "RuntimeError", "fds_error": "ENOENT"}


def test_no_new_privs_not_confirmed_is_reported_as_inactive(ops):
    ops.no_new_privs = 0  # PR_SET "succeeded" but PR_GET says it is off
    assert worker._isolate_linux()["no_new_privs"] is False


def test_harden_process_linux_isolates_first_then_applies_the_limits(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(worker, "_os_kind", lambda: "linux")
    monkeypatch.setattr(worker, "_isolate_linux", lambda: order.append("isolate") or {"no_new_privs": True})
    monkeypatch.setattr(worker, "_limit_posix", lambda mb: order.append(f"limits {mb}") or {"rlimits": ["AS"]})
    status = worker.harden_process(640)
    assert order == ["isolate", "limits 640"]
    assert status == {"platform": sys.platform, "no_new_privs": True, "rlimits": ["AS"]}


@pytest.mark.parametrize("kind", ["nt", "linux", "posix"])
def test_harden_process_never_raises(monkeypatch, capsys, kind):
    def broken(*args):
        raise RuntimeError("fallo simulado")

    called: list[str] = []
    monkeypatch.setattr(worker, "_os_kind", lambda: kind)
    monkeypatch.setattr(worker, "_limit_windows", broken)
    monkeypatch.setattr(worker, "_isolate_linux", lambda: called.append("isolate") or broken())
    monkeypatch.setattr(worker, "_limit_posix", lambda mb: called.append("limits") or broken())
    status = worker.harden_process(512)
    assert status["platform"] == sys.platform and status["error"] == "RuntimeError"
    if kind == "nt":
        assert status["job_object"] is False and called == []
    else:  # a failed isolation never skips the limits
        assert called == (["isolate", "limits"] if kind == "linux" else ["limits"])
    assert "pychart worker:" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimits only (resource module)")
def test_limit_posix_reports_the_limits_it_applied(monkeypatch):
    import resource

    monkeypatch.setattr(worker, "_raise_oom_priority", lambda: True)
    monkeypatch.setattr(resource, "getrlimit", lambda res: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda res, limits: None)
    status = worker._limit_posix(320)
    expected = ["AS", "FSIZE", "CORE"] + (["NPROC"] if hasattr(resource, "RLIMIT_NPROC") else [])
    assert status == {"oom_score_adj": True, "rlimits": expected}

    def refuse(res, limits):
        raise ValueError("no")

    monkeypatch.setattr(resource, "setrlimit", refuse)
    assert worker._limit_posix(320)["rlimits"] == []


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_close_inherited_fds_closes_only_the_listed_descriptors_above_stderr():
    read_end, write_end = os.pipe()
    other_read, other_write = os.pipe()  # not in the start-up list: must stay open
    try:
        assert worker._close_inherited_fds([0, 1, 2, read_end, write_end]) == 2
        assert not _is_open(read_end) and not _is_open(write_end)
        assert _is_open(0) and _is_open(1) and _is_open(2) and _is_open(other_read) and _is_open(other_write)
        assert worker._close_inherited_fds([read_end]) == 0  # already closed: not counted, no error
        assert worker._close_inherited_fds([]) == 0
    finally:
        for fd in (read_end, write_end, other_read, other_write):
            try:
                os.close(fd)
            except OSError:
                pass


def test_close_inherited_fds_without_a_start_up_list_raises(monkeypatch):
    monkeypatch.setattr(worker, "_INHERITED_FDS", None)
    with pytest.raises(OSError) as info:
        worker._close_inherited_fds()
    assert info.value.errno == errno.ENOENT


def test_start_up_list_of_descriptors():
    fds = worker._list_open_fds()
    if sys.platform.startswith("linux"):
        assert fds is not None and {1, 2} <= set(fds) and fds == sorted(fds)  # pytest's own stdout / stderr
    else:
        assert fds is None and worker._INHERITED_FDS is None


def test_raise_oom_priority_reports_whether_it_wrote(monkeypatch):
    def missing(*args, **kwargs):
        raise OSError("sin /proc/self/oom_score_adj")

    monkeypatch.setattr("builtins.open", missing)
    assert worker._raise_oom_priority() is False


# ----------------------------------------------------------------------------- launcher: internal report


def test_internal_key_is_the_same_on_both_sides():
    assert worker.OS_ISOLATION_KEY == sandbox.OS_ISOLATION_KEY == "_os_isolation"
    assert sandbox.OS_ISOLATION_KEY not in RESULT_KEYS


def test_clean_isolation_keeps_only_short_plain_values():
    raw = {
        "platform": "linux" * 40, "no_new_privs": True, "net_namespace": False, "net_namespace_error": "EPERM",
        "fds_closed": 3, "rlimits": ["AS", "FSIZE", "X" * 50], "huge": 10**12, "nested": {"a": 1},
        "Mayus": True, "con espacio": True, 7: True, "float": 1.5, "lista_mala": [1, 2],
    }
    clean = sandbox._clean_isolation(raw)
    assert clean == {"platform": ("linux" * 40)[:60], "no_new_privs": True, "net_namespace": False,
                     "net_namespace_error": "EPERM", "fds_closed": 3, "rlimits": ["AS", "FSIZE", "X" * 16]}
    for bad in (None, "texto", [1, 2], 42):
        assert sandbox._clean_isolation(bad) is None
    forged = sandbox._clean_isolation({"platform": "linux\n12:00:00 SYSTEM [system] falso\x1b[0m",
                                       "rlimits": ["AS\r\nX"]})
    assert forged == {"platform": "linux?12:00:00 SYSTEM [system] falso?[0m", "rlimits": ["AS??X"]}
    assert "\n" not in sandbox.describe_os_isolation(forged)
    assert len(sandbox._clean_isolation({f"k{i}": True for i in range(100)})) == 24


LINUX_FULL = {"platform": "linux", "no_new_privs": True, "net_namespace": True, "fds_closed": 0,
              "oom_score_adj": True, "rlimits": ["AS", "FSIZE", "CORE", "NPROC"]}
LINUX_NO_NS = dict(LINUX_FULL, net_namespace=False, net_namespace_error="EPERM")


def test_description_lists_active_protections():
    text = sandbox.describe_os_isolation(LINUX_FULL, {"platform": "linux", "applied": True, "non_dumpable": True})
    assert text.startswith("Aislamiento del sistema de PyChart (linux): ")
    for part in ("sin privilegios nuevos (no_new_privs): sí", "sin red IP (espacio de nombres de red vacío; ",
                 "descriptores heredados cerrados: 0", "límites del sistema: AS, FSIZE, CORE, NPROC",
                 "prioridad OOM del worker: sí", "servidor no volcable", "environ y mem"):
        assert part in text, text
    windows = sandbox.describe_os_isolation({"platform": "win32", "job_object": True},
                                            {"platform": "win32", "applied": False, "non_dumpable": False})
    assert windows == ("Aislamiento del sistema de PyChart (win32): "
                       "objeto de trabajo de Windows (memoria, sin procesos hijos): sí")


def test_description_states_the_scope_of_the_network_namespace():
    # The namespace removes IP networking only: a Unix socket with a path is a file and stays reachable, so the line
    # must not claim "sin red" without that limit (never overstate a protection).
    text = sandbox.describe_os_isolation(LINUX_FULL)
    assert "sin red IP (espacio de nombres de red vacío; los sockets Unix con ruta no se aíslan): sí" in text
    assert "sin red (" not in text


def test_description_never_claims_an_inactive_protection():
    text = sandbox.describe_os_isolation(LINUX_NO_NS, {"platform": "linux", "applied": False, "non_dumpable": False,
                                                       "error": "EPERM"})
    assert "espacio de nombres de red vacío" not in text and "red IP" not in text
    assert "sin red a nivel del sistema: no, este host no lo permite (EPERM)" in text
    assert "la red solo la bloquean las capas de Python del sandbox" in text
    assert text.endswith("servidor no volcable (su /proc/<pid>/environ y mem no los lee otro proceso del mismo "
                         "usuario): no")
    off = sandbox.describe_os_isolation({"platform": "linux", "no_new_privs": False, "fds_error": "ENOENT",
                                         "error": "MemoryError"})
    assert "no_new_privs): no" in off and "no se pudieron revisar (ENOENT)" in off and "error: MemoryError" in off
    assert sandbox.describe_os_isolation({}) == "Aislamiento del sistema de PyChart (?): sin datos"
    assert "): no" in sandbox.describe_os_isolation({"platform": "win32", "job_object": False})
    assert "no_new_privs): no" in sandbox.describe_os_isolation({"no_new_privs": "sí"})  # only a real True counts


def test_report_is_logged_once_until_it_changes(monkeypatch, isolation_state):
    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(sandbox, "_announce", lambda message, data: logged.append((message, data)))
    assert sandbox.last_os_isolation() is None
    sandbox._note_isolation(LINUX_FULL)
    sandbox._note_isolation(dict(LINUX_FULL))
    assert len(logged) == 1 and logged[0][1]["worker"] == LINUX_FULL
    assert "server" in logged[0][1]
    sandbox._note_isolation(LINUX_NO_NS)  # the namespace stopped being available: said again
    assert len(logged) == 2 and "no, este host no lo permite (EPERM)" in logged[1][0]
    latest = sandbox.last_os_isolation()
    assert latest == LINUX_NO_NS
    latest["net_namespace"] = True  # a copy
    assert sandbox.last_os_isolation()["net_namespace"] is False
    sandbox._note_isolation("no es un informe")  # ignored
    sandbox._note_isolation(None)
    assert len(logged) == 2 and sandbox.last_os_isolation() == LINUX_NO_NS


class _Bus:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[tuple] = []

    def emit_threadsafe(self, level, source, message, data=None):
        if self.fail:
            raise RuntimeError("Event loop is closed")
        self.events.append((level, source, message, data))


def test_announce_goes_to_the_event_bus_or_stderr(monkeypatch, capsys):
    bus = _Bus()
    monkeypatch.setattr(rt, "bus", bus)
    sandbox._announce("Aislamiento del sistema de PyChart (linux): prueba", {"worker": {}})
    assert bus.events == [("system", "scripts", "Aislamiento del sistema de PyChart (linux): prueba", {"worker": {}})]
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(rt, "bus", _Bus(fail=True))  # the bus of a stopped app: falls back to one stderr line
    sandbox._announce("mensaje de respaldo", {})
    assert capsys.readouterr().err == "mensaje de respaldo\n"
    monkeypatch.setattr(rt, "bus", None)
    sandbox._announce("sin bus", {})
    assert capsys.readouterr().err == "sin bus\n"


def _fake_worker(tmp_path, monkeypatch, report) -> None:
    result = {"ok": True, "errors": [], "stdout": "listo", sandbox.OS_ISOLATION_KEY: report}
    script = tmp_path / "fake_worker.py"
    script.write_text(
        "import sys\nsys.stdin.buffer.read()\n"
        f"sys.stdout.write({RESULT_MARKER!r} + {json.dumps(result)!r} + '\\n')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sandbox, "WORKER_PATH", script)


def test_launcher_strips_the_report_from_the_result(tmp_path, monkeypatch, isolation_state):
    logged: list[str] = []
    monkeypatch.setattr(sandbox, "_announce", lambda message, data: logged.append(message))
    _fake_worker(tmp_path, monkeypatch, LINUX_NO_NS)
    res = sandbox.execute_payload({"code": ""}, 20.0)
    assert res == {"ok": True, "errors": [], "stdout": "listo"}
    assert sandbox.last_os_isolation() == LINUX_NO_NS and len(logged) == 1


@pytest.mark.parametrize("report", ["forjado", 123, ["a"], None])
def test_launcher_ignores_a_malformed_report(tmp_path, monkeypatch, isolation_state, report):
    monkeypatch.setattr(sandbox, "_announce", lambda message, data: None)
    _fake_worker(tmp_path, monkeypatch, report)
    res = sandbox.execute_payload({"code": ""}, 20.0)
    assert res == {"ok": True, "errors": [], "stdout": "listo"}
    assert sandbox.last_os_isolation() is None


def test_launcher_survives_a_failing_logger(tmp_path, monkeypatch, isolation_state):
    def broken(*args):
        raise RuntimeError("logger roto")

    monkeypatch.setattr(sandbox, "_announce", broken)
    _fake_worker(tmp_path, monkeypatch, LINUX_FULL)
    assert sandbox.execute_payload({"code": ""}, 20.0)["ok"] is True


# ----------------------------------------------------------------------------- real worker, this platform


def test_real_run_reports_the_os_layer_without_changing_the_result(monkeypatch, isolation_state):
    monkeypatch.setattr(sandbox, "_announce", lambda message, data: None)
    candles = generate_candleset(n=80, seed=12, tf="1h", end_time=1_760_000_400).to_list()
    res = sandbox.run_script("indicator('capas')\nplot(ta.sma(close, 5))\n", candles)
    assert res["ok"], res["errors"]
    assert set(res) == RESULT_KEYS
    info = sandbox.last_os_isolation()
    assert info is not None and info["platform"] == sys.platform
    if os.name == "nt":
        assert info["job_object"] is True
    elif sys.platform.startswith("linux"):
        assert info["no_new_privs"] is True and isinstance(info["net_namespace"], bool)
        assert isinstance(info["fds_closed"], int) and info["fds_closed"] >= 0  # normally 0: close_fds in the launcher
        assert {"AS", "FSIZE", "CORE"} <= set(info["rlimits"])
    else:
        assert {"AS", "FSIZE", "CORE"} <= set(info["rlimits"])
