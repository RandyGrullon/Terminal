"""OS-level hardening of the server process (best effort, never raises). Called first thing in the app lifespan.

Linux: ``prctl(PR_SET_DUMPABLE, 0)``. The server becomes non-dumpable, so other processes running as the same user
(above all the PyChart sandbox workers it starts) can no longer read ``/proc/<server>/environ`` or
``/proc/<server>/mem`` (the database URL and the provider keys live in the environment and in memory) nor attach a
debugger to it, and the kernel writes no core dump of it. Limits: root or a process with CAP_SYS_PTRACE is not
affected, and the server's own children get a fresh dumpable flag when they exec (the workers carry no secrets:
the launcher gives them a minimal environment).

Windows / macOS: safe no-op (on Windows the sandbox worker is contained by its job object instead).

Only the standard library here (no ``app.config``): this module runs before anything else is set up.
"""
from __future__ import annotations

import errno
import os
import sys
import threading
from typing import Any

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4

_lock = threading.Lock()
_status: dict[str, Any] = {"platform": sys.platform, "applied": False, "non_dumpable": False}


def _platform() -> str:
    return sys.platform


def _libc() -> Any:
    import ctypes

    return ctypes.CDLL(None, use_errno=True)


def _prctl(libc: Any, option: int, arg2: int = 0) -> int:
    """prctl(option, arg2, 0, 0, 0) -> its result; OSError (errno) when it fails."""
    import ctypes

    fn = libc.prctl
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    res = fn(option, arg2, 0, 0, 0)
    if res < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err) if err else "prctl")
    return int(res)


def error_name(exc: BaseException) -> str:
    """Short, stable name of an error for the status dicts (``EPERM``, ``AttributeError`` ...)."""
    code = getattr(exc, "errno", None)
    if isinstance(code, int) and code in errno.errorcode:
        return errno.errorcode[code]
    return type(exc).__name__


def _apply() -> dict[str, Any]:
    platform = _platform()
    if not platform.startswith("linux"):
        return {"platform": platform, "applied": False, "non_dumpable": False, "reason": "solo Linux"}
    libc = _libc()
    _prctl(libc, PR_SET_DUMPABLE, 0)
    non_dumpable = _prctl(libc, PR_GET_DUMPABLE) == 0
    return {"platform": platform, "applied": True, "non_dumpable": non_dumpable}


def harden_server_process() -> None:
    """Best-effort, never raises. What was applied is kept for ``server_protections()``."""
    try:
        status = _apply()
    except Exception as exc:  # noqa: BLE001 - hardening must never stop the server from starting
        status = {"platform": _safe_platform(), "applied": False, "non_dumpable": False, "error": error_name(exc)}
    try:
        with _lock:
            _status.clear()
            _status.update(status)
    except Exception:  # noqa: BLE001
        pass
    return None


def _safe_platform() -> str:
    try:
        return str(_platform())
    except Exception:  # noqa: BLE001
        return "desconocida"


def server_protections() -> dict[str, Any]:
    """Copy of what ``harden_server_process`` applied (``applied`` False until it runs, or off Linux)."""
    with _lock:
        return dict(_status)
