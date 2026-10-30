"""``strategy`` object of PyChart: the ``strategy(...)`` declaration plus vectorized order rules
``strategy.entry / exit / close / close_all``.

PyChart scripts are evaluated once over the whole series, so orders are *rules*: every call registers series
(conditions and price levels aligned with the bars) and ``runtime_backtest.simulate`` replays them bar by bar:
- signals are evaluated at the bar close and filled at the next bar open (market orders);
- stop / limit levels are checked intrabar (gaps fill at the open, the stop wins when both are touched);
- ``stop`` / ``limit`` levels (of ``strategy.entry`` or ``strategy.exit``) are captured on the signal bar and stay
  fixed for the whole trade; ``strategy.exit(..., update=True)`` refreshes them on every bar close (Pine
  behaviour) and ``trail_atr=k`` adds a chandelier trailing stop at k × ATR(14) from the best price;
- one position at a time (no pyramiding); an opposite entry reverses the position.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .runtime_base import ScriptContext, ScriptRuntimeError, to_bool, to_float, to_series

MAX_RULES = 64
LONG = "long"
SHORT = "short"


class OrderRules:
    """Rules registered by the script (consumed by the backtester)."""

    __slots__ = ("entries", "exits", "closes")

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.exits: list[dict] = []
        self.closes: list[dict] = []

    def count(self) -> int:
        return len(self.entries) + len(self.exits) + len(self.closes)


def _direction(value: Any) -> int:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in (LONG, "buy", "largo", "compra"):
            return 1
        if key in (SHORT, "sell", "corto", "venta"):
            return -1
    elif isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        if value > 0:
            return 1
        if value < 0:
            return -1
    raise ScriptRuntimeError("strategy.entry: la dirección debe ser strategy.long o strategy.short.")


def _name(value: Any, what: str) -> str:
    if value is None:
        raise ScriptRuntimeError(f"{what}: falta el identificador (id) de la orden.")
    text = str(value).strip()
    if not text:
        raise ScriptRuntimeError(f"{what}: el identificador (id) no puede estar vacío.")
    return text[:40]


class StrategyAPI:
    """Callable declaration ``strategy(title, overlay, initial_capital, risk_pct, fee_pct, slippage_pct)`` and
    order namespace (``strategy.long``, ``strategy.short``, ``entry``, ``exit``, ``close``, ``close_all``)."""

    __slots__ = ("_ctx", "_declare")
    long = LONG
    short = SHORT

    def __init__(self, ctx: ScriptContext, declare: Any) -> None:
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_declare", declare)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("strategy es de solo lectura")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise AttributeError(
            f"strategy.{name} no está disponible en PyChart: el script se evalúa de forma vectorizada; "
            "gestiona la posición con strategy.entry / strategy.exit / strategy.close."
        )

    def __repr__(self) -> str:
        return "<strategy>"

    def __call__(
        self,
        title: Any = "Estrategia",
        overlay: Any = True,
        initial_capital: Any = 10_000,
        risk_pct: Any = 1.0,
        fee_pct: Any = 0.1,
        slippage_pct: Any = 0.0,
        shorttitle: Any = None,
        pyramiding: Any = None,
        default_qty_type: Any = None,
        default_qty_value: Any = None,
        commission_type: Any = None,
        commission_value: Any = None,
        currency: Any = None,
        process_orders_on_close: Any = None,
        calc_on_every_tick: Any = None,
        max_bars_back: Any = None,
        precision: Any = None,
        format: Any = None,
    ) -> None:
        capital = to_float(initial_capital, "strategy(initial_capital)")
        if capital <= 0:
            raise ScriptRuntimeError("strategy(initial_capital) debe ser mayor que 0.")
        risk = to_float(risk_pct, "strategy(risk_pct)")
        if not 0 < risk <= 100:
            raise ScriptRuntimeError("strategy(risk_pct) debe estar entre 0 y 100 (% del capital arriesgado por operación).")
        fee = to_float(fee_pct, "strategy(fee_pct)")
        slip = to_float(slippage_pct, "strategy(slippage_pct)")
        if not 0 <= fee <= 10 or not 0 <= slip <= 10:
            raise ScriptRuntimeError("strategy: fee_pct y slippage_pct deben estar entre 0 y 10 (%).")
        if self._declare("strategy", title, overlay):
            self._ctx.strategy_config = {
                "initial_capital": capital,
                "risk_pct": risk,
                "fee_pct": fee,
                "slippage_pct": slip,
            }
        return None

    # ------------------------------------------------------------------------- order rules
    def _rules(self, what: str) -> OrderRules:
        ctx = self._ctx
        if ctx.strategy_config is None:
            raise ScriptRuntimeError(f"{what}: declara strategy(...) al inicio del script para poder operar.")
        rules = ctx.strategy_rules
        if rules.count() >= MAX_RULES:
            raise ScriptRuntimeError(f"{what}: demasiadas órdenes registradas (máximo {MAX_RULES}).")
        return rules

    def _levels(self, value: Any, what: str) -> np.ndarray | None:
        if value is None:
            return None
        series = to_series(value, self._ctx.n, what)
        return series if np.isfinite(series).any() else None

    def entry(
        self,
        id: Any,
        direction: Any,
        when: Any = True,
        qty_pct: Any = None,
        stop: Any = None,
        limit: Any = None,
        comment: Any = None,
        qty: Any = None,
    ) -> None:
        """Opens (or reverses into) a position at the next bar open on every bar where ``when`` is true."""
        rules = self._rules("strategy.entry")
        if qty is not None and qty_pct is None:
            raise ScriptRuntimeError("strategy.entry: usa qty_pct (porcentaje del capital) en lugar de qty.")
        n = self._ctx.n
        size = None
        if qty_pct is not None:
            size = to_series(qty_pct, n, "strategy.entry(qty_pct)")
            if np.nanmax(np.where(np.isfinite(size), size, 0.0)) > 100 or np.nanmin(np.where(np.isfinite(size), size, 1.0)) <= 0:
                raise ScriptRuntimeError("strategy.entry(qty_pct) debe estar entre 0 (excluido) y 100.")
        rules.entries.append(
            {
                "id": _name(id, "strategy.entry"),
                "dir": _direction(direction),
                "when": to_bool(when, n, "strategy.entry(when)"),
                "qty_pct": size,
                "stop": self._levels(stop, "strategy.entry(stop)"),
                "limit": self._levels(limit, "strategy.entry(limit)"),
                "comment": None if comment is None else str(comment)[:40],
            }
        )
        return None

    def exit(
        self,
        id: Any,
        from_entry: Any = None,
        stop: Any = None,
        limit: Any = None,
        trail_atr: Any = None,
        when: Any = True,
        update: Any = False,
        comment: Any = None,
    ) -> None:
        """Protective stop / take-profit (and optional ATR trailing stop) for the position of ``from_entry``."""
        rules = self._rules("strategy.exit")
        n = self._ctx.n
        trail = None
        if trail_atr is not None:
            trail = to_float(trail_atr, "strategy.exit(trail_atr)")
            if trail <= 0:
                raise ScriptRuntimeError("strategy.exit(trail_atr) debe ser mayor que 0 (múltiplo del ATR).")
        stop_s = self._levels(stop, "strategy.exit(stop)")
        limit_s = self._levels(limit, "strategy.exit(limit)")
        if stop_s is None and limit_s is None and trail is None:
            raise ScriptRuntimeError("strategy.exit: indica stop, limit o trail_atr.")
        rules.exits.append(
            {
                "id": _name(id, "strategy.exit"),
                "from": None if from_entry is None else str(from_entry).strip()[:40],
                "stop": stop_s,
                "limit": limit_s,
                "trail": trail,
                "when": to_bool(when, n, "strategy.exit(when)"),
                "update": bool(update),
                "comment": None if comment is None else str(comment)[:40],
            }
        )
        return None

    def close(self, id: Any, when: Any = True, comment: Any = None) -> None:
        """Closes the position opened by entry ``id`` at the next bar open where ``when`` is true."""
        rules = self._rules("strategy.close")
        rules.closes.append(
            {
                "id": _name(id, "strategy.close"),
                "when": to_bool(when, self._ctx.n, "strategy.close(when)"),
                "comment": None if comment is None else str(comment)[:40],
            }
        )
        return None

    def close_all(self, when: Any = True, comment: Any = None) -> None:
        """Closes any open position at the next bar open where ``when`` is true."""
        rules = self._rules("strategy.close_all")
        rules.closes.append(
            {
                "id": None,
                "when": to_bool(when, self._ctx.n, "strategy.close_all(when)"),
                "comment": None if comment is None else str(comment)[:40],
            }
        )
        return None
