"""Statistical indicators (category Estadística): z-score, Hurst, efficiency, regression, moments of returns..."""
from __future__ import annotations

import math

import numpy as np

from app.engine import ta
from app.engine.indicators_core import (
    CYAN,
    GOLD,
    INFO,
    MUTED,
    NAN,
    ORANGE,
    POS,
    PURPLE,
    STATS,
    TEAL,
    WARN,
    YELLOW,
    Bars,
    hist,
    indicator,
    line,
    p_int,
    p_len,
    p_source,
    rolling_apply_windows,
)


def _log_returns(x: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(ta.f64(x) / ta.shift(x, 1))
    return np.where(np.isfinite(r), r, NAN)


def _central_moments(x: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rolling 2nd/3rd/4th central moments over the last n values (NaN when the window has a NaN)."""
    m = x.shape[0]
    out = [np.full(m, NAN) for _ in range(3)]
    w = rolling_apply_windows(x, n)
    if w is None:
        return out[0], out[1], out[2]
    d = w - w.mean(axis=1, keepdims=True)
    d2 = d * d
    out[0][n - 1 :] = d2.mean(axis=1)
    out[1][n - 1 :] = (d2 * d).mean(axis=1)
    out[2][n - 1 :] = (d2 * d2).mean(axis=1)
    return out[0], out[1], out[2]


@indicator(
    "zscore",
    "Puntuación Z",
    STATS,
    overlay=False,
    description="Cuántas desviaciones estándar se aleja el precio de su media de N velas. |Z| > 2 = extremo "
    "estadístico (posible reversión en rangos, fuerza en tendencias).",
    params=[p_source(), p_len(20, 2, 500)],
    outputs=[line("z", "Z", INFO)],
    levels=[-2.0, 0.0, 2.0],
)
def _zscore(b: Bars, p: dict) -> dict:
    return {"z": ta.zscore(b.src(p["source"]), p["length"])}


@indicator(
    "hurst",
    "Exponente de Hurst",
    STATS,
    overlay=False,
    description="Memoria del precio: > 0,5 tendencia persistente (seguir el movimiento), < 0,5 reversión a la media "
    "(operar rangos), ≈ 0,5 paseo aleatorio (sin ventaja).",
    params=[p_len(100, 40, 1000), p_int("max_lag", "Retardo máximo", 20, 4, 64)],
    outputs=[line("hurst", "Hurst", PURPLE)],
    levels=[0.5],
    range=(0.0, 1.0),
)
def _hurst(b: Bars, p: dict) -> dict:
    return {"hurst": ta.hurst(b.close, p["length"], p["max_lag"])}


@indicator(
    "er",
    "Ratio de eficiencia de Kaufman",
    STATS,
    overlay=False,
    description="Desplazamiento neto / recorrido total en N velas (0..1). Cerca de 1 = tendencia limpia; "
    "cerca de 0 = ruido. Bajo 0,25 el filtro de operaciones considera el mercado «picado».",
    params=[p_source(), p_len(10, 2, 500)],
    outputs=[line("er", "Eficiencia", CYAN)],
    levels=[0.25],
    range=(0.0, 1.0),
)
def _er(b: Bars, p: dict) -> dict:
    return {"er": ta.efficiency_ratio(b.src(p["source"]), p["length"])}


@indicator(
    "corr_time",
    "Correlación con el tiempo (R²)",
    STATS,
    overlay=False,
    description="Correlación (r) del precio con el tiempo en N velas y su R²: qué parte del movimiento explica una "
    "línea recta. R² alto = tendencia ordenada; r indica el sentido.",
    params=[p_source(), p_len(20, 3, 500)],
    outputs=[line("r", "r", INFO), line("r2", "R²", WARN)],
    levels=[0.0],
    range=(-1.0, 1.0),
)
def _corr_time(b: Bars, p: dict) -> dict:
    st = ta.linreg_stats(b.src(p["source"]), p["length"])
    r2 = st["r2"]
    return {"r": np.sign(st["slope"]) * np.sqrt(r2), "r2": r2}


@indicator(
    "linreg_slope",
    "Pendiente de regresión (% por vela)",
    STATS,
    overlay=False,
    description="Pendiente de la recta de regresión de N velas expresada en % del precio por vela: velocidad de "
    "la tendencia comparable entre activos.",
    params=[p_source(), p_len(20, 3, 500)],
    outputs=[hist("slope", "Pendiente %", POS)],
    levels=[0.0],
)
def _linreg_slope(b: Bars, p: dict) -> dict:
    st = ta.linreg_stats(b.src(p["source"]), p["length"])
    return {"slope": 100.0 * ta.safe_div(st["slope"], st["value"])}


@indicator(
    "linreg_t",
    "Pendiente de regresión (estadístico t)",
    STATS,
    overlay=False,
    description="Pendiente dividida por su error estándar: |t| > 2 indica una tendencia estadísticamente "
    "significativa en la ventana; cerca de 0, sin tendencia.",
    params=[p_source(), p_len(20, 3, 500)],
    outputs=[line("t", "t", ORANGE)],
    levels=[-2.0, 0.0, 2.0],
)
def _linreg_t(b: Bars, p: dict) -> dict:
    return {"t": ta.linreg_stats(b.src(p["source"]), p["length"])["tstat"]}


@indicator(
    "percent_rank",
    "Rango percentil",
    STATS,
    overlay=False,
    description="Porcentaje de las N velas anteriores con valor menor o igual al actual (0–100). "
    "> 90 = el precio está en la parte alta de su historia reciente.",
    params=[p_source(), p_len(100, 2, 1000)],
    outputs=[line("rank", "Percentil", YELLOW)],
    levels=[10.0, 50.0, 90.0],
    range=(0.0, 100.0),
)
def _percent_rank(b: Bars, p: dict) -> dict:
    return {"rank": ta.percent_rank(b.src(p["source"]), p["length"])}


@indicator(
    "autocorr",
    "Autocorrelación de retornos",
    STATS,
    overlay=False,
    description="Correlación de los retornos con los de K velas antes, en una ventana de N. Positiva = los "
    "movimientos tienden a continuar; negativa = tienden a revertir.",
    params=[p_len(50, 10, 500), p_int("lag", "Retardo (velas)", 1, 1, 50)],
    outputs=[hist("ac", "Autocorrelación", INFO)],
    levels=[0.0],
    range=(-1.0, 1.0),
)
def _autocorr(b: Bars, p: dict) -> dict:
    r = _log_returns(b.close)
    n = p["length"]
    out = np.full(b.n, NAN)
    wa = rolling_apply_windows(r, n)
    wb = rolling_apply_windows(ta.shift(r, p["lag"]), n)
    if wa is None or wb is None:
        return {"ac": out}
    da = wa - wa.mean(axis=1, keepdims=True)
    db = wb - wb.mean(axis=1, keepdims=True)
    den = np.sqrt((da * da).sum(axis=1) * (db * db).sum(axis=1))
    out[n - 1 :] = ta.safe_div((da * db).sum(axis=1), den)
    return {"ac": np.clip(out, -1.0, 1.0)}


@indicator(
    "skew",
    "Asimetría de retornos",
    STATS,
    overlay=False,
    description="Asimetría de los retornos en N velas: negativa = caídas bruscas ocasionales (riesgo de cola "
    "bajista); positiva = subidas explosivas ocasionales.",
    params=[p_len(50, 10, 1000)],
    outputs=[line("skew", "Asimetría", TEAL)],
    levels=[0.0],
)
def _skew(b: Bars, p: dict) -> dict:
    m2, m3, _ = _central_moments(_log_returns(b.close), p["length"])
    return {"skew": ta.safe_div(m3, m2 ** 1.5)}


@indicator(
    "kurtosis",
    "Curtosis de retornos (exceso)",
    STATS,
    overlay=False,
    description="Exceso de curtosis de los retornos en N velas: > 0 = colas gordas (movimientos extremos más "
    "frecuentes que en una normal). Conviene reducir el tamaño de posición.",
    params=[p_len(50, 10, 1000)],
    outputs=[line("kurt", "Curtosis", ORANGE)],
    levels=[0.0],
)
def _kurtosis(b: Bars, p: dict) -> dict:
    m2, _, m4 = _central_moments(_log_returns(b.close), p["length"])
    return {"kurt": ta.safe_div(m4, m2 * m2) - 3.0}


@indicator(
    "median",
    "Mediana móvil",
    STATS,
    overlay=True,
    description="Mediana de las últimas N velas: como una media pero inmune a velas extremas (mechas, picos).",
    params=[p_source(), p_len(20, 1, 500)],
    outputs=[line("median", "Mediana", GOLD)],
)
def _median(b: Bars, p: dict) -> dict:
    out = np.full(b.n, NAN)
    w = rolling_apply_windows(b.src(p["source"]), p["length"])
    if w is not None:
        out[p["length"] - 1 :] = np.median(w, axis=1)
    return {"median": out}


@indicator(
    "sharpe",
    "Ratio de Sharpe móvil (anualizado)",
    STATS,
    overlay=False,
    description="Retorno medio / volatilidad de las últimas N velas, anualizado (sin tasa libre de riesgo). "
    "> 1 = tendencia con buena relación rentabilidad/riesgo.",
    params=[p_len(50, 10, 1000), p_int("days", "Días de mercado por año", 365, 200, 366)],
    outputs=[hist("sharpe", "Sharpe", POS)],
    levels=[0.0, 1.0],
)
def _sharpe(b: Bars, p: dict) -> dict:
    r = _log_returns(b.close)
    sd = ta.rolling_std(r, p["length"], ddof=1)
    scale = math.sqrt(b.periods_per_year(p["days"]))
    return {"sharpe": ta.safe_div(ta.sma(r, p["length"]), sd) * scale}


@indicator(
    "drawdown",
    "Caída desde máximos (drawdown %)",
    STATS,
    overlay=False,
    description="Distancia porcentual del cierre al máximo de las últimas N velas (0 = todo el histórico "
    "cargado). Mide cuánto «duele» la posición desde su mejor momento.",
    params=[p_int("length", "Periodo (0 = todo el histórico)", 0, 0, 5000)],
    outputs=[hist("dd", "Drawdown %", MUTED)],
)
def _drawdown(b: Bars, p: dict) -> dict:
    c = b.close
    peak = np.fmax.accumulate(c) if p["length"] <= 0 else ta.rolling_max(c, p["length"])
    return {"dd": 100.0 * (ta.safe_div(c, peak) - 1.0)}
