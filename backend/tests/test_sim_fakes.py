"""Test doubles for the BE-SIM tests: an in-memory market-data hub and a scriptable NEXUS service.

(No tests in this module; it is named test_sim_* only to stay inside the BE-SIM file ownership.)
"""
from __future__ import annotations

from typing import Any

from app.data.base import CandleSet


def make_gate(direction: str = "long", verdict: str = "TAKE", entry: float = 100.0, risk: float = 2.0,
              score: float | None = None, time: int = 1_760_000_000) -> dict:
    s = 1 if direction == "long" else -1
    label = {"TAKE": "TOMAR", "WAIT": "ESPERAR", "AVOID": "NO TOMAR"}[verdict]
    return {
        "direction": direction,
        "verdict": verdict,
        "verdict_label": label,
        "score": score if score is not None else {"TAKE": 78.0, "WAIT": 52.0, "AVOID": 31.0}[verdict],
        "win_prob": 0.61 if verdict == "TAKE" else 0.4,
        "rr": 2.0,
        "ev_r": 0.4 if verdict == "TAKE" else -0.2,
        "entry": entry,
        "sl": entry - s * risk,
        "tp1": entry + s * risk,
        "tp2": entry + s * 2 * risk,
        "tp3": entry + s * 3 * risk,
        "checks": [
            {"id": "trend_alignment", "label": "Tendencia mayor a favor", "passed": verdict == "TAKE",
             "critical": False, "weight": 0.6, "value": None, "detail": "EMA 4h"},
            {"id": "risk_reward", "label": "Riesgo/beneficio", "passed": True, "critical": True, "weight": 0.8,
             "value": 2.0, "detail": "R:R 2.0"},
            {"id": "volume", "label": "Volumen", "passed": None, "critical": False, "weight": 0.3, "value": None,
             "detail": "neutral"},
        ],
        "reasons_for": ["Tendencia a favor"] if verdict == "TAKE" else [],
        "reasons_against": [] if verdict == "TAKE" else ["Mercado lateral y resistencia cercana"],
        "summary": f"{label}: prueba",
        "time": time,
    }


def make_current(action: str = "BUY", *, price: float = 100.0, prob_up: float = 0.62, score: float = 0.5,
                 long_verdict: str = "TAKE", short_verdict: str = "AVOID", time: int = 1_760_000_000,
                 warnings: list | None = None) -> dict:
    labels = {"BUY": "COMPRAR", "SELL": "VENDER", "HOLD_LONG": "MANTENER LARGO", "HOLD_SHORT": "MANTENER CORTO",
              "WAIT": "ESPERAR", "EXIT_LONG": "CERRAR LARGO", "EXIT_SHORT": "CERRAR CORTO"}
    return {
        "time": time,
        "price": price,
        "action": action,
        "action_label": labels[action],
        "score": score,
        "prob_up": prob_up,
        "confidence": 0.6,
        "regime": "trend_up",
        "regime_label": "Tendencia alcista",
        "position": None,
        "gate": {"long": make_gate("long", long_verdict, price, time=time),
                 "short": make_gate("short", short_verdict, price, time=time)},
        "advice": "Prueba",
        "warnings": warnings or [],
        "contributions": [],
        "levels": {"support": [], "resistance": []},
        "reasons": [],
    }


class FakeHub:
    """In-memory MarketDataHub with the contract methods used by BE-SIM."""

    def __init__(self, prices: dict[str, float] | None = None, candles: dict[tuple[str, str], CandleSet] | None = None,
                 scan_rows: list[dict] | None = None, info: dict[str, dict] | None = None):
        self.prices = dict(prices or {})
        self.candles = dict(candles or {})
        self.scan_rows = list(scan_rows or [])
        self.info = dict(info or {})
        self.listeners: list[Any] = []
        self.pushed: list[tuple[str, str, dict, bool]] = []
        self.subs: list[tuple[str, str, str]] = []
        self.scan_calls: list[dict] = []
        self.fail_filters = False

    async def last_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)

    async def quotes(self, symbols: list[str]) -> dict:
        return {s: {"symbol": s, "price": self.prices[s]} for s in symbols if s in self.prices}

    async def get_candles(self, symbol: str, tf: str, limit: int = 500) -> CandleSet:
        cs = self.candles.get((symbol, tf))
        if cs is None:
            return CandleSet.empty(symbol, tf, note="sin datos de prueba")
        return cs.tail(int(limit))

    async def push_candle(self, symbol: str, tf: str, candle: dict, closed: bool) -> None:
        self.pushed.append((symbol, tf, dict(candle), closed))
        for cb in list(self.listeners):
            await cb(symbol, tf, candle, closed)

    def add_listener(self, cb: Any) -> None:
        self.listeners.append(cb)

    def subscribe(self, symbol: str, tf: str, owner: str) -> None:
        self.subs.append((symbol, tf, owner))

    def unsubscribe(self, symbol: str, tf: str, owner: str) -> None:
        if (symbol, tf, owner) in self.subs:
            self.subs.remove((symbol, tf, owner))

    async def resolve(self, text: str) -> str | None:
        t = text.strip().upper()
        if ":" in t:
            return t
        return {"BTC": "BINANCE:BTCUSDT", "ETH": "BINANCE:ETHUSDT"}.get(t)

    async def symbol_info(self, symbol: str) -> dict:
        return self.info.get(symbol, {"symbol": symbol, "history": "full", "market": "crypto"})

    async def tv_scan(self, market: str, columns: list[str], *, filters: Any = None, sort_by: str | None = None,
                      order: str = "desc", limit: int = 300, offset: int = 0, tickers: list[str] | None = None) -> dict:
        self.scan_calls.append({"market": market, "columns": list(columns), "filters": filters, "sort_by": sort_by,
                                "limit": limit, "tickers": tickers})
        if filters and self.fail_filters:
            raise RuntimeError("filtro no soportado")
        rows = self.scan_rows
        if tickers:
            rows = [r for r in rows if r.get("symbol") in tickers]
        return {"total": len(rows), "rows": [dict(r) for r in rows[:limit]]}


class FakeNexus:
    """Scriptable NexusService: gate verdicts, current advice and analyze() responses."""

    def __init__(self, verdict: str = "TAKE", current: dict | None = None,
                 analyze_map: dict[str, dict] | None = None):
        self.verdict = verdict
        self.current_value = current
        self.analyze_map = dict(analyze_map or {})
        self.gate_calls: list[tuple] = []
        self.current_calls: list[tuple] = []

    async def gate(self, symbol: str, tf: str, direction: str, entry: float | None = None, sl: float | None = None,
                   tp: float | None = None) -> dict:
        self.gate_calls.append((symbol, tf, direction, entry, sl, tp))
        return make_gate(direction, self.verdict, entry if entry is not None else 100.0)

    async def current(self, symbol: str, tf: str) -> dict | None:
        self.current_calls.append((symbol, tf))
        return self.current_value

    async def analyze(self, symbol: str, tf: str, limit: int | None = None) -> dict:
        if symbol in self.analyze_map:
            return self.analyze_map[symbol]
        return {"ok": False, "message": "historial insuficiente", "current": None}


def scan_row(symbol: str, *, close: float = 100.0, rating: float = 0.5, rsi: float = 60.0, change: float = 1.0,
             value: float = 50_000_000.0, currency: str = "USDT", bullish: bool = True) -> dict:
    """A plausible TradingView scan row (crypto) with every verified column."""
    s = 1.0 if bullish else -1.0
    atr = close * 0.03
    return {
        "symbol": symbol, "name": symbol.split(":")[-1], "description": symbol.split(":")[-1], "type": "spot",
        "exchange": symbol.split(":")[0], "currency": currency, "close": close, "change": change * s,
        "volume": value / close, "24h_vol|5": value, "Recommend.All": rating * s, "Recommend.MA": 0.6 * s,
        "Recommend.Other": 0.2 * s, "RSI": 50 + 10 * s, "RSI[1]": 50 + 8 * s, "Stoch.K": 50 + 20 * s,
        "Stoch.D": 50 + 15 * s, "CCI20": 80 * s, "ADX": 28.0, "ADX+DI": 25 + 5 * s, "ADX-DI": 25 - 5 * s,
        "AO": 0.5 * atr * s, "Mom": 0.8 * atr * s, "MACD.macd": 0.3 * atr * s, "MACD.signal": 0.2 * atr * s,
        "BB.upper": close * 1.04, "BB.lower": close * 0.94, "EMA20": close * (1 - 0.01 * s),
        "EMA50": close * (1 - 0.02 * s), "EMA200": close * (1 - 0.05 * s), "SMA50": close * (1 - 0.02 * s),
        "SMA200": close * (1 - 0.05 * s), "ATR": atr, "Volatility.D": 3.0, "Perf.W": 4.0 * s, "Perf.1M": 9.0 * s,
        "average_volume_10d_calc": value / close, "relative_volume_10d_calc": 1.6, "RSI|15": rsi,
        "RSI|60": rsi if bullish else 100 - rsi, "RSI|240": 50 + 7 * s, "Recommend.All|15": 0.3 * s,
        "Recommend.All|60": rating * s, "Recommend.All|240": 0.4 * s, "Recommend.All|1W": 0.2 * s,
        "MACD.macd|60": 0.2 * s, "MACD.signal|60": 0.1 * s, "MACD.macd|240": 0.4 * s, "MACD.signal|240": 0.2 * s,
        "EMA20|60": close * (1 - 0.003 * s), "EMA50|60": close * (1 - 0.008 * s), "EMA20|240": close * (1 - 0.01 * s),
        "EMA50|240": close * (1 - 0.02 * s), "ADX|60": 30.0, "change|60": 0.4 * s, "change|240": 0.9 * s,
        "close|60": close, "close|240": close,
    }


_ENGINE_RUNS: list[bool] = []


def engine_runs() -> bool:
    """True when the real NEXUS engine imports AND runs (it may be incomplete while it is being developed)."""
    if not _ENGINE_RUNS:
        from app.config import DEFAULT_SETTINGS
        from app.sim.common import EngineUnavailable, build_nexus_params, load_engine, run_engine
        from app.sim.synthetic import generate_candleset

        try:
            _, params_cls = load_engine()
            params, _ = build_nexus_params(params_cls, dict(DEFAULT_SETTINGS), None)
            run_engine(generate_candleset(n=220, seed=1, end_time=1_760_000_400), params)
            _ENGINE_RUNS.append(True)
        except EngineUnavailable:
            _ENGINE_RUNS.append(False)
    return _ENGINE_RUNS[0]


def require_engine() -> None:
    """Skip a real-engine integration test while the engine is missing or incomplete."""
    import pytest

    if not engine_runs():
        pytest.skip("motor NEXUS no disponible o incompleto")
