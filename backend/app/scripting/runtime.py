"""PyChart runtime: the Pine-like namespace a script runs in, and the conversion of everything the script drew
into a ``ScriptRunResult`` (frontend/src/api/types.ts).

Used by the sandbox worker (``app/scripting/worker.py``):

    ctx = ScriptContext(Bars(...), inputs, symbol=..., tf=..., stdout=buffer)
    namespace = build_namespace(ctx)            # series, ta, input, plot, strategy, color, ...
    exec(code, {**namespace, "__builtins__": safe_builtins, ...})
    result = collect_result(ctx)                # plots, markers, hlines, bgcolors, alerts, strategy

Series (``open high low close volume time hl2 hlc3 ohlc4 bar_index``) are numpy arrays aligned with the bars;
every API call is vectorized. See runtime_ta / runtime_plot / runtime_strategy for the namespaces.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .runtime_backtest import simulate
from .runtime_base import (
    MAX_ALERTS,
    MAX_BGCOLORS,
    MAX_MARKERS,
    NUMBER_TYPES,
    Bars,
    OutputBuffer,
    ScriptContext,
    ScriptRuntimeError,
    clean_value,
    json_series,
    make_print,
    to_series,
)
from .runtime_metrics import bar_seconds
from .runtime_news import NewsAPI
from .runtime_plot import (
    ColorAPI,
    HLineAPI,
    LocationAPI,
    PlotAPI,
    ShapeAPI,
    alertcondition_factory,
    bgcolor_factory,
    plotshape_factory,
)
from .runtime_strategy import OrderRules, StrategyAPI
from .runtime_ta import TA
from .validator import DEFAULT_TITLE, SOURCE_NAMES, coerce_input_value

__all__ = [
    "Bars",
    "OutputBuffer",
    "ScriptContext",
    "ScriptRuntimeError",
    "build_namespace",
    "collect_result",
    "make_print",
]


class ReadOnlyNamespace:
    """Small attribute namespace (``syminfo``, ``timeframe``) that scripts cannot modify."""

    __slots__ = ("_label", "_values")

    def __init__(self, label: str, **values: Any) -> None:
        object.__setattr__(self, "_label", label)
        object.__setattr__(self, "_values", dict(values))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(f"{self._label}.{name} no existe") from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{self._label} es de solo lectura")

    def __repr__(self) -> str:
        return f"<{self._label}>"


def _in_options(kind: str, value: Any, options: list) -> bool:
    if kind in ("int", "float"):
        try:
            return any(abs(float(o) - float(value)) < 1e-12 for o in options)
        except (TypeError, ValueError):
            return False
    return str(value) in [str(o) for o in options]


class InputAPI:
    """``input.int/float/bool/string/select/source(defval, title, ...)`` and ``input(defval, title)``.

    Overrides arrive in ``ctx.inputs`` keyed by the name the compiler reported (the worker injects it as the
    hidden ``_nexus_key`` argument); values are coerced to the input type and clamped to ``minval``/``maxval``.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: ScriptContext) -> None:
        object.__setattr__(self, "_ctx", ctx)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("input es de solo lectura")

    def __repr__(self) -> str:
        return "<input>"

    def _resolve(self, kind: str, defval: Any, title: Any, key: Any, *, minval: Any = None, maxval: Any = None, options: Any = None) -> Any:
        ctx = self._ctx
        ctx.input_count += 1
        label = str(title) if title is not None else str(key or f"input_{ctx.input_count}")
        default, ok = coerce_input_value(kind, defval)
        if not ok:
            raise ScriptRuntimeError(f"input.{kind}('{label}'): el valor por defecto {defval!r} no es de tipo {kind}.")
        opts = list(options) if isinstance(options, (list, tuple)) and options else None
        if opts is not None and not _in_options(kind, default, opts):
            raise ScriptRuntimeError(f"input.{kind}('{label}'): el valor por defecto debe estar en options.")
        value = default
        name = str(key) if key else label
        if name in ctx.inputs:
            raw, ok = coerce_input_value(kind, ctx.inputs[name])
            if ok and (opts is None or _in_options(kind, raw, opts)):
                value = raw
            else:
                ctx.notice(f"valor inválido para la entrada '{label}'; se usa el valor por defecto.")
        if kind in ("int", "float"):
            if minval is not None and value < float(minval):
                value = float(minval)
            if maxval is not None and value > float(maxval):
                value = float(maxval)
            value = int(round(value)) if kind == "int" else float(value)
        return value

    def int(self, defval: Any, title: Any = None, minval: Any = None, maxval: Any = None, step: Any = None,
            options: Any = None, tooltip: Any = None, group: Any = None, inline: Any = None, confirm: Any = None,
            display: Any = None, _nexus_key: Any = None) -> int:
        return self._resolve("int", defval, title, _nexus_key, minval=minval, maxval=maxval, options=options)

    def float(self, defval: Any, title: Any = None, minval: Any = None, maxval: Any = None, step: Any = None,
              options: Any = None, tooltip: Any = None, group: Any = None, inline: Any = None, confirm: Any = None,
              display: Any = None, _nexus_key: Any = None) -> float:
        return self._resolve("float", defval, title, _nexus_key, minval=minval, maxval=maxval, options=options)

    def bool(self, defval: Any, title: Any = None, tooltip: Any = None, group: Any = None, inline: Any = None,
             confirm: Any = None, display: Any = None, _nexus_key: Any = None) -> bool:
        return self._resolve("bool", defval, title, _nexus_key)

    def string(self, defval: Any, title: Any = None, options: Any = None, tooltip: Any = None, group: Any = None,
               inline: Any = None, confirm: Any = None, display: Any = None, _nexus_key: Any = None) -> str:
        return self._resolve("string", defval, title, _nexus_key, options=options)

    def select(self, defval: Any, title: Any = None, options: Any = None, tooltip: Any = None, group: Any = None,
               inline: Any = None, confirm: Any = None, display: Any = None, _nexus_key: Any = None) -> str:
        if not isinstance(options, (list, tuple)) or not options:
            raise ScriptRuntimeError("input.select: indica la lista de opciones, p. ej. options=['EMA', 'SMA'].")
        return self._resolve("select", defval, title, _nexus_key, options=options)

    def source(self, defval: Any = None, title: Any = None, tooltip: Any = None, group: Any = None, inline: Any = None,
               confirm: Any = None, display: Any = None, _nexus_key: Any = None) -> np.ndarray:
        ctx = self._ctx
        ctx.input_count += 1
        name = str(_nexus_key) if _nexus_key else (str(title) if title is not None else f"input_{ctx.input_count}")
        override = ctx.inputs.get(name)
        if isinstance(override, str) and override in SOURCE_NAMES:
            return ctx.bars.source(override)
        if override is not None:
            ctx.notice(f"fuente inválida para la entrada '{title or name}'; se usa el valor por defecto.")
        if defval is None:
            return ctx.bars.source("close")
        if isinstance(defval, str):
            return ctx.bars.source(defval)
        return to_series(defval, ctx.n, f"input.source('{title or name}')")

    def __call__(self, defval: Any, title: Any = None, tooltip: Any = None, group: Any = None, inline: Any = None,
                 confirm: Any = None, display: Any = None, _nexus_key: Any = None) -> Any:
        if isinstance(defval, np.ndarray):
            return self.source(defval, title, _nexus_key=_nexus_key)
        if isinstance(defval, (bool, np.bool_)):
            return self.bool(bool(defval), title, _nexus_key=_nexus_key)
        if isinstance(defval, (int, np.integer)):
            return self.int(int(defval), title, _nexus_key=_nexus_key)
        if isinstance(defval, (float, np.floating)):
            return self.float(float(defval), title, _nexus_key=_nexus_key)
        return self.string(defval, title, _nexus_key=_nexus_key)


# --- end of section A: inputs ---


# ============================================================================= na helpers


def na(value: Any) -> Any:
    """True where the value is missing (NaN / None); element-wise for series."""
    if value is None:
        return True
    if isinstance(value, NUMBER_TYPES):
        return isinstance(value, (float, np.floating)) and math.isnan(value)
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ScriptRuntimeError("na(): se esperaba un número o una serie numérica.") from exc
    return np.isnan(arr) if arr.ndim else bool(np.isnan(arr))


def nz(value: Any, replacement: Any = 0.0) -> Any:
    """Replaces missing values (NaN / None) with ``replacement`` (0 by default)."""
    if value is None:
        return replacement
    if isinstance(value, NUMBER_TYPES):
        return replacement if isinstance(value, (float, np.floating)) and math.isnan(value) else value
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ScriptRuntimeError("nz(): se esperaba un número o una serie numérica.") from exc
    return np.where(np.isnan(arr), replacement, arr)


def fixnan(value: Any) -> np.ndarray:
    """Replaces NaN with the last non-NaN value (forward fill); leading NaN stay NaN."""
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ScriptRuntimeError("fixnan(): se esperaba una serie numérica.") from exc
    if arr.ndim != 1:
        raise ScriptRuntimeError("fixnan(): se esperaba una serie de una dimensión.")
    idx = np.where(np.isnan(arr), 0, np.arange(len(arr)))
    np.maximum.accumulate(idx, out=idx)
    out = arr[idx]
    if len(arr) and np.isnan(arr[0]):
        first = np.flatnonzero(~np.isnan(arr))
        out[: first[0] if first.size else len(arr)] = np.nan
    return out


# ============================================================================= namespace


def _declarer(ctx: ScriptContext):
    def declare(kind: str, title: Any, overlay: Any) -> bool:
        if ctx.declared:
            ctx.notice("solo se admite una declaración indicator()/strategy(); se ignoran las siguientes.")
            return False
        text = str(title).strip() if title is not None else ""
        ctx.declared = True
        ctx.meta = {"title": text[:120] or DEFAULT_TITLE, "overlay": bool(overlay), "kind": kind}
        return True

    return declare


def build_namespace(ctx: ScriptContext) -> dict[str, Any]:
    """Names available to the script (the worker adds ``np``, ``math``, ``print`` and the safe builtins)."""
    b = ctx.bars
    declare = _declarer(ctx)
    ctx.strategy_rules = OrderRules()

    def indicator(title: Any = DEFAULT_TITLE, overlay: Any = True, shorttitle: Any = None, format: Any = None,
                  precision: Any = None, scale: Any = None, max_bars_back: Any = None, timeframe: Any = None,
                  timeframe_gaps: Any = None, explicit_plot_zorder: Any = None, max_lines_count: Any = None,
                  max_labels_count: Any = None, max_boxes_count: Any = None, behind_chart: Any = None) -> None:
        """Declares the script as an indicator (title shown on the chart; overlay=True draws on the price pane)."""
        declare("indicator", title, overlay)
        return None

    symbol = ctx.symbol
    prefix, _, ticker = symbol.rpartition(":")
    seconds = int(bar_seconds(b.time)) if b.n >= 2 else 0
    hl2 = (b.high + b.low) / 2.0
    return {
        "open": b.open.copy(),
        "high": b.high.copy(),
        "low": b.low.copy(),
        "close": b.close.copy(),
        "volume": b.volume.copy(),
        "time": b.time.copy(),
        "hl2": hl2,
        "hlc3": (b.high + b.low + b.close) / 3.0,
        "ohlc4": (b.open + b.high + b.low + b.close) / 4.0,
        "bar_index": np.arange(b.n, dtype=np.int64),
        "last_bar_index": b.n - 1,
        "nan": float("nan"),
        "na": na,
        "nz": nz,
        "fixnan": fixnan,
        "indicator": indicator,
        "strategy": StrategyAPI(ctx, declare),
        "input": InputAPI(ctx),
        "ta": TA(b),
        "color": ColorAPI(),
        "plot": PlotAPI(ctx),
        "plotshape": plotshape_factory(ctx),
        "hline": HLineAPI(ctx),
        "bgcolor": bgcolor_factory(ctx),
        "alertcondition": alertcondition_factory(ctx),
        "location": LocationAPI(),
        "shape": ShapeAPI(),
        "noticias": NewsAPI(ctx),
        "syminfo": ReadOnlyNamespace("syminfo", tickerid=symbol, ticker=ticker or symbol, prefix=prefix),
        "timeframe": ReadOnlyNamespace(
            "timeframe",
            period=ctx.tf,
            seconds=seconds,
            isintraday=0 < seconds < 86400,
            isdaily=seconds == 86400,
            isweekly=seconds == 7 * 86400,
        ),
    }


# ============================================================================= result


def _resolve_pane(plot: dict, default_overlay: bool) -> tuple[bool, int]:
    pane, overlay = plot["pane"], plot["overlay"]
    if pane is not None:
        return pane == 0, int(pane)
    overlay = default_overlay if overlay is None else bool(overlay)
    return overlay, 0 if overlay else 1


def collect_result(ctx: ScriptContext) -> dict[str, Any]:
    """ScriptRunResult fields produced by the script (``candles`` and ``duration_ms`` are filled by the
    sandbox, which already has the candles)."""
    times = ctx.bars.time
    t_list = times.tolist()
    plots = []
    for p in ctx.plots:
        overlay, pane = _resolve_pane(p, bool(ctx.meta.get("overlay", True)))
        plots.append(dict(p, overlay=overlay, pane=pane, data=json_series(p["data"])))
    markers = sorted(ctx.markers, key=lambda m: m["time"])
    if len(markers) > MAX_MARKERS:
        ctx.notice(f"se muestran solo los últimos {MAX_MARKERS} marcadores de plotshape().")
        markers = markers[-MAX_MARKERS:]
    bg_idx = sorted(ctx.bgcolors)
    if len(bg_idx) > MAX_BGCOLORS:
        ctx.notice(f"bgcolor(): se muestran solo las últimas {MAX_BGCOLORS} velas coloreadas.")
        bg_idx = bg_idx[-MAX_BGCOLORS:]
    alerts = sorted(ctx.alerts, key=lambda a: a[0])[-MAX_ALERTS:]
    strategy = None
    if ctx.strategy_config is not None:
        strategy = simulate(
            ctx.bars,
            ctx.strategy_rules,
            ctx.strategy_config,
            symbol=ctx.symbol,
            tf=ctx.tf,
            title=str(ctx.meta.get("title", "")),
        )
    result = clean_value(
        {
            "ok": True,
            "errors": [],
            "stdout": "",
            "duration_ms": 0,
            "meta": dict(ctx.meta),
            "time": t_list,
            "plots": [],
            "markers": markers,
            "hlines": list(ctx.hlines),
            "bgcolors": [{"time": t_list[i], "color": ctx.bgcolors[i]} for i in bg_idx],
            "alerts": [{"time": t_list[i], "title": title, "message": msg} for i, title, msg in alerts],
            "strategy": strategy,
            "candles": [],
        }
    )
    result["plots"] = plots  # already JSON-safe (json_series)
    result["stdout"] = ctx.stdout.getvalue()
    return result
