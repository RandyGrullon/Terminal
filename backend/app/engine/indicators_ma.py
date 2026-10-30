"""Moving averages (category Tendencia): every classic MA plus a ribbon and a two-average cross."""
from __future__ import annotations

from typing import Callable

import numpy as np

from app.engine import ta
from app.engine.indicators_core import (
    ACCENT,
    BROWN,
    CYAN,
    GOLD,
    INDIGO,
    INFO,
    LIME,
    MAGENTA,
    ORANGE,
    PINK,
    PURPLE,
    SKY,
    TEAL,
    TREND,
    WARN,
    YELLOW,
    Bars,
    indicator,
    line,
    ma_of,
    p_float,
    p_int,
    p_len,
    p_ma,
    p_source,
)

MAX_LEN = 1000


def _simple(
    iid: str,
    name: str,
    title: str,
    color: str,
    default: int,
    fn: Callable[[np.ndarray, int], np.ndarray],
    description: str,
    lo: int = 1,
) -> None:
    """Registers a single-line moving average ``fn(source, length)``."""

    @indicator(
        iid,
        name,
        TREND,
        overlay=True,
        description=description,
        params=[p_source(), p_len(default, lo, MAX_LEN)],
        outputs=[line(iid, title, color)],
    )
    def _compute(b: Bars, p: dict) -> dict:
        return {iid: fn(b.src(p["source"]), p["length"])}


_simple(
    "sma", "Media móvil simple (SMA)", "SMA", WARN, 20, ta.sma,
    "Promedio aritmético de las últimas N velas. Marca la tendencia de fondo; reacciona con retraso a los giros.",
)
_simple(
    "ema", "Media móvil exponencial (EMA)", "EMA", INFO, 21, ta.ema,
    "Media que pondera más las velas recientes: sigue al precio antes que la SMA. Precio sobre la EMA = sesgo alcista.",
)
_simple(
    "wma", "Media móvil ponderada (WMA)", "WMA", PURPLE, 20, ta.wma,
    "Media con pesos lineales (la vela más reciente pesa N, la más antigua 1). Más reactiva que la SMA.",
)
_simple(
    "hma", "Media móvil de Hull (HMA)", "HMA", CYAN, 21, ta.hma,
    "Media de Alan Hull: casi sin retraso y muy suave. Su pendiente es un buen filtro de dirección.",
    lo=2,
)
_simple(
    "dema", "Media exponencial doble (DEMA)", "DEMA", ORANGE, 21, ta.dema,
    "2·EMA − EMA(EMA): reduce el retraso de la EMA clásica manteniendo la suavidad.",
)
_simple(
    "tema", "Media exponencial triple (TEMA)", "TEMA", PINK, 21, ta.tema,
    "3·EMA − 3·EMA² + EMA³: todavía menos retraso que la DEMA; útil para tendencias rápidas.",
)
_simple(
    "trima", "Media móvil triangular (TRIMA)", "TRIMA", TEAL, 20, ta.trima,
    "SMA de una SMA: pondera más el centro de la ventana. Muy suave, ideal para ver ciclos.",
)
_simple(
    "zlema", "EMA de retardo cero (ZLEMA)", "ZLEMA", LIME, 21, ta.zlema,
    "EMA aplicada a un precio corregido por su propio retraso: responde casi sin demora a los cambios.",
)
_simple(
    "smma", "Media suavizada (SMMA / RMA)", "SMMA", SKY, 14, ta.rma,
    "Media de Wilder (alfa = 1/N), la que usan el RSI y el ATR. Muy estable, ideal como tendencia de fondo.",
)
_simple(
    "lsma", "Media de mínimos cuadrados (LSMA)", "LSMA", BROWN, 25, ta.lsma,
    "Valor actual de la recta de regresión lineal de las últimas N velas: sigue la pendiente del precio.",
    lo=3,
)


@indicator(
    "vwma",
    "Media ponderada por volumen (VWMA)",
    TREND,
    overlay=True,
    needs_volume=True,
    description="Media donde cada vela pesa según su volumen: las velas con mucha participación mueven más la media. "
    "Si la VWMA está por encima de la SMA, el volumen acompaña a las subidas.",
    params=[p_source(), p_len(20, 1, MAX_LEN)],
    outputs=[line("vwma", "VWMA", GOLD)],
)
def _vwma(b: Bars, p: dict) -> dict:
    return {"vwma": ta.vwma(b.src(p["source"]), b.volume, p["length"])}


@indicator(
    "kama",
    "Media adaptativa de Kaufman (KAMA)",
    TREND,
    overlay=True,
    description="Se acelera cuando el precio avanza con eficiencia y casi se detiene en mercados laterales; "
    "filtra el ruido sin perder las tendencias.",
    params=[
        p_source(),
        p_len(10, 2, 200, label="Periodo de eficiencia"),
        p_int("fast", "Constante rápida", 2, 1, 50),
        p_int("slow", "Constante lenta", 30, 2, 200),
    ],
    outputs=[line("kama", "KAMA", YELLOW)],
)
def _kama(b: Bars, p: dict) -> dict:
    return {"kama": ta.kama(b.src(p["source"]), p["length"], p["fast"], p["slow"])}


@indicator(
    "alma",
    "Media de Arnaud Legoux (ALMA)",
    TREND,
    overlay=True,
    description="Media con pesos gaussianos desplazados hacia las velas recientes: muy suave y con poco retraso.",
    params=[
        p_source(),
        p_len(9, 2, MAX_LEN),
        p_float("offset", "Desplazamiento", 0.85, 0.0, 1.0, 0.01),
        p_float("sigma", "Sigma", 6.0, 0.5, 20.0, 0.5),
    ],
    outputs=[line("alma", "ALMA", INDIGO)],
)
def _alma(b: Bars, p: dict) -> dict:
    return {"alma": ta.alma(b.src(p["source"]), p["length"], p["offset"], p["sigma"])}


@indicator(
    "mcginley",
    "Indicador dinámico de McGinley",
    TREND,
    overlay=True,
    description="Media que ajusta su velocidad a la del mercado: acelera en caídas rápidas y evita separarse del precio.",
    params=[p_source(), p_len(14, 2, 500), p_float("k", "Constante K", 0.6, 0.1, 2.0, 0.05)],
    outputs=[line("mcginley", "McGinley", MAGENTA)],
)
def _mcginley(b: Bars, p: dict) -> dict:
    return {"mcginley": ta.mcginley(b.src(p["source"]), p["length"], p["k"])}


@indicator(
    "t3",
    "Media T3 de Tillson",
    TREND,
    overlay=True,
    description="Seis EMAs encadenadas con factor de volumen: extremadamente suave y con poco retraso.",
    params=[p_source(), p_len(5, 1, 200), p_float("factor", "Factor de volumen", 0.7, 0.0, 1.0, 0.05)],
    outputs=[line("t3", "T3", ACCENT)],
)
def _t3(b: Bars, p: dict) -> dict:
    return {"t3": ta.t3(b.src(p["source"]), p["length"], p["factor"])}


_RIBBON_COLORS = (CYAN, INFO, ACCENT, INDIGO, PURPLE, MAGENTA)


@indicator(
    "ma_ribbon",
    "Cinta de medias móviles",
    TREND,
    overlay=True,
    description="Seis medias de periodos crecientes. Cinta abierta y ordenada = tendencia sana; "
    "cinta comprimida o cruzada = falta de dirección.",
    params=[
        p_ma("EMA"),
        p_source(),
        p_int("base", "Periodo inicial", 20, 1, 500),
        p_int("step", "Incremento", 10, 1, 200),
    ],
    outputs=[line(f"ma{i + 1}", f"Media {i + 1}", c) for i, c in enumerate(_RIBBON_COLORS)],
)
def _ma_ribbon(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    out = {}
    for i in range(len(_RIBBON_COLORS)):
        length = min(MAX_LEN, p["base"] + i * p["step"])
        out[f"ma{i + 1}"] = ma_of(p["ma_type"], x, length, b.volume)
    return out


@indicator(
    "ma_cross",
    "Cruce de medias (rápida / lenta)",
    TREND,
    overlay=True,
    description="Dos medias configurables. Cruce al alza de la rápida = señal alcista (50/200 = «cruce dorado»); "
    "cruce a la baja = señal bajista («cruce de la muerte»).",
    params=[
        p_ma("SMA"),
        p_source(),
        p_int("fast", "Periodo rápido", 50, 1, MAX_LEN),
        p_int("slow", "Periodo lento", 200, 2, MAX_LEN),
    ],
    outputs=[line("fast", "Media rápida", WARN), line("slow", "Media lenta", PURPLE)],
)
def _ma_cross(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    return {
        "fast": ma_of(p["ma_type"], x, p["fast"], b.volume),
        "slow": ma_of(p["ma_type"], x, p["slow"], b.volume),
    }
