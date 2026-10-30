"""Terminal commands for PyChart (group "PyChart"): scripts, script, compile, run."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.runtime import rt
from app.scripting.service import (
    ScriptServiceError,
    fmt_pct,
    normalize_name,
    strategy_metrics,
    win_rate_pct,
)
from app.terminal import CommandContext, command
from app.util import normalize_tf

GROUP = "PyChart"
CODE_HEAD_LINES = 25
RESERVED_FLAGS = {"limit", "help"}


def _svc(ctx: CommandContext) -> Any:
    svc = getattr(rt, "scripts", None)
    if svc is None:
        ctx.fail("El servicio PyChart no está disponible (el módulo no se cargó)")
    return svc


async def _guard(ctx: CommandContext, coro: Any) -> Any:
    try:
        return await coro
    except ScriptServiceError as exc:
        ctx.fail(exc.detail)


def _fmt_num(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    if abs(v) >= 100:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _fmt_time(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _fmt_ms(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _last_value(data: Any) -> Any:
    """Value of a plot on the LAST bar (None when the plot is empty there, e.g. a split up/down series)."""
    return data[-1] if isinstance(data, list) and data else None


def _parse_value(raw: Any) -> Any:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip()
    low = text.lower()
    if low in ("true", "si", "sí", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


# ----------------------------------------------------------------------------- scripts / script


@command("scripts", help="Lista los scripts PyChart (tuyos y ejemplos)", usage="scripts [texto]", group=GROUP,
         aliases=("pychart",))
async def _scripts(ctx: CommandContext) -> None:
    metas = await _guard(ctx, _svc(ctx).list_scripts())
    needle = normalize_name(" ".join(ctx.args)) if ctx.args else ""
    if needle:
        metas = [m for m in metas if needle in normalize_name(m["name"])]
    if not metas:
        ctx.print("No hay scripts que coincidan." if needle else "Todavía no hay scripts.", "muted")
        ctx.data = []
        return
    own = sum(1 for m in metas if not m["is_example"])
    ctx.print(f"Scripts PyChart: {own} tuyos · {len(metas) - own} ejemplos", "system")
    ctx.table(
        ["ID", "Nombre", "Tipo", "Actualizado"],
        [[m["id"], m["name"], "ejemplo" if m["is_example"] else "propio", _fmt_ms(m["updated_at"])] for m in metas],
        max_rows=100,
    )
    ctx.print("Usa: script <nombre|id> · compile <nombre|id> · run <nombre|id> <símbolo> [tf]", "muted")
    ctx.data = metas


@command("script", help="Muestra un script PyChart (descripción y primeras líneas)", usage="script <nombre|id>",
         group=GROUP, aliases=("codigo", "código"))
async def _script(ctx: CommandContext) -> None:
    if not ctx.args:
        ctx.fail("Falta el nombre o id del script. Ejemplo: script \"Cruce de EMAs (estrategia)\"")
    sc = await _guard(ctx, _svc(ctx).find(" ".join(ctx.args)))
    kind = "ejemplo (solo lectura: guarda una copia para editarlo)" if sc["is_example"] else "propio"
    ctx.print(f"[{sc['id']}] {sc['name']} · {kind} · actualizado {_fmt_ms(sc['updated_at'])}", "system")
    if sc["description"]:
        ctx.print(sc["description"], "info")
    lines = sc["code"].splitlines()
    width = len(str(min(len(lines), CODE_HEAD_LINES)))
    for i, line in enumerate(lines[:CODE_HEAD_LINES], start=1):
        ctx.print(f"{i:>{width}} │ {line}", "muted")
    if len(lines) > CODE_HEAD_LINES:
        ctx.print(f"… {len(lines) - CODE_HEAD_LINES} líneas más (ábrelo en Studio para verlo completo)", "muted")
    ctx.data = sc


# ----------------------------------------------------------------------------- compile


@command("compile", help="Compila (valida) un script PyChart guardado", usage="compile <nombre|id>", group=GROUP,
         aliases=("compilar",))
async def _compile(ctx: CommandContext) -> None:
    if not ctx.args:
        ctx.fail("Falta el nombre o id del script. Ejemplo: compile 3")
    svc = _svc(ctx)
    sc = await _guard(ctx, svc.find(" ".join(ctx.args)))
    res = await _guard(ctx, svc.compile(sc["code"]))
    meta = res["meta"]
    kind = "estrategia" if meta["kind"] == "strategy" else "indicador"
    where = "sobre el precio" if meta["overlay"] else "en panel propio"
    if res["ok"]:
        ctx.print(f"✔ '{sc['name']}' compila sin errores · {kind} \"{meta['title']}\" · {where}", "success")
    else:
        ctx.print(f"✖ '{sc['name']}' tiene {len(res['errors'])} error(es):", "error")
        for err in res["errors"]:
            pos = f"línea {err['line']}" + (f", col {err['col']}" if err.get("col") is not None else "") if err.get("line") else "general"
            ctx.print(f"  {pos}: {err['message']}", "error")
    for warning in res["warnings"]:
        ctx.print(f"  aviso: {warning}", "warn")
    if res["inputs"]:
        rows = []
        for inp in res["inputs"]:
            span = "" if inp.get("min") is None and inp.get("max") is None else f"{inp.get('min', '')} … {inp.get('max', '')}"
            opts = ", ".join(map(str, inp.get("options") or []))
            rows.append([inp.get("name"), inp.get("title"), inp.get("type"), inp.get("default"), opts or span])
        ctx.table(["Entrada", "Título", "Tipo", "Defecto", "Rango/opciones"], rows)
    ctx.data = res
    if not res["ok"]:
        ctx.fail("La compilación falló")

# --- end of section A (scripts/script/compile) ---


# ----------------------------------------------------------------------------- run


def _is_tf(text: str) -> bool:
    try:
        normalize_tf(text)
    except ValueError:
        return False
    return True


def _key(text: Any) -> str:
    return normalize_name(str(text).replace("_", " ").replace("-", " "))


async def _run_target(ctx: CommandContext, svc: Any) -> tuple[dict, list[str]]:
    """Split `run <nombre...> <símbolo> [tf]`: script names may contain spaces even without quotes."""
    args = list(ctx.args)
    if len(args) < 2:
        ctx.fail('Uso: run <nombre|id> <símbolo> [tf] · ejemplo: run "Cruce de EMAs (estrategia)" btc 1h')
    n = len(args)
    splits = ([n - 2] if n >= 3 and _is_tf(args[-1]) else []) + [n - 1]
    for k in splits:
        found = await _guard(ctx, svc.match(" ".join(args[:k])))
        if len(found) == 1:
            return await _guard(ctx, svc.get(found[0]["id"])), args[k:]
    await _guard(ctx, svc.find(" ".join(args[: splits[0]])))  # raises the helpful 404 / ambiguity message
    ctx.fail("No pude identificar el script; usa su id (ver 'scripts')")
    raise AssertionError("unreachable")


def _inputs_from_flags(ctx: CommandContext, declared: list[dict]) -> dict:
    known: dict[str, str] = {}
    for inp in declared:
        target = str(inp.get("name") or inp.get("title") or "")
        for alias in (inp.get("name"), inp.get("title")):
            if alias:
                known[_key(alias)] = target
    values: dict[str, Any] = {}
    unknown: list[str] = []
    for flag, raw in ctx.flags.items():
        if flag in RESERVED_FLAGS:
            continue
        target = known.get(_key(flag))
        if target is None:
            unknown.append(f"--{flag}")
        else:
            values[target] = _parse_value(raw)
    if unknown:
        names = ", ".join(str(i.get("name")) for i in declared) or "ninguna"
        ctx.fail(f"Entrada(s) desconocida(s): {', '.join(unknown)}. Entradas del script: {names}")
    return values


def _print_failure(ctx: CommandContext, name: str, symbol: str, tf: str, res: dict) -> None:
    ctx.print(f"✖ '{name}' falló en {symbol} {tf}:", "error")
    for err in res["errors"][:8]:
        pos = f"línea {err['line']}: " if err.get("line") else ""
        ctx.print(f"  {pos}{err['message']}", "error")
    for line in res["stdout"].splitlines()[:10]:
        ctx.print(f"  consola │ {line}", "muted")


def _print_strategy(ctx: CommandContext, metrics: dict) -> None:
    def ratio(v: Any) -> str:
        try:
            return f"{float(v):.2f}"
        except (TypeError, ValueError):
            return "—"

    ctx.print("Estrategia (backtest del script):", "system")
    ctx.table(
        ["Métrica", "Valor"],
        [
            ["Operaciones", int(metrics.get("trades") or 0)],
            ["Retorno total", fmt_pct(metrics.get("total_return_pct"), signed=True)],
            ["Comprar y mantener", fmt_pct(metrics.get("buy_hold_return_pct"), signed=True)],
            ["Acierto", fmt_pct(win_rate_pct(metrics.get("win_rate")))],
            ["Profit factor", ratio(metrics.get("profit_factor"))],
            ["Drawdown máximo", fmt_pct(metrics.get("max_dd_pct"))],
            ["Sharpe", ratio(metrics.get("sharpe"))],
            ["R medio por operación", ratio(metrics.get("avg_r"))],
        ],
    )
    ctx.print("Resultados simulados sobre datos históricos: no garantizan resultados futuros.", "muted")


@command("run", help="Ejecuta un script PyChart sobre un símbolo y resume el resultado",
         usage="run <nombre|id> <símbolo> [tf] [--limit N] [--<entrada>=valor]", group=GROUP, aliases=("ejecutar",))
async def _run(ctx: CommandContext) -> None:
    svc = _svc(ctx)
    sc, rest = await _run_target(ctx, svc)
    symbol = await ctx.symbol(rest[0])
    tf = rest[1] if len(rest) > 1 else "1h"
    if not _is_tf(tf):
        ctx.fail(f"Temporalidad no válida: {tf}")
    tf = normalize_tf(tf)
    limit = ctx.flag_int("limit", 1000) or 1000
    inputs: dict[str, Any] = {}
    if any(f not in RESERVED_FLAGS for f in ctx.flags):
        compiled = await _guard(ctx, svc.compile(sc["code"]))
        inputs = _inputs_from_flags(ctx, compiled.get("inputs") or [])
    res = await _guard(ctx, svc.run(sc["code"], symbol, tf, limit, inputs, name=sc["name"]))
    times = res["time"]
    last_t = times[-1] if times else None
    metrics = strategy_metrics(res)
    ctx.data = {
        "script_id": sc["id"], "name": sc["name"], "symbol": symbol, "tf": tf, "ok": res["ok"], "bars": len(times),
        "duration_ms": res["duration_ms"], "meta": res["meta"], "inputs": inputs, "errors": res["errors"],
        "plots": [{"id": p.get("id"), "title": p.get("title"), "last": _last_value(p.get("data"))} for p in res["plots"]],
        "alerts_last_bar": [a for a in res["alerts"] if a.get("time") == last_t], "metrics": metrics,
    }
    if not res["ok"]:
        _print_failure(ctx, sc["name"], symbol, tf, res)
        ctx.fail("La ejecución falló")
    meta = res["meta"]
    kind = "Estrategia" if meta["kind"] == "strategy" else "Indicador"
    where = "sobre el precio" if meta["overlay"] else "en panel propio"
    ctx.print(f"PyChart ▶ {sc['name']} · {symbol} {tf} · {len(times)} velas · {res['duration_ms']} ms", "system")
    ctx.print(f"{kind} \"{meta['title']}\" · {where} · última vela {_fmt_time(last_t)}", "info")
    if inputs:
        ctx.print("Entradas: " + ", ".join(f"{k}={v}" for k, v in inputs.items()), "muted")
    if res["plots"]:
        rows = [
            [p.get("title") or p.get("id"), _fmt_num(_last_value(p.get("data"))),
             "precio" if int(p.get("pane") or 0) == 0 else f"panel {p.get('pane')}"]
            for p in res["plots"]
        ]
        ctx.table(["Gráfico", "Último valor", "Panel"], rows, max_rows=30)
    else:
        ctx.print("El script no dibujó ningún gráfico (usa plot(...)).", "muted")
    if res["hlines"]:
        levels = [f"{_fmt_num(h.get('price'))}" + (f" ({h['title']})" if h.get("title") else "") for h in res["hlines"][:8]]
        ctx.print("Niveles: " + ", ".join(levels), "muted")
    ctx.print(f"Marcas: {len(res['markers'])} · fondos: {len(res['bgcolors'])} velas · alertas: {len(res['alerts'])}", "muted")
    for marker in [m for m in res["markers"] if m.get("time") == last_t][:5]:
        ctx.print(f"  En la última vela: {marker.get('text') or marker.get('tooltip') or marker.get('kind')}", "signal")
    for alert in ctx.data["alerts_last_bar"][:5]:
        ctx.print(f"  ALERTA ahora: {alert.get('title')}: {alert.get('message')}", "signal")
    if res["alerts"] and not ctx.data["alerts_last_bar"]:
        last = res["alerts"][-1]
        ctx.print(f"  Última alerta: {last.get('title')} · {_fmt_time(last.get('time'))}", "muted")
    if metrics is not None:
        _print_strategy(ctx, metrics)
    out_lines = res["stdout"].splitlines()
    for line in out_lines[:10]:
        ctx.print(f"consola │ {line}", "muted")
    if len(out_lines) > 10:
        ctx.print(f"consola │ … {len(out_lines) - 10} líneas más", "muted")
