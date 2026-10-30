# ─────────────────────────────────────────────────────────────────────────────
#  Squeeze de Bollinger
#
#  Cuando las bandas de Bollinger (volatilidad por desviación estándar) quedan
#  DENTRO del canal de Keltner (volatilidad por ATR), el mercado está
#  comprimido: suele preceder a un movimiento fuerte. La señal llega cuando la
#  compresión se libera; la dirección la da el histograma de momento.
#
#  Consejo: el squeeze dice CUÁNDO puede haber movimiento, no hacia dónde.
#  Espera la liberación y confirma con el momento antes de actuar.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

indicator("Squeeze de Bollinger", overlay=True)

longitud = input.int(20, "Longitud")
mult_bb = input.float(2.0, "Bollinger: desviaciones")
mult_kc = input.float(1.5, "Keltner: múltiplos de ATR")
mostrar_keltner = input.bool(True, "Mostrar canal de Keltner")

# ── Bandas (equivalen a ta.bb y ta.kc, escritas a mano para verlas) ─────────
media = ta.sma(close, longitud)
desv = ta.stdev(close, longitud)
bb_sup = media + mult_bb * desv
bb_inf = media - mult_bb * desv

kc_media = ta.ema(close, longitud)
rango = ta.atr(longitud)
kc_sup = kc_media + mult_kc * rango
kc_inf = kc_media - mult_kc * rango

# ── Estado del squeeze ──────────────────────────────────────────────────────
comprimido = (bb_sup < kc_sup) & (bb_inf > kc_inf)
previo = np.concatenate(([False], comprimido[:-1]))
liberacion = previo & ~comprimido  # primera vela tras la compresión

# Momento (estilo LazyBear): regresión lineal del precio respecto al centro
# del rango reciente. Positivo = presión compradora, negativo = vendedora.
centro = (ta.highest(high, longitud) + ta.lowest(low, longitud)) / 2
momento = ta.linreg(close - (centro + media) / 2, longitud, 0)
subiendo = momento > np.concatenate(([np.nan], momento[:-1]))

ruptura_alcista = liberacion & (momento > 0)
ruptura_bajista = liberacion & (momento < 0)

# Ancho de las bandas en % del precio: mínimos históricos = gran compresión
ancho = (bb_sup - bb_inf) / media * 100

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(bb_sup, "Bollinger superior", color=color.blue, linewidth=1)
plot(media, "Media", color=color.gray, linewidth=1)
plot(bb_inf, "Bollinger inferior", color=color.blue, linewidth=1)
if mostrar_keltner:
    plot(kc_sup, "Keltner superior", color=color.orange, linewidth=1, style="step")
    plot(kc_inf, "Keltner inferior", color=color.orange, linewidth=1, style="step")
bgcolor(comprimido, color.yellow)

# Histograma de momento en panel propio (4 colores): claro = acelera, oscuro = frena
positivo = momento >= 0
plot(np.where(positivo & subiendo, momento, np.nan), "Momento + acelerando", color=color.lime, style="histogram", pane=1)
plot(np.where(positivo & ~subiendo, momento, np.nan), "Momento + frenando", color=color.green, style="histogram", pane=1)
plot(np.where(~positivo & ~subiendo, momento, np.nan), "Momento - acelerando", color=color.red, style="histogram", pane=1)
plot(np.where(~positivo & subiendo, momento, np.nan), "Momento - frenando", color=color.orange, style="histogram", pane=1)
plot(np.where(comprimido, 0.0, np.nan), "Squeeze activo", color=color.yellow, style="circles", pane=1)
plot(ancho, "Ancho de bandas %", color=color.purple, linewidth=1, pane=2)

plotshape(ruptura_alcista, "Liberación alcista", location="belowBar", color=color.green, shape="arrowUp", text="SQZ")
plotshape(ruptura_bajista, "Liberación bajista", location="aboveBar", color=color.red, shape="arrowDown", text="SQZ")

alertcondition(ruptura_alcista, "Squeeze alcista", "Se liberó la compresión con momento positivo")
alertcondition(ruptura_bajista, "Squeeze bajista", "Se liberó la compresión con momento negativo")

en_curso = bool(comprimido[-1]) if len(comprimido) else False
print("Squeeze activo ahora:", "sí" if en_curso else "no", "· liberaciones:", int(np.sum(liberacion)))
