"""PyChart API reference (`ScriptReference` in types.ts) served by GET /api/scripts/reference and used by the
Studio help panel and the editor autocompletion. Every item of ARCHITECTURE §12 is documented in Spanish.

Shape: {"sections": [{"title": str, "items": [{"name": str, "signature": str, "doc": str}]}]}
"""
from __future__ import annotations

Item = tuple[str, str, str]


def _section(title: str, *items: Item) -> dict:
    return {"title": title, "items": [{"name": n, "signature": s, "doc": d} for n, s, d in items]}


_DECLARATION = _section(
    "Declaración",
    ("indicator", "indicator(title, overlay=True)",
     "Declara un indicador. overlay=True lo dibuja sobre el precio; overlay=False en un panel propio debajo. "
     "Ponlo al principio del script; el título debe ser un texto literal. Ejemplo: indicator(\"Mi RSI\", overlay=False)"),
    ("strategy", "strategy(title, overlay=True, initial_capital=10000, risk_pct=1, fee_pct=0.1, slippage_pct=0)",
     "Declara una estrategia con backtest. initial_capital = capital inicial; risk_pct = % del capital arriesgado "
     "por operación (el tamaño se calcula con la distancia al stop); fee_pct = comisión por lado en %; "
     "slippage_pct = deslizamiento en %. Las órdenes se ejecutan en la APERTURA de la vela siguiente a la señal "
     "(sin mirar el futuro). Usa strategy.entry / exit / close."),
)

_SERIES = _section(
    "Series de precios",
    ("open", "open", "Apertura de cada vela: array de numpy (float64) alineado con las velas. open[-1] es la última."),
    ("high", "high", "Máximo de cada vela (array float64)."),
    ("low", "low", "Mínimo de cada vela (array float64)."),
    ("close", "close",
     "Cierre de cada vela (array float64). Ojo: close[1] NO es el cierre anterior como en Pine, es el segundo "
     "elemento del array. Para comparar con velas pasadas usa ta.change(close, n) o desplaza el array con numpy, "
     "p. ej. np.concatenate(([np.nan], close[:-1]))."),
    ("volume", "volume", "Volumen de cada vela (array float64; 0 si el activo no publica volumen)."),
    ("time", "time", "Hora de apertura de cada vela en segundos Unix UTC (array int64)."),
    ("hl2", "hl2", "(high + low) / 2: precio medio de la vela."),
    ("hlc3", "hlc3", "(high + low + close) / 3: precio típico (el que usa el VWAP)."),
    ("ohlc4", "ohlc4", "(open + high + low + close) / 4."),
    ("bar_index", "bar_index", "Índice de cada vela: 0, 1, 2, … (array de enteros). len(close) da el número de velas."),
    ("last_bar_index", "last_bar_index", "Índice de la última vela (len(close) − 1)."),
    ("nan", "nan", "Valor vacío (NaN), igual que np.nan: úsalo para no dibujar en una vela."),
)

_SYMBOL = _section(
    "Símbolo y temporalidad",
    ("syminfo.tickerid", "syminfo.tickerid", "Símbolo completo de la ejecución, p. ej. \"BINANCE:BTCUSDT\"."),
    ("syminfo.ticker", "syminfo.ticker", "Ticker sin el mercado, p. ej. \"BTCUSDT\"."),
    ("syminfo.prefix", "syminfo.prefix", "Mercado o bolsa del símbolo, p. ej. \"BINANCE\"."),
    ("timeframe.period", "timeframe.period", "Temporalidad de las velas, p. ej. \"1h\" o \"1d\"."),
    ("timeframe.seconds", "timeframe.seconds", "Duración de cada vela en segundos (3600 en 1h)."),
    ("timeframe.isintraday", "timeframe.isintraday · timeframe.isdaily · timeframe.isweekly",
     "True según la temporalidad sea intradía, diaria o semanal."),
)

_NEWS = _section(
    "Noticias (calendario económico)",
    ("noticias.cerca", "noticias.cerca(antes=30, despues=30, eventos=\"principales\", moneda=\"USD\")",
     "Serie booleana: True en las velas que tocan la ventana [noticia − antes, noticia + despues] (en minutos). "
     "Úsala como filtro de «no operar»: when=senal & ~noticias.cerca(30, 30). Las horas de publicación se conocen "
     "de antemano, así que no es mirar el futuro. eventos: \"principales\" (IPC, informe de empleo y Fed, con "
     "historial desde febrero de 2024), \"alto\", \"medio\" o \"todos\" (estos tres solo tienen datos desde noviembre "
     "de 2026: no sirven para backtests largos). moneda=None incluye todas las divisas."),
    ("noticias.lista", "noticias.lista(eventos=\"principales\", moneda=\"USD\")",
     "Lista de (hora Unix UTC, título, divisa, impacto) de las noticias del periodo de las velas, la más antigua "
     "primero. Ejemplo: for t, titulo, divisa, impacto in noticias.lista(): print(titulo)"),
    ("noticias.desde", "noticias.desde · noticias.hasta · noticias.disponible",
     "Desde y hasta qué hora Unix están completas las noticias principales, y si esta ejecución recibió el "
     "calendario (False sin el módulo de mercado: entonces noticias.cerca() no marca nada y la consola lo avisa)."),
)

_INPUTS = _section(
    "Entradas (input.*)",
    ("input.int", "input.int(defval, title, minval=None, maxval=None, step=None, options=None)",
     "Entrada entera editable desde el panel «Entradas» sin tocar el código. El nombre de la entrada es la variable "
     "a la que se asigna: longitud = input.int(14, \"Longitud\"). defval debe ser un literal."),
    ("input.float", "input.float(defval, title, minval=None, maxval=None, step=None, options=None)",
     "Entrada decimal. Ejemplo: mult = input.float(2.0, \"Desviaciones\", minval=0.5, maxval=5, step=0.1)"),
    ("input.bool", "input.bool(defval, title)",
     "Interruptor sí/no; devuelve True o False (úsalo en un if). Ejemplo: cortos = input.bool(False, \"Permitir cortos\")"),
    ("input.string", "input.string(defval, title, options=None)",
     "Texto libre, o lista desplegable si pasas options=[...]."),
    ("input.select", "input.select(defval, title, options)",
     "Lista desplegable: options es una lista literal de textos. Ejemplo: "
     "modo = input.select(\"EMA\", \"Media\", options=[\"EMA\", \"SMA\", \"HMA\"])"),
    ("input.source", "input.source(defval=close, title)",
     "Elige la serie de precios (close, open, high, low, hl2, hlc3, ohlc4, volume) y devuelve esa serie. "
     "Ejemplo: fuente = input.source(close, \"Fuente\")"),
    ("input", "input(defval, title)",
     "Forma corta: el tipo se deduce del valor por defecto (entero, decimal, booleano, texto o serie de precios)."),
)

# --- end of section A (declaration, series, inputs) ---

_TA_MA = _section(
    "Medias móviles (ta)",
    ("ta.sma", "ta.sma(source, length)", "Media simple de las últimas length velas. NaN mientras no hay suficientes velas."),
    ("ta.ema", "ta.ema(source, length)", "Media exponencial: reacciona antes que la SMA. Ejemplo: ta.ema(close, 21)"),
    ("ta.wma", "ta.wma(source, length)", "Media ponderada linealmente (más peso a las velas recientes)."),
    ("ta.hma", "ta.hma(source, length)", "Media de Hull: muy suave y con poco retraso."),
    ("ta.rma", "ta.rma(source, length)", "Media de Wilder (la que usan RSI y ATR); alfa = 1/length."),
    ("ta.vwma", "ta.vwma(source, length)", "Media ponderada por volumen."),
    ("ta.dema", "ta.dema(source, length)", "Doble media exponencial (menos retraso que la EMA)."),
    ("ta.tema", "ta.tema(source, length)", "Triple media exponencial."),
    ("ta.alma", "ta.alma(source, length, offset=0.85, sigma=6.0)", "Media de Arnaud Legoux (filtro gaussiano desplazado)."),
    ("ta.swma", "ta.swma(source)", "Media simétrica de 4 velas con pesos 1-2-2-1."),
    ("ta.linreg", "ta.linreg(source, length, offset=0)",
     "Valor de la recta de regresión de las últimas length velas en la vela actual (offset desplaza hacia atrás)."),
)

_TA_OSC = _section(
    "Osciladores y momento (ta)",
    ("ta.rsi", "ta.rsi(source, length=14)", "Índice de fuerza relativa (0-100). >70 sobrecompra, <30 sobreventa."),
    ("ta.macd", "ta.macd(source, fast=12, slow=26, signal=9)",
     "Devuelve una tupla (macd, señal, histograma): linea, senal, hist = ta.macd(close, 12, 26, 9)"),
    ("ta.stoch", "ta.stoch(source, high, low, length=14)",
     "Estocástico %K bruto (0-100). Para %K suavizado y %D: k = ta.sma(ta.stoch(close, high, low, 14), 3); d = ta.sma(k, 3)"),
    ("ta.cci", "ta.cci(source, length=20)", "Commodity Channel Index: ±100 marca extremos habituales."),
    ("ta.mfi", "ta.mfi(source, length=14)", "Money Flow Index (RSI ponderado por volumen, 0-100). Usa hlc3 como fuente."),
    ("ta.roc", "ta.roc(source, length)", "Tasa de cambio en % respecto a hace length velas."),
    ("ta.mom", "ta.mom(source, length)", "Momento: source − source de hace length velas."),
    ("ta.cmo", "ta.cmo(source, length)", "Chande Momentum Oscillator (−100 a 100)."),
    ("ta.tsi", "ta.tsi(source, short_length=13, long_length=25)", "True Strength Index, escalado entre −1 y 1."),
    ("ta.wpr", "ta.wpr(length=14)", "Williams %R (−100 a 0) calculado con high/low/close de las velas."),
)

_TA_VOL = _section(
    "Volatilidad y bandas (ta)",
    ("ta.tr", "ta.tr(handle_na=True)", "Rango verdadero de cada vela: max(high−low, |high−cierre previo|, |low−cierre previo|)."),
    ("ta.atr", "ta.atr(length=14)",
     "Average True Range (media de Wilder del rango verdadero). Base de los stops: stop = close − 2 * ta.atr(14)"),
    ("ta.stdev", "ta.stdev(source, length, biased=True)", "Desviación estándar móvil (biased=True = poblacional, como Pine)."),
    ("ta.variance", "ta.variance(source, length, biased=True)", "Varianza móvil."),
    ("ta.dev", "ta.dev(source, length)", "Desviación media absoluta respecto a la media móvil."),
    ("ta.bb", "ta.bb(source, length=20, mult=2.0)",
     "Bandas de Bollinger. Devuelve (media, superior, inferior): media, sup, inf = ta.bb(close, 20, 2)"),
    ("ta.bbw", "ta.bbw(source, length=20, mult=2.0)", "Ancho de Bollinger: (superior − inferior) / media."),
    ("ta.kc", "ta.kc(source, length=20, mult=1.5, use_true_range=True)",
     "Canal de Keltner (EMA ± mult × rango medio). Devuelve (media, superior, inferior)."),
    ("ta.donchian", "ta.donchian(length=20)",
     "Canal de Donchian con high/low. Devuelve (medio, superior, inferior). Incluye la vela actual: para rupturas "
     "compara con el canal de la vela anterior."),
)

_TA_TREND = _section(
    "Tendencia (ta)",
    ("ta.supertrend", "ta.supertrend(factor=3.0, atr_period=10)",
     "Devuelve (línea, dirección). Como en Pine, dirección = −1 en tendencia ALCISTA y +1 en BAJISTA: "
     "linea, direccion = ta.supertrend(3, 10); alcista = direccion < 0"),
    ("ta.sar", "ta.sar(start=0.02, inc=0.02, maximum=0.2)", "Parabolic SAR (stop y reverso)."),
    ("ta.dmi", "ta.dmi(di_length=14, adx_smoothing=14)",
     "Movimiento direccional. Devuelve (+DI, −DI, ADX): mas_di, menos_di, adx = ta.dmi(14, 14)"),
    ("ta.adx", "ta.adx(length=14)",
     "ADX: FUERZA de la tendencia (no dirección). < 18-20 = mercado lateral, mejor no seguir tendencias."),
)

_TA_VOLUME = _section(
    "Volumen (ta)",
    ("ta.obv", "ta.obv()", "On Balance Volume: suma el volumen en velas alcistas y lo resta en las bajistas."),
    ("ta.vwap", "ta.vwap(source=hlc3, anchor=\"D\")",
     "VWAP anclado: se reinicia cada día UTC (\"D\"), semana (\"W\"), mes (\"M\") o nunca (None)."),
    ("ta.cum", "ta.cum(source)", "Suma acumulada desde la primera vela (ignora valores vacíos)."),
)

_TA_EXTREMES = _section(
    "Máximos, mínimos y pivotes (ta)",
    ("ta.highest", "ta.highest(source, length)",
     "Máximo de las últimas length velas (incluida la actual). ta.highest(20) usa high."),
    ("ta.lowest", "ta.lowest(source, length)", "Mínimo de las últimas length velas. ta.lowest(20) usa low."),
    ("ta.highestbars", "ta.highestbars(source, length)", "Desplazamiento (0 o negativo) hasta el máximo de la ventana."),
    ("ta.lowestbars", "ta.lowestbars(source, length)", "Desplazamiento (0 o negativo) hasta el mínimo de la ventana."),
    ("ta.pivothigh", "ta.pivothigh(source, left, right)",
     "Máximo pivote: vale el precio del pivote en la vela donde se CONFIRMA (right velas después) y NaN en el resto. "
     "ta.pivothigh(5, 5) usa high."),
    ("ta.pivotlow", "ta.pivotlow(source, left, right)",
     "Mínimo pivote, confirmado right velas después (sin mirar el futuro). ta.pivotlow(5, 5) usa low."),
)

_TA_EVENTS = _section(
    "Cruces y condiciones (ta)",
    ("ta.change", "ta.change(source, length=1)", "Diferencia con el valor de hace length velas (equivale a x − x[length] en Pine)."),
    ("ta.crossover", "ta.crossover(a, b)",
     "True en la vela en que a cruza por ENCIMA de b (a y b pueden ser series o números). Devuelve un array booleano."),
    ("ta.crossunder", "ta.crossunder(a, b)", "True en la vela en que a cruza por DEBAJO de b."),
    ("ta.cross", "ta.cross(a, b)", "True cuando a cruza a b en cualquier sentido."),
    ("ta.rising", "ta.rising(source, length)", "True si source supera el máximo de las length velas anteriores."),
    ("ta.falling", "ta.falling(source, length)", "True si source cae por debajo del mínimo de las length velas anteriores."),
    ("ta.barssince", "ta.barssince(condition)", "Velas transcurridas desde la última vez que se cumplió la condición (NaN si nunca)."),
    ("ta.valuewhen", "ta.valuewhen(condition, source, occurrence=0)",
     "Valor de source en la vela donde se cumplió la condición (0 = la más reciente, 1 = la anterior…). "
     "Útil para fijar un stop en la vela de la señal: ta.valuewhen(senal, close - 2 * atr, 0)"),
)

_TA_STATS = _section(
    "Estadística (ta)",
    ("ta.correlation", "ta.correlation(source1, source2, length)", "Correlación de Pearson móvil (−1 a 1)."),
    ("ta.percentrank", "ta.percentrank(source, length)",
     "Percentil (0-100) del valor actual dentro de las últimas length velas."),
    ("ta.median", "ta.median(source, length)", "Mediana móvil."),
)

# --- end of section B (ta) ---

_MISSING = _section(
    "Valores vacíos (NaN)",
    ("nz", "nz(x, replacement=0)", "Sustituye los NaN (valores vacíos del calentamiento) por replacement."),
    ("na", "na(x)", "True donde x está vacío (NaN). Devuelve un array booleano si x es una serie."),
    ("fixnan", "fixnan(x)", "Rellena cada NaN con el último valor válido anterior (nunca con valores futuros)."),
)

_DRAWING = _section(
    "Dibujo",
    ("plot", "plot(series, title, color, linewidth=1, style=\"line\", pane=None, offset=0)",
     "Dibuja una serie. style: \"line\", \"histogram\", \"columns\", \"area\", \"step\" o \"circles\" (o plot.style_*). "
     "pane: 0 = panel del precio, 1, 2… = paneles inferiores (por defecto según overlay). Los NaN dejan huecos. "
     "offset desplaza solo el dibujo (un offset negativo muestra valores posteriores: no lo uses para señales). "
     "Máximo 64 plots."),
    ("plotshape", "plotshape(cond, title, location=\"aboveBar\", color, shape=\"circle\", text=\"\")",
     "Marca las velas donde cond es True. location: \"aboveBar\", \"belowBar\" o \"inBar\" (o location.abovebar…); "
     "shape: \"arrowUp\", \"arrowDown\", \"circle\", \"square\" (o shape.triangleup…). text: etiqueta corta."),
    ("hline", "hline(price, title, color, style=\"dashed\")",
     "Línea horizontal fija (niveles 70/30 del RSI, cero del MACD…). style: \"solid\", \"dashed\" o \"dotted\". "
     "Se dibuja en el panel principal del script."),
    ("bgcolor", "bgcolor(cond, color)",
     "Colorea el fondo de las velas donde cond es True (se aplica transparencia automáticamente). Útil para marcar "
     "zonas de «no operar»."),
    ("location.abovebar", "location.abovebar · location.belowbar · location.top · location.bottom · location.absolute",
     "Constantes de ubicación para plotshape (equivalen a los textos \"aboveBar\", \"belowBar\"…)."),
    ("shape.arrowup", "shape.arrowup · arrowdown · triangleup · triangledown · labelup · labeldown · circle · square",
     "Constantes de forma para plotshape (también xcross, cross, diamond y flag, que se muestran como círculo)."),
    ("plot.style_line", "plot.style_line · style_histogram · style_columns · style_area · style_stepline · style_circles",
     "Constantes de estilo para plot (equivalen a los textos \"line\", \"histogram\"…)."),
    ("hline.style_dashed", "hline.style_solid · hline.style_dashed · hline.style_dotted", "Estilos de línea para hline."),
)

_ALERTS = _section(
    "Alertas",
    ("alertcondition", "alertcondition(cond, title, message)",
     "Registra una alerta en cada vela donde cond es True (pestaña «Alertas»). Si se cumple en la ÚLTIMA vela, NEXUS "
     "la publica también como señal en la terminal."),
)

_STRATEGY = _section(
    "Estrategia (backtest)",
    ("strategy.entry", "strategy.entry(id, direction, when=True, qty_pct=None, stop=None, limit=None, comment=None)",
     "Abre una posición \"long\" o \"short\" (o strategy.long / strategy.short) en la apertura de la vela siguiente "
     "a cada vela donde when es True. Una sola posición a la vez: una entrada contraria da la vuelta a la posición. "
     "Sin qty_pct el tamaño sale de risk_pct y la distancia al stop; qty_pct = % del capital a invertir."),
    ("strategy.exit",
     "strategy.exit(id, from_entry=None, stop=None, limit=None, trail_atr=None, when=True, update=False, comment=None)",
     "Salidas de la posición from_entry: stop = stop «hasta aquí», limit = objetivo, trail_atr = stop dinámico a "
     "k × ATR(14) del mejor precio. Los niveles se toman de la vela de la señal y quedan FIJOS durante la operación; "
     "con update=True se recalculan en cada vela (útil para seguir una línea como Supertrend). Stop y objetivo se "
     "comprueban dentro de la vela; si ambos se tocan en la misma vela se asume el stop (prudente)."),
    ("strategy.close", "strategy.close(id, when=True, comment=None)",
     "Cierra la posición abierta por la entrada id en la apertura de la vela siguiente a cada vela donde when es True."),
    ("strategy.close_all", "strategy.close_all(when=True, comment=None)",
     "Cierra cualquier posición abierta cuando when es True (por ejemplo un filtro de «no operar» que se activa)."),
    ("strategy.long", "strategy.long · strategy.short",
     "Constantes de dirección para strategy.entry (equivalen a \"long\" y \"short\")."),
)

_COLORS = _section(
    "Colores (color.*)",
    ("color.green", "color.green", "Verde (#4CAF50): compras, tendencia alcista."),
    ("color.red", "color.red", "Rojo (#FF5252): ventas, tendencia bajista."),
    ("color.blue", "color.blue", "Azul (#2196F3)."),
    ("color.orange", "color.orange", "Naranja (#FF9800)."),
    ("color.yellow", "color.yellow", "Amarillo (#FFEB3B)."),
    ("color.purple", "color.purple", "Púrpura (#9C27B0)."),
    ("color.gray", "color.gray", "Gris (#787B86): señales descartadas, zonas neutrales."),
    ("color.white", "color.white", "Blanco (#FFFFFF)."),
    ("color.black", "color.black", "Negro (#363A45)."),
    ("color.teal", "color.teal", "Verde azulado (#00897B)."),
    ("color.aqua", "color.aqua", "Aguamarina (#00BCD4)."),
    ("color.lime", "color.lime", "Lima (#00E676)."),
    ("color.fuchsia", "color.fuchsia", "Fucsia (#E040FB)."),
    ("color.maroon", "color.maroon", "Granate (#880E4F)."),
    ("color.navy", "color.navy", "Azul marino (#311B92)."),
    ("color.olive", "color.olive", "Oliva (#808000)."),
    ("color.silver", "color.silver", "Plata (#B2B5BE)."),
    ("color.new", "color.new(color, transp)", "El mismo color con transparencia 0 (opaco) a 100 (invisible)."),
    ("color.rgb", "color.rgb(red, green, blue, transp=0)", "Color a partir de componentes 0-255. También vale un texto \"#RRGGBB\"."),
)

_PYTHON = _section(
    "Python en PyChart",
    ("print", "print(*valores)",
     "Escribe en la pestaña «Consola» (y en la terminal de NEXUS como depuración). Máximo 20 000 caracteres."),
    ("import", "import math · import statistics · import numpy as np",
     "Únicos módulos permitidos (también numpy.linalg, numpy.random y numpy.fft). Sin archivos, red ni sistema: "
     "cada ejecución corre aislada en un proceso aparte con un máximo de 10 segundos y los errores indican la "
     "línea de tu script."),
    ("np", "np.where · np.clip · np.full · np.nan · np.isnan · np.maximum …",
     "Las series son arrays de numpy: opera con ellas de forma vectorizada (close > ema, (a & b), np.where(...)). "
     "Combina condiciones con & | ~ y paréntesis, no con and/or. Para resumir usa las funciones de numpy "
     "(np.sum(senal), np.mean(x), np.max(x)) en lugar de los métodos del array. Todas las funciones ta.* son causales (la vela i "
     "solo usa velas ≤ i): no indexes hacia delante (close[i + 1]) o tus señales serán imposibles de reproducir en vivo."),
    ("def", "def mi_funcion(serie, n): …",
     "Puedes definir funciones y usar bucles for/while. No se permiten clases, with, global, yield, eval/exec ni "
     "atributos que empiecen por _. Límites por script: 64 plots, 3000 marcas y 100 000 caracteres de código."),
)

REFERENCE: dict = {
    "sections": [
        _DECLARATION, _SERIES, _SYMBOL, _NEWS, _INPUTS, _TA_MA, _TA_OSC, _TA_VOL, _TA_TREND, _TA_VOLUME, _TA_EXTREMES, _TA_EVENTS,
        _TA_STATS, _MISSING, _DRAWING, _ALERTS, _STRATEGY, _COLORS, _PYTHON,
    ]
}


def reference_names() -> set[str]:
    """Every documented name (used by tests and the autocompletion consistency checks)."""
    return {item["name"] for section in REFERENCE["sections"] for item in section["items"]}
