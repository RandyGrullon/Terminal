"""Trend indicators (category Tendencia): VWAP, Supertrend, SAR, Ichimoku, regression channel, ADX, Aroon..."""
from __future__ import annotations

import numpy as np

from app.engine import ta
from app.engine.indicators_core import (
    ACCENT,
    CYAN,
    INFO,
    LILAC,
    MAGENTA,
    MUTED,
    NEG,
    ORANGE,
    POS,
    ROSE,
    TREND,
    WARN,
    Bars,
    auto_session_anchor,
    dots,
    group_cumsum,
    groups_of,
    hist,
    indicator,
    line,
    mask,
    p_anchor,
    p_float,
    p_int,
    p_len,
    p_source,
)


@indicator(
    "vwap",
    "VWAP de sesión (anclado)",
    TREND,
    overlay=True,
    needs_volume=True,
    description="Precio medio ponderado por volumen desde el inicio de cada periodo (día, semana, mes o año) con "
    "bandas de desviación estándar. Precio sobre el VWAP = compradores al mando. Requiere datos de volumen.",
    params=[
        p_anchor("Auto"),
        p_source("hlc3"),
        p_float("mult", "Multiplicador de bandas", 1.0, 0.0, 5.0, 0.25),
    ],
    outputs=[line("vwap", "VWAP", ORANGE), line("upper", "Banda superior", MUTED), line("lower", "Banda inferior", MUTED)],
)
def _vwap(b: Bars, p: dict) -> dict:
    g = groups_of(b.anchor_ids(p["anchor"], auto_session_anchor(b.tf_seconds())))
    x = b.src(p["source"])
    v = b.volume
    ref = float(x[0]) if np.isfinite(x[0]) else 0.0  # causal offset (first bar) for numeric precision
    xd = x - ref
    sv = group_cumsum(v, g)
    mean_d = ta.safe_div(group_cumsum(xd * v, g), sv)
    var = np.maximum(ta.safe_div(group_cumsum(xd * xd * v, g), sv) - mean_d * mean_d, 0.0)
    vwap = mean_d + ref
    sd = np.sqrt(var)
    return {"vwap": vwap, "upper": vwap + p["mult"] * sd, "lower": vwap - p["mult"] * sd}


@indicator(
    "vwap_rolling",
    "VWAP móvil",
    TREND,
    overlay=True,
    needs_volume=True,
    description="VWAP de las últimas N velas (ventana deslizante, sin reinicio por sesión). Útil en mercados 24/7.",
    params=[p_len(20, 1, 1000), p_source("hlc3")],
    outputs=[line("vwap", "VWAP móvil", CYAN)],
)
def _vwap_rolling(b: Bars, p: dict) -> dict:
    return {"vwap": ta.vwma(b.src(p["source"]), b.volume, p["length"])}


@indicator(
    "supertrend",
    "Supertrend",
    TREND,
    overlay=True,
    description="Stop dinámico basado en el ATR que cambia de lado cuando el precio lo perfora. Verde = tendencia "
    "alcista (la línea actúa de soporte), rojo = bajista (resistencia).",
    params=[p_len(10, 1, 200, label="Periodo ATR"), p_float("mult", "Multiplicador ATR", 3.0, 0.5, 10.0, 0.1)],
    outputs=[line("up", "Supertrend alcista", POS), line("down", "Supertrend bajista", NEG)],
)
def _supertrend(b: Bars, p: dict) -> dict:
    st, direction = ta.supertrend(b.high, b.low, b.close, p["length"], p["mult"])
    return {"up": mask(st, direction > 0), "down": mask(st, direction < 0)}


@indicator(
    "psar",
    "SAR parabólico",
    TREND,
    overlay=True,
    description="Puntos de parada y giro de Wilder: debajo del precio en tendencia alcista, encima en bajista. "
    "Se acercan al precio a medida que la tendencia madura; útil como stop dinámico.",
    params=[
        p_float("start", "Aceleración inicial", 0.02, 0.001, 0.5, 0.001),
        p_float("increment", "Incremento", 0.02, 0.001, 0.5, 0.001),
        p_float("maximum", "Aceleración máxima", 0.2, 0.01, 1.0, 0.01),
    ],
    outputs=[dots("bull", "SAR alcista", POS), dots("bear", "SAR bajista", NEG)],
)
def _psar(b: Bars, p: dict) -> dict:
    sar = ta.psar(b.high, b.low, p["start"], p["increment"], max(p["maximum"], p["start"]))
    return {"bull": mask(sar, sar < b.close), "bear": mask(sar, sar >= b.close)}


@indicator(
    "ichimoku",
    "Ichimoku Kinko Hyo",
    TREND,
    overlay=True,
    description="Tenkan, Kijun y la nube (Senkou A/B desplazadas hacia delante). Precio sobre la nube = tendencia "
    "alcista. La línea Chikou se omite porque necesitaría velas futuras.",
    params=[
        p_int("conversion", "Tenkan-sen (conversión)", 9, 1, 200),
        p_int("base", "Kijun-sen (base)", 26, 1, 300),
        p_int("span_b", "Senkou B", 52, 1, 500),
        p_int("displacement", "Desplazamiento", 26, 1, 200),
    ],
    outputs=[
        line("tenkan", "Tenkan-sen", ACCENT),
        line("kijun", "Kijun-sen", ROSE),
        line("span_a", "Senkou A", POS),
        line("span_b", "Senkou B", NEG),
    ],
)
def _ichimoku(b: Bars, p: dict) -> dict:
    def mid(n: int) -> np.ndarray:
        return (ta.rolling_max(b.high, n) + ta.rolling_min(b.low, n)) / 2.0

    tenkan = mid(p["conversion"])
    kijun = mid(p["base"])
    k = p["displacement"] - 1  # TradingView plots the cloud with offset = displacement - 1
    return {
        "tenkan": tenkan,
        "kijun": kijun,
        "span_a": ta.shift((tenkan + kijun) / 2.0, k),
        "span_b": ta.shift(mid(p["span_b"]), k),
    }


@indicator(
    "linreg_channel",
    "Canal de regresión lineal",
    TREND,
    overlay=True,
    description="Extremo actual de la recta de regresión de las últimas N velas ± K desviaciones de los residuos. "
    "Versión causal: cada vela usa solo su propia ventana (no se redibuja hacia atrás).",
    params=[p_source(), p_len(100, 5, 1000), p_float("mult", "Desviaciones", 2.0, 0.1, 5.0, 0.1)],
    outputs=[line("mid", "Regresión", MAGENTA), line("upper", "Canal superior", LILAC), line("lower", "Canal inferior", LILAC)],
)
def _linreg_channel(b: Bars, p: dict) -> dict:
    st = ta.linreg_stats(b.src(p["source"]), p["length"])
    mid, sd = st["value"], st["resid_std"]
    return {"mid": mid, "upper": mid + p["mult"] * sd, "lower": mid - p["mult"] * sd}


@indicator(
    "adx",
    "ADX / DMI",
    TREND,
    overlay=False,
    description="Fuerza de la tendencia (ADX) y su dirección (+DI / −DI). ADX > 25 = tendencia fuerte; "
    "< 20 = mercado sin dirección. +DI sobre −DI = presión compradora.",
    params=[p_len(14, 2, 200)],
    outputs=[line("adx", "ADX", WARN), line("plus_di", "+DI", POS), line("minus_di", "−DI", NEG)],
    levels=[20.0, 25.0],
)
def _adx(b: Bars, p: dict) -> dict:
    pdi, mdi, adx = ta.adx_dmi(b.high, b.low, b.close, p["length"])
    return {"adx": adx, "plus_di": pdi, "minus_di": mdi}


@indicator(
    "aroon",
    "Aroon",
    TREND,
    overlay=False,
    description="Cuánto hace del último máximo (Aroon arriba) y del último mínimo (Aroon abajo) en N velas. "
    "Arriba > 70 y abajo < 30 = tendencia alcista fresca.",
    params=[p_len(14, 1, 500)],
    outputs=[line("up", "Aroon arriba", POS), line("down", "Aroon abajo", NEG)],
    levels=[30.0, 70.0],
    range=(0.0, 100.0),
)
def _aroon(b: Bars, p: dict) -> dict:
    up, down = ta.aroon(b.high, b.low, p["length"])
    return {"up": up, "down": down}


@indicator(
    "aroon_osc",
    "Oscilador Aroon",
    TREND,
    overlay=False,
    description="Aroon arriba − Aroon abajo (−100..100). Positivo = los máximos son más recientes que los mínimos.",
    params=[p_len(14, 1, 500)],
    outputs=[hist("osc", "Oscilador Aroon", INFO)],
    levels=[-50.0, 0.0, 50.0],
    range=(-100.0, 100.0),
)
def _aroon_osc(b: Bars, p: dict) -> dict:
    up, down = ta.aroon(b.high, b.low, p["length"])
    return {"osc": up - down}


@indicator(
    "vortex",
    "Indicador Vortex",
    TREND,
    overlay=False,
    description="Movimiento positivo (VI+) frente a negativo (VI−) normalizado por el rango verdadero. "
    "VI+ cruzando sobre VI− = inicio de tendencia alcista.",
    params=[p_len(14, 2, 200)],
    outputs=[line("plus", "VI+", POS), line("minus", "VI−", NEG)],
    levels=[1.0],
)
def _vortex(b: Bars, p: dict) -> dict:
    vip, vim = ta.vortex(b.high, b.low, b.close, p["length"])
    return {"plus": vip, "minus": vim}


@indicator(
    "choppiness",
    "Índice de choppiness",
    TREND,
    overlay=False,
    description="Mide si el mercado está en tendencia (valores bajos, < 38,2) o lateral y errático (> 61,8). "
    "Útil para NO operar rupturas en mercados picados.",
    params=[p_len(14, 2, 200)],
    outputs=[line("chop", "Choppiness", INFO)],
    levels=[38.2, 61.8],
    range=(0.0, 100.0),
)
def _choppiness(b: Bars, p: dict) -> dict:
    return {"chop": ta.choppiness(b.high, b.low, b.close, p["length"])}
