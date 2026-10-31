"""Regression (review round, pychart-unbounded-worker-output-dos): a PyChart script cannot make the server buffer
an unbounded amount of output.

The worker caps exception messages (every ScriptError), its serialized result and its Python-level stderr; the
launcher reads at most MAX_STDOUT_BYTES of stdout (then kills the worker) and keeps only the tail of stderr; the
service puts short texts in bus events (kept in memory, stored and sent to every terminal). The worker's memory cap
comes from config.SCRIPT_MEMORY_MB (the worker environment has no NEXUS_* variables).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import time

import pytest

from app import config
from app.scripting import sandbox, worker
from app.scripting import service as service_mod
from app.scripting.runtime_base import RESULT_MARKER
from app.scripting.service import ScriptService
from app.scripting.validator import ERROR_MESSAGE_MAX, make_error
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END


@pytest.fixture(scope="module")
def candles():
    return generate_candleset(n=60, seed=6, tf="1h", end_time=FIXED_END).to_list()


# ----------------------------------------------------------------------------- error messages


def test_huge_exception_message_is_truncated(candles):
    res = sandbox.run_script("indicator('grande')\nraise ValueError('A' * 20_000_000)\n", candles)
    err = res["errors"][0]
    assert res["ok"] is False and err["kind"] == "runtime" and err["line"] == 2
    assert err["message"].startswith("Valor inválido (ValueError): AAAA") and "truncado" in err["message"]
    assert len(err["message"]) <= worker.MESSAGE_MAX + 100
    assert len(json.dumps(res)) < 200_000  # candles included


def test_huge_api_error_message_is_truncated(candles):
    res = sandbox.run_script("indicator('color')\nc = color.new('x' * 5_000_000)\n", candles)  # ScriptRuntimeError
    err = res["errors"][0]
    assert res["ok"] is False and "color inválido" in err["message"] and err["line"] == 2
    assert len(err["message"]) <= worker.MESSAGE_MAX + 100


class _BadStr(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no")


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("A" * 1_000_000),
        worker.ScriptRuntimeError("B" * 1_000_000),
        worker.BlockedAttribute("C" * 1_000_000),
        worker.SandboxBlocked("D" * 1_000_000),
        NameError(f"name '{'n' * 1_000_000}' is not defined"),
        KeyError("k" * 1_000_000),
    ],
)
def test_exception_message_is_capped_in_every_branch(exc):
    assert len(worker.exception_message(exc)) <= worker.MESSAGE_MAX + 100


def test_exception_message_survives_a_failing_str():
    assert worker.exception_message(_BadStr()) == "Error de ejecución (_BadStr)."


def test_make_error_caps_the_message():
    err = make_error("runtime", "x" * 1_000_000, 3, 1)
    assert len(err["message"]) <= ERROR_MESSAGE_MAX + 80 and "truncado" in err["message"]
    assert make_error("runtime", "corto")["message"] == "corto"


# ----------------------------------------------------------------------------- worker result size


def _parse(buf: io.BytesIO) -> dict:
    parsed = sandbox._parse_output(buf.getvalue())
    assert parsed is not None
    return parsed


def test_emit_replaces_an_oversized_result(monkeypatch):
    monkeypatch.setattr(worker, "MAX_RESULT_BYTES", 50_000)
    result = worker.empty_result()
    result.update(ok=True, stdout="hola\n", duration_ms=12.5, meta={"title": "Grande", "overlay": True, "kind": "indicator"})
    result["plots"] = [{"id": "p0", "title": "x", "data": [1.2345678] * 20_000}]
    buf = io.BytesIO()
    worker.emit(buf, result)
    assert len(buf.getvalue()) < 5_000
    out = _parse(buf)
    assert out["ok"] is False and out["plots"] == []
    assert "demasiado grande" in out["errors"][0]["message"]
    assert out["meta"]["title"] == "Grande" and out["stdout"] == "hola\n" and out["duration_ms"] == 12.5


def test_emit_reports_an_unserializable_result():
    result = worker.empty_result()
    result.update(ok=True, alerts=[{1, 2}])
    buf = io.BytesIO()
    worker.emit(buf, result)
    out = _parse(buf)
    assert out["ok"] is False and "no se pudo serializar" in out["errors"][0]["message"]


# ----------------------------------------------------------------------------- launcher limits (fake workers)


def _fake_worker(tmp_path, monkeypatch, body: str) -> None:
    script = tmp_path / "fake_worker.py"
    script.write_text("import json, sys, time\n" + body, encoding="utf-8")
    monkeypatch.setattr(sandbox, "WORKER_PATH", script)


def test_launcher_kills_a_worker_that_floods_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "MAX_STDOUT_BYTES", 2 * 1024 * 1024)
    _fake_worker(tmp_path, monkeypatch, (  # never reads stdin, writes 8 MB, then would sleep past the timeout
        "chunk = b'x' * 65536\n"
        "for _ in range(128):\n"
        "    sys.stdout.buffer.write(chunk)\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    ))
    started = time.monotonic()
    res = sandbox.execute_payload({"code": "x" * 2_000_000}, 20.0)
    assert time.monotonic() - started < 15
    assert res["ok"] is False and "demasiado grande" in res["errors"][0]["message"]
    assert "2 MB" in res["errors"][0]["message"]


def test_launcher_keeps_only_the_tail_of_stderr(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, (
        "sys.stdin.buffer.read()\n"
        "for i in range(60_000):\n"
        "    sys.stderr.write('ruido ' * 8 + '\\n')\n"
        "sys.stderr.write('ultima linea\\n')\n"
        f"sys.stdout.write({RESULT_MARKER!r} + json.dumps({{'ok': True, 'errors': [], 'stdout': 'listo'}}) + '\\n')\n"
    ))
    seen = {}
    original = sandbox._launch

    def spy(payload, timeout, extra_env=None):
        proc = original(payload, timeout, extra_env)
        seen["stderr"] = proc.stderr
        return proc

    monkeypatch.setattr(sandbox, "_launch", spy)
    res = sandbox.execute_payload({"code": ""}, 20.0)
    assert res["ok"] is True and res["stdout"] == "listo"
    assert 0 < len(seen["stderr"]) <= sandbox.STDERR_TAIL_BYTES
    assert seen["stderr"].rstrip().endswith(b"ultima linea")


def test_launcher_reports_a_crash_with_the_last_stderr_line(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, "sys.stderr.write('x' * 500_000 + '\\nfallo final\\n')\nsys.exit(3)\n")
    res = sandbox.execute_payload({"code": ""}, 20.0)
    message = res["errors"][0]["message"]
    assert res["ok"] is False and "código 3" in message and message.endswith("fallo final")


def test_launcher_timeout_with_a_worker_that_never_reads_stdin(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, "time.sleep(30)\n")
    started = time.monotonic()
    res = sandbox.execute_payload({"code": "y" * 4_000_000}, 1.0)  # far bigger than a pipe buffer
    assert time.monotonic() - started < 10
    assert res["ok"] is False and res["errors"][0]["kind"] == "timeout"


def test_stderr_flood_from_a_script_keeps_only_the_tail(candles, monkeypatch):
    seen = {}
    original = sandbox._launch

    def spy(payload, timeout, extra_env=None):
        proc = original(payload, timeout, extra_env)
        seen["stderr"] = len(proc.stderr)
        return proc

    monkeypatch.setattr(sandbox, "_launch", spy)
    code = "indicator('ruido')\nnp.seterr(all='print')\na = np.zeros(1)\nfor i in range(60000):\n    a / a\nplot(close)\n"
    res = sandbox.run_script(code, candles, timeout=30)
    assert res["ok"], res["errors"]
    assert 0 < seen["stderr"] <= sandbox.STDERR_TAIL_BYTES


# ----------------------------------------------------------------------------- memory cap from the configuration


def test_memory_cap_comes_from_config(candles, monkeypatch):
    # np.empty commits (Windows job object) / maps (POSIX address space = cap + 1 GB) without touching the pages
    size = 600 * 2**20 if os.name == "nt" else 2 * 2**30
    code = f"indicator('memoria')\nx = np.empty({size // 8})\nprint(x.size)\n"
    monkeypatch.setattr(config, "SCRIPT_MEMORY_MB", 256)
    low = sandbox.run_script(code, candles, timeout=30)
    assert low["ok"] is False and "Memoria insuficiente" in low["errors"][0]["message"]
    monkeypatch.setattr(config, "SCRIPT_MEMORY_MB", 4096)
    high = sandbox.run_script(code, candles, timeout=30)
    assert high["ok"], high["errors"]


def test_worker_memory_setting_is_validated():
    assert worker.memory_limit_mb(None) == worker.DEFAULT_MEMORY_MB
    assert worker.memory_limit_mb(True) == worker.memory_limit_mb("2048") == worker.DEFAULT_MEMORY_MB
    assert worker.memory_limit_mb(float("nan")) == worker.DEFAULT_MEMORY_MB
    assert worker.memory_limit_mb(10) == 128 and worker.memory_limit_mb(10**9) == 8192
    assert worker.memory_limit_mb(512.0) == 512


# ----------------------------------------------------------------------------- service: short texts in events


class _Hub:
    def __init__(self):
        self.cs = generate_candleset(n=300, seed=9, end_time=FIXED_END, symbol="SIM:NEXUS")

    async def get_candles(self, symbol, tf, limit=500):
        return self.cs.tail(int(limit))

    async def resolve(self, text):
        return None


def _huge_sandbox(ok: bool):
    huge = "Z" * 1_000_000

    def run_script(code, candles, inputs=None, *, symbol="", tf="", timeout=10.0):
        if not ok:
            return {"ok": False, "errors": [{"line": 2, "col": 1, "message": huge, "kind": "runtime"}] * 7,
                    "stdout": "", "meta": {"title": huge, "overlay": True, "kind": "indicator"}}
        last = candles[-1]["time"]
        return {"ok": True, "errors": [], "stdout": "", "meta": {"title": "Alertas", "overlay": True, "kind": "indicator"},
                "time": [c["time"] for c in candles], "plots": [], "markers": [], "hlines": [], "bgcolors": [],
                "alerts": [{"time": last, "title": huge, "message": huge, "extra": huge}], "strategy": None}

    return run_script


def test_run_events_and_errors_carry_bounded_texts(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = _Hub()
            svc = ScriptService(rt)
            await svc.start()
            monkeypatch.setattr(service_mod, "_load_sandbox", lambda: _huge_sandbox(ok=False))
            res = await svc.run("indicator('x')\nplot(close)\n", "SIM:NEXUS", "1h", 100)
            assert res["ok"] is False and len(res["errors"]) == 7
            assert all(len(e["message"]) <= ERROR_MESSAGE_MAX + 1 for e in res["errors"])
            monkeypatch.setattr(service_mod, "_load_sandbox", lambda: _huge_sandbox(ok=True))
            await svc.run("indicator('x')\nplot(close)\n", "SIM:NEXUS", "1h", 100)
            await svc.compile("indicator('x')\ny = close._" + "a" * 60_000 + "\n")
            events = rt.bus.recent(500, source="scripts")
            assert {"warn", "signal"} <= {e["level"] for e in events}
            for event in events:
                assert len(json.dumps(event, default=str)) < 6_000, event["message"][:80]
            warn = [e for e in events if e["level"] == "warn" and "✖" in e["message"]][-1]
            assert len(warn["data"]["errors"]) == service_mod.EVENT_ERRORS_MAX
            signal = [e for e in events if e["level"] == "signal"][-1]
            assert set(signal["data"]["alert"]) == {"time", "title", "message"}

    asyncio.run(scenario())
