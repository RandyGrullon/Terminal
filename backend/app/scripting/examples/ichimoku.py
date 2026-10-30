# ─────────────────────────────────────────────────────────────────────────────
#  Ichimoku completo
#
#  · Tenkan-sen: punto medio de las últimas 9 velas (rápida).
#  · Kijun-sen: punto medio de 26 velas (equilibrio; buen stop de referencia).
#  · Nube (Senkou A y B): se calcula hoy y se proyecta 26 velas al futuro. La
#    nube dibujada en cada vela es la que se calculó 26 velas antes, así que no
#    usa información futura.
#  · Chikou: el cierre de hoy comparado con el precio de hace 26 velas. Los
#    gráficos clásicos lo dibujan desplazado hacia atrás (eso mostraría datos
#    futuros en velas pasadas); aquí se usa como filtro de confirmación.
#
#  Señal fuerte: cruce Tenkan/Kijun a favor, precio fuera de la nube y Chikou
#  a favor. Precio DENTRO de la nube = indecisión: mejor no operar.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

indicator("Ichimoku completo", overlay=True)

tenkan_n = input.int(9, "Tenkan-sen")
kijun_n = input.int(26, "Kijun-sen")
senkou_n = input.int(52, "Senkou Span B")
desplazamiento = input.int(26, "Desplazamiento")


def punto_medio(longitud):
    """(máximo + mínimo) / 2 de las últimas 'longitud' velas."""
    return (ta.highest(high, longitud) + ta.lowest(low, longitud)) / 2


def atras(serie, k):
    """Valor de hace k velas (equivale a serie[k] en Pine); las primeras k quedan vacías."""
    serie = np.asarray(serie, dtype=float)
    out = np.full(len(serie), np.nan)
    if 0 < k < len(serie):
        out[k:] = serie[:-k]
    elif k == 0:
        out[:] = serie
    return out


tenkan = punto_medio(tenkan_n)
kijun = punto_medio(kijun_n)
senkou_a_hoy = (tenkan + kijun) / 2
senkou_b_hoy = punto_medio(senkou_n)

# Nube vigente en cada vela (calculada 'desplazamiento' velas antes)
senkou_a = atras(senkou_a_hoy, desplazamiento)
senkou_b = atras(senkou_b_hoy, desplazamiento)
techo = np.maximum(senkou_a, senkou_b)
suelo = np.minimum(senkou_a, senkou_b)

sobre_nube = close > techo
bajo_nube = close < suelo
dentro_nube = ~sobre_nube & ~bajo_nube & ~np.isnan(techo)

# Chikou como confirmación causal
precio_pasado = atras(close, desplazamiento)
chikou_alcista = close > precio_pasado
chikou_bajista = close < precio_pasado

# Nube futura (la que se está formando ahora): verde si Senkou A > Senkou B
nube_futura_alcista = senkou_a_hoy > senkou_b_hoy

tk_alcista = ta.crossover(tenkan, kijun)
tk_bajista = ta.crossunder(tenkan, kijun)
compra_fuerte = tk_alcista & sobre_nube & chikou_alcista
venta_fuerte = tk_bajista & bajo_nube & chikou_bajista
cruce_debil = (tk_alcista | tk_bajista) & ~(compra_fuerte | venta_fuerte)

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(tenkan, "Tenkan-sen", color=color.blue, linewidth=1)
plot(kijun, "Kijun-sen", color=color.red, linewidth=2)
plot(senkou_a, "Senkou A", color=color.green, linewidth=1)
plot(senkou_b, "Senkou B", color=color.orange, linewidth=1)
bgcolor(dentro_nube, color.gray)

plotshape(compra_fuerte, "Compra fuerte (TK + nube + Chikou)", location="belowBar", color=color.green, shape="arrowUp", text="TK")
plotshape(venta_fuerte, "Venta fuerte (TK + nube + Chikou)", location="aboveBar", color=color.red, shape="arrowDown", text="TK")
plotshape(cruce_debil, "Cruce TK sin confirmar", location="aboveBar", color=color.gray, shape="circle", text="tk")

alertcondition(compra_fuerte, "Ichimoku compra fuerte", "Cruce TK alcista sobre la nube con Chikou a favor")
alertcondition(venta_fuerte, "Ichimoku venta fuerte", "Cruce TK bajista bajo la nube con Chikou a favor")
alertcondition(ta.crossover(close, techo), "Ruptura de la nube", "El precio cerró por encima de la nube")

if len(close):
    estado = "sobre la nube" if sobre_nube[-1] else "bajo la nube" if bajo_nube[-1] else "dentro de la nube (indecisión)"
    futura = "alcista" if nube_futura_alcista[-1] else "bajista"
    print("Precio", estado, "· nube futura", futura)
