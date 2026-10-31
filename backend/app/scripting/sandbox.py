"""PyChart sandbox launcher.

``run_script(code, candles, inputs=None, *, symbol="", tf="", timeout=10.0) -> dict`` (ScriptRunResult) validates
the script with ``compile_script`` (invalid code is never executed) and runs it in a fresh, isolated Python
process (``worker.py``) with a minimal environment, a memory cap (``config.SCRIPT_MEMORY_MB``), a scratch working
directory under ``backend/.tmp/sandbox`` and a hard timeout (the process is killed). The worker's output is read
with hard limits (``MAX_STDOUT_BYTES`` of stdout, the last ``STDERR_TAIL_BYTES`` of stderr), so a script cannot
make this process buffer an unbounded amount of data. Blocking: call it through ``asyncio.to_thread``. It never
raises for user errors: they are reported in ``result["errors"]``.

The worker reports the OS protections it applied (job object; on Linux no_new_privs, empty network namespace,
closed descriptors, rlimits) under the internal key ``OS_ISOLATION_KEY``: this module removes it from the result
(ScriptRunResult is unchanged), keeps the latest one for ``last_os_isolation()`` and logs it once, together with the
server's own protection (``app.hardening``), as a system event (again only if it changes).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from app import config

from .runtime_base import RESULT_MARKER
from .validator import DEFAULT_TITLE, compile_script, make_error

WORKER_PATH = Path(__file__).resolve().with_name("worker.py")
BACKEND_DIR = Path(__file__).resolve().parents[2]
SANDBOX_DIR = BACKEND_DIR / ".tmp" / "sandbox"
MAX_BARS = 20_000
MAX_CONCURRENT = 4
MIN_TIMEOUT_S = 0.5
MAX_TIMEOUT_S = 60.0
MAX_STDOUT_BYTES = 48 * 1024 * 1024  # the worker caps its own result at 40 MB (worker.MAX_RESULT_BYTES)
STDERR_TAIL_BYTES = 64 * 1024
_READ_CHUNK = 64 * 1024
_MARKER = RESULT_MARKER.encode("ascii")
OS_ISOLATION_KEY = "_os_isolation"  # internal key of the worker result (= worker.OS_ISOLATION_KEY), never returned

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_isolation_lock = threading.Lock()
_isolation_last: dict[str, Any] | None = None
_isolation_announced: str | None = None
_ISOLATION_FIELD = re.compile(r"[a-z][a-z0-9_]{0,31}")


class OutputLimitExceeded(Exception):
    """The worker wrote more than MAX_STDOUT_BYTES to stdout (it was killed)."""


def _empty_result(meta: dict | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "errors": [],
        "stdout": "",
        "duration_ms": 0,
        "meta": dict(meta) if isinstance(meta, dict) else {"title": DEFAULT_TITLE, "overlay": True, "kind": "indicator"},
        "time": [],
        "plots": [],
        "markers": [],
        "hlines": [],
        "bgcolors": [],
        "alerts": [],
        "strategy": None,
        "candles": [],
    }


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def clean_candles(candles: Iterable[Any] | None) -> list[dict]:
    """Candle dicts sorted by time, unique times (last wins), finite OHLC, volume defaulting to 0; at most
    ``MAX_BARS`` (the most recent)."""
    by_time: dict[int, dict] = {}
    for row in candles or ():
        if isinstance(row, dict):
            t, o, h, lo, c, v = (row.get(k) for k in ("time", "open", "high", "low", "close", "volume"))
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            t, o, h, lo, c = row[:5]
            v = row[5] if len(row) > 5 else 0.0
        else:
            continue
        ts = _num(t)
        prices = [_num(o), _num(h), _num(lo), _num(c)]
        if ts is None or any(p is None for p in prices):
            continue
        vol = _num(v)
        by_time[int(ts)] = {
            "time": int(ts),
            "open": prices[0],
            "high": prices[1],
            "low": prices[2],
            "close": prices[3],
            "volume": vol if vol is not None and vol >= 0 else 0.0,
        }
    rows = [by_time[t] for t in sorted(by_time)]
    return rows[-MAX_BARS:]


def _worker_env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", os.path.dirname(sys.executable))}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _parse_output(stdout: bytes | bytearray | None) -> dict | None:
    data = stdout or b""
    idx = data.rfind(_MARKER)
    if idx < 0:
        return None
    try:
        parsed = json.loads(data[idx + len(_MARKER):])  # the worker writes ASCII JSON (+ "\n")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _plain(text: str, limit: int) -> str:
    """At most ``limit`` printable characters (no line breaks or control characters reach the log)."""
    return "".join(ch if ch.isprintable() else "?" for ch in text[:limit])


def _clean_isolation(raw: Any) -> dict[str, Any] | None:
    """The worker's protection report reduced to short, plain values. It is informative only (a script that took
    over its worker could forge it), so nothing decides anything from it."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in list(raw.items())[:24]:
        if not isinstance(key, str) or not _ISOLATION_FIELD.fullmatch(key):
            continue
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int) and abs(value) < 10**6:
            out[key] = value
        elif isinstance(value, str):
            out[key] = _plain(value, 60)
        elif isinstance(value, list) and len(value) <= 8 and all(isinstance(v, str) for v in value):
            out[key] = [_plain(v, 16) for v in value]
    return out


def _yes(value: Any) -> str:
    return "sí" if value is True else "no"


def describe_os_isolation(info: dict[str, Any], server: dict[str, Any] | None = None) -> str:
    """One Spanish line listing every OS protection reported, active or not (it never claims an inactive one)."""
    parts: list[str] = []
    if "job_object" in info:
        parts.append(f"objeto de trabajo de Windows (memoria, sin procesos hijos): {_yes(info['job_object'])}")
    if "no_new_privs" in info:
        parts.append(f"sin privilegios nuevos (no_new_privs): {_yes(info['no_new_privs'])}")
    if "net_namespace" in info:
        if info["net_namespace"] is True:  # IP only: a Unix socket with a path lives on the (shared) filesystem
            parts.append("sin red IP (espacio de nombres de red vacío; los sockets Unix con ruta no se aíslan): sí")
        else:
            why = info.get("net_namespace_error") or "no disponible"
            parts.append(f"sin red a nivel del sistema: no, este host no lo permite ({why}); la red solo la "
                         "bloquean las capas de Python del sandbox")
    if "fds_closed" in info:
        parts.append(f"descriptores heredados cerrados: {info['fds_closed']}")
    elif "fds_error" in info:
        parts.append(f"descriptores heredados: no se pudieron revisar ({info['fds_error']})")
    if "rlimits" in info:
        parts.append(f"límites del sistema: {', '.join(info['rlimits']) or 'ninguno'}")
    if "oom_score_adj" in info:
        parts.append(f"prioridad OOM del worker: {_yes(info['oom_score_adj'])}")
    if server and str(server.get("platform", "")).startswith("linux"):
        parts.append("servidor no volcable (su /proc/<pid>/environ y mem no los lee otro proceso del mismo usuario): "
                     f"{_yes(server.get('non_dumpable'))}")
    if info.get("error"):
        parts.append(f"error: {info['error']}")
    platform = info.get("platform") or "?"
    return f"Aislamiento del sistema de PyChart ({platform}): " + (" · ".join(parts) if parts else "sin datos")


def _server_protections() -> dict[str, Any] | None:
    try:
        from app.hardening import server_protections

        return server_protections()
    except Exception:  # noqa: BLE001 - informative only
        return None


def _announce(message: str, data: dict[str, Any]) -> None:
    """Best effort: a system event (console + terminal) when the app's event bus runs, else one stderr line (a bus
    that was never started, e.g. a standby instance or a test runtime, would drop the event without a word)."""
    try:
        from app.runtime import rt

        bus = rt.bus
    except Exception:  # noqa: BLE001
        bus = None
    if bus is not None and getattr(bus, "running", True) is not False:
        try:
            bus.emit_threadsafe("system", "scripts", message, data)
            return
        except Exception:  # noqa: BLE001 - e.g. the loop of an old bus is closed
            pass
    try:
        sys.stderr.write(message + "\n")
    except Exception:  # noqa: BLE001
        pass


def _note_isolation(raw: Any) -> None:
    """Keeps the worker's report for last_os_isolation() and logs it once (again only when it changes)."""
    global _isolation_last, _isolation_announced
    info = _clean_isolation(raw)
    if info is None:
        return
    server = _server_protections()
    message = describe_os_isolation(info, server)
    with _isolation_lock:
        _isolation_last = info
        if message == _isolation_announced:
            return
        _isolation_announced = message
    _announce(message, {"worker": info, "server": server})


def last_os_isolation() -> dict[str, Any] | None:
    """OS protections reported by the latest worker that returned a result (None before the first one)."""
    with _isolation_lock:
        return dict(_isolation_last) if _isolation_last is not None else None


def _last_line(stderr: bytes | None) -> str:
    lines = [ln.strip() for ln in (stderr or b"").decode("utf-8", errors="replace").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:  # already gone
        pass


def _feed(stream: Any, data: bytes) -> None:
    """Writes the payload to the worker's stdin (in its own thread: a worker that stops reading cannot block
    the caller past the timeout)."""
    try:
        stream.write(data)
    except (OSError, ValueError):  # the worker died early (killed, crashed): its exit code tells why
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _read_head(proc: subprocess.Popen, out: bytearray, limit: int, overflow: threading.Event) -> None:
    """Keeps the first ``limit`` bytes of stdout; past that the worker is killed and the rest is discarded."""
    stream = proc.stdout
    try:
        while chunk := stream.read1(_READ_CHUNK):
            if overflow.is_set():
                continue
            if len(out) + len(chunk) > limit:
                overflow.set()
                _kill(proc)
                continue
            out += chunk
    except (OSError, ValueError):
        pass


def _read_tail(stream: Any, out: bytearray, keep: int) -> None:
    """Keeps only the last ``keep`` bytes of stderr (a script can flood it, e.g. np.seterr(all='print'))."""
    try:
        while chunk := stream.read1(_READ_CHUNK):
            out += chunk
            if len(out) > 2 * keep:
                del out[:-keep]
    except (OSError, ValueError):
        pass
    if len(out) > keep:
        del out[:-keep]


def _launch(payload: bytes, timeout: float, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Runs the worker with ``payload`` on stdin -> CompletedProcess(stdout <= MAX_STDOUT_BYTES, stderr tail).
    Raises subprocess.TimeoutExpired after ``timeout`` s and OutputLimitExceeded when stdout is too big (the worker
    is killed in both cases)."""
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    env = _worker_env()
    if extra_env:
        env.update(extra_env)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        # -I: isolated (no PYTHON* variables, no user site, no script dir on sys.path). -B: no __pycache__ writes,
        # because -I also ignores PYTHONDONTWRITEBYTECODE and the worker would write next to the code before its
        # audit hook is armed (it still reads existing bytecode, e.g. the image's precompiled code).
        [sys.executable, "-I", "-B", str(WORKER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(SANDBOX_DIR),
        env=env,
        close_fds=True,  # the default, stated: only the three pipes reach the worker (which closes any other itself)
        creationflags=flags,
    )
    out, err = bytearray(), bytearray()
    overflow = threading.Event()
    threads = [
        threading.Thread(target=_read_head, args=(proc, out, MAX_STDOUT_BYTES, overflow), daemon=True),
        threading.Thread(target=_read_tail, args=(proc.stderr, err, STDERR_TAIL_BYTES), daemon=True),
        threading.Thread(target=_feed, args=(proc.stdin, payload), daemon=True),
    ]
    started: list[threading.Thread] = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:  # timeout, or interrupted while starting / waiting
            _kill(proc)
        for thread in started:
            thread.join(timeout=5.0)  # the pipes reach EOF once the worker is gone
        for thread, stream in zip(threads, (proc.stdout, proc.stderr, proc.stdin)):
            if not thread.is_alive():  # never close a stream under a thread that is still blocked on it
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
    if overflow.is_set():
        raise OutputLimitExceeded(f"más de {MAX_STDOUT_BYTES // (1024 * 1024)} MB")
    return subprocess.CompletedProcess(proc.args, proc.returncode, bytes(out), bytes(err))


def execute_payload(payload: dict[str, Any], timeout: float, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Runs a raw worker payload and returns the worker's result (or a timeout / launch error result).
    ``extra_env`` exists for the sandbox tests only (run_script never passes it). The worker's memory cap is
    ``config.SCRIPT_MEMORY_MB`` unless the payload sets ``memory_mb`` (its environment has no NEXUS_* variables)."""
    try:
        data = json.dumps({"memory_mb": config.SCRIPT_MEMORY_MB, **payload}, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        result = _empty_result()
        result["errors"] = [make_error("runtime", f"Datos de entrada no serializables: {exc}")]
        return result
    if not _slots.acquire(timeout=max(timeout, 1.0)):
        result = _empty_result()
        result["errors"] = [make_error("runtime", "El sandbox está ocupado con otros scripts; inténtalo de nuevo.")]
        return result
    try:
        proc = _launch(data, timeout, extra_env)
    except subprocess.TimeoutExpired:
        result = _empty_result()
        result["errors"] = [
            make_error(
                "timeout",
                f"El script superó el tiempo máximo de {timeout:g} s y se detuvo. "
                "Revisa bucles infinitos o cálculos demasiado costosos.",
            )
        ]
        return result
    except OutputLimitExceeded as exc:
        result = _empty_result()
        result["errors"] = [
            make_error(
                "runtime",
                f"El script generó una salida demasiado grande ({exc}) y se detuvo. "
                "Reduce el número de velas, de plot() o de marcadores.",
            )
        ]
        return result
    except OSError as exc:
        result = _empty_result()
        result["errors"] = [make_error("runtime", f"No se pudo iniciar el sandbox de PyChart: {exc}")]
        return result
    finally:
        _slots.release()
    parsed = _parse_output(proc.stdout)
    if parsed is None:
        result = _empty_result()
        detail = _last_line(proc.stderr)
        message = f"El sandbox terminó sin resultado (código {proc.returncode})."
        if proc.returncode not in (0, None) and not detail:
            message += " Posible exceso de memoria o terminación forzada."
        result["errors"] = [make_error("runtime", f"{message} {detail}".strip())]
        return result
    report = parsed.pop(OS_ISOLATION_KEY, None)  # internal: never part of ScriptRunResult
    try:
        _note_isolation(report)
    except Exception:  # noqa: BLE001 - logging the protections must never break a run
        pass
    return parsed


def run_script(
    code: str,
    candles: list[dict],
    inputs: dict | None = None,
    *,
    symbol: str = "",
    tf: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Validates and runs a PyChart script over ``candles``; returns a ScriptRunResult dict (never raises for
    user errors)."""
    started = time.perf_counter()
    try:
        limit = float(timeout)
    except (TypeError, ValueError):
        limit = 10.0
    limit = min(max(limit if math.isfinite(limit) else 10.0, MIN_TIMEOUT_S), MAX_TIMEOUT_S)
    compiled = compile_script(code)
    rows = clean_candles(candles)
    times = [r["time"] for r in rows]
    if not compiled["ok"]:
        result = _empty_result(compiled.get("meta"))
        result["errors"] = compiled["errors"]
    elif not rows:
        result = _empty_result(compiled.get("meta"))
        result["errors"] = [make_error("runtime", "No hay velas para ejecutar el script (símbolo sin datos).")]
    else:
        payload = {
            "code": code,
            "time": times,
            "open": [r["open"] for r in rows],
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r["volume"] for r in rows],
            "inputs": dict(inputs) if isinstance(inputs, dict) else {},
            "symbol": str(symbol or ""),
            "tf": str(tf or ""),
        }
        result = execute_payload(payload, limit)
        if not result.get("ok"):
            result["meta"] = dict(compiled.get("meta") or result.get("meta") or {})
    result["candles"] = rows
    result["time"] = times
    result["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return result
