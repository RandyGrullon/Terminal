# ─────────────────────────────────────────────────────────────────────────────
#  Breakout de Donchian con stop ATR (estrategia)
#
#  Sistema "tortuga": se compra cuando el cierre supera el máximo de las N
#  velas ANTERIORES y se vende en corto cuando pierde su mínimo. Cada entrada
#  lleva:
#    · un stop inicial "hasta aquí" a 'stop_atr' ATR del precio de la señal,
#    · un trailing de 'trail' ATR que protege la ganancia (trail_atr),
#    · una salida por el canal corto (mínimo/máximo de M velas anteriores).
#  Filtro de no operar: si el ATR es anormalmente bajo (mercado dormido) las
#  rupturas suelen ser falsas → se ignoran.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

strategy("Breakout de Donchian", initial_capital=10000, risk_pct=1, fee_pct=0.1)

entrada_n = input.int(20, "Canal de entrada (velas)")
salida_n = input.int(10, "Canal de salida (velas)")
stop_atr = input.float(2.0, "Stop inicial (ATR)")
trail = input.float(3.0, "Trailing (ATR)")
atr_pct_min = input.float(20.0, "Percentil mínimo de ATR para operar")
permitir_cortos = input.bool(True, "Permitir cortos")


def atras(serie, k=1):
    """Valor de hace k velas (serie[k] en Pine)."""
    serie = np.asarray(serie, dtype=float)
    out = np.full(len(serie), np.nan)
    if 0 < k < len(serie):
        out[k:] = serie[:-k]
    return out


# Canales de las velas ANTERIORES: romperlos con el cierre actual es causal
techo = atras(ta.highest(high, entrada_n))
suelo = atras(ta.lowest(low, entrada_n))
techo_salida = atras(ta.highest(high, salida_n))
suelo_salida = atras(ta.lowest(low, salida_n))
medio = (techo + suelo) / 2
atr = ta.atr(20)

# Filtro: percentil del ATR en las últimas 200 velas (0 = el más bajo). Hasta
# tener 200 velas de historia no hay referencia y no se opera.
atr_percentil = ta.percentrank(atr, 200)
con_historia = ~np.isnan(atr_percentil)
mercado_vivo = atr_percentil >= atr_pct_min

ruptura_alcista = (close > techo) & (atras(close) <= atras(techo))
ruptura_bajista = (close < suelo) & (atras(close) >= atras(suelo))
largo = ruptura_alcista & mercado_vivo
corto = ruptura_bajista & mercado_vivo & permitir_cortos
ignorada = (ruptura_alcista | (ruptura_bajista & permitir_cortos)) & con_historia & ~mercado_vivo

# El stop inicial se fija en la vela de la ruptura; el trailing (trail_atr) lo
# va subiendo a 'trail' ATR del mejor precio alcanzado.
strategy.entry("Largo", "long", when=largo)
strategy.exit("Stop largo", from_entry="Largo", stop=close - stop_atr * atr, trail_atr=trail)
strategy.close("Largo", when=close < suelo_salida)

if permitir_cortos:
    strategy.entry("Corto", "short", when=corto)
    strategy.exit("Stop corto", from_entry="Corto", stop=close + stop_atr * atr, trail_atr=trail)
    strategy.close("Corto", when=close > techo_salida)

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(techo, "Canal superior", color=color.green, linewidth=1, style="step")
plot(suelo, "Canal inferior", color=color.red, linewidth=1, style="step")
plot(medio, "Centro", color=color.gray, linewidth=1)
plot(suelo_salida, "Salida largos", color=color.orange, linewidth=1, style="step")
bgcolor(con_historia & ~mercado_vivo, color.gray)  # mercado dormido: no operar
plotshape(ignorada, "Ruptura ignorada (ATR bajo)", location="aboveBar", color=color.gray, shape="circle", text="x")

alertcondition(largo, "Ruptura alcista", "El cierre superó el máximo del canal de Donchian")
alertcondition(corto, "Ruptura bajista", "El cierre perdió el mínimo del canal de Donchian")

print("Rupturas operables:", int(np.sum(largo | corto)), "· ignoradas por ATR bajo:", int(np.sum(ignorada)))
