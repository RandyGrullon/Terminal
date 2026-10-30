# ─────────────────────────────────────────────────────────────────────────────
#  NEXUS-lite: mini ensamble con filtro de no operar
#
#  Versión de juguete del motor NEXUS. Cinco "expertos" opinan entre -1
#  (bajista) y +1 (alcista); el score es su media. Un score alto es un
#  CANDIDATO, no una orden: antes pasa por el filtro de NO OPERAR, igual que la
#  Puerta de NEXUS (Trade Gate). No se entra cuando:
#    1) ADX < 18          → no hay tendencia: las señales fallan más.
#    2) |precio − EMA| > 2.5 ATR → precio estirado: se compra caro / vende barato.
#    3) volumen débil (z < −0.5) → nadie acompaña el movimiento.
#  Desactiva el filtro en las entradas para comparar el backtest con y sin él.
#  Ningún indicador predice el futuro con certeza: mide, compara y decide.
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np

strategy("NEXUS-lite", initial_capital=10000, risk_pct=1, fee_pct=0.1)

umbral = input.float(0.35, "Umbral de entrada del score")
usar_filtro = input.bool(True, "Aplicar filtro de no operar")
adx_min = input.float(18.0, "ADX mínimo")
max_atr = input.float(2.5, "Distancia máxima a la EMA (ATR)")
vol_z_min = input.float(-0.5, "Volumen: z-score mínimo")
stop_atr = input.float(2.0, "Stop (ATR)")
objetivo_r = input.float(2.0, "Objetivo (R)")
permitir_cortos = input.bool(True, "Permitir cortos")

n = len(close)


def atras(serie, k=1):
    """Valor de hace k velas (serie[k] en Pine)."""
    serie = np.asarray(serie, dtype=float)
    out = np.full(len(serie), np.nan)
    if 0 < k < len(serie):
        out[k:] = serie[:-k]
    return out


def dividir(a, b):
    """a / b sin avisos: NaN donde b no es positivo o falta."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float) * np.ones(a.shape)
    return np.divide(a, b, out=np.full(a.shape, np.nan), where=b > 0)


# ── Indicadores base ────────────────────────────────────────────────────────
ema21 = ta.ema(close, 21)
ema55 = ta.ema(close, 55)
atr = ta.atr(14)
adx = ta.adx(14)
rsi = ta.rsi(close, 14)
macd, senal, hist = ta.macd(close, 12, 26, 9)
techo = atras(ta.highest(high, 20))  # canal de las 20 velas ANTERIORES
suelo = atras(ta.lowest(low, 20))
obv = np.cumsum(np.sign(np.diff(close, prepend=close[0])) * volume)
vol_z = dividir(volume - ta.sma(volume, 20), ta.stdev(volume, 20))

# ── Expertos (cada uno entre -1 y +1) ───────────────────────────────────────
expertos = {
    "Tendencia EMA": np.clip(dividir(ema21 - ema55, atr), -1, 1),
    "Momento MACD": np.clip(dividir(hist, 0.25 * atr), -1, 1),
    "RSI": np.clip((rsi - 50) / 20, -1, 1),
    "Donchian": np.clip(dividir(close - (techo + suelo) / 2, (techo - suelo) / 2), -1, 1),
    "Volumen OBV": np.clip(dividir(obv - ta.ema(obv, 20), 2 * ta.stdev(obv, 20)), -1, 1),
}
matriz = np.vstack([np.nan_to_num(v, nan=0.0) for v in expertos.values()])
score = np.mean(matriz, axis=0)
score[np.isnan(atr) | np.isnan(adx)] = np.nan  # sin datos suficientes todavía
acuerdo = np.mean(np.sign(matriz) == np.sign(score), axis=0)  # fracción de expertos de acuerdo

# ── Candidatos y filtro de NO OPERAR ────────────────────────────────────────
candidato_largo = ta.crossover(score, np.full(n, umbral))
candidato_corto = ta.crossunder(score, np.full(n, -umbral))

adx_bajo = adx < adx_min
estirado = np.abs(close - ema21) > max_atr * atr
vol_debil = vol_z < vol_z_min
no_operar = (adx_bajo | estirado | vol_debil) if usar_filtro else np.zeros(n, dtype=bool)

entrada_larga = candidato_largo & ~no_operar
entrada_corta = candidato_corto & ~no_operar & permitir_cortos
bloqueada = (candidato_largo | (candidato_corto & permitir_cortos)) & no_operar

# Stop "hasta aquí" y objetivo: se fijan en la vela de la señal. Se sale antes
# si el score cruza el cero en contra (el ensamble cambió de opinión).
riesgo = stop_atr * atr
strategy.entry("Largo", "long", when=entrada_larga)
strategy.exit("Salida largo", from_entry="Largo", stop=close - riesgo, limit=close + objetivo_r * riesgo)
strategy.close("Largo", when=ta.crossunder(score, np.zeros(n)))
if permitir_cortos:
    strategy.entry("Corto", "short", when=entrada_corta)
    strategy.exit("Salida corto", from_entry="Corto", stop=close + riesgo, limit=close - objetivo_r * riesgo)
    strategy.close("Corto", when=ta.crossover(score, np.zeros(n)))

# ── Dibujo ──────────────────────────────────────────────────────────────────
plot(ema21, "EMA 21", color=color.blue, linewidth=1)
plot(score, "Score NEXUS-lite", color=color.purple, linewidth=2, pane=1)
plot(np.full(n, umbral), "Umbral compra", color=color.green, linewidth=1, pane=1)
plot(np.full(n, -umbral), "Umbral venta", color=color.red, linewidth=1, pane=1)
plot(acuerdo * 100, "Acuerdo de expertos %", color=color.gray, linewidth=1, pane=2)
bgcolor(no_operar, color.gray)  # zona de NO OPERAR
plotshape(bloqueada, "Señal bloqueada por el filtro", location="aboveBar", color=color.orange, shape="square", text="NO")

alertcondition(entrada_larga, "NEXUS-lite compra", "Score alcista y el filtro permite operar")
alertcondition(entrada_corta, "NEXUS-lite venta", "Score bajista y el filtro permite operar")
alertcondition(bloqueada, "Señal bloqueada", "Había señal pero el filtro dice NO OPERAR")

# ── Resumen en la consola ───────────────────────────────────────────────────
candidatos = candidato_largo | (candidato_corto & permitir_cortos)
filtrables = candidatos & (adx_bajo | estirado | vol_debil)
print("Candidatos:", int(np.sum(candidatos)), "· tomados:", int(np.sum(entrada_larga | entrada_corta)),
      "· bloqueados:", int(np.sum(bloqueada)))
motivos_txt = ("ADX bajo: " + str(int(np.sum(candidatos & adx_bajo))) + " · precio estirado: "
               + str(int(np.sum(candidatos & estirado))) + " · volumen débil: " + str(int(np.sum(candidatos & vol_debil))))
if usar_filtro:
    print("Bloqueos por motivo →", motivos_txt)
else:
    print("Filtro desactivado: el filtro habría bloqueado", int(np.sum(filtrables)), "candidatos →", motivos_txt)
if n and not np.isnan(score[-1]):
    motivos = [m for m, activo in (("ADX bajo", adx_bajo[-1]), ("precio estirado", estirado[-1]),
                                    ("volumen débil", vol_debil[-1])) if activo]
    estado = "NO OPERAR (" + ", ".join(motivos) + ")" if motivos else "se puede operar"
    print("Ahora: score", round(float(score[-1]), 2), "· acuerdo", int(round(acuerdo[-1] * 100)), "% ·", estado)
