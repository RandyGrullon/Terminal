"""Terminal commands of the simulation package (groups: Escáner, Simulación, Cuenta demo, Aprendizaje)."""
from __future__ import annotations

import asyncio
import re
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
from app.sim.evaluator import (
    REPLAY_DAYS_DEFAULT,
    ReplayBusy,
    learning_summary,
    list_predictions,
    prediction_stats,
    replay_report,
    skill_report,
)
from app.sim.montecarlo import MonteCarloError, run_montecarlo
from app.sim.paper import DEMO_NOTE, PaperError, PaperRejected, PaperRiskBlocked
from app.sim.scanner import CAL_MIN_LESSONS, CAL_MIN_ROWS, ScanError, is_opportunity
from app.sim.simulator import SimError
from app.terminal import CommandContext, command, register_bool_flags
from app.util import clamp, normalize_tf, now_ms, now_s

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
    if ctx.flag("take"):  # gate TOMAR with the NEXUS signal in that direction
        rows = [r for r in rows if is_opportunity(r)]
    ctx.print(f"Escaneo #{run['id']} · {run['market']} {run['tf']} · {_ago(run['started_at'])} · "
              f"{run['n_symbols']} activos · {run.get('message') or ''}", "scan")
    if not rows:
        ctx.print("Sin filas que mostrar" + (" (ninguna oportunidad TOMAR)" if ctx.flag("take") else ""), "muted")
        return
    table = []
    for r in rows[:n]:
        verdict = r.get("verdict")
        filt = f"{VERDICT_LABEL.get(verdict, verdict)} {r.get('gate_score') or 0:.0f}" if verdict else "—"
        if verdict == "TAKE" and r.get("opportunity") is False:
            filt = f"Favorable {r.get('gate_score') or 0:.0f} · sin señal NEXUS"
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
            fee_pct=None,  # the commission of the symbol's asset class (Ajustes: risk.fee_pct_by_class)
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
        wr, judged = m.get("rejected_would_win_rate"), m.get("rejected_resolved")
        detail = ""
        if wr is not None:
            detail = (f" · de las {judged} con resultado conocido habrían ganado {fmt_prob(wr)}" if judged
                      else f" · de las que tienen resultado conocido habrían ganado {fmt_prob(wr)}")
        ctx.print(f"Señales rechazadas por el filtro: {m['rejected_signals']}{detail}", "info")
    fee_note = next((n for n in (result.get("params") or {}).get("notes") or [] if str(n).startswith("Comisión")), None)
    if fee_note:
        ctx.print(fee_note, "muted")
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


# ----------------------------------------------------------------------------- NEXUS automática (account 3)

AUTOPILOT_MIN_N = 30  # closed trades before its win share is given as a percentage (no conclusion below 30)
_AP_LIST = ("lista", "list")
_AP_SCANNER = ("escaner", "escáner", "scanner")
_AP_BOTH = ("ambos", "both", "las-dos")
_AP_STATUS = ("status", "estado", "ver")
_AP_POSITIONS = ("pos", "positions", "posiciones")
_AP_LOG = ("log", "decisiones", "registro")
_AP_CLOSE = ("close", "cerrar")
_AP_RESET = ("reset", "reiniciar")
_AP_ALERTS = ("avisos", "alertas", "alerts")
_AP_COMPARE = ("azar", "comparar", "referencia")
_AP_JOURNAL = ("diario", "errores", "patrones", "journal")
_AP_WEEKLY = ("semana", "informe", "semanal")
_AP_KIND = {"open": ("Abre", "success"), "close": ("Cierra", "info"), "skip": ("Descarta", "muted"),
            "info": ("Nota", "info")}
# --instrumento: how it trades stocks and funds (autopilot.stock_instrument)
_AP_INSTRUMENTS = {"auto": "auto", "nexus": "auto", "acciones": "shares", "shares": "shares", "opciones": "options",
                   "options": "options"}
AUTOPILOT_USAGE = ("autopilot [on|off] [lista <símbolos…> | escaner [N] | ambos [N]] [--tf 1h] [--max 5] [--risk 1] "
                   "[--top 10] [--expo 100] [--freno 20] [--margen 50] [--instrumento auto|acciones|opciones] "
                   "[--prima 10] · "
                   "autopilot pos | log | azar | diario | semana | avisos on|off | close <id|símbolo|all> | "
                   "reset [saldo] --yes")


def _ap_when(ctx: CommandContext, ms: Any) -> str:
    from app.macro.fmt import day_clock

    return day_clock(int(ms) // 1000, now_s(), str(_setting(ctx, "ui.timezone", "") or ""))


def _ap_what(st: dict) -> str:
    listed = ", ".join(st["symbols"]) or "vacía (añade símbolos: autopilot lista btc eth)"
    if st["source"] == "list":
        return "lista fija: " + listed
    scan = f"hasta {st['scanner_top']} candidatos del último escaneo (primero las oportunidades TOMAR)"
    return f"lista ({listed}) y {scan}" if st["source"] == "both" else f"escáner: {scan}"


def _ap_wins(account: dict) -> str:
    """Winners as "x de n": a percentage only from 30 closed trades on (honest statistics)."""
    n, wins = int(account.get("trades") or 0), int(account.get("wins") or 0)
    if n <= 0:
        return "sin operaciones cerradas todavía"
    text = f"{wins} de {n}"
    return text + (f" ({fmt_prob(account.get('win_rate'))})" if n >= AUTOPILOT_MIN_N else " · aún sin evidencia suficiente")


def _ap_stocks_line(st: dict) -> str:
    """How it trades stocks and funds: autopilot.stock_instrument and the premium budget of its options."""
    mode, prem = st.get("stock_instrument") or "auto", float(st.get("options_premium_pct") or 0.0)
    opts = (f"opciones teóricas con hasta el {fmt_pct(prem, 0, False)} del capital en primas por operación" if prem
            else "sin opciones (--prima 0)")
    if mode == "shares":
        return "Acciones: compra acciones y también vende en corto (simulado) · --instrumento acciones"
    if mode == "options":
        return f"Acciones: solo opciones, un call para apostar al alza y un put a la baja; {opts}"
    return (f"Acciones: NEXUS elige, acciones para apostar al alza y un put a la baja (una cuenta de efectivo no vende "
            f"en corto); {opts}")


def _ap_cells(p: dict) -> tuple[str, str, str]:
    """(market, side, open quantity) of one of its positions: an option by its contract, a future in contracts."""
    inst = p.get("instrument") or {}
    mult = float(inst.get("multiplier") or 0.0)
    if inst.get("kind") == "option" and mult > 0:
        n = p["qty_open"] / mult
        return (str(inst.get("name") or p["symbol"]), "Call" if inst.get("right") == "call" else "Put",
                f"{n:g} {'contrato' if n == 1 else 'contratos'}")
    if inst.get("kind") == "future" and mult > 0:
        n = p["qty_open"] / mult
        return (p["symbol"], SIDE_LABEL.get(p["side"], p["side"]),
                f"{n:g} {'contrato' if n == 1 else 'contratos'} {inst.get('root') or ''}".strip())
    return p["symbol"], SIDE_LABEL.get(p["side"], p["side"]), fmt_qty(p["qty_open"])


def _print_autopilot(ctx: CommandContext, snap: dict) -> None:
    st, a = snap["autopilot"], snap["account"]
    ctx.print(f"NEXUS automática (cuenta demo 3, simulada) · {'ACTIVA: opera sola' if st['enabled'] else 'PAUSADA'}",
              "system")
    ctx.print(f"Qué vigila: {_ap_what(st)} · decide en velas de {st['tf']} · máximo {st['max_positions']} posiciones · "
              f"riesgo {fmt_pct(st['risk_pct'], 1, False)} por operación", "info")
    scan = st.get("scan")
    if st["source"] in ("scanner", "both"):
        if scan is None:
            ctx.print("Sin escaneos terminados todavía: vigilará los mejores candidatos del próximo", "muted")
        elif scan.get("finished_at") is None:
            ctx.print(f"Escaneo #{scan['id']} aún en curso", "muted")
        else:
            n = int(scan.get("opportunities") or 0)
            ctx.print(f"Escaneo #{scan['id']} terminado {_ago(scan['finished_at'])}: {n} "
                      f"{'oportunidad' if n == 1 else 'oportunidades'} TOMAR", "muted")
    watching = st.get("watching") or []
    if watching:
        rows = []
        for w in watching:
            last = w.get("last")
            read = f"{last['label']} · vela de {_ap_when(ctx, last['bar_time'])}" if last else "—"
            rows.append([w["symbol"], w["why"], SIDE_LABEL.get(w.get("direction") or "", "según la señal"), read])
        ctx.table(["Símbolo", "Motivo", "Dirección", "Última vela"], rows)
    elif st["enabled"]:
        ctx.print("Ahora mismo no vigila ningún símbolo", "muted")
    expo = st.get("max_exposure_pct") or 0
    freno = st.get("max_drawdown_pct") or 0
    ctx.print(("Cuenta de efectivo: hasta el " + fmt_pct(expo, 0, False) + " de su capital invertido" if expo
               else "Sin tope de exposición propio") + " · freno de pérdidas: "
              + (fmt_pct(freno, 0, False) + " desde el saldo inicial" if freno else "desactivado"), "info")
    fm = st.get("futures_margin_pct") or 0
    ctx.print(("Futuros: contratos enteros, con hasta el " + fmt_pct(fm, 0, False) + " del capital como margen aproximado"
               if fm else "No opera futuros (--margen 0)"), "muted")
    ctx.print(_ap_stocks_line(st), "muted")
    health = st.get("health") or {}
    if health.get("text"):
        ctx.print(("✔ " if health.get("ok") else "⚠ ") + health["text"], "info" if health.get("ok") else "warn")
    down = health.get("downtime")
    if down:
        ctx.print(f"Estuvo detenida de {_ap_when(ctx, down['from'])} a {_ap_when(ctx, down['to'])} (servidor apagado o PC "
                  "suspendido)", "warn")
    brake = st.get("brake") or {}
    if brake.get("active"):
        ctx.print(f"⛔ Freno de pérdidas activo: la cuenta está un {fmt_pct(brake.get('drawdown_pct'), 1, False)} por debajo "
                  "de su saldo inicial; no abre operaciones nuevas (reiníciala o cambia --freno)", "warn")
    ctx.print(_ap_alerts_line(st), "info" if st.get("alerts") else "muted")
    ctx.print(f"Saldo {fmt_num(a['balance'], 2)} · equity {fmt_num(a['equity'], 2)} · rentabilidad "
              f"{fmt_pct(a['return_pct'], 2)} · ganadoras {_ap_wins(a)} · posiciones abiertas {a['open_positions']}",
              "info")
    if st.get("note"):
        ctx.print(st["note"], "muted")


def _ap_alerts_line(st: dict) -> str:
    n = int(st.get("alert_channels") or 0)
    if not st.get("alerts"):
        return "Avisos por Telegram o correo: desactivados (autopilot avisos on)"
    if n <= 0:
        return ("Avisos por Telegram o correo: activados, pero ningún canal está activo (configúralo en Ajustes → "
                "Alertas)")
    return f"Avisos por Telegram o correo: activados ({n} {'canal activo' if n == 1 else 'canales activos'})"


def _ap_number(ctx: CommandContext, flag: str, what: str, *, zero: bool = False) -> float | None:
    value = ctx.flag(flag)
    if value is None:
        return None
    if zero and str(value).strip() in ("0", "0,0", "0.0"):
        return 0.0  # 0 switches that limit off
    return _number(ctx, value, what, flag=f"--{flag}")


@command("autopilot", help="NEXUS automática: la cuenta demo propia de NEXUS, que opera sola (simulada)",
         usage=AUTOPILOT_USAGE, group="Cuenta demo", aliases=("piloto", "automatica", "automática"))
async def _autopilot(ctx: CommandContext) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    args = [str(a) for a in ctx.args]
    sub = args[0].lower() if args else ""

    if sub in _AP_POSITIONS:
        positions = paper.open_positions(3)
        if not positions:
            on = paper.autopilot_status()["enabled"]
            ctx.print("NEXUS no tiene posiciones abiertas" + ("" if on else " (está pausada: autopilot on)"), "muted")
        else:
            ctx.table(["ID", "Símbolo", "Lado", "Cant.", "Entrada", "Precio", "Stop", "TP1", "P&L", "R"], [[
                p["id"], *_ap_cells(p), fmt_price(p["entry_price"]), fmt_price(p.get("price")),
                fmt_price(p["trail"] if p.get("trail") is not None else p["sl"]), fmt_price(p.get("tp1")),
                fmt_num(p.get("pnl"), 2, True), fmt_r(p.get("r_multiple")),
            ] for p in positions])
            ctx.print("Ciérralas a mano con: autopilot close <id|símbolo|all>", "muted")
            if any((p.get("instrument") or {}).get("kind") == "option" for p in positions):
                ctx.print("Opciones: precio teórico (Black-Scholes con la volatilidad histórica de 20 días), por "
                          "acción; cada contrato son 100 acciones", "muted")
        ctx.data = positions
        return

    if sub in _AP_LOG:
        log = paper.autopilot_status()["log"]
        if not log:
            ctx.print("Sin decisiones todavía: aparecerán al cerrar la próxima vela de los símbolos que vigila "
                      "(las velas sin señal de entrada no se anotan: mira «Última vela» en autopilot)", "muted")
        for e in reversed(log):  # oldest first, like the terminal's own output
            label, level = _AP_KIND.get(e["kind"], ("Nota", "info"))
            sym = "" if e["symbol"] in ("", "—") else f" {e['symbol']}"
            ctx.print(f"{_ago(e['ts'])} · {label}{sym}: {e['text']}", level)
        ctx.data = log
        return

    if sub in _AP_CLOSE:
        target = args[1] if len(args) > 1 else None
        if not target:
            ctx.fail("Indica qué cerrar: el id de la posición, un símbolo o 'all'")
        try:
            if target.lower() in _ALL:
                closed = await paper.close_autopilot(all=True)
            elif target.lstrip("#").isdigit():
                closed = await paper.close_autopilot(int(target.lstrip("#")))
            else:
                closed = await paper.close_autopilot(symbol=await ctx.symbol(target))
        except PaperError as exc:
            ctx.fail(str(exc))
        if not closed:
            ctx.print("NEXUS no tenía posiciones abiertas", "muted")
        for p in closed:
            market, side, _ = _ap_cells(p)
            ctx.print(f"Cerrado #{p['id']} {market} {side} de NEXUS "
                      f"{fmt_r(p.get('r_multiple'))} ({fmt_pct(p.get('pnl_pct'), 2)} · {fmt_num(p.get('pnl'), 2, True)})",
                      "success" if (p.get("pnl") or 0) > 0 else "warn")
        ctx.data = {"closed": closed}
        return

    if sub in _AP_COMPARE:
        c = await paper.autopilot_comparison(fresh=bool(ctx.flag("fresh", False)))
        tone = {"better": "success", "worse": "warn"}.get(c["verdict"], "info")
        ctx.print(f"NEXUS automática frente a entrar al azar · {c['verdict_label']}", "system")
        ctx.print(c["text"], tone)
        if c["compared"]:
            nx, rnd = c["nexus"], c["random"]
            ctx.table(["", "R medio por operación", "Ganadoras"], [
                ["NEXUS automática", fmt_r(nx["mean_r"], 2), f"{nx['wins']} de {nx['n']}"],
                ["Entrar al azar", fmt_r(rnd["mean_r"], 2), f"{fmt_num(rnd['wins'], 0)} de {fmt_num(rnd['entries'], 0)} "
                                                             "entradas"],
            ])
            ci = c.get("ci95")
            ctx.print(f"Diferencia: {fmt_r(c['diff_r'], 2)} por operación · IC 95 %: "
                      + (f"{fmt_r(ci[0], 2)} a {fmt_r(ci[1], 2)}" if ci else "sin intervalo")
                      + f" · {c['compared']} de {c['trades']} operaciones con referencia, en {c['days']} "
                      + ("día" if c["days"] == 1 else "días"), "info")
        ctx.print(c["note"], "muted")
        ctx.data = c
        return

    if sub in _AP_WEEKLY:
        st = paper.autopilot_status()
        weekly = st.get("weekly")
        if weekly:
            ctx.print("Informe semanal de NEXUS automática", "system")
            ctx.print(weekly["text"], "info")
        else:
            ctx.print("Todavía no hay informe semanal: se hace los lunes (00:00 UTC) con la semana que terminó, si "
                      "NEXUS automática estuvo activa.", "muted")
        ctx.data = weekly
        return

    if sub in _AP_JOURNAL:
        j = await paper.autopilot_journal(fresh=bool(ctx.flag("fresh", False)))
        ctx.print(f"Diario de NEXUS automática · {j['verdict_label']}", "system")
        if j["trades"]:
            ci = j.get("ci95")
            ctx.print(f"{j['wins']} de {j['trades']} ganadoras · R medio {fmt_r(j['mean_r'], 2)} · IC 95 %: "
                      + (f"{fmt_r(ci[0], 2)} a {fmt_r(ci[1], 2)}" if ci else "sin intervalo"), "info")
        for line in j["insights"]:
            ctx.print("• " + line, "info")
        for g in j["groups"]:
            if not g["buckets"]:
                continue
            ctx.print(g["label"], "system")
            ctx.table(["", "Ganadoras", "R medio", "Veredicto"], [
                [b["label"], f"{b['wins']} de {b['n']}", fmt_r(b["mean_r"], 2), b["verdict_label"]]
                for b in g["buckets"]])
        ctx.print(j["note"], "muted")
        ctx.data = j
        return

    if sub in _AP_ALERTS:
        word = args[1].lower() if len(args) > 1 else ""
        if word not in _ON and word not in _OFF:
            ctx.fail("Indica on u off. Ejemplo: autopilot avisos on")
        try:
            snap = await paper.set_autopilot(alerts=word in _ON)
        except PaperError as exc:
            ctx.fail(str(exc))
        st = snap["autopilot"]
        ok = st["alerts"] and int(st.get("alert_channels") or 0) > 0
        ctx.print(("✔ " if ok or not st["alerts"] else "⚠ ") + _ap_alerts_line(st),
                  "success" if ok else ("warn" if st["alerts"] else "info"))
        if st["alerts"]:
            ctx.print("Se avisa de sus aperturas, cierres y stops (operaciones simuladas), según el nivel de avisos de "
                      "Ajustes → Alertas", "muted")
        ctx.data = snap
        return

    if sub in _AP_RESET:
        amount = _number(ctx, args[1], "el saldo inicial") if len(args) > 1 else None
        if not ctx.flag("yes"):
            ctx.print("Esto borra las posiciones, el historial y las decisiones de la cuenta de NEXUS (tu cuenta no "
                      "cambia y lo aprendido por NEXUS y el filtro no se pierde). Repite con --yes para confirmar.",
                      "warn")
            return
        try:
            await paper.reset(amount, account_id=3)
        except PaperError as exc:
            ctx.fail(str(exc))
        snap = await paper.autopilot_snapshot()
        ctx.print(f"✔ Cuenta de NEXUS reiniciada con saldo {fmt_num(snap['account']['balance'], 2)}", "success")
        ctx.data = snap
        return

    # settings: on / off, a source with its symbols or candidates, and flags, in any combination
    update: dict[str, Any] = {}
    rest = args
    if sub in _ON or sub in _OFF:
        update["enabled"] = sub in _ON
        rest = args[1:]
    elif sub in _AP_STATUS:
        rest = args[1:]
    if rest:
        head = rest[0].lower()
        if head in _AP_SCANNER or head in _AP_BOTH:
            update["source"] = "scanner" if head in _AP_SCANNER else "both"
            if len(rest) > 2:
                ctx.fail(f"Sobra «{rest[2]}». Uso: autopilot {head} [N]")
            if len(rest) == 2:
                update["scanner_top"] = _number(ctx, rest[1], "el número de candidatos del escáner")
        elif head in _AP_LIST or "enabled" in update:  # autopilot on btc eth = a fixed list, like `auto on btc eth`
            symbols = rest[1:] if head in _AP_LIST else rest
            if not symbols:
                ctx.fail("Faltan los símbolos de la lista. Ejemplo: autopilot lista btc eth sol")
            update["source"] = "list"
            update["symbols"] = [await ctx.symbol(s) for s in symbols]
        else:
            ctx.fail(f"No entiendo «{rest[0]}». Uso: {AUTOPILOT_USAGE}")
    if ctx.flag("tf") is not None:
        update["tf"] = _tf(ctx, str(ctx.flag("tf")))
    for flag, key, what, zero in (("max", "max_positions", "el máximo de posiciones", False),
                                  ("risk", "risk_pct", "el riesgo por operación", False),
                                  ("top", "scanner_top", "el número de candidatos del escáner", False),
                                  ("expo", "max_exposure_pct", "la exposición máxima", True),
                                  ("freno", "max_drawdown_pct", "el freno de pérdidas", True),
                                  ("margen", "futures_margin_pct", "el margen máximo en futuros", True),
                                  ("prima", "options_premium_pct", "la prima máxima por operación", True)):
        value = _ap_number(ctx, flag, what, zero=zero)
        if value is not None:
            update[key] = value
    instr = ctx.flag("instrumento")
    if instr is not None:
        mode = _AP_INSTRUMENTS.get(str(instr).strip().lower())
        if mode is None:
            ctx.fail("Indica --instrumento auto (NEXUS elige), acciones u opciones")
        update["stock_instrument"] = mode
    try:
        snap = await paper.set_autopilot(**update) if update else await paper.autopilot_snapshot()
    except PaperError as exc:
        ctx.fail(str(exc))
    st = snap["autopilot"]
    if update.get("enabled") is True:
        if st["source"] == "list":
            what = (f"su lista fija ({', '.join(st['symbols'])})" if st["symbols"]
                    else "su lista fija, todavía vacía (añade símbolos: autopilot lista btc eth)")
        else:
            scan_what = f"los mejores candidatos del último escaneo (hasta {st['scanner_top']})"
            what = (f"su lista ({', '.join(st['symbols'])}) y {scan_what}" if st["source"] == "both" and st["symbols"]
                    else scan_what)
        ctx.print(f"✔ NEXUS automática activada: opera sola en su cuenta demo (simulada) con {what}. Solo entra con "
                  f"señal de NEXUS y filtro TOMAR, al cierre de cada vela de {st['tf']}, y solo mientras el servidor "
                  "está en marcha", "success")
        if not config.BACKGROUND:
            ctx.print("Nota: los bucles en segundo plano están deshabilitados en este proceso", "muted")
    elif update.get("enabled") is False:
        ctx.print("NEXUS automática pausada: no abre posiciones nuevas; las abiertas siguen con su stop y sus objetivos",
                  "warn")
    elif update:
        ctx.print("✔ Cambios de NEXUS automática guardados", "success")
    _print_autopilot(ctx, snap)
    ctx.data = snap


# ----------------------------------------------------------------------------- pending orders (limit / stop)

_NUM_PLAIN = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")  # 95000 · 95000.5 · 1.0850
_NUM_ES = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+,\d+")  # 95.000,50 · 1.234.567 · 1,0850
_NUM_AMBIGUOUS = re.compile(r"\d{1,3},\d{3}")  # 95,000: thousands (English) or decimals (Spanish)?
_ALL = ("all", "todo", "todas", "todos")


def _number(ctx: CommandContext, value: Any, what: str, *, flag: str | None = None,
            required: bool = False) -> float | None:
    """A positive number typed in the terminal: decimal point (95000.5) or the Spanish format the terminal prints
    (95.000,50 · 1,0850). An ambiguous or invalid value fails instead of being ignored."""
    if value is None:
        if required:
            ctx.fail(f"Falta {what}")
        return None
    text = str(value).strip().replace(" ", "")
    if value is True or not text:
        ctx.fail(f"Falta el valor de {flag}" if flag else f"Falta {what}")
    what = f"{what} ({flag})" if flag else what
    if _NUM_PLAIN.fullmatch(text):
        v = float(text)
    elif _NUM_ES.fullmatch(text) and not _NUM_AMBIGUOUS.fullmatch(text):
        v = float(text.replace(".", "").replace(",", "."))
    else:
        hint = " (¿miles o decimales? escríbelo sin separador de miles: 95000 o 95.5)" if _NUM_AMBIGUOUS.fullmatch(
            text) else " (usa un número como 95000.5)"
        ctx.fail(f"Valor no válido para {what}: {value}{hint}")
    if not v > 0 or v == float("inf"):
        ctx.fail(f"{what[0].upper()}{what[1:]} debe ser un número mayor que 0")
    return v


def _expiry(ctx: CommandContext, value: Any) -> dict[str, Any]:
    """--exp of a pending order → place_order arguments: gtc (default, until cancelled) · N or Nv (N bars of the
    order's timeframe) · 30m / 4h / 2d (from now) · YYYY-MM-DD [HH:MM] (in the ui.timezone zone; a bare date means
    the end of that day)."""
    if value is None:
        return {}
    text = str(value).strip().lower()
    if value is True or not text:
        ctx.fail("Falta el valor de --exp: gtc, 12 (velas), 4h, 2d o una fecha AAAA-MM-DD")
    if text in ("gtc", "cancelar", "hasta-cancelar", "nunca"):
        return {"expiry": "gtc"}
    m = re.fullmatch(r"(\d+)\s*(?:v|vela|velas|b|bar|bars)?", text)
    if m:
        n = int(m.group(1))
        if n >= 1_000_000_000:  # unix time (s or ms)
            return {"expiry": "date", "expire_time": n}
        return {"expiry": "bars", "expire_bars": n}
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(m|min|h|d)", text)
    if m:
        amount = float(m.group(1).replace(",", "."))
        return {"expiry": "date", "expire_time": now_s() + int(amount * {"m": 60, "min": 60, "h": 3600, "d": 86400}[
            m.group(2)])}
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{1,2}):(\d{2}))?", text)
    if m:
        from app.macro.fmt import TZ_SETTING, zone

        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (23, 59)
        try:
            local = datetime(y, mo, d, hh, mm)
        except ValueError:
            ctx.fail(f"Fecha no válida en --exp: {value}")
        tz = zone(_setting(ctx, TZ_SETTING, ""))
        aware = local.replace(tzinfo=tz) if tz is not None else local.astimezone()
        return {"expiry": "date", "expire_time": int(aware.timestamp())}
    ctx.fail(f"Caducidad no válida: {value}. Usa gtc (hasta cancelarla), 12 (velas del tf), 4h, 2d o AAAA-MM-DD [HH:MM]")
    return {}  # unreachable (ctx.fail raises)


def _order_size(o: dict) -> str:
    if o.get("qty") is not None:
        return fmt_qty(o["qty"])
    return f"riesgo {fmt_num(o['risk_pct'], 2)} %" if o.get("risk_pct") is not None else "riesgo de Ajustes"


def _order_gate(o: dict) -> str:
    verdict = o.get("gate_verdict")
    if not verdict:
        return "sin filtro"
    score = o.get("gate_score")
    text = VERDICT_LABEL.get(verdict, verdict) + (f" {fmt_num(score, 0)}" if score is not None else "")
    return text + (" (forzada)" if o.get("forced") else "")


def _order_levels(o: dict, compact: bool = False) -> str:
    """The order's own SL / target, or who sets them at the fill (NEXUS, relative to the fill price)."""
    sl, tp = o.get("sl"), o.get("tp")
    if sl is None and tp is None:
        return "niveles de NEXUS" if compact else "SL y objetivos de NEXUS al ejecutarse"
    if compact:
        return (f"SL {fmt_price(sl)}" if sl is not None else "SL NEXUS") + " · " + (
            f"obj. {fmt_price(tp)}" if tp is not None else "obj. NEXUS")
    parts = [f"SL {fmt_price(sl)}" if sl is not None else "", f"objetivo {fmt_price(tp)}" if tp is not None else ""]
    text = " · ".join(p for p in parts if p)
    if sl is None:
        return text + " · SL de NEXUS al ejecutarse"
    if tp is None:
        return text + " · objetivos de NEXUS al ejecutarse"
    return text


_SIDE_WORDS = {"long": "long", "largo": "long", "compra": "long", "comprar": "long", "buy": "long",
               "short": "short", "corto": "short", "venta": "short", "vender": "short", "sell": "short"}


async def _pending(ctx: CommandContext, kind: str) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    if len(ctx.args) < 2:
        hint = " (para detener el simulador usa: sim stop)" if kind == "stop" and not ctx.args else ""
        ctx.fail(f"Uso: {kind} <símbolo> <precio> [--short] [--sl X] [--tp Y] [--exp gtc|12v|4h|AAAA-MM-DD]{hint}")
    sides = {s for s in ("long", "short") if ctx.flag(s)}
    tf_text = ctx.flag("tf")
    for extra in ctx.args[2:]:  # never ignore a word silently (e.g. a side typed without --)
        word = str(extra).lower()
        if word in _SIDE_WORDS:
            sides.add(_SIDE_WORDS[word])
        elif _is_tf(word) and not tf_text:
            tf_text = word
        else:
            ctx.fail(f"Argumento no reconocido: {extra}. Uso: {kind} <símbolo> <precio> [--short] [--sl X] [--tp Y]")
    if len(sides) > 1:
        ctx.fail("Indica un solo lado: compra (por defecto, o --long) o venta (--short)")
    side = sides.pop() if sides else "long"
    price = _number(ctx, ctx.arg(1), "el precio de la orden", required=True)
    sl = _number(ctx, ctx.flag("sl"), "el stop", flag="--sl")
    tp = _number(ctx, ctx.flag("tp"), "el objetivo", flag="--tp")
    qty = _number(ctx, ctx.flag("qty"), "la cantidad", flag="--qty")
    risk = _number(ctx, ctx.flag("risk"), "el riesgo por operación (%)", flag="--risk")
    expiry = _expiry(ctx, ctx.flag("exp"))
    sym = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, tf_text, str(_setting(ctx, "paper.auto_tf", "1h")))
    what = f"{'compra' if side == 'long' else 'venta'} {'límite' if kind == 'limit' else 'stop'}"
    try:
        res = await paper.place_order(1, sym, side, kind, price, tf, qty=qty, risk_pct=risk, sl=sl, tp=tp,
                                      force=bool(ctx.flag("force", False)), origin="manual", **expiry)
    except PaperRejected as rej:
        ctx.print(f"✖ NO TOMAR — {what} en {sym} a {fmt_price(price)} bloqueada", "error")
        _print_gate(ctx, rej.gate, "warn")
        ctx.print("Si aun así quieres dejarla (no recomendado), repite la orden con --force: al ejecutarse el filtro se "
                  "vuelve a consultar y un NO TOMAR entonces la cancela.", "muted")
        ctx.data = {"rejected": True, "gate": rej.gate}
        ctx.fail("Orden no creada: el filtro dice NO TOMAR")
    except PaperRiskBlocked as blk:
        ctx.print(f"✖ {blk.message}", "error")
        ctx.print("Los límites de riesgo de la cuenta demo no se saltan con --force (se cambian en Ajustes › Riesgo de "
                  "cartera). Detalle: risk", "muted")
        ctx.data = {"blocked": True, "rule": blk.rule, "risk": blk.risk}
        ctx.fail("Orden no creada: límite de riesgo de la cuenta demo")
    except PaperError as exc:
        ctx.fail(str(exc))
    o, gate = res["order"], res.get("gate")
    ctx.print(f"✔ Orden demo #{o['id']}: {o['label'].lower()} {sym} a {fmt_price(o['price'])} · {o['expiry_text']}",
              "success")
    market = o.get("market_price")
    ctx.print(f"Precio actual {fmt_price(market)}" + (f" (orden a {fmt_pct(o['distance_pct'], 2)})" if
                                                       o.get("distance_pct") is not None else "")
              + f" · {_order_levels(o)} · tamaño: {_order_size(o)} · tf {o['tf']}", "info")
    if o.get("distance_pct") is not None and abs(float(o["distance_pct"])) >= 50:
        ctx.print("⚠ La orden está muy lejos del precio actual: revisa el precio (punto decimal, sin separador de "
                  "miles: 95000.5).", "warn")
    _print_gate(ctx, gate, "info")
    if o.get("forced"):
        ctx.print("⚠ Orden FORZADA contra el filtro (NO TOMAR al dejarla)", "warn")
    ctx.print("Al llegar a su precio se vuelve a consultar el filtro al precio de ejecución: si entonces dice NO TOMAR, "
              "la orden se cancela.", "muted")
    if kind == "limit":
        ctx.print("En esta demo una orden límite se ejecuta en cuanto el precio la toca; en un mercado real tocar el "
                  "precio no garantiza la ejecución.", "muted")
    ctx.print(f"{DEMO_NOTE}. Cancélala con: cancel {o['id']}", "muted")
    ctx.action("open_chart", symbol=sym, tf=tf)
    ctx.data = res


@command("limit", help="Deja una orden límite pendiente en la cuenta demo (compra por debajo / venta por encima)",
         usage="limit <simbolo> <precio> [--short] [--sl X] [--tp Y] [--exp gtc|12v|4h|AAAA-MM-DD] [--qty Q] "
               "[--risk 1] [--tf 1h] [--force]",
         group="Cuenta demo", aliases=("limite", "límite"))
async def _limit(ctx: CommandContext) -> None:
    await _pending(ctx, "limit")


@command("stop", help="Deja una orden stop pendiente en la cuenta demo (compra por encima / venta por debajo)",
         usage="stop <simbolo> <precio> [--short] [--sl X] [--tp Y] [--exp gtc|12v|4h|AAAA-MM-DD] [--qty Q] "
               "[--risk 1] [--tf 1h] [--force]",
         group="Cuenta demo")
async def _stop_order(ctx: CommandContext) -> None:
    await _pending(ctx, "stop")


@command("orders", help="Órdenes pendientes (límite / stop) de la cuenta demo", usage="orders [all]",
         group="Cuenta demo", aliases=("ordenes", "órdenes"))
async def _orders(ctx: CommandContext) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    show_all = bool(ctx.flag("all")) or str(ctx.arg(0, "")).lower() in _ALL + ("historial",)
    orders = await paper.list_orders(1, "all" if show_all else "pending")
    pending = sorted((o for o in orders if o["status"] == "pending"), key=lambda o: o["id"])
    if pending:
        ctx.table(["ID", "Símbolo", "Orden", "Precio", "Mercado", "Distancia", "Niveles", "Tamaño", "Validez", "Filtro"], [[
            o["id"], o["symbol"], o["label"] + (" (ejecutándose)" if o.get("executing") else ""), fmt_price(o["price"]),
            fmt_price(o.get("market_price")), fmt_pct(o.get("distance_pct"), 2), _order_levels(o, compact=True),
            _order_size(o), _short(str(o["expiry_text"]).removeprefix("válida "), 40), _order_gate(o),
        ] for o in pending])
        ctx.print("Al llegar a su precio se vuelve a consultar el filtro; un NO TOMAR entonces cancela la orden. "
                  "Cancela con: cancel <id|símbolo|all>", "muted")
    else:
        ctx.print("Sin órdenes pendientes. Deja una con: limit btc 95000 --sl 93000  (o stop btc 99000)", "muted")
    if show_all:
        closed = [o for o in orders if o["status"] != "pending"]
        if closed:
            ctx.print("Últimas órdenes cerradas:", "info")
            ctx.table(["ID", "Símbolo", "Orden", "Precio", "Estado", "Detalle"], [[
                o["id"], o["symbol"], o["label"], fmt_price(o["price"]), o["status_label"],
                _short(f"ejecutada a {fmt_price(o['fill_price'])} · posición #{o['position_id']}" if o["status"] == "filled"
                       else o.get("reason") or "—", 60),
            ] for o in closed], max_rows=20)
        else:
            ctx.print("No hay órdenes cerradas todavía", "muted")
    ctx.print(f"{DEMO_NOTE}.", "muted")
    ctx.data = {"orders": orders}


@command("cancel", help="Cancela órdenes pendientes de la cuenta demo", usage="cancel <id|simbolo|all>",
         group="Cuenta demo", aliases=("cancelar",))
async def _cancel(ctx: CommandContext) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    target = ctx.arg(0) or ("all" if ctx.flag("all") else None)
    if not target:
        ctx.fail("Indica qué cancelar: el id de la orden, un símbolo o 'all' (mira 'orders')")
    text = str(target).strip()
    try:
        if text.lower() in _ALL:
            cancelled = await paper.cancel_orders(all=True)
        elif text.lstrip("#").isdigit():
            cancelled = await paper.cancel_orders(int(text.lstrip("#")))
        else:
            cancelled = await paper.cancel_orders(symbol=await ctx.symbol(text))
    except PaperError as exc:
        ctx.fail(str(exc))
    if not cancelled:
        ctx.print("No había órdenes pendientes", "muted")
    for o in cancelled:
        ctx.print(f"Cancelada #{o['id']}: {o['label'].lower()} {o['symbol']} a {fmt_price(o['price'])}", "success")
    ctx.data = {"cancelled": cancelled}


@command("risk", help="Riesgo de cartera de la cuenta demo (riesgo abierto, grupos, pérdida diaria)", usage="risk",
         group="Cuenta demo", aliases=("riesgo",))
async def _risk(ctx: CommandContext) -> None:
    paper = _svc(ctx, "paper", "La cuenta demo")
    r = await paper.risk_state(1)
    cap, per_group, loss = r.get("max_open_risk_pct"), r.get("max_positions_per_group"), r.get("daily_loss_limit")
    ctx.print("Riesgo de cartera de la cuenta demo (simulada)", "system")
    ctx.table(["Concepto", "Valor"], [
        ["Riesgo abierto (entrada → stop)", f"{fmt_num(r['open_risk'], 2)} ({fmt_pct(r.get('open_risk_pct'), 2, False)} "
                                             "del equity) · " + (f"límite {fmt_pct(cap, 1, False)}" if cap else "sin límite")],
        ["Posiciones abiertas", f"{r['open_positions']} · " + (f"máximo {per_group} por grupo" if per_group
                                                               else "sin límite por grupo")],
        ["P&L realizado hoy (día UTC)", f"{fmt_num(r['daily_pnl'], 2, True)} ({fmt_pct(r.get('daily_pnl_pct'), 2)})"],
        ["Límite de pérdida diaria", f"{fmt_num(-float(loss), 2, True)} ({fmt_pct(r.get('daily_loss_limit_pct'), 1, False)}"
                                     " del saldo al empezar el día)" if loss is not None else "sin límite"],
        ["Peor P&L realizado del día", fmt_num(r.get("daily_low"), 2, True)],
        ["P&L abierto (no cuenta en el límite)", fmt_num(r.get("unrealized_pnl"), 2, True)],
        ["Órdenes pendientes", str(r.get("pending_orders", 0))],
    ])
    for g in r.get("groups") or []:
        n = int(g.get("positions") or 0)
        count = f"{n} {'posición' if n == 1 else 'posiciones'}" + ("" if g.get("max") is None else f" de {g['max']}")
        ctx.print(f"  {g['label']}: {count} · riesgo {fmt_num(g['risk'], 2)} · {', '.join(g.get('symbols') or [])}"
                  + (" · lleno" if g.get("full") else ""), "warn" if g.get("full") else "muted")
    if r.get("blocked"):
        ctx.print(f"⛔ {r.get('block_reason') or 'Límite de pérdida diaria alcanzado'}", "warn")
    if r.get("note"):
        ctx.print(r["note"], "muted")
    ctx.data = r


# ============================================================================= Aprendizaje


@command("predictions", help="Últimas predicciones y su acierto real (fuera de muestra)", usage="predictions [n]",
         group="Aprendizaje", aliases=("predicciones",))
async def _predictions(ctx: CommandContext) -> None:
    n = int(clamp(int(ctx.arg(0)), 1, 200)) if str(ctx.arg(0, "")).isdigit() else 20
    rows = await list_predictions(ctx.rt.db, limit=n)
    stats = await prediction_stats(ctx.rt.db)
    brier = fmt_num(stats.get("brier"), 3)
    ctx.print(f"Predicciones: {stats['total']} · resueltas {stats['resolved']} · pendientes {stats['pending']} · "
              f"acierto {fmt_prob(stats.get('hit_rate'), 1)} · Brier {brier}", "learn")
    for src, s in (stats.get("by_source") or {}).items():
        ctx.print(f"  {src}: {s['resolved']} resueltas · acierto {fmt_prob(s.get('hit_rate'), 1)}", "muted")
    report = await skill_report(ctx.rt.db, days=None)
    overall = next((g for g in report["groups"] if g["id"] == "all"), None) or (report["groups"] or [None])[0]
    if overall and overall.get("n"):
        ctx.print(f"Frente a la referencia (filas guardadas): {overall['verdict_text']}", _verdict_level(overall))
    ctx.print("Hay tres resultados posibles (sube, baja o neutral), así que el 50 % no es el azar: compara con la "
              "referencia ('skill').", "muted")
    if not rows:
        ctx.print("Aún no hay predicciones: se crean con cada escaneo y con cada vela cerrada de la lista de seguimiento",
                  "muted")
        return
    table = []
    for p in rows:
        direction = {1: "↑", -1: "↓", 0: "="}.get(p["direction"], "=")
        if p["resolved"] and p["outcome_return_pct"] is not None:
            outcome = fmt_pct(p["outcome_return_pct"], 2)
            ok = "—" if p["correct"] is None else "✔" if p["correct"] else "✖"  # — = move too small to judge
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
    report = await skill_report(ctx.rt.db, days=7)
    groups = {g["id"]: g for g in report["groups"]}
    sm = s.get("scanner_model")
    if sm:
        brier = fmt_num(sm.get("brier"), 3)
        ctx.print(f"Modelo del escáner {sm['key']}: {sm['updates']} actualizaciones · acierto "
                  f"{fmt_prob(sm.get('hit_rate'), 1)} · Brier {brier} (histórico)", "learn")
        model = ctx.rt.scanner.get_model_sync(sm["key"]) if ctx.rt.scanner is not None else None
        if model is not None and hasattr(model, "status_text"):
            cal = model.cal_dir
            cal_txt = ("recalibración Platt activa" if cal.active() else
                       f"recalibración Platt pendiente ({fmt_num(cal.rows, 0)}/{fmt_num(CAL_MIN_ROWS, 0)} "
                       f"resultados, {fmt_num(cal.lessons, 1)}/{fmt_num(CAL_MIN_LESSONS, 0)} ventanas)")
            ctx.print(f"  {model.status_text()} · {cal_txt}", "muted")
        g = groups.get(f"scanner:{sm['key']}")
        if g and g.get("n"):
            ctx.print(f"  Últimos 7 días: {g['verdict_text']}", _verdict_level(g))
        top = [f for f in sm.get("features") or [] if abs(f["weight"]) > 0][:5]
        for f in top:
            ctx.print(f"  {'↑' if f['weight'] > 0 else '↓'} {f['label']}: {fmt_num(f['weight'], 3, True)}", "muted")
        if not top and sm.get("updates"):
            ctx.print("  Pesos aún en cero: el modelo usa reglas técnicas mientras aprende", "muted")
        elif not top:
            ctx.print("  El modelo actual aún no ha aprendido de ningún resultado: usa reglas técnicas mientras aprende"
                      + (" (el acierto histórico incluye predicciones de modelos anteriores)"
                         if sm.get("hit_rate") is not None else ""), "muted")
    else:
        ctx.print("El escáner todavía no tiene modelo (ejecuta 'scan' y espera a que se resuelvan las predicciones)",
                  "muted")
    g = s["gate"]

    def judged(rate: Any, n: Any, what: str) -> str:
        n = int(n or 0)
        if rate is None or not n:
            return "aún sin resultados conocidos"
        return f"{what} {fmt_prob(rate)}, {round(rate * n)} de {n} con resultado conocido"

    ctx.print(f"Filtro de operaciones: {g['decisions']} decisiones · TOMAR {g['taken']} ("
              f"{judged(g.get('taken_win_rate'), g.get('taken_judged'), 'llegaron a TP1 antes del SL:')}) · "
              f"NO TOMAR {g['avoided']} ("
              f"{judged(g.get('avoided_would_lose_rate'), g.get('avoided_judged'), 'no habrían llegado a TP1:')})",
              "learn")
    if min(int(g.get("taken_judged") or 0), int(g.get("avoided_judged") or 0)) < 30:
        ctx.print("Con menos de 30 resultados por grupo estas cifras aún no son evidencia sólida del filtro.", "muted")
    nm = s.get("nexus_models") or []
    if nm:
        ctx.print(f"Modelos NEXUS: {len(nm)}", "learn")
        rows = [[m["symbol"], m["tf"], m["updates"], fmt_prob(m.get("hit_rate"), 1),
                 fmt_num(m.get("brier"), 3)]
                for m in sorted(nm, key=lambda m: -m["updates"])[:8]]
        ctx.table(["Símbolo", "tf", "Actualizaciones", "Acierto", "Brier"], rows)
        ctx.print("  Acierto de cada modelo NEXUS: dirección de la vela siguiente (sube / baja); compáralo con la "
                  "frecuencia de subidas del periodo, no con un 50 % (y el Brier con la climatología, no con 0,25).",
                  "muted")
    p = s["predictions"]
    ctx.print(f"Predicciones: {p['resolved']} resueltas · acierto global {fmt_prob(p.get('hit_rate'), 1)} (histórico)",
              "learn")
    overall = groups.get("all") or (report["groups"] or [None])[0]
    if overall and overall.get("n"):
        ctx.print(f"  Últimos 7 días: {overall['verdict_text']}", _verdict_level(overall))
    tl = s.get("timeline") or []
    if tl:
        refs = {d["time"]: d for d in (overall or {}).get("daily") or []}
        ctx.table(["Día", "Acierto", "Referencia", "Brier", "Climatología", "N"],
                  [[_day(t["time"]), fmt_prob(t["hit_rate"], 1),
                    fmt_prob(refs[t["time"]]["reference"], 1) if t["time"] in refs else "—", fmt_num(t["brier"], 3),
                    fmt_num(refs[t["time"]]["brier_ref"], 3) if t["time"] in refs else "—", t["n"]] for t in tl[-7:]])
    ctx.print("Todas las cifras son fuera de muestra: cada predicción se guarda antes de conocer el resultado. Hay tres "
              "resultados posibles, así que el 50 % no es el azar: la referencia es acertar siempre el resultado más "
              "frecuente del día ('skill' para el detalle).", "muted")
    ctx.data = s


def _verdict_level(group: dict) -> str:
    return {"better": "success", "worse": "warn"}.get(str(group.get("verdict")), "muted")


def _fmt_pp(v: Any) -> str:
    return "—" if v is None else f"{fmt_num(v, 1, True)} pp"


def _print_skill(ctx: CommandContext, rep: dict) -> None:
    days = rep.get("days")
    period = f"últimos {fmt_num(days, 0 if float(days).is_integer() else 1)} días" if days else "todo lo guardado"
    ctx.print(f"Habilidad frente a la referencia · {period} · fuera de muestra", "learn")
    groups = [g for g in rep.get("groups") or []]
    if not any(g.get("n") for g in groups):
        ctx.print("Aún no hay predicciones verificadas en este periodo: no se puede medir la habilidad.", "muted")
        return
    rows = []
    for g in groups:
        if not g.get("n"):
            rows.append([_short(g["label"], 34), "0", "—", "—", "—", "—", "—", "—", "0"])
            continue
        ref = g["reference"]
        ci = g.get("skill_ci")
        diff = _fmt_pp(g.get("skill_pp")) + (f" [{fmt_num(ci[0], 1, True)}; {fmt_num(ci[1], 1, True)}]" if ci else "")
        rows.append([
            _short(g["label"], 34), f"{fmt_num(g['hits'], 0)} de {fmt_num(g['n'], 0)} ({fmt_prob(g['accuracy'], 1)})",
            f"{fmt_prob(ref['accuracy'], 1)} {ref['label'].replace('siempre ', '')}", fmt_prob(g.get("chance"), 1), diff,
            f"{fmt_num(g.get('brier'), 3)} / {fmt_num(g.get('brier_ref'), 3)}",
            "—" if g.get("bss") is None else f"{fmt_num(g['bss'] * 100.0, 0, True)} %",
            "—" if g.get("ece_pp") is None else f"{fmt_num(g['ece_pp'], 1)} pp", str(g["windows"]),
        ])
    ctx.table(["Grupo", "Acierto", "Referencia", "Azar (mezcla)", "Diferencia [IC 95 %]", "Brier / clim.",
               "Habilidad Brier", "Calibración", "Ventanas"], rows)
    for g in groups:
        if not g.get("n"):
            continue
        ctx.print(f"{g['label']}: {g['verdict_text']}", _verdict_level(g))
        if g.get("brier_text"):
            ctx.print(f"  {g['brier_text']}", "muted")
        if g.get("source") == "scanner" and g.get("model_key"):
            for d in g.get("by_direction") or []:
                if d["n"]:
                    ctx.print(f"  Predicción {d['label']}: {fmt_num(d['hits'], 0)} de {fmt_num(d['n'], 0)} "
                              f"({fmt_prob(d['accuracy'], 1)}) · ese resultado ocurrió en el "
                              f"{fmt_prob(d['base_rate'], 1)} de todas", "muted")
    for note in rep.get("notes") or []:
        ctx.print(note, "muted")


def _print_replay(ctx: CommandContext, rep: dict) -> None:
    if not rep.get("key"):
        ctx.print("No hay predicciones del escáner con su vector de características guardado en el periodo: la "
                  "repetición necesita escaneos resueltos de los últimos días.", "muted")
        return
    ctx.print(f"Repetición del escáner {rep['key']} · últimos {fmt_num(rep['days'], 0)} días · "
              f"{fmt_num(rep['rows'], 0)} predicciones en {fmt_num(rep['scans'], 0)} escaneos", "learn")
    rows = []
    for d in rep.get("designs") or []:
        if not d.get("n"):
            continue
        ref = d["reference"]
        rows.append([_short(d["label"], 40), f"{fmt_num(d['hits'], 0)} de {fmt_num(d['n'], 0)} "
                     f"({fmt_prob(d['accuracy'], 1)})", f"{fmt_prob(ref['accuracy'], 1)}",
                     f"{fmt_num(d.get('brier'), 3)} / {fmt_num(d.get('brier_ref'), 3)}",
                     "—" if d.get("ece_pp") is None else f"{fmt_num(d['ece_pp'], 1)} pp", str(d["windows"])])
    if rows:
        ctx.table(["Diseño", "Acierto", "Referencia", "Brier / clim.", "Calibración", "Ventanas"], rows)
    comparison = rep.get("comparison") or {}
    if comparison.get("text"):
        ctx.print(comparison["text"], _verdict_level(comparison))
    if rep.get("truncated"):
        ctx.print(f"Repetición limitada a las primeras {fmt_num(rep['rows'], 0)} predicciones del periodo: usa "
                  "--days con menos días para cubrir solo las más recientes.", "warn")
    if rep.get("note"):
        ctx.print(rep["note"], "muted")


@command("skill", help="Habilidad de las predicciones frente a la referencia (y repetición offline del escáner)",
         usage="skill [escáner|nexus|todo] [--days 7] | skill replay [--days 7] [--key crypto|1h|14400]",
         group="Aprendizaje",
         aliases=("habilidad",))
async def _skill(ctx: CommandContext) -> None:
    sub = str(ctx.arg(0, "")).lower()
    if sub in ("replay", "repeticion", "repetición", "repetir"):
        default = _setting(ctx, "scanner.replay_days", REPLAY_DAYS_DEFAULT) or REPLAY_DAYS_DEFAULT
        days = ctx.flag_float("days", None) or float(default)
        try:
            rep = await replay_report(ctx.rt.db, days=days, model_key_value=ctx.flag("key"))
        except ReplayBusy as exc:
            ctx.fail(str(exc))
        _print_replay(ctx, rep)
        ctx.data = rep
        return
    sources = {"": None, "todo": None, "todas": None, "all": None, "escaner": "scanner", "escáner": "scanner",
               "scanner": "scanner", "nexus": "nexus"}
    if sub not in sources:
        ctx.fail(f"Fuente no válida: {sub}. Usa escáner, nexus, todo o replay")
    days = ctx.flag_float("days", 7.0)
    rep = await skill_report(ctx.rt.db, source=sources[sub], days=days if days and days > 0 else None)
    _print_skill(ctx, rep)
    ctx.data = rep

