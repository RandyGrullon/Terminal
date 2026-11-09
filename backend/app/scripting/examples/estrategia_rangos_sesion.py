# ─────────────────────────────────────────────────────────────────────────────
#  Estrategia: rango de sesión (Londres → NY o Sídney → Asia)
#
#  Convierte el indicador de rangos en reglas FIJAS de compra, venta y cierre,
#  para que el backtest de PyChart diga cuánto habrían ganado o perdido:
#
#  Modo «Barrida (reversión)»: apuesta a que la toma del nivel es una trampa.
#    · Barrida del máximo (mecha por encima, cierre dentro) → VENDE.
#      Stop: encima de la mecha. Objetivo: lado contrario o mitad del rango, o 2R.
#    · Barrida del mínimo → COMPRA (espejo).
#  Modo «Ruptura (continuación)»: apuesta a que el precio sigue.
#    · Cierre por encima del máximo → COMPRA. Stop: medio ATR por debajo del
#      nivel roto (si el precio vuelve a entrar al rango, la ruptura falló).
#      Objetivo: R múltiplos.
#    · Cierre por debajo del mínimo → VENDE (espejo).
#  Siempre: una sola operación por sesión, entrada en la apertura de la vela
#  siguiente a la señal y CIERRE de lo que quede abierto al terminar la ventana.
#  Noticias: por defecto no abre operaciones cerca de las noticias principales
#  (IPC, informe de empleo y Fed; historial desde febrero de 2024) y, si lo
#  activas, cierra lo abierto antes de cada publicación. Compara el backtest con
#  y sin el filtro: que filtre no garantiza que mejore.
#
#  Son reglas para medir, no una predicción: si el backtest no gana más que
#  sus costes con al menos 30 operaciones, no hay nada que operar.
#  Horas en la zona elegida («Nueva York» aplica su horario de verano).
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

strategy("Rango de sesión: barrida o ruptura", initial_capital=10000, risk_pct=1, fee_pct=0.01, slippage_pct=0.005)

sesion = input.select("Londres → NY", "Sesión", options=["Londres → NY", "Sídney → Asia"])
modo = input.select("Barrida (reversión)", "Qué operar", options=["Barrida (reversión)", "Ruptura (continuación)"])
objetivo = input.select("Lado contrario del rango", "Objetivo de las barridas",
                        options=["Lado contrario del rango", "Mitad del rango", "R múltiplos"])
rr = input.float(2.0, "Objetivo en R (rupturas, o barridas con «R múltiplos»)", minval=0.5, maxval=10, step=0.25)
margen = input.float(0.1, "Margen del stop sobre la mecha en barridas (ATR)", minval=0, maxval=3, step=0.05)
margen_rup = input.float(0.5, "Margen del stop tras el nivel en rupturas (ATR)", minval=0, maxval=3, step=0.05)
operar_largos = input.bool(True, "Permitir compras")
operar_cortos = input.bool(True, "Permitir ventas")
evitar_noticias = input.bool(True, "No abrir operaciones cerca de noticias (IPC, empleo, Fed)")
min_antes = input.int(30, "Minutos antes de la noticia", minval=0, maxval=240)
min_despues = input.int(30, "Minutos después de la noticia", minval=0, maxval=240)
cerrar_por_noticia = input.bool(False, "Cerrar lo abierto antes de cada noticia")
zona = input.select("Nueva York", "Zona horaria de las horas", options=["Nueva York", "Santo Domingo", "UTC"])
syd_desde = input.float(17.0, "Rango Sídney: desde", minval=0, maxval=24, step=0.25)
syd_hasta = input.float(19.0, "Rango Sídney: hasta", minval=0, maxval=24, step=0.25)
asia_desde = input.float(19.0, "Operar en Asia: desde", minval=0, maxval=24, step=0.25)
asia_hasta = input.float(2.0, "Operar en Asia: hasta", minval=0, maxval=24, step=0.25)
lon_desde = input.float(2.0, "Rango Londres: desde", minval=0, maxval=24, step=0.25)
lon_hasta = input.float(7.0, "Rango Londres: hasta", minval=0, maxval=24, step=0.25)
ny_desde = input.float(7.0, "Operar en NY: desde", minval=0, maxval=24, step=0.25)
ny_hasta = input.float(11.0, "Operar en NY: hasta", minval=0, maxval=24, step=0.25)

n = len(close)
DIA = 86400
paso_min = int(round(float(np.median(np.diff(time))) / 60)) if n > 1 else 1


# ── Hora local de cada vela (igual que el indicador) ─────────────────────────
def domingo_numero(anio, mes, k):
    d0 = int(np.datetime64(str(anio) + mes + "-01").astype(np.int64))
    dia_semana = (d0 + 3) % 7  # 0 = lunes … 6 = domingo
    return d0 + (6 - dia_semana) % 7 + 7 * (k - 1)


if zona == "Nueva York":
    anios = time.astype("datetime64[s]").astype("datetime64[Y]").astype(np.int64) + 1970
    desfase = np.full(n, -5 * 3600, dtype=np.int64)
    for anio in np.unique(anios):
        a = int(anio)
        inicio_verano = domingo_numero(a, "-03", 2) * DIA + 7 * 3600
        fin_verano = domingo_numero(a, "-11", 1) * DIA + 6 * 3600
        desfase = np.where((anios == anio) & (time >= inicio_verano) & (time < fin_verano), -4 * 3600, desfase)
elif zona == "Santo Domingo":
    desfase = np.full(n, -4 * 3600, dtype=np.int64)
else:
    desfase = np.zeros(n, dtype=np.int64)

local = time.astype(np.int64) + desfase
minuto = (local // 60) % 1440
dia = local // DIA


def dentro(m, a, b):
    if a == b:
        return np.zeros(len(m), dtype=bool)
    if a < b:
        return (m >= a) & (m < b)
    return (m >= a) | (m < b)


if sesion == "Londres → NY":
    r_a, r_b = int(round(lon_desde * 60)) % 1440, int(round(lon_hasta * 60)) % 1440
    o_a, o_b = int(round(ny_desde * 60)) % 1440, int(round(ny_hasta * 60)) % 1440
    etiqueta = "Londres"
else:
    r_a, r_b = int(round(syd_desde * 60)) % 1440, int(round(syd_hasta * 60)) % 1440
    o_a, o_b = int(round(asia_desde * 60)) % 1440, int(round(asia_hasta * 60)) % 1440
    etiqueta = "Sídney"

en_rango = dentro(minuto, r_a, r_b)
en_operar = dentro(minuto, o_a, o_b)
# última vela de la ventana según el reloj (no mirando la vela siguiente): causal
ultima_vela = en_operar & ~dentro((minuto + paso_min) % 1440, o_a, o_b)
sid = np.where(en_rango, dia - ((minuto < r_b) & (r_a > r_b)).astype(np.int64), -1)
atr = ta.atr(14)
# la entrada es en la vela siguiente: la ventana «antes» se alarga una vela para cubrirla
bloqueo_noticia = noticias.cerca(min_antes + paso_min, min_despues)
antes_de_noticia = noticias.cerca(min_antes + paso_min, 0)

niv_max = np.full(n, np.nan)
niv_min = np.full(n, np.nan)
compra = np.zeros(n, dtype=bool)
venta = np.zeros(n, dtype=bool)
bloqueada = np.zeros(n, dtype=bool)
stop_l = np.full(n, np.nan)
obj_l = np.full(n, np.nan)
stop_c = np.full(n, np.nan)
obj_c = np.full(n, np.nan)

estado, sesion_actual = "fuera", -1
rmax = rmin = np.nan
tomado_max = tomado_min = opero = False
for i in range(n):
    if en_rango[i]:
        if estado != "formando" or sid[i] != sesion_actual:
            estado, sesion_actual = "formando", sid[i]
            rmax, rmin = high[i], low[i]
        else:
            rmax, rmin = max(rmax, high[i]), min(rmin, low[i])
        continue
    if estado == "formando":
        estado = "fijo"
        tomado_max = tomado_min = opero = False
    if estado != "fijo":
        continue
    if not en_operar[i]:
        if opero or tomado_max or tomado_min:  # la ventana ya pasó: hasta el próximo rango no hay señales
            estado = "fuera"
        continue

    niv_max[i], niv_min[i] = rmax, rmin
    a = atr[i] if not np.isnan(atr[i]) else 0.0
    toma_max = (not tomado_max) and high[i] > rmax
    toma_min = (not tomado_min) and low[i] < rmin
    tomado_max = tomado_max or toma_max
    tomado_min = tomado_min or toma_min
    if opero or ultima_vela[i] or (toma_max and toma_min):  # una operación por sesión; nada en la última vela
        continue
    mitad = (rmax + rmin) / 2
    lado, stop, meta = 0, np.nan, np.nan  # +1 compra, −1 venta, 0 nada

    if modo == "Barrida (reversión)":
        if toma_max and close[i] <= rmax and operar_cortos:
            stop = high[i] + margen * a
            if objetivo == "Lado contrario del rango":
                meta = rmin
            elif objetivo == "Mitad del rango":
                meta = mitad
            else:
                meta = close[i] - rr * (stop - close[i])
            lado = -1 if meta < close[i] < stop else 0
        elif toma_min and close[i] >= rmin and operar_largos:
            stop = low[i] - margen * a
            if objetivo == "Lado contrario del rango":
                meta = rmax
            elif objetivo == "Mitad del rango":
                meta = mitad
            else:
                meta = close[i] + rr * (close[i] - stop)
            lado = 1 if stop < close[i] < meta else 0
    else:
        if toma_max and close[i] > rmax and operar_largos:
            stop = rmax - margen_rup * a
            meta = close[i] + rr * (close[i] - stop)
            lado = 1 if stop < close[i] else 0
        elif toma_min and close[i] < rmin and operar_cortos:
            stop = rmin + margen_rup * a
            meta = close[i] - rr * (stop - close[i])
            lado = -1 if stop > close[i] else 0

    if lado == 0:
        continue
    if evitar_noticias and bloqueo_noticia[i]:
        bloqueada[i] = True  # señal válida cerca de una noticia: no se opera (el otro nivel aún puede dar señal)
        continue
    if lado > 0:
        compra[i], stop_l[i], obj_l[i] = True, stop, meta
    else:
        venta[i], stop_c[i], obj_c[i] = True, stop, meta
    opero = True

# ── Órdenes ──────────────────────────────────────────────────────────────────
if np.any(compra):  # strategy.exit necesita al menos un nivel: solo se registra si hay señales
    strategy.entry("Compra", "long", when=compra)
    strategy.exit("Stop/objetivo compra", from_entry="Compra", stop=stop_l, limit=obj_l)
if np.any(venta):
    strategy.entry("Venta", "short", when=venta)
    strategy.exit("Stop/objetivo venta", from_entry="Venta", stop=stop_c, limit=obj_c)
strategy.close_all(when=ultima_vela, comment="Fin de la ventana")
if cerrar_por_noticia:
    strategy.close_all(when=antes_de_noticia, comment="Antes de una noticia")

# ── Dibujo ───────────────────────────────────────────────────────────────────
plot(niv_max, etiqueta + " máximo", color=color.blue, linewidth=2, style="step")
plot(niv_min, etiqueta + " mínimo", color=color.blue, linewidth=2, style="step")
bgcolor(en_rango, color.blue)
plotshape(compra, "Compra", location="belowBar", color=color.green, shape="arrowUp", text="COMPRA")
plotshape(venta, "Venta", location="aboveBar", color=color.red, shape="arrowDown", text="VENTA")
plotshape(bloqueada, "Señal descartada por noticia", location="aboveBar", color=color.gray, shape="circle", text="N")
bgcolor(bloqueo_noticia & evitar_noticias, color.red)
alertcondition(compra, "Compra", "Señal de compra del rango de " + etiqueta)
alertcondition(venta, "Venta", "Señal de venta del rango de " + etiqueta)

senales = int(np.sum(compra | venta))
print("Señales:", senales, "(compras", int(np.sum(compra)), "· ventas", int(np.sum(venta)), ")")
if evitar_noticias:
    print("Descartadas por noticias:", int(np.sum(bloqueada)))
if senales < 30:
    print("Menos de 30 operaciones: el resultado del backtest todavía no dice nada. Carga más historial.")
print("Costes por defecto (comisión 0,01 % y deslizamiento 0,005 % por lado): ajústalos a tu bróker en strategy(...).")
