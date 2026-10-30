# ─────────────────────────────────────────────────────────────────────────────
#  RSI con divergencias
#
#  Divergencia alcista: el precio marca un mínimo MÁS BAJO pero el RSI marca un
#  mínimo MÁS ALTO → la presión vendedora se agota.
#  Divergencia bajista: el precio marca un máximo MÁS ALTO pero el RSI uno MÁS
#  BAJO → la compra pierde fuerza.
#
#  Un pivote solo se confirma cuando pasan 'derecha' velas después de él, por
#  eso la marca aparece con ese retraso: es el precio de no mirar el futuro.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

indicator("RSI con divergencias", overlay=False)

longitud = input.int(14, "Longitud del RSI")
izquierda = input.int(5, "Pivote: velas a la izquierda")
derecha = input.int(3, "Pivote: velas a la derecha (confirmación)")
distancia_max = input.int(60, "Distancia máxima entre pivotes (velas)")
sobrecompra = input.float(70.0, "Sobrecompra")
sobreventa = input.float(30.0, "Sobreventa")

rsi = ta.rsi(close, longitud)

# ta.pivotlow / ta.pivothigh devuelven el valor del pivote en la vela donde se
# CONFIRMA (pivote + 'derecha' velas) y NaN en el resto.
piv_bajo = ta.pivotlow(rsi, izquierda, derecha)
piv_alto = ta.pivothigh(rsi, izquierda, derecha)

n = len(close)
div_alcista = np.zeros(n, dtype=bool)
div_bajista = np.zeros(n, dtype=bool)
prev_bajo = None  # (vela del pivote, rsi, precio)
prev_alto = None

for i in range(n):
    p = i - derecha  # vela donde estuvo el pivote (ya en el pasado)
    if p < 0:
        continue
    if not np.isnan(piv_bajo[i]):
        actual = (p, piv_bajo[i], low[p])
        if prev_bajo is not None and p - prev_bajo[0] <= distancia_max:
            if actual[2] < prev_bajo[2] and actual[1] > prev_bajo[1] and prev_bajo[1] < 50:
                div_alcista[i] = True
        prev_bajo = actual
    if not np.isnan(piv_alto[i]):
        actual = (p, piv_alto[i], high[p])
        if prev_alto is not None and p - prev_alto[0] <= distancia_max:
            if actual[2] > prev_alto[2] and actual[1] < prev_alto[1] and prev_alto[1] > 50:
                div_bajista[i] = True
        prev_alto = actual

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(rsi, "RSI", color=color.purple, linewidth=2, pane=1)
hline(sobrecompra, "Sobrecompra", color=color.red, style="dashed")
hline(50, "Neutral", color=color.gray, style="dotted")
hline(sobreventa, "Sobreventa", color=color.green, style="dashed")
bgcolor(rsi >= sobrecompra, color.red)
bgcolor(rsi <= sobreventa, color.green)

plotshape(div_alcista, "Divergencia alcista", location="belowBar", color=color.green, shape="arrowUp", text="Div+")
plotshape(div_bajista, "Divergencia bajista", location="aboveBar", color=color.red, shape="arrowDown", text="Div-")

alertcondition(div_alcista, "Divergencia alcista", "El precio hizo un mínimo más bajo y el RSI uno más alto")
alertcondition(div_bajista, "Divergencia bajista", "El precio hizo un máximo más alto y el RSI uno más bajo")

print("Divergencias alcistas:", int(np.sum(div_alcista)), "· bajistas:", int(np.sum(div_bajista)))
