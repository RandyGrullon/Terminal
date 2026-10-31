"""Improvement 12 (sandbox-os), Linux only: the OS isolation of the PyChart worker, checked from real processes.

Each probe is a fresh ``python -I`` process started like the launcher starts the worker (minimal environment, cwd
SANDBOX_DIR). It applies ``worker.harden_process`` exactly as ``run_payload`` does and then tries what a script
that got past every Python layer would try. The audit hook is NOT installed in the probes, so what is checked
here is the OS layer alone. Skipped off Linux (they run on GitHub Actions, ubuntu).

- no_new_privs is set (``NoNewPrivs: 1`` in /proc/self/status), the rlimits and oom_score_adj are still applied;
- with the empty network namespace (when the host allows it) no connection reaches a listener of the host,
  not even on 127.0.0.1, and nothing can be sent anywhere;
- a plain process of the same user cannot read ``/proc/<server>/environ`` (nor open its ``mem``) once the server
  called ``harden_server_process`` (and, as a control, it could before). The control uses a plain process because a
  worker in its own user namespace is already kept out of the server's /proc by the namespace itself;
- a hardened worker cannot read them either, whatever the host allows;
- inherited descriptors above stderr are closed;
- the real launcher from a hardened server: the run works, the report stays out of ScriptRunResult and is logged
  once, and a script past its timeout is still killed (also when the worker is inside its namespace).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.scripting.sandbox import SANDBOX_DIR

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="aislamiento del sistema: solo Linux")

BACKEND = Path(__file__).resolve().parents[1]
SECRET = "nexus-valor-secreto-de-prueba-7f3a"
# errors that mean "this host does not allow an unprivileged network namespace" (not a bug of the worker)
PLATFORM_REFUSALS = {"EPERM", "EACCES", "ENOSPC", "EUSERS", "ENOSYS"}


def _program(body: str, *, harden: bool = True, before: str = "") -> str:
    """Python source of one probe: imports the worker, runs ``before``, hardens, runs ``body``, prints ``out``."""
    return "\n".join([
        "import errno, json, os, socket, sys",
        f"sys.path.insert(0, {str(BACKEND)!r})",
        "from app.scripting import worker",
        "out = {}",
        "def is_open(fd):",
        "    try:",
        "        os.fstat(fd)",
        "    except OSError:",
        "        return False",
        "    return True",
        "def threads():",
        "    try:",
        "        with open('/proc/self/status', encoding='ascii') as fh:",
        "            return int(next(ln.split()[1] for ln in fh if ln.startswith('Threads:')))",
        "    except (OSError, StopIteration, ValueError, IndexError):",
        "        return None",
        textwrap.dedent(before),
        "out['threads_before'] = threads()",  # unshare(CLONE_NEWUSER) needs a single-threaded process
        "out['status'] = worker.harden_process(1024)" if harden else "out['status'] = None",
        textwrap.dedent(body),
        "print('@@' + json.dumps(out), flush=True)",
    ])


def _env(**extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", os.path.dirname(sys.executable))}
    env.update(extra)
    return env


def _last_json(stdout: bytes) -> dict:
    lines = [ln for ln in stdout.decode("utf-8", errors="replace").splitlines() if ln.startswith("@@")]
    assert lines, stdout[-2000:]
    return json.loads(lines[-1][2:])


def _probe(body: str, *, harden: bool = True, before: str = "", pass_fds: tuple[int, ...] = ()) -> tuple[dict, str]:
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _program(body, harden=harden, before=before)],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120, env=_env(), cwd=str(SANDBOX_DIR),
        pass_fds=pass_fds,
    )
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, stderr[-3000:]
    return _last_json(proc.stdout), stderr


# ----------------------------------------------------------------------------- no_new_privs + the existing limits


def test_worker_has_no_new_privs_and_keeps_its_limits():
    out, _ = _probe("""
        import resource
        with open('/proc/self/status', encoding='ascii') as fh:
            out['proc_status'] = dict(ln.rstrip('\\n').split(':\\t', 1) for ln in fh if ':\\t' in ln)
        with open('/proc/self/oom_score_adj', encoding='ascii') as fh:
            out['oom'] = fh.read().strip()
        out['nproc'] = list(resource.getrlimit(resource.RLIMIT_NPROC))
        out['fsize'] = list(resource.getrlimit(resource.RLIMIT_FSIZE))
        out['as'] = list(resource.getrlimit(resource.RLIMIT_AS))
    """)
    status = out["status"]
    assert status["platform"].startswith("linux") and status["no_new_privs"] is True, status
    proc_status = out["proc_status"]
    if "NoNewPrivs" in proc_status:  # kernels >= 4.10 show it
        assert proc_status["NoNewPrivs"].strip() == "1", proc_status
    assert set(status["rlimits"]) == {"AS", "FSIZE", "CORE", "NPROC"}, status
    assert out["nproc"] == [0, 0] and out["fsize"] == [0, 0]
    assert out["as"][0] == (1024 + 512) * 1024 * 1024
    assert status["oom_score_adj"] is True and out["oom"] == "1000"


# ----------------------------------------------------------------------------- network


NET_PROBE = """
    try:
        socket.create_connection(('127.0.0.1', PORT), timeout=5).close()
        out['connected'] = True
    except OSError as exc:
        out['connected'] = False
        out['connect_error'] = errno.errorcode.get(exc.errno or 0, str(exc))
    try:
        out['interfaces'] = sorted(name for _, name in socket.if_nameindex())
    except OSError:
        out['interfaces'] = None
    if out['status'] and out['status'].get('net_namespace') is True:  # never in the control: it would send a packet
        try:  # 192.0.2.1 is TEST-NET-1 (RFC 5737): no real host; in the empty namespace there is no route at all
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.sendto(b'x', ('192.0.2.1', 9))
            out['sent'] = True
        except OSError as exc:
            out['sent'] = False
            out['send_error'] = errno.errorcode.get(exc.errno or 0, str(exc))
"""


def test_worker_cannot_reach_the_host_network_when_the_namespace_is_available():
    listener = socket.create_server(("127.0.0.1", 0))  # lives in the host's network namespace (this process)
    try:
        body = NET_PROBE.replace("PORT", str(listener.getsockname()[1]))
        control, _ = _probe(body, harden=False)
        assert control["connected"] is True, control  # an ordinary process reaches the listener
        out, _ = _probe(body)
    finally:
        listener.close()
    status = out["status"]
    if status["net_namespace"] is not True:
        problem = status.get("net_namespace_error")
        # EINVAL from a single-threaded process: a kernel without CONFIG_USER_NS / CONFIG_NET_NS (the platform). From
        # a process with threads it is the worker's own bug (unshare(CLONE_NEWUSER) needs a single thread).
        refused = problem in PLATFORM_REFUSALS or (problem == "EINVAL" and out.get("threads_before") == 1)
        assert refused, f"the worker could not enter the namespace for its own reasons: {out}"
        pytest.skip(f"este host no permite un espacio de nombres de red sin privilegios ({problem})")
    assert out["connected"] is False, out  # normally ENETUNREACH: the loopback of the new namespace is down
    assert out.get("connect_error"), out
    assert out["sent"] is False and out.get("send_error"), out  # nor can anything be sent outside: no route at all
    if out["interfaces"] is not None:
        assert out["interfaces"] == ["lo"], out  # nothing but the (down) loopback of the new namespace


# ----------------------------------------------------------------------------- the server's environment


CHILD_PROBE = """
    ppid = os.getppid()
    try:
        with open(f'/proc/{ppid}/status', encoding='ascii') as fh:
            out['parent_visible'] = any(ln.startswith('Pid:') for ln in fh)
    except OSError:
        out['parent_visible'] = False
    try:
        with open(f'/proc/{ppid}/environ', 'rb') as fh:
            data = fh.read()
        out['environ'] = 'leido'
        out['leaked'] = SECRET.encode() in data
    except OSError as exc:
        out['environ'] = errno.errorcode.get(exc.errno or 0, str(exc))
        out['leaked'] = False
    try:
        with open(f'/proc/{ppid}/mem', 'rb', buffering=0):
            pass
        out['mem'] = 'abierto'
    except OSError as exc:
        out['mem'] = errno.errorcode.get(exc.errno or 0, str(exc))
"""
NOT_READABLE = {"EACCES", "EPERM"}
AS_ROOT_REASON = "root (CAP_SYS_PTRACE) lee /proc de cualquier proceso; la protección es para otro usuario"
not_as_root = pytest.mark.skipif(sys.platform.startswith("linux") and os.geteuid() == 0, reason=AS_ROOT_REASON)


def _server_chain(*, harden_server: bool, harden_child: bool) -> dict:
    """Test -> "server" process (the secret in its environment; harden_server_process() or not) -> child started
    like the launcher starts the worker (minimal environment, no secret), hardened like the worker or a plain
    process, that tries to read the server's /proc/<pid>/environ and to open its /proc/<pid>/mem."""
    child = _program(CHILD_PROBE.replace("SECRET", repr(SECRET)), harden=harden_child)
    server = "\n".join([
        "import json, os, subprocess, sys",
        f"sys.path.insert(0, {str(BACKEND)!r})",
        "from app import hardening",
        "hardening.harden_server_process()" if harden_server else "",
        f"child = subprocess.run([sys.executable, '-I', '-c', {child!r}], capture_output=True, timeout=120,",
        "                       stdin=subprocess.DEVNULL, env={'PATH': os.environ.get('PATH', '')})",
        "lines = [ln for ln in child.stdout.decode(errors='replace').splitlines() if ln.startswith('@@')]",
        "print('@@' + json.dumps({'server': hardening.server_protections(), 'rc': child.returncode,",
        "      'child': json.loads(lines[-1][2:]) if lines else None,",
        "      'stderr': child.stderr.decode(errors='replace')[-2000:]}), flush=True)",
    ])
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sys.executable, "-I", "-c", server], stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=240, env=_env(NEXUS_TEST_SECRET=SECRET), cwd=str(SANDBOX_DIR))
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-3000:]
    out = _last_json(proc.stdout)
    assert out["rc"] == 0 and out["child"] is not None, out["stderr"]
    return out


@not_as_root
def test_non_dumpable_server_hides_its_environment_from_same_user_processes():
    # A plain child (no user namespace of its own, like a worker on a host that refuses one): what is measured here is
    # the server's non-dumpable flag alone. A hardened worker would not do as a control: when its namespace is active
    # the namespace already keeps it out of the server's /proc, so the control would never leak and this would skip.
    control = _server_chain(harden_server=False, harden_child=False)
    assert control["server"]["applied"] is False
    if control["child"]["leaked"] is not True:
        pytest.skip(f"este host ya oculta /proc/<pid>/environ entre procesos del mismo usuario: {control['child']}")
    out = _server_chain(harden_server=True, harden_child=False)
    assert out["server"] == {"platform": sys.platform, "applied": True, "non_dumpable": True}
    child = out["child"]
    assert child["parent_visible"] is True, child  # the server process is there...
    assert child["leaked"] is False and child["environ"] in NOT_READABLE, child  # ...its environment is not
    assert child["mem"] in NOT_READABLE, child  # ...nor its memory


@not_as_root
def test_hardened_worker_cannot_read_the_server_environment():
    # The production pair: hardened server -> hardened worker (in its own user namespace when the host allows one).
    # Whatever the host allows, the server's environment and memory stay out of reach (no control needed: this must
    # hold on every host, and the test above shows the server flag alone is enough).
    out = _server_chain(harden_server=True, harden_child=True)
    assert out["server"]["non_dumpable"] is True, out["server"]
    child = out["child"]
    assert child["status"]["no_new_privs"] is True, child
    assert child["parent_visible"] is True, child
    assert child["leaked"] is False and child["environ"] in NOT_READABLE, child
    assert child["mem"] in NOT_READABLE, child


# ----------------------------------------------------------------------------- inherited descriptors


def test_worker_closes_inherited_descriptors_but_keeps_stdio():
    read_end, write_end = os.pipe()
    try:
        out, stderr = _probe(
            """
            out['after'] = [is_open(fd) for fd in FDS]
            listed = [int(n) for n in os.listdir('/proc/self/fd') if n.isdigit()]
            out['left'] = sorted(fd for fd in listed if fd > 2 and is_open(fd))
            out['stdio'] = [is_open(fd) for fd in (0, 1, 2)]
            sys.stderr.write('stderr sigue abierto\\n')
            sys.stderr.flush()
            """.replace("FDS", repr((read_end, write_end))),
            before=f"out['before'] = [is_open(fd) for fd in {(read_end, write_end)!r}]",
            pass_fds=(read_end, write_end),
        )
    finally:
        os.close(read_end)
        os.close(write_end)
    assert out["before"] == [True, True]  # inherited for real...
    assert out["after"] == [False, False]  # ...and closed by the hardening
    assert out["status"]["fds_closed"] >= 2
    assert read_end not in out["left"] and write_end not in out["left"], out
    assert out["stdio"] == [True, True, True]
    assert "stderr sigue abierto" in stderr


# ----------------------------------------------------------------------------- the real launcher, hardened server


RESULT_KEYS = {
    "ok", "errors", "stdout", "duration_ms", "meta", "time", "plots", "markers", "hlines", "bgcolors", "alerts",
    "strategy", "candles",
}
LAUNCHER_SERVER = """
import json, os, sys
sys.path.insert(0, BACKEND)
from app import hardening
hardening.harden_server_process()
from app.scripting import sandbox
messages = []
sandbox._announce = lambda message, data: messages.append(message)


def live_children():
    try:
        names = os.listdir('/proc')
    except OSError:
        return None
    found = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f'/proc/{name}/stat', encoding='ascii', errors='replace') as fh:
                stat = fh.read()
        except OSError:
            continue
        fields = stat[stat.rfind(')') + 2:].split()  # state, ppid, ...
        if len(fields) > 1 and fields[1] == str(os.getpid()) and fields[0] != 'Z':
            found.append(int(name))
    return found


candles = [{'time': 1700000000 + 3600 * i, 'open': 100.0 + i, 'high': 101.5 + i, 'low': 99.0 + i,
            'close': 100.5 + i, 'volume': 10.0} for i in range(80)]
res = sandbox.run_script("indicator('capas')\\nplot(ta.sma(close, 5))\\n", candles)
info = sandbox.last_os_isolation()
loop = sandbox.run_script("indicator('bucle')\\nwhile True:\\n    pass\\n", candles, timeout=LOOP_TIMEOUT)
print('@@' + json.dumps({'server': hardening.server_protections(), 'keys': sorted(res), 'ok': res['ok'],
                         'errors': res['errors'], 'info': info, 'messages': messages,
                         'loop_errors': loop['errors'], 'children': live_children()}), flush=True)
"""


def test_real_launcher_runs_and_stops_scripts_from_a_hardened_server():
    # The production chain for real: a non-dumpable "server" runs PyChart through sandbox.run_script (real launcher,
    # real `python -I worker.py` with every layer). The other real-worker tests start it from pytest, which is not
    # non-dumpable. The looping script runs long enough to be inside its namespace (when there is one) when killed.
    program = LAUNCHER_SERVER.replace("BACKEND", repr(str(BACKEND))).replace("LOOP_TIMEOUT", "6.0")
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sys.executable, "-I", "-c", program], stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=240, env=_env(), cwd=str(SANDBOX_DIR))
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-3000:]
    out = _last_json(proc.stdout)
    assert out["server"] == {"platform": sys.platform, "applied": True, "non_dumpable": True}
    assert out["ok"] is True, out["errors"]
    assert set(out["keys"]) == RESULT_KEYS  # the internal report never reaches ScriptRunResult
    info = out["info"]
    assert info is not None, out  # the launcher kept the worker's report
    assert info["platform"] == sys.platform and info["no_new_privs"] is True, info
    assert isinstance(info["net_namespace"], bool) and "fds_error" not in info and info["fds_closed"] >= 0, info
    assert set(info["rlimits"]) == {"AS", "FSIZE", "CORE", "NPROC"} and info["oom_score_adj"] is True, info
    assert len(out["messages"]) == 1, out["messages"]  # logged once (the killed run returns no report)
    message = out["messages"][0]
    assert message.startswith(f"Aislamiento del sistema de PyChart ({sys.platform}): "), message
    assert "sin privilegios nuevos (no_new_privs): sí" in message, message
    assert ("servidor no volcable (su /proc/<pid>/environ y mem no los lee otro proceso del mismo usuario): sí"
            in message), message
    assert [e["kind"] for e in out["loop_errors"]] == ["timeout"], out["loop_errors"]
    assert out["children"] == [], out["children"]  # the looping worker was killed and reaped: nothing left behind
