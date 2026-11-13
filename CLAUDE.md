# NEXUS Terminal — guía para Claude Code

Claude lee este archivo al abrir el proyecto. Úsalo como mapa; el contrato completo (APIs, tipos, reglas de cada
módulo) está en `docs/ARCHITECTURE.md`, que es la fuente de verdad. Guía de usuario: `README.md`. Nube:
`docs/DEPLOY.md`. ¿Hay ventaja real?: `docs/VALIDATION.md`.

## Qué es

Terminal local de investigación de trading, **solo simulación / papel**: FastAPI (Python) + React/TypeScript. Gráficos
de cualquier activo de TradingView, motor autoadaptativo NEXUS (comprar / vender / mantener, niveles «hasta aquí»,
salidas anticipadas), filtro «¿Tomo esta operación?» (TOMAR / ESPERAR / NO TOMAR), semáforo de mercado (FED,
calendario de esta semana y la próxima, futuros, volatilidad), escáner, simulador y backtests, cuenta demo (a mercado,
órdenes límite / stop, riesgo de cartera, auto-trading), la cuenta propia de NEXUS («NEXUS automática», cuenta 3,
1.000 $ simulados: opera sola con sus señales y el filtro TOMAR en cripto, acciones, futuros de índices y opciones con
precio teórico; apagada por defecto), alertas por Telegram y correo, validación walk-forward, PyChart (scripts Python
estilo Pine en un sandbox) y una terminal de comandos. Base de datos local SQLite (`data/nexus.db`) o
PostgreSQL/Supabase en modo nube. La validación publicada concluye «sin evidencia de ventaja» (`docs/VALIDATION.md`):
ningún texto de la app ni de la documentación puede sugerir lo contrario.

## Puesta en marcha en una máquina nueva

Requisitos: Git, Python 3.13 (probado; 3.11+ debería funcionar), Node.js 18+ (probado con 20, el de CI). Opcional:
`uv` (instala más rápido y respeta los certificados del sistema).

- Windows: `.\start.bat` (opciones: `-Dev`, `-Port 9000`, `-NoBrowser`, `-Rebuild`). No uses `.\start.ps1` directo:
  la política de ejecución de Windows lo bloquea; `start.bat` lo llama con `-ExecutionPolicy Bypass`.
- macOS / Linux: `bash start.sh` (opciones: `--dev`, `--port N`, `--no-browser`).

El script crea `backend/.venv`, instala `backend/requirements-dev.txt`, hace `npm ci` y compila `frontend/dist`
(solo cuando cambian), arranca el servidor en http://127.0.0.1:8420, crea o migra la base de datos y abre el
navegador. No hace falta ningún `.env` en modo local.

Arranque manual (equivalente):

```bash
cd backend
python -m venv .venv                      # o: uv venv .venv --python 3.13
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/python
cd ../frontend && npm ci && npm run build
cd ../backend && PYTHONUTF8=1 .venv/Scripts/python -m app.main   # http://127.0.0.1:8420
```

Desarrollo con recarga en caliente: `.\start.bat -Dev` / `bash start.sh --dev` (Vite en :5173 + API en :8420).

## Verificación (corre esto antes de dar algo por terminado)

| Qué | Comando (desde la raíz del repo) |
|---|---|
| Backend (SQLite) | `cd backend && PYTHONUTF8=1 NEXUS_OFFLINE=1 .venv/Scripts/python -m pytest -q -p no:cacheprovider` |
| Backend (PostgreSQL) | igual, con `NEXUS_TEST_PG_URL=<uri>` (ver abajo); obligatorio si cambia SQL |
| Tipos + compilación UI | `cd frontend && npm run typecheck && npm run build` |
| Pruebas de la UI | `cd frontend && npm test` (`tests/run-all.mjs`: ejecuta cada `tests/*.run.mjs` y cada `tests/*.test.mjs`; una suite nueva entra sola por su nombre) |
| Contrato de la API en vivo | `backend/.venv/Scripts/python tools/contract_check.py --base http://127.0.0.1:8420` (sin red: `--symbol SIM:NEXUS`, como en CI; solo lectura) |
| Portabilidad Linux | `backend/.venv/Scripts/python tools/ci/portability.py` |
| Imagen Docker (con Docker) | `docker build -t nexus-terminal:ci . && python tools/ci/docker_smoke.py nexus-terminal:ci && python tools/ci/docker_tests.py nexus-terminal:ci` |
| Lock de dependencias | `backend/.venv/Scripts/python tools/lock_requirements.py --check` |
| Peso de la primera carga | `cd frontend && node tests/imp-fe-perf.measure.mjs ../backend/.tmp/fe-perf/dist` (compila en una carpeta temporal, nunca en `dist`) |
| Validación walk-forward (con red) | `python -m app.validation.cli …` desde `backend/` (comando completo en `docs/VALIDATION.md` §6) |

CI: `.github/workflows/ci.yml` ejecuta en cada push a `main` y en cada PR la portabilidad, el backend sobre SQLite y
sobre PostgreSQL 16, la UI (Node 20: typecheck, `npm test`, build), la imagen Docker (prueba de humo + suite dentro de
la imagen) y `bash start.sh` en un Linux limpio. Los scripts están en `tools/ci/` (solo biblioteca estándar, Python
3.9+). Cómo leer sus resultados: sección «Integración continua» del README. Para juzgar el escáner con honestidad,
`skill` y `skill replay` en la terminal.

En Linux/macOS el intérprete del venv es `backend/.venv/bin/python` (`bin/` en lugar de `Scripts/`).

PostgreSQL sin Docker: `pgserver` solo tiene wheels hasta Python 3.12, así que va en su propio venv:

```bash
uv venv backend/.tmp/pgenv --python 3.12
uv pip install --python backend/.tmp/pgenv/Scripts/python.exe pgserver
backend/.tmp/pgenv/Scripts/python.exe tools/pg_start.py backend/.tmp/pgdata   # imprime la URI
```

Estado tras los rangos de sesión y las noticias de PyChart (PR #1, con los costes por mercado en su estrategia;
13/11/2026, en Windows): backend 1997 passed / 27 skipped sobre SQLite y 2008 passed / 16 skipped sobre PostgreSQL
(las omisiones son las de PostgreSQL, red, solo Linux / POSIX y el lock exacto); UI 250 pruebas en 9 suites; `tsc` sin
errores; contrato 130 comprobaciones · 0 fallos · 6 avisos (sin red, `--symbol SIM:NEXUS` y base nueva, como en CI:
los avisos son listas vacías y ningún informe de validación guardado). La prueba más lenta es
`tests/test_imp_engine_repaint_history.py` (regresión de repintado: 300 cierres, ~2,5 min). Si la suite sobre
PostgreSQL local se vuelve muy lenta (miles de esquemas creados y borrados por las pruebas), arranca
`tools/pg_start.py` con una carpeta nueva.

## Mapa del código

- `backend/app/`: `main.py` (app; middlewares, de fuera a dentro: CORS → comprobación de Origin (CSRF) → Auth →
  Host → StandbyGate; servicios y liderazgo en PostgreSQL), `config.py` (variables de entorno y `DEFAULT_SETTINGS`),
  `settings_ext/` (valores por defecto de cada módulo: `alerts`, `autopilot`, `paper_risk`, `realtime`,
  `scanner_model`, `validation`; se suman a `DEFAULT_SETTINGS`), `runtime.py` (`rt`, localizador de servicios), `db.py` / `db_pg.py`
  (SQLite / PostgreSQL con traducción de SQL; `TABLE_PKS`, `IDENTITY_TABLES`), `migrations/` (migraciones numeradas
  y `pg_rls.sql`), `settings.py`, `events.py` (bus de eventos), `ws.py`, `terminal.py` (registro de comandos y
  enmascarado de secretos), `auth.py` (Supabase), `hardening.py` (servidor no volcable en Linux), `maintenance.py`
  (retención).
  - `engine/`: NEXUS (`nexus*.py`; `nexus_resume.py` = reanudación exacta desde las anclas guardadas; `service.py`,
    `service_watch.py`), filtro (`gate*.py`), semáforo dentro del filtro (`macro_merge.py`), niveles, aprendizaje,
    +100 indicadores (`indicators*.py`, `ta.py`), comisiones por clase de activo (`costs.py`), futuros de índices
    (`contracts.py`) y opciones teóricas de NEXUS automática (`options.py`: Black-Scholes, vencimientos, strikes).
  - `data/`: `hub.py` (velas, cotizaciones, feed en vivo), proveedores (Binance, OKX, Coinbase, Kraken, KuCoin;
    claves opcionales Twelve Data / Alpha Vantage / Polygon), `realtime.py` (cotizaciones en tiempo real con clave,
    dentro de los límites del plan), `stamping.py` (sellado de las instantáneas de TradingView), TradingView
    (escáner público), grabador de velas.
  - `sim/`: cuenta demo (`paper.py`: órdenes límite / stop pendientes, riesgo de cartera; cuentas 1 «Principal»,
    2 «Simulación» y 3 «NEXUS automática», que opera sola con un `AutoProfile` como el auto-trading;
    `autopilot_compare.py` la compara con entrar al azar con el método de la validación), escáner,
    evaluador de predicciones (habilidad frente a la referencia, repetición offline), backtest, simulador, Monte Carlo.
  - `scripting/`: PyChart (validador AST, `worker.py` en subproceso aislado, `sandbox.py`; `runtime_news.py` =
    `noticias`, el calendario económico que el servidor pasa en el payload porque el worker no tiene red).
  - `macro/`: semáforo (calendario económico, FOMC, VIX, futuros, DXY, tipos, funding); `official.py` (ICS en vivo
    del BLS y del BEA), `schedules.py` (fechas fijas de BCE, BoE, BoJ, BLS y BEA hasta dic. 2027: actualízalas cada
    año con fechas comprobadas en las páginas oficiales), `history.py` (fechas oficiales pasadas de IPC, empleo y
    FOMC desde feb. 2024, solo para `noticias` de PyChart).
  - `alerts/`: `AlertService`, `classify.py`, `policy.py` (deduplicado, límite por hora, horas de silencio),
    `channels.py` (Telegram y SMTP).
  - `validation/`: validación walk-forward (`walkforward.py`, `mechanics.py`, `stats.py`, `verdict.py`,
    `aggregate.py`, `service.py`, `cli.py`, `markdown.py`…).
  - `api/`: routers REST.
- `frontend/src/`: `api/` (`client.ts`, `ws.ts`, `types.ts` = tipos canónicos; `types-*.ts` = tipos de cada módulo
  (alerts, paper-orders, realtime, validation), reexportados por `types.ts`), `pages/` (una por sección, cargada bajo
  demanda), `components/<área>/`, `components/settings/schema.ts` + `schema-*.ts` (grupos de Ajustes de cada
  módulo), `lib/` (`chart.ts`, `format.ts`, `plural.ts`), `auth/`.
- Pruebas: `backend/tests/` (pytest; fixtures en `conftest.py`, fakes en `test_*_fakes.py`), `frontend/tests/`
  (`run-all.mjs`).
- Herramientas: `tools/contract_check.py`, `tools/pg_start.py`, `tools/fake_supabase.py`, `tools/lock_requirements.py`,
  `tools/ci/` (`portability.py`, `docker_smoke.py`, `docker_tests.py`, `launcher_smoke.py`, `pytest_failures.py`,
  `ci_common.py`).
- Docs: `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`, `docs/VALIDATION.md` y `docs/validation/*.json` (informes
  publicados).

## Reglas del proyecto (obligatorias)

1. Todo texto visible (UI, errores de la API, terminal) en español neutro.
2. Honestidad: nunca exagerar acierto, certeza ni protección. Estadísticas como «x de n», frente a una referencia;
   sin conclusiones con n < 30 o cuando el intervalo del 95 % incluye la referencia. Avisos de que no es asesoría
   financiera.
3. Solo simulación / papel: nunca ejecutar órdenes reales ni conectar brokers con dinero real.
4. Red: solo endpoints oficiales o públicos, con peticiones normales a través de `app.util.http_client()`. Nunca
   falsificar User-Agent, cabeceras, cookies ni saltarse protecciones anti-bot (por eso Yahoo, Stooq y la búsqueda
   de símbolos de TradingView están excluidos). Una fuente que rechaza al cliente se registra y no se esquiva.
5. Sin mirar el futuro: el motor es causal (una vela solo usa información disponible a su cierre); las pruebas de
   no-lookahead deben pasar. Si cambia lo que calcula el motor, sube `RESUME_VERSION` en
   `engine/nexus_resume.py` (si no, las anclas guardadas reanudarían otro cálculo) y repite la validación
   (`docs/VALIDATION.md` está ligado a la huella del motor medido).
6. Contratos: si cambia una respuesta de la API, actualiza `frontend/src/api/types.ts` (o su `types-*.ts`) y
   `docs/ARCHITECTURE.md` en el mismo cambio; `tools/contract_check.py` los compara con el servidor.
7. Base de datos: nunca cambies el DDL de `schema.sql` / `schema_pg.sql` (base congelada; una prueba guarda su
   huella). Todo cambio es una migración numerada nueva en `backend/app/migrations/`: `NNNN_nombre.sqlite.sql` +
   `NNNN_nombre.pg.sql` (mismo número y nombre), compatible hacia atrás (tablas nuevas, columnas que admiten NULL,
   índices), con sus tablas nuevas en `TABLE_PKS` / `IDENTITY_TABLES` de `db_pg.py`, y probada en SQLite y
   PostgreSQL (`tests/test_imp_paper_db_migrations.py` muestra cómo). Se aplican solas al arrancar.
8. Secretos: nunca subir `.env` ni claves; las claves se guardan desde Ajustes en la base de datos. Un ajuste cuyo
   nombre contiene key, token, password, passwd, secret, passphrase o credential es un secreto
   (`terminal.is_secret_setting`): la terminal lo muestra como `••••••` y nunca lo escribe en el registro de eventos
   (`loggable_command`); estado, `/api/health`, eventos y errores nunca llevan secretos. Límite conocido:
   `GET /api/settings` sí los devuelve a la interfaz con sesión (el formulario los necesita).
9. PyChart: el sandbox tiene varias capas (validador AST, builtins restringidos, proxies de módulos, audit hook,
   límites del sistema y objeto de trabajo en Windows) y, en Linux, aislamiento del sistema (no_new_privs, espacio
   de nombres de usuario y red vacío cuando el host lo permite, descriptores heredados cerrados; servidor no
   volcable). El worker arranca con `python -I -B`. Cualquier cambio ahí necesita pruebas de escape
   (`tests/test_fix_sandbox_*.py`, `tests/test_imp_sandbox_os_*.py`; las de Linux se omiten en Windows y corren en CI).
10. Pruebas: una prueba exclusiva de una plataforma usa `pytest.mark.skipif(<os.name / sys.platform>, reason=...)`;
    si el entorno rechaza algo, el motivo lleva el errno (EPERM…), así la suite dentro de la imagen distingue un
    fallo de un entorno restringido. Las pruebas de alertas nunca envían (fakes de `app.util.http_client` y
    `smtplib`) y el comprobador de contrato nunca llama a `POST /api/alerts/test`.
11. CSRF: un POST/PUT/PATCH/DELETE con cabecera Origin solo pasa si viene de la propia interfaz (mismo host), de Vite
    (localhost / 127.0.0.1:5173) o de un origen de `NEXUS_CORS_ORIGINS`; si no, 403. Scripts y curl sin Origin no
    se ven afectados.

## Detalles prácticos

- Windows: exporta siempre `PYTHONUTF8=1`. Con proxy corporativo que intercepta TLS, la app usa `truststore`
  (certificados del sistema) y para instalar usa `uv pip install --system-certs`.
- `NEXUS_OFFLINE=1` desactiva toda la red (lo usan las pruebas); `NEXUS_BACKGROUND=0` arranca sin bucles de fondo.
- `data/` (la base de datos), `backend/.venv`, `frontend/node_modules`, `frontend/dist` y `backend/.tmp/`
  (archivos temporales) no están en git.
- Puertos: 8420 (servidor), 5173 (Vite en modo desarrollo).
- Rutas largas en Windows: usa `backend/.tmp/` para archivos temporales en vez de carpetas muy profundas.
- Bajo mucha carga, Windows puede no crear procesos (código 0xC0000142): las pruebas que lanzan subprocesos
  (`test_imp_ci_*`, `test_imp_sandbox_os_*`) fallan entonces por el entorno; repítelas solas.
- Límites conocidos y riesgos residuales: sección «Known limitations» de `docs/ARCHITECTURE.md`.
