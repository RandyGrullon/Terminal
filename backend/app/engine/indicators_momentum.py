"""Momentum oscillators (category Momento), part 1: RSI, stochastics, MACD, CCI, %R, ROC, TRIX, TSI, UO, AO..."""
from __future__ import annotations

from app.engine import ta
from app.engine.indicators_core import (
    INFO,
    MOMENTUM,
    NEG,
    ORANGE,
    POS,
    PURPLE,
    ROSE,
    TEAL,
    WARN,
    Bars,
    hist,
    indicator,
    line,
    p_int,
    p_len,
    p_source,
)


@indicator(
    "rsi",
    "Índice de fuerza relativa (RSI)",
    MOMENTUM,
    overlay=False,
    description="Oscilador 0–100 de Wilder que compara subidas y bajadas recientes. > 70 sobrecompra, < 30 "
    "sobreventa; en tendencia fuerte puede quedarse en zona extrema. Incluye su media como señal.",
    params=[p_source(), p_len(14, 2, 200), p_int("ma_length", "Periodo de la media", 14, 1, 200)],
    outputs=[line("rsi", "RSI", PURPLE), line("ma", "Media del RSI", WARN)],
    levels=[30.0, 50.0, 70.0],
    range=(0.0, 100.0),
)
def _rsi(b: Bars, p: dict) -> dict:
    r = ta.rsi(b.src(p["source"]), p["length"])
    return {"rsi": r, "ma": ta.sma(r, p["ma_length"])}


@indicator(
    "stoch",
    "Estocástico",
    MOMENTUM,
    overlay=False,
    description="Posición del cierre dentro del rango de las últimas N velas (%K) y su media (%D). "
    "> 80 sobrecompra, < 20 sobreventa; los cruces %K/%D anticipan giros.",
    params=[
        p_len(14, 1, 500, label="Periodo %K"),
        p_int("smooth", "Suavizado %K", 3, 1, 50),
        p_int("d", "Periodo %D", 3, 1, 50),
    ],
    outputs=[line("k", "%K", INFO), line("d", "%D", WARN)],
    levels=[20.0, 50.0, 80.0],
    range=(0.0, 100.0),
)
def _stoch(b: Bars, p: dict) -> dict:
    k, d = ta.stoch(b.high, b.low, b.close, p["length"], p["smooth"], p["d"])
    return {"k": k, "d": d}


@indicator(
    "stochrsi",
    "RSI estocástico",
    MOMENTUM,
    overlay=False,
    description="Estocástico aplicado al RSI: mucho más sensible que el RSI, detecta giros de corto plazo. "
    "> 80 sobrecompra, < 20 sobreventa.",
    params=[
        p_source(),
        p_int("rsi_length", "Periodo RSI", 14, 2, 200),
        p_int("stoch_length", "Periodo estocástico", 14, 1, 200),
        p_int("k", "Suavizado %K", 3, 1, 50),
        p_int("d", "Periodo %D", 3, 1, 50),
    ],
    outputs=[line("k", "%K", INFO), line("d", "%D", WARN)],
    levels=[20.0, 80.0],
    range=(0.0, 100.0),
)
def _stochrsi(b: Bars, p: dict) -> dict:
    k, d = ta.stochrsi(b.src(p["source"]), p["rsi_length"], p["stoch_length"], p["k"], p["d"])
    return {"k": k, "d": d}


@indicator(
    "macd",
    "MACD",
    MOMENTUM,
    overlay=False,
    description="Diferencia entre EMA rápida y lenta (línea MACD), su media (señal) y el histograma. "
    "Histograma positivo y creciente = momento alcista; cruce de la señal = cambio de impulso.",
    params=[
        p_source(),
        p_int("fast", "Periodo rápido", 12, 1, 200),
        p_int("slow", "Periodo lento", 26, 2, 400),
        p_int("signal", "Periodo de señal", 9, 1, 200),
    ],
    outputs=[hist("hist", "Histograma", TEAL), line("macd", "MACD", INFO), line("signal", "Señal", ORANGE)],
    levels=[0.0],
)
def _macd(b: Bars, p: dict) -> dict:
    m, s, h = ta.macd(b.src(p["source"]), p["fast"], p["slow"], p["signal"])
    return {"hist": h, "macd": m, "signal": s}


@indicator(
    "cci",
    "Índice de canal de materias primas (CCI)",
    MOMENTUM,
    overlay=False,
    description="Desviación del precio típico respecto de su media en unidades de desviación media. "
    "> +100 impulso alcista fuerte, < −100 impulso bajista.",
    params=[p_source("hlc3"), p_len(20, 2, 500)],
    outputs=[line("cci", "CCI", INFO)],
    levels=[-100.0, 0.0, 100.0],
)
def _cci(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    return {"cci": ta.cci(x, x, x, p["length"])}


@indicator(
    "willr",
    "Williams %R",
    MOMENTUM,
    overlay=False,
    description="Distancia del cierre al máximo de N velas (0 a −100). Por encima de −20 sobrecompra, "
    "por debajo de −80 sobreventa.",
    params=[p_len(14, 1, 500)],
    outputs=[line("wr", "%R", PURPLE)],
    levels=[-80.0, -50.0, -20.0],
    range=(-100.0, 0.0),
)
def _willr(b: Bars, p: dict) -> dict:
    return {"wr": ta.willr(b.high, b.low, b.close, p["length"])}


@indicator(
    "roc",
    "Tasa de cambio (ROC)",
    MOMENTUM,
    overlay=False,
    description="Variación porcentual del precio respecto de hace N velas. Cruces de cero = cambio de momento.",
    params=[p_source(), p_len(9, 1, 500)],
    outputs=[line("roc", "ROC %", INFO)],
    levels=[0.0],
)
def _roc(b: Bars, p: dict) -> dict:
    return {"roc": ta.roc(b.src(p["source"]), p["length"])}


@indicator(
    "mom",
    "Momentum",
    MOMENTUM,
    overlay=False,
    description="Diferencia de precio respecto de hace N velas. Positivo y creciente = aceleración alcista.",
    params=[p_source(), p_len(10, 1, 500)],
    outputs=[line("mom", "Momentum", INFO)],
    levels=[0.0],
)
def _mom(b: Bars, p: dict) -> dict:
    return {"mom": ta.mom(b.src(p["source"]), p["length"])}


@indicator(
    "trix",
    "TRIX",
    MOMENTUM,
    overlay=False,
    description="Variación porcentual de una EMA triple: filtra el ruido y muestra el ciclo dominante. "
    "Cruce de cero o de su señal = cambio de tendencia.",
    params=[p_source(), p_len(18, 1, 200), p_int("signal", "Periodo de señal", 9, 1, 100)],
    outputs=[line("trix", "TRIX", ROSE), line("signal", "Señal", WARN)],
    levels=[0.0],
)
def _trix(b: Bars, p: dict) -> dict:
    t = ta.trix(b.src(p["source"]), p["length"])
    return {"trix": t, "signal": ta.ema(t, p["signal"])}


@indicator(
    "tsi",
    "Índice de fuerza verdadera (TSI)",
    MOMENTUM,
    overlay=False,
    description="Momento doblemente suavizado (−100..100) de Blau. Cruces de su señal y de cero marcan giros.",
    params=[
        p_source(),
        p_int("long", "Periodo largo", 25, 1, 200),
        p_int("short", "Periodo corto", 13, 1, 200),
        p_int("signal", "Periodo de señal", 13, 1, 100),
    ],
    outputs=[line("tsi", "TSI", INFO), line("signal", "Señal", NEG)],
    levels=[0.0],
)
def _tsi(b: Bars, p: dict) -> dict:
    t = ta.tsi(b.src(p["source"]), p["long"], p["short"])
    return {"tsi": t, "signal": ta.ema(t, p["signal"])}


@indicator(
    "uo",
    "Oscilador definitivo (Ultimate)",
    MOMENTUM,
    overlay=False,
    description="Presión compradora promediada en tres horizontes (7/14/28) para evitar señales falsas. "
    "> 70 sobrecompra, < 30 sobreventa.",
    params=[
        p_int("fast", "Periodo rápido", 7, 1, 100),
        p_int("mid", "Periodo medio", 14, 1, 200),
        p_int("slow", "Periodo lento", 28, 1, 400),
    ],
    outputs=[line("uo", "UO", PURPLE)],
    levels=[30.0, 50.0, 70.0],
    range=(0.0, 100.0),
)
def _uo(b: Bars, p: dict) -> dict:
    return {"uo": ta.ultimate(b.high, b.low, b.close, p["fast"], p["mid"], p["slow"])}


@indicator(
    "ao",
    "Oscilador asombroso (AO)",
    MOMENTUM,
    overlay=False,
    description="SMA 5 − SMA 34 del precio medio (Bill Williams). Histograma sobre cero = momento alcista.",
    params=[p_int("fast", "Periodo rápido", 5, 1, 100), p_int("slow", "Periodo lento", 34, 2, 300)],
    outputs=[hist("ao", "AO", POS)],
    levels=[0.0],
)
def _ao(b: Bars, p: dict) -> dict:
    return {"ao": ta.awesome(b.high, b.low, p["fast"], p["slow"])}


@indicator(
    "ac",
    "Oscilador de aceleración (AC)",
    MOMENTUM,
    overlay=False,
    description="AO menos su media de 5: mide la aceleración del momento; cambia antes que el AO.",
    params=[
        p_int("fast", "Periodo rápido", 5, 1, 100),
        p_int("slow", "Periodo lento", 34, 2, 300),
        p_int("signal", "Periodo de media", 5, 1, 100),
    ],
    outputs=[hist("ac", "AC", POS)],
    levels=[0.0],
)
def _ac(b: Bars, p: dict) -> dict:
    ao = ta.awesome(b.high, b.low, p["fast"], p["slow"])
    return {"ac": ao - ta.sma(ao, p["signal"])}


# --- end of momentum part 1 ---
