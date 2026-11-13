# ─────────────────────────────────────────────────────────────────────────────
#  Rangos de sesión: Sídney → Asia y Londres → Nueva York
#
#  Marca el máximo y el mínimo de dos sesiones «de rango» y los deja fijos
#  durante la sesión en la que quieres operar:
#    · Rango de Sídney  → se opera en Asia.
#    · Rango de Londres → se opera en Nueva York.
#  Mientras el rango se forma se dibuja su máximo/mínimo provisional (puede
#  cambiar); cuando la sesión termina, los niveles quedan FIJOS hasta el final
#  de la ventana de operación.
#
#  En la ventana de operación marca la PRIMERA vez que el precio toma cada nivel:
#    · R = ruptura: la vela CIERRA fuera del nivel.
#    · B = barrida: la mecha pasa el nivel pero la vela cierra DENTRO.
#  La consola resume, como «x de n», qué pasó después de cada ruptura y cada
#  barrida en el historial cargado. Es estadística descriptiva, no una ventaja:
#  para eso hace falta un backtest con reglas de entrada, stop y costes.
#
#  Noticias: colorea de rojo las velas cerca de las noticias principales (IPC,
#  informe de empleo y Fed) del calendario de NEXUS (historial desde febrero de
#  2024) y marca con «N» la vela de cada publicación.
#
#  Horas en la zona elegida. «Nueva York» aplica su horario de verano (2.º
#  domingo de marzo → 1.er domingo de noviembre). En noviembre-marzo la hora de
#  Santo Domingo (UTC−4) es una hora MÁS que la de Nueva York.
#  Sídney y Londres cambian la hora en otras fechas que Nueva York (Sídney de
#  octubre a abril, Londres del último domingo de marzo al de octubre): con las
#  horas fijas en Nueva York, su sesión real se corre 1 h (Londres, unas semanas
#  en marzo y en octubre-noviembre) o hasta 2 h (Sídney) en parte del año.
#  Ajusta las horas si quieres seguir su hora local.
#  Todo es causal: cada vela solo usa velas ya cerradas. Pensado para 1–15 min.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

indicator("Rangos de sesión: Sídney → Asia, Londres → NY", overlay=True)

zona = input.select("Nueva York", "Zona horaria de las horas", options=["Nueva York", "Santo Domingo", "UTC"])
usar_sydney = input.bool(True, "Sídney → Asia")
syd_desde = input.float(17.0, "Rango Sídney: desde (hora, 17.5 = 17:30)", minval=0, maxval=24, step=0.25)
syd_hasta = input.float(19.0, "Rango Sídney: hasta", minval=0, maxval=24, step=0.25)
asia_desde = input.float(19.0, "Operar en Asia: desde", minval=0, maxval=24, step=0.25)
asia_hasta = input.float(2.0, "Operar en Asia: hasta", minval=0, maxval=24, step=0.25)
usar_londres = input.bool(True, "Londres → Nueva York")
lon_desde = input.float(2.0, "Rango Londres: desde", minval=0, maxval=24, step=0.25)
lon_hasta = input.float(7.0, "Rango Londres: hasta", minval=0, maxval=24, step=0.25)
ny_desde = input.float(7.0, "Operar en NY: desde", minval=0, maxval=24, step=0.25)
ny_hasta = input.float(11.0, "Operar en NY: hasta", minval=0, maxval=24, step=0.25)
marcar = input.bool(True, "Marcar rupturas (R) y barridas (B)")
sombrear_operar = input.bool(False, "Sombrear también las ventanas de operación")
marcar_noticias = input.bool(True, "Marcar noticias principales (IPC, empleo, Fed)")
minutos_noticia = input.int(30, "Minutos antes y después de cada noticia", minval=0, maxval=240)

n = len(close)
DIA = 86400


# ── Hora local de cada vela ──────────────────────────────────────────────────
def domingo_numero(anio, mes, k):
    """Día (días desde 1970-01-01) del k-ésimo domingo del mes."""
    d0 = int(np.datetime64(str(anio) + mes + "-01").astype(np.int64))
    dia_semana = (d0 + 3) % 7  # 0 = lunes … 6 = domingo (1970-01-01 fue jueves)
    return d0 + (6 - dia_semana) % 7 + 7 * (k - 1)


if zona == "Nueva York":
    anios = time.astype("datetime64[s]").astype("datetime64[Y]").astype(np.int64) + 1970
    desfase = np.full(n, -5 * 3600, dtype=np.int64)
    for anio in np.unique(anios):
        a = int(anio)
        inicio_verano = domingo_numero(a, "-03", 2) * DIA + 7 * 3600  # 2:00 hora estándar = 07:00 UTC
        fin_verano = domingo_numero(a, "-11", 1) * DIA + 6 * 3600  # 2:00 hora de verano = 06:00 UTC
        en_verano = (anios == anio) & (time >= inicio_verano) & (time < fin_verano)
        desfase = np.where(en_verano, -4 * 3600, desfase)
elif zona == "Santo Domingo":
    desfase = np.full(n, -4 * 3600, dtype=np.int64)
else:
    desfase = np.zeros(n, dtype=np.int64)

local = time.astype(np.int64) + desfase
minuto = (local // 60) % 1440
dia = local // DIA


def en_ventana(desde, hasta):
    """Velas cuya hora de apertura cae en [desde, hasta); admite ventanas que cruzan la medianoche."""
    a, b = int(round(desde * 60)) % 1440, int(round(hasta * 60)) % 1440
    if a == b:
        return np.zeros(n, dtype=bool), a, b
    if a < b:
        return (minuto >= a) & (minuto < b), a, b
    return (minuto >= a) | (minuto < b), a, b


def id_sesion(dentro, a, b):
    """Día local en que EMPEZÓ la sesión (en ventanas que cruzan la medianoche, lo de después de las 0:00 es del día anterior)."""
    if a > b:
        return np.where(dentro, dia - (minuto < b).astype(np.int64), -1)
    return np.where(dentro, dia, -1)


# ── Motor común: un rango y su ventana de operación ──────────────────────────
def procesar(r_desde, r_hasta, o_desde, o_hasta):
    en_rango, ra, rb = en_ventana(r_desde, r_hasta)
    en_operar, _, _ = en_ventana(o_desde, o_hasta)
    sid = id_sesion(en_rango, ra, rb)

    prov_max = np.full(n, np.nan)
    prov_min = np.full(n, np.nan)
    niv_max = np.full(n, np.nan)
    niv_min = np.full(n, np.nan)
    rup_arriba = np.zeros(n, dtype=bool)
    bar_arriba = np.zeros(n, dtype=bool)
    rup_abajo = np.zeros(n, dtype=bool)
    bar_abajo = np.zeros(n, dtype=bool)
    ciclos = []

    estado = "fuera"  # fuera · formando · fijo
    sesion_actual = -1
    rmax = rmin = np.nan
    ciclo = None
    estuvo_en_ventana = False

    for i in range(n):
        if en_rango[i]:
            if estado != "formando" or sid[i] != sesion_actual:
                if ciclo is not None and estuvo_en_ventana:
                    ciclos.append(ciclo)  # la ventana anterior quedó sin cerrar del todo: se cuenta igual
                estado, sesion_actual = "formando", sid[i]
                rmax, rmin = high[i], low[i]
                ciclo, estuvo_en_ventana = None, False
            else:
                rmax, rmin = max(rmax, high[i]), min(rmin, low[i])
            prov_max[i], prov_min[i] = rmax, rmin
            continue

        if estado == "formando":  # primera vela fuera del rango: los niveles quedan fijos
            estado = "fijo"
            ciclo = {"max": rmax, "min": rmin, "toma_max": "", "toma_min": "",
                     "llega_min_tras_barrida": False, "llega_max_tras_barrida": False, "cierre": np.nan}

        if estado != "fijo":
            continue

        if en_operar[i]:
            estuvo_en_ventana = True
            niv_max[i], niv_min[i] = ciclo["max"], ciclo["min"]
            # después de una barrida, ¿llega el precio al lado contrario del rango dentro de la ventana?
            if ciclo["toma_max"] == "B" and low[i] <= ciclo["min"]:
                ciclo["llega_min_tras_barrida"] = True
            if ciclo["toma_min"] == "B" and high[i] >= ciclo["max"]:
                ciclo["llega_max_tras_barrida"] = True
            if ciclo["toma_max"] == "" and high[i] > ciclo["max"]:
                if close[i] > ciclo["max"]:
                    ciclo["toma_max"], rup_arriba[i] = "R", True
                else:
                    ciclo["toma_max"], bar_arriba[i] = "B", True
            if ciclo["toma_min"] == "" and low[i] < ciclo["min"]:
                if close[i] < ciclo["min"]:
                    ciclo["toma_min"], rup_abajo[i] = "R", True
                else:
                    ciclo["toma_min"], bar_abajo[i] = "B", True
            ciclo["cierre"] = close[i]
        elif estuvo_en_ventana:  # terminó la ventana de operación: ciclo completo
            ciclos.append(ciclo)
            estado, ciclo, estuvo_en_ventana = "fuera", None, False
        else:  # entre el fin del rango y el inicio de la ventana: niveles visibles, sin señales
            niv_max[i], niv_min[i] = ciclo["max"], ciclo["min"]

    return {"prov_max": prov_max, "prov_min": prov_min, "max": niv_max, "min": niv_min,
            "rup_arriba": rup_arriba, "bar_arriba": bar_arriba, "rup_abajo": rup_abajo, "bar_abajo": bar_abajo,
            "en_rango": en_rango, "en_operar": en_operar, "ciclos": ciclos}


def resumen(nombre, r):
    c = r["ciclos"]
    total = len(c)
    print("──", nombre, "──", total, "sesiones completas en el historial cargado")
    if total == 0:
        print("   Sin sesiones completas: carga más historial o revisa las horas.")
        return
    tmax = sum(1 for x in c if x["toma_max"])
    tmin = sum(1 for x in c if x["toma_min"])
    ambos = sum(1 for x in c if x["toma_max"] and x["toma_min"])
    ninguno = sum(1 for x in c if not x["toma_max"] and not x["toma_min"])
    print("   Tomó el máximo", tmax, "de", total, "· el mínimo", tmin, "de", total,
          "· ambos", ambos, "de", total, "· ninguno", ninguno, "de", total)

    rup_up = [x for x in c if x["toma_max"] == "R"]
    rup_dn = [x for x in c if x["toma_min"] == "R"]
    bar_up = [x for x in c if x["toma_max"] == "B"]
    bar_dn = [x for x in c if x["toma_min"] == "B"]
    sigue_up = sum(1 for x in rup_up if x["cierre"] > x["max"])
    sigue_dn = sum(1 for x in rup_dn if x["cierre"] < x["min"])
    gira_up = sum(1 for x in bar_up if x["llega_min_tras_barrida"])
    gira_dn = sum(1 for x in bar_dn if x["llega_max_tras_barrida"])
    print("   Ruptura del máximo:", len(rup_up), "· terminó la ventana por encima:", sigue_up, "de", len(rup_up))
    print("   Ruptura del mínimo:", len(rup_dn), "· terminó la ventana por debajo:", sigue_dn, "de", len(rup_dn))
    print("   Barrida del máximo:", len(bar_up), "· luego llegó al mínimo del rango:", gira_up, "de", len(bar_up))
    print("   Barrida del mínimo:", len(bar_dn), "· luego llegó al máximo del rango:", gira_dn, "de", len(bar_dn))
    if total < 30:
        print("   Muestra pequeña (menos de 30 sesiones): no saques conclusiones.")


# ── Cálculo y dibujo ─────────────────────────────────────────────────────────
if n > 1 and float(np.median(np.diff(time))) > 3600:
    print("Aviso: este indicador está pensado para velas de 1 h o menos; con velas mayores las sesiones se deforman.")

vacio = np.full(n, np.nan)
nada = np.zeros(n, dtype=bool)

if usar_sydney:
    sy = procesar(syd_desde, syd_hasta, asia_desde, asia_hasta)
else:
    sy = {"prov_max": vacio, "prov_min": vacio, "max": vacio, "min": vacio, "rup_arriba": nada,
          "bar_arriba": nada, "rup_abajo": nada, "bar_abajo": nada, "en_rango": nada, "en_operar": nada, "ciclos": []}

if usar_londres:
    ld = procesar(lon_desde, lon_hasta, ny_desde, ny_hasta)
else:
    ld = {"prov_max": vacio, "prov_min": vacio, "max": vacio, "min": vacio, "rup_arriba": nada,
          "bar_arriba": nada, "rup_abajo": nada, "bar_abajo": nada, "en_rango": nada, "en_operar": nada, "ciclos": []}

plot(sy["prov_max"], "Sídney máx. (formándose)", color=color.new(color.purple, 50), linewidth=1, style="step")
plot(sy["prov_min"], "Sídney mín. (formándose)", color=color.new(color.purple, 50), linewidth=1, style="step")
plot(sy["max"], "Sídney máximo", color=color.purple, linewidth=2, style="step")
plot(sy["min"], "Sídney mínimo", color=color.purple, linewidth=2, style="step")
plot(ld["prov_max"], "Londres máx. (formándose)", color=color.new(color.blue, 50), linewidth=1, style="step")
plot(ld["prov_min"], "Londres mín. (formándose)", color=color.new(color.blue, 50), linewidth=1, style="step")
plot(ld["max"], "Londres máximo", color=color.blue, linewidth=2, style="step")
plot(ld["min"], "Londres mínimo", color=color.blue, linewidth=2, style="step")

bgcolor(sy["en_rango"], color.purple)
bgcolor(ld["en_rango"], color.blue)
if sombrear_operar:
    bgcolor(sy["en_operar"] & ~sy["en_rango"], color.yellow)
    bgcolor(ld["en_operar"] & ~ld["en_rango"], color.orange)

if marcar:
    plotshape(sy["rup_arriba"], "Sídney: ruptura del máximo", location="belowBar", color=color.green, shape="arrowUp", text="Sy R")
    plotshape(sy["bar_arriba"], "Sídney: barrida del máximo", location="aboveBar", color=color.red, shape="arrowDown", text="Sy B")
    plotshape(sy["rup_abajo"], "Sídney: ruptura del mínimo", location="aboveBar", color=color.red, shape="arrowDown", text="Sy R")
    plotshape(sy["bar_abajo"], "Sídney: barrida del mínimo", location="belowBar", color=color.green, shape="arrowUp", text="Sy B")
    plotshape(ld["rup_arriba"], "Londres: ruptura del máximo", location="belowBar", color=color.green, shape="arrowUp", text="Ld R")
    plotshape(ld["bar_arriba"], "Londres: barrida del máximo", location="aboveBar", color=color.red, shape="arrowDown", text="Ld B")
    plotshape(ld["rup_abajo"], "Londres: ruptura del mínimo", location="aboveBar", color=color.red, shape="arrowDown", text="Ld R")
    plotshape(ld["bar_abajo"], "Londres: barrida del mínimo", location="belowBar", color=color.green, shape="arrowUp", text="Ld B")

if marcar_noticias:
    bgcolor(noticias.cerca(minutos_noticia, minutos_noticia), color.red)
    plotshape(noticias.cerca(0, 0), "Noticia principal", location="aboveBar", color=color.red, shape="circle", text="N")

alertcondition(sy["rup_arriba"] | sy["rup_abajo"], "Ruptura del rango de Sídney", "El precio cerró fuera del rango de Sídney")
alertcondition(sy["bar_arriba"] | sy["bar_abajo"], "Barrida del rango de Sídney", "Mecha fuera del rango de Sídney y cierre dentro")
alertcondition(ld["rup_arriba"] | ld["rup_abajo"], "Ruptura del rango de Londres", "El precio cerró fuera del rango de Londres")
alertcondition(ld["bar_arriba"] | ld["bar_abajo"], "Barrida del rango de Londres", "Mecha fuera del rango de Londres y cierre dentro")

if usar_sydney:
    resumen("Sídney → Asia", sy)
if usar_londres:
    resumen("Londres → Nueva York", ld)
if marcar_noticias and noticias.disponible:
    print("Noticias principales en el periodo:", len(noticias.lista()))
print("Descriptivo, no es una ventaja: para saber si se puede operar hace falta un backtest con stop, objetivo y costes.")
