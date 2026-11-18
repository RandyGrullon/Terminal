# ─────────────────────────────────────────────────────────────────────────────
#  GenXAU #3 / #4 — MACD/señal + tendencia H4 + TP parcial (oro M30)
#  FALLÓ EL HOLDOUT — SOLO ESTUDIO. No es una estrategia para operar.
#
#  Son dos de los 5 finalistas de la investigación GenXAU (generador genético en
#  el probador de MetaTrader 5, XAUUSD de XM en M30): 16.651 estrategias
#  probadas, ninguna pasó el Deflated Sharpe, y en el holdout
#  (2025-11-17 → 2026-11-18) los 5 fallaron: #3 +1,9R (PF 1,06) y #4 +1,1R
#  (PF 1,04), sin ventaja. Este script sirve para ver en el gráfico qué hacían.
#
#  Reglas (iguales para #3 y #4 salvo donde se indica):
#  - Señal: el MACD(8, 21, 9) cruza su línea de señal al CIERRE de la vela
#    (al alza = compra, a la baja = venta); se entra en la apertura siguiente.
#  - Filtros: RSI(14) > 50 para compras (< 50 para ventas) y cierre por encima
#    (por debajo) de la EMA 50 de H4. PyChart no lee otra temporalidad desde el
#    gráfico, así que se APROXIMA con una EMA de 50 × (4 h / temporalidad) del
#    mismo gráfico: EMA 400 en 30m. No es idéntica a la EMA de la última vela H4
#    cerrada.
#  - Sesión: solo entradas entre las 12:00 y las 16:00 UTC (overlap
#    Londres-NY). Nunca entre las 23:00 y la 01:00 del servidor de XM, ni el
#    viernes desde las 21:00, ni el lunes antes de las 02:00 del servidor.
#    Cierre forzado el viernes a las 23:00 del servidor. Servidor de XM = GMT+2
#    (GMT+3 con el horario de verano de EE. UU.). Tampoco opera sábado ni
#    domingo (el oro de XM no cotiza; el perpetuo de Binance sí).
#  - Salida: SL = 2,5 × ATR(14). A +1,5 × ATR cierra el 50 % y el stop del
#    resto pasa a la entrada. El resto va a 4,5 × ATR (#3) o 5,5 × ATR (#4).
#  - Tiempo máximo: 24 velas (#3) / 8 velas (#4). El #3 además cierra y gira si
#    aparece la señal contraria (la misma regla de entrada en la otra
#    dirección); el #4 no.
#  - Una posición a la vez, como mucho 4 entradas al día, riesgo 1 %.
#
#  Costes: comisión y deslizamiento pequeños, parecidos al diferencial del oro
#  en XM (~0,02-0,03R por operación). El swap de un día para otro no entra en
#  este backtest (en GenXAU pesó entre el 0 y el 7 % del resultado neto).
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

strategy("GenXAU #3/#4 · falló el holdout — solo estudio", initial_capital=10000, risk_pct=1, fee_pct=0.01,
         slippage_pct=0.01, max_entries_per_day=4)

variante = input.select("#3", "Variante de GenXAU", options=["#3", "#4"])
macd_rapida = input.int(8, "MACD rápida")
macd_lenta = input.int(21, "MACD lenta")
macd_senal = input.int(9, "MACD señal")
rsi_largo = input.int(14, "RSI")
sl_atr = input.float(2.5, "Stop (× ATR)")
parcial_atr = input.float(1.5, "TP parcial del 50 % (× ATR)")

es3 = variante == "#3"
tp_atr = 4.5 if es3 else 5.5
max_velas = 24 if es3 else 8


# ── Hora del servidor de XM (GMT+2; GMT+3 con el horario de verano de EE. UU.) ─
def dias_desde_1970(y, m, d):
    """Días desde el 1-1-1970 de una fecha (algoritmo de Howard Hinnant)."""
    y = y - (1 if m <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def domingo(y, m, n):
    """Día (desde 1970) del n-ésimo domingo del mes."""
    d1 = dias_desde_1970(y, m, 1)
    dia_semana = (d1 + 3) % 7  # 0 = lunes … 6 = domingo
    return d1 + (6 - dia_semana) % 7 + 7 * (n - 1)


# Todo se evalúa para la APERTURA de la vela siguiente, que es cuando se entra
t_ent = time + timeframe.seconds
verano = np.full(len(time), False)
primero = 1969 + int(t_ent[0] // 31556952)
ultimo = 1971 + int(t_ent[-1] // 31556952)
for anio in range(primero, ultimo + 1):
    desde = domingo(anio, 3, 2) * 86400 + 7 * 3600  # 2.º domingo de marzo, 2:00 en Nueva York
    hasta = domingo(anio, 11, 1) * 86400 + 6 * 3600  # 1.er domingo de noviembre, 2:00 en Nueva York
    verano = verano | ((t_ent >= desde) & (t_ent < hasta))
servidor = t_ent + np.where(verano, 3, 2) * 3600
hora_srv = (servidor // 3600) % 24
dia_srv = ((servidor // 86400) + 3) % 7  # 0 = lunes … 4 = viernes
hora_utc = (t_ent // 3600) % 24

overlap = (hora_utc >= 12) & (hora_utc < 16)
# El oro de XM no cotiza el fin de semana; el perpetuo de Binance sí (24/7): aquí tampoco se opera sábado ni domingo
finde = dia_srv >= 5
prohibido = ((hora_srv >= 23) | (hora_srv < 1) | ((dia_srv == 4) & (hora_srv >= 21)) | ((dia_srv == 0) & (hora_srv < 2))
             | finde)
ventana = overlap & ~prohibido
cierre_viernes = ((dia_srv == 4) & (hora_srv >= 23)) | finde

# ── Señal y filtros ─────────────────────────────────────────────────────────
linea, senal, hist = ta.macd(close, macd_rapida, macd_lenta, macd_senal)
rsi = ta.rsi(close, rsi_largo)
largo_ema = int(round(50 * max(14400 / timeframe.seconds, 1)))  # ≈ EMA 50 de H4 (400 en 30m)
tendencia = ta.ema(close, largo_ema)
atr = ta.atr(14)

largo = ta.crossover(linea, senal) & (rsi > 50) & (close > tendencia) & ventana
corto = ta.crossunder(linea, senal) & (rsi < 50) & (close < tendencia) & ventana

# ── Reglas ──────────────────────────────────────────────────────────────────
# #3 gira con la señal contraria (reverse=True); #4 solo entra sin posición abierta
strategy.entry("Largo", "long", when=largo, reverse=es3)
strategy.entry("Corto", "short", when=corto, reverse=es3)
strategy.exit("Salida largo", from_entry="Largo", stop=close - sl_atr * atr, limit=close + tp_atr * atr,
              partial_limit=close + parcial_atr * atr, partial_pct=50, breakeven=True, max_bars=max_velas)
strategy.exit("Salida corto", from_entry="Corto", stop=close + sl_atr * atr, limit=close - tp_atr * atr,
              partial_limit=close - parcial_atr * atr, partial_pct=50, breakeven=True, max_bars=max_velas)
strategy.close_all(when=cierre_viernes, comment="Viernes 23:00")

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(tendencia, f"EMA {largo_ema} (≈ EMA 50 de H4)", color=color.gray, linewidth=2)
bgcolor(overlap, color=color.blue)
plotshape(largo, "Compra GenXAU", location="belowBar", color=color.green, shape="arrowUp", text="estudio")
plotshape(corto, "Venta GenXAU", location="aboveBar", color=color.red, shape="arrowDown", text="estudio")

resultado = "+1,9R, PF 1,06" if es3 else "+1,1R, PF 1,04"
print(f"GenXAU {variante}: FALLÓ EL HOLDOUT (2025-11-17 → 2026-11-18: {resultado}) — solo estudio, sin ventaja.")
print(f"Tendencia H4 aproximada con una EMA {largo_ema} de esta temporalidad; TP final {tp_atr} × ATR, "
      f"máximo {max_velas} velas.")
if timeframe.period != "30m":
    print("Aviso: GenXAU se probó en velas de 30 minutos; en otra temporalidad las reglas no son las mismas.")
print("Señales de compra:", int(np.sum(largo)), "· de venta:", int(np.sum(corto)))
