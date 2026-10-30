"""Volume indicators (category Volumen). Assets without volume data (indices, forex) yield NaN/flat series."""
from __future__ import annotations

import numpy as np

from app.engine import ta
from app.engine.indicators_core import (
    CYAN,
    INFO,
    NEG,
    ORANGE,
    POS,
    PURPLE,
    ROSE,
    TEAL,
    VOLUME,
    WARN,
    Bars,
    hist,
    indicator,
    line,
    mask,
    p_int,
    p_len,
)


@indicator(
    "volume",
    "Volumen con media",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Volumen por vela (verde si cerró al alza, rojo si a la baja) y su media. Rupturas con volumen "
    "por encima de la media son más fiables.",
    params=[p_int("ma_length", "Periodo de la media", 20, 1, 500)],
    outputs=[hist("up", "Volumen alcista", POS), hist("down", "Volumen bajista", NEG), line("ma", "Media", WARN)],
)
def _volume(b: Bars, p: dict) -> dict:
    v = b.volume
    up = b.close >= b.open
    return {"up": mask(v, up), "down": mask(v, ~up), "ma": ta.sma(v, p["ma_length"])}


@indicator(
    "obv",
    "Volumen en balance (OBV)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Suma el volumen de las velas alcistas y resta el de las bajistas. Si el OBV marca máximos antes "
    "que el precio, hay acumulación.",
    params=[p_int("ma_length", "Periodo de la media", 20, 1, 500)],
    outputs=[line("obv", "OBV", INFO), line("ma", "Media", WARN)],
)
def _obv(b: Bars, p: dict) -> dict:
    o = ta.obv(b.close, b.volume)
    return {"obv": o, "ma": ta.sma(o, p["ma_length"])}


@indicator(
    "ad",
    "Línea de acumulación/distribución",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Volumen ponderado por la posición del cierre dentro del rango de la vela (Chaikin). "
    "Sube con acumulación, baja con distribución.",
    params=[],
    outputs=[line("ad", "A/D", TEAL)],
)
def _ad(b: Bars, p: dict) -> dict:
    return {"ad": ta.ad_line(b.high, b.low, b.close, b.volume)}


@indicator(
    "cmf",
    "Flujo de dinero de Chaikin (CMF)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Promedio del flujo de dinero de N velas (−1..1). > 0 presión compradora sostenida, < 0 vendedora.",
    params=[p_len(20, 1, 500)],
    outputs=[hist("cmf", "CMF", POS)],
    levels=[0.0],
)
def _cmf(b: Bars, p: dict) -> dict:
    return {"cmf": ta.cmf(b.high, b.low, b.close, b.volume, p["length"])}


@indicator(
    "chaikin_osc",
    "Oscilador de Chaikin",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="EMA rápida − EMA lenta de la línea A/D: mide la aceleración de la acumulación/distribución.",
    params=[p_int("fast", "Periodo rápido", 3, 1, 100), p_int("slow", "Periodo lento", 10, 2, 200)],
    outputs=[line("osc", "Chaikin", ORANGE)],
    levels=[0.0],
)
def _chaikin_osc(b: Bars, p: dict) -> dict:
    ad = ta.ad_line(b.high, b.low, b.close, b.volume)
    return {"osc": ta.ema(ad, p["fast"]) - ta.ema(ad, p["slow"])}


@indicator(
    "mfi",
    "Índice de flujo de dinero (MFI)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="RSI ponderado por volumen (0–100). > 80 sobrecompra, < 20 sobreventa; las divergencias con el "
    "precio son especialmente significativas.",
    params=[p_len(14, 1, 200)],
    outputs=[line("mfi", "MFI", CYAN)],
    levels=[20.0, 50.0, 80.0],
    range=(0.0, 100.0),
)
def _mfi(b: Bars, p: dict) -> dict:
    return {"mfi": ta.mfi(b.high, b.low, b.close, b.volume, p["length"])}


@indicator(
    "force",
    "Índice de fuerza (Elder)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Cambio de precio × volumen suavizado: combina dirección, magnitud y participación.",
    params=[p_len(13, 1, 200)],
    outputs=[line("efi", "Fuerza", ROSE)],
    levels=[0.0],
)
def _force(b: Bars, p: dict) -> dict:
    return {"efi": ta.force_index(b.close, b.volume, p["length"])}


@indicator(
    "eom",
    "Facilidad de movimiento (EOM)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Cuánto se mueve el precio por unidad de volumen. Valores altos = el precio sube con poco esfuerzo.",
    params=[p_len(14, 1, 200), p_int("divisor", "Divisor de volumen", 10000, 1, 1_000_000_000, 1000)],
    outputs=[line("eom", "EOM", INFO)],
    levels=[0.0],
)
def _eom(b: Bars, p: dict) -> dict:
    return {"eom": ta.ease_of_movement(b.high, b.low, b.volume, p["length"], float(p["divisor"]))}


@indicator(
    "vol_osc",
    "Oscilador de volumen (%)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Diferencia porcentual entre una EMA rápida y una lenta del volumen: > 0 la participación crece.",
    params=[p_int("fast", "Periodo rápido", 5, 1, 100), p_int("slow", "Periodo lento", 10, 2, 300)],
    outputs=[line("osc", "Osc. volumen %", ORANGE)],
    levels=[0.0],
)
def _vol_osc(b: Bars, p: dict) -> dict:
    slow = ta.ema(b.volume, p["slow"])
    return {"osc": 100.0 * ta.safe_div(ta.ema(b.volume, p["fast"]) - slow, slow)}


@indicator(
    "pvt",
    "Tendencia precio-volumen (PVT)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Volumen acumulado ponderado por el cambio porcentual del precio. Confirma (o no) la tendencia.",
    params=[p_int("ma_length", "Periodo de la media", 20, 1, 500)],
    outputs=[line("pvt", "PVT", TEAL), line("ma", "Media", WARN)],
)
def _pvt(b: Bars, p: dict) -> dict:
    v = ta.pvt(b.close, b.volume)
    return {"pvt": v, "ma": ta.sma(v, p["ma_length"])}


@indicator(
    "nvi",
    "Índice de volumen negativo (NVI)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Solo cambia en velas con menos volumen que la anterior («dinero inteligente»). NVI sobre su "
    "EMA larga = mercado alcista probable.",
    params=[p_int("ema_length", "Periodo EMA", 255, 1, 500)],
    outputs=[line("nvi", "NVI", INFO), line("ema", "EMA", WARN)],
)
def _nvi(b: Bars, p: dict) -> dict:
    nvi, _ = ta.nvi_pvi(b.close, b.volume)
    return {"nvi": nvi, "ema": ta.ema(nvi, p["ema_length"])}


@indicator(
    "pvi",
    "Índice de volumen positivo (PVI)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Solo cambia en velas con más volumen que la anterior (participación de la masa). "
    "PVI sobre su EMA larga = tendencia alcista respaldada.",
    params=[p_int("ema_length", "Periodo EMA", 255, 1, 500)],
    outputs=[line("pvi", "PVI", PURPLE), line("ema", "EMA", WARN)],
)
def _pvi(b: Bars, p: dict) -> dict:
    _, pvi = ta.nvi_pvi(b.close, b.volume)
    return {"pvi": pvi, "ema": ta.ema(pvi, p["ema_length"])}


@indicator(
    "klinger",
    "Oscilador de Klinger",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Compara el flujo de volumen de largo y corto plazo para detectar cambios de tendencia del dinero.",
    params=[
        p_int("fast", "Periodo rápido", 34, 1, 200),
        p_int("slow", "Periodo lento", 55, 2, 300),
        p_int("signal", "Periodo de señal", 13, 1, 100),
    ],
    outputs=[line("kvo", "KVO", INFO), line("signal", "Señal", ORANGE)],
    levels=[0.0],
)
def _klinger(b: Bars, p: dict) -> dict:
    kvo, sig = ta.klinger(b.high, b.low, b.close, b.volume, p["fast"], p["slow"], p["signal"])
    return {"kvo": kvo, "signal": sig}


@indicator(
    "rvol",
    "Volumen relativo (RVOL)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Volumen de la vela dividido por la media de las N velas anteriores. > 2 = actividad inusual "
    "(noticias, rupturas); < 0,5 = mercado dormido.",
    params=[p_len(20, 1, 500)],
    outputs=[hist("rvol", "RVOL", INFO)],
    levels=[1.0, 2.0],
)
def _rvol(b: Bars, p: dict) -> dict:
    prev = ta.shift(ta.sma(b.volume, p["length"]), 1)
    return {"rvol": ta.safe_div(b.volume, np.where(prev > 0, prev, np.nan))}


@indicator(
    "vzo",
    "Oscilador de zona de volumen (VZO)",
    VOLUME,
    overlay=False,
    needs_volume=True,
    description="Volumen con signo (según suba o baje el cierre) sobre el volumen total suavizado (−100..100). "
    "> 40 zona alcista, < −40 bajista.",
    params=[p_len(14, 2, 200)],
    outputs=[line("vzo", "VZO", PURPLE)],
    levels=[-40.0, 0.0, 40.0],
    range=(-100.0, 100.0),
)
def _vzo(b: Bars, p: dict) -> dict:
    d = ta.diff(b.close, 1)
    signed = np.where(np.isfinite(d), np.sign(d) * b.volume, np.nan)
    vp = ta.ema(signed, p["length"])
    tv = ta.ema(np.where(np.isfinite(d), b.volume, np.nan), p["length"])
    return {"vzo": 100.0 * ta.safe_div(vp, tv)}
