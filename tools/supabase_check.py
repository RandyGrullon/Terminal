"""Check the Supabase settings of the repo's .env without printing any secret.

    backend/.venv/Scripts/python.exe tools/supabase_check.py          (from the repo root)

Reads ./.env (NEXUS_* lines only), then checks: the project URL (no /rest/v1), the publishable key (refuses a
sb_secret_ key: NEXUS never uses it), the Auth settings Supabase publishes (sign-ups should be closed), the allowed
e-mails, and the database: the Session pooler URI (port 5432), a connection, the `nexus` schema and its accounts.
Exit code 0 when everything needed for NEXUS is in place. Nothing is written anywhere.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db_pg import redact_dsn  # noqa: E402
from app.util import http_client  # noqa: E402

PLACEHOLDER = "PEGA_AQUI"
OK, BAD, WARN = "✓", "✗", "!"


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(NEXUS_[A-Z0-9_]+)\s*=\s*(.*?)\s*$", raw)
        if m and not raw.lstrip().startswith("#"):
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def missing(v: str | None) -> bool:
    return not v or PLACEHOLDER in v


async def main() -> int:
    env = load_env(ROOT / ".env")
    lines: list[tuple[str, str]] = []
    fatal = False

    def say(mark: str, text: str) -> None:
        nonlocal fatal
        lines.append((mark, text))
        if mark == BAD:
            fatal = True

    if not env:
        print("No hay .env en la raíz del repo (o no tiene variables NEXUS_).")
        return 1
    url = env.get("NEXUS_SUPABASE_URL", "")
    key = env.get("NEXUS_SUPABASE_ANON_KEY", "")
    dsn = env.get("NEXUS_DATABASE_URL", "")
    emails = env.get("NEXUS_ALLOWED_EMAILS", "")

    # project URL
    parts = urlsplit(url)
    ref = parts.hostname.split(".")[0] if parts.hostname else ""
    if missing(url) or parts.scheme != "https" or not (parts.hostname or "").endswith(".supabase.co"):
        say(BAD, "NEXUS_SUPABASE_URL: debe ser https://<proyecto>.supabase.co")
    elif parts.path not in ("", "/"):
        say(BAD, f"NEXUS_SUPABASE_URL: quita «{parts.path}» del final (solo la dirección base)")
    else:
        say(OK, f"Proyecto: {parts.hostname}")

    # publishable key (never the secret one)
    if missing(key):
        say(BAD, "NEXUS_SUPABASE_ANON_KEY: falta pegar la clave sb_publishable_…")
    elif key.startswith("sb_secret_"):
        say(BAD, "NEXUS_SUPABASE_ANON_KEY tiene la clave SECRETA (sb_secret_…): bórrala de aquí, cámbiala en Supabase "
                 "y pega la publishable")
    elif key.startswith("sb_publishable_") or key.startswith("eyJ"):
        say(OK, "Clave publishable presente (no se muestra)")
    else:
        say(WARN, "NEXUS_SUPABASE_ANON_KEY no parece una clave de Supabase (sb_publishable_… o eyJ…)")

    # Auth settings published by Supabase (public endpoint, needs only the publishable key)
    if not fatal and not missing(key):
        try:
            async with http_client(timeout=15.0, headers={"apikey": key}) as client:
                r = await client.get(f"https://{parts.hostname}/auth/v1/settings")
            if r.status_code == 200:
                s = r.json()
                if s.get("disable_signup"):
                    say(OK, "Registro cerrado: nadie más puede crearse una cuenta")
                else:
                    say(WARN, "El registro está ABIERTO: en Supabase → Authentication → Sign In / Providers desactiva "
                              "«Allow new users to sign up»")
                if not (s.get("external") or {}).get("email", True):
                    say(BAD, "El acceso por correo está desactivado en Supabase (NEXUS lo usa)")
            elif r.status_code in (401, 403):
                say(BAD, f"Supabase rechazó la clave publishable ({r.status_code}): revisa que sea la de este proyecto")
            else:
                say(WARN, f"Supabase respondió {r.status_code} al leer los ajustes de Auth")
        except Exception as exc:  # noqa: BLE001
            say(WARN, f"No se pudo hablar con Supabase Auth: {type(exc).__name__}")

    # allowed e-mails
    mails = [m.strip() for m in emails.split(",") if m.strip()]
    if missing(emails) or not mails or not all(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", m) for m in mails):
        say(BAD, "NEXUS_ALLOWED_EMAILS: pon el correo del usuario que creaste en Supabase")
    else:
        say(OK, f"Correos permitidos: {len(mails)}")

    # database
    placeholder = re.search(r"\[YOUR-[A-Z-]*PASSWORD\]", dsn or "")
    if missing(dsn):
        say(BAD, "NEXUS_DATABASE_URL: falta pegar la URI del Session pooler")
    elif placeholder:  # before parsing: brackets in the host part are read as an IPv6 address
        say(BAD, f"NEXUS_DATABASE_URL todavía dice {placeholder.group(0)}: reemplázalo (corchetes incluidos) por la "
                 "contraseña de la base de datos")
    else:
        d = urlsplit(dsn)
        shown = redact_dsn(dsn)
        if d.scheme not in ("postgresql", "postgres"):
            say(BAD, f"NEXUS_DATABASE_URL debe empezar por postgresql:// ({shown})")
        elif d.port == 6543:
            say(BAD, "NEXUS_DATABASE_URL usa el Transaction pooler (6543): usa el Session pooler (5432)")
        else:
            if ref and ref not in dsn:
                say(WARN, f"La URI no menciona el proyecto {ref}: ¿es de otro proyecto? ({shown})")
            try:
                import asyncpg

                from app.db_pg import _ssl_option

                conn = await asyncpg.connect(dsn, ssl=_ssl_option(dsn), timeout=20)
                try:
                    ver = await conn.fetchval("SHOW server_version")
                    say(OK, f"Conexión a la base: PostgreSQL {ver} ({shown})")
                    schema = env.get("NEXUS_DB_SCHEMA", "nexus")
                    has = await conn.fetchval("SELECT 1 FROM information_schema.schemata WHERE schema_name=$1", schema)
                    if not has:
                        say(WARN, f"El esquema «{schema}» aún no existe: se crea al copiar tus cuentas (python -m app.dbcopy) "
                                  "o al arrancar NEXUS con esta base")
                    else:
                        rows = await conn.fetch(f'SELECT id, name, balance FROM "{schema}".paper_accounts ORDER BY id')
                        say(OK, f"Esquema «{schema}» con {len(rows)} cuentas"
                                + (": " + ", ".join(f"{r['name']} {float(r['balance']):,.2f} $" for r in rows[:6]) if rows else ""))
                finally:
                    await conn.close()
            except Exception as exc:  # noqa: BLE001
                say(BAD, f"No se pudo conectar a la base ({type(exc).__name__}): revisa la contraseña y que sea el Session "
                         f"pooler ({shown})")

    print("\nComprobación de Supabase (los valores secretos nunca se muestran)\n")
    for mark, text in lines:
        print(f"  {mark} {text}")
    print("\n" + ("Faltan cosas (✗) antes de usar Supabase con NEXUS." if fatal else "Todo lo necesario está listo."))
    return 1 if fatal else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(asyncio.run(main()))
