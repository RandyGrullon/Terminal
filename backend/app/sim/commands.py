"""Terminal commands of the simulation package (groups: Escáner, Simulación, Cuenta demo, Aprendizaje)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app import config
from app.sim.backtest import BacktestError, run_backtest, save_backtest, summarize_backtest
from app.sim.common import (
    ACTION_LABEL,
    DISCLAIMER,
    SIDE_LABEL,
    VERDICT_LABEL,
    EngineUnavailable,
    fmt_num,
    fmt_pct,
    fmt_price,
    fmt_prob,
    fmt_qty,
    fmt_r,
    gate_line,
    rating_label,
)
from app.sim.evaluator import learning_summary, list_predictions, prediction_stats
from app.sim.montecarlo import MonteCarloError, run_montecarlo
from app.sim.paper import PaperError, PaperRejected
from app.sim.scanner import ScanError
from app.sim.simulator import SimError
from app.terminal import CommandContext, command, register_bool_flags
from app.util import clamp, normalize_tf, now_ms

register_bool_flags("take", "synthetic", "sintetico", "noauto")

_ON = {"on", "si", "sí", "activar", "activa", "1", "true", "encender"}
_OFF = {"off", "no", "desactivar", "desactiva", "0", "false", "apagar"}


# ----------------------------------------------------------------------------- helpers


def _svc(ctx: CommandContext, name: str, label: str) -> Any:
    svc = getattr(ctx.rt, name, None)
    if svc is None:
        ctx.fail(f"{label} no está disponible (el módulo no se cargó)")
    return svc


def _tf(ctx: CommandContext, text: str | None, default: str = "1h") -> str:
    try:
        return normalize_tf(text or default)
    except ValueError as exc:
        ctx.fail(str(exc))
        raise  # unreachable (ctx.fail raises)


def _is_tf(text: str | None) -> bool:
    if not text:
        return False
    try:
        normalize_tf(text)
        return True
    except ValueError:
        return False


def _setting(ctx: CommandContext, key: str, default: Any = None) -> Any:
    return ctx.rt.settings.get(key, default) if ctx.rt.settings is not None else default


def _ago(ms: int | None) -> str:
    if not ms:
        return "—"
    s = max(0, int((now_ms() - int(ms)) / 1000))
    if s < 60:
        return f"hace {s} s"
    if s < 3600:
        return f"hace {s // 60} min"
    if s < 86400:
        return f"hace {s // 3600} h"
    return f"hace {s // 86400} d"


def _day(ts_s: int) -> str:
    return datetime.fromtimestamp(int(ts_s), tz=timezone.utc).strftime("%d/%m")


def _short(text: Any, n: int = 46) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_gate(ctx: CommandContext, gate: dict | None, level: str = "info") -> None:
    if not gate:
        return
    ctx.print(f"Filtro: {gate_line(gate)} · prob. TP1 antes que SL {fmt_prob(gate.get('win_prob'))} · "
              f"R:R {fmt_num(gate.get('rr'), 2)} · EV {fmt_r(gate.get('ev_r'), 2)}", level)
    if gate.get("summary"):
        ctx.print(f"  {gate['summary']}", level)
    for r in (gate.get("reasons_for") or [])[:3]:
        ctx.print(f"  ✔ {r}", "success")
    for r in (gate.get("reasons_against") or [])[:3]:
        ctx.print(f"  ✖ {r}", "warn")


# ============================================================================= Escáner


@command("scan", help="Escanea un mercado con el modelo rápido (NEXUS + filtro con --deep)",
         usage="scan [mercado] [tf] [--deep] [--limit N]", group="Escáner", aliases=("escanear",))
async def _scan(ctx: CommandContext) -> None:
    svc = _svc(ctx, "scanner", "El escáner")
    a0, a1 = ctx.arg(0), ctx.arg(1)
    market, tf = (None, a0) if (_is_tf(a0) and a1 is None) else (a0, a1)
    tf_n = _tf(ctx, tf, str(_setting(ctx, "scanner.tf", "1h")))
    if config.OFFLINE:
        ctx.fail("Modo sin conexión: el escáner necesita datos de TradingView")
    deep = bool(ctx.flag("deep", False))
    limit = ctx.flag_int("limit", None)
    top = ctx.flag_int("top", None)
    try:
        run_id = await svc.run_scan(market, tf_n, limit, deep, top)
    except (ScanError, ValueError) as exc:
        ctx.fail(str(exc))
    m = (market or (_setting(ctx, "scanner.markets", ["crypto"]) or ["crypto"])[0]).lower()
    ctx.print(
        f"Escaneo #{run_id} iniciado: {m} {tf_n} · hasta {limit or _setting(ctx, 'scanner.limit', 300)} activos"
        + (" · modo profundo (NEXUS + filtro)" if deep else "")
        + ". Te aviso al terminar; luego escribe 'results'.",
        "scan",
    )
    ctx.data = {"run_id": run_id}


@command("results", help="Resultados del último escaneo (con --take solo las oportunidades TOMAR)",
         usage="results [n] [--take]", group="Escáner", aliases=("resultados",))
async def _results(ctx: CommandContext) -> None:
    svc = _svc(ctx, "scanner", "El escáner")
    n = int(clamp(int(ctx.arg(0)), 1, 200)) if str(ctx.arg(0, "")).isdigit() else 20
    run, rows = await svc.results(None)
    if run is None:
        ctx.print("Todavía no hay escaneos. Prueba: scan crypto 1h --deep", "muted")
        return
    if ctx.flag("take"):
        rows = [r for r in rows if r.get("verdict") == "TAKE"]
    ctx.print(f"Escaneo #{run['id']} · {run['market']} {run['tf']} · {_ago(run['started_at'])} · "
              f"{run['n_symbols']} activos · {run.get('message') or ''}", "scan")
    if not rows:
        ctx.print("Sin filas que mostrar" + (" (ninguna oportunidad TOMAR)" if ctx.flag("take") else ""), "muted")
        return
    table = []
    for r in rows[:n]:
        verdict = r.get("verdict")
        filt = f"{VERDICT_LABEL.get(verdict, verdict)} {r.get('gate_score') or 0:.0f}" if verdict else "—"
        table.append([
            r["rank"], r["symbol"], fmt_price(r.get("price")), fmt_pct(r.get("change_pct")),
            fmt_num(r.get("score"), 2, True), fmt_prob(r.get("prob_up")), ACTION_LABEL.get(r.get("action"), r.get("action")),
            fmt_prob(r.get("confidence")), filt, _short((r.get("reasons") or [""])[0]),
        ])
    ctx.table(["#", "Símbolo", "Precio", "Cambio", "Score", "p↑", "Acción", "Conf.", "Filtro", "Motivo principal"],
              table, max_rows=n)
    ctx.data = {"run": run, "results": rows[:n]}


@command("scanner", help="Activa o desactiva el escáner automático", usage="scanner on|off", group="Escáner")
async def _scanner(ctx: CommandContext) -> None:
    svc = _svc(ctx, "scanner", "El escáner")
    arg = str(ctx.arg(0, "")).lower()
    if arg in _ON or arg in _OFF:
        await ctx.rt.settings.set("scanner.enabled", arg in _ON)
    st = svc.status()
    enabled = bool(_setting(ctx, "scanner.enabled", True))
    markets = ", ".join(_setting(ctx, "scanner.markets", ["crypto"]) or [])
    ctx.print(f"Escáner automático {'ACTIVADO' if enabled else 'DESACTIVADO'} · mercados: {markets} · "
              f"cada {int(_setting(ctx, 'scanner.interval_s', 900)) // 60} min", "success" if enabled else "warn")
    if not config.BACKGROUND:
        ctx.print("Los bucles en segundo plano están deshabilitados en este proceso (NEXUS_BACKGROUND=0)", "muted")
    elif st.get("next_run_in_s") is not None and enabled:
        ctx.print(f"Próximo escaneo en {st['next_run_in_s']} s · último: {_ago(st.get('last_run'))} · "
                  f"aprendizaje: {st.get('model_updates', 0)} actualizaciones", "muted")
    ctx.data = st


@command("quick", help="Lectura rápida de cualquier símbolo (modelo del escáner)", usage="quick <simbolo> [tf]",
         group="Escáner", aliases=("rapido", "rápido"))
async def _quick(ctx: CommandContext) -> None:
    svc = _svc(ctx, "scanner", "El escáner")
    sym = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, ctx.arg(1), "1h")
    q = await svc.quick_signal(sym, tf)
    if not q:
        ctx.fail(f"Sin datos técnicos para {sym} {tf}")
    tone = "success" if q["action"] == "BUY" else "error" if q["action"] == "SELL" else "warn"
    ctx.print(f"{sym} {tf} → {ACTION_LABEL.get(q['action'], q['action'])} · score {fmt_num(q['score'], 2, True)} · "
              f"p↑ {fmt_prob(q['prob_up'])} · confianza {fmt_prob(q['confidence'])} · precio {fmt_price(q.get('price'))}",
              tone)
    if q.get("rating") is not None:
        ctx.print(f"Rating TradingView: {rating_label(q['rating'])} ({fmt_num(q['rating'], 2, True)})", "muted")
    for r in q.get("reasons") or []:
        ctx.print(f"  • {r}")
    ctx.print("Lectura probabilística, no una garantía. Para decidir usa: check <símbolo> long|short", "muted")
    ctx.data = q


# ============================================================================= Simulación


def _metric_rows(m: dict, u: dict | None) -> list[list[str]]:
    def both(label: str, key: str, fmt) -> list[str]:
        row = [label, fmt(m.get(key))]
        if u is not None:
            row.append(fmt(u.get(key)))
        return row

    def pf(v: Any) -> str:
        return fmt_num(v, 2) if v is not None else "—"

    return [
        both("Operaciones", "trades", lambda v: str(v or 0)),
        both("Retorno total", "total_return_pct", fmt_pct),
        both("CAGR", "cagr_pct", fmt_pct),
        both("Buy & hold", "buy_hold_return_pct", fmt_pct),
        both("Acierto", "win_rate", fmt_prob),
        both("Profit factor", "profit_factor", pf),
        both("Expectativa/op.", "expectancy_pct", lambda v: fmt_pct(v, 2)),
        both("R medio", "avg_r", lambda v: fmt_r(v, 2)),
        both("Drawdown máx.", "max_dd_pct", lambda v: fmt_pct(v, 1, signed=False)),
        both("Sharpe", "sharpe", pf),
        both("Sortino", "sortino", pf),
        both("Exposición", "exposure_pct", lambda v: fmt_pct(v, 0, signed=False)),
        both("Pérdidas seguidas", "max_consecutive_losses", lambda v: str(v or 0)),
        both("Costes", "fees_paid", lambda v: fmt_num(v, 2)),
    ]


@command("backtest", help="Backtest de NEXUS con y sin el filtro de operaciones",
         usage="backtest <simbolo> [tf] [--bars N] [--nogate]", group="Simulación", aliases=("bt",))
async def _backtest(ctx: CommandContext) -> None:
    hub = _svc(ctx, "hub", "El servicio de datos")
    sym = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, ctx.arg(1), "1h")
    bars = int(clamp(ctx.flag_int("bars", 2000) or 2000, 300, 5000))
    use_gate = not bool(ctx.flag("nogate", False))
    try:
        cs = await hub.get_candles(sym, tf, bars)
    except Exception as exc:  # ProviderError (invalid input) or an unexpected provider failure
        ctx.fail(f"No se pudieron obtener las velas de {sym} {tf}: {exc}")
    if cs is None or len(cs) == 0:
        ctx.fail(f"Sin velas para {sym} {tf}" + (f": {cs.note}" if cs is not None and cs.note else ""))
    ctx.print(f"Backtest de {sym} {tf} sobre {len(cs)} velas ({cs.provider or 'datos locales'})…", "muted")
    try:
        result = await asyncio.to_thread(
            run_backtest, cs, None,
            initial_capital=float(_setting(ctx, "risk.account_size", 10000.0)),
            risk_pct=float(_setting(ctx, "risk.per_trade_pct", 1.0)),
            fee_pct=float(_setting(ctx, "risk.fee_pct", 0.1)),
            slippage_pct=float(_setting(ctx, "risk.slippage_pct", 0.02)),
            use_gate=use_gate,
            include_candles=False,
        )
    except (EngineUnavailable, BacktestError, ValueError) as exc:
        ctx.fail(str(exc))
    await save_backtest(ctx.rt.db, result)
    m, u = result["metrics"], result.get("ungated")
    headers = ["Métrica", "Con filtro", "Sin filtro"] if u is not None else ["Métrica", "Valor"]
    ctx.table(headers, _metric_rows(m, u), max_rows=30)
    lift = m.get("gate_lift_pct")
    if lift is not None:
        verb = "aporta" if lift >= 0 else "resta"
        ctx.print(f"El filtro «¿Tomo esta operación?» {verb} {fmt_pct(abs(lift) if lift >= 0 else lift)} de retorno "
                  f"frente a tomar todas las señales.", "success" if lift >= 0 else "warn")
    if m.get("rejected_signals"):
        wr = m.get("rejected_would_win_rate")
        ctx.print(f"Señales rechazadas por el filtro: {m['rejected_signals']}"
                  + (f" · de ellas habrían ganado {fmt_prob(wr)}" if wr is not None else ""), "info")
    ctx.print(DISCLAIMER, "muted")
    ctx.data = {"metrics": m, "ungated": u, "symbol": sym, "tf": tf}
    if ctx.rt.bus is not None:
        ctx.rt.bus.emit("info", "backtest", summarize_backtest(result))


@command("mc", help="Monte Carlo: bootstrap de operaciones o NEXUS en mercados sintéticos",
         usage="mc <simbolo> [tf] [--paths N] [--synthetic]", group="Simulación", aliases=("montecarlo",))
async def _mc(ctx: CommandContext) -> None:
    synthetic = bool(ctx.flag("synthetic", False) or ctx.flag("sintetico", False))
    args = list(ctx.args)
    sym = None
    if synthetic:
        tf = _tf(ctx, next((a for a in args if _is_tf(a)), None), "1h")
    else:
        _svc(ctx, "hub", "El servicio de datos")
        sym = await ctx.symbol(ctx.arg(0))
        tf = _tf(ctx, ctx.arg(1), "1h")
    paths = ctx.flag_int("paths", 30 if synthetic else 300) or (30 if synthetic else 300)
    bars = ctx.flag_int("bars", 500 if synthetic else 1500) or (500 if synthetic else 1500)
    seed = ctx.flag_int("seed", 1) or 1
    ctx.print(f"Monte Carlo {'sintético' if synthetic else 'bootstrap'}{' de ' + sym if sym else ''} {tf}…", "muted")
    try:
        res = await run_montecarlo(ctx.rt, mode="synthetic" if synthetic else "bootstrap", symbol=sym, tf=tf,
                                   paths=paths, bars=bars, seed=seed,
                                   initial_capital=float(_setting(ctx, "risk.account_size", 10000.0)))
    except (EngineUnavailable, MonteCarloError, ValueError) as exc:
        ctx.fail(str(exc))
    dds = sorted(res["max_drawdowns_pct"])
    median_dd = dds[len(dds) // 2] if dds else 0.0
    ctx.table(["Resultado", "Valor"], [
        ["Caminos simulados", str(res["paths"])],
        ["Pasos por camino", str(res["steps"])],
        ["P(ganancia)", fmt_prob(res["prob_profit"])],
        ["Retorno mediano", fmt_pct(res["median_return_pct"])],
        ["Retorno esperado", fmt_pct(res["expected_return_pct"])],
        ["VaR 95 %", fmt_pct(res["var95_pct"])],
        ["CVaR 95 %", fmt_pct(res["cvar95_pct"])],
        ["Drawdown mediano", fmt_pct(median_dd, 1, signed=False)],
        ["P(ruina, DD ≥ 50 %)", fmt_prob(res["prob_ruin"], 1)],
    ])
    for note in res.get("notes") or []:
        ctx.print(note, "muted")
    ctx.data = {k: v for k, v in res.items() if k not in ("final_returns_pct", "max_drawdowns_pct", "sample_paths")}


def _print_sim_state(ctx: CommandContext, st: dict) -> None:
    if not st.get("running"):
        acct = st.get("account") or {}
        ctx.print("Simulador detenido" + (f" · última sesión: {acct.get('trades', 0)} operaciones, "
                                          f"{fmt_pct(acct.get('return_pct'))}" if acct else ""), "muted")
        return
    total = f"/{st['bars_total']}" if st.get("bars_total") else ""
    regime = st.get("regime") or "—"
    if st.get("market_regime") and st.get("regime") == "auto":
        regime = f"auto ({st['market_regime']})"
    ctx.print(f"{'⏸ En pausa' if st.get('paused') else '▶ En marcha'} · {st.get('mode')} {st.get('symbol')} {st.get('tf')} · "
              f"velas {st.get('bars_done')}{total} · {fmt_num(st.get('speed'), 1)} velas/s · régimen {regime} · "
              f"precio {fmt_price(st.get('price'))}", "info")
    stats = st.get("stats") or {}
    ctx.print(f"Cuenta Simulación: {stats.get('trades', 0)} operaciones · acierto {fmt_prob(stats.get('win_rate'))} · "
              f"resultado {fmt_pct(stats.get('pnl_pct'))} · auto-trading {'sí' if st.get('auto_trade') else 'no'} · "
              f"última acción NEXUS: {st.get('last_action') or '—'}", "muted")


@command("sim", help="Simulador de mercado (sintético o replay de un símbolo real)",
         usage="sim start [synthetic|replay <simbolo>] [tf] [--speed N] [--seed N] [--regime=x] | "
               "sim stop|pause|resume|speed N|regime x|status",
         group="Simulación", aliases=("simulador",))
async def _sim(ctx: CommandContext) -> None:
    sim = _svc(ctx, "sim", "El simulador")
    sub = str(ctx.arg(0, "status")).lower()
    try:
        if sub in ("start", "iniciar", "empezar"):
            rest = list(ctx.args[1:])
            body: dict[str, Any] = {"mode": "synthetic"}
            tf = None
            if rest and rest[0].lower() in ("replay", "repeticion", "repetición"):
                body["mode"] = "replay"
                body["symbol"] = await ctx.symbol(rest[1] if len(rest) > 1 else None)
                tf = rest[2] if len(rest) > 2 else None
            elif rest and rest[0].lower() in ("synthetic", "sintetico", "sintético", "synth"):
                tf = rest[1] if len(rest) > 1 else None
            elif rest:
                tf = rest[0]
            body["tf"] = _tf(ctx, tf, "1h")
            body["speed"] = ctx.flag_float("speed", None)
            body["seed"] = ctx.flag_int("seed", None)
            body["regime"] = ctx.flag("regime", None)
            body["warmup"] = ctx.flag_int("warmup", None)
            body["auto_trade"] = not bool(ctx.flag("noauto", False))
            st = await sim.start_session(body)
            ctx.print("✔ Simulación iniciada", "success")
            _print_sim_state(ctx, st)
            ctx.action("navigate", page="sim")
            ctx.data = st
        elif sub in ("stop", "detener", "parar"):
            st = await sim.stop_session()
            ctx.print("■ Simulación detenida", "warn")
            _print_sim_state(ctx, st)
            ctx.data = st
        elif sub in ("pause", "pausa", "pausar"):
            ctx.data = await sim.configure(paused=True)
            ctx.print("⏸ Simulación en pausa", "warn")
        elif sub in ("resume", "reanudar", "continuar", "play"):
            ctx.data = await sim.configure(paused=False)
            ctx.print("▶ Simulación reanudada", "success")
        elif sub in ("speed", "velocidad"):
            val = ctx.arg(1)
            if val is None:
                ctx.fail("Indica la velocidad en velas por segundo (0,5 a 20)")
            try:
                speed = float(str(val).replace(",", "."))
            except ValueError:
                ctx.fail(f"Velocidad no válida: {val}")
            ctx.data = await sim.configure(speed=speed)
            ctx.print(f"Velocidad: {fmt_num(ctx.data['speed'], 1)} velas/s", "success")
        elif sub in ("regime", "regimen", "régimen"):
            val = ctx.arg(1)
            if not val:
                ctx.fail("Indica el régimen: auto, bull, bear, range, volatile o crash")
            ctx.data = await sim.configure(regime=val)
            ctx.print(f"Régimen: {val}", "success")
        elif sub in ("status", "estado"):
            st = sim.state()
            _print_sim_state(ctx, st)
            ctx.data = st
        else:
            ctx.fail(f"Subcomando desconocido: {sub}. Usa start, stop, pause, resume, speed, regime o status")
    except SimError as exc:
        ctx.fail(str(exc))


# ============================================================================= Cuenta demo


async def _order(ctx: CommandContext, side: str) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    sym = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, ctx.flag("tf") or ctx.arg(1), str(_setting(ctx, "paper.auto_tf", "1h")))
    try:
        res = await paper.open_position(
            1, sym, side, tf, qty=ctx.flag_float("qty"), risk_pct=ctx.flag_float("risk"), sl=ctx.flag_float("sl"),
            tp=ctx.flag_float("tp"), force=bool(ctx.flag("force", False)), origin="manual",
        )
    except PaperRejected as rej:
        ctx.print(f"✖ NO TOMAR — orden {'de compra' if side == 'long' else 'de venta'} en {sym} bloqueada", "error")
        _print_gate(ctx, rej.gate, "warn")
        ctx.print("Si aun así quieres operar (no recomendado), repite la orden con --force", "muted")
        ctx.data = {"rejected": True, "gate": rej.gate}
        ctx.fail("Operación no abierta: el filtro dice NO TOMAR")
    except PaperError as exc:
        ctx.fail(str(exc))
    pos, gate = res["position"], res.get("gate")
    tps = " · ".join(f"TP{i} {fmt_price(pos.get(f'tp{i}'))}" for i in (1, 2, 3) if pos.get(f"tp{i}") is not None)
    ctx.print(f"✔ {'COMPRA' if side == 'long' else 'VENTA'} {sym}: {fmt_qty(pos['qty'])} @ {fmt_price(pos['entry_price'])} "
              f"(#{pos['id']})", "success")
    ctx.print(f"SL {fmt_price(pos['sl'])} (hasta aquí) · {tps}", "info")
    _print_gate(ctx, gate, "info")
    ctx.action("open_chart", symbol=sym, tf=tf)
    ctx.data = res


@command("buy", help="Compra (largo) en la cuenta demo, validada por el filtro",
         usage="buy <simbolo> [--risk 1] [--sl X] [--tp Y] [--qty Q] [--tf 1h] [--force]",
         group="Cuenta demo", aliases=("comprar",))
async def _buy(ctx: CommandContext) -> None:
    await _order(ctx, "long")


@command("sell", help="Venta en corto en la cuenta demo, validada por el filtro",
         usage="sell <simbolo> [--risk 1] [--sl X] [--tp Y] [--qty Q] [--tf 1h] [--force]",
         group="Cuenta demo", aliases=("vender",))
async def _sell(ctx: CommandContext) -> None:
    await _order(ctx, "short")


@command("close", help="Cierra posiciones de la cuenta demo a mercado", usage="close <simbolo|id|all>",
         group="Cuenta demo", aliases=("cerrar",))
async def _close(ctx: CommandContext) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    target = ctx.arg(0)
    if not target:
        ctx.fail("Indica qué cerrar: un símbolo, el id de la posición o 'all'")
    try:
        if str(target).lower() in ("all", "todo", "todas", "todos"):
            closed = await paper.close_position(all=True)
        elif str(target).isdigit():
            closed = await paper.close_position(int(target))
        else:
            closed = await paper.close_position(symbol=await ctx.symbol(target))
    except PaperError as exc:
        ctx.fail(str(exc))
    if not closed:
        ctx.print("No había posiciones abiertas", "muted")
    for p in closed:
        ctx.print(f"Cerrado #{p['id']} {p['symbol']} {SIDE_LABEL.get(p['side'], p['side'])} {fmt_r(p.get('r_multiple'))} "
                  f"({fmt_pct(p.get('pnl_pct'), 2)} · {fmt_num(p.get('pnl'), 2, True)})",
                  "success" if (p.get("pnl") or 0) > 0 else "warn")
    ctx.data = {"closed": closed}


@command("positions", help="Posiciones abiertas de la cuenta demo con el consejo de NEXUS", usage="positions",
         group="Cuenta demo", aliases=("posiciones", "pos"))
async def _positions(ctx: CommandContext) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    positions = paper.open_positions(1)
    if not positions:
        ctx.print("Sin posiciones abiertas. Abre una con: buy btc  (el filtro la valida antes)", "muted")
        ctx.data = []
        return
    rows = [[
        p["id"], p["symbol"], SIDE_LABEL.get(p["side"], p["side"]), fmt_qty(p["qty_open"]), fmt_price(p["entry_price"]),
        fmt_price(p.get("price")), fmt_price(p["trail"] if p.get("trail") is not None else p["sl"]),
        fmt_price(p.get("tp1")), fmt_num(p.get("pnl"), 2, True), fmt_r(p.get("r_multiple")), _short(p.get("advice"), 40),
    ] for p in positions]
    ctx.table(["ID", "Símbolo", "Lado", "Cant.", "Entrada", "Precio", "Stop", "TP1", "P&L", "R", "Consejo NEXUS"], rows)
    for p in positions:
        for w in (p.get("warnings") or [])[:2]:
            if w.get("severity") in ("warn", "critical"):
                ctx.print(f"⚠ #{p['id']} {p['symbol']}: {w.get('message')}", "warn")
    ctx.data = positions


@command("account", help="Resumen de la cuenta demo", usage="account", group="Cuenta demo", aliases=("cuenta",))
async def _account(ctx: CommandContext) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    a = await paper.account(1)
    ctx.print(f"Cuenta {a['name']} (demo, sin dinero real)", "system")
    ctx.table(["Concepto", "Valor"], [
        ["Saldo", fmt_num(a["balance"], 2)],
        ["Equity", fmt_num(a["equity"], 2)],
        ["Rentabilidad", fmt_pct(a["return_pct"], 2)],
        ["P&L realizado", fmt_num(a["realized_pnl"], 2, True)],
        ["P&L abierto", fmt_num(a["unrealized_pnl"], 2, True)],
        ["Operaciones", f"{a['trades']} ({a['wins']} ganadas / {a['losses']} perdidas)"],
        ["Acierto", fmt_prob(a.get("win_rate"))],
        ["Posiciones abiertas", str(a["open_positions"])],
        ["Auto-trading", ("sí · " + (", ".join(a["auto_symbols"]) or "sin símbolos") + f" · {a['auto_tf']}")
         if a["auto_trade"] else "no"],
    ])
    ctx.data = a


@command("auto", help="Auto-trading en la cuenta demo (solo abre señales TOMAR)",
         usage="auto on|off [simbolos...] [--tf 1h]", group="Cuenta demo")
async def _auto(ctx: CommandContext) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    sub = str(ctx.arg(0, "")).lower()
    if sub not in _ON and sub not in _OFF:
        a = await paper.account(1)
        ctx.print(f"Auto-trading {'ACTIVADO' if a['auto_trade'] else 'DESACTIVADO'} · símbolos: "
                  f"{', '.join(a['auto_symbols']) or '—'} · {a['auto_tf']}. Uso: auto on btc eth --tf 1h", "info")
        return
    symbols = [await ctx.symbol(s) for s in ctx.args[1:]] if len(ctx.args) > 1 else None
    tf = ctx.flag("tf")
    try:
        auto = await paper.set_auto(sub in _ON, symbols, tf)
    except (PaperError, ValueError) as exc:
        ctx.fail(str(exc))
    if auto["enabled"]:
        ctx.print(f"✔ Auto-trading activado: {', '.join(auto['symbols']) or 'añade símbolos (auto on btc eth)'} · "
                  f"{auto['tf']} · solo entra cuando el filtro dice TOMAR", "success")
        if not config.BACKGROUND:
            ctx.print("Nota: los bucles en segundo plano están deshabilitados en este proceso", "muted")
    else:
        ctx.print("Auto-trading desactivado (las posiciones abiertas siguen gestionándose)", "warn")
    ctx.data = auto


# ============================================================================= Aprendizaje


@command("predictions", help="Últimas predicciones y su acierto real (fuera de muestra)", usage="predictions [n]",
         group="Aprendizaje", aliases=("predicciones",))
async def _predictions(ctx: CommandContext) -> None:
    n = int(clamp(int(ctx.arg(0)), 1, 200)) if str(ctx.arg(0, "")).isdigit() else 20
    rows = await list_predictions(ctx.rt.db, limit=n)
    stats = await prediction_stats(ctx.rt.db)
    brier = f"{stats['brier']:.3f}" if stats.get("brier") is not None else "—"
    ctx.print(f"Predicciones: {stats['total']} · resueltas {stats['resolved']} · pendientes {stats['pending']} · "
              f"acierto {fmt_prob(stats.get('hit_rate'), 1)} · Brier {brier}", "learn")
    for src, s in (stats.get("by_source") or {}).items():
        ctx.print(f"  {src}: {s['resolved']} resueltas · acierto {fmt_prob(s.get('hit_rate'), 1)}", "muted")
    if not rows:
        ctx.print("Aún no hay predicciones: se crean con cada escaneo y con cada vela cerrada de la lista de seguimiento",
                  "muted")
        return
    table = []
    for p in rows:
        direction = {1: "↑", -1: "↓", 0: "="}.get(p["direction"], "=")
        if p["resolved"] and p["outcome_return_pct"] is not None:
            outcome = fmt_pct(p["outcome_return_pct"], 2)
            ok = "✔" if p["correct"] else "✖"
        elif p["resolved"]:
            outcome, ok = "sin precio", "—"
        else:
            mins = max(0, int((p["target_at"] - now_ms()) / 60000))
            outcome, ok = f"en {mins} min", "…"
        table.append([p["id"], p["source"], p["symbol"], p["tf"], direction, fmt_prob(p["prob_up"]),
                      fmt_price(p["price"]), outcome, ok])
    ctx.table(["ID", "Fuente", "Símbolo", "tf", "Dir.", "p↑", "Precio", "Resultado", "¿Acierto?"], table, max_rows=n)
    ctx.data = {"predictions": rows, "stats": stats}


@command("learning", help="Qué ha aprendido el sistema (escáner, NEXUS y filtro)", usage="learning",
         group="Aprendizaje", aliases=("aprendizaje",))
async def _learning(ctx: CommandContext) -> None:
    s = await learning_summary(ctx.rt.db, ctx.rt.settings.all() if ctx.rt.settings is not None else None)
    sm = s.get("scanner_model")
    if sm:
        brier = f"{sm['brier']:.3f}" if sm.get("brier") is not None else "—"
        ctx.print(f"Modelo del escáner {sm['key']}: {sm['updates']} actualizaciones · acierto "
                  f"{fmt_prob(sm.get('hit_rate'), 1)} · Brier {brier}", "learn")
        top = [f for f in sm.get("features") or [] if abs(f["weight"]) > 0][:5]
        for f in top:
            ctx.print(f"  {'↑' if f['weight'] > 0 else '↓'} {f['label']}: {fmt_num(f['weight'], 3, True)}", "muted")
        if not top:
            ctx.print("  Aún sin resultados resueltos: el modelo usa reglas técnicas mientras aprende", "muted")
    else:
        ctx.print("El escáner todavía no tiene modelo (ejecuta 'scan' y espera a que se resuelvan las predicciones)",
                  "muted")
    g = s["gate"]
    ctx.print(f"Filtro de operaciones: {g['decisions']} decisiones · TOMAR {g['taken']} (acierto "
              f"{fmt_prob(g.get('taken_win_rate'))}) · NO TOMAR {g['avoided']} (habrían perdido "
              f"{fmt_prob(g.get('avoided_would_lose_rate'))})", "learn")
    nm = s.get("nexus_models") or []
    if nm:
        ctx.print(f"Modelos NEXUS: {len(nm)}", "learn")
        rows = [[m["symbol"], m["tf"], m["updates"], fmt_prob(m.get("hit_rate"), 1),
                 f"{m['brier']:.3f}" if m.get("brier") is not None else "—"]
                for m in sorted(nm, key=lambda m: -m["updates"])[:8]]
        ctx.table(["Símbolo", "tf", "Actualizaciones", "Acierto", "Brier"], rows)
    p = s["predictions"]
    ctx.print(f"Predicciones: {p['resolved']} resueltas · acierto global {fmt_prob(p.get('hit_rate'), 1)}", "learn")
    tl = s.get("timeline") or []
    if tl:
        ctx.table(["Día", "Acierto", "Brier", "N"],
                  [[_day(t["time"]), fmt_prob(t["hit_rate"], 1), f"{t['brier']:.3f}", t["n"]] for t in tl[-7:]])
    ctx.print("Todas las cifras son fuera de muestra: cada predicción se guarda antes de conocer el resultado.", "muted")
    ctx.data = s

