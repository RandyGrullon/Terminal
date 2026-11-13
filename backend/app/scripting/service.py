"""PyChart service layer (`rt.scripts`): script storage (CRUD + seeded examples), compile and run.

The compiler/sandbox (app.scripting.validator / app.scripting.sandbox) are imported lazily: when they are not
available every compile/run call fails with HTTP-like status 503 and a Spanish message, while CRUD keeps working.

Errors are raised as `ScriptServiceError(status, detail)`; the REST layer turns them into HTTP errors and the
terminal commands print the Spanish detail.
"""
from __future__ import annotations

import asyncio
import time
import unicodedata
from typing import Any, Callable

from app import config
from app.util import RateLimitedLog, clean_json, normalize_tf, now_ms, now_s, tf_seconds

SOURCE = "scripts"
NAME_MAX = 80
DESCRIPTION_MAX = 500
CODE_MAX = 100_000  # same limit as the compiler (validator.MAX_CODE_CHARS)
LIMIT_MIN = 50
LIMIT_MAX = 5000
RUN_TIMEOUT_S = 10.0
NEWS_MARGIN_S = 2 * 86400  # calendar margin around the candles of a run (noticias)
MAX_CONCURRENT_RUNS = 2
STDOUT_EVENT_LINES = 40
STDOUT_LINE_MAX = 300
EVENT_ERRORS_MAX = 5
EVENT_TEXT_MAX = 300  # events are kept in memory, stored and sent to every terminal: never user-sized payloads
ERROR_MESSAGE_MAX = 4000  # same limit as the compiler/sandbox (validator.ERROR_MESSAGE_MAX)
COMPILE_WARN_EVERY_S = 10.0

MSG_UNAVAILABLE = "Compilador PyChart no disponible todavía"
MSG_EXAMPLE_PROTECTED = "Los ejemplos no se pueden modificar; guarda una copia"
MSG_NOT_FOUND = "Script no encontrado"

DEFAULT_META = {"title": "Script", "overlay": True, "kind": "indicator"}
RUN_KEYS_DEFAULTS: dict[str, Any] = {
    "ok": False,
    "errors": [],
    "stdout": "",
    "duration_ms": 0,
    "time": [],
    "plots": [],
    "markers": [],
    "hlines": [],
    "bgcolors": [],
    "alerts": [],
    "strategy": None,
}


class ScriptServiceError(Exception):
    """Expected, user-facing error (Spanish `detail`) with the HTTP status the API should answer."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = detail


# ----------------------------------------------------------------------------- helpers


def normalize_name(name: str) -> str:
    """Comparison key for script names: case-, accent- and whitespace-insensitive."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def _clean_name(name: Any) -> str:
    if name is None or not str(name).strip():
        raise ScriptServiceError(400, "El nombre del script es obligatorio")
    text = " ".join(str(name).split())
    if len(text) > NAME_MAX:
        raise ScriptServiceError(400, f"El nombre es demasiado largo (máximo {NAME_MAX} caracteres)")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise ScriptServiceError(400, "El nombre contiene caracteres no válidos")
    return text


def _clean_description(description: Any) -> str:
    text = "" if description is None else str(description).strip()
    if len(text) > DESCRIPTION_MAX:
        raise ScriptServiceError(400, f"La descripción es demasiado larga (máximo {DESCRIPTION_MAX} caracteres)")
    return text


def _clean_code(code: Any, *, allow_empty: bool = False) -> str:
    if code is None:
        raise ScriptServiceError(400, "Falta el código del script")
    text = str(code).replace("\r\n", "\n").replace("\r", "\n")
    if not allow_empty and not text.strip():
        raise ScriptServiceError(400, "El código del script está vacío")
    if len(text) > CODE_MAX:
        raise ScriptServiceError(400, f"El código es demasiado largo (máximo {CODE_MAX // 1000} KB)")
    return text


def _meta_row(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "description": str(row.get("description") or ""),
        "is_example": bool(int(row.get("is_example") or 0)),
        "updated_at": int(row.get("updated_at") or 0),
    }


def _script_row(row: dict) -> dict:
    out = _meta_row(row)
    out["code"] = str(row.get("code") or "")
    out["created_at"] = int(row.get("created_at") or 0)
    return out


def _clip(text: Any, limit: int) -> str:
    value = str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def _error_line(errors: list[dict]) -> str:
    """'línea 12: mensaje' for the first error (Spanish one-liner for events and the terminal)."""
    if not errors:
        return "error desconocido"
    err = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
    line = err.get("line")
    msg = (str(err.get("message") or "")[:1000].splitlines() or ["error desconocido"])[0][:200]
    return f"línea {line}: {msg}" if line else msg


def _event_errors(errors: list) -> list[dict]:
    """The first errors with short messages, for event data."""
    return [
        {**err, "message": _clip(err.get("message") or "", EVENT_TEXT_MAX)}
        for err in errors[:EVENT_ERRORS_MAX]
        if isinstance(err, dict)
    ]


def _load_compiler() -> Callable[[str], dict]:
    try:
        from app.scripting.validator import compile_script
    except Exception as exc:  # module missing or broken: degrade, never crash
        raise ScriptServiceError(503, MSG_UNAVAILABLE) from exc
    return compile_script


def _load_sandbox() -> Callable[..., dict]:
    try:
        from app.scripting.sandbox import run_script
    except Exception as exc:
        raise ScriptServiceError(503, MSG_UNAVAILABLE) from exc
    return run_script


def compiler_available() -> bool:
    try:
        _load_compiler()
        _load_sandbox()
    except ScriptServiceError:
        return False
    return True


# --- end of section A (helpers) ---


class ScriptService:
    """`rt.scripts`: PyChart script storage, compiler and sandboxed runner (contract §6.5 / §12)."""

    def __init__(self, rt: Any):
        self.rt = rt
        self._running = False
        self._write_lock = asyncio.Lock()
        self._run_sem = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self._log = RateLimitedLog()
        self._alerted: dict[tuple, int] = {}
        self._total = 0
        self._examples = 0
        self._runs = 0
        self._run_errors = 0
        self._compiles = 0
        self._last_run_ms: int | None = None
        self._seed_report: dict[str, int] = {}
        self._compiler_ok = False
        self._compiler_checked_at = -1e18

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        try:
            self._seed_report = await self.seed_examples()
        except Exception as exc:  # never block the app start because of the examples
            self._emit("error", f"PyChart: no se pudieron instalar los ejemplos: {type(exc).__name__}: {exc}")
        await self._refresh_counts()
        self._running = True

    async def stop(self) -> None:
        self._running = False

    def status(self) -> dict:
        now = time.monotonic()
        if now - self._compiler_checked_at > 30.0:  # import probe is cheap but not free when the module is missing
            self._compiler_ok = compiler_available()
            self._compiler_checked_at = now
        return {
            "running": self._running,
            "scripts": self._total,
            "examples": self._examples,
            "runs": self._runs,
            "last_run_ms": self._last_run_ms,
            "run_errors": self._run_errors,
            "compiles": self._compiles,
            "compiler": self._compiler_ok,
        }

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        bus = getattr(self.rt, "bus", None)
        if bus is not None:
            try:
                bus.emit(level, SOURCE, message, data)
            except Exception:
                pass

    async def _refresh_counts(self) -> None:
        try:
            row = await self.rt.db.fetchone(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN is_example = 1 THEN 1 ELSE 0 END) AS examples FROM scripts"
            )
        except Exception:
            return
        if row:
            self._total = int(row.get("total") or 0)
            self._examples = int(row.get("examples") or 0)

    # ------------------------------------------------------------------ examples

    async def seed_examples(self) -> dict[str, int]:
        """Install the bundled examples (is_example=1), refresh their code when it changed, remove obsolete ones.
        User scripts are never touched: if a user script already uses an example's name, the example is installed
        as '<nombre> (ejemplo)' or skipped."""
        from app.scripting.examples import load_examples

        examples = load_examples()
        db = self.rt.db
        report = {"inserted": 0, "updated": 0, "removed": 0, "skipped": 0, "total": len(examples)}
        async with self._write_lock:
            rows = await db.fetchall("SELECT id, name, description, code, is_example FROM scripts")
            by_key = {normalize_name(r["name"]): r for r in rows}
            keep: set[int] = set()
            now = now_ms()
            for ex in examples:
                target, target_name, free_name = None, "", None
                for candidate in (ex["name"], f"{ex['name']} (ejemplo)"):
                    row = by_key.get(normalize_name(candidate))
                    if row is None:
                        free_name = free_name or candidate
                    elif int(row.get("is_example") or 0) == 1 and int(row["id"]) not in keep:
                        target, target_name = row, candidate
                        break
                if target is not None:
                    keep.add(int(target["id"]))
                    current = (target["name"], target["code"], target["description"])
                    if current != (target_name, ex["code"], ex["description"]):
                        try:  # also normalizes the stored name (e.g. an example whose capitalization changed)
                            await db.execute(
                                "UPDATE scripts SET name = ?, description = ?, code = ?, updated_at = ? "
                                "WHERE id = ? AND is_example = 1",
                                (target_name, ex["description"], ex["code"], now, int(target["id"])),
                            )
                        except Exception:
                            await db.execute(
                                "UPDATE scripts SET description = ?, code = ?, updated_at = ? WHERE id = ? AND is_example = 1",
                                (ex["description"], ex["code"], now, int(target["id"])),
                            )
                        report["updated"] += 1
                    continue
                if free_name is None:
                    report["skipped"] += 1
                    self._emit("warn", f"PyChart: el ejemplo '{ex['name']}' no se instaló porque ese nombre ya lo usa un script tuyo")
                    continue
                try:
                    new_id = await db.execute(
                        "INSERT INTO scripts(name, description, code, is_example, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                        (free_name, ex["description"], ex["code"], now, now),
                    )
                except Exception:
                    report["skipped"] += 1
                    continue
                by_key[normalize_name(free_name)] = {"id": new_id, "name": free_name, "is_example": 1,
                                                     "code": ex["code"], "description": ex["description"]}
                keep.add(int(new_id))
                report["inserted"] += 1
            for row in rows:
                if int(row.get("is_example") or 0) == 1 and int(row["id"]) not in keep:
                    await db.execute("DELETE FROM scripts WHERE id = ? AND is_example = 1", (int(row["id"]),))
                    report["removed"] += 1
        await self._refresh_counts()
        if report["inserted"] or report["updated"] or report["removed"]:
            self._emit(
                "info",
                f"PyChart: {self._examples} ejemplos listos ({report['inserted']} nuevos, "
                f"{report['updated']} actualizados, {report['removed']} retirados)",
            )
        return report

    # ------------------------------------------------------------------ CRUD

    async def list_scripts(self) -> list[dict]:
        """ScriptMeta[]: user scripts first, then the examples, each group alphabetically."""
        rows = await self.rt.db.fetchall("SELECT id, name, description, is_example, updated_at FROM scripts")
        metas = [_meta_row(r) for r in rows]
        metas.sort(key=lambda m: (m["is_example"], normalize_name(m["name"]), m["id"]))
        return metas

    async def get(self, script_id: Any) -> dict:
        try:
            sid = int(script_id)
        except (TypeError, ValueError):
            raise ScriptServiceError(404, MSG_NOT_FOUND) from None
        if sid <= 0 or sid >= 2**53:
            raise ScriptServiceError(404, MSG_NOT_FOUND)
        row = await self.rt.db.fetchone(
            "SELECT id, name, description, code, is_example, created_at, updated_at FROM scripts WHERE id = ?", (sid,)
        )
        if row is None:
            raise ScriptServiceError(404, MSG_NOT_FOUND)
        return _script_row(row)

    async def _name_owner(self, name: str, exclude_id: int | None = None) -> dict | None:
        key = normalize_name(name)
        for row in await self.rt.db.fetchall("SELECT id, name, is_example FROM scripts"):
            if normalize_name(row["name"]) == key and int(row["id"]) != exclude_id:
                return row
        return None

    async def create(self, name: Any, code: Any, description: Any = "") -> dict:
        clean_name = _clean_name(name)
        clean_code = _clean_code(code)
        clean_desc = _clean_description(description)
        async with self._write_lock:
            owner = await self._name_owner(clean_name)
            if owner is not None:
                raise ScriptServiceError(409, f"Ya existe un script llamado '{owner['name']}'")
            now = now_ms()
            try:
                new_id = await self.rt.db.execute(
                    "INSERT INTO scripts(name, description, code, is_example, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
                    (clean_name, clean_desc, clean_code, now, now),
                )
            except Exception:
                owner = await self._name_owner(clean_name)
                if owner is not None:
                    raise ScriptServiceError(409, f"Ya existe un script llamado '{owner['name']}'") from None
                raise
            if not new_id:
                new_id = await self.rt.db.fetchval("SELECT id FROM scripts WHERE name = ?", (clean_name,), default=0)
        await self._refresh_counts()
        self._emit("info", f"PyChart: script '{clean_name}' guardado")
        return await self.get(new_id)

    async def update(self, script_id: Any, *, name: Any = None, code: Any = None, description: Any = None) -> dict:
        current = await self.get(script_id)
        if current["is_example"]:
            raise ScriptServiceError(403, MSG_EXAMPLE_PROTECTED)
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = _clean_name(name)
        if code is not None:
            fields["code"] = _clean_code(code)
        if description is not None:
            fields["description"] = _clean_description(description)
        if not fields:
            return current
        async with self._write_lock:
            if "name" in fields:
                owner = await self._name_owner(fields["name"], exclude_id=current["id"])
                if owner is not None:
                    raise ScriptServiceError(409, f"Ya existe un script llamado '{owner['name']}'")
            assignments = ", ".join(f"{col} = ?" for col in fields)
            try:
                await self.rt.db.execute(
                    f"UPDATE scripts SET {assignments}, updated_at = ? WHERE id = ? AND is_example = 0",
                    (*fields.values(), now_ms(), current["id"]),
                )
            except Exception:
                if "name" in fields and await self._name_owner(fields["name"], exclude_id=current["id"]):
                    raise ScriptServiceError(409, f"Ya existe un script llamado '{fields['name']}'") from None
                raise
        return await self.get(current["id"])

    async def delete(self, script_id: Any) -> dict:
        current = await self.get(script_id)
        if current["is_example"]:
            raise ScriptServiceError(403, MSG_EXAMPLE_PROTECTED)
        async with self._write_lock:
            await self.rt.db.execute("DELETE FROM scripts WHERE id = ? AND is_example = 0", (current["id"],))
        await self._refresh_counts()
        self._emit("info", f"PyChart: script '{current['name']}' eliminado")
        return {"ok": True}

    async def match(self, query: Any) -> list[dict]:
        """ScriptMeta matches for user text (terminal): id, exact name, unique prefix, substring, all words.
        Returns the matches of the first tier that has any (accent/case-insensitive)."""
        text = str(query or "").strip()
        if not text:
            return []
        metas = await self.list_scripts()
        if text.isdigit():
            by_id = [m for m in metas if m["id"] == int(text)]
            if by_id:
                return by_id
        key = normalize_name(text)
        words = key.split()
        tiers: list[Callable[[str], bool]] = [
            lambda n: n == key,
            lambda n: n.startswith(key),
            lambda n: key in n,
            lambda n: all(w in n for w in words),
        ]
        for test in tiers:
            found = [m for m in metas if test(normalize_name(m["name"]))]
            if found:
                return found
        return []

    async def find(self, query: Any) -> dict:
        """Full Script for user text; 404 when nothing matches, 400 when ambiguous."""
        found = await self.match(query)
        if not found:
            raise ScriptServiceError(404, f"No hay ningún script que coincida con '{query}'. Usa 'scripts' para verlos")
        if len(found) > 1:
            names = ", ".join(f"[{m['id']}] {m['name']}" for m in found[:6])
            raise ScriptServiceError(400, f"'{query}' es ambiguo: {names}. Usa el id o el nombre entre comillas")
        return await self.get(found[0]["id"])

    # ------------------------------------------------------------------ compile

    async def compile(self, code: Any) -> dict:
        """CompileResult for `code` (AST validation, inputs and meta; nothing is executed)."""
        text = _clean_code(code, allow_empty=True)
        compile_script = _load_compiler()
        self._compiles += 1
        try:
            raw = await asyncio.to_thread(compile_script, text)
        except Exception as exc:  # a compiler bug must not become a 500
            if self._log.should_log("compile-crash", 60):
                self._emit("error", f"PyChart: fallo interno del compilador: {type(exc).__name__}: {_clip(exc, EVENT_TEXT_MAX)}")
            raw = {"ok": False, "errors": [_internal_error(f"Error interno del compilador: {type(exc).__name__}: {exc}", "syntax")]}
        result = _normalize_compile(raw)
        if not result["ok"] and self._log.should_log("compile-warn", COMPILE_WARN_EVERY_S):
            self._emit(
                "warn",
                f"PyChart: error de compilación en '{_clip(result['meta']['title'], 120)}' ({_error_line(result['errors'])})",
                {"errors": _event_errors(result["errors"])},
            )
        return result

    # ------------------------------------------------------------------ run

    async def run(
        self,
        code: Any,
        symbol: Any,
        tf: Any = "1h",
        limit: Any = 1000,
        inputs: Any = None,
        *,
        name: str | None = None,
        closed_only: bool = False,
        quiet: bool = False,
    ) -> dict:
        """Run a script on `limit` candles of symbol/tf inside the sandbox -> ScriptRunResult. closed_only: leave out a
        last candle that is still forming (a strategy's `next` is then the action at the open after a CLOSED bar).
        quiet: no events nor alerts at all (the caller reports its errors: NEXUS automática runs its strategies on every
        bar of every market and notes an error at most every 6 h)."""
        text = _clean_code(code)
        if inputs is not None and not isinstance(inputs, dict):
            raise ScriptServiceError(400, "Las entradas deben ser un objeto {nombre: valor}")
        try:
            tf_key = normalize_tf(tf or "1h")
        except ValueError as exc:
            raise ScriptServiceError(400, str(exc)) from None
        try:
            bars = int(limit) if limit is not None else 1000
        except (TypeError, ValueError):
            raise ScriptServiceError(400, "El número de velas (limit) debe ser un entero") from None
        bars = max(LIMIT_MIN, min(LIMIT_MAX, bars))
        run_script = _load_sandbox()
        sym = await self._resolve_symbol(symbol)
        candles = await self._candles(sym, tf_key, bars)
        if closed_only and candles and int(candles[-1]["time"]) + tf_seconds(tf_key) > now_s():
            candles = candles[:-1]
            if not candles:
                raise ScriptServiceError(400, f"Sin velas cerradas de {sym} {tf_key}")
        news = self._news(candles)
        extra = {"news": news} if news is not None else {}
        started = time.perf_counter()
        async with self._run_sem:
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(run_script, text, candles, dict(inputs or {}), symbol=sym, tf=tf_key,
                                      timeout=RUN_TIMEOUT_S, **extra),
                    timeout=RUN_TIMEOUT_S + 20,
                )
            except asyncio.TimeoutError:
                raw = {"ok": False, "errors": [_internal_error(f"El script superó el tiempo máximo ({RUN_TIMEOUT_S:.0f} s)", "timeout")]}
            except Exception as exc:
                if self._log.should_log("sandbox-crash", 60):
                    self._emit("error", f"PyChart: fallo interno del sandbox: {type(exc).__name__}: {_clip(exc, EVENT_TEXT_MAX)}")
                raw = {"ok": False, "errors": [_internal_error(f"Error interno del sandbox: {type(exc).__name__}: {exc}", "runtime")]}
        elapsed_ms = int(round((time.perf_counter() - started) * 1000))
        result = _normalize_run(raw, candles, elapsed_ms)
        self._runs += 1
        self._last_run_ms = int(result["duration_ms"])
        if not result["ok"]:
            self._run_errors += 1
        if not quiet:
            self._run_events(result, sym, tf_key, name)
        return result

    def _news(self, candles: list[dict]) -> dict | None:
        """Economic calendar for `noticias` over the candles' dates (± NEWS_MARGIN_S), from the macro module. None
        when the module is not loaded or fails: the script then runs without news (noticias.disponible = False)."""
        macro = getattr(self.rt, "macro", None)
        if macro is None or not candles or not hasattr(macro, "script_events"):
            return None
        try:
            first, last = int(candles[0]["time"]), int(candles[-1]["time"])
            return macro.script_events(first - NEWS_MARGIN_S, last + NEWS_MARGIN_S)
        except Exception as exc:  # noqa: BLE001 - news are optional: never break a run
            if self._log.should_log("news", 600):
                self._emit("warn", f"PyChart: no se pudo preparar el calendario de noticias: {_clip(exc, EVENT_TEXT_MAX)}")
            return None

    async def _resolve_symbol(self, symbol: Any) -> str:
        text = str(symbol or "").strip()
        if not text:
            raise ScriptServiceError(400, "Falta el símbolo (por ejemplo BINANCE:BTCUSDT o SIM:NEXUS)")
        if ":" in text:
            return text.upper()
        hub = getattr(self.rt, "hub", None)
        if hub is not None:
            try:
                resolved = await hub.resolve(text)
            except Exception:
                resolved = None
            if resolved:
                return str(resolved)
        return text.upper()

    async def _candles(self, symbol: str, tf: str, limit: int) -> list[dict]:
        hub = getattr(self.rt, "hub", None)
        if hub is None:
            raise ScriptServiceError(400, "No hay datos de mercado: el módulo de mercado no está cargado")
        try:
            cs = await hub.get_candles(symbol, tf, limit)
        except Exception as exc:
            raise ScriptServiceError(400, f"No se pudieron obtener velas de {symbol} {tf}: {exc}") from None
        rows = cs.to_list() if hasattr(cs, "to_list") else list(cs or [])
        if not rows:
            note = getattr(cs, "note", None)
            offline = " (modo sin conexión)" if config.OFFLINE else ""
            raise ScriptServiceError(400, f"Sin velas para {symbol} {tf}{offline}" + (f": {note}" if note else ""))
        return rows[-limit:]

    # ------------------------------------------------------------------ events

    def _run_events(self, result: dict, symbol: str, tf: str, name: str | None) -> None:
        """Bus events of a run. Every text taken from the script result is clipped: events are kept in memory,
        stored in the database and sent to every terminal."""
        title = _clip(name or result["meta"].get("title") or "Script", 120)
        bars = len(result["time"])
        info = {"symbol": symbol, "tf": tf, "title": title, "bars": bars, "duration_ms": result["duration_ms"]}
        if result["ok"]:
            parts = [f"PyChart ▶ '{title}' · {symbol} {tf}", f"{bars} velas", f"{len(result['plots'])} gráficos"]
            if result["alerts"]:
                parts.append(f"{len(result['alerts'])} alertas")
            metrics = strategy_metrics(result)
            if metrics is not None:
                parts.append(
                    f"estrategia: {int(metrics.get('trades') or 0)} ops, retorno {fmt_pct(metrics.get('total_return_pct'), signed=True)}, "
                    f"acierto {fmt_pct(win_rate_pct(metrics.get('win_rate')))}"
                )
            parts.append(f"{result['duration_ms']} ms")
            self._emit("info", " · ".join(parts), info)
        else:
            kind = (result["errors"][0].get("kind") if result["errors"] and isinstance(result["errors"][0], dict) else None) or "runtime"
            self._emit(
                "warn",
                f"PyChart ✖ '{title}' · {symbol} {tf} · {_KIND_LABELS.get(kind, kind)}: {_error_line(result['errors'])}",
                {**info, "errors": _event_errors(result["errors"])},
            )
        last_time = result["time"][-1] if result["time"] else None
        if last_time is not None:
            fired = 0
            for alert in result["alerts"]:
                if not isinstance(alert, dict) or alert.get("time") != last_time or fired >= 10:
                    continue
                key = (symbol, tf, str(alert.get("title")), int(last_time))
                if key in self._alerted:
                    continue
                if len(self._alerted) > 500:
                    self._alerted.clear()
                self._alerted[key] = now_ms()
                fired += 1
                alert_title = _clip(alert.get("title") or title, 80)
                message = _clip(str(alert.get("message") or alert.get("title") or "Alerta").strip(), EVENT_TEXT_MAX)
                self._emit("signal", f"PyChart · {symbol} {tf} · {alert_title}: {message}",
                           {**info, "alert": {"time": alert.get("time"), "title": alert_title, "message": message}})
        lines = [ln.rstrip() for ln in str(result["stdout"]).splitlines() if ln.strip()]
        for ln in lines[:STDOUT_EVENT_LINES]:
            self._emit("debug", f"PyChart ▸ {ln[:STDOUT_LINE_MAX]}")
        if len(lines) > STDOUT_EVENT_LINES:
            self._emit("debug", f"PyChart ▸ … {len(lines) - STDOUT_EVENT_LINES} líneas más")


# ----------------------------------------------------------------------------- result normalization

_KIND_LABELS = {"syntax": "error de sintaxis", "security": "bloqueado por seguridad", "runtime": "error de ejecución",
                "timeout": "tiempo agotado"}


def _internal_error(message: str, kind: str) -> dict:
    return {"line": None, "col": None, "message": message, "kind": kind}


def _normalize_errors(errors: Any) -> list[dict]:
    out: list[dict] = []
    for err in errors if isinstance(errors, list) else []:
        if isinstance(err, dict):
            kind = err.get("kind") if err.get("kind") in _KIND_LABELS else "runtime"
            message = _clip(err.get("message") or "", ERROR_MESSAGE_MAX)
            out.append({"line": err.get("line"), "col": err.get("col"), "message": message, "kind": kind})
        else:
            out.append(_internal_error(_clip(err, ERROR_MESSAGE_MAX), "runtime"))
    return out


def _normalize_meta(meta: Any) -> dict:
    out = dict(DEFAULT_META)
    if isinstance(meta, dict):
        out.update({k: v for k, v in meta.items() if v is not None})
    out["overlay"] = bool(out.get("overlay"))
    out["kind"] = "strategy" if out.get("kind") == "strategy" else "indicator"
    out["title"] = str(out.get("title") or DEFAULT_META["title"])
    return out


def _normalize_compile(raw: Any) -> dict:
    src = raw if isinstance(raw, dict) else {}
    errors = _normalize_errors(src.get("errors"))
    return clean_json({
        "ok": bool(src.get("ok")) and not errors,
        "errors": errors,
        "warnings": [str(w) for w in (src.get("warnings") or [])],
        "inputs": list(src.get("inputs") or []),
        "meta": _normalize_meta(src.get("meta")),
    })


def _normalize_run(raw: Any, candles: list[dict], elapsed_ms: int) -> dict:
    out = dict(raw) if isinstance(raw, dict) else {}
    for key, default in RUN_KEYS_DEFAULTS.items():
        if out.get(key) is None:
            out[key] = list(default) if isinstance(default, list) else default
    out["errors"] = _normalize_errors(out["errors"])
    out["ok"] = bool(out["ok"]) and not out["errors"]
    out["meta"] = _normalize_meta(out.get("meta"))
    out["stdout"] = str(out["stdout"])
    if not out.get("candles"):
        out["candles"] = candles
    if not out["time"]:
        out["time"] = [int(c["time"]) for c in out["candles"]]
    try:
        out["duration_ms"] = int(round(float(out["duration_ms"]))) or elapsed_ms
    except (TypeError, ValueError):
        out["duration_ms"] = elapsed_ms
    return clean_json(out)


def strategy_metrics(result: dict) -> dict | None:
    strat = result.get("strategy")
    if isinstance(strat, dict) and isinstance(strat.get("metrics"), dict):
        return strat["metrics"]
    return None


def win_rate_pct(value: Any) -> float | None:
    """BacktestMetrics.win_rate is a fraction (0..1); accept percentages too."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v * 100.0 if abs(v) <= 1.0 else v


def fmt_pct(value: Any, signed: bool = False) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"
