// Starter code for new PyChart scripts (Spanish comments, Pine-like API from ARCHITECTURE §12).

export type TemplateKind = 'indicator' | 'strategy'

export const TEMPLATE_NAMES: Record<TemplateKind, string> = {
  indicator: 'Nuevo indicador',
  strategy: 'Nueva estrategia',
}

const INDICATOR = `# ─────────────────────────────────────────────────────────────
#  Nuevo indicador PyChart (Python con una API al estilo Pine)
#  open, high, low, close y volume son arrays de numpy alineados
#  con las velas: cada cálculo es vectorizado y causal.
#  Ctrl+Enter = ejecutar · Ctrl+Shift+B = compilar · Ctrl+S = guardar
# ─────────────────────────────────────────────────────────────

# Declara el script. overlay=True lo dibuja sobre el precio.
indicator("Cruce de EMAs", overlay=True)

# Entradas editables desde el panel "Entradas" (sin tocar el código)
rapida = input.int(9, "EMA rápida")
lenta = input.int(21, "EMA lenta")

# Cálculos
ema_rapida = ta.ema(close, rapida)
ema_lenta = ta.ema(close, lenta)
cruce_alcista = ta.crossover(ema_rapida, ema_lenta)
cruce_bajista = ta.crossunder(ema_rapida, ema_lenta)

# Dibujo: líneas y marcas en las velas donde ocurre el cruce
plot(ema_rapida, "EMA rápida", color=color.blue, linewidth=2)
plot(ema_lenta, "EMA lenta", color=color.orange, linewidth=2)
plotshape(cruce_alcista, "Cruce alcista", location="belowbar", color=color.green, shape="arrowup", text="C")
plotshape(cruce_bajista, "Cruce bajista", location="abovebar", color=color.red, shape="arrowdown", text="V")

# Alerta reutilizable (aparece en la pestaña "Alertas")
alertcondition(cruce_alcista, "Cruce alcista", "La EMA rápida cruzó por encima de la lenta")

# print() escribe en la pestaña "Consola"
print("Velas analizadas:", len(close))

# ── ¿Quieres probarlo como estrategia con backtest? ─────────────
# 1) Cambia la línea indicator(...) por:
#      strategy("Cruce de EMAs", initial_capital=10000, risk_pct=1, fee_pct=0.1)
# 2) Añade las reglas de entrada y salida:
#      strategy.entry("Largo", "long", when=cruce_alcista)
#      strategy.exit("Salida", from_entry="Largo", stop=ta.lowest(low, 10), trail_atr=3)
#      strategy.close("Largo", when=cruce_bajista)
`

const STRATEGY = `# ─────────────────────────────────────────────────────────────
#  Nueva estrategia PyChart: genera operaciones y un backtest
#  (pestaña "Estrategia": métricas, curva de capital y operaciones).
#  Las entradas se ejecutan en la apertura de la vela siguiente.
#  Ctrl+Enter = ejecutar · Ctrl+Shift+B = compilar · Ctrl+S = guardar
# ─────────────────────────────────────────────────────────────

strategy("Cruce de EMAs con stop ATR", initial_capital=10000, risk_pct=1, fee_pct=0.1)

# Entradas editables
rapida = input.int(9, "EMA rápida")
lenta = input.int(21, "EMA lenta")
filtro = input.int(200, "EMA de tendencia")
stop_atr = input.float(2.0, "Stop (múltiplos de ATR)")
objetivo_r = input.float(2.0, "Objetivo (R)")

# Cálculos
ema_rapida = ta.ema(close, rapida)
ema_lenta = ta.ema(close, lenta)
ema_tendencia = ta.ema(close, filtro)
atr = ta.atr(14)

# Solo compra a favor de la tendencia mayor (no operar contra ella)
a_favor = close > ema_tendencia
entrada_larga = ta.crossover(ema_rapida, ema_lenta) & a_favor
salida = ta.crossunder(ema_rapida, ema_lenta)

# Dibujo
plot(ema_rapida, "EMA rápida", color=color.blue)
plot(ema_lenta, "EMA lenta", color=color.orange)
plot(ema_tendencia, "EMA tendencia", color=color.gray)
plotshape(entrada_larga, "Entrada", location="belowbar", color=color.green, shape="arrowup", text="L")

# Reglas: entrada, stop "hasta aquí" + objetivo en R, y cierre por señal contraria
stop = close - stop_atr * atr
objetivo = close + objetivo_r * stop_atr * atr
strategy.entry("Largo", "long", when=entrada_larga)
strategy.exit("Salida", from_entry="Largo", stop=stop, limit=objetivo)
strategy.close("Largo", when=salida)

alertcondition(entrada_larga, "Entrada larga", "Cruce alcista a favor de la tendencia")
`

export const TEMPLATES: Record<TemplateKind, string> = {
  indicator: INDICATOR,
  strategy: STRATEGY,
}
