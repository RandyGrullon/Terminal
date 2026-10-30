"""Indicator registry core: spec builders, parameter coercion, price sources and time anchors.

Category modules (``indicators_trend``, ``indicators_momentum``...) register their indicators with the
:func:`indicator` decorator. Each indicator function receives a :class:`Bars` view and the already coerced
parameters and returns ``{output_key: array}`` aligned with the bars. Every value at bar ``i`` must only use
bars ``<= i`` (causality); ``NaN`` marks the warm-up.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from app.engine import ta
from app.engine.indicators_helpers import (  # noqa: F401 - re-exported for the category modules
    Groups,
    ffill,
    group_cumsum,
    group_running,
    groups_of,
    hold_events,
    ma_of,
    mask,
    previous_group_ohlc,
    rolling_apply_windows,
)

NAN = float("nan")


class IndicatorError(ValueError):
    """Unknown indicator, invalid parameters or a failed computation. The message is Spanish (user-facing)."""


# ----------------------------------------------------------------------------- palette & vocabulary

POS = "#16c784"
NEG = "#ea3943"
WARN = "#f5a524"
INFO = "#3ea6ff"
ACCENT = "#2f6bff"
PURPLE = "#a97bff"
CYAN = "#22d3ee"
MUTED = "#7a8396"
ORANGE = "#ff8c42"
PINK = "#ff5fa2"
YELLOW = "#ffd166"
TEAL = "#14b8a6"
LIME = "#a3e635"
INDIGO = "#6366f1"
GOLD = "#eab308"
BROWN = "#c08457"
SKY = "#7dd3fc"
MAGENTA = "#e879f9"
LILAC = "#c4b5fd"
ROSE = "#fb7185"
SAND = "#fcd34d"
WHITE = "#e6e9ef"

TREND = "Tendencia"
MOMENTUM = "Momento"
VOLATILITY = "Volatilidad"
VOLUME = "Volumen"
LEVELS = "Soportes/Resistencias"
STATS = "Estadística"
NEXUS = "NEXUS"
CATEGORIES: tuple[str, ...] = (TREND, MOMENTUM, VOLATILITY, VOLUME, LEVELS, STATS, NEXUS)

PARAM_TYPES = ("int", "float", "bool", "select", "source")
OUTPUT_TYPES = ("line", "histogram", "area", "dots", "step")
SOURCES: tuple[str, ...] = ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "hlcc4")
MA_TYPES: tuple[str, ...] = ("SMA", "EMA", "WMA", "HMA", "RMA", "VWMA")
ANCHORS: tuple[str, ...] = ("Auto", "Día", "Semana", "Mes", "Año")

DAY_S = 86_400
WEEK_S = 7 * DAY_S
_MONDAY_OFFSET_S = 4 * DAY_S  # 1970-01-01 was a Thursday; weeks start on Monday 00:00 UTC


# ----------------------------------------------------------------------------- spec builders


def p_int(name: str, label: str, default: int, lo: int, hi: int, step: int = 1) -> dict:
    return {"name": name, "label": label, "type": "int", "default": int(default), "min": int(lo), "max": int(hi), "step": int(step)}


def p_float(name: str, label: str, default: float, lo: float, hi: float, step: float = 0.1) -> dict:
    return {
        "name": name,
        "label": label,
        "type": "float",
        "default": float(default),
        "min": float(lo),
        "max": float(hi),
        "step": float(step),
    }


def p_bool(name: str, label: str, default: bool) -> dict:
    return {"name": name, "label": label, "type": "bool", "default": bool(default)}


def p_select(name: str, label: str, default: str, options: tuple[str, ...] | list[str]) -> dict:
    return {"name": name, "label": label, "type": "select", "default": str(default), "options": [str(o) for o in options]}


def p_source(default: str = "close", name: str = "source", label: str = "Fuente") -> dict:
    return {"name": name, "label": label, "type": "source", "default": default, "options": list(SOURCES)}


def p_len(default: int, lo: int = 1, hi: int = 500, name: str = "length", label: str = "Periodo") -> dict:
    return p_int(name, label, default, lo, hi)


def p_ma(default: str = "EMA", name: str = "ma_type", label: str = "Tipo de media") -> dict:
    return p_select(name, label, default, MA_TYPES)


def p_anchor(default: str = "Auto", name: str = "anchor", label: str = "Periodo de anclaje") -> dict:
    return p_select(name, label, default, ANCHORS)


def _out(key: str, title: str, color: str, kind: str) -> dict:
    return {"key": key, "title": title, "type": kind, "color": color}


def line(key: str, title: str, color: str) -> dict:
    return _out(key, title, color, "line")


def hist(key: str, title: str, color: str) -> dict:
    return _out(key, title, color, "histogram")


def area(key: str, title: str, color: str) -> dict:
    return _out(key, title, color, "area")


def dots(key: str, title: str, color: str) -> dict:
    return _out(key, title, color, "dots")


def step(key: str, title: str, color: str) -> dict:
    return _out(key, title, color, "step")


# ----------------------------------------------------------------------------- registry

IndicatorFn = Callable[["Bars", dict], dict]


@dataclass(frozen=True)
class Entry:
    spec: dict
    fn: IndicatorFn
    needs_volume: bool = False  # internal: refuse assets without volume data (kept out of the public spec)


REGISTRY: dict[str, Entry] = {}


def _check_spec(spec: dict) -> None:
    """Programming-error guard: a malformed spec must fail at import time, never at request time."""
    sid = spec["id"]
    if not sid or sid != sid.strip() or sid in REGISTRY:
        raise RuntimeError(f"indicator id inválido o duplicado: {sid!r}")
    if spec["category"] not in CATEGORIES:
        raise RuntimeError(f"{sid}: categoría desconocida {spec['category']!r}")
    names: set[str] = set()
    for p in spec["params"]:
        if p["type"] not in PARAM_TYPES or p["name"] in names:
            raise RuntimeError(f"{sid}: parámetro inválido {p!r}")
        names.add(p["name"])
        if p["type"] in ("int", "float") and not (p["min"] <= p["default"] <= p["max"]):
            raise RuntimeError(f"{sid}: valor por defecto fuera de rango en {p['name']}")
        if p["type"] in ("select", "source") and p["default"] not in p["options"]:
            raise RuntimeError(f"{sid}: valor por defecto no está entre las opciones de {p['name']}")
    keys: set[str] = set()
    if not spec["outputs"]:
        raise RuntimeError(f"{sid}: sin salidas")
    for o in spec["outputs"]:
        if o["type"] not in OUTPUT_TYPES or o["key"] in keys or len(o["color"]) != 7:
            raise RuntimeError(f"{sid}: salida inválida {o!r}")
        keys.add(o["key"])


def indicator(
    indicator_id: str,
    name: str,
    category: str,
    *,
    overlay: bool,
    description: str,
    params: list[dict],
    outputs: list[dict],
    levels: list[float] | None = None,
    range: tuple[float, float] | None = None,  # noqa: A002 - mirrors the IndicatorSpec field name
    needs_volume: bool = False,
) -> Callable[[IndicatorFn], IndicatorFn]:
    """Registers an indicator function under ``indicator_id`` with its IndicatorSpec."""
    spec: dict[str, Any] = {
        "id": indicator_id,
        "name": name,
        "category": category,
        "overlay": bool(overlay),
        "description": description,
        "params": params,
        "outputs": outputs,
    }
    if levels:
        spec["levels"] = [float(v) for v in levels]
    if range is not None:
        spec["range"] = [float(range[0]), float(range[1])]

    def deco(fn: IndicatorFn) -> IndicatorFn:
        _check_spec(spec)
        REGISTRY[indicator_id] = Entry(spec=spec, fn=fn, needs_volume=bool(needs_volume))
        return fn

    return deco


# --- end of section A (builders & registry) ---


# ----------------------------------------------------------------------------- bars view


class Bars:
    """Read-only OHLCV view handed to indicator functions, with cached derived price sources."""

    __slots__ = ("cs", "time", "open", "high", "low", "close", "volume", "n", "_cache")

    def __init__(self, cs: Any):
        self.cs = cs
        self.time = np.asarray(cs.time, dtype=np.int64)
        self.open = np.asarray(cs.open, dtype=np.float64)
        self.high = np.asarray(cs.high, dtype=np.float64)
        self.low = np.asarray(cs.low, dtype=np.float64)
        self.close = np.asarray(cs.close, dtype=np.float64)
        self.volume = ta.nz(np.asarray(cs.volume, dtype=np.float64))
        self.n = int(self.close.shape[0])
        self._cache: dict[str, Any] = {}

    def src(self, name: str) -> np.ndarray:
        """Price source by name (close/open/high/low/hl2/hlc3/ohlc4/hlcc4/volume)."""
        key = f"src:{name}"
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        o, h, lo, c = self.open, self.high, self.low, self.close
        if name == "close":
            out = c
        elif name == "open":
            out = o
        elif name == "high":
            out = h
        elif name == "low":
            out = lo
        elif name == "hl2":
            out = (h + lo) / 2.0
        elif name == "hlc3":
            out = (h + lo + c) / 3.0
        elif name == "ohlc4":
            out = (o + h + lo + c) / 4.0
        elif name == "hlcc4":
            out = (h + lo + 2.0 * c) / 4.0
        elif name == "volume":
            out = self.volume
        else:
            raise IndicatorError(f"Fuente de precio desconocida: «{name}»")
        self._cache[key] = out
        return out

    @property
    def hl2(self) -> np.ndarray:
        return self.src("hl2")

    @property
    def hlc3(self) -> np.ndarray:
        return self.src("hlc3")

    def tf_seconds(self) -> int:
        """Bar duration in seconds: from the CandleSet timeframe, else the median spacing of the bar times."""
        hit = self._cache.get("tf_s")
        if hit is not None:
            return hit
        sec = 0
        try:
            from app.util import tf_seconds

            sec = int(tf_seconds(str(getattr(self.cs, "tf", "") or "")))
        except (KeyError, ValueError, TypeError):
            sec = 0
        if sec <= 0:
            d = np.diff(self.time[: min(self.n, 200)])
            d = d[d > 0]
            sec = int(np.median(d)) if d.size else 3600
        self._cache["tf_s"] = sec
        return sec

    def periods_per_year(self, days_per_year: float) -> float:
        return max(1.0, float(days_per_year) * DAY_S / max(1, self.tf_seconds()))

    def anchor_ids(self, anchor: str, auto: str) -> np.ndarray:
        """Calendar period index of every bar (day / week / month / year, UTC); ``auto`` resolves "Auto"."""
        kind = auto if anchor == "Auto" else anchor
        key = f"anchor:{kind}"
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        t = self.time
        if kind == "Día":
            ids = t // DAY_S
        elif kind == "Semana":
            ids = (t - _MONDAY_OFFSET_S) // WEEK_S
        elif kind == "Mes":
            ids = t.astype("datetime64[s]").astype("datetime64[M]").astype(np.int64)
        elif kind == "Año":
            ids = t.astype("datetime64[s]").astype("datetime64[Y]").astype(np.int64)
        else:
            raise IndicatorError(f"Periodo de anclaje desconocido: «{anchor}»")
        ids = np.asarray(ids, dtype=np.int64)
        self._cache[key] = ids
        return ids


def auto_session_anchor(tf_s: int) -> str:
    """Anchor used by "Auto" for session-style indicators (VWAP, session range)."""
    if tf_s < DAY_S:
        return "Día"
    if tf_s < WEEK_S:
        return "Mes"
    return "Año"


def auto_pivot_anchor(tf_s: int) -> str:
    """TradingView-like "Auto" timeframe for pivot points: intraday <= 15m -> day, < 1D -> week, 1D -> month."""
    if tf_s <= 900:
        return "Día"
    if tf_s < DAY_S:
        return "Semana"
    if tf_s < WEEK_S:
        return "Mes"
    return "Año"


# ----------------------------------------------------------------------------- parameter coercion


def _fold(text: str) -> str:
    norm = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).strip().casefold()


_TRUE = {"true", "1", "si", "yes", "on", "verdadero"}
_FALSE = {"false", "0", "no", "off", "falso"}


def _bad(p: dict, expected: str) -> IndicatorError:
    return IndicatorError(f"Parámetro «{p['label']}» ({p['name']}) inválido: se esperaba {expected}")


def _to_number(p: dict, raw: Any) -> float:
    if isinstance(raw, bool):
        raise _bad(p, "un número")
    if isinstance(raw, (int, float, np.integer, np.floating)):
        val = float(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            val = float(raw.strip().replace(",", "."))
        except ValueError:
            raise _bad(p, "un número") from None
    else:
        raise _bad(p, "un número")
    if not math.isfinite(val):
        raise _bad(p, "un número finito")
    return val


def coerce_param(p: dict, raw: Any) -> Any:
    """Converts one raw value to the parameter's type; numbers are clamped to [min, max]."""
    kind = p["type"]
    if raw is None:
        return p["default"]
    if kind in ("int", "float"):
        val = _to_number(p, raw)
        if kind == "int":
            val = float(math.floor(val + 0.5))
        val = min(max(val, float(p["min"])), float(p["max"]))
        return int(val) if kind == "int" else val
    if kind == "bool":
        if isinstance(raw, (bool, np.bool_)):
            return bool(raw)
        if isinstance(raw, (int, float, np.integer, np.floating)) and float(raw) in (0.0, 1.0):
            return bool(raw)
        if isinstance(raw, str) and _fold(raw) in _TRUE | _FALSE:
            return _fold(raw) in _TRUE
        raise _bad(p, "verdadero o falso")
    text = _fold(raw) if isinstance(raw, str) else None
    for opt in p["options"]:
        if text is not None and _fold(opt) == text:
            return opt
    raise _bad(p, "uno de: " + ", ".join(p["options"]))


def coerce_params(spec: dict, params: Any) -> dict:
    """Validated parameters for ``spec`` (defaults for missing ones; unknown names are ignored)."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise IndicatorError("Los parámetros del indicador deben ser un objeto {nombre: valor}")
    return {p["name"]: coerce_param(p, params.get(p["name"])) for p in spec["params"]}


# --- end of section B (bars & params) ---


# ----------------------------------------------------------------------------- output finalization


def finalize(spec: dict, raw: Any, n: int) -> dict[str, np.ndarray]:
    """Float64 arrays of length ``n`` for every declared output; +-inf becomes NaN."""
    if not isinstance(raw, dict):
        raise IndicatorError(f"{spec['name']}: el cálculo no devolvió resultados")
    out: dict[str, np.ndarray] = {}
    for o in spec["outputs"]:
        key = o["key"]
        if key not in raw:
            raise IndicatorError(f"{spec['name']}: falta la salida «{o['title']}»")
        arr = np.asarray(raw[key], dtype=np.float64).reshape(-1)
        if arr.shape[0] != n:
            raise IndicatorError(f"{spec['name']}: la salida «{o['title']}» no está alineada con las velas")
        if not np.all(np.isfinite(arr) | np.isnan(arr)):
            arr = np.where(np.isinf(arr), NAN, arr)
        out[key] = arr
    return out


# --- end of indicators_core ---
