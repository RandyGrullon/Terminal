"""REST API for simulation, backtests, Monte Carlo, paper trading, scanner and learning (contract §6.4).

Errors are `{"detail": "<español>"}`: 400 invalid input, 404 not found, 409 conflict (the Trade Gate said NO TOMAR:
detail = {"message", "gate"}; a portfolio-risk limit of the demo account: detail = {"message", "code": "risk_limit",
"rule", "risk"}; an offline replay of the scanner already running), 503 when a required module is not loaded.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import config
from app.data.base import ProviderError
from app.runtime import rt
from app.sim.backtest import BacktestError, run_backtest, save_backtest, summarize_backtest
from app.sim.common import EngineUnavailable, load_engine
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
from app.sim.paper import PaperError, PaperRejected, PaperRiskBlocked
from app.sim.scanner import ScanError
from app.sim.simulator import SimError
from app.util import clamp, clean_json, normalize_tf, safe_float

router = APIRouter()


# ----------------------------------------------------------------------------- request models


class BacktestRequest(BaseModel):
    symbol: str | None = None
    tf: str = "1h"
    limit: int = 2000
    params: dict[str, Any] = Field(default_factory=dict)
    initial_capital: float = 10000.0
    risk_pct: float = 1.0
    fee_pct: float | None = None  # % per side; None = the asset class's cost (settings risk.fee_pct_by_class)
    slippage_pct: float = 0.02
    use_gate: bool = True


class MonteCarloRequest(BaseModel):
    mode: str = "bootstrap"
    symbol: str | None = None
    tf: str | None = "1h"
    paths: int = 300
    bars: int = 500
    seed: int = 1
    initial_capital: float = 10000.0


class SimStartRequest(BaseModel):
    mode: str = "synthetic"
    symbol: str | None = None
    tf: str | None = None
    speed: float | None = None
    seed: int | None = None
    regime: str | None = None
    start_price: float | None = None
    warmup: int | None = None
    auto_trade: bool = True
    initial_balance: float | None = None


class SimConfigRequest(BaseModel):
    speed: float | None = None
    regime: str | None = None
    paused: bool | None = None
    auto_trade: bool | None = None


class OrderRequest(BaseModel):
    symbol: str | None = None
    side: str | None = None
    tf: str | None = None
    qty: float | None = None
    risk_pct: float | None = None
    sl: float | None = None
    tp: float | None = None
    force: bool = False


class CloseRequest(BaseModel):
    position_id: int | None = None
    symbol: str | None = None
    all: bool = False


class PendingOrderRequest(BaseModel):
    symbol: str | None = None
    side: str | None = None
    kind: str | None = None  # limit | stop
    price: float | None = None  # limit price / stop trigger
    tf: str | None = None
    qty: float | None = None
    risk_pct: float | None = None
    sl: float | None = None
    tp: float | None = None
    expiry: str | None = None  # gtc | bars | date
    expire_bars: int | None = None
    expire_time: float | None = None  # unix s (ms accepted)
    force: bool = False


class CancelOrderRequest(BaseModel):
    order_id: int | None = None
    symbol: str | None = None
    all: bool = False


class ResetRequest(BaseModel):
    initial_balance: float | None = None


class AutoRequest(BaseModel):
    enabled: bool = False
    symbols: list[str] | None = None
    tf: str | None = None


class ScanRequest(BaseModel):
    market: str | None = None
    tf: str | None = None
    limit: int | None = None
    deep: bool = False
    deep_top: int | None = None


# ----------------------------------------------------------------------------- helpers


def _service(name: str, label: str) -> Any:
    svc = getattr(rt, name, None)
    if svc is None:
        raise HTTPException(status_code=503, detail=f"{label} no está disponible (el módulo no se cargó)")
    return svc


def _tf(text: str | None, default: str = "1h") -> str:
    try:
        return normalize_tf(text or default)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


async def _resolve(symbol: str | None) -> str:
    text = (symbol or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Falta el símbolo")
    if ":" in text:
        return text.upper()
    hub = rt.hub
    if hub is not None and hasattr(hub, "resolve"):
        try:
            resolved = await asyncio.wait_for(hub.resolve(text), timeout=20)
        except Exception:
            resolved = None
        if resolved:
            return str(resolved).upper()
        raise HTTPException(status_code=404, detail=f"No encontré el símbolo '{text}'")
    return text.upper()


def _emit(level: str, source: str, message: str) -> None:
    if rt.bus is not None:
        rt.bus.emit(level, source, message)


_NOT_FOUND = ("No hay posiciones", "No hay una posición", "No hay órdenes pendientes", "No hay una orden pendiente")


def _paper_error(exc: PaperError) -> HTTPException:
    msg = str(exc)
    status = 404 if msg.startswith(_NOT_FOUND) else 400
    return HTTPException(status_code=status, detail=msg)


async def _risk_blocked(paper: Any, exc: PaperRiskBlocked) -> HTTPException:
    """409 for a portfolio-risk limit of the demo account (never overridable with `force`)."""
    risk = exc.risk
    if risk is None:
        try:
            risk = await paper.risk_state(1)
        except Exception:
            risk = None
    return HTTPException(status_code=409, detail=clean_json({"message": exc.message, "code": "risk_limit",
                                                             "rule": exc.rule, "risk": risk}))


def _sim_error(exc: SimError) -> HTTPException:
    msg = str(exc)
    status = 409 if "No hay ninguna simulación" in msg or "en curso" in msg else 400
    return HTTPException(status_code=status, detail=msg)


# ----------------------------------------------------------------------------- backtest / monte carlo


@router.post("/api/backtest")
async def backtest(req: BacktestRequest) -> dict:
    hub = _service("hub", "El servicio de datos")
    symbol = await _resolve(req.symbol)
    tf = _tf(req.tf)
    try:
        load_engine()
    except EngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    limit = int(clamp(int(req.limit or 2000), 300, 5000))
    try:
        cs = await hub.get_candles(symbol, tf, limit)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudieron obtener las velas de {symbol} {tf}: {exc}") from None
    if cs is None or len(cs) == 0:
        note = getattr(cs, "note", None) if cs is not None else None
        raise HTTPException(status_code=404, detail=f"Sin velas para {symbol} {tf}" + (f": {note}" if note else ""))
    try:
        result = await asyncio.to_thread(
            run_backtest,
            cs,
            req.params or None,
            initial_capital=float(req.initial_capital),
            risk_pct=float(req.risk_pct),
            fee_pct=float(req.fee_pct) if req.fee_pct is not None else None,
            slippage_pct=float(req.slippage_pct),
            use_gate=bool(req.use_gate),
        )
    except EngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except (BacktestError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await save_backtest(rt.db, result)
    _emit("info", "backtest", summarize_backtest(result))
    return result


@router.post("/api/montecarlo")
async def montecarlo(req: MonteCarloRequest) -> dict:
    mode = (req.mode or "bootstrap").lower()
    if mode not in ("bootstrap", "synthetic"):
        raise HTTPException(status_code=400, detail="Modo no válido: usa 'bootstrap' o 'synthetic'")
    try:
        load_engine()
    except EngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    symbol = await _resolve(req.symbol) if mode == "bootstrap" else None
    if mode == "bootstrap":
        _service("hub", "El servicio de datos")
    tf = _tf(req.tf)
    try:
        result = await run_montecarlo(
            rt,
            mode=mode,
            symbol=symbol,
            tf=tf,
            paths=int(req.paths),
            bars=int(req.bars),
            seed=int(req.seed),
            initial_capital=float(req.initial_capital),
        )
    except EngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except (MonteCarloError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _emit("info", "montecarlo",
          f"Monte Carlo {mode}{' ' + symbol if symbol else ''} {tf}: {result['paths']} caminos · "
          f"P(ganancia) {result['prob_profit'] * 100:.0f} % · P(ruina) {result['prob_ruin'] * 100:.1f} %")
    return result


# ----------------------------------------------------------------------------- simulator


@router.post("/api/sim/start")
async def sim_start(req: SimStartRequest) -> dict:
    sim = _service("sim", "El simulador")
    body = req.model_dump()
    if body.get("mode") == "replay":
        body["symbol"] = await _resolve(body.get("symbol"))
    try:
        state = await sim.start_session(body)
    except SimError as exc:
        raise _sim_error(exc) from None
    return {"state": clean_json(state)}


@router.post("/api/sim/stop")
async def sim_stop() -> dict:
    sim = _service("sim", "El simulador")
    return {"state": clean_json(await sim.stop_session())}


@router.post("/api/sim/config")
async def sim_config(req: SimConfigRequest) -> dict:
    sim = _service("sim", "El simulador")
    try:
        state = await sim.configure(speed=req.speed, regime=req.regime, paused=req.paused, auto_trade=req.auto_trade)
    except SimError as exc:
        raise _sim_error(exc) from None
    return {"state": clean_json(state)}


@router.get("/api/sim/state")
async def sim_state() -> dict:
    sim = _service("sim", "El simulador")
    return clean_json(await sim.snapshot())


# ----------------------------------------------------------------------------- paper trading


@router.get("/api/paper/account")
async def paper_account() -> dict:
    paper = _service("paper", "La cuenta demo")
    return clean_json(await paper.snapshot(1))


@router.post("/api/paper/order")
async def paper_order(req: OrderRequest) -> dict:
    paper = _service("paper", "La cuenta demo")
    symbol = await _resolve(req.symbol)
    if not req.side:
        raise HTTPException(status_code=400, detail="Falta el lado de la orden: long o short")
    tf = _tf(req.tf, str(rt.settings.get("paper.auto_tf", "1h")) if rt.settings is not None else "1h")
    try:
        res = await paper.open_position(
            1, symbol, req.side, tf, qty=req.qty, risk_pct=req.risk_pct, sl=req.sl, tp=req.tp,
            force=bool(req.force), origin="manual",
        )
    except PaperRejected as rej:
        raise HTTPException(status_code=409, detail=clean_json({"message": rej.message, "gate": rej.gate})) from None
    except PaperRiskBlocked as blk:
        raise await _risk_blocked(paper, blk) from None
    except PaperError as exc:
        raise _paper_error(exc) from None
    return clean_json(res)


@router.get("/api/paper/orders")
async def paper_orders(status: str = "pending", limit: int = 50) -> dict:
    """Pending limit / stop orders of the demo account (status=all adds the latest filled / cancelled / expired)."""
    paper = _service("paper", "La cuenta demo")
    if status not in ("pending", "all"):
        raise HTTPException(status_code=400, detail="Estado no válido: usa 'pending' o 'all'")
    return clean_json({"orders": await paper.list_orders(1, status, int(clamp(limit, 1, 500)))})


@router.post("/api/paper/orders")
async def paper_place_order(req: PendingOrderRequest) -> dict:
    """Leave a pending limit / stop entry order (demo account). The gate is checked at the order price now and again
    at the fill (a NO TOMAR then cancels the order)."""
    paper = _service("paper", "La cuenta demo")
    symbol = await _resolve(req.symbol)
    if not req.side:
        raise HTTPException(status_code=400, detail="Falta el lado de la orden: long o short")
    if not req.kind:
        raise HTTPException(status_code=400, detail="Falta el tipo de orden: limit o stop")
    tf = _tf(req.tf, str(rt.settings.get("paper.auto_tf", "1h")) if rt.settings is not None else "1h")
    try:
        res = await paper.place_order(
            1, symbol, req.side, req.kind, req.price, tf, qty=req.qty, risk_pct=req.risk_pct, sl=req.sl, tp=req.tp,
            expiry=req.expiry, expire_bars=req.expire_bars, expire_time=req.expire_time, force=bool(req.force),
            origin="manual",
        )
    except PaperRejected as rej:
        raise HTTPException(status_code=409, detail=clean_json({"message": rej.message, "gate": rej.gate})) from None
    except PaperRiskBlocked as blk:
        raise await _risk_blocked(paper, blk) from None
    except PaperError as exc:
        raise _paper_error(exc) from None
    return clean_json(res)


@router.post("/api/paper/orders/cancel")
async def paper_cancel_orders(req: CancelOrderRequest) -> dict:
    paper = _service("paper", "La cuenta demo")
    symbol = await _resolve(req.symbol) if req.symbol and req.order_id is None and not req.all else None
    try:
        cancelled = await paper.cancel_orders(req.order_id, symbol, bool(req.all), account_id=1)
    except PaperError as exc:
        raise _paper_error(exc) from None
    return {"cancelled": clean_json(cancelled)}


@router.get("/api/paper/risk")
async def paper_risk() -> dict:
    """Portfolio risk of the demo account: open risk, positions per correlated group, daily P&L vs its limit."""
    paper = _service("paper", "La cuenta demo")
    return clean_json(await paper.risk_state(1))


@router.post("/api/paper/close")
async def paper_close(req: CloseRequest) -> dict:
    paper = _service("paper", "La cuenta demo")
    symbol = await _resolve(req.symbol) if req.symbol and req.position_id is None and not req.all else None
    try:
        closed = await paper.close_position(req.position_id, symbol, bool(req.all), account_id=1)
    except PaperError as exc:
        raise _paper_error(exc) from None
    return {"closed": clean_json(closed)}


@router.post("/api/paper/reset")
async def paper_reset(req: ResetRequest | None = None) -> dict:
    paper = _service("paper", "La cuenta demo")
    try:
        snap = await paper.reset(req.initial_balance if req is not None else None, account_id=1)
    except PaperError as exc:
        raise _paper_error(exc) from None
    return clean_json(snap)


@router.post("/api/paper/auto")
async def paper_auto(req: AutoRequest) -> dict:
    paper = _service("paper", "La cuenta demo")
    symbols = None
    if req.symbols is not None:
        symbols = [await _resolve(s) for s in req.symbols if str(s).strip()]
    try:
        auto = await paper.set_auto(bool(req.enabled), symbols, req.tf)
    except (PaperError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc).strip("'\"")) from None
    return {"auto": clean_json(auto)}


# ----------------------------------------------------------------------------- scanner


@router.post("/api/scanner/run")
async def scanner_run(req: ScanRequest) -> dict:
    scanner = _service("scanner", "El escáner")
    if config.OFFLINE:
        raise HTTPException(status_code=409, detail="Modo sin conexión: el escáner necesita datos de TradingView")
    tf = _tf(req.tf, str(rt.settings.get("scanner.tf", "1h")) if rt.settings is not None else "1h")
    try:
        run_id = await scanner.run_scan(req.market, tf, req.limit, bool(req.deep), req.deep_top)
    except (ScanError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"run_id": run_id}


@router.get("/api/scanner/results")
async def scanner_results(run_id: int | None = None) -> dict:
    scanner = _service("scanner", "El escáner")
    run, results = await scanner.results(run_id)
    if run_id is not None and run is None:
        raise HTTPException(status_code=404, detail=f"No existe el escaneo #{run_id}")
    return clean_json({"run": run, "results": results})


@router.get("/api/scanner/runs")
async def scanner_runs(limit: int = 20) -> dict:
    scanner = _service("scanner", "El escáner")
    return clean_json({"runs": await scanner.runs(limit)})


@router.get("/api/scanner/quick")
async def scanner_quick(symbol: str | None = None, tf: str | None = None) -> dict:
    scanner = _service("scanner", "El escáner")
    sym = await _resolve(symbol)
    tf_n = _tf(tf)
    quick = await scanner.quick_signal(sym, tf_n)
    if quick is None:
        raise HTTPException(status_code=404, detail=f"Sin datos técnicos para {sym} {tf_n}")
    return clean_json(quick)


# ----------------------------------------------------------------------------- predictions / learning


@router.get("/api/predictions")
async def predictions(source: str | None = None, symbol: str | None = None, resolved: bool | None = None,
                      limit: int = 200) -> dict:
    if source and source not in ("nexus", "scanner"):
        raise HTTPException(status_code=400, detail="Fuente no válida: usa 'nexus' o 'scanner'")
    sym = symbol.strip().upper() if symbol and symbol.strip() else None
    rows = await list_predictions(rt.db, source, sym, resolved, int(clamp(limit, 1, 2000)))
    stats = await prediction_stats(rt.db, source, sym)
    return clean_json({"predictions": rows, "stats": stats})


@router.get("/api/learning/summary")
async def learning() -> dict:
    settings = rt.settings.all() if rt.settings is not None else None
    return clean_json(await learning_summary(rt.db, settings))


def _days(value: float | None) -> float | None:
    if value is not None and not math.isfinite(value):
        raise HTTPException(status_code=400, detail="Parámetro 'days' no válido: usa un número de días (0 = todo)")
    return value


@router.get("/api/learning/skill")
async def learning_skill(source: str | None = None, days: float = 7.0, model_key: str | None = None) -> dict:
    """SkillReport (app.sim.evaluator.skill_report): hit rate of the verified predictions against the reference
    (always the most frequent outcome of the same rows) and chance with the same mix of calls, Brier against
    climatology, 95 % intervals clustered by independent horizon windows. days <= 0 = every stored row."""
    if source not in (None, "", "scanner", "nexus"):
        raise HTTPException(status_code=400, detail="Fuente no válida: usa scanner o nexus")
    days = _days(days)
    return clean_json(await skill_report(rt.db, source=source or None, model_key_value=(model_key or "").strip() or None,
                                         days=days if days and days > 0 else None))


@router.get("/api/learning/replay")
async def learning_replay(days: float | None = None, model_key: str | None = None) -> dict:
    """Offline replay of the scanner on its stored feature vectors (app.sim.evaluator.replay_report): ≈ 15 s of CPU
    per week of data in a worker thread, one at a time per database (409 while another one runs). Default period:
    the setting scanner.replay_days (clamped to 1 h .. 30 days by the evaluator)."""
    days = _days(days)
    if days is None or days <= 0:
        stored = rt.settings.get("scanner.replay_days", REPLAY_DAYS_DEFAULT) if rt.settings is not None else None
        days = safe_float(stored)
        if days is None or days <= 0:  # setting 0 (vectors dropped at resolution): the default period, as the terminal
            days = float(REPLAY_DAYS_DEFAULT)
    try:
        rep = await replay_report(rt.db, days=float(days), model_key_value=(model_key or "").strip() or None)
    except ReplayBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return clean_json(rep)
