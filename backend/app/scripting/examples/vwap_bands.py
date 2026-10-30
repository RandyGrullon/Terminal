# ─────────────────────────────────────────────────────────────────────────────
#  Bandas VWAP
#
#  El VWAP (precio medio ponderado por volumen) de la sesión es el "precio
#  justo" que pagaron los participantes desde la apertura. Las bandas a 1 y 2
#  desviaciones (también ponderadas por volumen) marcan la zona de valor y las
#  extensiones: fuera de ±2σ el precio está estirado y suele volver al VWAP.
#
#  Sesión = día UTC en temporalidades intradía (o la semana, si lo eliges);
#  con velas diarias la sesión es el mes y con semanales, el año. Todo se
#  acumula vela a vela desde el inicio de la sesión: causal, sin mirar el futuro.
# ─────────────────────────────────────────────────────────────────────────────
import math

import numpy as np

indicator("Bandas VWAP", overlay=True)

mult1 = input.float(1.0, "Banda interior (desviaciones)")
mult2 = input.float(2.0, "Banda exterior (desviaciones)")
anclaje_semanal = input.bool(False, "Anclar por semana (temporalidades intradía)")
min_velas = input.int(4, "Velas mínimas de sesión para señales")

n = len(close)
precio = hlc3
paso = float(np.median(np.diff(time))) if n > 1 else 0.0
fechas = time.astype("datetime64[s]")
if paso >= 604800:  # velas semanales: sesión = año
    sesion, tipo_sesion = fechas.astype("datetime64[Y]").astype(np.int64), "anual"
elif paso >= 86400:  # velas diarias: sesión = mes
    sesion, tipo_sesion = fechas.astype("datetime64[M]").astype(np.int64), "mensual"
elif anclaje_semanal:
    # Lunes 00:00 UTC = 345600 s después del 1/1/1970 (jueves): las semanas empiezan en lunes
    sesion, tipo_sesion = (time - 345600) // 604800, "semanal"
else:
    sesion, tipo_sesion = time // 86400, "diaria"

con_volumen = bool(np.any(volume > 0))
peso = np.where(volume > 0, volume, 0.0) if con_volumen else np.ones(n)  # sin volumen → TWAP

vwap = np.full(n, np.nan)
desv = np.full(n, np.nan)
velas_sesion = np.zeros(n)  # velas acumuladas en la sesión actual
suma_pv = suma_v = suma_p2v = 0.0
for i in range(n):
    if i == 0 or sesion[i] != sesion[i - 1]:
        suma_pv = suma_v = suma_p2v = 0.0  # nueva sesión: se reinicia la acumulación
    velas_sesion[i] = 1 if i == 0 or sesion[i] != sesion[i - 1] else velas_sesion[i - 1] + 1
    suma_pv += precio[i] * peso[i]
    suma_v += peso[i]
    suma_p2v += precio[i] * precio[i] * peso[i]
    if suma_v > 0:
        media = suma_pv / suma_v
        vwap[i] = media
        desv[i] = math.sqrt(max(suma_p2v / suma_v - media * media, 0.0))

sup1, inf1 = vwap + mult1 * desv, vwap - mult1 * desv
sup2, inf2 = vwap + mult2 * desv, vwap - mult2 * desv

# Señales de reversión a la media: vuelve a entrar desde fuera de ±2σ. Al
# inicio de cada sesión las bandas aún son estrechas (pocas velas), así que se
# exige un mínimo de velas acumuladas para no dar señales falsas.
madura = velas_sesion >= min_velas
rebote_inferior = ta.crossover(close, inf2) & madura
rechazo_superior = ta.crossunder(close, sup2) & madura
sobre_vwap = close > vwap

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(vwap, "VWAP", color=color.orange, linewidth=2)
plot(sup1, "+1σ", color=color.blue, linewidth=1)
plot(inf1, "-1σ", color=color.blue, linewidth=1)
plot(sup2, "+2σ", color=color.purple, linewidth=1, style="step")
plot(inf2, "-2σ", color=color.purple, linewidth=1, style="step")
bgcolor(((close > sup2) | (close < inf2)) & madura, color.yellow)  # precio estirado

plotshape(rebote_inferior, "Vuelve desde -2σ", location="belowBar", color=color.green, shape="arrowUp", text="2σ")
plotshape(rechazo_superior, "Vuelve desde +2σ", location="aboveBar", color=color.red, shape="arrowDown", text="2σ")

alertcondition(rebote_inferior, "Rebote en -2σ", "El precio volvió a entrar desde la banda inferior")
alertcondition(rechazo_superior, "Rechazo en +2σ", "El precio volvió a entrar desde la banda superior")
alertcondition(ta.cross(close, vwap), "Cruce del VWAP", "El precio cruzó el VWAP de la sesión")

if not con_volumen:
    print("Este activo no publica volumen: se usa el precio medio de la sesión (TWAP)")
if n and not np.isnan(vwap[-1]):
    distancia = (close[-1] - vwap[-1]) / desv[-1] if desv[-1] > 0 else 0.0
    print("Sesión", tipo_sesion, "· precio a", round(float(distancia), 2), "σ del VWAP",
          "(por encima)" if sobre_vwap[-1] else "(por debajo)")
