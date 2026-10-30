"""Terminal commands of the NEXUS group: signal, check, predict, explain, model, reset-model, indicators,
watchlist-signals."""
from __future__ import annotations

import asyncio
from typing import Any

from app.engine import fmt
from app.terminal import CommandContext, command
from app.util import normalize_tf

GROUP = "NEXUS"
DIRECTIONS = {"long": "long", "largo": "long", "compra": "long", "buy": "long", "l": "long",
              "short": "short", "corto": "short", "venta": "short", "sell": "short", "s": "short"}
VERDICT_LEVEL = {"TAKE": "success", "WAIT": "warn", "AVOID": "error"}
ACTION_LEVEL = {"BUY": "success", "SELL": "error", "EXIT_LONG": "warn", "EXIT_SHORT": "warn"}
DISCLAIMER = "Probabilidades medidas fuera de muestra; ningún indicador garantiza resultados."


def _nexus(ctx: CommandContext) -> Any:
    if ctx.rt.nexus is None:
        ctx.fail("El motor NEXUS no está disponible")
    return ctx.rt.nexus


def _tf(ctx: CommandContext, positional: str | None) -> str:
    raw = ctx.flag("tf") or positional or "1h"
    try:
        return normalize_tf(raw)
    except ValueError as exc:
        ctx.fail(str(exc))
    return "1h"


async def _run(ctx: CommandContext, coro: Any) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=120)
    except asyncio.TimeoutError:
        ctx.fail("El análisis tardó demasiado; inténtalo de nuevo")
    except ValueError as exc:
        ctx.fail(str(exc))
    except Exception as exc:
        name = type(exc).__name__
        ctx.fail(str(exc) if name == "NexusDataError" else f"NEXUS falló: {name}: {exc}")


async def _analysis(ctx: CommandContext) -> tuple[str, str, dict]:
    sym = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, ctx.arg(1))
    resp = await _run(ctx, _nexus(ctx).analyze(sym, tf))
    if not resp.get("ok"):
        ctx.print(resp.get("message") or "Historial insuficiente", "warn")
        quick = resp.get("quick")
        if quick:
            ctx.print(f"Lectura rápida: {quick.get('action')} · score {fmt.num(quick.get('score'), 2, True)} · "
                      f"p↑ {fmt.prob(quick.get('prob_up'))}", "info")
            for r in (quick.get("reasons") or [])[:5]:
                ctx.print(f"  · {r}", "muted")
        ctx.data = resp
        ctx.fail("NEXUS necesita más historial para este activo")
    return sym, tf, resp


def _print_warnings(ctx: CommandContext, cur: dict) -> None:
    for w in cur.get("warnings") or []:
        level = "error" if w.get("severity") == "critical" else "warn" if w.get("severity") == "warn" else "muted"
        ctx.print(f"⚠ {w.get('message')}", level)


@command("signal", help="Señal NEXUS actual de un símbolo (acción, filtro y consejo)",
         usage="signal <símbolo> [tf]", group=GROUP, aliases=("señal", "senal", "nexus"))
async def _signal(ctx: CommandContext) -> None:
    sym, tf, resp = await _analysis(ctx)
    cur = resp["current"]
    gl, gs = cur["gate"]["long"], cur["gate"]["short"]
    ctx.print(f"{sym} {tf} · {fmt.price(cur['price'])} · {cur['regime_label']}", "info")
    ctx.print(f"→ {cur['action_label']} · score {fmt.num(cur['score'], 2, True)} · p↑ {fmt.prob(cur['prob_up'])} · "
              f"confianza {fmt.prob(cur['confidence'])}", ACTION_LEVEL.get(cur["action"], "signal"))
    ctx.print(f"Filtro: largo {gl['verdict_label']} {fmt.num(gl['score'], 0)}/100 · corto {gs['verdict_label']} "
              f"{fmt.num(gs['score'], 0)}/100", "info")
    pos = cur.get("position")
    if pos:
        side = "largo" if pos["side"] == "long" else "corto"
        ctx.print(f"Posición {side} desde {fmt.price(pos['entry_price'])} · stop {fmt.price(pos['sl'])} · "
                  f"TP {fmt.price(pos['tp1'])} / {fmt.price(pos['tp2'])} / {fmt.price(pos['tp3'])} · "
                  f"{fmt.rr(pos['r_multiple'])} · EV mantener {fmt.rr(pos['ev_hold_r'])}", "info")
    ctx.print(f"Consejo: {cur['advice']}", "signal")
    _print_warnings(ctx, cur)
    st = resp.get("stats") or {}
    ctx.print(f"Histórico: {st.get('trades', 0)} operaciones · acierto {fmt.prob(st.get('win_rate'))} · "
              f"rechazadas por el filtro {st.get('rejected_signals', 0)} (habrían ganado "
              f"{fmt.prob(st.get('rejected_would_win_rate'))})", "muted")
    ctx.action("open_chart", symbol=sym, tf=tf)
    ctx.data = cur


def _check_line(ctx: CommandContext, chk: dict) -> None:
    passed = chk.get("passed")
    mark = "✓" if passed else "·" if passed is None else "✗"
    crit = " (CRÍTICO)" if chk.get("critical") and passed is False else ""
    level = "success" if passed else "muted" if passed is None else ("error" if chk.get("critical") else "warn")
    ctx.print(f"{mark} {chk['label']}{crit} — {chk['detail']} [peso {fmt.num(chk.get('weight'), 2)}]", level)


@command("check", help="¿Tomo esta operación? Lista completa ✓/✗ del filtro para una idea",
         usage="check <símbolo> <long|short> [tf] [--entry X] [--sl Y] [--tp Z]", group=GROUP,
         aliases=("filtro", "chequear", "gate"))
async def _check(ctx: CommandContext) -> None:
    sym = await ctx.symbol(ctx.arg(0))
    raw_dir = (ctx.arg(1) or ("short" if ctx.flag("short") else "long")).lower()
    direction = DIRECTIONS.get(raw_dir)
    rest = ctx.args[2:] if direction else ctx.args[1:]
    if direction is None:
        direction = "short" if ctx.flag("short") else "long"
    tf = _tf(ctx, rest[0] if rest else None)
    entry, sl, tp = ctx.flag_float("entry"), ctx.flag_float("sl"), ctx.flag_float("tp")
    gate = await _run(ctx, _nexus(ctx).gate(sym, tf, direction, entry=entry, sl=sl, tp=tp))
    side = "LARGO" if direction == "long" else "CORTO"
    ctx.print(f"¿Tomo esta operación? {sym} {tf} · {side}", "info")
    ctx.print(f"Veredicto: {gate['verdict_label']} {fmt.num(gate['score'], 0)}/100 · P(TP1) {fmt.prob(gate['win_prob'])}"
              f" · R:R {fmt.num(gate['rr'], 1)} · EV {fmt.rr(gate['ev_r'], 2)}", VERDICT_LEVEL[gate["verdict"]])
    risk_pct = (gate["sl"] / gate["entry"] - 1.0) * 100.0 if gate["entry"] else None
    ctx.print(f"Entrada {fmt.price(gate['entry'])} · Stop {fmt.price(gate['sl'])} ({fmt.pct(risk_pct)}) · "
              f"TP1 {fmt.price(gate['tp1'])} · TP2 {fmt.price(gate['tp2'])} · TP3 {fmt.price(gate['tp3'])}", "info")
    for chk in gate.get("checks") or []:
        _check_line(ctx, chk)
    ctx.print(f"Resumen: {gate['summary']}", VERDICT_LEVEL[gate["verdict"]])
    ctx.print(DISCLAIMER, "muted")
    ctx.action("open_chart", symbol=sym, tf=tf)
    ctx.data = gate


@command("predict", help="Predicción NEXUS: probabilidad y cono de precios para las próximas velas",
         usage="predict <símbolo> [tf]", group=GROUP, aliases=("prediccion", "predicción", "pronostico"))
async def _predict(ctx: CommandContext) -> None:
    sym, tf, resp = await _analysis(ctx)
    pred = resp.get("prediction")
    if not pred:
        ctx.fail("No hay predicción disponible")
    cone = pred.get("cone") or []
    ctx.print(f"{sym} {tf} · próximas {pred['horizon_bars']} velas · método: {pred['method']}", "info")
    ctx.print(f"P(subida) {fmt.prob(pred['prob_up'])} · rentabilidad esperada {fmt.pct(pred['expected_return_pct'], 2)}",
              "signal")
    rows = [[str(k), fmt.price(p["p10"]), fmt.price(p["p25"]), fmt.price(p["p50"]), fmt.price(p["p75"]),
             fmt.price(p["p90"])] for k, p in enumerate(cone) if k > 0]
    ctx.table(["Vela", "P10", "P25", "P50", "P75", "P90"], rows)
    learning = resp.get("learning") or {}
    ctx.print(f"Acierto medido del modelo: {fmt.prob(learning.get('hit_rate'))} en {learning.get('updates', 0)} "
              f"actualizaciones · Brier {fmt.num(learning.get('brier'), 3)}", "muted")
    ctx.print(DISCLAIMER, "muted")
    ctx.data = pred


@command("explain", help="Explica la señal: régimen, expertos que la apoyan y advertencias",
         usage="explain <símbolo> [tf]", group=GROUP, aliases=("explicar", "porque"))
async def _explain(ctx: CommandContext) -> None:
    sym, tf, resp = await _analysis(ctx)
    cur = resp["current"]
    ctx.print(f"{sym} {tf} → {cur['action_label']} · {cur['advice']}", ACTION_LEVEL.get(cur["action"], "signal"))
    for reason in cur.get("reasons") or []:
        ctx.print(f"· {reason}", "info")
    rows = [[c["label"], fmt.num(c["signal"], 2, True), fmt.num(c["weight"], 3), fmt.num(c["contribution"], 3, True)]
            for c in cur.get("contributions") or []]
    ctx.table(["Experto", "Señal", "Peso", "Aporte"], rows)
    lv = cur.get("levels") or {}
    if lv.get("support") or lv.get("resistance"):
        ctx.print("Soportes: " + (", ".join(fmt.price(p) for p in lv.get("support") or []) or "—") +
                  " · Resistencias: " + (", ".join(fmt.price(p) for p in lv.get("resistance") or []) or "—"), "muted")
    _print_warnings(ctx, cur)
    ctx.data = {"reasons": cur.get("reasons"), "contributions": cur.get("contributions")}


@command("model", help="Estado del aprendizaje NEXUS de un símbolo (acierto, Brier, pesos del filtro)",
         usage="model <símbolo> [tf]", group=GROUP, aliases=("modelo",))
async def _model(ctx: CommandContext) -> None:
    sym = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, ctx.arg(1))
    model = await _run(ctx, _nexus(ctx).model(sym, tf))
    lr = model.get("learning") or {}
    until = lr.get("trained_until")
    ctx.print(f"Modelo NEXUS {sym} {tf}: {lr.get('updates', 0)} actualizaciones · memoria {lr.get('memory_bars', 0)} "
              f"velas · acierto {fmt.prob(lr.get('hit_rate'))} · Brier {fmt.num(lr.get('brier'), 3)}", "learn")
    if until:
        ctx.print(f"Entrenado hasta la vela {until} (unix)", "muted")
    cal = model.get("calibration") or []
    if cal:
        ctx.table(["Prob. predicha", "Observada", "Casos"],
                  [[fmt.prob(b["predicted"]), fmt.prob(b["observed"]), str(b["count"])] for b in cal])
    gw = lr.get("gate_weights") or {}
    if gw:
        rows = sorted(gw.items(), key=lambda kv: -kv[1])
        ctx.table(["Chequeo del filtro", "Peso aprendido"], [[k, fmt.num(v, 2)] for k, v in rows])
    ctx.data = model


@command("reset-model", help="Borra la memoria aprendida de NEXUS para un símbolo (pide --yes)",
         usage="reset-model <símbolo> [tf] --yes", group=GROUP, aliases=("reiniciar-modelo",))
async def _reset_model(ctx: CommandContext) -> None:
    sym = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, ctx.arg(1))
    if not ctx.flag("yes"):
        ctx.print(f"Esto borra la memoria aprendida de {sym} {tf}. Repite con --yes para confirmar.", "warn")
        return
    await _run(ctx, _nexus(ctx).reset(sym, tf))
    ctx.print(f"Memoria NEXUS de {sym} {tf} reiniciada: el modelo vuelve a aprender desde cero.", "success")


@command("indicators", help="Lista los indicadores disponibles (filtra por texto)",
         usage="indicators [texto]", group=GROUP, aliases=("indicadores", "ind"))
async def _indicators(ctx: CommandContext) -> None:
    try:
        from app.engine.indicators import CATALOG
    except Exception:
        ctx.fail("Biblioteca de indicadores no disponible")
        return
    text = " ".join(ctx.args).strip().lower()
    items = [s for s in CATALOG if not text or any(text in str(s.get(k, "")).lower()
                                                   for k in ("id", "name", "category", "description"))]
    rows = [[s["id"], s["name"], s["category"], "precio" if s.get("overlay") else "panel"] for s in items]
    ctx.print(f"{len(items)} de {len(CATALOG)} indicadores" + (f" para '{text}'" if text else ""), "info")
    ctx.table(["Id", "Nombre", "Categoría", "Dónde"], rows, max_rows=80)
    ctx.data = [s["id"] for s in items]


@command("watchlist-signals", help="Señales NEXUS de toda la lista de seguimiento",
         usage="watchlist-signals [tf]", group=GROUP, aliases=("senales", "señales", "wls"))
async def _watchlist_signals(ctx: CommandContext) -> None:
    hub = ctx.rt.hub
    nexus = _nexus(ctx)
    if hub is None:
        ctx.fail("Los datos de mercado no están disponibles")
    tf = _tf(ctx, ctx.arg(0) or (ctx.rt.settings.get("watch.tf", "1h") if ctx.rt.settings else "1h"))
    symbols = list(await hub.watchlist())[:30]
    if not symbols:
        ctx.fail("La lista de seguimiento está vacía")
    sem = asyncio.Semaphore(3)

    async def one(sym: str) -> tuple[str, dict | None, str | None]:
        async with sem:
            try:
                resp = await asyncio.wait_for(nexus.analyze(sym, tf), timeout=90)
            except Exception as exc:
                return sym, None, type(exc).__name__
        if not resp.get("ok"):
            return sym, None, "historial insuficiente"
        return sym, resp["current"], None

    results = await asyncio.gather(*(one(s) for s in symbols))
    rows, data = [], []
    for sym, cur, err in results:
        if cur is None:
            rows.append([sym, "—", "—", "—", "—", "—", err or ""])
            continue
        gl, gs = cur["gate"]["long"], cur["gate"]["short"]
        rows.append([sym, cur["action_label"], fmt.num(cur["score"], 2, True), fmt.prob(cur["prob_up"]),
                     f"{gl['verdict_label']} {fmt.num(gl['score'], 0)}", f"{gs['verdict_label']} {fmt.num(gs['score'], 0)}",
                     cur["regime_label"]])
        data.append({"symbol": sym, "action": cur["action"], "score": cur["score"], "prob_up": cur["prob_up"],
                     "long": gl["verdict"], "short": gs["verdict"]})
    ctx.print(f"Señales NEXUS · {len(symbols)} símbolos · {tf}", "info")
    ctx.table(["Símbolo", "Acción", "Score", "P↑", "Filtro largo", "Filtro corto", "Régimen"], rows)
    ctx.data = data
