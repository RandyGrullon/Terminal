"""PyChart sandbox worker: runs ONE user script in this (isolated, short-lived) process.

Started by ``app.scripting.sandbox.run_script`` as ``python -I worker.py`` with a minimal environment. Protocol:
a JSON object on stdin ``{code, time, open, high, low, close, volume, inputs, symbol, tf, memory_mb}``; one JSON
``ScriptRunResult`` (at most ``MAX_RESULT_BYTES``) on stdout after ``RESULT_MARKER``.

Defence in depth (the AST validation of ``validator.py`` is layer 1):
2. restricted ``__builtins__`` (safe subset, custom ``print``/``__import__``, no ``open``/``eval``/``type``...);
3. imports limited to math / statistics / numpy, handed out as read-only module proxies that refuse private
   names, file/system APIs and modules outside the whitelist (``statistics.sys``, ``numpy.random.mtrand`` ...),
   also for ``from <module> import <name>``;
4. a ``sys.addaudithook`` guard armed right before the script runs: every audited operation is denied except a
   few harmless ones (no file ``open``, ``os.*``, ``subprocess``, ``socket``, ``ctypes``, ``shutil``, ``winreg``,
   non-whitelisted imports, ``compile`` or a second ``exec``). Its state lives only in a closure (nothing in the
   module globals can disarm it);
5. OS limits (memory cap ``memory_mb``): Windows job object (memory, no child processes) / POSIX rlimits (address
   space, file size, no child processes) + OOM priority; on Linux also OS isolation (best effort, each step
   recorded): a new user + network namespace (only a loopback interface, down: no IP route anywhere) when the
   platform allows it, ``PR_SET_NO_NEW_PRIVS``, and every inherited descriptor above stderr closed. The server is
   non-dumpable (``app.hardening``), so this worker cannot read ``/proc/<server>/environ`` or ``/proc/<server>/mem``.
What was applied travels back in the result under ``OS_ISOLATION_KEY`` (internal: the launcher removes it and logs
it once; it is not part of ScriptRunResult). The parent kills the process on timeout and reads its output with hard
size limits.
"""
from __future__ import annotations

import os
import sys

for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_var] = "1"  # before numpy is imported: no BLAS thread pools in the sandbox


def _list_open_fds() -> list[int] | None:
    """Descriptors open right now (Linux /proc/self/fd), None when they cannot be listed (or off Linux)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        return None
    fds: list[int] = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            os.fstat(int(name))
        except OSError:  # the descriptor of the listing itself, already closed again
            continue
        fds.append(int(name))
    return sorted(fds)


# What this worker inherited from its launcher, listed before numpy & co. open anything of their own: only these
# are closed by the hardening (see _close_inherited_fds), so no descriptor a library still holds is touched.
_INHERITED_FDS = _list_open_fds()

import builtins
import errno
import json
import math
import re
import statistics
import time
import types
import warnings
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import numpy as np  # noqa: E402
import numpy.fft  # noqa: E402,F401
import numpy.linalg  # noqa: E402,F401
import numpy.ma  # noqa: E402,F401  (np.percentile & co import it lazily)
import numpy.polynomial  # noqa: E402,F401
import numpy.random  # noqa: E402,F401


def _warm_numpy() -> None:
    """numpy imports some internals lazily on first use (numpy._core._methods for ndarray.mean/sum/std,
    arrayprint for repr, nanfunctions, ...). From user code those imports would go through the restricted
    ``__import__`` and fail, so exercise the common paths once here, before any restriction is installed."""
    a = np.arange(1.0, 41.0)
    b = a.reshape(4, 10)
    calls = (
        a.sum, a.mean, a.std, a.var, a.min, a.max, a.argmin, a.argmax, a.prod, a.cumsum, a.cumprod, a.any, a.all,
        a.tolist, a.copy, a.argsort, a.nonzero, lambda: a.round(2), lambda: a.clip(2, 5), lambda: b.sum(axis=0),
        lambda: b.mean(axis=1), lambda: b.std(axis=0), lambda: b.T @ b, lambda: np.ptp(a), lambda: np.nanmean(a),
        lambda: np.nanstd(a), lambda: np.nanmax(a), lambda: np.nanmin(a), lambda: np.nansum(a),
        lambda: np.nanmedian(a), lambda: np.nanpercentile(a, 50), lambda: np.nanquantile(a, 0.5),
        lambda: np.median(a), lambda: np.percentile(a, [10, 90]), lambda: np.quantile(a, 0.5),
        lambda: np.average(a, weights=a), lambda: np.cov(a, a), lambda: np.corrcoef(a, a),
        lambda: np.histogram(a, bins=5), lambda: np.digitize(a, [5.0, 10.0]), lambda: np.searchsorted(a, 3.0),
        lambda: np.unique(a), lambda: np.isin(a, [1.0, 2.0]), lambda: np.intersect1d(a, a),
        lambda: np.setdiff1d(a, a[:3]), lambda: np.union1d(a, a), lambda: np.convolve(a, np.ones(3) / 3, "valid"),
        lambda: np.correlate(a, a[:3]), lambda: np.diff(a), lambda: np.gradient(a), lambda: np.interp(2.5, a, a),
        lambda: np.polyfit(a, a, 1), lambda: np.polyval([1.0, 0.0], a),
        lambda: np.linalg.lstsq(np.c_[a, np.ones_like(a)], a, rcond=None), lambda: np.where(a > 3, a, 0.0),
        lambda: np.select([a > 2], [a]), lambda: np.maximum.accumulate(a), lambda: np.minimum.accumulate(a),
        lambda: np.roll(a, 1), lambda: np.concatenate([a, a]), lambda: np.stack([a, a]), lambda: np.vstack([a, a]),
        lambda: np.hstack([a, a]), lambda: np.column_stack([a, a]), lambda: np.nan_to_num(a),
        lambda: np.around(a, 2), lambda: np.argwhere(a > 2), lambda: np.count_nonzero(a),
        lambda: np.partition(a, 3), lambda: np.argpartition(a, 3), lambda: np.lexsort((a, a)), lambda: np.sort(a),
        lambda: a.astype(int), lambda: a.astype(bool), lambda: str(a), lambda: repr(b), lambda: format(a[0], ".2f"),
        lambda: np.array2string(a), lambda: str(np.float64(1.5)), lambda: np.full_like(a, np.nan),
        lambda: np.isclose(a, a), lambda: np.allclose(a, a), lambda: np.random.default_rng(0).normal(size=3),
        lambda: statistics.fmean([1.0, 2.0]), lambda: statistics.median([1.0, 2.0]),
        lambda: statistics.stdev([1.0, 2.0, 4.0]), lambda: statistics.correlation([1.0, 2.0, 3.0], [1.0, 2.0, 4.0]),
        lambda: statistics.linear_regression([1.0, 2.0, 3.0], [1.0, 2.0, 4.0]),
        lambda: statistics.mode([1.0, 1.0, 2.0]),  # Counter.most_common imports heapq on first use
    )
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore")
        for call in calls:
            try:
                call()
            except Exception:
                pass


_warm_numpy()

from app.scripting.runtime_base import RESULT_MARKER, SANDBOX_TEST_ENV  # noqa: E402
from app.scripting.runtime import (  # noqa: E402
    Bars,
    OutputBuffer,
    ScriptContext,
    ScriptRuntimeError,
    build_namespace,
    collect_result,
    make_print,
)
from app.scripting.validator import (  # noqa: E402
    ALLOWED_IMPORTS,
    DEFAULT_TITLE,
    FORBIDDEN_NAMES,
    SCRIPT_FILENAME,
    attribute_problem,
    build_code,
    clip_text,
    make_error,
)

DEFAULT_MEMORY_MB = 1024  # app.config.SCRIPT_MEMORY_MB, sent by the launcher as payload["memory_mb"]
MEMORY_MB_RANGE = (128, 8192)
# POSIX only: extra *virtual* address space over memory_mb for RLIMIT_AS (see _limit_posix for the trade-off).
POSIX_AS_HEADROOM_MB = 512
MAX_RESULT_BYTES = 40 * 1024 * 1024  # serialized result; below the launcher's read limit (sandbox.MAX_STDOUT_BYTES)
OS_ISOLATION_KEY = "_os_isolation"  # internal result key (same value as sandbox.OS_ISOLATION_KEY), never in the API
MESSAGE_MAX = 2000  # characters of an exception message shown to the user
STDERR_SINK_MAX = 4000  # characters of Python-level stderr kept while the script runs (the rest is dropped)

SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "callable", "chr", "complex", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "hash", "hex", "int", "isinstance", "issubclass",
    "iter", "len", "list", "map", "max", "min", "next", "oct", "ord", "pow", "range", "repr", "reversed",
    "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip", "True", "False", "None", "Ellipsis",
    "NotImplemented", "ArithmeticError", "AssertionError", "AttributeError", "Exception", "FloatingPointError",
    "IndexError", "KeyError", "LookupError", "NameError", "NotImplementedError", "OverflowError",
    "RecursionError", "RuntimeError", "StopIteration", "TypeError", "UnboundLocalError", "ValueError",
    "ZeroDivisionError",
)
# Audited events that stay allowed while the script runs (everything else is denied).
SAFE_EVENTS = frozenset({"builtins.id", "random.seed", "sys.unraisablehook", "cpython._PySys_ClearAuditHooks"})


# ============================================================================= module proxies


class SafeModule:
    """Read-only view of an allowed module: no private/dunder names, no file/system APIs, and submodules only
    when they are themselves whitelisted (so ``statistics.sys`` or ``np.lib`` are unreachable).

    It must never expose ``__name__``: when ``from m import x`` fails with AttributeError, CPython falls back to
    ``sys.modules[m.__name__ + "." + x]``, which would hand out the raw submodule."""

    __slots__ = ("_m", "_r")

    def __init__(self, module: types.ModuleType, registry: dict[int, "SafeModule"]) -> None:
        object.__setattr__(self, "_m", module)
        object.__setattr__(self, "_r", registry)

    def __getattribute__(self, name: str) -> Any:
        problem = attribute_problem(name)
        if problem:
            raise BlockedAttribute(problem)
        module = object.__getattribute__(self, "_m")
        try:
            value = getattr(module, name)
        except AttributeError:
            raise AttributeError(f"El módulo '{module.__name__}' no tiene el atributo '{name}'.") from None
        if isinstance(value, types.ModuleType):
            wrapped = object.__getattribute__(self, "_r").get(id(value))
            if wrapped is None:
                raise BlockedAttribute(f"El submódulo '{name}' no está disponible en PyChart.")
            return wrapped
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Los módulos son de solo lectura en PyChart.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Los módulos son de solo lectura en PyChart.")

    def __repr__(self) -> str:
        return f"<módulo {object.__getattribute__(self, '_m').__name__}>"


def build_module_registry() -> dict[int, SafeModule]:
    allowed = [math, statistics, np, np.linalg, np.fft, np.random, np.ma, np.polynomial, np.emath, np.dtypes, np.exceptions]
    allowed += [getattr(np.polynomial, sub, None) for sub in ("polynomial", "chebyshev", "legendre", "laguerre", "hermite", "hermite_e")]
    allowed = [m for m in allowed if isinstance(m, types.ModuleType)]
    registry: dict[int, SafeModule] = {}
    for module in allowed:
        registry[id(module)] = SafeModule(module, registry)
    return registry


_BLOCKED_NUMPY_INTERNALS = (
    "numpy.ctypeslib", "numpy.f2py", "numpy.distutils", "numpy.testing", "numpy.lib.format", "numpy.lib.npyio",
    "numpy.lib._npyio_impl", "numpy.lib._datasource",
)


def make_import(registry: dict[int, SafeModule]):
    def safe_import(name: str, globals: Any = None, locals: Any = None, fromlist: Any = (), level: int = 0) -> Any:
        if type(name) is not str:
            raise BlockedImport("Importación no permitida.")
        # numpy's C code imports its internals lazily (repr of a dtype, ufunc errors ...) through PyImport_Import,
        # which calls the *calling frame's* __import__ with an empty list as fromlist, ignores the return value and
        # then reads sys.modules itself. Import statements always pass a tuple or None, so the script never takes
        # this branch, and it gets nothing back from it anyway.
        if (type(fromlist) is list and not fromlist and level == 0 and name not in ALLOWED_IMPORTS
                and name.startswith("numpy.") and not name.startswith(_BLOCKED_NUMPY_INTERNALS) and name in sys.modules):
            return None
        if level != 0 or name not in ALLOWED_IMPORTS:
            raise BlockedImport(f"Importación no permitida: '{name}'. Solo se permiten math, statistics y numpy.")
        target = sys.modules.get(name if fromlist else name.partition(".")[0])
        proxy = registry.get(id(target)) if target is not None else None
        if proxy is None:
            raise BlockedImport(f"El módulo '{name}' no está disponible en PyChart.")
        for item in fromlist or ():
            if type(item) is not str or item == "*" or attribute_problem(item):
                raise BlockedImport(f"No se puede importar '{item}' desde '{name}'.")
            try:  # resolved through the proxy, so `from numpy.random import mtrand` is refused like np.random.mtrand
                getattr(proxy, item)
            except BlockedAttribute as exc:
                raise BlockedImport(f"No se puede importar '{item}' desde '{name}': {exc}") from None
            except AttributeError:
                raise ImportError(f"No se puede importar '{item}' desde '{name}': ese nombre no existe.") from None
        return proxy

    return safe_import


def build_builtins(registry: dict[int, SafeModule], printer: Any, input_api: Any) -> dict[str, Any]:
    safe = {name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES if name not in FORBIDDEN_NAMES}
    safe["print"] = printer
    safe["input"] = input_api
    safe["__import__"] = make_import(registry)  # import statements go through the whitelist + proxies
    return safe


# ============================================================================= audit guard


def make_audit_guard(code_obj: Any) -> tuple[Any, Any, list[str]]:
    """(hook, set_phase, blocked) for one run. The phase starts as "user" (script running) and only moves to
    "report" (mapping errors / writing the result), where reading frames from tracebacks is allowed.

    Everything the hook depends on lives in this closure: once the hook is installed, the only other reference to
    its state is ``set_phase`` in run_payload's frame, so a script that reached the worker's module globals (via
    ``sys.modules['__main__']``) still cannot disarm it, and an unknown phase is treated as "user"."""
    phase = "user"
    exec_budget = 1
    blocked: list[str] = []
    safe_events, blocked_error, allowed_roots = SAFE_EVENTS, SandboxBlocked, ("math", "statistics", "numpy")
    as_str, is_instance, size = str, isinstance, len  # bound now: replacing builtins later changes nothing here

    def audit(event: str, args: tuple) -> None:
        nonlocal exec_budget
        if event in safe_events:
            return
        if event == "exec" and exec_budget > 0 and args and args[0] is code_obj:
            exec_budget -= 1
            return
        if event == "object.__getattr__" and phase == "report":
            return
        if event == "import" and args and as_str(args[0]).partition(".")[0] in allowed_roots:
            return  # cached already; a real load would still be stopped by the "open" rule
        detail = f"{event} {args[0]}" if args and is_instance(args[0], str) else event
        if size(blocked) < 20:
            blocked.append(detail[:80])
        raise blocked_error(f"Operación bloqueada por el sandbox de PyChart: {detail[:120]}")

    def set_phase(value: str) -> None:
        nonlocal phase
        phase = value

    return audit, set_phase, blocked


# --- end of section A: guards ---


# ============================================================================= OS limits

_JOB_HANDLE: Any = None  # kept open for the life of the process


def memory_limit_mb(value: Any) -> int:
    """Memory cap of this worker in MB (payload ``memory_mb``; DEFAULT_MEMORY_MB when missing or invalid)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return DEFAULT_MEMORY_MB
    return int(min(max(value, MEMORY_MB_RANGE[0]), MEMORY_MB_RANGE[1]))


def _limit_windows(memory_mb: int) -> None:
    """Job object: memory cap + no child processes (CreateProcess fails inside the job)."""
    global _JOB_HANDLE
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

    class _Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basic),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    info = _Extended()
    info.BasicLimitInformation.LimitFlags = 0x100 | 0x8  # PROCESS_MEMORY | ACTIVE_PROCESS
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = memory_mb * 1024 * 1024
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
    _JOB_HANDLE = job


def _raise_oom_priority() -> bool:
    """Best effort (Linux): make the kernel OOM killer sacrifice THIS short-lived sandbox worker before anything else
    (above all the server) when the VM runs out of memory. Raising one's own oom_score_adj needs no privilege; on
    macOS / inside containers without the file, or on any error, this is a silent no-op (False)."""
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as fh:
            fh.write("1000")  # max: most killable
    except OSError:
        return False
    return True


def _limit_posix(memory_mb: int) -> dict[str, Any]:
    """rlimits + OOM priority -> {"oom_score_adj": bool, "rlimits": [names of the limits applied]}."""
    import resource

    status: dict[str, Any] = {"oom_score_adj": bool(_raise_oom_priority())}
    # RLIMIT_AS caps *virtual* address space, not resident memory. numpy / BLAS reserve large virtual mappings that are
    # mostly non-resident, so the cap must sit well above memory_mb or legitimate vector math fails with MemoryError;
    # yet every extra MB of cap is memory a hostile script can actually fault in, and on a small VM (e.g. Fly 1 GB with
    # NEXUS_SCRIPT_MEMORY_MB=320) no single cap both allows numpy and stays under the VM. So the cap stays a coarse
    # guard with enough headroom for numpy (BLAS threads are already disabled above), trimmed from +1024 to
    # +POSIX_AS_HEADROOM_MB MB; the real protection against taking the server down is _raise_oom_priority() above.
    address_space = (memory_mb + POSIX_AS_HEADROOM_MB) * 1024 * 1024
    limits = [("AS", resource.RLIMIT_AS, address_space), ("FSIZE", resource.RLIMIT_FSIZE, 0),
              ("CORE", resource.RLIMIT_CORE, 0)]
    if hasattr(resource, "RLIMIT_NPROC"):  # no child processes (nor threads), like the Windows job object
        limits.append(("NPROC", resource.RLIMIT_NPROC, 0))
    applied: list[str] = []
    for name, res, value in limits:
        try:
            _, hard = resource.getrlimit(res)
            new = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(res, (new, new))
        except (ValueError, OSError):
            continue
        applied.append(name)
    status["rlimits"] = applied
    return status


# ----------------------------------------------------------------------------- Linux isolation (best effort)

_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000


def _err_name(exc: BaseException) -> str:
    """Short, stable name of an error for the status dict (``EPERM``, ``AttributeError`` ...)."""
    code = getattr(exc, "errno", None)
    if isinstance(code, int) and code in errno.errorcode:
        return errno.errorcode[code]
    return type(exc).__name__


def _prctl(option: int, arg2: int = 0) -> int:
    """prctl(option, arg2, 0, 0, 0) through libc -> its result; OSError (errno) when it fails."""
    import ctypes

    fn = ctypes.CDLL(None, use_errno=True).prctl
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    res = fn(option, arg2, 0, 0, 0)
    if res < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err) if err else "prctl")
    return int(res)


def _unshare(flags: int) -> None:
    """unshare(2): ``os.unshare`` on Python 3.12+, libc before. OSError (errno) when the kernel refuses."""
    native = getattr(os, "unshare", None)
    if native is not None:
        native(flags)
        return
    import ctypes

    fn = ctypes.CDLL(None, use_errno=True).unshare
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_int]
    if fn(flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err) if err else "unshare")


def _net_namespace_id() -> str | None:
    try:
        return os.readlink("/proc/self/ns/net")  # e.g. "net:[4026531840]"
    except OSError:
        return None


def _enter_empty_network() -> tuple[bool, str]:
    """Moves this worker into a new user + network namespace -> (active, error name).

    The new network namespace has only a loopback interface, and it is down: no IP connection can leave the worker,
    not even to 127.0.0.1 of the host (a Unix socket with a path is a file: the namespace does not isolate it, nor
    does it isolate the filesystem). The user namespace is what lets an unprivileged process create it (no uid
    map is written). Platforms may refuse, and then the run goes on without it: EPERM (container seccomp profile,
    restricted unprivileged user namespaces), ENOSPC / EUSERS (namespace limit reached), EINVAL (the process has
    threads), ENOSYS."""
    before = _net_namespace_id()
    try:
        _unshare(getattr(os, "CLONE_NEWUSER", _CLONE_NEWUSER) | getattr(os, "CLONE_NEWNET", _CLONE_NEWNET))
    except OSError as exc:
        return False, _err_name(exc)
    if before is not None and _net_namespace_id() == before:
        return False, "sin cambio"
    return True, ""


def _close_inherited_fds(inherited: list[int] | None = None) -> int:
    """Closes every inherited descriptor above stderr (``_INHERITED_FDS``, listed at start-up) -> how many. The
    launcher already starts the worker with close_fds, so this is normally 0; it covers a launcher that passes more.
    Raises OSError when /proc/self/fd could not be listed at start-up."""
    fds = _INHERITED_FDS if inherited is None else inherited
    if fds is None:
        raise OSError(errno.ENOENT, "no se pudo listar /proc/self/fd")
    closed = 0
    for fd in fds:
        if fd <= 2:
            continue
        try:
            os.close(fd)
        except OSError:  # closed meanwhile
            continue
        closed += 1
    return closed


def _isolate_linux() -> dict[str, Any]:
    """Linux OS isolation of this worker, step by step (a failing step never skips the next ones) -> which
    protections are active. Runs once, right before the script, while the worker is still single-threaded.

    The namespace comes first: an LSM may move a task to another profile when it creates a user namespace (Ubuntu's
    AppArmor restriction of unprivileged user namespaces), and AppArmor may refuse a profile transition to a task
    that already has no_new_privs. no_new_privs needs no privilege, so setting it afterwards always works."""
    status: dict[str, Any] = {}
    try:
        active, problem = _enter_empty_network()
    except Exception as exc:  # noqa: BLE001 - best effort, recorded
        active, problem = False, _err_name(exc)
    status["net_namespace"] = active
    if problem:
        status["net_namespace_error"] = problem
    try:  # irreversible and inherited: no execve can ever grant privileges again (setuid bits, file capabilities)
        status["no_new_privs"] = _prctl(_PR_SET_NO_NEW_PRIVS, 1) == 0 and _prctl(_PR_GET_NO_NEW_PRIVS) == 1
    except Exception as exc:  # noqa: BLE001
        status["no_new_privs"] = False
        status["no_new_privs_error"] = _err_name(exc)
    try:
        status["fds_closed"] = _close_inherited_fds()
    except Exception as exc:  # noqa: BLE001
        status["fds_error"] = _err_name(exc)
    return status


def _os_kind() -> str:
    """Which OS layer applies: "nt", "linux" or "posix" (macOS & co.)."""
    if os.name == "nt":
        return "nt"
    return "linux" if sys.platform.startswith("linux") else "posix"


def harden_process(memory_mb: int = DEFAULT_MEMORY_MB) -> dict[str, Any]:
    """Applies the OS layer -> what is active (internal: sent back under OS_ISOLATION_KEY). Never raises: the run
    continues with the other layers whatever fails here."""
    status: dict[str, Any] = {"platform": sys.platform}
    kind = _os_kind()
    if kind == "nt":
        try:
            _limit_windows(memory_mb)
            status["job_object"] = True
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"pychart worker: no se aplicaron los límites del sistema: {exc}\n")
            status.update(job_object=False, error=_err_name(exc))
        return status
    if kind == "linux":
        try:
            status.update(_isolate_linux())
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"pychart worker: aislamiento del sistema incompleto: {exc}\n")
            status["error"] = _err_name(exc)
    try:
        status.update(_limit_posix(memory_mb))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"pychart worker: no se aplicaron los límites del sistema: {exc}\n")
        status["error"] = _err_name(exc)
    return status


# ============================================================================= errors

class SandboxBlocked(PermissionError):
    """Raised by the audit hook."""


class BlockedImport(ImportError):
    """Raised by the import hook."""


class BlockedAttribute(AttributeError):
    """Raised by the module proxies."""


SANDBOX_ERRORS = (SandboxBlocked, BlockedImport, BlockedAttribute)

_EXC_LABELS = {
    "TypeError": "Error de tipo",
    "ValueError": "Valor inválido",
    "ZeroDivisionError": "División por cero",
    "IndexError": "Índice fuera de rango",
    "KeyError": "Clave inexistente",
    "AttributeError": "Atributo inexistente",
    "OverflowError": "Desbordamiento numérico",
    "ImportError": "Error de importación",
    "ModuleNotFoundError": "Importación no permitida",
    "PermissionError": "Operación bloqueada",
    "AssertionError": "Aserción fallida",
    "UnboundLocalError": "Variable local sin asignar",
    "StopIteration": "Iteración terminada",
    "FloatingPointError": "Error de coma flotante",
    "LinAlgError": "Error de álgebra lineal",
}


def _column(code: types.CodeType, lasti: int) -> int | None:
    if lasti is None or lasti < 0:
        return None
    for i, pos in enumerate(code.co_positions()):
        if i == lasti // 2:
            return pos[2] + 1 if isinstance(pos[2], int) else None
    return None


def user_location(exc: BaseException) -> tuple[int | None, int | None]:
    """(line, col) of the innermost frame of the user script in the traceback."""
    line = col = None
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if code.co_filename == SCRIPT_FILENAME:
            line, col = tb.tb_lineno, _column(code, tb.tb_lasti)
        tb = tb.tb_next
    return line, col


_NAME_ERROR = re.compile(r"name '(.+?)' is not defined")


def _exception_text(exc: BaseException) -> str:
    try:
        text = str(exc)
    except BaseException:  # noqa: BLE001 - a failing __str__ (or a script formatter) must not break the report
        return ""
    return clip_text(text, MESSAGE_MAX).strip()  # cut first: the script controls the size of its messages


def exception_message(exc: BaseException) -> str:
    """Spanish message for a script failure (at most about MESSAGE_MAX characters, whatever the script raised)."""
    text = _exception_text(exc)
    if isinstance(exc, (ScriptRuntimeError,) + SANDBOX_ERRORS):
        return text or type(exc).__name__
    name = type(exc).__name__
    if isinstance(exc, NameError) and not isinstance(exc, UnboundLocalError):
        match = _NAME_ERROR.match(text)
        if match:
            ident = match.group(1)
            if ident in FORBIDDEN_NAMES or ident.startswith("__"):
                return f"'{ident}' no está disponible en PyChart."
            return f"El nombre '{ident}' no está definido."
    if isinstance(exc, MemoryError):
        return "Memoria insuficiente: el script superó el límite de memoria del sandbox."
    if isinstance(exc, RecursionError):
        return "Recursión demasiado profunda (RecursionError)."
    label = _EXC_LABELS.get(name, "Error de ejecución")
    return f"{label} ({name}): {text}" if text else f"{label} ({name})."


# ============================================================================= run


def empty_result() -> dict[str, Any]:
    return {
        "ok": False,
        "errors": [],
        "stdout": "",
        "duration_ms": 0,
        "meta": {"title": DEFAULT_TITLE, "overlay": True, "kind": "indicator"},
        "time": [],
        "plots": [],
        "markers": [],
        "hlines": [],
        "bgcolors": [],
        "alerts": [],
        "strategy": None,
        "candles": [],
    }


def _bars_from_payload(payload: dict) -> Bars:
    times = payload.get("time") or []
    n = len(times)

    def column(key: str, default: float) -> np.ndarray:
        values = payload.get(key)
        if values is None:
            return np.full(n, default)
        return np.asarray([math.nan if v is None else v for v in values], dtype=np.float64)

    return Bars(np.asarray(times, dtype=np.int64), column("open", math.nan), column("high", math.nan),
                column("low", math.nan), column("close", math.nan), column("volume", 0.0))


def run_payload(payload: dict) -> dict[str, Any]:
    """Validates, executes and collects one script. Never raises."""
    started = time.perf_counter()
    result = empty_result()
    try:
        code = payload.get("code")
        skip = payload.get("skip_validation") is True and os.environ.get(SANDBOX_TEST_ENV) == "1"
        code_obj, errors = build_code(code if isinstance(code, str) else "", validate=not skip)
        if errors:
            result["errors"] = errors
            return result
        inputs = payload.get("inputs")
        buffer = OutputBuffer()
        news = payload.get("news")
        ctx = ScriptContext(_bars_from_payload(payload), inputs if isinstance(inputs, dict) else {},
                            symbol=str(payload.get("symbol") or ""), tf=str(payload.get("tf") or ""), stdout=buffer,
                            news=news if isinstance(news, dict) else None)
        namespace = build_namespace(ctx)
        registry = build_module_registry()
        printer = make_print(buffer)
        script_globals = dict(namespace)
        script_globals.update(np=registry[id(np)], math=registry[id(math)], print=printer, __name__="__pychart__")
        script_globals["__builtins__"] = build_builtins(registry, printer, namespace["input"])
        memory_mb = memory_limit_mb(payload.get("memory_mb"))
    except Exception as exc:
        result["errors"] = [make_error("runtime", f"No se pudo preparar la ejecución del script: {exc}")]
        return result
    os_isolation = harden_process(memory_mb)
    warnings.simplefilter("ignore")
    np.seterr(all="ignore")
    real_stderr = sys.stderr
    sys.stdout = buffer  # stray Python-level writes end up in the captured output
    sys.stderr = OutputBuffer(STDERR_SINK_MAX)  # capped (C-level stderr is capped by the launcher)
    hook, set_phase, blocked = make_audit_guard(code_obj)
    sys.addaudithook(hook)
    del hook  # from here on only the interpreter holds the hook
    try:
        exec(code_obj, script_globals)
        result = collect_result(ctx)
        if blocked:
            result["stdout"] += f"Aviso: el sandbox bloqueó: {', '.join(sorted(set(blocked)))}\n"
    except BaseException as exc:  # noqa: BLE001 - every user failure becomes a ScriptError
        # str(exc) may run script code (e.g. a numpy print formatter): build the message under the "user" rules.
        message = exception_message(exc)
        kind = "security" if isinstance(exc, SANDBOX_ERRORS) else "runtime"
        script_globals.clear()
        set_phase("report")
        line, col = user_location(exc)
        result = empty_result()
        result["meta"] = dict(ctx.meta)
        result["errors"] = [make_error(kind, message, line, col)]
        result["stdout"] = buffer.getvalue()
    set_phase("report")
    sys.stderr = real_stderr
    result["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    result[OS_ISOLATION_KEY] = os_isolation  # internal: the launcher removes it (not part of ScriptRunResult)
    return result


def _fallback_json(result: dict, message: str) -> str:
    """Error result (keeping the small ``meta`` / ``stdout`` / ``duration_ms`` of ``result``) as JSON."""
    fallback = empty_result()
    fallback["errors"] = [make_error("runtime", message)]
    try:
        if isinstance(result.get("meta"), dict):
            fallback["meta"] = result["meta"]
        if isinstance(result.get("stdout"), str):
            fallback["stdout"] = clip_text(result["stdout"], 30_000)
        if isinstance(result.get("duration_ms"), (int, float)):
            fallback["duration_ms"] = result["duration_ms"]
        return json.dumps(fallback, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, MemoryError, RecursionError):
        bare = empty_result()
        bare["errors"] = [make_error("runtime", message)]
        return json.dumps(bare, ensure_ascii=True)


def emit(stream: Any, result: dict) -> None:
    """Writes ``RESULT_MARKER`` + the result as JSON: an error result instead when it cannot be serialized or is
    bigger than MAX_RESULT_BYTES (the launcher stops reading a little above that)."""
    try:
        text = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, MemoryError, RecursionError):
        text = _fallback_json(result, "El resultado del script no se pudo serializar.")
    if len(text) > MAX_RESULT_BYTES:  # ASCII: characters == bytes
        size_mb = len(text) / (1024 * 1024)
        del text  # release it before building the replacement
        text = _fallback_json(
            result,
            f"El resultado del script es demasiado grande ({size_mb:.0f} MB; el máximo es "
            f"{MAX_RESULT_BYTES // (1024 * 1024)} MB). Reduce el número de velas, de plot() o de marcadores.",
        )
    stream.write(RESULT_MARKER.encode("ascii"))
    stream.write(text.encode("ascii"))
    stream.write(b"\n")
    stream.flush()


def main() -> int:
    out = sys.stdout.buffer
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload")
    except (ValueError, UnicodeDecodeError):
        bad = empty_result()
        bad["errors"] = [make_error("runtime", "Entrada inválida para el sandbox de PyChart.")]
        emit(out, bad)
        return 0
    try:
        result = run_payload(payload)
    except BaseException as exc:  # noqa: BLE001 - run_payload never raises; report rather than exit without a result
        result = empty_result()
        result["errors"] = [make_error("runtime", f"Fallo interno del sandbox de PyChart ({type(exc).__name__}).")]
    emit(out, result)
    return 0


if __name__ == "__main__":
    exit_code = main()
    try:
        sys.stderr.flush()
    finally:
        os._exit(exit_code)
