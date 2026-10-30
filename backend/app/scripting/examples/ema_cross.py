# ─────────────────────────────────────────────────────────────────────────────
#  Cruce de EMAs (estrategia)
#
#  Idea: comprar cuando la EMA rápida cruza por encima de la lenta, pero SOLO si
#  el precio está por encima de la EMA de tendencia (no se opera contra la
#  tendencia mayor). Cada operación lleva un stop "hasta aquí" a N ATR y un
#  objetivo en múltiplos de R (R = distancia al stop). Se cierra antes si la EMA
#  rápida vuelve a cruzar por debajo de la lenta.
#
#  Las órdenes se ejecutan en la apertura de la vela siguiente a la señal, así
#  que el backtest no usa información del futuro.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

strategy("Cruce de EMAs", initial_capital=10000, risk_pct=1, fee_pct=0.1)

# ── Entradas (editables desde el panel sin tocar el código) ─────────────────
rapida = input.int(9, "EMA rápida")
lenta = input.int(21, "EMA lenta")
tendencia = input.int(200, "EMA de tendencia")
stop_atr = input.float(2.0, "Stop (múltiplos de ATR)")
objetivo_r = input.float(2.0, "Objetivo (R)")
permitir_cortos = input.bool(False, "Permitir cortos")

# ── Cálculos ────────────────────────────────────────────────────────────────
ema_r = ta.ema(close, rapida)
ema_l = ta.ema(close, lenta)
ema_t = ta.ema(close, tendencia)
atr = ta.atr(14)

cruce_arriba = ta.crossover(ema_r, ema_l)
cruce_abajo = ta.crossunder(ema_r, ema_l)

# Filtro de tendencia: solo largos sobre la EMA 200 y cortos bajo ella
largo = cruce_arriba & (close > ema_t)
corto = cruce_abajo & (close < ema_t)

# Niveles por vela: el motor toma el stop y el objetivo de la vela de la señal
# y los deja FIJOS mientras la operación está abierta (con update=True se
# recalcularían en cada vela, como en Pine).
riesgo = stop_atr * atr

# ── Reglas de la estrategia ────────────────────────────────────────────────
strategy.entry("Largo", "long", when=largo)
strategy.exit("Salida largo", from_entry="Largo", stop=close - riesgo, limit=close + objetivo_r * riesgo)
strategy.close("Largo", when=cruce_abajo)

if permitir_cortos:
    strategy.entry("Corto", "short", when=corto)
    strategy.exit("Salida corto", from_entry="Corto", stop=close + riesgo, limit=close - objetivo_r * riesgo)
    strategy.close("Corto", when=cruce_arriba)

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(ema_r, "EMA rápida", color=color.blue, linewidth=2)
plot(ema_l, "EMA lenta", color=color.orange, linewidth=2)
plot(ema_t, "EMA tendencia", color=color.gray, linewidth=1)

# Cruces que el filtro de tendencia descartó (no se operan): útil para ver qué evita
descartado = (cruce_arriba & (close <= ema_t)) | (cruce_abajo & (close >= ema_t))
plotshape(descartado, "Cruce descartado por tendencia", location="aboveBar", color=color.gray, shape="circle", text="x")

alertcondition(largo, "Compra", "Cruce alcista de EMAs a favor de la tendencia mayor")
alertcondition(cruce_abajo, "Cierre largo", "La EMA rápida cruzó por debajo de la lenta")

print("Señales de compra:", int(np.sum(largo)), "· descartadas por tendencia:", int(np.sum(descartado)))
