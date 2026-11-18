"""Strategy research on long history (`rt.research`): imports historical candles (app.data.importer: a QuantDataManager
or other CSV from data/import, or Binance's official public dumps) and runs every saved PyChart strategy plus NEXUS
itself (with and without its gate) over up to RESEARCH_BARS candles of one market and timeframe.

Each strategy is judged as the copilot's lab judges a script (app.ai.lab): its closed trades split by the candles
into a development period (the first 70 %) and a check period (the rest), each compared with random entries in the
same period with the same direction, the same duration and the same costs, with a 95 % interval by a block bootstrap
by day; nothing is concluded below 30 trades nor when the interval includes 0. A strategy is a «candidata» only when
it is above random entries in BOTH periods; even then the report says to confirm it on another market or timeframe
(each strategy tried is another chance to look good by luck). Never a promise, never financial advice.

One job at a time (an import or a research), in the background, with its progress; the last KEEP_REPORTS research
reports are kept in table `meta` (key research.reports), no schema change.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import numpy as np

from app import config
from app.ai import lab
from app.data.importer import (
    BINANCE_TFS, IMPORT_SOURCE, ImportError_, count_imported, import_binance, import_csv, resolve_import_path,
)
from app.sim.common import json_dumps, json_loads
from app.util import normalize_tf, now_ms

RESEARCH_BARS = 20_000  # the PyChart sandbox's MAX_BARS: the newest candles are used
MIN_BARS = 1_000
SCRIPT_TIMEOUT_S = 240.0
REPORTS_KEY = "research.reports"
KEEP_REPORTS = 20
NO_EDGE = ("Ninguna estrategia supera a entrar al azar en los dos periodos (desarrollo y comprobación). Es lo "
           "esperable: la validación publicada de NEXUS tampoco encontró ventaja.")
CANDIDATE = ("{names}: por encima de entrar al azar en desarrollo y en comprobación. Antes de usarla, confírmala en "
             "otro mercado o temporalidad: con {n} estrategias probadas, alguna puede salir bien por suerte.")


class ResearchError(Exception):
    """A Spanish message for the user (another job running, no candles, bad symbol…)."""


def _iso(ts: Any) -> str | None:
    return lab._iso(ts)


class ResearchService:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self._task: asyncio.Task | None = None
        self.job: dict[str, Any] | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def status(self) -> dict:
        job = self.job or {}
        return {"running": self.busy(), "job": job.get("kind"), "state": job.get("state")}

    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def _emit(self, level: str, message: str) -> None:
        bus = getattr(self.rt, "bus", None)
        if bus is not None:
            bus.emit(level, "research", message)

    # ------------------------------------------------------------------ jobs

    def _launch(self, kind: str, title: str, coro_fn) -> dict:
        if self.busy():
            raise ResearchError(f"Ya hay un trabajo en marcha: {self.job.get('title') if self.job else ''}. Espera a "
                                "que termine (`investigar estado`).")
        job: dict[str, Any] = {"kind": kind, "title": title, "state": "running", "progress": [], "started_ms": now_ms(),
                               "finished_ms": None, "error": None, "result": None}
        self.job = job

        def progress(text: str) -> None:
            job["progress"] = (job["progress"] + [text])[-12:]

        async def runner() -> None:
            try:
                job["result"] = await coro_fn(progress)
                job["state"] = "done"
                self._emit("info", f"Investigación: {title} terminado")
            except (ResearchError, ImportError_) as exc:
                job.update({"state": "error", "error": str(exc)})
                self._emit("warn", f"Investigación: {title}: {exc}")
            except asyncio.CancelledError:
                job.update({"state": "error", "error": "Cancelado (el servidor se detuvo)."})
                raise
            except Exception as exc:  # noqa: BLE001 - reported to the user, never kills the server
                job.update({"state": "error", "error": f"Error interno: {type(exc).__name__}: {exc}"})
                self._emit("error", f"Investigación: {title}: {type(exc).__name__}: {exc}")
            finally:
                job["finished_ms"] = now_ms()

        self._task = asyncio.create_task(runner(), name=f"research-{kind}")
        return self.snapshot()

    def snapshot(self) -> dict:
        job = dict(self.job) if self.job else None
        return {"busy": self.busy(), "job": job}

    async def wait(self) -> dict:
        """For tests and the CLI: until the current job ends, then its snapshot."""
        if self._task is not None:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        return self.snapshot()

    def start_import_csv(self, name: str, symbol: str, *, tz_offset_h: float = 0.0) -> dict:
        path = resolve_import_path(name)
        sym = str(symbol or "").strip().upper()

        async def work(progress) -> dict:
            return await import_csv(self.rt.db, path, sym, tz_offset_h=tz_offset_h, progress=progress)

        return self._launch("import_csv", f"importar {path.name} como {sym}", work)

    def start_import_binance(self, symbols: list[str], *, months: int = 36, tfs: list[str] | None = None) -> dict:
        if config.OFFLINE:
            raise ResearchError("Modo sin conexión (NEXUS_OFFLINE): no se descarga nada.")
        syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not syms:
            raise ResearchError("Faltan los símbolos. Ejemplo: importar binance BINANCE:BTCUSDT BINANCE:ETHUSDT.P")
        months = int(min(max(int(months), 1), 120))
        wanted = list(tfs or BINANCE_TFS)

        async def work(progress) -> dict:
            out = []
            for s in syms:
                out.append(await import_binance(self.rt.db, s, months=months, tfs=wanted, progress=progress))
            return {"imports": out}

        return self._launch("import_binance", f"descargar {', '.join(syms)} ({months} meses) de Binance", work)

    def start_research(self, symbol: str, tf: str, *, bars: int = RESEARCH_BARS, nexus: bool = True,
                       script_ids: list[int] | None = None) -> dict:
        sym = str(symbol or "").strip().upper()
        if ":" not in sym:
            raise ResearchError("El símbolo debe llevar su mercado, por ejemplo BINANCE:BTCUSDT.")
        try:
            tf_key = normalize_tf(tf)
        except ValueError as exc:
            raise ResearchError(str(exc)) from None
        n = int(min(max(int(bars), MIN_BARS), RESEARCH_BARS))

        async def work(progress) -> dict:
            report = await self.research(sym, tf_key, bars=n, nexus=nexus, script_ids=script_ids, progress=progress)
            await self._save(report)
            return report

        return self._launch("research", f"investigar {sym} {tf_key}", work)

    # ------------------------------------------------------------------ research

    async def _candles(self, symbol: str, tf: str, bars: int) -> list[dict]:
        rows = await self.rt.db.fetchall(
            "SELECT time, open, high, low, close, volume FROM candles WHERE symbol = ? AND tf = ? "
            "ORDER BY time DESC LIMIT ?", (symbol, tf, int(bars)))
        return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                 "close": float(r["close"]), "volume": float(r["volume"] or 0.0)} for r in reversed(rows)]

    async def _strategies(self, script_ids: list[int] | None) -> list[dict]:
        scripts = getattr(self.rt, "scripts", None)
        if scripts is None:
            return []
        out = []
        for s in await scripts.list_scripts():
            if script_ids and int(s.get("id")) not in script_ids:
                continue
            full = await scripts.get(s["id"])
            comp = await scripts.compile(full.get("code"))
            if comp.get("ok") and (comp.get("meta") or {}).get("kind") == "strategy":
                out.append({"id": int(full["id"]), "name": str(full.get("name") or f"Script {full['id']}"),
                            "code": full.get("code")})
        return out

    @staticmethod
    def _verdicts(periods: list[dict]) -> tuple[str | None, str | None]:
        keys = [p.get("veredicto_clave") for p in periods]
        return (keys[0] if keys else None), (keys[1] if len(keys) > 1 else None)

    async def _nexus(self, symbol: str, tf: str, candles: list[dict], *, gate: bool, seed: int) -> dict:
        from app.data.base import CandleSet
        from app.sim.backtest import run_backtest

        arr = {k: np.array([c[k] for c in candles], dtype=float) for k in ("open", "high", "low", "close", "volume")}
        times = np.array([c["time"] for c in candles], dtype=np.int64)
        cs = CandleSet(symbol, tf, times, arr["open"], arr["high"], arr["low"], arr["close"], arr["volume"],
                       provider=IMPORT_SOURCE)
        res = await asyncio.to_thread(run_backtest, cs, None, use_gate=gate, compare_ungated=False,
                                      include_candles=False)
        params = res.get("params") or {}
        fee = float(params.get("fee_pct") or 0.0)
        slip = float(params.get("slippage_pct") or 0.0)
        closed = [t for t in res.get("trades") or [] if t.get("exit_time") is not None
                  and t.get("exit_reason") != "OPEN" and t.get("pnl_pct") is not None]
        rows = [(int(t["entry_time"]), -1 if t.get("side") == "short" else 1, max(int(t.get("bars") or 1), 1),
                 float(t["pnl_pct"])) for t in closed]
        n = len(candles)
        split = min(max(int(n * lab.DEV_SHARE), 1), n - 1)
        split_time = int(times[split])
        cost = 2.0 * (fee + slip)
        periods = [
            lab.period_summary("desarrollo (primer 70 % de las velas)", [r for r in rows if r[0] < split_time], times,
                               arr["open"], arr["close"], 0, split, cost_pct=cost, seed=seed),
            lab.period_summary("comprobación (último 30 % de las velas)", [r for r in rows if r[0] >= split_time],
                               times, arr["open"], arr["close"], split, n, cost_pct=cost, seed=seed + 7),
        ]
        wins = sum(1 for r in rows if r[3] > 0)
        trial = lab.trial_from_trades(closed, split_time, idea=f"nexus:{'filtro' if gate else 'sin_filtro'}",
                                      variant=f"nexus:{'filtro' if gate else 'sin_filtro'}",
                                      label="NEXUS con su filtro" if gate else "NEXUS sin filtro", symbol=symbol, tf=tf)
        return {"_trial": trial, "ok": True, "total": {"operaciones": len(rows), "ganadoras": f"{wins} de {len(rows)}",
                                      "rentabilidad_pct": lab._r((res.get("metrics") or {}).get("total_return_pct"))},
                "periodos": periods, "costes": {"comision_pct_por_lado": lab._r(fee, 4),
                                                "deslizamiento_pct": lab._r(slip, 4)}}

    async def research(self, symbol: str, tf: str, *, bars: int = RESEARCH_BARS, nexus: bool = True,
                       script_ids: list[int] | None = None, progress=None) -> dict:
        say = progress or (lambda _t: None)
        candles = await self._candles(symbol, tf, bars)
        if len(candles) < MIN_BARS:
            raise ResearchError(f"Solo hay {len(candles)} velas de {symbol} {tf} guardadas: hacen falta al menos "
                                f"{MIN_BARS}. Importa historial primero (`importar binance …` o `importar archivo …`).")
        say(f"{len(candles)} velas de {symbol} {tf} ({_iso(candles[0]['time'])} → {_iso(candles[-1]['time'])})")
        results: list[dict] = []
        strategies = await self._strategies(script_ids)
        for i, s in enumerate(strategies):
            say(f"Estrategia {i + 1} de {len(strategies)}: {s['name']}")
            started = time.perf_counter()
            run = await self.rt.scripts.run_on(s["code"], candles, symbol, tf, timeout=SCRIPT_TIMEOUT_S)
            summary = lab.summarize_run(run, symbol=symbol, tf=tf, seed=1000 + i)
            entry = {"nombre": s["name"], "fuente": "pychart", "script_id": s["id"],
                     "segundos": round(time.perf_counter() - started, 1), **summary,
                     "_trial": lab.trial_of(run, code=s["code"], symbol=symbol, tf=tf) if summary.get("ok") else None}
            results.append(entry)
        if nexus:
            for gate in (True, False):
                name = "NEXUS con su filtro" if gate else "NEXUS sin filtro"
                say(f"{name}…")
                started = time.perf_counter()
                try:
                    entry = await self._nexus(symbol, tf, candles, gate=gate, seed=77 if gate else 99)
                except Exception as exc:  # noqa: BLE001 - one strategy failing never stops the research
                    entry = {"ok": False, "errores": [{"mensaje": f"{type(exc).__name__}: {exc}"}]}
                results.append({"nombre": name, "fuente": "nexus", "segundos": round(time.perf_counter() - started, 1),
                                **entry})
        # how many strategies were tried in this research: each one's deflated Sharpe against all of them
        trials = [r["_trial"] for r in results if r.get("_trial")]
        for r in results:
            trial = r.pop("_trial", None)
            if trial is not None:
                r["multiplicidad"] = lab.multiplicity(trial, trials)
        for r in results:
            dev, chk = self._verdicts(r.get("periodos") or [])
            dsr = r.get("multiplicidad") or {}
            # with a computable DSR, a candidate also has to pass it (DSR ≥ 0.5)
            r["candidata"] = (bool(r.get("ok")) and dev == "above" and chk == "above"
                              and (dsr.get("dsr") is None or bool(dsr.get("pasa_dsr"))))
        candidates = [r["nombre"] for r in results if r["candidata"]]
        tried = sum(1 for r in results if r.get("ok"))
        conclusion = (CANDIDATE.format(names=", ".join(candidates), n=tried) if candidates else NO_EDGE)
        return {"simbolo": symbol, "temporalidad": tf, "velas": len(candles), "desde": _iso(candles[0]["time"]),
                "hasta": _iso(candles[-1]["time"]), "creado_ms": now_ms(), "estrategias": results,
                "candidatas": candidates, "conclusion": conclusion, "referencia": lab.REFERENCE_NOTE, "nota": lab.NOTE}

    # ------------------------------------------------------------------ reports

    async def reports(self) -> list[dict]:
        db = getattr(self.rt, "db", None)
        if db is None:
            return []
        try:
            raw = await db.fetchval("SELECT value FROM meta WHERE key=?", (REPORTS_KEY,))
        except Exception:
            return []
        data = json_loads(raw, [])
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def _save(self, report: dict) -> None:
        items = [report, *[r for r in await self.reports()
                           if (r.get("simbolo"), r.get("temporalidad")) != (report["simbolo"], report["temporalidad"])]]
        await self.rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                 (REPORTS_KEY, json_dumps(items[:KEEP_REPORTS])))

    async def imported(self, symbol: str) -> dict:
        return await count_imported(self.rt.db, str(symbol or "").strip().upper())
