# NEXUS Terminal

Terminal de investigación de mercados **local**, con un motor de señales que **aprende de sus propios resultados** y,
sobre todo, un **filtro que te dice cuándo NO tomar una operación y cuándo sí**.

> ⚠️ **Aviso**: herramienta educativa y de investigación. Las señales y probabilidades se calculan con datos
> históricos y **no garantizan resultados futuros**. No es asesoría financiera. Todas las operaciones dentro de
> NEXUS son **simuladas** (cuenta demo); NEXUS no envía órdenes a ningún bróker.

---

## Qué incluye

| Módulo | Qué hace |
|---|---|
| **Mercados** | Explora los ~458.000 activos de TradingView (acciones de muchos países, cripto, forex, futuros, CFD, bonos, índices) con precio, cambio, volumen, rating técnico de TradingView, RSI, ADX, rendimiento y la lectura rápida de NEXUS. |
| **Gráfico** | Velas profesionales (Lightweight Charts de TradingView) + vista oficial de TradingView para cualquier símbolo. Más de 70 indicadores (medias, Bollinger, Ichimoku, Supertrend, SAR, RSI, MACD, estocástico, ADX, volumen, pivotes, ZigZag…), cada uno con sus parámetros y en su propio panel. |
| **NEXUS** | Motor adaptativo: un ensamble de más de 10 "expertos" (tendencia, momento, reversión, rupturas, volumen, temporalidad mayor…) cuyos pesos se ajustan solos según aciertan o fallan, más un modelo de probabilidad calibrado. Te dice **COMPRAR / VENDER / MANTENER / ESPERAR / CERRAR**, dibuja el **stop "hasta aquí"**, los objetivos TP1/TP2/TP3 y el stop dinámico, y detecta **en mitad de la operación** cuando conviene **cerrar para no perder más** (valor esperado negativo, divergencias, picos de volatilidad, cambio de régimen). |
| **Filtro "¿Tomo esta operación?"** | Cada señal pasa por un checklist (tendencia mayor, mercado lateral, momento, espacio hasta la resistencia, riesgo/beneficio, sobreextensión, volatilidad, volumen, probabilidad aprendida, historial de situaciones parecidas, valor esperado tras comisiones, racha reciente, confluencia). Resultado: **✅ TOMAR / ⏳ ESPERAR / ⛔ NO TOMAR** con nota 0-100 y los motivos. Los pesos de cada verificación **se aprenden** de los resultados reales. También puedes verificar tu propia idea (con tu entrada, stop y objetivo). |
| **Terminal** | Todo lo que pasa (señales, operaciones, escaneos, aprendizaje, errores) en vivo, y una línea de comandos: `signal btc`, `check eth long`, `scan crypto`, `backtest sol 4h`, `sim start`… |
| **Escáner** | Escanea un mercado entero, rankea oportunidades y, en modo profundo, aplica NEXUS + filtro a los mejores candidatos. Cada predicción se **guarda antes de conocer el resultado** y se verifica después: así se mide el acierto real y el modelo se reentrena (retroalimentación). |
| **Simulación** | Mercado sintético en tiempo real (regímenes alcista/bajista/lateral/volátil/crash), replay histórico vela a vela sin mirar el futuro, backtest **con y sin filtro** (para ver lo que aporta) y Monte Carlo (riesgo de ruina, VaR). |
| **Cuenta demo** | Paper trading con gestión automática de SL/TP parciales/stop dinámico, avisos de salida anticipada y auto-trading opcional que solo toma las operaciones que el filtro aprueba. |
| **PyChart Studio** | Escribe tus propios indicadores y estrategias en **Python** con una API estilo Pine Script (`ta.ema`, `plot`, `plotshape`, `strategy.entry`…). Compilador con errores por línea, ejecución aislada (sandbox) y backtest de estrategias. |
| **Aprendizaje** | Cómo se retroalimenta el sistema: acierto fuera de muestra, calibración, pesos aprendidos, decisiones del filtro y cuántas operaciones evitadas habrían perdido. |
| **Base de datos local** | SQLite (`data/nexus.db`), se crea sola al arrancar. Guarda velas, señales, predicciones, decisiones del filtro, modelos aprendidos, cuenta demo, scripts, escaneos y el registro de eventos. |

---

## Inicio rápido

Requisitos: **Python 3.11+** (recomendado 3.13) y **Node.js 18+**. Windows 10/11, Linux o macOS.

**Windows** — doble clic en `start.bat`, o en PowerShell:

```powershell
.\start.ps1
```

**Linux / macOS**:

```bash
./start.sh
```

La primera vez crea el entorno, instala dependencias y compila la interfaz (unos minutos). Las siguientes veces
arranca en segundos. Se abre el navegador en **http://127.0.0.1:8420**.

Opciones: `-Dev` (recarga en caliente en http://127.0.0.1:5173), `-Port 9000`, `-NoBrowser`, `-Rebuild`
(en `start.sh`: `--dev`, `--port N`, `--no-browser`).

Al arrancar, NEXUS levanta automáticamente: base de datos, datos en vivo, grabador local de velas, vigilancia de tu
lista (señales en la terminal), escáner periódico, evaluador de predicciones (aprendizaje) y cuenta demo.

### En la nube (accesible desde cualquier lugar)

Despliegue híbrido: **interfaz en Vercel**, **base de datos y login en Supabase** y el **backend siempre
encendido en Railway** (o Fly.io), porque NEXUS vigila el mercado 24/7. Con login obligatorio y solo para los
correos que autorices. Guía paso a paso: [docs/DEPLOY.md](docs/DEPLOY.md). El modo local sigue igual.

---

## Fuentes de datos

- **Universo, búsqueda y cotizaciones de todos los activos**: el escáner público de TradingView (endpoint no
  oficial, consultado con moderación y caché). Revisa los términos de uso de TradingView; puedes desactivarlo en
  *Ajustes → Proveedores*.
- **Historial completo de cripto**: APIs públicas oficiales de Binance (`data-api.binance.vision`), OKX, Coinbase,
  Kraken y KuCoin.
- **Historial de acciones, forex, índices y futuros**: añade una API key gratuita en *Ajustes → Proveedores*
  (Twelve Data recomendado; también Alpha Vantage o Polygon). Sin key, NEXUS **graba su propio historial local**
  a partir de las cotizaciones en vivo (crece con el tiempo) y la vista *TradingView* del gráfico muestra el
  histórico completo con los gráficos oficiales de TradingView.
- **Mercado sintético** (`SIM:NEXUS`) para simulación y pruebas sin conexión.

NEXUS se identifica honestamente en cada petición, usa los certificados del sistema (funciona detrás de proxies
corporativos) y **no** evade protecciones anti-bots ni suplanta navegadores.

---

## Cómo decide NEXUS

1. **Expertos** (−1…+1 cada uno): cinta de EMAs, MACD, RSI (momento en tendencia, reversión en rango), Bollinger,
   rupturas de Donchian, Supertrend, ADX/DMI, volumen/OBV, pendiente de regresión, tendencia de temporalidad
   mayor, StochRSI y acción del precio en soportes/resistencias.
2. **Régimen**: tendencia alcista, bajista, lateral o volátil (ADX, eficiencia de Kaufman, pendiente, volatilidad).
3. **Aprendizaje**: cuando se conoce el resultado de una predicción (h velas después), los pesos de los expertos
   se ajustan por régimen (pesos multiplicativos con "fixed-share") y se actualiza una regresión logística online
   que da la **probabilidad calibrada** de subida. Todo es **causal**: nunca usa información del futuro.
4. **Decisión** con histéresis → candidato de compra/venta → **filtro** → TOMAR / ESPERAR / NO TOMAR.
5. **Gestión**: stop inicial por ATR y swing, TP1/TP2/TP3 por múltiplos de R, parciales y stop a break-even,
   stop dinámico y **salida anticipada** cuando el valor esperado de mantener se vuelve negativo.
6. **Memoria**: el estado aprendido se guarda en la base de datos y se reutiliza sin mirar el futuro.

---

## Terminal: comandos útiles

| Comando | Para qué |
|---|---|
| `help` | Lista todos los comandos |
| `signal btc [1h]` | Señal NEXUS actual con niveles y consejo |
| `check eth long [--sl 3000 --tp 3600]` | **¿Tomo esta operación?** checklist completo |
| `watchlist-signals` | Veredictos de toda tu lista: ¿dónde hay oportunidad ahora? |
| `scan crypto 1h --deep` | Escanea un mercado y rankea oportunidades |
| `backtest sol 4h` | Backtest con y sin filtro |
| `mc btc 1h` | Monte Carlo de la estrategia |
| `sim start --speed 8` / `sim replay btc 1h` | Simulador de mercado |
| `buy btc --risk 1` / `close all` / `positions` | Cuenta demo |
| `search apple` · `quote aapl msft` · `chart nvda 1d` | Mercado |
| `status` · `db` · `events 50` | Sistema |

---

## PyChart (scripts en Python)

```python
indicator("Cruce de EMAs", overlay=True)
rapida = input.int(9, "EMA rápida", minval=2)
lenta = input.int(21, "EMA lenta", minval=3)

ema_r = ta.ema(close, rapida)
ema_l = ta.ema(close, lenta)
plot(ema_r, "EMA rápida", color=color.green)
plot(ema_l, "EMA lenta", color=color.red)

compra = ta.crossover(ema_r, ema_l) & (ta.adx(14) > 18)   # filtro de "no operar" en lateral
plotshape(compra, "Compra", location="belowbar", color=color.green, shape="arrowup", text="COMPRA")
```

Los scripts se validan antes de ejecutarse (solo `math`, `statistics` y `numpy`; sin acceso a archivos, red ni
sistema) y corren en un proceso aislado con límite de tiempo.

---

## Arquitectura

```
backend/   FastAPI + SQLite (aiosqlite) + numpy
  app/data/       proveedores de datos, hub en vivo, grabador local, escáner de TradingView
  app/engine/     NEXUS (expertos, régimen, aprendizaje, filtro, niveles, predicción) + 70+ indicadores
  app/sim/        backtest, Monte Carlo, simulador, cuenta demo, escáner, evaluador (retroalimentación)
  app/scripting/  PyChart: validador, sandbox, runtime estilo Pine, backtester
frontend/  React + TypeScript + Vite + Lightweight Charts v5 + CodeMirror
docs/ARCHITECTURE.md  contrato completo (API REST, WebSocket, base de datos, especificaciones)
```

Configuración por variables de entorno: `NEXUS_PORT` (8420), `NEXUS_HOST` (127.0.0.1), `NEXUS_DB_PATH`,
`NEXUS_OFFLINE=1` (sin red), `NEXUS_BACKGROUND=0` (sin servicios en segundo plano). Modo nube:
`NEXUS_DATABASE_URL` (Postgres/Supabase), `NEXUS_AUTH=supabase`, `NEXUS_SUPABASE_URL`,
`NEXUS_SUPABASE_ANON_KEY`, `NEXUS_ALLOWED_EMAILS`, `NEXUS_CORS_ORIGINS` y, en Vercel, `VITE_API_BASE`
(ver [.env.example](.env.example)). En local el servidor solo acepta conexiones de tu PC; en la nube exige login.

## Desarrollo

```bash
cd backend && .venv/Scripts/python.exe -m pytest -q      # pruebas del backend (sin red, SQLite)
NEXUS_TEST_PG_URL=postgresql://... .venv/Scripts/python.exe -m pytest -q   # las mismas pruebas sobre Postgres
cd frontend && npx tsc --noEmit -p tsconfig.json          # chequeo de tipos de la interfaz
```

Documentación interactiva de la API: http://127.0.0.1:8420/api/docs
