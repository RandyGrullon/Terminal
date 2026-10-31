"""Terminal commands of the validation feature: `validar [símbolos] [--tf=1h,4h] [--bars=3000]`, `validar estado`,
`validar resultado`."""
from __future__ import annotations

import time
from typing import Any

from app.sim.common import fmt_num, fmt_pct
from app.terminal import CommandContext, command
from app.validation.runner import ValidationInputError, split_list
from app.validation.service import ValidationBusy
from app.validation.verdict import ci_for, r2

STATUS_WORDS = {"estado", "status", "progreso"}
RESULT_WORDS = {"resultado", "resultados", "ultimo", "último", "informe", "reporte", "report"}


def _when(ms: Any) -> str:
    try:
        return time.strftime("%d/%m/%Y %H:%M UTC", time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def _status(ctx: CommandContext, svc: Any) -> None:
    st = svc.status()
    job = st.get("job")
    ctx.data = st
    if not job:
        ctx.print("No hay ninguna validación en curso ni terminada desde que arrancó el servidor. Inicia una con: "
                  "validar · el último informe guardado: validar resultado", "muted")
        return
    state = job.get("state")
    if state == "running":
        ctx.print(f"Validación en curso: {job.get('done', 0)} de {job.get('total', 0)} series · "
                  f"{job.get('message') or ''}", "info")
    elif state == "done":
        ctx.print(f"Última validación terminada ({_when(job.get('finished_at'))}): {job.get('message') or ''}",
                  "success")
    elif state == "error":
        ctx.print(f"La última validación falló: {job.get('error') or job.get('message')}", "error")
    else:
        ctx.print(f"Última validación: {job.get('message') or state}", "muted")
    for err in job.get("errors") or []:
        ctx.print(f"  {err.get('series')}: {err.get('message')}", "warn")


async def _result(ctx: CommandContext, svc: Any) -> None:
    rep = await svc.latest()
    if not rep:
        ctx.print("Aún no hay ninguna validación terminada. Inicia una con: validar", "muted")
        return
    series = rep.get("series") or []
    req = rep.get("request") or {}
    ctx.print(f"Validación walk-forward del {_when(rep.get('generated_at'))} · {len(series)} series · "
              f"{req.get('bars', '—')} velas por serie", "system")
    rows = []
    for s in series:
        o, rnd, bh = s["overall"]["nexus"], s["overall"]["random"], s["overall"]["buy_hold"]
        pct = rnd.get("percentile")
        rows.append([f"{s['symbol']} {s['tf']}", f"{o.get('wins', 0)} de {o.get('trades', 0)}",
                     r2(o.get("expectancy_r")), ci_for(o.get("ci95"), o.get("trades")), r2(rnd.get("expectancy_r")),
                     "—" if pct is None else fmt_num(pct, 0), fmt_pct(bh.get("return_pct")), s["verdict"]["label"]])
    ctx.table(["Serie", "Ganadas", "Expectativa", "IC 95 %", "Azar", "Percentil", "Comprar y mantener",
               "Veredicto"], rows, max_rows=30)
    for s in series:  # where the bars came from when it matters (synthetic market, equivalent pair, saved data…)
        if s.get("note"):
            ctx.print(f"Datos de {s['symbol']} {s['tf']}: {s['note']}", "warn")
    for err in rep.get("errors") or []:
        ctx.print(f"Sin validar {err.get('symbol')} {err.get('tf')}: {err.get('message')}", "warn")
    for g in rep.get("groups") or []:
        ctx.print(f"{g['label']} ({g['trades']} operaciones, {g['assets']} activos): {g['verdict']['text']}",
                  "success" if g["verdict"]["code"] == "edge" else "info")
    summary = rep.get("summary") or {}
    if summary.get("text"):
        ctx.print(summary["text"], "success" if summary.get("code") == "edge" else "warn")
    ctx.print(rep.get("disclaimer") or "", "muted")
    ctx.data = {"summary": summary, "series": len(series), "generated_at": rep.get("generated_at")}


@command("validar", help="Validación walk-forward de NEXUS + filtro frente al azar y a comprar y mantener",
         usage="validar [símbolos] [--tf=1h,4h] [--bars=3000] · validar estado · validar resultado",
         group="Aprendizaje", aliases=("validate", "validacion", "validación"))
async def _validar(ctx: CommandContext) -> None:
    svc = getattr(ctx.rt, "validation", None)
    if svc is None:
        ctx.fail("La validación no está disponible (el módulo no se cargó)")
    first = str(ctx.arg(0) or "").lower()
    if first in STATUS_WORDS:
        _status(ctx, svc)
        return
    if first in RESULT_WORDS:
        await _result(ctx, svc)
        return
    tfs = ctx.flag("tf", ctx.flag("tfs"))
    try:
        st = await svc.run(split_list(ctx.args) or None, tfs if isinstance(tfs, str) else None,
                           ctx.flag_int("bars", ctx.flag_int("velas")))
    except ValidationInputError as exc:
        ctx.fail(str(exc))
    except ValidationBusy as exc:
        ctx.fail(f"{exc}. Consulta el progreso con: validar estado")
    except RuntimeError as exc:
        ctx.fail(str(exc))
    req = (st.get("job") or {}).get("request") or {}
    ctx.print(f"Validación iniciada: {', '.join(req.get('symbols') or [])} · {', '.join(req.get('tfs') or [])} · "
              f"{req.get('bars')} velas por serie", "success")
    ctx.print("Corre en segundo plano y avisa con eventos. Consulta: validar estado · validar resultado (el informe "
              "también aparece en Aprendizaje).", "muted")
    ctx.data = st
