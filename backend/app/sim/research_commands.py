"""Terminal commands of strategy research (app.sim.research, group «Investigación»):

- `importar binance <símbolos…> [--meses=36] [--tf=5m,15m,1h,4h,1d]`: Binance's official public dumps.
- `importar archivo <nombre.csv> <símbolo> [--tz=0]`: a CSV from data/import (QuantDataManager, MetaTrader…).
- `importar` alone: the files waiting in data/import and how to use them; `importar <símbolo>`: what is stored.
- `investigar <símbolo> <tf> [--velas=20000] [--sin-nexus]`, `investigar estado`, `investigar resultado [símbolo tf]`.
"""
from __future__ import annotations

from typing import Any

from app.data.importer import IMPORT_DIR, ImportError_
from app.sim.research import RESEARCH_BARS, ResearchError
from app.terminal import CommandContext, command
from app.util import normalize_tf

STATUS_WORDS = {"estado", "status", "progreso"}
RESULT_WORDS = {"resultado", "resultados", "informe", "último", "ultimo"}
IMPORT_USAGE = ("importar binance <símbolos…> [--meses=36] [--tf=5m,15m,1h,4h,1d] · importar archivo <nombre.csv> "
                "<símbolo> [--tz=0] · importar <símbolo> · importar estado")
RESEARCH_USAGE = "investigar <símbolo> <temporalidad> [--velas=20000] [--sin-nexus] · investigar estado · investigar resultado"


def _service(ctx: CommandContext):
    svc = getattr(ctx.rt, "research", None)
    if svc is None:
        ctx.fail("El módulo de investigación no está cargado.")
    return svc


def _when(iso: Any) -> str:
    return str(iso or "—").replace(" UTC", "")


def _job(ctx: CommandContext, svc: Any) -> None:
    snap = svc.snapshot()
    ctx.data = snap
    job = snap.get("job")
    if not job:
        ctx.print("No hay ningún trabajo de importación ni de investigación desde que arrancó el servidor.", "muted")
        return
    state = {"running": "en curso", "done": "terminado", "error": "con error"}.get(job.get("state"), job.get("state"))
    ctx.print(f"{job.get('title')}: {state}", "error" if job.get("state") == "error" else "info")
    for line in job.get("progress") or []:
        ctx.print(f"  {line}", "muted")
    if job.get("error"):
        ctx.print(job["error"], "error")


def _period(p: dict | None) -> str:
    if not p:
        return "—"
    n = p.get("operaciones", 0)
    if not n:
        return "sin operaciones"
    diff = p.get("diferencia_con_azar_pct")
    return f"{p.get('veredicto')} · {n} ops · {'—' if diff is None else f'{diff:+.3f} %'.replace('.', ',')} vs azar"


def _dsr(m: dict | None) -> str:
    """'0,12 (no pasa)' / '0,61 (pasa)' / '—' (not computable: too few trades or strategies)."""
    if not isinstance(m, dict) or m.get("dsr") is None:
        return "—"
    return f"{m['dsr']:.2f} ({'pasa' if m.get('pasa_dsr') else 'no pasa'})".replace(".", ",")


def _report(ctx: CommandContext, rep: dict) -> None:
    ctx.print(f"Investigación de {rep['simbolo']} {rep['temporalidad']}: {rep['velas']} velas "
              f"({_when(rep.get('desde'))} → {_when(rep.get('hasta'))})", "system")
    rows = []
    for r in rep.get("estrategias") or []:
        if not r.get("ok"):
            err = (r.get("errores") or [{}])[0].get("mensaje", "falló")
            rows.append([r["nombre"], "—", f"falló: {str(err)[:60]}", "—", "—", ""])
            continue
        per = r.get("periodos") or [None, None]
        rows.append([r["nombre"], (r.get("total") or {}).get("ganadoras", "—"), _period(per[0]),
                     _period(per[1] if len(per) > 1 else None), _dsr(r.get("multiplicidad")),
                     "candidata" if r.get("candidata") else ""])
    ctx.table(["Estrategia", "Ganadoras", "Desarrollo (70 %)", "Comprobación (30 %)", "DSR", ""], rows)
    ctx.print(rep.get("conclusion") or "", "success" if rep.get("candidatas") else "info")
    ctx.print(rep.get("nota") or "", "muted")


@command("importar", help="Historial largo para investigar estrategias: volcados oficiales de Binance o un CSV "
                          "(QuantDataManager, MetaTrader…) de la carpeta data/import",
         usage=IMPORT_USAGE, group="Investigación", aliases=("import",))
async def _importar(ctx: CommandContext) -> None:
    svc = _service(ctx)
    words = [str(a) for a in ctx.args]
    try:
        if not words:
            files = sorted(p.name for p in IMPORT_DIR.glob("*.csv")) if IMPORT_DIR.is_dir() else []
            ctx.print(f"Carpeta de importación: {IMPORT_DIR}", "system")
            ctx.print("Archivos CSV esperando: " + (", ".join(files[:20]) if files else "ninguno"), "info")
            ctx.print(f"Uso: {IMPORT_USAGE}", "muted")
            ctx.data = {"dir": str(IMPORT_DIR), "files": files}
            return
        head = words[0].lower()
        if head in STATUS_WORDS:
            _job(ctx, svc)
            return
        if head == "binance":
            symbols = [await ctx.symbol(s) for s in words[1:]]
            months = ctx.flag_int("meses", 36) or 36
            tfs = [normalize_tf(t) for t in str(ctx.flag("tf") or "5m,15m,1h,4h,1d").split(",") if t.strip()]
            snap = svc.start_import_binance(symbols, months=months, tfs=tfs)
        elif head in ("archivo", "file", "csv"):
            if len(words) < 3:
                ctx.fail(f"Uso: {IMPORT_USAGE}")
            symbol = words[2].upper() if ":" in words[2] else await ctx.symbol(words[2])
            snap = svc.start_import_csv(words[1], symbol, tz_offset_h=ctx.flag_float("tz", 0.0) or 0.0)
        else:
            symbol = await ctx.symbol(words[0])
            stored = await svc.imported(symbol)
            ctx.data = {"symbol": symbol, "tfs": stored}
            if not stored:
                ctx.print(f"No hay historial importado de {symbol}.", "muted")
                return
            ctx.table(["Temporalidad", "Velas", "Desde", "Hasta"],
                      [[tf, v["bars"], _when(_iso(v["from"])), _when(_iso(v["to"]))] for tf, v in sorted(stored.items())])
            return
    except (ResearchError, ImportError_, ValueError) as exc:
        ctx.fail(str(exc))
        return
    ctx.data = snap
    ctx.print(f"En marcha: {(snap.get('job') or {}).get('title')}. Mira el progreso con: importar estado", "info")


def _iso(ts: Any) -> str | None:
    from app.ai.lab import _iso as iso

    return iso(ts)


@command("investigar", help="Prueba todas las estrategias de PyChart y NEXUS sobre el historial largo de un mercado: "
                            "desarrollo y comprobación frente a entrar al azar",
         usage=RESEARCH_USAGE, group="Investigación", aliases=("research",))
async def _investigar(ctx: CommandContext) -> None:
    svc = _service(ctx)
    words = [str(a) for a in ctx.args]
    if not words:
        ctx.fail(f"Uso: {RESEARCH_USAGE}")
    head = words[0].lower()
    if head in STATUS_WORDS:
        _job(ctx, svc)
        return
    if head in RESULT_WORDS:
        reports = await svc.reports()
        if len(words) >= 3:
            sym = words[1].upper() if ":" in words[1] else await ctx.symbol(words[1])
            reports = [r for r in reports if r.get("simbolo") == sym and r.get("temporalidad") == normalize_tf(words[2])]
        if not reports:
            ctx.print("Aún no hay ninguna investigación terminada. Inicia una con: investigar btc 1h", "muted")
            return
        ctx.data = reports[0]
        _report(ctx, reports[0])
        return
    if len(words) < 2:
        ctx.fail(f"Uso: {RESEARCH_USAGE}")
    try:
        symbol = words[0].upper() if ":" in words[0] else await ctx.symbol(words[0])
        snap = svc.start_research(symbol, words[1], bars=ctx.flag_int("velas", RESEARCH_BARS) or RESEARCH_BARS,
                                  nexus=not bool(ctx.flag("sin-nexus")))
    except (ResearchError, ImportError_, ValueError) as exc:
        ctx.fail(str(exc))
        return
    ctx.data = snap
    ctx.print(f"En marcha: {(snap.get('job') or {}).get('title')}. Tarda unos minutos; mira el progreso con: "
              "investigar estado · el informe: investigar resultado", "info")
