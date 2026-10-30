"""Drawing API of PyChart: ``color``, ``plot``, ``plotshape``, ``hline``, ``bgcolor``, ``alertcondition`` and the
Pine-style constant namespaces ``location`` and ``shape``. Outputs are stored in the ScriptContext and turned
into ScriptRunResult fields by ``runtime.collect_result``.
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np

from app.engine import ta as T

from .runtime_base import (
    MAX_ALERTS,
    MAX_HLINES,
    MAX_PLOTS,
    ScriptContext,
    ScriptRuntimeError,
    to_bool,
    to_float,
    to_int,
    to_series,
)

PINE_COLORS = {
    "green": "#4CAF50", "red": "#FF5252", "blue": "#2196F3", "orange": "#FF9800", "yellow": "#FFEB3B",
    "purple": "#9C27B0", "gray": "#787B86", "white": "#FFFFFF", "black": "#363A45", "teal": "#00897B",
    "aqua": "#00BCD4", "lime": "#00E676", "fuchsia": "#E040FB", "maroon": "#880E4F", "navy": "#311B92",
    "olive": "#808000", "silver": "#B2B5BE",
}
PLOT_PALETTE = ("#2962FF", "#FF6D00", "#00BFA5", "#D500F9", "#FFD600", "#F23645", "#089981", "#787B86")
PLOT_STYLES = {
    "line": "line", "linebr": "line", "histogram": "histogram", "columns": "columns", "area": "area",
    "areabr": "area", "step": "step", "stepline": "step", "stepline_diamond": "step", "circles": "circles",
    "cross": "circles",
}
LOCATIONS = {
    "abovebar": "aboveBar", "belowbar": "belowBar", "absolute": "inBar", "top": "aboveBar", "bottom": "belowBar",
    "inbar": "inBar",
}
SHAPES = {
    "arrowup": "arrowUp", "triangleup": "arrowUp", "labelup": "arrowUp", "arrowdown": "arrowDown",
    "triangledown": "arrowDown", "labeldown": "arrowDown", "circle": "circle", "xcross": "circle",
    "cross": "circle", "diamond": "circle", "flag": "circle", "square": "square",
}
LINE_STYLES = {"solid": "solid", "dashed": "dashed", "dotted": "dotted"}

_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB = re.compile(r"^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$", re.I)


def parse_color(value: str) -> tuple[int, int, int, float] | None:
    """(r, g, b, alpha 0..1) for '#rgb', '#rrggbb(aa)', 'rgb()/rgba()' or a Pine color name."""
    text = value.strip()
    named = PINE_COLORS.get(text.lower())
    if named:
        text = named
    m = _HEX.match(text)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0
        return r, g, b, a
    m = _RGB.match(text)
    if m:
        r, g, b = (min(255, int(round(float(m.group(i))))) for i in (1, 2, 3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return r, g, b, min(max(a, 0.0), 1.0)
    return None


def format_color(r: int, g: int, b: int, a: float = 1.0) -> str:
    if a >= 0.999:
        return f"#{r:02X}{g:02X}{b:02X}"
    return f"rgba({r}, {g}, {b}, {round(max(a, 0.0), 3)})"


def normalize_color(value: Any, default: str, what: str = "color") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        parsed = parse_color(value)
        if parsed:
            return format_color(*parsed)
    raise ScriptRuntimeError(f"{what}: color inválido {value!r}. Usa color.red, '#FF0000' o color.new(color.red, 50).")


def _color_list(value: Any, n: int, default: str, what: str) -> list[str | None]:
    """Per-bar colors: a single color (or None -> default) or a sequence of n colors / None."""
    if value is None or isinstance(value, str):
        return [normalize_color(value, default, what)] * n
    items = list(value) if isinstance(value, (list, tuple, np.ndarray)) else None
    if items is None or len(items) != n:
        raise ScriptRuntimeError(f"{what}: usa un color o una lista de {n} colores (uno por vela).")
    return [None if c is None else normalize_color(c, default, what) for c in items]


class ColorAPI:
    """``color`` namespace: Pine constants plus ``color.new(c, transp)`` and ``color.rgb(r, g, b, transp)``."""

    __slots__ = ()
    green = PINE_COLORS["green"]
    red = PINE_COLORS["red"]
    blue = PINE_COLORS["blue"]
    orange = PINE_COLORS["orange"]
    yellow = PINE_COLORS["yellow"]
    purple = PINE_COLORS["purple"]
    gray = PINE_COLORS["gray"]
    white = PINE_COLORS["white"]
    black = PINE_COLORS["black"]
    teal = PINE_COLORS["teal"]
    aqua = PINE_COLORS["aqua"]
    lime = PINE_COLORS["lime"]
    fuchsia = PINE_COLORS["fuchsia"]
    maroon = PINE_COLORS["maroon"]
    navy = PINE_COLORS["navy"]
    olive = PINE_COLORS["olive"]
    silver = PINE_COLORS["silver"]

    def new(self, color: Any, transp: Any = 0) -> str:
        """Same color with transparency 0 (opaque) .. 100 (invisible)."""
        parsed = parse_color(normalize_color(color, PINE_COLORS["blue"], "color.new"))
        t = min(max(to_float(transp, "color.new(transp)"), 0.0), 100.0)
        r, g, b, a = parsed  # type: ignore[misc]
        return format_color(r, g, b, a * (1.0 - t / 100.0))

    def rgb(self, red: Any, green: Any, blue: Any, transp: Any = 0) -> str:
        comps = [int(min(max(round(to_float(v, "color.rgb")), 0), 255)) for v in (red, green, blue)]
        t = min(max(to_float(transp, "color.rgb(transp)"), 0.0), 100.0)
        return format_color(comps[0], comps[1], comps[2], 1.0 - t / 100.0)

    def __repr__(self) -> str:
        return "<color>"


class LocationAPI:
    __slots__ = ()
    abovebar = "abovebar"
    belowbar = "belowbar"
    absolute = "absolute"
    top = "top"
    bottom = "bottom"


class ShapeAPI:
    __slots__ = ()
    arrowup = "arrowup"
    arrowdown = "arrowdown"
    triangleup = "triangleup"
    triangledown = "triangledown"
    labelup = "labelup"
    labeldown = "labeldown"
    circle = "circle"
    square = "square"
    xcross = "xcross"
    cross = "cross"
    diamond = "diamond"
    flag = "flag"


def _shifted(arr: np.ndarray, offset: int) -> np.ndarray:
    if offset > 0:
        return T.shift(arr, offset)
    if offset < 0:
        out = np.full(len(arr), np.nan)
        out[: len(arr) + offset] = arr[-offset:]
        return out
    return arr


class PlotAPI:
    """``plot(series, title, color, linewidth, style, pane, overlay)`` with Pine style constants."""

    __slots__ = ("_ctx",)
    style_line = "line"
    style_linebr = "linebr"
    style_stepline = "stepline"
    style_histogram = "histogram"
    style_columns = "columns"
    style_area = "area"
    style_areabr = "areabr"
    style_circles = "circles"
    style_cross = "cross"

    def __init__(self, ctx: ScriptContext) -> None:
        object.__setattr__(self, "_ctx", ctx)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("plot es de solo lectura")

    def __call__(
        self,
        series: Any,
        title: Any = None,
        color: Any = None,
        linewidth: Any = 1,
        style: Any = "line",
        pane: Any = None,
        overlay: Any = None,
        offset: Any = 0,
        show_last: Any = None,
        trackprice: Any = None,
        histbase: Any = None,
        join: Any = None,
        editable: Any = None,
        display: Any = None,
        transp: Any = None,
        precision: Any = None,
        format: Any = None,
        force_overlay: Any = None,
    ) -> None:
        ctx = self._ctx
        if len(ctx.plots) >= MAX_PLOTS:
            raise ScriptRuntimeError(f"Demasiados plot(): el máximo es {MAX_PLOTS} por script.")
        k = len(ctx.plots)
        name = str(title) if title is not None else f"Plot {k + 1}"
        data = to_series(series, ctx.n, f"plot('{name}')")
        data = _shifted(data, to_int(offset, "plot(offset)", minimum=-10_000, maximum=10_000))
        if show_last is not None:
            keep = to_int(show_last, "plot(show_last)", minimum=1, maximum=1_000_000)
            data[: max(ctx.n - keep, 0)] = np.nan
        style_key = str(style).lower() if style is not None else "line"
        if style_key not in PLOT_STYLES:
            raise ScriptRuntimeError(f"plot('{name}'): estilo desconocido {style!r}. Usa plot.style_line, plot.style_histogram…")
        col = normalize_color(color, PLOT_PALETTE[k % len(PLOT_PALETTE)], f"plot('{name}')")
        if transp is not None:
            col = ColorAPI().new(col, transp)
        ctx.plots.append(
            {
                "id": f"plot_{k}",
                "title": name[:80],
                "color": col,
                "linewidth": to_int(linewidth, "plot(linewidth)", minimum=1, maximum=10),
                "style": PLOT_STYLES[style_key],
                "overlay": None if overlay is None else bool(overlay),
                "pane": None if pane is None else to_int(pane, "plot(pane)", minimum=0, maximum=20),
                "data": data,
            }
        )
        return None


def _named(value: Any, table: dict[str, str]) -> bool:
    return isinstance(value, str) and value.lower() in table


def plotshape_factory(ctx: ScriptContext):
    def plotshape(
        series: Any,
        title: Any = None,
        location: Any = "abovebar",
        color: Any = None,
        shape: Any = None,
        text: Any = "",
        style: Any = None,
        size: Any = None,
        textcolor: Any = None,
        offset: Any = 0,
        editable: Any = None,
        show_last: Any = None,
        display: Any = None,
    ) -> None:
        """Markers on the bars where ``series`` is true."""
        name = str(title) if title is not None else "Señal"
        if _named(location, SHAPES) and (color is None or _named(color, LOCATIONS)):
            # Pine positional order plotshape(series, title, style, location, color)
            location, color, shape = (color if color is not None else "abovebar"), shape, location
        shape_key = str(shape if shape is not None else (style if style is not None else "circle")).lower()
        loc_key = str(location).lower() if location is not None else "abovebar"
        if loc_key not in LOCATIONS:
            raise ScriptRuntimeError(f"plotshape('{name}'): ubicación desconocida {location!r}. Usa location.abovebar o location.belowbar.")
        if shape_key not in SHAPES:
            raise ScriptRuntimeError(f"plotshape('{name}'): forma desconocida {shape_key!r}. Usa shape.arrowup, shape.circle…")
        mapped = SHAPES[shape_key]
        default = "#26A69A" if mapped == "arrowUp" else "#EF5350" if mapped == "arrowDown" else "#2962FF"
        cond = to_bool(series, ctx.n, f"plotshape('{name}')")
        off = to_int(offset, "plotshape(offset)", minimum=-10_000, maximum=10_000)
        if off:
            cond = _shifted(cond.astype(float), off) == 1.0
        if show_last is not None:
            keep = to_int(show_last, "plotshape(show_last)", minimum=1, maximum=1_000_000)
            cond[: max(ctx.n - keep, 0)] = False
        colors = _color_list(color, ctx.n, default, f"plotshape('{name}')")
        label = "" if text is None else str(text)[:40]
        times = ctx.bars.time
        for i in np.flatnonzero(cond).tolist():
            if colors[i] is None:
                continue
            ctx.markers.append(
                {
                    "time": int(times[i]),
                    "kind": "SCRIPT",
                    "position": LOCATIONS[loc_key],
                    "shape": mapped,
                    "color": colors[i],
                    "text": label,
                    "tooltip": name[:80],
                }
            )
        return None

    return plotshape


class HLineAPI:
    """``hline(price, title, color, linestyle)`` with ``hline.style_solid/dashed/dotted``."""

    __slots__ = ("_ctx",)
    style_solid = "solid"
    style_dashed = "dashed"
    style_dotted = "dotted"

    def __init__(self, ctx: ScriptContext) -> None:
        object.__setattr__(self, "_ctx", ctx)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("hline es de solo lectura")

    def __call__(
        self,
        price: Any,
        title: Any = None,
        color: Any = None,
        linestyle: Any = None,
        linewidth: Any = 1,
        style: Any = None,
        editable: Any = None,
        display: Any = None,
    ) -> None:
        ctx = self._ctx
        if len(ctx.hlines) >= MAX_HLINES:
            raise ScriptRuntimeError(f"Demasiadas hline(): el máximo es {MAX_HLINES}.")
        value = to_float(price, "hline(price)", allow_nan=True)
        key = str(linestyle if linestyle is not None else (style if style is not None else "dashed")).lower()
        if key not in LINE_STYLES:
            raise ScriptRuntimeError(f"hline: estilo desconocido {key!r}. Usa hline.style_solid, style_dashed o style_dotted.")
        if value == value and abs(value) != float("inf"):
            ctx.hlines.append(
                {
                    "price": value,
                    "title": "" if title is None else str(title)[:80],
                    "color": normalize_color(color, PINE_COLORS["gray"], "hline(color)"),
                    "style": LINE_STYLES[key],
                }
            )
        return None


def bgcolor_factory(ctx: ScriptContext):
    def bgcolor(
        condition: Any,
        color: Any = None,
        title: Any = None,
        offset: Any = 0,
        editable: Any = None,
        show_last: Any = None,
        display: Any = None,
    ) -> None:
        """Background color on the bars where ``condition`` is true (or a color / list of colors per bar)."""
        n = ctx.n
        if isinstance(condition, str) and color is None:
            condition, color = True, condition
        if isinstance(condition, np.ndarray):
            is_color_seq = condition.dtype == object and any(isinstance(c, str) for c in condition.tolist())
        else:
            is_color_seq = isinstance(condition, (list, tuple)) and any(isinstance(c, str) for c in condition)
        if is_color_seq and color is None:
            colors = _color_list(condition, n, "rgba(41, 98, 255, 0.15)", "bgcolor")
            cond = np.array([c is not None for c in colors], dtype=bool)
        else:
            cond = to_bool(condition, n, "bgcolor(condition)")
            colors = _color_list(color, n, "rgba(41, 98, 255, 0.15)", "bgcolor(color)")
        off = to_int(offset, "bgcolor(offset)", minimum=-10_000, maximum=10_000)
        idx = np.flatnonzero(cond)
        if show_last is not None:
            keep = to_int(show_last, "bgcolor(show_last)", minimum=1, maximum=1_000_000)
            idx = idx[idx >= n - keep]
        for i in idx.tolist():
            j = i + off
            if 0 <= j < n and colors[i] is not None:
                ctx.bgcolors[j] = colors[i]  # type: ignore[assignment]
        return None

    return bgcolor


def alertcondition_factory(ctx: ScriptContext):
    def alertcondition(condition: Any, title: Any = None, message: Any = None) -> None:
        """Registers an alert on every bar where ``condition`` is true (the last 200 are reported)."""
        name = "Alerta" if title is None else str(title)[:80]
        msg = name if message is None else str(message)[:300]
        cond = to_bool(condition, ctx.n, f"alertcondition('{name}')")
        hits = np.flatnonzero(cond)[-MAX_ALERTS:]
        ctx.alerts.extend((int(i), name, msg) for i in hits.tolist())
        return None

    return alertcondition
