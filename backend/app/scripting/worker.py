"""PyChart sandbox worker: runs ONE user script in this (isolated, short-lived) process.

Started by ``app.scripting.sandbox.run_script`` as ``python -I worker.py`` with a minimal environment. Protocol:
a JSON object on stdin ``{code, time, open, high, low, close, volume, inputs, symbol, tf}``; one JSON
``ScriptRunResult`` on stdout after ``RESULT_MARKER``.

Defence in depth (the AST validation of ``validator.py`` is layer 1):
2. restricted ``__builtins__`` (safe subset, custom ``print``/``__import__``, no ``open``/``eval``/``type``...);
3. imports limited to math / statistics / numpy, handed out as read-only module proxies that refuse private
   names, file/system APIs and modules outside the whitelist (``statistics.sys`` ...);
4. a ``sys.addaudithook`` guard armed right before the script runs: every audited operation is denied except a
   few harmless ones (no file ``open``, ``os.*``, ``subprocess``, ``socket``, ``ctypes``, ``shutil``, ``winreg``,
   non-whitelisted imports, ``compile`` or a second ``exec``);
5. OS limits: Windows job object (memory cap, no child processes) / POSIX rlimits (address space, file size).
The parent kills the process on timeout.
"""
from __future__ import annotations

import os

for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_var] = "1"  # before numpy is imported: no BLAS thread pools in the sandbox

import builtins
import json
import math
import re
import statistics
import sys
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
    make_error,
)

MEMORY_LIMIT_MB = 1536

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
    when they are themselves whitelisted (so ``statistics.sys`` or ``np.lib`` are unreachable)."""

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
        # numpy's own C code imports its internals lazily (PyImport_ImportModule -> __import__(name, ..., ["__doc__"]))
        # and, when called from the script, that goes through these builtins. The AST validator never lets user
        # code name a numpy submodule, so only numpy itself reaches this branch; serve already-loaded internals.
        if level == 0 and fromlist and name.startswith("numpy.") and not name.startswith(_BLOCKED_NUMPY_INTERNALS):
            internal = sys.modules.get(name)
            if internal is not None:
                return internal
        if level != 0 or name not in ALLOWED_IMPORTS:
            raise BlockedImport(f"Importación no permitida: '{name}'. Solo se permiten math, statistics y numpy.")
        for item in fromlist or ():
            if item == "*" or attribute_problem(str(item)):
                raise BlockedImport(f"No se puede importar '{item}' desde '{name}'.")
        target = sys.modules.get(name if fromlist else name.partition(".")[0])
        proxy = registry.get(id(target)) if target is not None else None
        if proxy is None:
            raise BlockedImport(f"El módulo '{name}' no está disponible en PyChart.")
        return proxy

    return safe_import


def build_builtins(registry: dict[int, SafeModule], printer: Any, input_api: Any) -> dict[str, Any]:
    safe = {name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES if name not in FORBIDDEN_NAMES}
    safe["print"] = printer
    safe["input"] = input_api
    safe["__import__"] = make_import(registry)  # import statements go through the whitelist + proxies
    return safe


# ============================================================================= audit guard


class _Guard:
    phase = "setup"  # setup -> user (script running) -> report (mapping errors / writing the result)
    code: Any = None
    exec_budget = 1
    blocked: list[str] = []


def _audit(event: str, args: tuple) -> None:
    phase = _Guard.phase
    if phase == "setup" or event in SAFE_EVENTS:
        return
    if event == "exec" and _Guard.exec_budget > 0 and args and args[0] is _Guard.code:
        _Guard.exec_budget -= 1
        return
    if event == "object.__getattr__" and phase == "report":
        return
    if event == "import" and args and str(args[0]).partition(".")[0] in ("math", "statistics", "numpy"):
        return  # cached already; a real load would still be stopped by the "open" rule
    detail = f"{event} {args[0]}" if args and isinstance(args[0], str) else event
    if len(_Guard.blocked) < 20:
        _Guard.blocked.append(detail[:80])
    raise SandboxBlocked(f"Operación bloqueada por el sandbox de PyChart: {detail[:120]}")


# --- end of section A: guards ---


# ============================================================================= OS limits

_JOB_HANDLE: Any = None  # kept open for the life of the process


def _limit_windows() -> None:
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
    info.ProcessMemoryLimit = MEMORY_LIMIT_MB * 1024 * 1024
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
    _JOB_HANDLE = job


def _limit_posix() -> None:
    import resource

    address_space = (MEMORY_LIMIT_MB + 1024) * 1024 * 1024  # headroom for interpreter / numpy mappings
    for res, value in ((resource.RLIMIT_AS, address_space), (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_CORE, 0)):
        try:
            _, hard = resource.getrlimit(res)
            new = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(res, (new, new))
        except (ValueError, OSError):
            continue


def harden_process() -> None:
    try:
        _limit_windows() if os.name == "nt" else _limit_posix()
    except Exception as exc:  # the run continues with the other layers
        sys.stderr.write(f"pychart worker: no se aplicaron los límites del sistema: {exc}\n")


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
    "ImportError": "Importación no permitida",
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


def exception_message(exc: BaseException) -> str:
    if isinstance(exc, (ScriptRuntimeError,) + SANDBOX_ERRORS):
        return str(exc)
    name = type(exc).__name__
    text = str(exc).strip()
    if isinstance(exc, NameError) and not isinstance(exc, UnboundLocalError):
        match = re.match(r"name '(.+?)' is not defined", text)
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
        ctx = ScriptContext(_bars_from_payload(payload), inputs if isinstance(inputs, dict) else {},
                            symbol=str(payload.get("symbol") or ""), tf=str(payload.get("tf") or ""), stdout=buffer)
        namespace = build_namespace(ctx)
        registry = build_module_registry()
        printer = make_print(buffer)
        script_globals = dict(namespace)
        script_globals.update(np=registry[id(np)], math=registry[id(math)], print=printer, __name__="__pychart__")
        script_globals["__builtins__"] = build_builtins(registry, printer, namespace["input"])
    except Exception as exc:
        result["errors"] = [make_error("runtime", f"No se pudo preparar la ejecución del script: {exc}")]
        return result
    harden_process()
    warnings.simplefilter("ignore")
    np.seterr(all="ignore")
    sys.stdout = buffer  # stray Python-level writes end up in the captured output
    _Guard.code = code_obj
    sys.addaudithook(_audit)
    _Guard.phase = "user"
    try:
        exec(code_obj, script_globals)
        result = collect_result(ctx)
        if _Guard.blocked:
            result["stdout"] += f"Aviso: el sandbox bloqueó: {', '.join(sorted(set(_Guard.blocked)))}\n"
    except BaseException as exc:  # noqa: BLE001 - every user failure becomes a ScriptError
        _Guard.phase = "report"
        script_globals.clear()
        line, col = user_location(exc)
        result = empty_result()
        result["meta"] = dict(ctx.meta)
        kind = "security" if isinstance(exc, SANDBOX_ERRORS) else "runtime"
        result["errors"] = [make_error(kind, exception_message(exc), line, col)]
        result["stdout"] = buffer.getvalue()
    _Guard.phase = "report"
    result["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return result


def emit(stream: Any, result: dict) -> None:
    try:
        text = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        fallback = empty_result()
        fallback["errors"] = [make_error("runtime", "El resultado del script no se pudo serializar.")]
        text = json.dumps(fallback, ensure_ascii=True)
    stream.write((RESULT_MARKER + text + "\n").encode("ascii"))
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
    emit(out, run_payload(payload))
    return 0


if __name__ == "__main__":
    exit_code = main()
    try:
        sys.stderr.flush()
    finally:
        os._exit(exit_code)
