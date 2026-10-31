# NEXUS Terminal

[![CI](https://github.com/RandyGrullon/Terminal/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/RandyGrullon/Terminal/actions/workflows/ci.yml)

Terminal de investigación de mercados que corre **en tu propio equipo**: gráficos de cualquier activo de TradingView,
un motor de señales (NEXUS) que aprende de sus propios resultados, un filtro que te dice cuándo **no** tomar una
operación, un semáforo de condiciones de mercado, escáner, simulador y backtests, cuenta demo, scripts en Python
(PyChart) y una terminal de comandos.

> **Antes de usarlo**
>
> - **Es una herramienta de investigación y simulación, sin ventaja demostrada.** La validación walk-forward publicada
>   ([docs/VALIDATION.md](docs/VALIDATION.md)) concluye **«sin evidencia de ventaja»** (31/10/2026, 5 pares de Binance
>   × 15m/1h/4h/1d, motor final de la ronda de mejoras): NEXUS + filtro pierde −0,08R por operación (437 operaciones;
>   IC 95 % por bloques −0,18R a +0,02R) y queda 0,13R por debajo de entradas al azar con las mismas reglas de riesgo;
>   sin el filtro pierde −0,23R por operación (1.907 operaciones). El filtro reduce la pérdida sobre todo porque opera
>   menos. Un «TOMAR» no significa que la operación tenga ventaja (sección 4 de ese documento).
> - **Todo es simulado**: la cuenta demo nunca envía órdenes a un bróker ni mueve dinero real.
> - Señales, probabilidades y estadísticas se calculan con datos históricos y **no garantizan resultados futuros**.
>   No es asesoría financiera.

**Contenido**: [Qué es y qué no es](#qué-es-y-qué-no-es) · [Qué incluye](#qué-incluye) · [Requisitos](#requisitos) ·
[Instalación y arranque](#instalación-y-arranque) · [Primeros pasos](#primeros-pasos) ·
[Terminal](#terminal-comandos-útiles) · [Configuración](#configuración-todo-opcional) ·
[Fuentes de datos](#fuentes-de-datos) · [Pruebas](#pruebas) · [Integración continua](#integración-continua-github-actions) ·
[En la nube](#en-la-nube) · [Solución de problemas](#solución-de-problemas) · [Uso con Claude Code](#uso-con-claude-code)

---

## Qué es y qué no es

**Es** un laboratorio local para estudiar mercados y practicar sin riesgo:

- ver cualquier activo con las señales de NEXUS explicadas (expertos, régimen, niveles, motivos del filtro);
- comprobar una idea propia con el filtro «¿Tomo esta operación?» antes de simularla;
- medir con honestidad: cada predicción se guarda **antes** de conocer el resultado y el acierto se compara con una
  referencia exigente (no con «el 50 %»), como «x de n» y sin conclusiones con muestras pequeñas;
- simular: mercado sintético, replay vela a vela, backtests con y sin filtro, Monte Carlo y una cuenta demo con
  órdenes a mercado, límite y stop y límites de riesgo de cartera;
- programar indicadores y estrategias en Python (PyChart) y hacer su backtest.

**No es** un bróker (no conecta con cuentas reales), ni un sistema que gane dinero (la validación no encontró ventaja
frente al azar), ni asesoría financiera. Para usarlo en local no hace falta crear ninguna cuenta (las claves de
proveedores son opcionales): tus datos (operaciones demo, scripts, ajustes y claves) quedan en `data/nexus.db`.
NEXUS solo pide datos de mercado a fuentes públicas y, si las activas, envía alertas a tu propio bot de Telegram o a
tu servidor de correo.

## Qué incluye

| Sección | Qué hace |
|---|---|
| **Gráfico** | Velas con las señales de NEXUS, las señales que el filtro rechazó (con el motivo), niveles SL/TP «hasta aquí», cono de predicción y más de 100 indicadores con sus parámetros; también la vista oficial de TradingView. Una etiqueta dice si el precio es en tiempo real, con retraso, de mercado cerrado, desactualizado o sintético. |
| **Mercados** | Los ~458.000 activos del escáner público de TradingView (acciones de muchos países, cripto, forex, futuros, CFD, bonos, índices) con precio, cambio, volumen, rating técnico, RSI, ADX, rendimiento y la lectura rápida de NEXUS. |
| **Escáner** | Recorre un mercado entero, ordena oportunidades y, en modo profundo, aplica NEXUS + filtro a los mejores. Cada predicción se guarda antes del resultado y se verifica después; el modelo del escáner se reentrena con esos resultados. |
| **Simulación** | Mercado simulado (sintético con regímenes alcista, bajista, lateral, volátil y crash, o replay vela a vela de un activo real sin mirar el futuro), backtest **con y sin filtro** y Monte Carlo (riesgo de ruina, VaR). |
| **Cuenta demo** | Operaciones simuladas a mercado, **órdenes límite y stop pendientes**, gestión automática de SL, TP parciales y stop dinámico, avisos de salida, **límites de riesgo de cartera** y auto-trading opcional que solo abre señales TOMAR. |
| **PyChart** | Indicadores y estrategias en **Python** con una API estilo Pine Script (`ta.ema`, `plot`, `plotshape`, `strategy.entry`…): errores por línea, ejecución aislada (sandbox) y backtest. |
| **Aprendizaje** | Cómo se retroalimenta el sistema: acierto fuera de muestra frente a la referencia, calibración, pesos aprendidos, decisiones del filtro y la **validación walk-forward** («¿hay ventaja real?»). |
| **Ajustes** | Riesgo, NEXUS, filtro, escáner, proveedores de datos y sus claves, cuenta demo, alertas, riesgo de cartera, tiempo real y validación; estado del sistema y de la base de datos. |
| **Semáforo** (cabecera) | *Operar*, *Precaución* o *No operar* según la Fed, el calendario económico de esta semana y la próxima, la volatilidad (VIX), futuros, dólar, tipos y *funding*. En rojo, el filtro dice NO TOMAR. |
| **Terminal** (botón de la cabecera o ``Ctrl+` ``) | Todo lo que pasa, en vivo (señales, operaciones, escaneos, aprendizaje, errores), y una línea de comandos con alias en español. |

Además: NEXUS combina 12 «expertos» (tendencia, momento, reversión, rupturas, volumen, temporalidad mayor…) cuyos
pesos se ajustan solos según aciertan; alertas fuera de la app (Telegram, correo, notificaciones del navegador);
precios en tiempo real con tu propia clave de proveedor; y una base de datos local SQLite que se crea y se migra sola.

---

## Requisitos

- **Git**, para clonar el repositorio.
- **Python 3.11 o superior** (probado con 3.13, la versión de CI y de la imagen Docker).
- **Node.js 18 o superior** (probado con Node 20, el de CI), para compilar la interfaz. Sin Node, el arranque usa la
  interfaz ya compilada si existe.
- Windows 10/11, macOS o Linux, y un navegador actual.
- Conexión a internet para los datos de mercado (sin red funciona el mercado sintético `SIM:NEXUS`).
- Opcional: [uv](https://docs.astral.sh/uv/), que instala más rápido y, en Windows, usa los certificados del sistema.

## Instalación y arranque

```bash
git clone https://github.com/RandyGrullon/Terminal.git
cd Terminal
```

**Windows**: doble clic en `start.bat`, o desde PowerShell o CMD:

```powershell
.\start.bat
```

`start.bat` lanza `start.ps1` con `-ExecutionPolicy Bypass`, porque la política predeterminada de Windows no deja
ejecutar `.\start.ps1` directamente. Equivalente en PowerShell: `powershell -ExecutionPolicy Bypass -File .\start.ps1`.

**macOS / Linux**:

```bash
bash start.sh
```

(o `./start.sh`: el archivo es ejecutable en el repositorio; si lo descargaste sin permisos, usa `bash start.sh`).

La primera vez el script crea `backend/.venv`, instala `backend/requirements-dev.txt`, ejecuta `npm ci`, compila la
interfaz y crea la base de datos `data/nexus.db` (unos minutos). Las siguientes veces arranca en segundos: solo
reinstala o recompila lo que cambió. El navegador se abre en **http://127.0.0.1:8420** cuando el servidor responde.
Para detenerlo, `Ctrl+C` en esa ventana. No hace falta ningún archivo `.env` en local.

| Opción | Windows (`start.bat`) | macOS / Linux (`start.sh`) |
|---|---|---|
| Recarga en caliente (Vite en http://127.0.0.1:5173 + API) | `-Dev` | `--dev` |
| Otro puerto del servidor | `-Port 9000` | `--port 9000` |
| No abrir el navegador | `-NoBrowser` | `--no-browser` |
| Recompilar la interfaz aunque no haya cambios | `-Rebuild` | (recompila sola cuando cambia algo) |

Al arrancar, NEXUS levanta sus servicios: datos en vivo, grabador local de velas, semáforo, vigilancia de tu lista de
seguimiento (señales en la terminal), escáner periódico, verificador de predicciones (aprendizaje), cuenta demo,
simulador, PyChart, mantenimiento (retención de datos), alertas y validación (esta solo trabaja cuando se la pides).
*Ajustes → Sistema* muestra el estado de cada uno.

**Actualizar**: `git pull` y vuelve a arrancar. El script reinstala dependencias o recompila la interfaz solo si
cambiaron, y la base de datos se migra sola al arrancar (migraciones numeradas que no borran datos).

<details>
<summary>Arranque manual (lo mismo que hace el script)</summary>

```bash
cd backend
python -m venv .venv                      # o: uv venv .venv --python 3.13
.venv/Scripts/python -m pip install -r requirements-dev.txt   # macOS/Linux: .venv/bin/python
cd ../frontend && npm ci && npm run build
cd ../backend && PYTHONUTF8=1 .venv/Scripts/python -m app.main   # http://127.0.0.1:8420
```

En PowerShell, en lugar de `PYTHONUTF8=1 …`: `$env:PYTHONUTF8 = '1'` y luego `.venv\Scripts\python -m app.main`.
</details>

---

## Primeros pasos

Un recorrido por lo principal. Las secciones están en la barra de la izquierda; el símbolo y la temporalidad activos,
en la cabecera (buscador, que se enfoca con `/`, y botones de 1m a 1w).

### 1. Gráfico

Busca un símbolo (`btc`, `aapl`, `eurusd`, `BINANCE:ETHUSDT`…) y elige la temporalidad. En **Gráfico** verás:

- las señales de NEXUS y las que el filtro rechazó (capa «Rechazadas»: pasa el cursor para ver el motivo). Las capas
  (señales, niveles SL/TP, cono de predicción, volumen, EMAs, panel NEXUS) se activan con los botones de la barra;
- **Indicadores** (atajo `i`): más de 100, cada uno con sus parámetros y en su propio panel;
- el panel lateral: la señal actual con su consejo, la tarjeta «¿Tomo esta operación?» y tu lista de seguimiento;
- la etiqueta de frescura del precio: «Tiempo real · fuente», «Retraso 15 min · TradingView», «Mercado cerrado»,
  «Desactualizado · hace…» o «Sintético». Sin clave de proveedor, las acciones de EE. UU. llegan con el retraso que
  anuncia TradingView;
- el botón **TradingView**: el gráfico oficial de TradingView, con su historial completo y sus indicadores.

### 2. Semáforo de mercado

La píldora de la cabecera resume las condiciones: **Operar**, **Precaución** o **No operar**. Al pulsarla ves por qué
(«Lo que importa ahora»), los próximos eventos de alto impacto (FOMC, IPC, empleo…) y el calendario de **esta semana
y la próxima** (ForexFactory más las fechas oficiales de la Fed, BLS, BEA, BCE, BoE y BoJ). En rojo (por defecto,
30 min antes y después de un dato importante, o con volatilidad extrema) el filtro dice NO TOMAR y las compras o
ventas pasan a ESPERAR; en ámbar baja la nota del filtro. Las posiciones abiertas reciben un aviso, no se cierran
solas. Terminal: `macro`, `calendario`.

### 3. Filtro «¿Tomo esta operación?»

Cada señal de NEXUS pasa por 13 verificaciones (tendencia mayor, mercado con dirección, momento, espacio hasta el
objetivo, riesgo/beneficio, sobreextensión, volatilidad, volumen, probabilidad aprendida, historial de señales
similares, valor esperado tras comisiones, racha de pérdidas y confluencia de expertos) más el semáforo. Resultado:
**TOMAR / ESPERAR / NO TOMAR**, con una nota de 0 a 100 y los motivos; los pesos de cada verificación se aprenden de
los resultados. Para comprobar **tu propia idea**, usa la tarjeta «¿Tomo esta operación?» del gráfico (dirección y,
si quieres, entrada, stop y objetivo) o la terminal: `check eth long --sl 3000 --tp 3600`. Desde la tarjeta puedes
dejar la idea como orden límite o stop en la cuenta demo.

Recuerda lo que mide la validación: el filtro reduce sobre todo **cuánto** se opera; un TOMAR no es una ventaja
demostrada.

### 4. Escáner

**Escáner** recorre un mercado con un modelo rápido (en segundo plano, por defecto cada 15 min: cripto en 1h, 300
activos) y, en modo profundo, aplica NEXUS + filtro a los mejores. Terminal: `scan crypto 1h --deep`,
`results --take`, `quick aapl`. Cada predicción se guarda **antes** de conocer el resultado y se verifica al vencer su
horizonte (4 h por defecto). El acierto se compara con la **referencia** (hay tres resultados posibles: sube, baja o
queda en la banda neutral, así que el 50 % no es el azar) y el Brier, con la climatología. `skill` muestra esa
comparación y `skill replay` repite los escaneos guardados para comparar diseños del modelo. Con menos de 30
ventanas independientes el veredicto es «sin conclusión»: es lo normal durante los primeros días.

### 5. Simulación

**Simulación** tiene tres pestañas:

- **Mercado simulado**: un mercado sintético en tiempo real con regímenes (alcista, bajista, lateral, volátil, crash)
  o el replay vela a vela de un activo real, sin mirar el futuro. NEXUS opera ahí en una cuenta demo aparte
  («Simulación»). Terminal: `sim start --speed 8`, `sim start replay btc 1h`, `sim stop`.
- **Backtest**: NEXUS **con y sin filtro** sobre el historial, para ver qué cambia el filtro (`backtest sol 4h`).
- **Monte Carlo**: remuestreo de las operaciones o mercados sintéticos, con riesgo de ruina y VaR (`mc btc 1h`).

### 6. Cuenta demo

**Cuenta demo** es tu cuenta simulada (saldo inicial de *Ajustes → Riesgo*, 10.000 por defecto; «Reiniciar» empieza
de cero). En «Nueva orden demo» eliges símbolo, lado, tipo, riesgo % y, si quieres, SL y TP:

- **A mercado**: se abre al precio actual. Si el filtro dice NO TOMAR, se pide confirmación.
- **Límite / Stop**: queda **pendiente** hasta que el precio llegue, con validez «hasta cancelarla», «durante N velas»
  o «hasta una fecha». Solo se ejecuta con precios **en vivo** (un dato con retraso nunca la ejecuta) y, al ejecutarse,
  se vuelve a consultar el filtro: si entonces dice NO TOMAR, la orden se cancela y verás el motivo. En un mercado real,
  que el precio toque una orden límite no garantiza que se ejecute.
- **Riesgo de cartera** (*Ajustes → Riesgo de cartera*): riesgo abierto máximo (6 % del capital), posiciones por grupo
  correlacionado (3; toda la cripto cuenta como un grupo) y pérdida diaria máxima (3 % del saldo del día, hasta las
  00:00 UTC). Bloquean órdenes nuevas y ejecuciones pendientes, no se pueden forzar y nunca impiden cerrar.
- Las posiciones se gestionan solas (SL, TP1/TP2/TP3 con tomas parciales, stop dinámico) y muestran el consejo de
  NEXUS («Cerrar ya», «vigila la salida»). Un consejo nunca cierra una posición manual: solo avisa.
- **Auto-trading** (desactivado por defecto): abre solo señales con TOMAR del filtro, en los símbolos que elijas.

Terminal: `buy btc --risk 1`, `limit btc <precio> --exp 2d`, `stop eth <precio> --sl <stop>`, `orders`, `cancel all`,
`risk`, `positions`, `close all`. Todo es simulado: nada llega a un bróker.

### 7. PyChart

**PyChart** es un editor de scripts en Python con una API estilo Pine Script. Empieza por uno de los «Ejemplos»,
pulsa **Compilar** (`Ctrl+Shift+B`) para ver los errores por línea, **Ejecutar** (`Ctrl+Enter`) para dibujarlo sobre el
gráfico y ajusta sus entradas; las estrategias (`strategy(...)`, `strategy.entry`…) muestran su backtest. `Ctrl+S`
guarda (un ejemplo se guarda como copia).

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

Los scripts se validan antes de ejecutarse (solo `math`, `statistics` y `numpy`; sin archivos, red ni sistema) y
corren en un proceso aparte con límite de tiempo y de memoria. En Linux se suman protecciones del sistema operativo
(sin privilegios nuevos, sin red IP cuando el sistema lo permite, descriptores heredados cerrados) y el primer script
deja en la terminal una línea con las que están activas. Terminal: `scripts`, `run <script> btc 1h`.

### 8. Aprendizaje y validación

**Aprendizaje** explica el ciclo (primero se predice, luego se comprueba) y muestra:

- **Habilidad frente a la referencia** (7 días, 24 h o todo lo guardado): acierto de NEXUS y del escáner frente al
  resultado más frecuente del periodo y Brier frente a la climatología, con intervalo del 95 % y veredicto solo a
  partir de 30 ventanas independientes;
- la calibración, los pesos aprendidos de los expertos y del filtro, y cuántas operaciones evitadas habrían perdido;
- **Validación walk-forward: ¿hay ventaja real?** → *Validar ahora* repite, con los datos del momento, la prueba de
  [docs/VALIDATION.md](docs/VALIDATION.md): NEXUS + filtro frente a entradas al azar con las mismas reglas de riesgo y
  frente a comprar y mantener, después de costes. Corre en segundo plano (necesita red) y el último informe queda
  guardado. Terminal: `validar`, `validar estado`, `validar resultado`;
- los modelos NEXUS por activo (puedes reiniciar uno) y el registro de predicciones.

Terminal: `learning`, `predictions`, `skill`, `model btc 1h`.

### 9. Alertas fuera de la app

*Ajustes → Alertas* envía avisos a **Telegram** o por **correo** aunque cierres la pestaña, mientras el servidor de
NEXUS siga en marcha: cambios del semáforo y avisos antes de datos de alto impacto, entradas y salidas de NEXUS de tu
lista de seguimiento (con «señal del modelo, no es asesoría financiera»), y aperturas, cierres y stops de la cuenta
demo (marcados como simulados). No se envía nada hasta que actives un canal y guardes.

- **Telegram**: crea un bot con @BotFather, copia su token, escríbele una vez y busca tu ID de chat en
  `https://api.telegram.org/bot<token>/getUpdates` (el número de `"chat":{"id"…}`; en grupos empieza por −100).
- **Correo**: tu servidor SMTP, puerto 587 (STARTTLS) o 465 (SSL/TLS); Gmail y Outlook piden una contraseña de
  aplicación. La contraseña puede tener espacios, pero debe ser ASCII: el inicio de sesión SMTP no acepta ñ ni tildes.
- **Enviar prueba**, en la tarjeta de estado de las alertas, comprueba cada canal configurado.
- Hay un máximo por hora (los críticos pueden superarlo hasta 60 por hora, o el doble del límite si es mayor), los
  avisos simultáneos van en un solo mensaje y puedes fijar horas de silencio (los críticos pasan solo si lo permites).
  En «Señales solo de estos símbolos» vale `EXCHANGE:TICKER` o solo el ticker.
- **Notificaciones del navegador**: se activan en la misma tarjeta y solo funcionan con una pestaña de NEXUS abierta.

Terminal: `alertas`, `alertas test`. El token y la contraseña se guardan en tu base de datos; no aparecen en el estado,
en los registros ni en la terminal, y en *Ajustes* se muestran ocultos.

### 10. Precios en tiempo real con tu clave

Sin claves, la cripto llega en vivo de los exchanges y el resto, de TradingView con el retraso que anuncia (por
ejemplo, 15 min en NASDAQ y NYSE); la etiqueta del gráfico lo dice. Con una clave gratuita de **Twelve Data**
(*Ajustes → Proveedores de datos*), el gráfico abierto y las posiciones demo de acciones y ETF de EE. UU. y de forex
usan precios en tiempo real dentro de los límites del plan gratuito (8 créditos por minuto y 800 al día: con un
símbolo, un precio cada 35–40 s en sesión; más lento con varios). *Ajustes → Tiempo real* ajusta esos límites. Polygon
solo tiene tiempo real en planes de pago y Alpha Vantage gratis, solo cierres diarios. «Tiempo real» significa que cada
precio lo era al consultarlo, no tick a tick. Terminal: `realtime`, `providers`, `quote aapl`.

### 11. Terminal

Ábrela con el botón de la cabecera o ``Ctrl+` ``. Muestra en vivo todo lo que pasa (señales, operaciones, escaneos,
aprendizaje, errores) y acepta comandos: `help` los lista por grupos y `help <comando>` explica uno. Muchos tienen
alias en español (`señal btc`, `comprar eth`, `posiciones`).

---

## Terminal: comandos útiles

| Comando | Para qué |
|---|---|
| `help [comando]` | Lista los comandos o explica uno |
| `signal btc [1h]` · `explain btc` · `predict btc` | Señal NEXUS actual con niveles y consejo · por qué · cono de precios |
| `check eth long [--sl 3000 --tp 3600]` | **¿Tomo esta operación?** con el checklist completo |
| `watchlist-signals` · `watch add aapl` | Veredictos de toda tu lista · añadir un símbolo a la lista |
| `macro` · `calendario` | Semáforo de mercado · próximos datos económicos con cuenta atrás |
| `scan crypto 1h --deep` · `results --take` · `quick aapl` | Escáner |
| `backtest sol 4h` · `mc btc 1h` | Backtest con y sin filtro · Monte Carlo |
| `sim start --speed 8` · `sim start replay btc 1h` · `sim stop` | Simulador de mercado |
| `buy btc --risk 1` · `sell eth` · `positions` · `close all` · `account` | Cuenta demo a mercado |
| `limit <símbolo> <precio> [--exp 2d]` · `stop <símbolo> <precio>` · `orders` · `cancel all` · `risk` | Órdenes pendientes y riesgo de cartera |
| `learning` · `predictions` · `skill` · `skill replay` | Aprendizaje y habilidad frente a la referencia |
| `validar` · `validar resultado` | Validación walk-forward |
| `alertas` · `alertas test` | Alertas por Telegram y correo |
| `search apple` · `quote aapl msft` · `chart nvda 1d` · `realtime` · `providers` | Mercado y fuentes de datos |
| `scripts` · `run <script> btc 1h` | PyChart |
| `status` · `services` · `settings [clave] [valor]` · `db` · `events 50` | Sistema |

`settings` muestra las claves y contraseñas como `••••••`, y la terminal nunca las escribe en el registro de eventos.

## Configuración (todo opcional)

NEXUS funciona sin configurar nada. Lo que quieras cambiar está en **Ajustes** (se guarda en tu base de datos;
`Ctrl+S` guarda):

| Grupo | Para qué |
|---|---|
| Riesgo | Capital de la cuenta demo, riesgo por operación, comisiones por clase de activo, deslizamiento |
| NEXUS · Filtro · Vigilancia | Parámetros del motor, umbrales de TOMAR / ESPERAR, temporalidad de la lista vigilada |
| Escáner · Grabador local | Mercados y frecuencia del escaneo en segundo plano · grabación de velas propias |
| Proveedores de datos | TradingView sí o no, exchange cripto por defecto y claves **opcionales** de Twelve Data, Alpha Vantage y Polygon |
| Cuenta demo · Riesgo de cartera | Auto-trading y tomas parciales · riesgo abierto, grupos correlacionados y pérdida diaria |
| Simulador · Validación | Velocidad del simulador · símbolos, temporalidades y velas de la validación |
| Alertas · Tiempo real | Canales Telegram y correo (apagados por defecto) · límites de consultas de tu plan |

Las tarjetas **Sistema** (estado de cada servicio) y **Base de datos local** (tamaño y tablas) están en la misma
página. Todo se puede cambiar también desde la terminal (`settings gate.take_score 70`); el semáforo, solo desde ahí
(`settings macro.block_before_min 45`, `settings macro.currencies USD,EUR`).

**Claves de proveedores y de alertas**: se guardan en tu base de datos (SQLite en tu equipo o, en la nube, tu
Postgres) y solo las usa tu servidor. La terminal y los registros las ocultan; la página de Ajustes las recibe para
poder editarlas.

**Variables de entorno** (en local no hace falta ninguna): `NEXUS_PORT` (8420), `NEXUS_HOST` (127.0.0.1: solo tu
equipo; en una interfaz pública el servidor exige login y no arranca sin él), `NEXUS_DATA_DIR` / `NEXUS_DB_PATH`
(dónde está la base de datos), `NEXUS_OFFLINE=1` (sin red: datos guardados y `SIM:*`), `NEXUS_BACKGROUND=0` (sin
servicios en segundo plano), `NEXUS_SCRIPT_MEMORY_MB` (memoria aproximada por script PyChart, 1024 por defecto) y
`NEXUS_CORS_ORIGINS` (otros orígenes del navegador que pueden usar la API y enviarle cambios). Las del modo nube están
en [.env.example](.env.example) y [docs/DEPLOY.md](docs/DEPLOY.md).

---

## Fuentes de datos

- **Universo, búsqueda y cotizaciones de todos los activos**: el escáner público de TradingView (endpoint no oficial,
  consultado con moderación y caché). Revisa sus términos de uso; puedes desactivarlo en *Ajustes → Proveedores de
  datos*.
- **Cripto** (historial completo y precio en vivo): APIs públicas oficiales de Binance (`data-api.binance.vision`),
  OKX, Coinbase, Kraken y KuCoin.
- **Acciones, forex, índices y futuros**: con una clave gratuita en *Ajustes → Proveedores de datos* (Twelve Data
  recomendado; también Alpha Vantage o Polygon) tienes historial y, con Twelve Data, precios en tiempo real. Sin
  clave, NEXUS **graba su propio historial local** a partir de las cotizaciones (crece con el tiempo) y la vista
  *TradingView* del gráfico muestra el histórico completo.
- **Semáforo**: calendario semanal de ForexFactory; calendarios oficiales del BLS y del BEA (se leen cada 12 h);
  fechas oficiales de la Fed (FOMC), el BCE, el BoE y el BoJ en listas fijas revisadas a mano (hasta diciembre de 2027;
  la hora del BoJ es aproximada); volatilidad, futuros, dólar y tipos vía TradingView; Fear & Greed de alternative.me
  y *funding* de OKX.
- **Mercado sintético** (`SIM:NEXUS`) para simulación y pruebas sin conexión.

NEXUS se identifica honestamente en cada petición, usa los certificados del sistema (funciona detrás de proxies
corporativos que inspeccionan TLS) y **no** evade protecciones anti-bots ni suplanta navegadores. Si una fuente
rechaza las peticiones, NEXUS lo avisa en la terminal y no intenta esquivarlo.

## Cómo decide NEXUS

1. **Expertos** (−1…+1 cada uno): cinta de EMAs, MACD, RSI (momento en tendencia, reversión en rango), Bollinger,
   rupturas de Donchian, Supertrend, ADX/DMI, volumen/OBV, pendiente de regresión, tendencia de temporalidad
   mayor, StochRSI y acción del precio en soportes/resistencias.
2. **Régimen**: tendencia alcista, bajista, lateral o volátil (ADX, eficiencia de Kaufman, pendiente, volatilidad).
3. **Aprendizaje**: cuando se conoce el resultado de una predicción (h velas después), los pesos de los expertos
   se ajustan por régimen (pesos multiplicativos con «fixed-share») y se actualiza una regresión logística online
   que da la **probabilidad calibrada** de subida. Todo es **causal**: nunca usa información del futuro.
4. **Decisión** con histéresis → candidato de compra/venta → **filtro** (y semáforo) → TOMAR / ESPERAR / NO TOMAR.
5. **Gestión**: stop inicial por ATR y swing, TP1/TP2/TP3 por múltiplos de R, parciales y stop a break-even,
   stop dinámico y **salida anticipada** cuando el valor esperado de mantener se vuelve negativo.
6. **Memoria**: el estado aprendido se guarda en la base de datos y se reutiliza sin mirar el futuro. Las velas ya
   cerradas conservan la decisión que se tomó al cerrarse: el historial dibujado no se recalcula al llegar velas
   nuevas (salvo los casos descritos en «Known limitations» de [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

---

## Pruebas

Las pruebas no usan la red ni envían alertas reales. Desde la raíz del repositorio, en bash (Git Bash en Windows; en
macOS y Linux el intérprete es `backend/.venv/bin/python`):

```bash
# Backend sobre SQLite
(cd backend && PYTHONUTF8=1 NEXUS_OFFLINE=1 .venv/Scripts/python -m pytest -q -p no:cacheprovider)

# Las mismas pruebas sobre PostgreSQL (crean un esquema por prueba)
(cd backend && NEXUS_TEST_PG_URL=postgresql://usuario:clave@localhost:5432/postgres PYTHONUTF8=1 NEXUS_OFFLINE=1 \
  .venv/Scripts/python -m pytest -q -p no:cacheprovider)

# Interfaz: chequeo de tipos y todas las suites (npm test ejecuta cada tests/*.run.mjs y cada tests/*.test.mjs)
(cd frontend && npm run typecheck && npm test)

# Contrato de la API contra un servidor en marcha: cada respuesta frente a los tipos de frontend/src/api/types*.ts
backend/.venv/Scripts/python tools/contract_check.py --base http://127.0.0.1:8420

# Portabilidad a Linux: LF, permisos, mayúsculas de los imports, COPY del Dockerfile, lock de npm
backend/.venv/Scripts/python tools/ci/portability.py
```

- El comprobador de contrato es de solo lectura: nunca envía una alerta de prueba, ni lanza una validación, ni coloca
  o cancela órdenes demo. Sin red, usa `--symbol SIM:NEXUS` (como en CI); los avisos de listas vacías en una base de
  datos nueva son normales.
- PostgreSQL local sin Docker (con `pgserver`) y el resto de comprobaciones: tabla «Verificación» de
  [CLAUDE.md](CLAUDE.md).

## Integración continua (GitHub Actions)

Cada push a `main` y cada *pull request* ejecutan [.github/workflows/ci.yml](.github/workflows/ci.yml) (también se
puede lanzar a mano). No usa secretos y la app corre sin red (`NEXUS_OFFLINE=1`). Trabajos:

| Trabajo | Qué comprueba |
|---|---|
| Portabilidad Linux | LF en scripts y Dockerfile, permisos de `start.sh`, mayúsculas de los imports, `COPY` del Dockerfile y binarios de Linux en `package-lock.json` (`tools/ci/portability.py`) |
| Backend (SQLite) | Toda la suite pytest en Ubuntu con Python 3.13 |
| Backend (PostgreSQL 16) | La misma suite contra un PostgreSQL 16 real, un esquema por prueba |
| Interfaz (Node 20) | `npm ci`, chequeo de tipos, `npm test` y compilación |
| Imagen Docker (Linux) | Construye la imagen de producción y comprueba que no arranca sin login, sin base de datos o sin lista de correos; `/api/health` en loopback; el servidor sin root ni capacidades y el código de solo lectura; PyChart por la API (un script y el límite de memoria); el contrato de la API; la parada limpia con SIGTERM; y la suite del backend dentro de la imagen (usuario 10001, versiones exactas del lock) |
| Lanzador Linux (start.sh) | `bash start.sh` en un Ubuntu limpio: venv, dependencias, interfaz compilada, `/api/health` y la interfaz servida |

**Cómo leer los resultados**

- La insignia verde de arriba significa que la última ejecución en `main` pasó todos los trabajos; roja, que alguno
  falló. Un clic abre la ejecución.
- En *Actions → CI →* la ejecución: arriba, las anotaciones (errores y avisos con su motivo); abajo, el resumen con
  las tablas «Imagen Docker: …» (Estado | Comprobación | Detalle). Dentro de un trabajo, el paso fallido se abre solo
  y los registros largos (`docker logs`) están plegados.
- pytest termina con «N passed, M skipped». El resumen corto (`-ra`) lista cada prueba omitida con su motivo y
  `--durations=15`, las más lentas. Omisiones esperadas: las de PostgreSQL en el trabajo SQLite (se ejecutan en el
  otro), las de red (`NEXUS_NET_TESTS=1`), la comparación exacta del lock fuera de la imagen y las que necesitan git
  dentro de la imagen.
- La prueba de red del sandbox (`tests/test_imp_sandbox_os_linux.py`) necesita un espacio de nombres de red sin
  privilegios, que Ubuntu 24.04 restringe con AppArmor. El trabajo «Backend (SQLite)» levanta esa restricción solo en
  su máquina virtual desechable, así que ahí se ejecuta; si no puede, el paso deja un aviso y la prueba se omite con
  el motivo (EPERM). En «Backend (PostgreSQL 16)» y dentro de la imagen Docker (perfil seccomp) es normal verla
  omitida; las demás capas del sandbox sí se prueban.
- Dentro de la imagen, una prueba «solo POSIX/Linux» omitida es un **fallo** (en Linux debe ejecutarse); una prueba de
  seguridad omitida porque el contenedor no lo permite (EPERM) es un **aviso**, igual que «tardó X s en detenerse»
  (se superaron los 5 s que Fly.io espera por defecto).
- Con Node 20, `npm ci` muestra el aviso EBADENGINE de `@napi-rs/lzma-linux-x64-gnu`: rollup lo declara como
  dependencia opcional y solo lo usa para compilarse a sí mismo. No afecta a la compilación de la interfaz.
- «Cancelled» significa que un push nuevo a la misma rama o PR canceló la ejecución anterior; no es un fallo.
  «Re-run failed jobs» repite solo lo que falló.
- Reproducir en local: los comandos de [Pruebas](#pruebas). Con Docker (Linux o Docker Desktop):
  `docker build -t nexus-terminal:ci .`, luego `python tools/ci/docker_smoke.py nexus-terminal:ci` y
  `python tools/ci/docker_tests.py nexus-terminal:ci`. `tools/ci/launcher_smoke.py` es solo para Linux y macOS.

---

## En la nube

Para usarlo desde cualquier lugar: interfaz en **Vercel**, base de datos y login en **Supabase** y el backend siempre
encendido en **Railway** (o Fly.io), porque NEXUS vigila el mercado 24/7. El login es obligatorio y solo para los
correos que autorices; el servidor no arranca en público sin login, sin lista de correos o sin base de datos
persistente. Guía paso a paso: [docs/DEPLOY.md](docs/DEPLOY.md). El modo local no cambia.

## Solución de problemas

| Síntoma | Qué hacer |
|---|---|
| Windows: «la ejecución de scripts está deshabilitada en este sistema» | Usa `.\start.bat`, que lanza `start.ps1` con `-ExecutionPolicy Bypass`, o `powershell -ExecutionPolicy Bypass -File .\start.ps1`. No hace falta cambiar la política del sistema. |
| «No se encontró Python 3.11 o superior» o «No se encontró Node.js 18+» | Instala Python 3.13 (python.org; en Windows sirve también el lanzador `py`) y Node.js LTS (nodejs.org), abre una terminal nueva y vuelve a arrancar. Si `backend/.venv` se creó con un Python antiguo, bórralo y arranca otra vez para que se recree. |
| Errores de certificado (`CERTIFICATE_VERIFY_FAILED`, `SELF_SIGNED_CERT_IN_CHAIN`) en una red corporativa | La app usa los certificados del sistema (`truststore`): funciona si el certificado de la empresa está instalado en el sistema. Para instalar dependencias: con `uv`, `start.bat` ya usa `--system-certs` (o `--native-tls`); con pip, `pip install --cert <ca-de-la-empresa.pem> …`; con npm, `npm config set cafile <ca-de-la-empresa.pem>` o la variable `NODE_EXTRA_CA_CERTS`. Si la red exige proxy, define `HTTPS_PROXY`. |
| `error while attempting to bind on address ('127.0.0.1', 8420)` | Otro programa (u otro NEXUS) usa el puerto: `.\start.bat -Port 9000` o `bash start.sh --port 9000`. En modo desarrollo, Vite usa además el 5173. |
| «La API está funcionando, pero la interfaz no está compilada» | No había Node al arrancar: instálalo y vuelve a ejecutar el script (o `cd frontend && npm ci && npm run build`). |
| «No se pudo descargar esta sección» | La interfaz se recompiló o se actualizó con la pestaña abierta: pulsa *Recargar página*. |
| Acciones o forex sin historial, o con «Retraso 15 min» | Es lo esperado sin clave: añade una clave gratuita de Twelve Data en *Ajustes → Proveedores de datos* o usa la vista TradingView. |
| Una orden límite o stop no se ejecuta | Solo se ejecutan con precios en vivo: con datos con retraso (acciones sin clave) no se llenan. Mira la etiqueta de frescura del gráfico. |
| `403 Origen no permitido (…)` al guardar o al enviar órdenes | Protección CSRF: los cambios solo se aceptan desde la propia interfaz de NEXUS (o Vite en el 5173). Abre http://127.0.0.1:8420 o añade ese origen a `NEXUS_CORS_ORIGINS`. |
| Texto con caracteres raros en la consola de Windows | Define `PYTHONUTF8=1` (los scripts de arranque ya lo hacen). |
| Empezar de cero | Detén NEXUS y mueve o renombra `data/nexus.db`; al arrancar se crea una nueva. Para olvidar lo aprendido de un símbolo: `reset-model btc 1h --yes`; para la cuenta demo, «Reiniciar» en *Cuenta demo*. |

Más detalles técnicos y riesgos conocidos: «Known limitations» en [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Uso con Claude Code

El repositorio está preparado para abrirse con Claude Code: al abrir la carpeta, Claude lee [CLAUDE.md](CLAUDE.md),
que resume cómo arrancar NEXUS, cómo verificar cambios (pruebas del backend y de la interfaz, contrato de la API, CI),
el mapa del código y las reglas del proyecto (texto en español, honestidad, solo simulación, red solo por endpoints
oficiales o públicos, migraciones numeradas para la base de datos…). Puedes pedirle, por ejemplo, «arranca NEXUS»,
«ejecuta las pruebas» o «explícame cómo decide el filtro». El contrato técnico completo (API, WebSocket, base de
datos, reglas de cada módulo) está en [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Arquitectura

```
backend/   FastAPI + numpy; SQLite (aiosqlite) en local o PostgreSQL (asyncpg) en la nube
  app/data/        proveedores de datos, hub en vivo, tiempo real con clave, grabador local, escáner de TradingView
  app/engine/      NEXUS (expertos, régimen, aprendizaje, filtro, niveles, predicción) + más de 100 indicadores
  app/macro/       semáforo de mercado y calendarios oficiales
  app/sim/         backtest, Monte Carlo, simulador, cuenta demo, escáner, evaluador (retroalimentación)
  app/scripting/   PyChart: validador, sandbox, runtime estilo Pine, backtester
  app/alerts/      alertas por Telegram y correo
  app/validation/  validación walk-forward
  app/migrations/  migraciones numeradas de la base de datos
frontend/  React + TypeScript + Vite + Lightweight Charts v5 + CodeMirror (cada sección se descarga al abrirla)
tools/     comprobador de contrato, PostgreSQL local, lock de dependencias y scripts de CI (tools/ci/)
docs/      ARCHITECTURE.md (contrato completo), DEPLOY.md (nube), VALIDATION.md (¿hay ventaja real?)
```

Documentación interactiva de la API, con el servidor en marcha: http://127.0.0.1:8420/api/docs

## Desarrollo

- `start.bat` / `start.sh` instalan `backend/requirements-dev.txt` (dependencias del servidor + pytest).
- La imagen Docker instala las versiones exactas de `backend/requirements.lock` (las que pasaron las pruebas). Si
  cambias `backend/requirements.txt` o actualizas paquetes del entorno, regénerala con
  `backend/.venv/Scripts/python tools/lock_requirements.py` (`--check` solo comprueba que esté al día).
- Reglas del proyecto, mapa del código y verificación: [CLAUDE.md](CLAUDE.md).
