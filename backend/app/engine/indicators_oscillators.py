"""Momentum oscillators (category Momento), part 2: KST, PPO, DPO, Coppock, Connors RSI, Fisher, STC, Elder ray..."""
from __future__ import annotations

import math

import numpy as np

from app.engine import ta
from app.engine.indicators_core import (
    INFO,
    MOMENTUM,
    NAN,
    NEG,
    ORANGE,
    POS,
    PURPLE,
    TEAL,
    WARN,
    Bars,
    hist,
    indicator,
    line,
    p_float,
    p_int,
    p_len,
    p_source,
)


@indicator(
    "kst",
    "Know Sure Thing (KST)",
    MOMENTUM,
    overlay=False,
    description="Suma ponderada de cuatro ROC suavizados (Martin Pring): resume el momento de varios horizontes. "
    "Cruce de KST sobre su señal = compra.",
    params=[
        p_int("roc1", "ROC 1", 10, 1, 200),
        p_int("roc2", "ROC 2", 15, 1, 200),
        p_int("roc3", "ROC 3", 20, 1, 200),
        p_int("roc4", "ROC 4", 30, 1, 300),
        p_int("sma1", "Media 1", 10, 1, 200),
        p_int("sma2", "Media 2", 10, 1, 200),
        p_int("sma3", "Media 3", 10, 1, 200),
        p_int("sma4", "Media 4", 15, 1, 200),
        p_int("signal", "Periodo de señal", 9, 1, 100),
    ],
    outputs=[line("kst", "KST", POS), line("signal", "Señal", NEG)],
    levels=[0.0],
)
def _kst(b: Bars, p: dict) -> dict:
    c = b.close
    k = sum(w * ta.sma(ta.roc(c, p[f"roc{w}"]), p[f"sma{w}"]) for w in (1, 2, 3, 4))
    return {"kst": k, "signal": ta.sma(k, p["signal"])}


@indicator(
    "ppo",
    "Oscilador de precio porcentual (PPO)",
    MOMENTUM,
    overlay=False,
    description="MACD expresado en % del precio: permite comparar activos de precios muy distintos.",
    params=[
        p_source(),
        p_int("fast", "Periodo rápido", 12, 1, 200),
        p_int("slow", "Periodo lento", 26, 2, 400),
        p_int("signal", "Periodo de señal", 9, 1, 200),
    ],
    outputs=[hist("hist", "Histograma", TEAL), line("ppo", "PPO %", INFO), line("signal", "Señal", ORANGE)],
    levels=[0.0],
)
def _ppo(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    slow = ta.ema(x, p["slow"])
    ppo = 100.0 * ta.safe_div(ta.ema(x, p["fast"]) - slow, slow)
    sig = ta.ema(ppo, p["signal"])
    return {"hist": ppo - sig, "ppo": ppo, "signal": sig}


@indicator(
    "dpo",
    "Oscilador de precio sin tendencia (DPO)",
    MOMENTUM,
    overlay=False,
    description="Precio menos una SMA desplazada N/2+1 velas: elimina la tendencia y deja ver los ciclos. "
    "Versión no centrada (causal).",
    params=[p_source(), p_len(21, 2, 500)],
    outputs=[line("dpo", "DPO", INFO)],
    levels=[0.0],
)
def _dpo(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    n = p["length"]
    return {"dpo": x - ta.shift(ta.sma(x, n), n // 2 + 1)}


@indicator(
    "coppock",
    "Curva de Coppock",
    MOMENTUM,
    overlay=False,
    description="WMA de la suma de dos ROC largos. Pensada para detectar suelos de mercado: el giro al alza "
    "desde valores negativos es la señal clásica de compra.",
    params=[
        p_source(),
        p_int("wma", "Periodo WMA", 10, 1, 200),
        p_int("long_roc", "ROC largo", 14, 1, 300),
        p_int("short_roc", "ROC corto", 11, 1, 300),
    ],
    outputs=[hist("coppock", "Coppock", POS)],
    levels=[0.0],
)
def _coppock(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    return {"coppock": ta.wma(ta.roc(x, p["long_roc"]) + ta.roc(x, p["short_roc"]), p["wma"])}


@indicator(
    "crsi",
    "RSI de Connors",
    MOMENTUM,
    overlay=False,
    description="Media de tres componentes: RSI corto, RSI de la racha de subidas/bajadas y rango percentil del "
    "retorno. Diseñado para reversiones a la media de corto plazo (< 10 sobreventa extrema, > 90 sobrecompra).",
    params=[
        p_source(),
        p_int("rsi_length", "Periodo RSI", 3, 2, 50),
        p_int("streak_length", "Periodo RSI de racha", 2, 2, 50),
        p_int("rank_length", "Periodo rango percentil", 100, 10, 500),
    ],
    outputs=[line("crsi", "CRSI", PURPLE)],
    levels=[10.0, 30.0, 70.0, 90.0],
    range=(0.0, 100.0),
)
def _crsi(b: Bars, p: dict) -> dict:
    x = b.src(p["source"])
    a = ta.rsi(x, p["rsi_length"])
    s = ta.rsi(ta.streak(x), p["streak_length"])
    r = ta.percent_rank(ta.roc(x, 1), p["rank_length"])
    return {"crsi": (a + s + r) / 3.0}


@indicator(
    "fisher",
    "Transformada de Fisher",
    MOMENTUM,
    overlay=False,
    description="Convierte el precio en una distribución casi normal: los extremos (±1,5) marcan giros con "
    "claridad. Cruce con el disparador = señal.",
    params=[p_len(9, 2, 200)],
    outputs=[line("fisher", "Fisher", INFO), line("trigger", "Disparador", WARN)],
    levels=[-1.5, 0.0, 1.5],
)
def _fisher(b: Bars, p: dict) -> dict:
    f, t = ta.fisher(b.high, b.low, p["length"])
    return {"fisher": f, "trigger": t}


@indicator(
    "stc",
    "Ciclo de tendencia de Schaff (STC)",
    MOMENTUM,
    overlay=False,
    description="MACD pasado dos veces por un estocástico: oscila 0–100 y gira antes que el MACD. "
    "Sube desde < 25 = compra; cae desde > 75 = venta.",
    params=[
        p_source(),
        p_int("fast", "Periodo rápido", 23, 1, 200),
        p_int("slow", "Periodo lento", 50, 2, 400),
        p_int("cycle", "Ciclo", 10, 2, 100),
    ],
    outputs=[line("stc", "STC", PURPLE)],
    levels=[25.0, 75.0],
    range=(0.0, 100.0),
)
def _stc(b: Bars, p: dict) -> dict:
    return {"stc": ta.schaff(b.src(p["source"]), p["fast"], p["slow"], p["cycle"])}


@indicator(
    "elder_ray",
    "Elder Ray (poder alcista / bajista)",
    MOMENTUM,
    overlay=False,
    description="Máximo − EMA (poder alcista) y mínimo − EMA (poder bajista). Compras cuando la EMA sube y el "
    "poder bajista es negativo pero se recupera.",
    params=[p_len(13, 1, 200)],
    outputs=[line("bull", "Poder alcista", POS), line("bear", "Poder bajista", NEG)],
    levels=[0.0],
)
def _elder_ray(b: Bars, p: dict) -> dict:
    e = ta.ema(b.close, p["length"])
    return {"bull": b.high - e, "bear": b.low - e}


@indicator(
    "bop",
    "Balance de poder (BOP)",
    MOMENTUM,
    overlay=False,
    description="(Cierre − apertura) / (máximo − mínimo) suavizado: quién controla las velas, compradores (> 0) "
    "o vendedores (< 0).",
    params=[p_len(14, 1, 200, label="Suavizado")],
    outputs=[hist("bop", "BOP", POS)],
    levels=[0.0],
    range=(-1.0, 1.0),
)
def _bop(b: Bars, p: dict) -> dict:
    raw = ta.safe_div(b.close - b.open, b.high - b.low, fill=0.0)
    return {"bop": ta.sma(raw, p["length"])}


@indicator(
    "cmo",
    "Oscilador de momento de Chande (CMO)",
    MOMENTUM,
    overlay=False,
    description="(Subidas − bajadas) / (subidas + bajadas) × 100 en N velas. > +50 sobrecompra, < −50 sobreventa.",
    params=[p_source(), p_len(9, 1, 200)],
    outputs=[line("cmo", "CMO", INFO)],
    levels=[-50.0, 0.0, 50.0],
    range=(-100.0, 100.0),
)
def _cmo(b: Bars, p: dict) -> dict:
    return {"cmo": ta.cmo(b.src(p["source"]), p["length"])}


# --- end of oscillators part A ---


@indicator(
    "rvgi",
    "Índice de vigor relativo (RVI)",
    MOMENTUM,
    overlay=False,
    description="Compara el cuerpo de las velas (cierre − apertura) con su rango: en tendencias alcistas los "
    "cierres superan a las aperturas. Cruce del RVI sobre su señal = impulso comprador.",
    params=[p_len(10, 1, 200)],
    outputs=[line("rvi", "RVI", POS), line("signal", "Señal", NEG)],
    levels=[0.0],
)
def _rvgi(b: Bars, p: dict) -> dict:
    num = ta.swma(b.close - b.open)
    den = ta.swma(b.high - b.low)
    rvi = ta.safe_div(ta.rolling_sum(num, p["length"]), ta.rolling_sum(den, p["length"]), fill=0.0)
    rvi = np.where(np.isfinite(ta.rolling_sum(den, p["length"])), rvi, NAN)
    return {"rvi": rvi, "signal": ta.swma(rvi)}


@indicator(
    "smi",
    "Índice de momento estocástico (SMI)",
    MOMENTUM,
    overlay=False,
    description="Estocástico centrado de Blau: distancia del cierre al punto medio del rango, doblemente "
    "suavizada (−100..100). > 40 sobrecompra, < −40 sobreventa.",
    params=[
        p_len(10, 1, 200, label="Periodo %K"),
        p_int("smooth", "Suavizado %D", 3, 1, 50),
        p_int("signal", "Periodo de señal", 10, 1, 100),
    ],
    outputs=[line("smi", "SMI", INFO), line("signal", "Señal", ORANGE)],
    levels=[-40.0, 0.0, 40.0],
    range=(-100.0, 100.0),
)
def _smi(b: Bars, p: dict) -> dict:
    hh = ta.rolling_max(b.high, p["length"])
    ll = ta.rolling_min(b.low, p["length"])
    d = p["smooth"]
    rel = ta.ema(ta.ema(b.close - (hh + ll) / 2.0, d), d)
    rng = ta.ema(ta.ema(hh - ll, d), d)
    smi = 200.0 * ta.safe_div(rel, rng, fill=0.0)
    smi = np.where(np.isfinite(rng), smi, NAN)
    return {"smi": smi, "signal": ta.ema(smi, p["signal"])}


@indicator(
    "wavetrend",
    "WaveTrend",
    MOMENTUM,
    overlay=False,
    description="Oscilador de canal sobre el precio típico (LazyBear). Cruces de WT1 y WT2 en zona extrema "
    "(±60) suelen anticipar giros.",
    params=[p_int("channel", "Periodo del canal", 10, 1, 200), p_int("average", "Periodo medio", 21, 1, 200)],
    outputs=[line("wt1", "WT1", POS), line("wt2", "WT2", NEG)],
    levels=[-60.0, 0.0, 60.0],
)
def _wavetrend(b: Bars, p: dict) -> dict:
    ap = b.hlc3
    esa = ta.ema(ap, p["channel"])
    dev = ta.ema(np.abs(ap - esa), p["channel"])
    ci = ta.safe_div(ap - esa, 0.015 * dev, fill=0.0)
    ci = np.where(np.isfinite(dev), ci, NAN)
    wt1 = ta.ema(ci, p["average"])
    return {"wt1": wt1, "wt2": ta.sma(wt1, 4)}


def _crossed(a: float, a_prev: float, b: float, b_prev: float) -> bool:
    if not all(math.isfinite(v) for v in (a, a_prev, b, b_prev)):
        return False
    return (a > b and a_prev <= b_prev) or (a < b and a_prev >= b_prev)


@indicator(
    "qqe",
    "QQE (Quantitative Qualitative Estimation)",
    MOMENTUM,
    overlay=False,
    description="RSI suavizado con una línea de arrastre basada en su volatilidad (ATR del RSI). "
    "RSI por encima de la línea = sesgo alcista; los cruces marcan cambios de tendencia del momento.",
    params=[
        p_source(),
        p_len(14, 2, 100, label="Periodo RSI"),
        p_int("smooth", "Suavizado", 5, 1, 50),
        p_float("factor", "Factor QQE", 4.236, 0.5, 10.0, 0.001),
    ],
    outputs=[line("rsi_ma", "RSI suavizado", PURPLE), line("trail", "Línea QQE", WARN)],
    levels=[30.0, 50.0, 70.0],
)
def _qqe(b: Bars, p: dict) -> dict:
    wp = p["length"] * 2 - 1
    rsi_ma = ta.ema(ta.rsi(b.src(p["source"]), p["length"]), p["smooth"])
    dar = ta.ema(ta.ema(np.abs(ta.diff(rsi_ma, 1)), wp), wp) * p["factor"]
    rm, dr = rsi_ma.tolist(), dar.tolist()
    m = len(rm)
    lb = [NAN] * m
    sb = [NAN] * m
    trail = [NAN] * m
    trend = 1
    for i in range(m):
        r, d = rm[i], dr[i]
        if r != r or d != d:
            continue
        rp = rm[i - 1] if i > 0 else NAN
        lbp = lb[i - 1] if i > 0 else NAN
        sbp = sb[i - 1] if i > 0 else NAN
        lbp2 = lb[i - 2] if i > 1 else NAN
        sbp2 = sb[i - 2] if i > 1 else NAN
        new_long, new_short = r - d, r + d
        lb_i = max(lbp, new_long) if (rp == rp and lbp == lbp and rp > lbp and r > lbp) else new_long
        sb_i = min(sbp, new_short) if (rp == rp and sbp == sbp and rp < sbp and r < sbp) else new_short
        if _crossed(r, rp, sbp, sbp2):
            trend = 1
        elif _crossed(lbp, lbp2, r, rp):
            trend = -1
        lb[i], sb[i] = lb_i, sb_i
        trail[i] = lb_i if trend == 1 else sb_i
    return {"rsi_ma": rsi_ma, "trail": np.array(trail)}
