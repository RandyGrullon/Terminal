"""NEXUS indicators (category NEXUS): per-bar score and probability of the adaptive engine as chart series.

``app.engine.nexus`` is imported lazily inside the computation, so the catalog always loads; when the engine is
missing or broken the computation raises ``IndicatorError("Motor NEXUS no disponible todavía")``. The engine runs
causally over the requested bars (no stored memory); one run feeds both indicators through a small cache.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

import numpy as np

from app.engine import ta
from app.engine.indicators_core import (
    NAN,
    NEXUS,
    POS,
    PURPLE,
    WARN,
    Bars,
    IndicatorError,
    hist,
    indicator,
    line,
    p_int,
)

_CACHE_MAX = 8
_CACHE: OrderedDict[tuple, tuple[np.ndarray, np.ndarray]] = OrderedDict()
_LOCK = threading.Lock()


def _load_engine() -> tuple[Any, Any]:
    try:
        from app.engine.nexus import NexusParams, run_nexus
    except Exception as exc:  # missing module, half-written file, renamed symbols...
        raise IndicatorError("Motor NEXUS no disponible todavía") from exc
    return NexusParams, run_nexus


def _make_params(nexus_params_cls: Any) -> Any:
    """Engine parameters from the user's settings when the runtime is up, else the engine defaults."""
    settings = None
    try:
        from app.runtime import rt

        store = getattr(rt, "settings", None)
        settings = store.all() if store is not None else None
    except Exception:
        settings = None
    if isinstance(settings, dict) and hasattr(nexus_params_cls, "from_settings"):
        try:
            return nexus_params_cls.from_settings(settings)
        except Exception:
            pass
    try:
        return nexus_params_cls()
    except Exception as exc:
        raise IndicatorError("Motor NEXUS no disponible todavía") from exc


def _columns(bars: Any) -> dict | None:
    """Normalizes the engine's per-bar output (columnar dict, list of rows or object) to a column dict."""
    if bars is None:
        return None
    if isinstance(bars, dict):
        return bars if "score" in bars and "prob_up" in bars else None
    if isinstance(bars, (list, tuple)):
        if bars and isinstance(bars[0], dict) and "score" in bars[0]:
            keys = ("time", "score", "prob_up")
            return {k: [row.get(k) for row in bars] for k in keys if k in bars[0]}
        return None
    if hasattr(bars, "score") and hasattr(bars, "prob_up"):
        cols = {"score": getattr(bars, "score"), "prob_up": getattr(bars, "prob_up")}
        if hasattr(bars, "time"):
            cols["time"] = getattr(bars, "time")
        return cols
    return None


def _as_float(values: Any) -> np.ndarray:
    try:
        return np.asarray(values, dtype=float).reshape(-1)  # None -> NaN
    except (TypeError, ValueError):
        out = []
        for v in list(values):
            try:
                out.append(NAN if v is None else float(v))
            except (TypeError, ValueError):
                out.append(NAN)
        return np.asarray(out, dtype=float)


def _align(cols: dict, key: str, b: Bars) -> np.ndarray:
    """Engine column aligned with the bars: by bar time when available, else right-aligned by position."""
    vals = _as_float(cols[key])
    out = np.full(b.n, NAN)
    times = cols.get("time")
    if times is not None:
        t = _as_float(times)
        if t.shape[0] == vals.shape[0]:
            ok = np.isfinite(t)
            idx = np.searchsorted(b.time, t[ok].astype(np.int64))
            vv = vals[ok]
            hit = (idx < b.n) & (b.time[np.minimum(idx, b.n - 1)] == t[ok].astype(np.int64))
            out[idx[hit]] = vv[hit]
            return out
    k = min(b.n, vals.shape[0])
    if k:
        out[b.n - k :] = vals[vals.shape[0] - k :]
    return out


def _series(b: Bars) -> tuple[np.ndarray, np.ndarray]:
    """(score in [-1, 1], prob_up in [0, 1]) per bar, cached by candle content and engine parameters."""
    params_cls, run_nexus = _load_engine()
    params = _make_params(params_cls)
    key = (
        str(getattr(b.cs, "symbol", "")),
        str(getattr(b.cs, "tf", "")),
        b.n,
        int(b.time[0]),
        int(b.time[-1]),
        float(np.nansum(b.close)),
        float(np.nansum(b.volume)),
        repr(params),
    )
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    try:
        res = run_nexus(b.cs, params, None)
    except IndicatorError:
        raise
    except ImportError as exc:  # the engine lazily imports sub-modules that may not exist yet
        raise IndicatorError("Motor NEXUS no disponible todavía") from exc
    except Exception as exc:
        raise IndicatorError(f"El motor NEXUS falló al calcular la serie: {exc}") from exc
    cols = _columns(getattr(res, "bars", None))
    if cols is None and hasattr(res, "to_response"):
        try:
            cols = _columns((res.to_response(b.cs) or {}).get("bars"))
        except Exception:
            cols = None
    if cols is None:
        raise IndicatorError("El motor NEXUS no devolvió series por vela")
    score = np.clip(_align(cols, "score", b), -1.0, 1.0)
    prob = np.clip(_align(cols, "prob_up", b), 0.0, 1.0)
    with _LOCK:
        _CACHE[key] = (score, prob)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return score, prob


@indicator(
    "nexus_score",
    "Score NEXUS",
    NEXUS,
    overlay=False,
    description="Puntuación del motor adaptativo NEXUS (−1..1): voto ponderado de los expertos + probabilidad "
    "aprendida. Por encima de +0,35 hay candidata a compra y por debajo de −0,35 a venta (siempre sujetas al "
    "filtro de operaciones). Calculado sobre las velas cargadas, sin memoria previa.",
    params=[p_int("smooth", "Suavizado de la señal", 5, 1, 50)],
    outputs=[hist("score", "Score", POS), line("signal", "Score suavizado", WARN)],
    levels=[-0.35, 0.0, 0.35],
    range=(-1.0, 1.0),
)
def _nexus_score(b: Bars, p: dict) -> dict:
    score, _ = _series(b)
    return {"score": score, "signal": ta.ema(score, p["smooth"])}


@indicator(
    "nexus_prob",
    "Probabilidad NEXUS (P↑ %)",
    NEXUS,
    overlay=False,
    description="Probabilidad calibrada (regresión logística online) de que el precio suba en el horizonte del "
    "motor. Cerca de 50 % = sin ventaja; es una estimación, no una certeza.",
    params=[p_int("smooth", "Suavizado", 1, 1, 50)],
    outputs=[line("prob", "P↑ %", PURPLE)],
    levels=[40.0, 50.0, 60.0],
    range=(0.0, 100.0),
)
def _nexus_prob(b: Bars, p: dict) -> dict:
    _, prob = _series(b)
    pct = 100.0 * prob
    return {"prob": pct if p["smooth"] <= 1 else ta.ema(pct, p["smooth"])}
