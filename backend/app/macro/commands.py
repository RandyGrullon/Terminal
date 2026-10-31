"""Terminal commands of the semáforo de mercado (group "Mercado"): `macro [símbolo]` and `calendario [días]`."""
from __future__ import annotations

from app.macro import fmt
from app.macro.official import SOURCES as OFFICIAL_LIVE
from app.macro.schedules import VERIFIED_LABEL
from app.macro.scope import describe
from app.runtime import rt
from app.terminal import CommandContext, command

LEVEL_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}
LINE_LEVEL = {"green": "success", "amber": "warn", "red": "error"}
IMPACT_ES = {"high": "Alto", "medium": "Medio", "low": "Bajo", "holiday": "Festivo"}
METRIC_MARK = {"ok": "", "warn": " ▲", "danger": " ‼"}
SOURCE_TAG = {"forexfactory": "FF", "federalreserve": "Fed", "bls": "BLS", "bea": "BEA", "ecb": "BCE", "boe": "BoE",
              "boj": "BoJ"}
OFFICIAL_NAMES = "Fed, BLS, BEA, BCE, BoE y BoJ"


def _service(ctx: CommandContext):
    if rt.macro is None:
        ctx.fail("El semáforo de mercado no está disponible (el módulo macro no se cargó).")
    return rt.macro


def _tz() -> str | None:
    return rt.settings.get(fmt.TZ_SETTING) if rt.settings is not None else None


def _event_rows(events: list[dict], now: int, tz: str | None, extra: bool = False) -> list[list[str]]:
    """Rows with the (long) Spanish title last, so it is never cut. `extra` adds forecast, previous and source."""
    rows = []
    for ev in events:
        row = [fmt.day_clock(ev["time"], now, tz), fmt.until(ev["countdown_s"]), ev["currency"],
               IMPACT_ES.get(ev["impact"], ev["impact"])]
        if extra:
            source = str(ev.get("source") or "")
            row += [ev.get("forecast") or "-", ev.get("previous") or "-", SOURCE_TAG.get(source, source or "-")]
        rows.append(row + [ev["title_es"]])
    return rows


def _events_table(ctx: CommandContext, headers: list[str], rows: list[list[str]], max_rows: int = 50) -> None:
    """Like ctx.table, but the last column (event title) keeps its full text."""
    widths = [len(h) for h in headers[:-1]]
    for row in rows[:max_rows]:
        for i, v in enumerate(row[:-1]):
            widths[i] = max(widths[i], len(v))

    def line(row: list[str]) -> str:
        return "  ".join([v.ljust(widths[i]) for i, v in enumerate(row[:-1])] + [row[-1]])

    ctx.print(line(headers), "muted")
    ctx.print("  ".join("─" * w for w in widths + [len(headers[-1])]), "muted")
    for row in rows[:max_rows]:
        ctx.print(line(row))
    if len(rows) > max_rows:
        ctx.print(f"… {len(rows) - max_rows} eventos más", "muted")


@command("macro", help="Semáforo de mercado: ¿opero ahora o no? (Fed, datos, volatilidad, futuros)",
         usage="macro [símbolo]", group="Mercado", aliases=("mercado", "semaforo", "semáforo"))
async def _macro(ctx: CommandContext) -> None:
    svc = _service(ctx)
    text = ctx.arg(0)
    symbol = await ctx.symbol(text) if text else None
    cond = await svc.conditions(symbol)
    now, tz = svc.now(), _tz()
    level = cond["level"]
    where = symbol or "global"
    ctx.print(f"{LEVEL_DOT[level]} {cond['label'].upper()} · {where}", LINE_LEVEL[level])
    ctx.print(cond["headline"], LINE_LEVEL[level])
    ctx.print(f"→ {cond['advice']}", "info")
    bo = cond["blackout"]
    if bo["active"] and bo["until"]:
        ctx.print(f"Ventana sin operar hasta las {fmt.clock(bo['until'], tz)} ({fmt.until(bo['until'] - now)})"
                  f"{' · ' + bo['event'] if bo['event'] else ''}", "warn")
    for reason in cond["reasons"]:
        ctx.print(f"  • {reason}", "muted")
    if cond["metrics"]:
        ctx.print("")
        rows = [[m["label"] + METRIC_MARK.get(m["level"], ""), m["display"], m["change_display"], m["note"]]
                for m in cond["metrics"]]
        ctx.table(["Indicador", "Valor", "Cambio", "Lectura"], rows)
    if cond["events"]:
        ctx.print("")
        _events_table(ctx, [f"Cuándo ({fmt.tz_label(tz)})", "Falta", "Divisa", "Impacto", "Evento"],
                      _event_rows(cond["events"], now, tz))
    elif cond["scope"] != "sim":
        ctx.print("Sin eventos de alto impacto próximos para este mercado.", "muted")
    if cond["stale"]:
        ctx.print("⚠ Datos desactualizados: alguna fuente no respondió; confirma antes de operar.", "warn")
    scope = dict(await svc.scope(symbol))
    if not scope["currencies"] and not scope["sim"]:
        scope["currencies"] = _currencies()
    ctx.print(f"Ámbito: {describe(scope)} · actualizado {fmt.clock(cond['updated_at'] / 1000, tz)}", "muted")
    ctx.data = cond


def _currencies() -> list[str]:
    configured = rt.settings.get("macro.currencies") if rt.settings is not None else None
    return [str(c).upper() for c in (configured or ["USD"])]


@command("calendario", help="Calendario económico: próximos datos (Fed, IPC, empleo...) con cuenta atrás",
         usage="calendario [días] [--all] [--divisa=EUR,USD]", group="Mercado", aliases=("agenda",))
async def _calendario(ctx: CommandContext) -> None:
    svc = _service(ctx)
    raw = ctx.arg(0, "7")
    try:
        days = int(str(raw))
    except ValueError:
        ctx.fail(f"Días no válidos: {raw!r}. Usa un número entre 1 y 30 (ejemplo: calendario 3).")
    if not 1 <= days <= 30:
        ctx.fail("Días fuera de rango: usa un número entre 1 y 30.")
    show_all = bool(ctx.flag("all", False))
    cur_flag = ctx.flag("divisa") or ctx.flag("currency")
    if cur_flag and cur_flag is not True:
        currencies = [c.strip().upper() for c in str(cur_flag).split(",") if c.strip()]
    elif show_all:
        currencies = None
    else:
        currencies = _currencies()
    impacts = None if show_all else ("high", "medium")
    events = await svc.calendar(days=days, impacts=impacts, currencies=currencies)
    now, tz = svc.now(), _tz()
    scope_txt = "todas las divisas" if not currencies else ", ".join(currencies)
    level_txt = "todos los impactos" if show_all else "impacto alto y medio"
    ctx.print(f"Calendario económico · próximos {days} días · {scope_txt} · {level_txt}", "system")
    if not events:
        ctx.print("No hay eventos en ese periodo.", "muted")
    else:
        _events_table(ctx, [f"Cuándo ({fmt.tz_label(tz)})", "Falta", "Divisa", "Impacto", "Previsión", "Anterior",
                            "Fuente", "Evento"], _event_rows(events, now, tz, extra=True), max_rows=80)
    cache = svc.cache
    week_end = cache.get("week_end") if cache else None
    if svc.calendar_stale(now):
        ctx.print(f"⚠ Calendario semanal sin actualizar: solo se muestran las fechas oficiales ({OFFICIAL_NAMES}), "
                  "sin previsiones ni datos de impacto medio de otras fuentes.", "warn")
    elif week_end and now + days * 86400 > week_end:
        ctx.print(f"Los datos semanales (FF) llegan hasta el {fmt.date_label(week_end - 60, tz)}; después solo se "
                  f"muestran las fechas oficiales ({OFFICIAL_NAMES}), sin previsiones.", "muted")
    coverage = svc.coverage(now) if hasattr(svc, "coverage") else None
    for row in (coverage or {}).get("sources") or []:
        if row.get("live") and row.get("error") and row.get("id") in OFFICIAL_LIVE:
            ctx.print(f"⚠ {row['label']}: el calendario oficial no respondió ({row['error']}); se usan las fechas "
                      f"guardadas o las verificadas el {VERIFIED_LABEL}.", "warn")
    ctx.data = {"events": events, "coverage": coverage}
