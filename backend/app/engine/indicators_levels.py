"""Support / resistance indicators (category Soportes/Resistencias).

Swing-based levels (ZigZag, fractals, pivots high/low) are drawn from the bar where the swing is CONFIRMED,
never from the swing bar itself: that is what keeps them causal (they do not repaint).
"""
from __future__ import annotations

from collections import deque

import numpy as np

from app.engine import ta
from app.engine.indicators_core import (
    DAY_S,
    INFO,
    LEVELS,
    MUTED,
    NAN,
    NEG,
    POS,
    PURPLE,
    ROSE,
    WARN,
    WEEK_S,
    Bars,
    auto_pivot_anchor,
    auto_session_anchor,
    dots,
    group_running,
    groups_of,
    hold_events,
    indicator,
    line,
    p_anchor,
    p_float,
    p_int,
    p_len,
    p_select,
    previous_group_ohlc,
    step,
)

_R_COLORS = ("#fda4af", "#fb7185", "#ea3943")  # R1 (light) .. R3 (strong)
_S_COLORS = ("#86efac", "#4ade80", "#16c784")  # S1 .. S3
PIVOT_KINDS = ("Clásico", "Fibonacci", "Camarilla", "Woodie", "DeMark")


@indicator(
    "pivots",
    "Puntos pivote (clásico / Fibonacci / Camarilla)",
    LEVELS,
    overlay=True,
    description="Niveles calculados con el máximo, mínimo y cierre del periodo anterior (día, semana, mes...). "
    "P es el equilibrio; R1–R3 resistencias y S1–S3 soportes. DeMark solo define P, R1 y S1.",
    params=[p_select("kind", "Tipo", "Clásico", PIVOT_KINDS), p_anchor("Auto", label="Periodo")],
    outputs=[
        step("r3", "R3", _R_COLORS[2]),
        step("r2", "R2", _R_COLORS[1]),
        step("r1", "R1", _R_COLORS[0]),
        step("p", "P", WARN),
        step("s1", "S1", _S_COLORS[0]),
        step("s2", "S2", _S_COLORS[1]),
        step("s3", "S3", _S_COLORS[2]),
    ],
)
def _pivots(b: Bars, p: dict) -> dict:
    g = groups_of(b.anchor_ids(p["anchor"], auto_pivot_anchor(b.tf_seconds())))
    prev = previous_group_ohlc(b, g)
    o, h, lo, c = prev["open"], prev["high"], prev["low"], prev["close"]
    rng = h - lo
    nan = np.full(b.n, NAN)
    kind = p["kind"]
    if kind == "DeMark":
        x = np.where(c < o, h + 2.0 * lo + c, np.where(c > o, 2.0 * h + lo + c, h + lo + 2.0 * c))
        return {"p": x / 4.0, "r1": x / 2.0 - lo, "s1": x / 2.0 - h, "r2": nan, "r3": nan, "s2": nan, "s3": nan}
    if kind == "Camarilla":
        k = 1.1 * rng
        pp = (h + lo + c) / 3.0
        return {
            "p": pp,
            "r1": c + k / 12.0, "r2": c + k / 6.0, "r3": c + k / 4.0,
            "s1": c - k / 12.0, "s2": c - k / 6.0, "s3": c - k / 4.0,
        }
    if kind == "Fibonacci":
        pp = (h + lo + c) / 3.0
        return {
            "p": pp,
            "r1": pp + 0.382 * rng, "r2": pp + 0.618 * rng, "r3": pp + rng,
            "s1": pp - 0.382 * rng, "s2": pp - 0.618 * rng, "s3": pp - rng,
        }
    pp = (h + lo + 2.0 * c) / 4.0 if kind == "Woodie" else (h + lo + c) / 3.0
    return {
        "p": pp,
        "r1": 2.0 * pp - lo, "r2": pp + rng, "r3": h + 2.0 * (pp - lo),
        "s1": 2.0 * pp - h, "s2": pp - rng, "s3": lo - 2.0 * (h - pp),
    }


@indicator(
    "zigzag",
    "ZigZag (giros confirmados)",
    LEVELS,
    overlay=True,
    description="Un giro se confirma cuando el precio retrocede más de X% desde el extremo del tramo. Muestra el "
    "último máximo y mínimo de giro confirmados (sin redibujar el pasado) y el extremo del tramo en curso.",
    params=[p_float("deviation", "Desviación mínima (%)", 3.0, 0.1, 50.0, 0.1)],
    outputs=[
        step("high", "Último máximo de giro", NEG),
        step("low", "Último mínimo de giro", POS),
        dots("ext", "Extremo del tramo en curso", WARN),
    ],
)
def _zigzag(b: Bars, p: dict) -> dict:
    dev = p["deviation"] / 100.0
    hs, ls = b.high.tolist(), b.low.tolist()
    m = len(hs)
    out_hi, out_lo, out_ext = [NAN] * m, [NAN] * m, [NAN] * m
    direction = 0
    hi = lo = ext = NAN
    ph = pl = NAN
    for i in range(m):
        hv, lv = hs[i], ls[i]
        if direction == 0:
            if i == 0:
                hi, lo = hv, lv
            elif hv >= lo * (1.0 + dev):
                pl, direction, ext = lo, 1, hv
            elif lv <= hi * (1.0 - dev):
                ph, direction, ext = hi, -1, lv
            else:
                hi, lo = max(hi, hv), min(lo, lv)
        elif direction == 1:
            if hv > ext:
                ext = hv
            elif lv <= ext * (1.0 - dev):
                ph, direction, ext = ext, -1, lv
        else:
            if lv < ext:
                ext = lv
            elif hv >= ext * (1.0 + dev):
                pl, direction, ext = ext, 1, hv
        out_hi[i], out_lo[i] = ph, pl
        out_ext[i] = ext if direction != 0 else NAN
    return {"high": np.array(out_hi), "low": np.array(out_lo), "ext": np.array(out_ext)}


def _swing_levels(b: Bars, left: int, right: int) -> tuple[np.ndarray, np.ndarray]:
    piv = ta.pivots(b.high, b.low, left, right)
    return (
        hold_events(b.n, piv["hi_conf"], piv["hi_price"]),
        hold_events(b.n, piv["lo_conf"], piv["lo_price"]),
    )


@indicator(
    "fractals",
    "Fractales de Williams",
    LEVELS,
    overlay=True,
    description="Máximo (mínimo) con N velas más bajas (altas) a cada lado. Se marca al confirmarse, N velas "
    "después; romper el último fractal superior es la entrada clásica de Bill Williams.",
    params=[p_int("period", "Velas a cada lado", 2, 1, 10)],
    outputs=[dots("up", "Fractal superior", NEG), dots("down", "Fractal inferior", POS)],
)
def _fractals(b: Bars, p: dict) -> dict:
    up, down = _swing_levels(b, p["period"], p["period"])
    return {"up": up, "down": down}


@indicator(
    "swing_hl",
    "Máximos y mínimos de giro (pivotes)",
    LEVELS,
    overlay=True,
    description="Último máximo y mínimo de giro confirmados con N velas a la izquierda y M a la derecha: "
    "resistencia y soporte estructurales.",
    params=[p_int("left", "Velas a la izquierda", 10, 1, 100), p_int("right", "Velas a la derecha", 10, 1, 100)],
    outputs=[step("high", "Máximo de giro", NEG), step("low", "Mínimo de giro", POS)],
)
def _swing_hl(b: Bars, p: dict) -> dict:
    hi, lo = _swing_levels(b, p["left"], p["right"])
    return {"high": hi, "low": lo}


@indicator(
    "sr_levels",
    "Soporte y resistencia más cercanos",
    LEVELS,
    overlay=True,
    description="De los últimos giros confirmados (máximos y mínimos), el nivel más próximo por encima del cierre "
    "(resistencia) y por debajo (soporte). Es la «distancia al objetivo» que vigila el filtro de operaciones.",
    params=[
        p_int("left", "Velas a la izquierda", 5, 1, 50),
        p_int("right", "Velas a la derecha", 5, 1, 50),
        p_int("max_levels", "Giros recordados", 30, 2, 200),
    ],
    outputs=[step("resistance", "Resistencia", NEG), step("support", "Soporte", POS)],
)
def _sr_levels(b: Bars, p: dict) -> dict:
    piv = ta.pivots(b.high, b.low, p["left"], p["right"])
    conf = np.concatenate((piv["hi_conf"], piv["lo_conf"]))
    price = np.concatenate((piv["hi_price"], piv["lo_price"]))
    order = np.argsort(conf, kind="stable")
    conf_l, price_l = conf[order].tolist(), price[order].tolist()
    known: deque[float] = deque(maxlen=p["max_levels"])
    closes = b.close.tolist()
    res, sup = [NAN] * b.n, [NAN] * b.n
    j = 0
    for i, c in enumerate(closes):
        while j < len(conf_l) and conf_l[j] <= i:
            known.append(price_l[j])
            j += 1
        above = [x for x in known if x > c]
        below = [x for x in known if x < c]
        if above:
            res[i] = min(above)
        if below:
            sup[i] = max(below)
    return {"resistance": np.array(res), "support": np.array(sup)}


# --- end of levels part A ---

_FIB_LEVELS = (
    ("l0", 0.0, "0 %", MUTED),
    ("l236", 0.236, "23,6 %", ROSE),
    ("l382", 0.382, "38,2 %", WARN),
    ("l500", 0.5, "50 %", INFO),
    ("l618", 0.618, "61,8 %", POS),
    ("l786", 0.786, "78,6 %", PURPLE),
    ("l100", 1.0, "100 %", MUTED),
)


@indicator(
    "fib_auto",
    "Retrocesos de Fibonacci automáticos",
    LEVELS,
    overlay=True,
    description="Retrocesos del último impulso dentro de las últimas N velas: si el máximo es más reciente que el "
    "mínimo se miden desde el máximo hacia abajo (y al revés). 38,2–61,8 % es la zona clásica de rebote.",
    params=[p_len(100, 10, 1000)],
    outputs=[step(key, title, color) for key, _, title, color in _FIB_LEVELS],
)
def _fib_auto(b: Bars, p: dict) -> dict:
    n = p["length"]
    hh = ta.rolling_max(b.high, n)
    ll = ta.rolling_min(b.low, n)
    up_leg = ta.bars_since_max(b.high, n) <= ta.bars_since_min(b.low, n)  # the high is the latest extreme
    rng = hh - ll
    out = {}
    for key, ratio, _, _ in _FIB_LEVELS:
        out[key] = np.where(up_leg, hh - ratio * rng, ll + ratio * rng)
    return out


def _auto_previous_anchor(tf_s: int) -> str:
    if tf_s < DAY_S:
        return "Día"
    if tf_s < WEEK_S:
        return "Semana"
    return "Mes"


@indicator(
    "prev_hl",
    "Máximo / mínimo del periodo anterior",
    LEVELS,
    overlay=True,
    description="Máximo, mínimo y cierre del día (semana, mes...) anterior: niveles que muchos operadores vigilan "
    "y que suelen actuar como soporte o resistencia.",
    params=[p_anchor("Auto", label="Periodo")],
    outputs=[step("high", "Máximo anterior", NEG), step("low", "Mínimo anterior", POS), step("close", "Cierre anterior", MUTED)],
)
def _prev_hl(b: Bars, p: dict) -> dict:
    g = groups_of(b.anchor_ids(p["anchor"], _auto_previous_anchor(b.tf_seconds())))
    prev = previous_group_ohlc(b, g)
    return {"high": prev["high"], "low": prev["low"], "close": prev["close"]}


@indicator(
    "session_hl",
    "Rango de la sesión en curso",
    LEVELS,
    overlay=True,
    description="Máximo y mínimo alcanzados desde el inicio del periodo actual y su precio de apertura. "
    "Romper el rango de la sesión con volumen suele indicar continuación.",
    params=[p_anchor("Auto", label="Periodo")],
    outputs=[line("high", "Máximo de la sesión", NEG), line("low", "Mínimo de la sesión", POS), step("open", "Apertura", WARN)],
)
def _session_hl(b: Bars, p: dict) -> dict:
    g = groups_of(b.anchor_ids(p["anchor"], auto_session_anchor(b.tf_seconds())))
    return {
        "high": group_running(b.high, g, np.maximum),
        "low": group_running(b.low, g, np.minimum),
        "open": b.open[g.starts][g.gid],
    }
