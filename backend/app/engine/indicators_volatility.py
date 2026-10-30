"""Volatility indicators (category Volatilidad): bands/channels, ATR family, Bollinger derivatives, squeeze..."""
from __future__ import annotations

import math

import numpy as np

from app.engine import ta
from app.engine.indicators_core import (
    CYAN,
    INFO,
    MUTED,
    NAN,
    NEG,
    ORANGE,
    POS,
    PURPLE,
    ROSE,
    SAND,
    SKY,
    TEAL,
    VOLATILITY,
    WARN,
    Bars,
    dots,
    hist,
    indicator,
    line,
    ma_of,
    mask,
    p_bool,
    p_float,
    p_int,
    p_len,
    p_ma,
    p_select,
    p_source,
    step,
)


@indicator(
    "bbands",
    "Bandas de Bollinger",
    VOLATILITY,
    overlay=True,
    description="Media ± K desviaciones estándar. Las bandas se abren con la volatilidad y se estrechan antes de "
    "los movimientos fuertes; tocar la banda no es por sí solo señal de giro.",
    params=[p_source(), p_len(20, 2, 500), p_float("mult", "Desviaciones", 2.0, 0.1, 5.0, 0.1), p_ma("SMA", label="Tipo de base")],
    outputs=[line("basis", "Base", ORANGE), line("upper", "Banda superior", INFO), line("lower", "Banda inferior", INFO)],
)
def _bbands(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    basis = ma_of(p["ma_type"], x, p["length"], b.volume)
    sd = ta.rolling_std(x, p["length"])
    return {"basis": basis, "upper": basis + p["mult"] * sd, "lower": basis - p["mult"] * sd}


@indicator(
    "keltner",
    "Canales de Keltner",
    VOLATILITY,
    overlay=True,
    description="EMA ± K veces el ATR. Cierres fuera del canal indican impulso; dentro, rango normal.",
    params=[
        p_source(),
        p_len(20, 1, 500),
        p_float("mult", "Multiplicador ATR", 2.0, 0.1, 10.0, 0.1),
        p_int("atr_length", "Periodo ATR", 10, 1, 200),
    ],
    outputs=[line("mid", "Base", TEAL), line("upper", "Canal superior", SKY), line("lower", "Canal inferior", SKY)],
)
def _keltner(b: Bars, p: dict) -> dict:
    mid = ta.ema(b.src(p["source"]), p["length"])
    a = ta.atr(b.high, b.low, b.close, p["atr_length"])
    return {"mid": mid, "upper": mid + p["mult"] * a, "lower": mid - p["mult"] * a}


@indicator(
    "donchian",
    "Canales de Donchian",
    VOLATILITY,
    overlay=True,
    description="Máximo y mínimo de las últimas N velas (base de las «tortugas»). Romper el canal superior = "
    "ruptura alcista; el inferior = bajista.",
    params=[p_len(20, 1, 1000)],
    outputs=[line("upper", "Canal superior", POS), line("lower", "Canal inferior", NEG), line("basis", "Base", MUTED)],
)
def _donchian(b: Bars, p: dict) -> dict:
    up, lo, mid = ta.donchian(b.high, b.low, p["length"])
    return {"upper": up, "lower": lo, "basis": mid}


@indicator(
    "envelopes",
    "Envolventes de media móvil",
    VOLATILITY,
    overlay=True,
    description="Media móvil ± un porcentaje fijo. Marca zonas de sobreextensión respecto de la tendencia.",
    params=[
        p_source(),
        p_len(20, 1, 500),
        p_float("percent", "Porcentaje (%)", 2.5, 0.1, 50.0, 0.1),
        p_ma("SMA"),
    ],
    outputs=[line("basis", "Base", WARN), line("upper", "Envolvente superior", SAND), line("lower", "Envolvente inferior", SAND)],
)
def _envelopes(b: Bars, p: dict) -> dict:
    basis = ma_of(p["ma_type"], b.src(p["source"]), p["length"], b.volume)
    k = p["percent"] / 100.0
    return {"basis": basis, "upper": basis * (1.0 + k), "lower": basis * (1.0 - k)}


@indicator(
    "chandelier",
    "Salida Chandelier",
    VOLATILITY,
    overlay=True,
    description="Stop de arrastre a K ATR del extremo de N velas que solo avanza a favor y cambia de lado cuando "
    "el precio lo cruza. Verde = stop para largos, rojo = stop para cortos («hasta aquí»).",
    params=[
        p_len(22, 1, 300, label="Periodo ATR"),
        p_float("mult", "Multiplicador ATR", 3.0, 0.5, 10.0, 0.1),
        p_bool("use_close", "Usar cierres para los extremos", True),
    ],
    outputs=[step("long", "Stop largo", POS), step("short", "Stop corto", NEG)],
)
def _chandelier(b: Bars, p: dict) -> dict:
    n = p["length"]
    a = p["mult"] * ta.atr(b.high, b.low, b.close, n)
    hi_src = b.close if p["use_close"] else b.high
    lo_src = b.close if p["use_close"] else b.low
    long_raw = (ta.rolling_max(hi_src, n) - a).tolist()
    short_raw = (ta.rolling_min(lo_src, n) + a).tolist()
    cl = b.close.tolist()
    m = len(cl)
    ls = [NAN] * m
    ss = [NAN] * m
    dirs = [0.0] * m
    d = 1.0
    lp = sp = NAN
    for i in range(m):
        lv, sv = long_raw[i], short_raw[i]
        if lv != lv or sv != sv:
            continue
        if lp == lp and cl[i - 1] > lp:
            lv = max(lv, lp)
        if sp == sp and cl[i - 1] < sp:
            sv = min(sv, sp)
        if sp == sp and cl[i] > sp:
            d = 1.0
        elif lp == lp and cl[i] < lp:
            d = -1.0
        ls[i], ss[i], dirs[i] = lv, sv, d
        lp, sp = lv, sv
    dd = np.array(dirs)
    return {"long": mask(np.array(ls), dd > 0), "short": mask(np.array(ss), dd < 0)}


@indicator(
    "atr",
    "Rango verdadero medio (ATR)",
    VOLATILITY,
    overlay=False,
    description="Volatilidad media por vela (incluye huecos). Base para colocar stops: SL típico = 1,5–3 ATR.",
    params=[p_len(14, 1, 500), p_select("smoothing", "Suavizado", "RMA", ("RMA", "SMA", "EMA", "WMA"))],
    outputs=[line("atr", "ATR", ROSE)],
)
def _atr(b: Bars, p: dict) -> dict:
    return {"atr": ma_of(p["smoothing"], ta.true_range(b.high, b.low, b.close), p["length"])}


@indicator(
    "natr",
    "ATR normalizado (%)",
    VOLATILITY,
    overlay=False,
    description="ATR en porcentaje del precio: permite comparar la volatilidad entre activos y épocas.",
    params=[p_len(14, 1, 500)],
    outputs=[line("natr", "NATR %", WARN)],
)
def _natr(b: Bars, p: dict) -> dict:
    return {"natr": 100.0 * ta.safe_div(ta.atr(b.high, b.low, b.close, p["length"]), b.close)}


@indicator(
    "bb_pctb",
    "%B de Bollinger",
    VOLATILITY,
    overlay=False,
    description="Posición del precio dentro de las bandas: 1 = banda superior, 0 = inferior, > 1 o < 0 = fuera.",
    params=[p_source(), p_len(20, 2, 500), p_float("mult", "Desviaciones", 2.0, 0.1, 5.0, 0.1)],
    outputs=[line("pctb", "%B", INFO)],
    levels=[0.0, 0.5, 1.0],
)
def _bb_pctb(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    mid, up, lo, _ = ta.bollinger(x, p["length"], p["mult"])
    return {"pctb": ta.safe_div(x - lo, up - lo)}


@indicator(
    "bb_width",
    "Ancho de Bollinger (%)",
    VOLATILITY,
    overlay=False,
    description="Distancia entre bandas en % de la base. Mínimos históricos de ancho = compresión que suele "
    "preceder a un movimiento fuerte.",
    params=[p_source(), p_len(20, 2, 500), p_float("mult", "Desviaciones", 2.0, 0.1, 5.0, 0.1)],
    outputs=[line("width", "Ancho %", PURPLE)],
)
def _bb_width(b: Bars, p: dict) -> dict:
    mid, up, lo, _ = ta.bollinger(b.src(p["source"]), p["length"], p["mult"])
    return {"width": 100.0 * ta.safe_div(up - lo, mid)}


# --- end of volatility part A ---


def _p_days() -> dict:
    return p_int("days", "Días de mercado por año", 365, 200, 366)


@indicator(
    "hv",
    "Volatilidad histórica (anualizada %)",
    VOLATILITY,
    overlay=False,
    description="Desviación estándar de los retornos logarítmicos anualizada. Usa 365 días para cripto y "
    "≈252 para acciones.",
    params=[p_len(20, 2, 500), _p_days()],
    outputs=[line("hv", "VH %", ORANGE)],
)
def _hv(b: Bars, p: dict) -> dict:
    return {"hv": ta.hist_volatility(b.close, p["length"], b.periods_per_year(p["days"]))}


@indicator(
    "stdev",
    "Desviación estándar",
    VOLATILITY,
    overlay=False,
    description="Dispersión del precio respecto de su media en N velas, en unidades de precio.",
    params=[p_source(), p_len(20, 2, 500)],
    outputs=[line("stdev", "Desv. estándar", INFO)],
)
def _stdev(b: Bars, p: dict) -> dict:
    return {"stdev": ta.rolling_std(b.src(p["source"]), p["length"])}


@indicator(
    "ulcer",
    "Índice Ulcer",
    VOLATILITY,
    overlay=False,
    description="Profundidad y duración de las caídas desde el máximo reciente: mide el «dolor» del riesgo "
    "bajista (solo cuenta las caídas).",
    params=[p_source(), p_len(14, 2, 500)],
    outputs=[line("ulcer", "Ulcer", NEG)],
)
def _ulcer(b: Bars, p: dict) -> dict:
    return {"ulcer": ta.ulcer_index(b.src(p["source"]), p["length"])}


@indicator(
    "chaikin_vol",
    "Volatilidad de Chaikin",
    VOLATILITY,
    overlay=False,
    description="Variación porcentual de la EMA del rango máximo−mínimo. Subidas bruscas suelen acompañar "
    "techos/pánicos; caídas largas, acumulación tranquila.",
    params=[p_len(10, 1, 200, label="Periodo EMA"), p_int("roc_length", "Periodo de variación", 10, 1, 200)],
    outputs=[line("cv", "Volatilidad Chaikin %", WARN)],
    levels=[0.0],
)
def _chaikin_vol(b: Bars, p: dict) -> dict:
    return {"cv": ta.chaikin_volatility(b.high, b.low, p["length"], p["roc_length"])}


@indicator(
    "mass_index",
    "Índice de masa",
    VOLATILITY,
    overlay=False,
    description="Detecta ensanchamientos del rango que preceden giros: la «protuberancia» clásica es superar 27 "
    "y volver a caer bajo 26,5.",
    params=[p_int("ema_length", "Periodo EMA", 9, 1, 100), p_int("sum_length", "Periodo de suma", 25, 2, 200)],
    outputs=[line("mi", "Índice de masa", CYAN)],
    levels=[26.5, 27.0],
)
def _mass_index(b: Bars, p: dict) -> dict:
    return {"mi": ta.mass_index(b.high, b.low, p["ema_length"], p["sum_length"])}


@indicator(
    "squeeze",
    "Squeeze Momentum (Bollinger dentro de Keltner)",
    VOLATILITY,
    overlay=False,
    description="Puntos ámbar = compresión activa (Bollinger dentro de Keltner): el mercado acumula energía, "
    "mejor esperar. Puntos azules = compresión liberada; el histograma indica la dirección del momento.",
    params=[
        p_int("bb_length", "Periodo Bollinger", 20, 2, 300),
        p_float("bb_mult", "Desviaciones Bollinger", 2.0, 0.1, 5.0, 0.1),
        p_int("kc_length", "Periodo Keltner", 20, 2, 300),
        p_float("kc_mult", "Multiplicador Keltner", 1.5, 0.1, 5.0, 0.1),
    ],
    outputs=[hist("mom", "Momento", POS), dots("on", "Compresión activa", WARN), dots("off", "Compresión liberada", INFO)],
    levels=[0.0],
)
def _squeeze(b: Bars, p: dict) -> dict:
    c = b.close
    _, up_bb, lo_bb, _ = ta.bollinger(c, p["bb_length"], p["bb_mult"])
    n = p["kc_length"]
    ma = ta.sma(c, n)
    rng = ta.sma(ta.true_range(b.high, b.low, c), n)
    up_kc, lo_kc = ma + p["kc_mult"] * rng, ma - p["kc_mult"] * rng
    valid = np.isfinite(up_bb) & np.isfinite(up_kc)
    on = valid & (lo_bb > lo_kc) & (up_bb < up_kc)
    off = valid & (lo_bb < lo_kc) & (up_bb > up_kc)
    mid = ((ta.rolling_max(b.high, n) + ta.rolling_min(b.low, n)) / 2.0 + ma) / 2.0
    zero = np.zeros(b.n)
    return {"mom": ta.lsma(c - mid, n), "on": mask(zero, on), "off": mask(zero, off)}


@indicator(
    "rvi_vol",
    "Índice de volatilidad relativa",
    VOLATILITY,
    overlay=False,
    description="RSI aplicado a la desviación estándar (Dorsey): > 50 la volatilidad acompaña a las subidas, "
    "< 50 a las bajadas. Útil para confirmar rupturas.",
    params=[p_source(), p_int("std_length", "Periodo desviación", 10, 2, 200), p_len(14, 1, 200, label="Suavizado")],
    outputs=[line("rvi", "RVI volatilidad", PURPLE)],
    levels=[20.0, 50.0, 80.0],
    range=(0.0, 100.0),
)
def _rvi_vol(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    sd = ta.rolling_std(x, p["std_length"])
    ch = ta.diff(x, 1)
    ok = np.isfinite(sd) & np.isfinite(ch)
    up = ta.ema(np.where(ok, np.where(ch > 0, sd, 0.0), NAN), p["length"])
    dn = ta.ema(np.where(ok, np.where(ch <= 0, sd, 0.0), NAN), p["length"])
    out = 100.0 * ta.safe_div(up, up + dn, fill=50.0)
    return {"rvi": np.where(np.isfinite(up) & np.isfinite(dn), out, NAN)}


@indicator(
    "gk_vol",
    "Volatilidad Garman-Klass (anualizada %)",
    VOLATILITY,
    overlay=False,
    description="Estimador de volatilidad que usa apertura, máximo, mínimo y cierre: más eficiente que la "
    "volatilidad de cierre a cierre.",
    params=[p_len(20, 2, 500), _p_days()],
    outputs=[line("gk", "Garman-Klass %", TEAL)],
)
def _gk_vol(b: Bars, p: dict) -> dict:
    with np.errstate(divide="ignore", invalid="ignore"):
        hl = np.log(b.high / b.low)
        co = np.log(b.close / b.open)
    var_bar = 0.5 * hl * hl - (2.0 * math.log(2.0) - 1.0) * co * co
    var = np.maximum(ta.sma(var_bar, p["length"]), 0.0)
    return {"gk": np.sqrt(var * b.periods_per_year(p["days"])) * 100.0}
