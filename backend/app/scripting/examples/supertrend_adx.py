# ─────────────────────────────────────────────────────────────────────────────
#  Supertrend + filtro ADX (estrategia)
#
#  Supertrend sigue la tendencia con una banda a 'factor' ATR del precio medio:
#  mientras el cierre no la cruce, la tendencia sigue. Aquí se calcula paso a
#  paso (vela a vela) para que veas cómo funciona; ta.supertrend(factor, periodo)
#  hace lo mismo en una línea.
#
#  Filtro: el ADX mide la FUERZA de la tendencia (no su dirección). Con ADX bajo
#  el mercado está lateral y Supertrend da muchas señales falsas → no se opera.
#  La línea de Supertrend es el stop "hasta aquí": si se cruza, se sale.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

strategy("Supertrend + ADX", initial_capital=10000, risk_pct=1, fee_pct=0.1)

periodo = input.int(10, "Periodo ATR")
factor = input.float(3.0, "Factor (múltiplos de ATR)")
adx_min = input.float(20.0, "ADX mínimo para operar")
permitir_cortos = input.bool(True, "Permitir cortos")

atr = ta.atr(periodo)
adx = ta.adx(14)

# ── Supertrend vela a vela (solo usa la vela actual y las anteriores) ───────
n = len(close)
banda_inf = np.full(n, np.nan)  # soporte en tendencia alcista
banda_sup = np.full(n, np.nan)  # resistencia en tendencia bajista
direccion = np.zeros(n)         # 1 alcista, -1 bajista, 0 sin datos
linea = np.full(n, np.nan)

for i in range(n):
    if np.isnan(atr[i]):
        continue
    base_inf = hl2[i] - factor * atr[i]
    base_sup = hl2[i] + factor * atr[i]
    if i == 0 or direccion[i - 1] == 0:
        banda_inf[i], banda_sup[i], direccion[i] = base_inf, base_sup, 1
    else:
        # Las bandas solo se mueven a favor de la tendencia (como un trailing stop)
        banda_inf[i] = max(base_inf, banda_inf[i - 1]) if close[i - 1] > banda_inf[i - 1] else base_inf
        banda_sup[i] = min(base_sup, banda_sup[i - 1]) if close[i - 1] < banda_sup[i - 1] else base_sup
        if direccion[i - 1] < 0 and close[i] > banda_sup[i - 1]:
            direccion[i] = 1
        elif direccion[i - 1] > 0 and close[i] < banda_inf[i - 1]:
            direccion[i] = -1
        else:
            direccion[i] = direccion[i - 1]
    linea[i] = banda_inf[i] if direccion[i] > 0 else banda_sup[i]

# ── Señales: la primera vela en la que tendencia y fuerza coinciden ─────────
fuerte = adx >= adx_min
cond_larga = (direccion > 0) & fuerte
cond_corta = (direccion < 0) & fuerte
previa_larga = np.concatenate(([False], cond_larga[:-1]))
previa_corta = np.concatenate(([False], cond_corta[:-1]))
entrada_larga = cond_larga & ~previa_larga
entrada_corta = cond_corta & ~previa_corta

giro_bajista = (direccion < 0) & (np.concatenate(([0.0], direccion[:-1])) > 0)
giro_alcista = (direccion > 0) & (np.concatenate(([0.0], direccion[:-1])) < 0)

# update=True: el stop se recalcula en cada vela y sigue a la línea de
# Supertrend (stop dinámico). Sin update el nivel quedaría fijo desde la señal.
strategy.entry("Largo", "long", when=entrada_larga)
strategy.exit("Stop largo", from_entry="Largo", stop=linea, update=True)
strategy.close("Largo", when=giro_bajista)

if permitir_cortos:
    strategy.entry("Corto", "short", when=entrada_corta)
    strategy.exit("Stop corto", from_entry="Corto", stop=linea, update=True)
    strategy.close("Corto", when=giro_alcista)

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(np.where(direccion > 0, linea, np.nan), "Supertrend alcista", color=color.green, linewidth=2)
plot(np.where(direccion < 0, linea, np.nan), "Supertrend bajista", color=color.red, linewidth=2)
plot(adx, "ADX", color=color.orange, linewidth=1, pane=1)
plot(np.full(n, adx_min), "ADX mínimo", color=color.gray, linewidth=1, pane=1)  # hline() iría al panel del precio
bgcolor(~fuerte & (direccion != 0), color.gray)  # gris = sin fuerza: no operar

alertcondition(entrada_larga, "Entrada larga", "Supertrend alcista con ADX fuerte")
alertcondition(entrada_corta, "Entrada corta", "Supertrend bajista con ADX fuerte")
alertcondition(giro_bajista | giro_alcista, "Cambio de tendencia", "Supertrend cambió de dirección")

print("Velas con tendencia fuerte:", int(np.sum(fuerte)), "de", n)
