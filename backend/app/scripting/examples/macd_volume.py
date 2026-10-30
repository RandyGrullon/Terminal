# ─────────────────────────────────────────────────────────────────────────────
#  MACD + volumen
#
#  Un cruce del MACD con su señal indica un cambio de momento, pero muchos son
#  ruido. Aquí solo cuentan los cruces acompañados de volumen relativo alto
#  (volumen de la vela / su media), y opcionalmente a favor de la EMA 200.
#  Los cruces SIN volumen se marcan en gris: mejor no fiarse de ellos.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

indicator("MACD + volumen", overlay=False)

rapida = input.int(12, "EMA rápida")
lenta = input.int(26, "EMA lenta")
senal_n = input.int(9, "Línea de señal")
vol_n = input.int(20, "Media de volumen")
vol_min = input.float(1.3, "Volumen relativo mínimo")
solo_tendencia = input.bool(True, "Solo a favor de la EMA 200")

macd, senal, hist = ta.macd(close, rapida, lenta, senal_n)
ema200 = ta.ema(close, 200)

# Volumen relativo (1.0 = volumen normal). Sin división entre cero.
n = len(close)
base_vol = ta.sma(volume, vol_n)
vol_rel = np.divide(volume, base_vol, out=np.full(n, np.nan), where=base_vol > 0)
sin_volumen = not bool(np.any(volume > 0))
volumen_ok = np.ones(n, dtype=bool) if sin_volumen else (vol_rel >= vol_min)

cruce_alcista = ta.crossover(macd, senal)
cruce_bajista = ta.crossunder(macd, senal)
if solo_tendencia:
    cruce_alcista = cruce_alcista & (close > ema200)
    cruce_bajista = cruce_bajista & (close < ema200)
compra = cruce_alcista & volumen_ok
venta = cruce_bajista & volumen_ok
# Cruces válidos por dirección pero SIN volumen: los que este filtro evita
debiles = (cruce_alcista | cruce_bajista) & ~volumen_ok

# ── Dibujo: MACD en el panel 1, volumen relativo en el panel 2 ──────────────
plot(np.where(hist >= 0, hist, np.nan), "Histograma +", color=color.green, style="histogram", pane=1)
plot(np.where(hist < 0, hist, np.nan), "Histograma -", color=color.red, style="histogram", pane=1)
plot(macd, "MACD", color=color.blue, linewidth=2, pane=1)
plot(senal, "Señal", color=color.orange, linewidth=1, pane=1)
hline(0, "Cero", color=color.gray, style="dotted")

if not sin_volumen:
    plot(vol_rel, "Volumen relativo", color=color.teal, style="columns", pane=2)
    plot(np.full(n, vol_min), "Umbral de volumen", color=color.gray, linewidth=1, pane=2)

plotshape(compra, "Compra confirmada", location="belowBar", color=color.green, shape="arrowUp", text="MACD+V")
plotshape(venta, "Venta confirmada", location="aboveBar", color=color.red, shape="arrowDown", text="MACD+V")
plotshape(debiles, "Cruce sin volumen (descartado)", location="aboveBar", color=color.gray, shape="circle", text="?")

alertcondition(compra, "Compra MACD + volumen", "Cruce alcista del MACD con volumen alto")
alertcondition(venta, "Venta MACD + volumen", "Cruce bajista del MACD con volumen alto")

if sin_volumen:
    print("Este activo no publica volumen: el filtro de volumen queda desactivado")
print("Cruces confirmados:", int(np.sum(compra) + np.sum(venta)), "· descartados:", int(np.sum(debiles)))
