"""ValidationService (`rt.validation`): walk-forward validation jobs of NEXUS + Trade Gate, one at a time.

`run()` starts a background job (candles through rt.hub, CPU work in worker threads), `status()` reports its
progress and `latest()` returns the last report, persisted in model_state(kind='validation', key='latest') so it
survives restarts (no schema change). Progress and results are also emitted as events (source 'validation').
"""
from __future__ import annotations

import asyncio
import copy
import json
import traceback
from typing import Any

from app.util import clean_json, now_ms
from app.validation.data import load_from_hub
from app.validation.runner import (
    DEFAULT_SYMBOLS, DEFAULT_TFS, ValidationInputError, ValidationRequest, normalize_request, run_validation,
)
from app.validation.verdict import count_text
from app.validation.walkforward import WFConfig

STATE_KIND = "validation"
STATE_KEY = "latest"
RESOLVE_TIMEOUT_S = 20.0


class ValidationBusy(RuntimeError):
    """A validation job is already running (HTTP 409)."""


class ValidationService:
    def __init__(self, rt: Any):
        self.rt = rt
        self._task: asyncio.Task | None = None
        self._job: dict[str, Any] | None = None
        self._latest: dict[str, Any] | None = None  # cached last report (None = not loaded yet)
        self._loaded = False
        self._lock = asyncio.Lock()
        self._seq = 0

    # ------------------------------------------------------------------ service protocol

    async def start(self) -> None:
        return None  # no background loop: jobs start on demand

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        """ValidationStatus: {running (a job is in progress), state, job, defaults}. `state` = the last job's state
        (running | done | error | cancelled) or "idle" when no job ran since the server started."""
        job = copy.deepcopy(self._job)
        return clean_json({"running": self.running, "state": job["state"] if job else "idle", "job": job,
                           "defaults": self.defaults()})

    # ------------------------------------------------------------------ settings

    def _setting(self, key: str, default: Any) -> Any:
        settings = getattr(self.rt, "settings", None)
        if settings is None:
            return default
        try:
            value = settings.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def defaults(self) -> dict[str, Any]:
        symbols = [str(s).upper() for s in (self._setting("validation.symbols", DEFAULT_SYMBOLS) or []) if str(s)]
        tfs = [str(t) for t in (self._setting("validation.tfs", DEFAULT_TFS) or []) if str(t)]
        try:
            bars = int(self._setting("validation.bars", 3000))
        except (TypeError, ValueError):
            bars = 3000
        return {"symbols": symbols or list(DEFAULT_SYMBOLS), "tfs": tfs or list(DEFAULT_TFS), "bars": bars}

    def config(self) -> WFConfig:
        def num(key: str, default: float) -> float:
            try:
                return float(self._setting(key, default))
            except (TypeError, ValueError):
                return default

        return WFConfig(folds=int(num("validation.folds", 5)), warmup_bars=int(num("validation.warmup_bars", 500)),
                        random_runs=int(num("validation.random_runs", 1000)),
                        risk_pct=num("risk.per_trade_pct", 1.0), slippage_pct=num("risk.slippage_pct", 0.02))

    def _settings(self) -> dict[str, Any]:
        settings = getattr(self.rt, "settings", None)
        if settings is not None:
            try:
                return settings.all()
            except Exception:
                pass
        from app.config import DEFAULT_SETTINGS

        return copy.deepcopy(DEFAULT_SETTINGS)

    # ------------------------------------------------------------------ jobs

    async def _resolve(self, text: str) -> str:
        """'btc' -> 'BINANCE:BTCUSDT'; full symbols are checked by the hub too (invalid ones are refused)."""
        hub = getattr(self.rt, "hub", None)
        resolved = None
        if hub is not None:
            try:
                resolved = await asyncio.wait_for(hub.resolve(text), timeout=RESOLVE_TIMEOUT_S)
            except Exception:
                resolved = None
        elif ":" in text:
            resolved = text.upper()
        if not resolved:
            raise ValidationInputError(f"No encontré el símbolo '{text}'")
        return str(resolved).upper()

    async def run(self, symbols: Any = None, tfs: Any = None, bars: Any = None) -> dict[str, Any]:
        """Start a job; returns status(). Raises ValidationInputError (400), ValidationBusy (409) or RuntimeError
        when the market-data service is missing (503)."""
        async with self._lock:
            if self.running:
                raise ValidationBusy("Ya hay una validación en curso: espera a que termine")
            if getattr(self.rt, "hub", None) is None:
                raise RuntimeError("El servicio de datos no está disponible")
            d = self.defaults()
            req = normalize_request(symbols, tfs, bars, ValidationRequest(d["symbols"], d["tfs"], d["bars"]))
            resolved = []
            for s in req.symbols:
                sym = await self._resolve(s)
                if sym not in resolved:
                    resolved.append(sym)
            req.symbols = resolved
            self._seq += 1
            total = len(req.symbols) * len(req.tfs)
            self._job = {"id": self._seq, "state": "running", "request": req.to_dict(),
                         "total": total, "done": 0, "current": None, "message": "Validación iniciada",
                         "started_at": now_ms(), "finished_at": None, "errors": [], "error": None,
                         "summary": None}
            self._task = asyncio.create_task(self._run_job(req, self._job), name="validation-job")
        self._emit("info", f"Validación iniciada: {count_text(len(req.symbols), 'símbolo', 'símbolos')} × "
                           f"{count_text(len(req.tfs), 'temporalidad', 'temporalidades')} ({req.bars} velas)")
        return self.status()

    def _progress(self, job: dict[str, Any], update: dict[str, Any]) -> None:
        for key in ("done", "total", "current", "message"):
            if key in update:
                job[key] = update[key]
        if update.get("error"):
            job["errors"].append({"series": update.get("current"), "message": update["error"]})
            self._emit("warn", f"Validación: {update.get('message')}")
        elif update.get("series") is not None:
            self._emit("learn", str(update.get("message")))

    async def _run_job(self, req: ValidationRequest, job: dict[str, Any]) -> None:
        hub = self.rt.hub
        try:
            report = await run_validation(
                req, lambda s, tf, n: load_from_hub(hub, s, tf, n), settings=self._settings(), config=self.config(),
                progress=lambda u: self._progress(job, u), source="app")
            if not report.get("series"):  # nothing validated (no data, network down…): keep the last good report
                errors = report.get("errors") or []
                why = errors[0].get("message") if errors else "no hay series que validar"
                msg = f"No se validó ninguna serie ({why}); el último informe guardado no cambia"
                job.update(state="error", done=job["total"], current=None, finished_at=now_ms(), error=msg,
                           message=f"La validación falló: {msg}")
                self._emit("error", f"La validación falló: {msg}")
                return
            await self._save(report)
            summary = report.get("summary") or {}
            job.update(state="done", done=job["total"], current=None, finished_at=now_ms(), summary=summary,
                       message=f"Validación terminada: {count_text(len(report['series']), 'serie', 'series')} · "
                               f"{summary.get('label', '')}")
            self._emit("success" if summary.get("code") == "edge" else "learn",
                       f"Validación terminada · {summary.get('text', '')}")
        except asyncio.CancelledError:
            job.update(state="cancelled", finished_at=now_ms(), message="Validación cancelada")
            raise
        except Exception as exc:
            job.update(state="error", finished_at=now_ms(), error=f"{type(exc).__name__}: {exc}",
                       message=f"La validación falló: {exc}")
            self._emit("error", f"La validación falló: {exc}", {"trace": traceback.format_exc(limit=6)})

    # ------------------------------------------------------------------ persistence

    async def _save(self, report: dict[str, Any]) -> None:
        self._latest, self._loaded = report, True
        db = getattr(self.rt, "db", None)
        if db is None:
            return
        payload = json.dumps(clean_json(report), ensure_ascii=False, separators=(",", ":"))
        await db.execute(
            "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET state=excluded.state, updates=excluded.updates, "
            "trained_until=excluded.trained_until, updated_at=excluded.updated_at",
            (STATE_KIND, STATE_KEY, payload, len(report.get("series") or []), None,
             int(report.get("generated_at") or now_ms())),
        )

    async def latest(self) -> dict[str, Any] | None:
        """The last report (memory, else model_state); None when no validation has finished yet."""
        if self._loaded:
            return copy.deepcopy(self._latest)
        db = getattr(self.rt, "db", None)
        report = None
        if db is not None:
            row = await db.fetchone("SELECT state FROM model_state WHERE kind=? AND key=?", (STATE_KIND, STATE_KEY))
            if self._loaded:  # a job saved a newer report while the row was being read: that one wins
                return copy.deepcopy(self._latest)
            if row:
                try:
                    parsed = json.loads(row["state"])
                except (TypeError, ValueError):
                    parsed = None
                report = parsed if isinstance(parsed, dict) and isinstance(parsed.get("series"), list) else None
        self._latest, self._loaded = report, True
        return copy.deepcopy(report)

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        bus = getattr(self.rt, "bus", None)
        if bus is not None:
            try:
                bus.emit(level, "validation", message, data)
            except Exception:
                pass
