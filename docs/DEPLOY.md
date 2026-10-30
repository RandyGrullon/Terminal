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
| Supabase (Postgres + login) | Free | gratis (500 MB; NEXUS aplica retención automática) |
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
   registrarse; además NEXUS solo deja entrar a los correos de `NEXUS_ALLOWED_EMAILS`).

No tienes que crear tablas: el backend crea el esquema `nexus` al arrancar (fuera del esquema público, con
Row Level Security activado, así la API pública de Supabase nunca expone tus datos).

## Paso 2 — Backend en Railway

1. 👤 [railway.com](https://railway.com) → **New Project → Deploy from GitHub repo** → `RandyGrullon/Terminal`.
   Railway detecta el `Dockerfile` y `railway.json` de la raíz.
2. 👤 **Settings → Region**: *EU West (Amsterdam)* (misma zona que Supabase).
3. 👤 **Variables** (copia los nombres de `.env.example`):

   | Variable | Valor |
   |---|---|
   | `NEXUS_DATABASE_URL` | la URI del *Session pooler* del paso 1.3 |
   | `NEXUS_AUTH` | `supabase` |
   | `NEXUS_SUPABASE_URL` | `https://<ref>.supabase.co` |
   | `NEXUS_SUPABASE_ANON_KEY` | la clave anon/publishable |
   | `NEXUS_ALLOWED_EMAILS` | tu correo (varios separados por comas) |
   | `NEXUS_CORS_ORIGINS` | de momento `http://localhost:5173`; en el paso 3 pondrás tu dominio de Vercel |

4. 👤 **Settings → Networking → Generate Domain** → copia la URL, por ejemplo `https://nexus-production.up.railway.app`.
5. Comprueba `https://<tu-backend>/api/health` → debe responder `{"ok": true, "auth": "required", ...}`.

**Importante**: deja **1 réplica** (los servicios de fondo no deben duplicarse). El servidor se niega a
arrancar en una IP pública si falta `NEXUS_AUTH=supabase`: un despliegue mal configurado falla cerrado.

<details>
<summary>Alternativa: Fly.io en lugar de Railway</summary>

```bash
fly launch --no-deploy --copy-config --name <tu-app>
fly secrets set NEXUS_DATABASE_URL="..." NEXUS_SUPABASE_URL="..." NEXUS_SUPABASE_ANON_KEY="..." \
  NEXUS_ALLOWED_EMAILS="tu@correo.com" NEXUS_CORS_ORIGINS="https://<tu-app>.vercel.app"
fly deploy
```
`fly.toml` ya deja la máquina siempre encendida, en Frankfurt y con chequeo de salud.
</details>

## Paso 3 — Interfaz en Vercel

1. 👤 [vercel.com](https://vercel.com) → **Add New → Project → Import** `RandyGrullon/Terminal`.
2. 👤 **Root Directory**: `frontend` (Vercel detecta Vite; `frontend/vercel.json` ya está incluido).
3. 👤 **Environment Variables**: `VITE_API_BASE` = la URL del backend del paso 2.4 (sin barra final).
   La configuración de Supabase la entrega el propio backend, no hace falta repetirla.
4. 👤 **Deploy** → copia tu URL, por ejemplo `https://nexus-terminal.vercel.app`.
5. 👤 Vuelve a Railway → `NEXUS_CORS_ORIGINS=https://nexus-terminal.vercel.app` (añade también tu dominio
   propio si lo usas, separado por comas) → se redepliega solo.
6. 👤 Supabase → **Authentication → URL Configuration → Site URL** = tu URL de Vercel.

## Paso 4 — Probar

Abre tu URL de Vercel → inicia sesión con el correo del paso 1.4 → deberías ver el gráfico, la terminal
"En vivo" y en **Ajustes → Sistema** la base de datos *PostgreSQL* y los servicios activos.

Cada `git push` a `main` vuelve a desplegar Railway y Vercel automáticamente.

---

## Datos desde la nube

- **Binance** bloquea su API principal desde EE. UU.; NEXUS usa `data-api.binance.vision` y tiene respaldo
  en OKX, Coinbase, Kraken y KuCoin. Aun así, una **región europea** es la opción más compatible.
- El escáner de **TradingView** podría limitar IPs de centros de datos; si pasa, verás avisos en la terminal
  y NEXUS reintentará con calma (caché y límite de frecuencia).
- Las API keys de proveedores (Twelve Data, etc.) se guardan en tu base de datos y solo el backend las ve.

## Usar la base de datos de Supabase también en local (opcional)

```powershell
$env:NEXUS_DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres"
.\start.ps1
```
En redes corporativas el puerto 5432 suele estar bloqueado; en ese caso sigue usando SQLite en local.

## Seguridad (resumen)

- Login obligatorio con Supabase Auth en toda la API y el WebSocket; lista de correos permitidos.
- CORS y WebSocket limitados a tus dominios de Vercel; cabeceras de seguridad en la interfaz.
- Tablas en un esquema privado con RLS; conexión a Postgres cifrada (TLS).
- Contenedor sin privilegios de root; los scripts de PyChart se validan y corren aislados con límite de tiempo.
- El servidor no arranca en público sin autenticación.
