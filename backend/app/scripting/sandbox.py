"""PyChart sandbox launcher.

``run_script(code, candles, inputs=None, *, symbol="", tf="", timeout=10.0) -> dict`` (ScriptRunResult) validates
the script with ``compile_script`` (invalid code is never executed) and runs it in a fresh, isolated Python
process (``worker.py``) with a minimal environment, a scratch working directory under ``backend/.tmp/sandbox``
and a hard timeout (the process is killed). Blocking: call it through ``asyncio.to_thread``. It never raises for
user errors: they are reported in ``result["errors"]``.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .runtime_base import RESULT_MARKER
from .validator import DEFAULT_TITLE, compile_script, make_error

WORKER_PATH = Path(__file__).resolve().with_name("worker.py")
BACKEND_DIR = Path(__file__).resolve().parents[2]
SANDBOX_DIR = BACKEND_DIR / ".tmp" / "sandbox"
MAX_BARS = 20_000
MAX_CONCURRENT = 4
MIN_TIMEOUT_S = 0.5
MAX_TIMEOUT_S = 60.0

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


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


def _parse_output(stdout: bytes | None) -> dict | None:
    text = (stdout or b"").decode("utf-8", errors="replace")
    idx = text.rfind(RESULT_MARKER)
    if idx < 0:
        return None
    try:
        data = json.loads(text[idx + len(RESULT_MARKER):].strip())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _last_line(stderr: bytes | None) -> str:
    lines = [ln.strip() for ln in (stderr or b"").decode("utf-8", errors="replace").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def _launch(payload: bytes, timeout: float, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    env = _worker_env()
    if extra_env:
        env.update(extra_env)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(
        [sys.executable, "-I", str(WORKER_PATH)],
        input=payload,
        capture_output=True,
        timeout=timeout,
        cwd=str(SANDBOX_DIR),
        env=env,
        creationflags=flags,
    )


def execute_payload(payload: dict[str, Any], timeout: float, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Runs a raw worker payload and returns the worker's result (or a timeout / launch error result).
    ``extra_env`` exists for the sandbox tests only (run_script never passes it)."""
    try:
        data = json.dumps(payload, allow_nan=False).encode("utf-8")
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
