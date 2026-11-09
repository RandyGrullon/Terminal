"""Shared pieces of the PyChart runtime: the per-run context, bar data, output capture and the coercion
helpers that turn whatever the user passes (numpy arrays, lists, scalars, None) into bar-aligned series.

The runtime modules are imported by the sandbox worker before the sandbox is armed; they never touch files,
the network or the interpreter internals. Objects handed to user code keep their state in ``_``-prefixed
attributes, which the AST validator forbids accessing.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

MAX_PLOTS = 64
MAX_MARKERS = 3000
MAX_BGCOLORS = 20_000
MAX_ALERTS = 200
MAX_HLINES = 64
MAX_STDOUT = 20_000

NUMBER_TYPES = (bool, int, float, np.number, np.bool_)

# Sandbox protocol (shared by sandbox.py and worker.py).
RESULT_MARKER = "@@NEXUS_PYCHART_RESULT@@"
SANDBOX_TEST_ENV = "NEXUS_SANDBOX_TEST"  # the worker honours skip_validation only when this is "1" (tests)


class ScriptRuntimeError(Exception):
    """Error caused by a wrong use of the PyChart API (message in Spanish, shown to the user)."""


class Bars:
    """OHLCV arrays of the run (float64; ``time`` int64 unix seconds), all with the same length."""

    __slots__ = ("time", "open", "high", "low", "close", "volume", "n")

    def __init__(self, time: Any, open_: Any, high: Any, low: Any, close: Any, volume: Any) -> None:
        self.time = np.asarray(time, dtype=np.int64)
        self.open = np.asarray(open_, dtype=np.float64)
        self.high = np.asarray(high, dtype=np.float64)
        self.low = np.asarray(low, dtype=np.float64)
        self.close = np.asarray(close, dtype=np.float64)
        self.volume = np.asarray(volume, dtype=np.float64)
        self.n = int(self.time.shape[0])
        for name in ("open", "high", "low", "close", "volume"):
            if getattr(self, name).shape != (self.n,):
                raise ValueError(f"bars: '{name}' must have {self.n} values")

    def source(self, name: str) -> np.ndarray:
        """Named price source (close, open, high, low, hl2, hlc3, ohlc4, volume)."""
        if name == "hl2":
            return (self.high + self.low) / 2.0
        if name == "hlc3":
            return (self.high + self.low + self.close) / 3.0
        if name == "ohlc4":
            return (self.open + self.high + self.low + self.close) / 4.0
        if name in ("open", "high", "low", "close", "volume"):
            return getattr(self, name).copy()
        raise ScriptRuntimeError(f"Fuente desconocida '{name}'. Usa close, open, high, low, hl2, hlc3, ohlc4 o volume.")


class OutputBuffer:
    """Captures ``print`` output up to ``limit`` characters (also stands in for ``sys.stdout`` in the worker)."""

    encoding = "utf-8"
    errors = "replace"

    def isatty(self) -> bool:
        return False

    def __init__(self, limit: int = MAX_STDOUT) -> None:
        self._parts: list[str] = []
        self._size = 0
        self._limit = int(limit)
        self.truncated = False

    def write(self, text: str) -> int:
        if not text:
            return 0
        if not self.truncated:
            room = self._limit - self._size
            if len(text) > room:
                self._parts.append(text[: max(room, 0)])
                self._size = self._limit
                self.truncated = True
            else:
                self._parts.append(text)
                self._size += len(text)
        return len(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        text = "".join(self._parts)
        if self.truncated:
            text += f"\n… (salida truncada a {self._limit:,} caracteres)".replace(",", " ")
        return text


def make_print(buffer: OutputBuffer):
    """``print`` replacement for user code: always writes to the capped buffer (``file=`` is ignored)."""

    def print_(*args: Any, sep: Any = " ", end: Any = "\n", file: Any = None, flush: bool = False) -> None:
        if buffer.truncated:
            return None
        sep_s = " " if sep is None else str(sep)
        for i, arg in enumerate(args):
            if i:
                buffer.write(sep_s)
            buffer.write(str(arg))
            if buffer.truncated:
                return None
        buffer.write("\n" if end is None else str(end))
        return None

    return print_


class ScriptContext:
    """Everything a run produces; filled by the API objects while the user script executes."""

    def __init__(
        self,
        bars: Bars,
        inputs: dict[str, Any] | None = None,
        *,
        symbol: str = "",
        tf: str = "",
        stdout: OutputBuffer | None = None,
        news: dict | None = None,
    ) -> None:
        self.bars = bars
        self.news = news if isinstance(news, dict) else None  # economic calendar of the run (runtime_news)
        self.n = bars.n
        self.inputs = dict(inputs or {})
        self.symbol = str(symbol or "")
        self.tf = str(tf or "")
        self.stdout = stdout or OutputBuffer()
        self.meta: dict[str, Any] = {"title": "Script sin título", "overlay": True, "kind": "indicator"}
        self.declared = False
        self.strategy_config: dict[str, Any] | None = None
        self.strategy_rules: Any = None  # runtime_strategy.OrderRules, set by runtime.build_namespace
        self.plots: list[dict] = []
        self.markers: list[dict] = []
        self.hlines: list[dict] = []
        self.bgcolors: dict[int, str] = {}
        self.alerts: list[tuple[int, str, str]] = []
        self.input_count = 0
        self.notices: set[str] = set()

    def notice(self, text: str) -> None:
        """One-time informative line in the script output (e.g. truncated markers)."""
        if text not in self.notices:
            self.notices.add(text)
            self.stdout.write(f"Aviso: {text}\n")


# ============================================================================= coercion helpers


def _object_to_float(arr: np.ndarray, what: str) -> np.ndarray:
    flat = []
    for v in arr.ravel().tolist():
        if v is None:
            flat.append(math.nan)
        elif isinstance(v, (bool, int, float)):
            flat.append(float(v))
        else:
            raise ScriptRuntimeError(f"{what}: se esperaba una serie numérica (contiene {type(v).__name__}).")
    return np.asarray(flat, dtype=np.float64).reshape(arr.shape)


def to_series(value: Any, n: int, what: str = "serie") -> np.ndarray:
    """Bar-aligned float64 series: scalars are broadcast, None becomes NaN, booleans 1/0."""
    if value is None:
        return np.full(n, np.nan)
    if isinstance(value, NUMBER_TYPES):
        return np.full(n, float(value))
    try:
        arr = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise ScriptRuntimeError(f"{what}: se esperaba una serie numérica.") from exc
    if arr.dtype == object:
        arr = _object_to_float(arr, what)
    elif arr.dtype.kind not in "biuf":
        raise ScriptRuntimeError(f"{what}: se esperaba una serie numérica (tipo {arr.dtype}).")
    if arr.ndim == 0:
        return np.full(n, float(arr))
    if arr.ndim != 1:
        raise ScriptRuntimeError(f"{what}: se esperaba una serie de una dimensión (forma {arr.shape}).")
    if arr.shape[0] != n:
        raise ScriptRuntimeError(
            f"{what}: la serie tiene {arr.shape[0]} valores pero hay {n} velas. Usa series alineadas con las velas "
            "(por ejemplo ta.change() en lugar de np.diff())."
        )
    return arr.astype(np.float64, copy=True)


def to_bool(value: Any, n: int, what: str = "condición") -> np.ndarray:
    """Bar-aligned boolean series (NaN / None are False)."""
    if value is None:
        return np.zeros(n, dtype=bool)
    if isinstance(value, NUMBER_TYPES):
        flag = bool(value) and not (isinstance(value, float) and math.isnan(value))
        return np.full(n, flag, dtype=bool)
    arr = np.asarray(value) if not isinstance(value, np.ndarray) else value
    if arr.dtype == bool and arr.ndim == 1 and arr.shape[0] == n:
        return arr.copy()
    series = to_series(value, n, what)
    return np.isfinite(series) & (series != 0.0)


def to_int(value: Any, what: str, *, minimum: int | None = 1, maximum: int | None = 100_000) -> int:
    """Integer parameter (lengths, offsets); integral floats are accepted."""
    if isinstance(value, np.ndarray) and value.ndim == 0:
        value = value.item()
    if isinstance(value, np.number):
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScriptRuntimeError(f"{what}: se esperaba un número entero (recibido: {value!r}).")
    if isinstance(value, float) and not value.is_integer():
        raise ScriptRuntimeError(f"{what}: se esperaba un número entero (recibido: {value}).")
    out = int(value)
    if minimum is not None and out < minimum:
        raise ScriptRuntimeError(f"{what}: debe ser ≥ {minimum} (recibido: {out}).")
    if maximum is not None and out > maximum:
        raise ScriptRuntimeError(f"{what}: debe ser ≤ {maximum} (recibido: {out}).")
    return out


def to_float(value: Any, what: str, *, allow_nan: bool = False) -> float:
    """Scalar float parameter."""
    if isinstance(value, np.ndarray) and value.ndim == 0:
        value = value.item()
    if isinstance(value, np.number):
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScriptRuntimeError(f"{what}: se esperaba un número (recibido: {value!r}).")
    out = float(value)
    if not allow_nan and not math.isfinite(out):
        raise ScriptRuntimeError(f"{what}: el número debe ser finito (recibido: {out}).")
    return out


def json_series(arr: np.ndarray, decimals: int | None = None) -> list:
    """numpy series -> JSON list with None for NaN / ±inf."""
    a = np.asarray(arr, dtype=np.float64)
    if decimals is not None:
        a = np.round(a, decimals)
    out = a.astype(object)
    out[~np.isfinite(a)] = None
    return out.tolist()


def clean_value(value: Any) -> Any:
    """Recursively converts numpy scalars/arrays to JSON-safe Python values (NaN/inf -> None)."""
    if isinstance(value, dict):
        return {str(k): clean_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_value(v) for v in value]
    if isinstance(value, np.ndarray):
        return [clean_value(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
