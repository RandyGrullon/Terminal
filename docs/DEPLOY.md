# Despliegue en la nube (híbrido): Vercel + Supabase + Railway

NEXUS tiene servicios que deben estar **siempre encendidos** (vigilancia de tu lista, escáner, evaluador de
predicciones, gestión de SL/TP de la cuenta demo, simulador en tiempo real y la terminal por WebSocket).
Por eso el despliegue recomendado reparte el trabajo así:

```
 Navegador ──HTTPS──▶ Vercel ··········· interfaz web (React, estática, gratis)
     │
     ├── login ────────▶ Supabase Auth ···· usuarios (solo los correos que autorices)
     │
     └── API + WebSocket (con tu sesión) ──▶ Railway ··· backend NEXUS (FastAPI, motor, servicios de fondo)
                                                │
                                                └──▶ Supabase Postgres ··· base de datos (esquema "nexus")
```

El **modo local no cambia**: `start.bat` sigue usando SQLite en tu PC, sin login y sin nube.

| Pieza | Plan | Coste aproximado |
|---|---|---|
| Vercel (interfaz) | Hobby | gratis |
| Supabase (Postgres + login) | Free | gratis (500 MB; NEXUS aplica retención automática, ver «Tamaño de la base de datos») |
| Railway (backend 24/7) | Hobby | ~5 US$/mes (o Fly.io, equivalente) |

> Yo no puedo crear cuentas ni manejar tus contraseñas: los pasos con 👤 los haces tú en cada web.

---

## Paso 1 — Supabase (base de datos y login)

1. 👤 Entra en [supabase.com](https://supabase.com) → **New project**.
   - **Región**: *Central EU (Frankfurt)* o *West EU (Ireland)* (recomendado, ver "Datos desde la nube").
   - Guarda la **contraseña de la base de datos**.
2. 👤 **Project Settings → API**: copia el **Project URL** (`https://<ref>.supabase.co`) y la clave **anon / publishable**.
3. 👤 **Connect** (o *Project Settings → Database → Connection string*) → **Session pooler** → copia la URI y
   reemplaza `[YOUR-PASSWORD]`. Se ve así:
   `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`
   (usa *Session pooler*: funciona por IPv4 y admite las sentencias preparadas que usa NEXUS).
4. 👤 **Authentication → Users → Add user → Create new user**: tu correo + contraseña (marca *Auto Confirm User*).
5. 👤 **Authentication → Sign In / Providers**: desactiva **Allow new users to sign up** (nadie más podrá
   registrarse; además NEXUS solo deja entrar a los correos de `NEXUS_ALLOWED_EMAILS` y no arranca si falta
   esa lista).

No tienes que crear tablas: el backend crea el esquema `nexus` al arrancar (fuera del esquema público, con
Row Level Security activado, así la API pública de Supabase nunca expone tus datos).

## Paso 2 — Backend en Railway

1. 👤 [railway.com](https://railway.com) → **New Project → Deploy from GitHub repo** → `RandyGrullon/Terminal`.
   Railway detecta el `Dockerfile` y `railway.json` de la raíz.
2. 👤 **Settings → Region**: *EU West (Amsterdam)* (misma zona que Supabase).
3. 👤 **Variables** (copia los nombres de `.env.example`):

   | Variable | Valor |
   |---|---|
   | `NEXUS_DATABASE_URL` | la URI del *Session pooler* del paso 1.3 (**obligatoria**) |
   | `NEXUS_AUTH` | `supabase` |
   | `NEXUS_SUPABASE_URL` | `https://<ref>.supabase.co` |
   | `NEXUS_SUPABASE_ANON_KEY` | la clave anon/publishable |
   | `NEXUS_ALLOWED_EMAILS` | tu correo (varios separados por comas) (**obligatoria**) |
   | `NEXUS_CORS_ORIGINS` | de momento `http://localhost:5173`; en el paso 3 pondrás tu dominio de Vercel |
   | `NEXUS_SCRIPT_MEMORY_MB` | opcional: límite aproximado de memoria de cada script PyChart, en MB (por defecto 1024; pueden ejecutarse 2 a la vez, así que mantenlo bien por debajo de la memoria de tu plan: con 1 GB usa `320`) |

4. 👤 **Settings → Networking → Generate Domain** → copia la URL, por ejemplo `https://nexus-production.up.railway.app`.
5. Comprueba `https://<tu-backend>/api/health` → debe responder `{"ok": true, "auth": "required", "role": "leader", ...}`.

**Falla cerrado**: en una IP pública el servidor se niega a arrancar si falta `NEXUS_AUTH=supabase`,
`NEXUS_ALLOWED_EMAILS` o `NEXUS_DATABASE_URL` (sin ella los datos irían a un archivo SQLite dentro del
contenedor, que se borra en cada despliegue). El motivo aparece en los *Deploy Logs*. Solo si de verdad lo
necesitas: `NEXUS_ALLOW_ANY_SUPABASE_USER=1` acepta a cualquier usuario de tu proyecto Supabase (no
recomendado) y `NEXUS_ALLOW_LOCAL_DB=1` usa SQLite cuando montaste un volumen persistente en `/app/data`. Si ese
volumen pertenece a root, el usuario `nexus` (uid 10001) no puede escribir en él; no ejecutes el contenedor como root
(p. ej. con `RAILWAY_RUN_UID=0`): perderías la protección sin root y el límite de procesos del sandbox de PyChart
(`RLIMIT_NPROC`) no se aplica a root. Mejor usa `NEXUS_DATABASE_URL`.

**Migraciones de la base de datos**: al arrancar, el backend aplica solas las migraciones pendientes (también en
Supabase), todas en una transacción y dentro del bloqueo del esquema (`pg_advisory_xact_lock`), así que dos
instancias que arrancan a la vez nunca aplican la misma dos veces; si una falla, no se aplica ninguna y el arranque se
detiene con su nombre. La primera vez con esta versión marca la base existente como esquema base y aplica
`0002_paper_orders` y `0003_paper_fills_time` (pequeñas y aditivas; el índice de 0003 toma un momento un bloqueo
SHARE sobre `paper_fills`). Las migraciones son compatibles hacia atrás: durante el solape de un despliegue la
instancia anterior sigue funcionando con la base ya migrada, y volver a la imagen anterior también funciona (ignora
`paper_orders` y reescribe `meta.schema_version='1'`, que es solo informativo). En los *Deploy Logs*:
«Iniciando NEXUS Terminal … (esquema vN)» y, si aplicó alguna, «Base de datos preparada: esquema base y migraciones
…» (base nueva) o «Base de datos actualizada al esquema vN: …»; con sesión iniciada, `/api/health` incluye
`db.schema_version`.

**Una sola instancia activa**: deja **1 réplica**. En cada despliegue Railway mantiene la versión anterior
hasta que la nueva responde `/api/health`, así que durante unos segundos conviven dos. NEXUS lo resuelve con
un bloqueo en Postgres: solo la instancia que lo tiene ejecuta los servicios de fondo (vigilancia, escáner,
evaluador, cuenta demo); la otra queda **en espera** (`/api/health` responde `"role": "standby"` y el resto
de la API `503 Instancia en espera`, un aviso pasajero) y toma el relevo unos segundos después de que la
anterior se detenga. Si la anterior se cae sin cerrar su conexión (por ejemplo, falla su máquina), Postgres
libera el bloqueo en unos 5 minutos y hasta entonces la nueva sigue en espera. Si ves una instancia en espera
sin ningún despliegue en curso, sobra una réplica.
El bloqueo necesita una conexión de sesión: usa la URI del *Session pooler* (o la directa), nunca la del
*Transaction pooler* (puerto 6543).

**`NEXUS_ALLOWED_HOSTS`** (opcional) limita los dominios aceptados en la cabecera `Host`. `/api/health` queda
fuera de esa comprobación, porque el chequeo de salud de Railway llega con `Host: healthcheck.railway.app` (y
el de Fly con la dirección de la máquina); el resto de la API sí se restringe.

<details>
<summary>Alternativa: Fly.io en lugar de Railway</summary>

```bash
fly launch --no-deploy --copy-config --ha=false --name <tu-app>
fly secrets set NEXUS_DATABASE_URL="..." NEXUS_SUPABASE_URL="..." NEXUS_SUPABASE_ANON_KEY="..." \
  NEXUS_ALLOWED_EMAILS="tu@correo.com" NEXUS_CORS_ORIGINS="https://<tu-app>.vercel.app"
fly deploy --ha=false
```
Sin `--ha=false`, `fly launch` y `fly deploy` crean **dos** máquinas: una quedaría siempre en espera y parte
de las peticiones recibiría `503 Instancia en espera`. Si ya tienes dos, deja una con `fly scale count 1`.
`fly.toml` ya deja la máquina siempre encendida, en Frankfurt, con chequeo de salud y con
`NEXUS_SCRIPT_MEMORY_MB=320` (adecuado para su máquina de 1 GB). Fly espera 5 s por defecto a que el servidor se
detenga (`kill_timeout`); si la prueba de la imagen en CI avisa de que la parada tarda más, sube `kill_timeout` en
`fly.toml`.
</details>

## Paso 3 — Interfaz en Vercel

1. 👤 [vercel.com](https://vercel.com) → **Add New → Project → Import** `RandyGrullon/Terminal`.
2. 👤 **Root Directory**: `frontend` (Vercel detecta Vite; `frontend/vercel.json` ya está incluido).
3. 👤 **Environment Variables**: `VITE_API_BASE` = la URL del backend del paso 2.4 (sin barra final).
   La configuración de Supabase la entrega el propio backend, no hace falta repetirla.
4. 👤 **Deploy** → copia tu URL, por ejemplo `https://nexus-terminal.vercel.app`.
5. 👤 Vuelve a Railway → `NEXUS_CORS_ORIGINS=https://nexus-terminal.vercel.app` (añade también tu dominio
   propio si lo usas, separado por comas) → se redepliega solo. Esta lista decide también qué sitios pueden enviar
   cambios al backend (protección CSRF): si falta tu dominio, cada POST/PUT/DELETE de esa interfaz recibe
   `403 Origen no permitido (…)` y los registros del backend lo avisan (como mucho una vez cada 5 min).
6. 👤 Supabase → **Authentication → URL Configuration → Site URL** = tu URL de Vercel.

## Paso 4 — Probar

Abre tu URL de Vercel → inicia sesión con el correo del paso 1.4 → deberías ver el gráfico, la terminal
"En vivo", en **Ajustes → Sistema** los servicios activos y en la tarjeta de la base de datos la conexión
`postgresql://…` (con la contraseña oculta) en lugar de un archivo SQLite.

Cada `git push` a `main` vuelve a desplegar Railway y Vercel automáticamente, y GitHub Actions ejecuta las
pruebas de CI sobre ese commit (insignia del README; cómo leerla: sección «Integración continua» del README).

La interfaz se descarga por secciones: una pestaña abierta antes de un despliegue que visita una sección que aún no
había cargado ve «No se pudo descargar esta sección.» y debe pulsar *Recargar página* (los archivos antiguos ya no
existen; es lo esperado, y `frontend/vercel.json` marca `/assets` como inmutables).

---

## Datos desde la nube

- **Binance** bloquea su API principal desde EE. UU.; NEXUS usa `data-api.binance.vision` y tiene respaldo
  en OKX, Coinbase, Kraken y KuCoin. Aun así, una **región europea** es la opción más compatible.
- El escáner de **TradingView** podría limitar IPs de centros de datos; si pasa, verás avisos en la terminal
  y NEXUS reintentará con calma (caché y límite de frecuencia).
- El semáforo lee cada 12 h los calendarios oficiales de `www.bls.gov` y `www.bea.gov` (HTTPS). Si el host
  restringe las salidas y no los permite, se usan las fechas fijas incluidas en el código.
- Las API keys de proveedores (Twelve Data, etc.) se guardan en tu base de datos (esquema privado, fuera de la
  API pública de Supabase). Las usa el backend y las ve en *Ajustes → Proveedores de datos* quien inicie sesión con
  un correo de `NEXUS_ALLOWED_EMAILS` (`GET /api/settings` se las devuelve a la interfaz); la terminal y los
  registros las muestran como `••••••`.
- Los contadores de créditos del tiempo real (Twelve Data, Polygon) son del proceso activo: si usas la misma clave en
  otro sitio a la vez, sus estimaciones por minuto y por día son optimistas (las respuestas 429 del proveedor se
  respetan igualmente).
- Una validación walk-forward (*Aprendizaje → Validar ahora*) descarga como mucho 24 series de 5.000 velas de las API
  públicas de los exchanges y usa unos minutos de CPU como mucho, en hilos aparte; no necesita variables nuevas.

## Alertas desde la nube

- Las alertas por Telegram y correo (*Ajustes → Alertas*) las envía **solo la instancia activa**; una en espera no
  envía nada.
- El host debe permitir salidas HTTPS a `api.telegram.org` y SMTP por los puertos 587 (STARTTLS) o 465 (SSL/TLS);
  muchas plataformas bloquean el 25.
- El token del bot y la contraseña SMTP se guardan en la tabla de ajustes de tu Postgres, como las claves de
  proveedores: `GET /api/settings` los devuelve a la interfaz con sesión, pero no aparecen en el estado, en
  `/api/health`, en los eventos ni en la terminal.

## PyChart en la nube

La primera ejecución de PyChart registra qué protecciones del sistema están activas, en una línea como
«Aislamiento del sistema de PyChart (linux): sin privilegios nuevos (no_new_privs): sí · … · servidor no volcable
(…): sí». En hosts que ejecutan la imagen como contenedor Docker (probablemente Railway y Render) es normal que el
espacio de nombres de red se rechace (EPERM): la línea lo dice y la red la bloquean entonces solo las capas de Python
del sandbox. No uses contenedores privilegiados ni añadas capacidades para habilitarlo. En una máquina virtual como
las de Fly.io (Firecracker) suele estar disponible. Si el servidor no pudo marcarse como no volcable, los *Deploy
Logs* lo avisan al arrancar («No se pudo marcar el proceso del servidor como no volcable …»). Límites: el proceso de
cada script usa el mismo usuario que el servidor y el espacio de nombres solo aísla la red IP (detalle en «Known
limitations» de `docs/ARCHITECTURE.md`).

## Tamaño de la base de datos

La retención automática mantiene la base pequeña (velas finas, eventos de más de 60 días, predicciones resueltas del
escáner de más de 30 días, órdenes demo terminadas de más de un año, memorias de NEXUS sin uso en 90 días). Lo que
más ocupa:

- **Memoria de NEXUS**: unos 0,6 MB por símbolo y temporalidad analizados (fila `model_state` de tipo `nexus`, con
  hasta 60 anclas; hasta ~3,5 MB con `nexus.horizon` 200), reescrita en cada análisis. Para reanudar exactamente su
  historial, cada análisis pide al hub `límite + 665` velas (1.665 con el límite por defecto de 1.000).
- **Vectores del escáner para `skill replay`**: unos 3 MB por día de predicciones resueltas (~20 MB con los 7 días por
  defecto de `scanner.replay_days`). Si el plan gratuito de Supabase (500 MB) se queda corto, ponlo a 0 (`settings
  scanner.replay_days 0`): el informe de habilidad en vivo no los necesita.
- **Último informe de validación**: ~0,2 MB (`model_state`, tipo `validation`).

## Usar la base de datos de Supabase también en local (opcional)

```powershell
$env:NEXUS_DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres"
.\start.bat
```
`start.bat` hereda la variable de esa ventana de PowerShell (ejecutar `.\start.ps1` directamente lo bloquea
la política de ejecución predeterminada de Windows; si prefieres PowerShell:
`powershell -ExecutionPolicy Bypass -File .\start.ps1`). Si a la vez tienes el backend desplegado con la misma
base de datos, la instancia que arranque segunda quedará en espera (ver "Una sola instancia activa").
En redes corporativas el puerto 5432 suele estar bloqueado; en ese caso sigue usando SQLite en local.

## Seguridad (resumen)

- Login obligatorio con Supabase Auth en toda la API y el WebSocket, y lista de correos permitidos obligatoria.
- Si Supabase no responde, la API contesta `503` (reintenta) en lugar de cerrar tu sesión; un token rechazado
  da `401`. El WebSocket se cierra con el código 4401 cuando la sesión caduca o se revoca (se comprueba
  periódicamente, no solo al conectar).
- CORS y WebSocket limitados a tus dominios de Vercel; los cambios (POST/PUT/PATCH/DELETE) que un navegador envía
  desde otro sitio se rechazan con 403 (protección CSRF por la cabecera Origin); cabeceras de seguridad en la
  interfaz.
- Tablas en un esquema privado con RLS; conexión a Postgres cifrada (TLS).
- Contenedor sin privilegios de root, con las versiones exactas de dependencias que pasaron las pruebas
  (`backend/requirements.lock`) y el código de la app de solo lectura para su usuario; los scripts de PyChart se
  validan y corren aislados con límite de tiempo y un límite aproximado de memoria (`NEXUS_SCRIPT_MEMORY_MB`), más
  el aislamiento del sistema que permita el host (ver «PyChart en la nube»).
- La terminal y los registros nunca muestran claves ni contraseñas (`••••••`).
- Cada push a `main` y cada PR construyen la imagen en GitHub Actions y comprueban que falla cerrada, que el servidor
  no corre como root, que su código no se puede modificar y que el sandbox limita la memoria (insignia CI del
  README).
- El servidor no arranca en público sin autenticación, sin lista de correos ni sin base de datos persistente.
- Una sola instancia ejecuta los servicios de fondo aunque coincidan dos (bloqueo en Postgres).
