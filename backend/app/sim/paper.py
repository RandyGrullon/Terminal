"""Paper trading (`rt.paper`): demo accounts, orders checked by the Trade Gate, live position management.

Accounts: 1 = "Principal" (manual / auto-trading), 2 = "Simulación" (driven by the market simulator),
3 = "NEXUS automática" (NEXUS's own account: it trades on its own, profile "autopilot", see _auto_check).
Margin-style accounting: the balance only moves with realised P&L and fees; equity = balance + unrealised P&L.

Order flow: price → Trade Gate (`rt.nexus.gate`) → AVOID blocks the order unless `force` (PaperRejected → HTTP
409) → SL/TP from the gate when not given ("hasta aquí") → size by risk % of equity and the stop distance →
market fill with slippage + fee (per asset class, `app.engine.costs.fee_pct_for`) → rows in paper_positions /
paper_fills inside one DB transaction. R multiples are net of fees, like the engine's and the backtest's.

Management (live feed through the hub, 1m bars for real symbols): SL first; TP1 closes 1/3 and moves the stop to
break-even; TP2 closes 1/3 and starts a chandelier trailing stop; TP3 closes the rest. A stop moved by a fill takes
effect from the next bar: the rest of that bar checks the stop against its close only (its earlier high/low may
predate the move), and on a real market a bar that ended before the move, delivered late, never checks it. Only
live prices manage levels: an old quote (venue closed, feed down, only the local cache left) never moves a stop or
fills a target.
NEXUS advice is attached to every open position (≤ 1 refresh/min per symbol): expected value of holding from the
engine's first-passage model; "Cerrar ya" only on NEXUS's own exit rules: it leaves the position's side
(EXIT_LONG / EXIT_SHORT or a confirmed entry the other way), or its early-exit rule holds for this position (EV of
holding ≤ −0.10 R with the score against); any other negative EV is a "vigila la salida" warning. Nothing is
closed on live advice: manual positions get a warning event, and the auto-trader decides on the bar that just
closed (never on the forming bar), where it closes its positions on those exits and opens new ones on BUY / SELL
with gate TOMAR.
Positions with origin 'sim' belong to the simulator: they are priced and managed only through `sim_bar`.

NEXUS's own account (account 3, settings autopilot.*, off by default): the same closed-bar trader as auto-trading
(origin 'autopilot', `AutoProfile`), with its candidates from a list (by default a few liquid cryptos in dollars) and
the best candidates of the newest finished scan (TOMAR opportunities first, then markets the gate approves while NEXUS
has no signal yet; a new candidate is decided at once on its last closed bar; pairs quoted in another currency than
the dollar are never traded: its account is in USD), at most autopilot.max_positions
positions, autopilot.risk_pct per trade and the portfolio risk rules of its own account. It keeps a short decision
log (opened / discarded and why / closed); paused, its open positions keep their stop and targets. WS message
{type 'paper_autopilot'} on topic 'paper'. Simulation only: nothing is ever sent to a broker.
It may trade stocks and funds with THEORETICAL options (app.engine.options, autopilot.stock_instrument): an option
position (symbol OPT:…, side long: it only buys calls and puts, qty = contracts × 100, levels on the premium) is
priced with Black-Scholes from its underlying's live price, follows the underlying's bars and NEXUS's exit signals on
it, and leaves options.EXIT_DAYS days before its expiry (`_expire_options`).

Pending orders (account 1, table paper_orders, migration 0002): limit / stop entries with an expiry (GTC, N bars of
the order's tf, or a date). They trigger on live prices only (live 1m bars or polled live quotes): a limit when
touched (at its price or better, the open on a gap), a stop when crossed (at market with slippage, the open on a gap).
A final bar that did not arrive live (replayed after a feed stall, or delayed data) never fills one: the gate at the
fill would be asked now, with prices that came after that bar, and the new position would miss the rest of the replay
(a stop crossed meanwhile). After a stall, as after a restart, the bars that began before the feed came back count by
close only; an order whose level such a bar reached gets a notice and stays pending. At the fill the gate is asked
again at the fill price and a NO TOMAR cancels the order with the reason; the position is then opened and managed
exactly like a market one (SL / TP relative to the fill, R net of fees), in the same transaction that marks the order
filled.

Portfolio risk (accounts 1 and 3, each on its own; settings paper.max_open_risk_pct / paper.max_positions_per_group /
paper.crypto_one_group / paper.daily_loss_limit_pct, 0 = off): new orders and pending fills (manual, auto-trading,
terminal, NEXUS's own account) are blocked (PaperRiskBlocked, never forced) when the total open risk (entry -> current stop, % of equity)
would exceed the cap, when the correlated group (asset class; crypto as one group by default) already has the
maximum of positions, or once the realized loss of the UTC day reached the daily limit (% of the balance the day
started with): blocked until 00:00 UTC. Closing positions is never blocked.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import numpy as np

from app import config
from app.data.symbols import fx_pair, split_pair, strip_derivative
from app.engine.costs import asset_class, fee_pct_for
from app.engine.levels import ev_hold
from app.engine.nexus_current import EARLY_EV
from app.engine.service_watch import gate_target_ms
from app.sim import journal as J
from app.validation.stats import seed_for as S_seed
from app.sim import metrics as M
from app.sim.common import (
    EXIT_LABEL,
    SIDE_LABEL,
    call_maybe_async,
    fmt_num,
    fmt_pct,
    fmt_price,
    fmt_prob,
    fmt_qty,
    fmt_r,
    gate_checks_vector,
    gate_line,
    json_dumps,
    json_loads,
    side_sign,
)
from app.engine import options as opt
from app.engine.contracts import futures_spec
from app.util import RateLimitedLog, clamp, floor_time, normalize_tf, now_ms, now_s, safe_float, tf_seconds

AUTOPILOT_ACCOUNT = 3  # NEXUS's own demo account: trades on its own with its signals and the gate
ACCOUNTS = {1: "Principal", 2: "Simulación", AUTOPILOT_ACCOUNT: "NEXUS automática"}
# accounts under the portfolio risk rules (open risk, correlated groups, daily loss): the user's and NEXUS's
RISK_ACCOUNTS = frozenset({1, AUTOPILOT_ACCOUNT})
# position origins: the user (manual / terminal script), auto-trading, the simulator, NEXUS's own account
ORIGINS = ("manual", "auto", "sim", "script", "autopilot")
AUTOPILOT_SOURCES = ("scanner", "list", "both")
# quotes in dollars: NEXUS's own account is in USD, so it never trades a pair quoted in another currency (TRY, EUR, BTC…)
USD_QUOTES = frozenset({"USD", "USDT", "USDC", "FDUSD", "USDE", "DAI", "BUSD", "TUSD", "USDP"})
# asset classes outside the cash exposure of NEXUS's account (futures: their own approximate-margin rule); an
# option's premium is cash paid, so an option counts in it at its premium value
MARGIN_CLASSES = frozenset({"futures"})
OPTION_CLASSES = frozenset({"stock", "fund"})  # what NEXUS's own account may trade with options
# autopilot.stock_instrument: auto = shares to bet on a rise, a put on a fall (a cash account cannot sell short) ·
# shares = shares both ways (a simulated short sale) · options = a call or a put
STOCK_INSTRUMENTS = ("auto", "shares", "options")
ALWAYS_OPEN_CLASSES = frozenset({"crypto"})  # 24/7 markets: no market-hours check before an entry
AUTOPILOT_LOG_MAX = 30
AUTOPILOT_REFRESH_S = 60.0  # how often the autopilot looks for a newer scan (its candidates)
AUTOPILOT_SETTLE_S = 0.5  # quiet time after autopilot.* settings change (saved one key at a time) before re-reading
AUTOPILOT_STATE_KEY = "autopilot.state"  # meta: its decision log and last read per symbol (kept across restarts)
AUTOPILOT_KINDS = ("open", "close", "skip", "info")
COMPARE_TTL_S = 300.0  # its comparison with random entries is recomputed at most every 5 min (new outcomes come in)
JOURNAL_LIMIT = 500  # newest closed trades its journal analyses
HEARTBEAT_KEY = "autopilot.heartbeat"  # meta: unix s it was last seen running while on (a gap = server or PC stopped)
HEARTBEAT_SAVE_S = 300.0
WEEKLY_KEY = "autopilot.weekly"  # meta: its last weekly report (AutopilotWeekly)
WEEKLY_WEEK_KEY = "autopilot.weekly_week"  # meta: Monday (unix s) of the last week it reported or skipped
HEALTH_GRACE_S = 300  # a 24/7 market with no decision 3 bars after its last one has «sin decisiones recientes»
DOWNTIME_SHOW_S = 86400  # a detected stop stays in its status for a day
WEEK_S = 7 * 86400
GUARD_WINDOW_S = 90 * 86400  # what it learns from its losses only looks at the trades of the last 90 days
AUTOPILOT_NOTE = (
    "Cuenta 100 % simulada que opera sola con las señales de NEXUS y el filtro; nunca se envía nada a un bróker. "
    "La validación publicada (docs/VALIDATION.md) no encontró ventaja: lo esperable son resultados parecidos a "
    "entrar al azar, menos las comisiones."
)
MAX_LEVERAGE = M.DEFAULT_MAX_LEVERAGE
PUBLISH_EVERY_S = 5.0
ADVICE_EVERY_S = 60.0
PRICE_STALE_S = 20.0
QUOTE_STALE_S = 120.0  # a hub quote older than this is not a live price (the hub also flags it `stale`)
WARN_REPEAT_S = 900.0
HISTORY_LIMIT = 100
SIGMA_ATR = 0.8  # per-bar volatility ≈ 0.8 ATR (the engine's own fallback for its sigma)
# Engine warnings about NEXUS's own simulated trade (its EV, its stop, its bar count): not about this position.
ENGINE_TRADE_WARNINGS = {"EARLY_EXIT", "WATCH_EXIT", "NEAR_SL", "TIME_STOP"}
# Engine warnings phrased for the side NEXUS holds ("en contra de tu largo"): dropped when it holds the other side.
SIDE_WARNINGS = {"DIVERGENCE", "VOL_SPIKE", "COUNTER_TREND", "REGIME_CHANGE"}


class PaperError(ValueError):
    """Invalid paper order / operation (Spanish message)."""


class PaperRejected(Exception):
    """The Trade Gate said NO TOMAR and the order was not forced."""

    def __init__(self, gate: dict, message: str):
        super().__init__(message)
        self.gate = gate
        self.message = message


class PaperRiskBlocked(PaperError):
    """A portfolio-risk rule of the demo account blocks a new order or a pending fill (never forced with `force`).

    `rule`: daily_loss | open_risk | group. `risk`: the account's PaperRisk state when it was blocked."""

    def __init__(self, message: str, rule: str, risk: dict | None = None):
        super().__init__(message)
        self.message = message
        self.rule = rule
        self.risk = risk


# ----------------------------------------------------------------------------- pending orders & portfolio risk

ORDER_KINDS = ("limit", "stop")
ORDER_LABEL = {("long", "limit"): "Compra límite", ("long", "stop"): "Compra stop",
               ("short", "limit"): "Venta límite", ("short", "stop"): "Venta stop"}
ORDER_STATUS_LABEL = {"pending": "Pendiente", "filled": "Ejecutada", "cancelled": "Cancelada", "expired": "Caducada"}
MAX_PENDING_ORDERS = 50
MAX_EXPIRY_BARS = 1000
MAX_EXPIRY_DAYS = 365
ORDERS_LIMIT = 50  # non-pending orders returned by list_orders(status="all")
DAY_S = 86400
RISK_GROUP_LABEL = {"crypto": "Cripto", "stock": "Acciones", "fund": "Fondos y ETF", "forex": "Forex",
                    "commodity": "Materias primas", "index": "Índices", "futures": "Futuros", "cfd": "CFD",
                    "bond": "Bonos", "option": "Opciones", "other": "Otros", "sim": "Mercado simulado"}
DEMO_NOTE = "Cuenta demo: órdenes simuladas, nunca se envían a un bróker"


def order_trigger(s: int, kind: str, level: float, high: float, low: float, close: float, *,
                  open_: float | None = None, use_hl: bool = True) -> float | None:
    """Fill reference price when a pending entry order triggers on this price update, else None.

    limit (buy below / sell above the market) triggers when TOUCHED and fills at its price or better: at the bar's
    open when the market gaps through it. stop (buy above / sell below) triggers when the price CROSSES it and fills
    at market: at its price, at the open on a gap through it, or at the observed price when only a close is known
    (pessimistic, like a stop loss); the caller adds slippage to stop fills. `use_hl=False` (a polled price, or a bar
    that began before the order was watched) uses the close only and never fills a limit better than its price."""
    if not use_hl:
        high = low = close
        open_ = None
    if kind == "limit":
        if s > 0:
            if low > level:
                return None
            return open_ if open_ is not None and open_ < level else level
        if high < level:
            return None
        return open_ if open_ is not None and open_ > level else level
    if s > 0:
        if high < level:
            return None
        if open_ is not None and open_ > level:
            return open_
        return level if use_hl else max(level, close)
    if low > level:
        return None
    if open_ is not None and open_ < level:
        return open_
    return level if use_hl else min(level, close)


def risk_group(symbol: str, crypto_one_group: bool = True) -> str:
    """Correlated group of a symbol for the per-group position limit: the asset class (cripto counts as ONE group
    by default: the coins tend to move together), each crypto asset on its own when `crypto_one_group` is off (BTC
    against any quote is one asset), and 'sim' for simulated markets."""
    sym = str(symbol or "").strip().upper()
    if sym.startswith("SIM:"):
        return "sim"
    parsed = opt.parse_option_symbol(sym)
    if parsed is not None:  # an option counts with its underlying: they move together
        sym = parsed["underlying"]
    cls = asset_class(sym)
    if cls == "crypto" and not crypto_one_group:
        ticker = sym.partition(":")[2] or sym
        pair = split_pair(strip_derivative(ticker)[0])
        return f"crypto:{pair[0] if pair else ticker}"
    return cls


def risk_group_label(group: str) -> str:
    if group.startswith("crypto:"):
        return f"Cripto {group.partition(':')[2]}"
    return RISK_GROUP_LABEL.get(group, group)


def quote_currency(symbol: str) -> str | None:
    """The currency a crypto or forex pair is quoted in ('USDT', 'TRY'…); None when it cannot be read from the symbol
    (stocks, funds, futures, indices: their exchange's currency; simulated markets)."""
    sym = str(symbol or "").strip().upper()
    if not sym or sym.startswith("SIM:") or sym.startswith("OPT:"):
        return None
    cls = asset_class(sym)
    ticker = sym.partition(":")[2] or sym
    if cls == "crypto":
        pair = split_pair(strip_derivative(ticker)[0])
        return pair[1] if pair else None
    if cls in ("forex", "commodity"):  # a metal against a currency (XAUUSD) counts as a commodity
        pair = fx_pair(ticker)
        return pair[1] if pair else None
    return None


def in_dollars(symbol: str) -> bool:
    """The symbol is quoted in dollars (or a dollar stablecoin), or its currency cannot be read from it."""
    quote = quote_currency(symbol)
    return quote is None or quote in USD_QUOTES


def utc_day(ts_s: int) -> int:
    """Start (unix s) of the UTC day containing ts_s."""
    return int(ts_s) - int(ts_s) % DAY_S


def _norm_side(side: Any) -> str:
    text = str(side or "").strip().lower()
    if text in ("buy", "compra", "largo"):
        return "long"
    if text in ("sell", "venta", "corto"):
        return "short"
    if text not in ("long", "short"):
        raise PaperError("Lado no válido: usa 'long' (compra) o 'short' (venta)")
    return text


def _finite_or_none(value: Any, label: str) -> float | None:
    """None stays None; a number must be finite (NaN / ±inf -> PaperError instead of being silently dropped)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise PaperError(f"Valor no válido para {label}")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise PaperError(f"Valor no válido para {label}") from None
    if not math.isfinite(out):
        raise PaperError(f"Valor no válido para {label}: debe ser un número finito")
    return out


# ----------------------------------------------------------------------------- pure level management


@dataclass
class LevelState:
    side: int
    entry: float
    qty: float
    qty_open: float
    sl: float
    tps: list[float | None]
    tp_hits: int = 0
    trail: float | None = None
    atr: float = 0.0
    trail_mult: float = 3.0
    high_water: float = 0.0
    low_water: float = 0.0
    partial: bool = True

    def effective_stop(self) -> float:
        if self.trail is None:
            return self.sl
        return max(self.sl, self.trail) if self.side > 0 else min(self.sl, self.trail)

    def targets(self) -> list[tuple[int, float]]:
        defined = [(k + 1, lvl) for k, lvl in enumerate(self.tps) if lvl is not None]
        if not self.partial and defined:
            return [next((t for t in defined if t[0] == 2), defined[-1])]
        return defined

    def next_target(self) -> float | None:
        for k, lvl in self.targets():
            if k > self.tp_hits:
                return lvl
        return None


@dataclass
class Fill:
    kind: str  # partial | close
    reason: str  # SL | TRAIL | TP1 | TP2 | TP3 | ...
    price: float
    qty: float
    market: bool  # market fills pay slippage (stops); limit fills (targets) do not
    label: str = ""
    extra: dict = field(default_factory=dict)


def process_bar(st: LevelState, high: float, low: float, close: float, *, open_: float | None = None,
                use_hl: bool = True, stop_hl: bool = True, check_stop: bool = True) -> list[Fill]:
    """Apply one price update to the position levels (mutates `st`). Stop first, then targets in order.

    `use_hl=False` uses only the close (bar that started before the entry, or a polled last price).
    `stop_hl=False` checks the stop against the close only (filled there) while the targets still use the high/low:
    the rest of a bar in which a fill moved the stop (break-even, trailing), whose earlier high/low may predate the
    move. Like the engine, a moved stop takes effect from the next bar and targets keep filling in the same bar.
    `check_stop=False` leaves the stop alone (targets only): a bar delivered late that ended before the stop moved,
    so even its close predates the move.
    """
    if not use_hl:
        high = low = close
        stop_hl = False
    s = st.side
    fills: list[Fill] = []
    stop = st.effective_stop()
    stop_low, stop_high = (low, high) if stop_hl else (close, close)
    if check_stop and ((s > 0 and stop_low <= stop) or (s < 0 and stop_high >= stop)):
        px = stop
        if not stop_hl:
            px = close
        elif open_ is not None and ((s > 0 and open_ < stop) or (s < 0 and open_ > stop)):
            px = open_  # gap through the stop: filled at the open
        trailing = st.trail is not None and abs(st.effective_stop() - st.trail) < 1e-12 and st.tp_hits >= 2
        if trailing:
            reason, label = "TRAIL", EXIT_LABEL["TRAIL"]
        elif st.tp_hits >= 1 and abs(st.sl - st.entry) <= 1e-9 * max(1.0, abs(st.entry)):
            reason, label = "SL", EXIT_LABEL["BE"]
        else:
            reason, label = "SL", EXIT_LABEL["SL"]
        fills.append(Fill("close", reason, px, st.qty_open, True, label))
        st.qty_open = 0.0
        return fills
    st.high_water = max(st.high_water, high)
    st.low_water = min(st.low_water, low) if st.low_water > 0 else low
    targets = st.targets()
    for idx, (k, lvl) in enumerate(targets):
        if k <= st.tp_hits:
            continue
        touched = high >= lvl if s > 0 else low <= lvl
        if not touched:
            break
        if idx == len(targets) - 1 or st.qty_open <= st.qty / 3.0 + 1e-12:
            fills.append(Fill("close", f"TP{k}", lvl, st.qty_open, False, EXIT_LABEL.get(f"TP{k}", f"TP{k}")))
            st.qty_open = 0.0
            st.tp_hits = k
            return fills
        part = min(st.qty / 3.0, st.qty_open)
        fills.append(Fill("partial", f"TP{k}", lvl, part, False, EXIT_LABEL.get(f"TP{k}", f"TP{k}")))
        st.qty_open -= part
        st.tp_hits = k
        if k == 1:  # break-even
            st.sl = max(st.sl, st.entry) if s > 0 else min(st.sl, st.entry)
    if st.tp_hits >= 2 and st.atr > 0:
        if s > 0:
            candidate = st.high_water - st.trail_mult * st.atr
            st.trail = max(st.trail if st.trail is not None else -math.inf, candidate)
        else:
            candidate = st.low_water + st.trail_mult * st.atr
            st.trail = min(st.trail if st.trail is not None else math.inf, candidate)
    return fills


def _market_warnings(current: dict, side: str) -> list[dict]:
    """The engine warnings that apply to a position on `side` (see ENGINE_TRADE_WARNINGS / SIDE_WARNINGS)."""
    nexus_pos = current.get("position")
    other_side = isinstance(nexus_pos, dict) and nexus_pos.get("side") not in (None, side)
    out: list[dict] = []
    for w in current.get("warnings") or []:
        if not isinstance(w, dict):
            continue
        code = str(w.get("code") or "")
        if code in ENGINE_TRADE_WARNINGS or (other_side and code in SIDE_WARNINGS):
            continue
        out.append(w)
    return out


def assess_position(side: str, price: float, stop: float, next_tp: float | None, risk_unit: float,
                    current: dict | None, exit_threshold: float = 0.0, *, sigma: float = 0.0,
                    horizon: int = 5) -> dict:
    """Live NEXUS advice for an open position: expected value of holding (R), P(next target before the stop) and
    whether NEXUS recommends closing it.

    The EV is the engine's (`levels.ev_hold`): p = P(next target before the stop) from NEXUS's calibrated
    directional probability over `horizon` bars and the per-bar volatility `sigma` (price units) through the
    first-passage model, so a trade near its target is not judged with the probability of a whole-horizon move.
    `early_exit` ("Cerrar ya") only on NEXUS's own exit rules (contract I7): the action leaves this side
    (EXIT_LONG / EXIT_SHORT, or a confirmed entry the other way; `exit_by` "signal"), or the engine's early-exit rule
    holds for this position: EV of holding <= EARLY_EV (−0.10 R) with the score against it (`exit_by` "ev"). Any
    other negative EV is an amber "Vigila la salida" warning with its value, never a close.
    """
    if not current or risk_unit <= 0 or price is None:
        return {"early_exit": False, "exit_by": None, "ev_r": None, "p_hit": None, "advice": None, "warnings": []}
    s = side_sign(side)
    p_up = safe_float(current.get("prob_up"), 0.5)
    p_dir = p_up if s > 0 else 1.0 - p_up
    ev, p_hit = ev_hold(s, float(price), float(stop), next_tp, float(risk_unit), p_dir, float(sigma or 0.0),
                        int(horizon or 5))
    score = safe_float(current.get("score"), 0.0)
    action = str(current.get("action") or "")
    exit_by = ("signal" if action in (("EXIT_LONG", "SELL") if s > 0 else ("EXIT_SHORT", "BUY"))
               else "ev" if ev <= EARLY_EV and s * score < 0 else None)  # the engine's rules (nexus_decide)
    warnings = _market_warnings(current, side)
    side_lbl = SIDE_LABEL.get(side, side)
    if exit_by is not None:
        if exit_by == "signal":
            advice = f"Cerrar ya: NEXUS recomienda cerrar el {side_lbl} ({current.get('action_label') or action})"
        else:
            advice = (f"Cerrar ya: valor esperado de mantener negativo ({fmt_r(ev, 2)}) con el score NEXUS en contra "
                      f"({fmt_num(score, 2, True)})")
        warnings = [{"code": "EARLY_EXIT", "severity": "critical", "message": advice}] + warnings
        return {"early_exit": True, "exit_by": exit_by, "ev_r": ev, "p_hit": p_hit, "advice": advice,
                "warnings": warnings}
    watch = None
    if ev <= -0.005:  # shown with 2 decimals: never "negativo (0,00R)"
        watch = f"Vigila la salida: el valor esperado de mantener es negativo ({fmt_r(ev, 2)})"
        if ev <= EARLY_EV:  # the engine's wording: below the exit level, but the score does not turn yet
            watch += ", aunque el score NEXUS aún no gira en contra"
        elif s * score < -max(exit_threshold, 0.1):
            watch += f" y el score NEXUS va en contra ({fmt_num(score, 2, True)})"
        warnings = [{"code": "WATCH_EXIT", "severity": "warn", "message": watch}] + warnings
    critical = next((w for w in warnings if w.get("severity") == "critical"), None)
    if critical:
        advice = str(critical.get("message") or "")
    elif watch:
        advice = watch
    else:
        target = "al próximo objetivo" if next_tp is not None else "a +1R"
        advice = (f"Mantener: EV {fmt_r(ev, 2)} · probabilidad estimada {fmt_prob(p_hit)} de llegar {target} "
                  "antes del stop")
    return {"early_exit": False, "exit_by": None, "ev_r": ev, "p_hit": p_hit, "advice": advice, "warnings": warnings}


def option_inst(pos: dict) -> dict | None:
    """The option contract of a position (NEXUS's own account: meta.instrument, or a Position's instrument, of kind
    'option'), or None."""
    inst = (pos.get("meta") or {}).get("instrument") or pos.get("instrument")
    return inst if isinstance(inst, dict) and inst.get("kind") == "option" else None


def traded_symbol(pos: dict) -> str:
    """The market a position follows: an option's underlying stock, any other position's own symbol."""
    inst = option_inst(pos)
    return str(inst["underlying"]).upper() if inst is not None and inst.get("underlying") else pos["symbol"]


def option_premium(inst: dict, spot: float, at_s: int) -> float:
    """Theoretical premium per share of an option position's contract with its underlying at `spot`, at unix s
    `at_s` (Black-Scholes with the volatility fixed at the entry; the intrinsic value from its expiry on)."""
    expiry = date.fromisoformat(str(inst["expiry"]))
    return opt.bs_price(str(inst["right"]), float(spot), float(inst["strike"]), opt.years_to(expiry, int(at_s)),
                        float(inst["sigma"]))


def option_bar(inst: dict, open_: float | None, high: float, low: float, close: float,
               at_s: int) -> tuple[float | None, float, float, float]:
    """(open, high, low, close) of a bar of the underlying as the option's premium: a put is worth the most when the
    stock is at its low."""
    def premium(x: float) -> float:
        return option_premium(inst, x, at_s)

    hi, lo = (premium(high), premium(low)) if inst.get("right") == "call" else (premium(low), premium(high))
    return (premium(open_) if open_ is not None else None), hi, lo, premium(close)


def assess_option(inst: dict, current: dict | None) -> dict:
    """Live NEXUS advice for a bought option: NEXUS's exit rule on its underlying (`early_exit` when the action leaves
    the option's bet: a call once NEXUS stops being long the stock, a put once it stops being short). No EV in R:
    NEXUS's levels are on the stock, the option's on its theoretical premium."""
    out = {"early_exit": False, "exit_by": None, "ev_r": None, "p_hit": None, "advice": None, "warnings": []}
    if not current:
        return out
    side = "long" if inst.get("right") == "call" else "short"
    action = str(current.get("action") or "")
    label = current.get("action_label") or action or "—"
    underlying = str(inst.get("underlying") or "")
    ticker = underlying.partition(":")[2] or underlying
    warnings = _market_warnings(current, side)
    if action in (("EXIT_LONG", "SELL") if side == "long" else ("EXIT_SHORT", "BUY")):
        advice = f"Cerrar ya: NEXUS deja su apuesta {'al alza' if side == 'long' else 'a la baja'} en {ticker} ({label})"
        warnings = [{"code": "EARLY_EXIT", "severity": "critical", "message": advice}] + warnings
        return {**out, "early_exit": True, "exit_by": "signal", "advice": advice, "warnings": warnings}
    critical = next((w for w in warnings if w.get("severity") == "critical"), None)
    advice = (str(critical.get("message") or "") if critical else
              f"Mantener: NEXUS no da señal de salida en {ticker} ({label}); la prima es un precio teórico")
    return {**out, "advice": advice, "warnings": warnings}


def atr_from_candles(cs: Any, n: int = 14) -> float | None:
    if cs is None or len(cs) < n + 1:
        return None
    high, low, close = (np.asarray(a, dtype=float) for a in (cs.high, cs.low, cs.close))
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])
    atr = float(np.mean(tr[-n:]))
    return atr if math.isfinite(atr) and atr > 0 else None


def quote_is_live(q: Any, now: int | None = None) -> bool:
    """True when a hub quote is a live price: not flagged `stale` and `ts` (the real time of the price, unix ms)
    not older than QUOTE_STALE_S. A quote without a timestamp counts as live."""
    if not isinstance(q, dict) or q.get("stale"):
        return False
    ts = safe_float(q.get("ts"))
    return ts is None or (now if now is not None else now_ms()) - ts <= QUOTE_STALE_S * 1000.0


def _bar_seconds(tf: str) -> int:
    try:
        return tf_seconds(tf)
    except (KeyError, ValueError):
        return 60


def _age_text(seconds: float) -> str:
    s = max(int(seconds), 0)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} días"


# ----------------------------------------------------------------------------- engine


@dataclass(frozen=True)
class AutoProfile:
    """An automatic trader and its account: the user's auto-trading (account 1, origin 'auto', fixed symbols) or
    NEXUS's own account (account 3, origin 'autopilot', symbols from the scanner or a list). Both decide on the bar
    that just closed with the same rules: NEXUS BUY / SELL + gate TOMAR opens, NEXUS's exit rules close."""

    account_id: int
    origin: str
    tf: str
    symbols: frozenset[str]
    max_positions: int  # 0 = no cap beyond one position per symbol
    risk_pct: float | None  # None = risk.per_trade_pct
    label: str  # prefix of its events ("Auto", "NEXUS automática")
    entries: bool = True  # False: it only follows the exits of the positions it holds (never opens one)


class PaperEngine:
    def __init__(self, rt: Any):
        self.rt = rt
        self._started = False
        self._open: dict[int, dict] = {}
        self._lock = asyncio.Lock()
        self._prices: dict[str, tuple[float, float]] = {}
        self._advice_at: dict[str, float] = {}
        self._warned: dict[int, float] = {}
        self._subs: set[tuple[str, str, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self._listener_registered = False
        self._last_publish = 0.0
        self._sim_info: dict[str, Any] = {"auto_trade": False, "symbols": [], "tf": "1h"}
        self._log = RateLimitedLog()
        # one automatic decision at a time per (account, symbol); last closed bar handed to each automatic trader
        self._auto_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._auto_bar: dict[tuple[int, str, str], int] = {}
        # NEXUS's own account (autopilot): symbol -> {why, direction} it watches, the scan they came from, decisions
        self._autopilot_watch: dict[str, dict[str, Any]] = {}
        self._autopilot_scan: dict[str, Any] | None = None
        self._autopilot_log: deque[dict[str, Any]] = deque(maxlen=AUTOPILOT_LOG_MAX)
        self._autopilot_checked = 0.0  # monotonic time of the last look for a newer scan
        self._autopilot_on: bool | None = None  # last enabled state seen (its changes are announced)
        self._autopilot_quiet = False  # set_autopilot is saving its settings (it re-reads them itself)
        self._autopilot_changed = 0.0  # monotonic time of the last autopilot.* change from elsewhere
        self._autopilot_settling = False
        self._autopilot_tf: str | None = None  # the tf its candidates were last decided on
        self._autopilot_seq = 0  # decision-log entries written so far (a check that wrote one publishes the account)
        self._autopilot_last: dict[str, dict[str, Any]] = {}  # symbol -> NEXUS's read of its last decided bar
        self._autopilot_reads = 0  # reads recorded so far (a check that recorded one publishes the account)
        self._autopilot_dirty = False  # its log / reads changed since they were last saved (meta autopilot.state)
        self._autopilot_saving = False
        self._compare_cache: tuple[tuple[int, int], float, dict[str, Any]] | None = None
        self._compare_lock = asyncio.Lock()
        self._journal_cache: tuple[tuple[int, int], float, dict[str, Any]] | None = None
        # its health: the monitor's last pass while it was on (heartbeat), the last stop detected, the stale alert
        self._ap_seen: int | None = None
        self._ap_seen_saved = 0.0
        self._ap_downtime: dict[str, int] | None = None
        self._ap_stale_warned = False
        self._ap_weekly: dict[str, Any] | None = None
        self._ap_weekly_week: int | None = None
        # what it learned from its losses: contexts where its recent trades lose with strong evidence (cached)
        self._guard_cache: tuple[tuple[int, int, int], list[dict[str, Any]]] | None = None
        self._stale_warned: set[str] = set()  # symbols whose "sin precio en vivo" warning was already emitted
        # pending orders (account 1): id -> order dict; ids whose fill is being decided (gate at the fill price)
        self._orders: dict[int, dict] = {}
        self._executing: set[int] = set()
        # unix s from which each pending order has been watched (placement, or this engine's start after a restart):
        # extremes of a bar that began earlier may predate it, so such a bar is judged by its close only
        self._watch_from: dict[int, int] = {}
        self._started_s = 0
        # worst realized P&L of the current UTC day per account: (day, low). Persisted in meta so the daily loss
        # block holds until 00:00 UTC even if later closes recover part of the loss (or the server restarts).
        self._day_low: dict[int, tuple[int, float]] = {}
        self._day_block_warned: dict[int, int] = {}

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._started = True
        await self.ensure_accounts()
        rows = await self.rt.db.fetchall("SELECT * FROM paper_positions WHERE status='open'")
        for row in rows:
            pos = _pos_from_row(row)
            self._open[pos["id"]] = pos
        stale_sim = [p for p in self._open.values() if p["origin"] == "sim"]
        for pos in stale_sim:  # the simulator session that owned them no longer exists
            px = safe_float(pos["meta"].get("last_price")) or pos["entry_price"]
            await self._close(pos, px, "MANUAL", "Sesión de simulación finalizada", now_s(), market=False, emit=False)
        self._started_s = now_s()
        for row in await self.rt.db.fetchall("SELECT * FROM paper_orders WHERE status='pending' ORDER BY id"):
            order = _order_from_row(row)
            self._orders[order["id"]] = order
            # prices moved while NEXUS was stopped are unknown: bars that began before this start count by close only
            self._watch_from[order["id"]] = max(int(order["created_at"] // 1000), self._started_s)
        await self._load_day_low()
        await self._expire_orders()  # orders whose expiry passed while NEXUS was stopped
        await self._register_listener()
        await self._load_autopilot_state()
        await self._load_autopilot_health()
        self._autopilot_on = self._autopilot_cfg()["enabled"]
        if self.rt.settings is not None:
            self.rt.settings.add_listener(self._on_setting)
        if config.BACKGROUND:
            self._loop_task = asyncio.create_task(self._monitor_loop(), name="paper-monitor")
            await self._sync_subscriptions()

    async def stop(self) -> None:
        self._started = False
        tasks = [t for t in [self._loop_task, *self._tasks] if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._loop_task = None
        if self._autopilot_dirty:  # its last decisions are not lost with the stop
            try:
                await self._autopilot_write()
            except Exception:
                pass
        hub = self.rt.hub
        for symbol, tf, owner in list(self._subs):
            try:
                await call_maybe_async(hub.unsubscribe, symbol, tf, owner)
            except Exception:
                pass
        self._subs.clear()

    def status(self) -> dict:
        return {
            "running": self._started,
            "open_positions": sum(1 for p in self._open.values() if p["account_id"] == 1),
            "autopilot": {
                "enabled": bool(self._setting("autopilot.enabled", False)),
                "open_positions": sum(1 for p in self._open.values() if p["account_id"] == AUTOPILOT_ACCOUNT),
                "watching": len(self._autopilot_watch),
            },
        }

    def _initial_size(self, account_id: int) -> float:
        """Initial balance of a new (or reset) demo account: risk.account_size; NEXUS's own one autopilot.initial_balance."""
        key, default = (("autopilot.initial_balance", 1000.0) if account_id == AUTOPILOT_ACCOUNT
                        else ("risk.account_size", 10000.0))
        value = safe_float(self._setting(key, default))
        return float(value) if value is not None and value > 0 else default

    async def ensure_accounts(self) -> None:
        ts = now_ms()
        await self.rt.db.executemany(
            "INSERT OR IGNORE INTO paper_accounts(id, name, balance, initial_balance, currency, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'USD', ?, ?)",
            [(aid, name, self._initial_size(aid), self._initial_size(aid), ts, ts) for aid, name in ACCOUNTS.items()],
        )
        # NEXUS's account created by an older version with the user's default size, never used: its own size now
        size = self._initial_size(AUTOPILOT_ACCOUNT)
        row = await self.rt.db.fetchone(
            "SELECT balance, initial_balance, (SELECT COUNT(*) FROM paper_positions WHERE account_id=?) AS n "
            "FROM paper_accounts WHERE id=?", (AUTOPILOT_ACCOUNT, AUTOPILOT_ACCOUNT)) or {}
        initial, balance = safe_float(row.get("initial_balance")), safe_float(row.get("balance"))
        legacy = float(self._setting("risk.account_size", 10000.0) or 10000.0)
        if (int(row.get("n") or 0) == 0 and initial is not None and balance == initial == legacy
                and abs(legacy - size) > 1e-9):
            await self.rt.db.execute("UPDATE paper_accounts SET balance=?, initial_balance=?, updated_at=? WHERE id=?",
                                     (size, size, ts, AUTOPILOT_ACCOUNT))
            self._emit("info", "paper", f"NEXUS automática empieza con {fmt_num(size, 2)} (aún no había operado)")

    async def _register_listener(self) -> None:
        hub = self.rt.hub
        if self._listener_registered or hub is None or not hasattr(hub, "add_listener"):
            return
        try:
            await call_maybe_async(hub.add_listener, self._on_candle)
            self._listener_registered = True
        except Exception as exc:
            self._emit("warn", "paper", f"No se pudo escuchar el feed de precios: {exc}")

    # ------------------------------------------------------------------ helpers

    def _setting(self, key: str, default: Any = None) -> Any:
        if self.rt.settings is None:
            from app.config import DEFAULT_SETTINGS

            return DEFAULT_SETTINGS.get(key, default)
        return self.rt.settings.get(key, default)

    def _emit(self, level: str, source: str, message: str, data: Any = None) -> None:
        """An event; those about NEXUS's own account (data.account_id 3) say so, never read as the user's trades."""
        if self.rt.bus is not None:
            label = ACCOUNTS[AUTOPILOT_ACCOUNT]
            if isinstance(data, dict) and data.get("account_id") == AUTOPILOT_ACCOUNT and not message.startswith(label):
                message = f"{label} · {message}"
            self.rt.bus.emit(level, source, message, data)

    @staticmethod
    def _source(pos_or_origin: Any) -> str:
        origin = pos_or_origin.get("origin") if isinstance(pos_or_origin, dict) else pos_or_origin
        return "sim" if origin == "sim" else "paper"

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _fee_pct(self, symbol: str) -> float:
        """Commission + spread in % per side for the symbol's asset class (risk.fee_pct_by_class, else risk.fee_pct);
        an option pays a fixed approximation of its premium (app.engine.options.FEE_PCT)."""
        if opt.is_option(symbol):
            return opt.FEE_PCT
        return fee_pct_for(symbol, {"risk.fee_pct": self._setting("risk.fee_pct", 0.1),
                                    "risk.fee_pct_by_class": self._setting("risk.fee_pct_by_class", None)})

    @staticmethod
    def _gate_source(symbol: str, origin: str) -> str:
        """gate_log source: 'sim' for decisions on simulated markets (the simulator's synthetic or replay trades and
        any SIM:* symbol), so the evaluator can keep them out of the global gate learner; 'paper' otherwise."""
        return "sim" if origin == "sim" or symbol.upper().startswith("SIM:") else "paper"

    @staticmethod
    def _follows(prof: AutoProfile, pos: dict) -> bool:
        """This trader manages the position: same account and origin, a symbol it trades (an option: its underlying)
        and the position's tf."""
        return (pos["origin"] == prof.origin and pos["account_id"] == prof.account_id
                and traded_symbol(pos) in prof.symbols and (pos.get("tf") or "1h") == prof.tf)

    def _auto_follows(self, pos: dict) -> bool:
        """True while an automatic trader manages this position (it decides its exits at the bar close)."""
        return any(self._follows(prof, pos) for prof in self._auto_profiles())

    # ------------------------------------------------------------------ automatic traders

    def _user_auto_profile(self) -> AutoProfile | None:
        """Auto-trading of the user's account (paper.auto_*), None when it is off."""
        if not self._setting("paper.auto_trade", False):
            return None
        symbols = frozenset(str(s).strip().upper() for s in (self._setting("paper.auto_symbols", []) or []) if str(s).strip())
        return AutoProfile(1, "auto", str(self._setting("paper.auto_tf", "1h")), symbols, 0, None, "Auto")

    def _autopilot_cfg(self) -> dict[str, Any]:
        """Validated settings of NEXUS's own account (out-of-range values are clamped, unknown ones fall back)."""
        def num(key: str, default: float, lo: float, hi: float) -> float:
            v = safe_float(self._setting(key, default))
            return default if v is None else min(max(v, lo), hi)

        source = str(self._setting("autopilot.source", "both") or "both").strip().lower()
        try:
            tf = normalize_tf(self._setting("autopilot.tf", "1h") or "1h")
        except ValueError:
            tf = "1h"
        instr = str(self._setting("autopilot.stock_instrument", "auto") or "auto").strip().lower()
        return {
            "enabled": bool(self._setting("autopilot.enabled", False)),
            "source": source if source in AUTOPILOT_SOURCES else "both",
            "symbols": [str(x).strip().upper() for x in (self._setting("autopilot.symbols", []) or []) if str(x).strip()],
            "tf": tf,
            "max_positions": int(num("autopilot.max_positions", 5, 1, 20)),
            "risk_pct": float(num("autopilot.risk_pct", 1.0, 0.1, 5.0)),
            "scanner_top": int(num("autopilot.scanner_top", 10, 1, 30)),
            "initial_balance": self._initial_size(AUTOPILOT_ACCOUNT),
            "max_exposure_pct": float(num("autopilot.max_exposure_pct", 100.0, 0.0, 500.0)),
            "max_drawdown_pct": float(num("autopilot.max_drawdown_pct", 20.0, 0.0, 90.0)),
            "futures_margin_pct": float(num("autopilot.futures_margin_pct", 50.0, 0.0, 100.0)),
            "stock_instrument": instr if instr in STOCK_INSTRUMENTS else "auto",
            "options_premium_pct": float(num("autopilot.options_premium_pct", 10.0, 0.0, 50.0)),
            "learn_guard": bool(self._setting("autopilot.learn_guard", True)),
        }

    def _autopilot_held(self) -> dict[str, set[str]]:
        """tf -> symbols (an option: its underlying) of the positions NEXUS's own trader holds."""
        held: dict[str, set[str]] = {}
        for pos in self._open.values():
            if pos["account_id"] == AUTOPILOT_ACCOUNT and pos["origin"] == "autopilot":
                held.setdefault(pos.get("tf") or "1h", set()).add(traded_symbol(pos))
        return held

    def _autopilot_profile(self) -> AutoProfile | None:
        """NEXUS's own account while it is on: its candidates plus the symbols it holds at autopilot.tf."""
        cfg = self._autopilot_cfg()
        if not cfg["enabled"]:
            return None
        held = self._autopilot_held().get(cfg["tf"], set())
        return AutoProfile(AUTOPILOT_ACCOUNT, "autopilot", cfg["tf"], frozenset(self._autopilot_watch) | frozenset(held),
                           cfg["max_positions"], cfg["risk_pct"], "NEXUS automática")

    def _autopilot_profiles(self) -> list[AutoProfile]:
        """Its trader, plus an exits-only trader per other timeframe it still holds positions on (opened before a
        change of autopilot.tf): those keep NEXUS's exits on the bars they were opened on; nothing new opens there."""
        main = self._autopilot_profile()
        if main is None:
            return []
        extra = [AutoProfile(AUTOPILOT_ACCOUNT, "autopilot", tf, frozenset(syms), main.max_positions, main.risk_pct,
                             main.label, entries=False)
                 for tf, syms in sorted(self._autopilot_held().items()) if tf != main.tf]
        return [main, *extra]

    def _auto_profiles(self) -> list[AutoProfile]:
        user = self._user_auto_profile()
        return ([user] if user is not None else []) + self._autopilot_profiles()

    def _autopilot_note(self, symbol: str, kind: str, text: str) -> None:
        """Decision log of NEXUS's own account (newest first in its status); `kind`: open | close | skip | info."""
        self._autopilot_seq += 1
        self._autopilot_log.appendleft({"ts": now_ms(), "symbol": symbol, "kind": kind, "text": text[:1].upper() + text[1:]})
        self._autopilot_save_soon()

    def _autopilot_save_soon(self) -> None:
        """Save its log and reads in the background (one writer: each write stores the newest state)."""
        self._autopilot_dirty = True
        if self._started and not self._autopilot_saving:
            self._autopilot_saving = True
            try:
                self._spawn(self._autopilot_saver())
            except RuntimeError:  # no running loop
                self._autopilot_saving = False

    async def _autopilot_saver(self) -> None:
        try:
            while self._autopilot_dirty:
                self._autopilot_dirty = False
                try:
                    await self._autopilot_write()
                except asyncio.CancelledError:
                    self._autopilot_dirty = True  # stop() saves it
                    raise
                except Exception as exc:
                    if self._log.should_log("autopilot-save", 600):
                        self._emit("warn", "paper", f"NEXUS automática: no se pudo guardar su registro: {exc}")
                    return
        finally:
            self._autopilot_saving = False

    async def _autopilot_write(self) -> None:
        state = {"log": list(self._autopilot_log), "reads": {k: dict(v) for k, v in self._autopilot_last.items()}}
        await self.rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                 (AUTOPILOT_STATE_KEY, json_dumps(state)))

    async def _load_autopilot_state(self) -> None:
        """Its decision log and last reads as they were saved (a restart keeps them; malformed rows are skipped)."""
        try:
            raw = await self.rt.db.fetchval("SELECT value FROM meta WHERE key=?", (AUTOPILOT_STATE_KEY,))
        except Exception:
            raw = None
        data = json_loads(raw, None)
        if not isinstance(data, dict):
            return
        log = []
        for e in data.get("log") or []:
            if (isinstance(e, dict) and safe_float(e.get("ts")) is not None and isinstance(e.get("symbol"), str)
                    and e.get("kind") in AUTOPILOT_KINDS and isinstance(e.get("text"), str)):
                log.append({"ts": int(e["ts"]), "symbol": e["symbol"], "kind": e["kind"], "text": e["text"]})
        self._autopilot_log.clear()
        self._autopilot_log.extend(log[:AUTOPILOT_LOG_MAX])  # stored newest first, as shown
        reads: dict[str, dict[str, Any]] = {}
        saved = data.get("reads")
        for sym, r in (saved.items() if isinstance(saved, dict) else ()):
            if not isinstance(r, dict) or safe_float(r.get("bar_time")) is None or not isinstance(r.get("label"), str):
                continue
            try:
                tf = normalize_tf(r.get("tf"))
            except (ValueError, TypeError):
                continue
            action = r.get("action")
            reads[str(sym).upper()] = {"bar_time": int(r["bar_time"]), "tf": tf,
                                       "action": action if isinstance(action, str) and action else None,
                                       "label": r["label"]}
        self._autopilot_last = reads

    def _slip_pct(self) -> float:
        return float(self._setting("risk.slippage_pct", 0.02) or 0.0)

    def cached_price(self, symbol: str, max_age_s: float | None = None) -> float | None:
        if opt.is_option(symbol):  # an option: its theoretical premium from its underlying's cached price
            inst = self._option_of(symbol)
            spot = self.cached_price(str(inst["underlying"]), max_age_s) if inst is not None else None
            return option_premium(inst, spot, now_s()) if inst is not None and spot is not None else None
        item = self._prices.get(symbol.upper())
        if item is None:
            return None
        price, ts = item
        if max_age_s is not None and time.monotonic() - ts > max_age_s:
            return None
        return price

    async def last_price(self, symbol: str) -> float | None:
        symbol = symbol.upper()
        if opt.is_option(symbol):  # never asked to the hub: priced from its underlying
            inst = self._option_of(symbol)
            spot = await self.last_price(str(inst["underlying"])) if inst is not None else None
            return option_premium(inst, spot, now_s()) if inst is not None and spot is not None else None
        cached = self.cached_price(symbol, 10.0)
        if cached is not None:
            return cached
        hub = self.rt.hub
        if hub is None:
            return None
        try:
            price = safe_float(await asyncio.wait_for(hub.last_price(symbol), timeout=20))
        except Exception:
            price = None
        if price is not None and price > 0:
            self._prices[symbol] = (price, time.monotonic())
            return price
        return None

    def _position_price(self, pos: dict) -> float | None:
        if pos["origin"] == "sim":
            return safe_float(pos["meta"].get("last_price"))
        inst = option_inst(pos)
        if inst is not None:  # its theoretical premium now (time decay included) with the underlying's last price
            spot = self.cached_price(str(inst["underlying"])) or safe_float(pos["meta"].get("spot"))
            return option_premium(inst, spot, now_s()) if spot else safe_float(pos["meta"].get("last_price"))
        return self.cached_price(pos["symbol"]) or safe_float(pos["meta"].get("last_price"))

    def _option_of(self, symbol: str) -> dict | None:
        """The contract of an open option position with this symbol (its volatility prices it), or None."""
        sym = symbol.upper()
        for p in self._open.values():
            if p["symbol"] == sym:
                inst = option_inst(p)
                if inst is not None:
                    return inst
        return None

    @staticmethod
    def _pos_label(pos: dict) -> str:
        """How events name a position: "BINANCE:BTCUSDT Largo"; an option by its contract ("AAPL 230 C … (call)")."""
        inst = option_inst(pos)
        if inst is not None:
            return f"{inst.get('name') or pos['symbol']} ({inst.get('right')})"
        return f"{pos['symbol']} {SIDE_LABEL[pos['side']]}"

    # ------------------------------------------------------------------ views

    def position_dict(self, pos: dict) -> dict:
        meta = pos["meta"]
        s = side_sign(pos["side"])
        qty, entry = pos["qty"], pos["entry_price"]
        risk_unit = safe_float(meta.get("risk_unit")) or abs(entry - (safe_float(meta.get("initial_sl")) or pos["sl"]))
        is_open = pos["status"] == "open"
        price = self._position_price(pos) if is_open else safe_float(pos.get("exit_price"))
        if is_open:
            unreal = s * ((price or entry) - entry) * pos["qty_open"]
            gross = float(meta.get("realized_gross", 0.0)) + unreal
            pnl = gross - float(meta.get("fees", 0.0))  # fees paid so far (no exit fee on the open part, as the engine)
            r_mult = pnl / (qty * risk_unit) if qty > 0 and risk_unit > 0 else None
        else:
            pnl = float(pos.get("pnl") or 0.0)
            r_mult = safe_float(pos.get("r_multiple"))
        notional = qty * entry
        reason = pos.get("exit_reason")
        return {
            "id": pos["id"],
            "account_id": pos["account_id"],
            "symbol": pos["symbol"],
            "tf": pos.get("tf") or "1h",
            "side": pos["side"],
            "qty": qty,
            "qty_open": pos["qty_open"],
            "entry_price": entry,
            "entry_time": pos["entry_time"],
            "sl": pos["sl"],
            "tp1": pos.get("tp1"),
            "tp2": pos.get("tp2"),
            "tp3": pos.get("tp3"),
            "trail": pos.get("trail"),
            "tp_hits": int(pos.get("tp_hits") or 0),
            "status": pos["status"],
            "price": price,
            "exit_price": pos.get("exit_price"),
            "exit_time": pos.get("exit_time"),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / notional * 100.0, 4) if notional > 0 else 0.0,
            "r_multiple": round(r_mult, 3) if r_mult is not None else None,
            "instrument": meta.get("instrument"),
            "exit_reason": reason,
            "exit_label": meta.get("exit_label") or (EXIT_LABEL.get(reason) if reason else None),
            "origin": pos.get("origin") or "manual",
            "gate_verdict": pos.get("gate_verdict"),
            "gate_score": pos.get("gate_score"),
            "advice": meta.get("advice"),
            "warnings": meta.get("warnings") or [],
        }

    def open_positions(self, account_id: int = 1, symbol: str | None = None) -> list[dict]:
        items = [p for p in self._open.values() if p["account_id"] == account_id
                 and (symbol is None or p["symbol"] == symbol.upper())]
        return [self.position_dict(p) for p in sorted(items, key=lambda p: p["id"])]

    async def history(self, account_id: int = 1, limit: int = HISTORY_LIMIT) -> list[dict]:
        rows = await self.rt.db.fetchall(
            "SELECT * FROM paper_positions WHERE account_id=? AND status='closed' ORDER BY exit_time DESC, id DESC LIMIT ?",
            (account_id, int(limit)),
        )
        return [self.position_dict(_pos_from_row(r)) for r in rows]

    async def account(self, account_id: int = 1) -> dict:
        row = await self.rt.db.fetchone("SELECT * FROM paper_accounts WHERE id=?", (account_id,))
        if row is None:
            await self.ensure_accounts()
            row = await self.rt.db.fetchone("SELECT * FROM paper_accounts WHERE id=?", (account_id,))
        stats = await self.rt.db.fetchone(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins FROM paper_positions "
            "WHERE account_id=? AND status='closed'",
            (account_id,),
        ) or {}
        balance = float(row["balance"])
        initial = float(row["initial_balance"])
        unreal = 0.0
        open_n = 0
        for pos in self._open.values():
            if pos["account_id"] != account_id:
                continue
            open_n += 1
            price = self._position_price(pos)
            if price is not None:
                unreal += side_sign(pos["side"]) * (price - pos["entry_price"]) * pos["qty_open"]
        trades = int(stats.get("n") or 0)
        wins = int(stats.get("wins") or 0)
        equity = balance + unreal
        if account_id == 1:
            auto = bool(self._setting("paper.auto_trade", False))
            symbols = list(self._setting("paper.auto_symbols", []) or [])
            auto_tf = str(self._setting("paper.auto_tf", "1h"))
        elif account_id == AUTOPILOT_ACCOUNT:
            cfg = self._autopilot_cfg()
            auto, auto_tf = cfg["enabled"], cfg["tf"]
            symbols = sorted(self._autopilot_watch) if cfg["source"] != "list" else cfg["symbols"]
        else:
            auto = bool(self._sim_info.get("auto_trade"))
            symbols = list(self._sim_info.get("symbols") or [])
            auto_tf = str(self._sim_info.get("tf") or "1h")
        return {
            "id": account_id,
            "name": row["name"],
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "initial_balance": round(initial, 2),
            "realized_pnl": round(balance - initial, 2),
            "unrealized_pnl": round(unreal, 2),
            "return_pct": round((equity / initial - 1.0) * 100.0, 4) if initial > 0 else 0.0,
            "trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate": wins / trades if trades else None,
            "open_positions": open_n,
            "auto_trade": auto,
            "auto_symbols": symbols,
            "auto_tf": auto_tf,
        }

    async def snapshot(self, account_id: int = 1) -> dict:
        return {
            "account": await self.account(account_id),
            "positions": self.open_positions(account_id),
            "history": await self.history(account_id),
        }

    async def publish(self, account_id: int = 1) -> None:
        """WS topic 'paper': {type 'paper'} for account 1 and {type 'paper_autopilot'} (AutopilotSnapshot) for NEXUS's
        own account; account 2 travels inside the simulator's 'sim' messages."""
        if self.rt.ws is None:
            return
        if account_id == AUTOPILOT_ACCOUNT:
            try:
                self.rt.ws.publish("paper", {"type": "paper_autopilot", **(await self.autopilot_snapshot())})
            except Exception:
                pass
            return
        if account_id != 1:
            return
        try:
            account = await self.account(1)
            msg: dict[str, Any] = {"type": "paper", "account": account, "positions": self.open_positions(1),
                                   "orders": self.pending_orders(1)}
            try:
                msg["risk"] = await self.risk_state(1, account)
            except Exception:
                msg["risk"] = None
            self.rt.ws.publish("paper", msg)
            self._last_publish = time.monotonic()
        except Exception:
            pass

    async def equity(self, account_id: int) -> float:
        return float((await self.account(account_id))["equity"])

    def set_sim_info(self, *, auto_trade: bool, symbols: list[str], tf: str) -> None:
        self._sim_info = {"auto_trade": bool(auto_trade), "symbols": list(symbols), "tf": tf}

    # ------------------------------------------------------------------ pending orders: views

    def _when(self, ts_s: int) -> str:
        """'hoy 14:00' / 'mañana 9:30' / 'jue 05/11 8:30' in the user's zone (ui.timezone)."""
        from app.macro.fmt import day_clock

        return day_clock(ts_s, now_s(), self._setting("ui.timezone", ""))

    def _expiry_text(self, order: dict) -> str:
        if order.get("expiry") == "bars" and order.get("expire_time"):
            n = order.get("expire_bars")
            return (f"válida {n} {'vela' if n == 1 else 'velas'} de {order.get('tf') or '1h'} "
                    f"(hasta {self._when(int(order['expire_time']))})")
        if order.get("expire_time"):
            return f"válida hasta {self._when(int(order['expire_time']))}"
        return "válida hasta cancelarla"

    def order_dict(self, order: dict) -> dict:
        side, kind = order["side"], order["kind"]
        pending = order["status"] == "pending"
        market = self.cached_price(order["symbol"]) if pending else None
        meta = order.get("meta") or {}
        return {
            "id": order["id"],
            "account_id": order["account_id"],
            "symbol": order["symbol"],
            "tf": order.get("tf") or "1h",
            "side": side,
            "kind": kind,
            "label": ORDER_LABEL.get((side, kind), kind),
            "price": order["price"],
            "qty": order.get("qty"),
            "risk_pct": order.get("risk_pct"),
            "sl": order.get("sl"),
            "tp": order.get("tp"),
            "expiry": order.get("expiry") or "gtc",
            "expire_bars": order.get("expire_bars"),
            "expire_time": order.get("expire_time"),
            "expiry_text": self._expiry_text(order),
            "status": order["status"],
            "status_label": ORDER_STATUS_LABEL.get(order["status"], order["status"]),
            "origin": order.get("origin") or "manual",
            "gate_verdict": order.get("gate_verdict"),
            "gate_score": order.get("gate_score"),
            "forced": bool(meta.get("forced")),
            "market_price": market,
            "distance_pct": round((order["price"] / market - 1.0) * 100.0, 4) if market else None,
            "executing": order["id"] in self._executing,
            "position_id": order.get("position_id"),
            "fill_price": order.get("fill_price"),
            "fill_time": order.get("fill_time"),
            "reason": order.get("reason"),
            "cancelled_by": order.get("cancelled_by"),
            "created_at": order["created_at"],
            "updated_at": order.get("updated_at"),
        }

    def pending_orders(self, account_id: int = 1, symbol: str | None = None) -> list[dict]:
        items = [o for o in self._orders.values() if o["account_id"] == account_id
                 and (symbol is None or o["symbol"] == symbol.upper())]
        return [self.order_dict(o) for o in sorted(items, key=lambda o: o["id"])]

    async def list_orders(self, account_id: int = 1, status: str = "pending", limit: int = ORDERS_LIMIT) -> list[dict]:
        """Pending orders (status 'pending'), or the pending ones followed by the latest `limit` filled / cancelled /
        expired ones (status 'all'), newest first."""
        await self._expire_orders()
        out = self.pending_orders(account_id)
        if status == "all":
            rows = await self.rt.db.fetchall(
                "SELECT * FROM paper_orders WHERE account_id=? AND status<>'pending' ORDER BY updated_at DESC, id DESC "
                "LIMIT ?",
                (account_id, int(clamp(int(limit), 1, 500))),
            )
            out = out[::-1] + [self.order_dict(_order_from_row(r)) for r in rows]
        return out

    # ------------------------------------------------------------------ portfolio risk (account 1)

    def _risk_limits(self) -> dict[str, Any]:
        """Current limits; 0 or a negative / invalid value switches a rule off."""
        def positive(key: str, default: float, cap: float) -> float:
            v = safe_float(self._setting(key, default))
            return min(v, cap) if v is not None and v > 0 else 0.0

        return {
            "max_open_risk_pct": positive("paper.max_open_risk_pct", 6.0, 100.0),
            "max_positions_per_group": int(positive("paper.max_positions_per_group", 3, 1000)),
            "crypto_one_group": bool(self._setting("paper.crypto_one_group", True)),
            "daily_loss_limit_pct": positive("paper.daily_loss_limit_pct", 3.0, 100.0),
        }

    def _position_risk(self, pos: dict) -> float:
        """Money lost from the entry if the position's current stop is hit (>= 0: 0 once the stop protects it)."""
        stop = self._level_state(pos).effective_stop()
        return max(0.0, side_sign(pos["side"]) * (pos["entry_price"] - stop)) * float(pos["qty_open"])

    def _open_risk(self, account_id: int) -> float:
        return sum(self._position_risk(p) for p in self._open.values() if p["account_id"] == account_id)

    async def _day_realized(self, account_id: int, day: int) -> float:
        """Realized P&L of the UTC day: gross result of the closing fills minus every fee paid that day (entries too),
        i.e. exactly what the balance moved by fills since 00:00 UTC."""
        row = await self.rt.db.fetchone(
            "SELECT COALESCE(SUM(CASE WHEN (p.side = 'long' AND f.side = 'sell') OR (p.side = 'short' AND f.side = 'buy') "
            "THEN (CASE WHEN p.side = 'long' THEN 1.0 ELSE -1.0 END) * (f.price - p.entry_price) * f.qty ELSE 0.0 END), 0.0) "
            "AS gross, COALESCE(SUM(f.fee), 0.0) AS fees FROM paper_fills f JOIN paper_positions p ON p.id = f.position_id "
            "WHERE f.account_id = ? AND f.time >= ? AND f.time < ?",
            (account_id, int(day), int(day) + DAY_S),
        ) or {}
        return float(row.get("gross") or 0.0) - float(row.get("fees") or 0.0)

    async def _load_day_low(self) -> None:
        for aid in ACCOUNTS:
            raw = await self.rt.db.fetchval("SELECT value FROM meta WHERE key=?", (f"paper.day_low.{aid}",))
            data = json_loads(raw, None)
            if isinstance(data, dict) and safe_float(data.get("day")) is not None and safe_float(data.get("low")) is not None:
                self._day_low[aid] = (int(data["day"]), float(data["low"]))

    async def _note_day_low(self, account_id: int, day: int, realized: float) -> float:
        """Running minimum of the day's realized P&L (persisted when it falls): the daily block latches on it."""
        prev = self._day_low.get(account_id)
        low = min(realized, prev[1]) if prev is not None and prev[0] == day else realized
        if prev is None or prev[0] != day or low < prev[1] - 1e-9:
            self._day_low[account_id] = (day, low)
            if low < 0 or prev is not None:
                await self.rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                         (f"paper.day_low.{account_id}", json_dumps({"day": day, "low": low})))
        return low

    async def _daily(self, account_id: int, account: dict | None = None) -> dict[str, Any]:
        limits = self._risk_limits()
        now = now_s()
        day = utc_day(now)
        realized = await self._day_realized(account_id, day)
        account = account or await self.account(account_id)
        start_balance = float(account["balance"]) - realized
        low = await self._note_day_low(account_id, day, realized)
        pct = limits["daily_loss_limit_pct"]
        limit = start_balance * pct / 100.0 if pct > 0 and start_balance > 0 else None
        blocked = limit is not None and low <= -limit + 1e-9
        reason = None
        if blocked:
            low_pct = low / start_balance * 100.0 if start_balance > 0 else None
            reason = (f"Límite de pérdida diaria alcanzado: la pérdida realizada hoy (día UTC) llegó a "
                      f"{fmt_num(low, 2, True)} ({fmt_pct(low_pct, 2)}) y el límite es {fmt_pct(pct, 1, False)} del "
                      "saldo con el que empezó el día. Hasta las 00:00 UTC la cuenta demo no abre operaciones nuevas "
                      "(manuales ni del auto-trading) ni ejecuta órdenes pendientes: las que lleguen a su precio "
                      "antes se cancelan")
            if self._day_block_warned.get(account_id) != day:
                self._day_block_warned[account_id] = day
                self._emit("warn", "paper", f"Cuenta demo: {reason[0].lower()}{reason[1:]}.",
                           {"account_id": account_id, "rule": "daily_loss", "day": day})
        return {"day": day, "realized": realized, "low": low, "start_balance": start_balance, "pct": pct,
                "limit": limit, "blocked": blocked, "reason": reason, "until": day + DAY_S}

    async def risk_state(self, account_id: int = 1, account: dict | None = None) -> dict[str, Any]:
        """PaperRisk: open risk vs its cap, positions per correlated group, the day's realized P&L vs the daily loss
        limit and whether new orders / pending fills are blocked."""
        limits = self._risk_limits()
        account = account or await self.account(account_id)
        equity = float(account["equity"])
        daily = await self._daily(account_id, account)
        open_risk = self._open_risk(account_id)
        crypto_one = limits["crypto_one_group"]
        groups: dict[str, dict] = {}
        for pos in sorted(self._open.values(), key=lambda p: p["id"]):
            if pos["account_id"] != account_id:
                continue
            g = risk_group(pos["symbol"], crypto_one)
            item = groups.setdefault(g, {"group": g, "label": risk_group_label(g), "positions": 0, "symbols": [],
                                         "risk": 0.0})
            item["positions"] += 1
            item["risk"] += self._position_risk(pos)
            if traded_symbol(pos) not in item["symbols"]:
                item["symbols"].append(traded_symbol(pos))
        max_group = limits["max_positions_per_group"] or None
        cap = limits["max_open_risk_pct"] or None
        for item in groups.values():
            item["max"] = max_group
            item["full"] = max_group is not None and item["positions"] >= max_group
            item["risk"] = round(item["risk"], 2)
        start = daily["start_balance"]
        return {
            "account_id": account_id,
            "equity": round(equity, 2),
            "open_risk": round(open_risk, 2),
            "open_risk_pct": round(open_risk / equity * 100.0, 4) if equity > 0 else None,
            "max_open_risk_pct": cap,
            "open_positions": sum(1 for p in self._open.values() if p["account_id"] == account_id),
            "groups": sorted(groups.values(), key=lambda g: (-g["positions"], g["label"])),
            "max_positions_per_group": max_group,
            "crypto_one_group": crypto_one,
            "day_start": daily["day"],
            "day_start_balance": round(start, 2),
            "daily_pnl": round(daily["realized"], 2),
            "daily_pnl_pct": round(daily["realized"] / start * 100.0, 4) if start > 0 else None,
            "daily_low": round(daily["low"], 2),
            "daily_loss_limit_pct": daily["pct"] or None,
            "daily_loss_limit": round(daily["limit"], 2) if daily["limit"] is not None else None,
            "unrealized_pnl": account.get("unrealized_pnl"),
            "blocked": daily["blocked"],
            "block_reason": daily["reason"],
            "blocked_until": daily["until"] if daily["blocked"] else None,
            "pending_orders": sum(1 for o in self._orders.values() if o["account_id"] == account_id),
            "note": (f"{DEMO_NOTE}. El riesgo abierto mide la distancia de la entrada al stop actual (sin comisiones); "
                     "la pérdida diaria solo cuenta lo realizado (cierres y comisiones) en el día UTC."),
        }

    async def _check_daily(self, account_id: int, account: dict | None = None) -> None:
        if account_id not in RISK_ACCOUNTS:
            return
        daily = await self._daily(account_id, account)
        if daily["blocked"]:
            raise PaperRiskBlocked(f"{daily['reason']}.", "daily_loss", await self._risk_detail(account_id))

    def _check_group(self, account_id: int, symbol: str) -> None:
        if account_id not in RISK_ACCOUNTS:
            return
        limits = self._risk_limits()
        mx = limits["max_positions_per_group"]
        if mx <= 0:
            return
        group = risk_group(symbol, limits["crypto_one_group"])
        same = [p for p in self._open.values()
                if p["account_id"] == account_id and risk_group(p["symbol"], limits["crypto_one_group"]) == group]
        if len(same) < mx:
            return
        why = (": las criptomonedas suelen moverse juntas, así que cuentan como una sola apuesta" if group == "crypto"
               else "")
        n = len(same)
        raise PaperRiskBlocked(
            f"Límite de posiciones por grupo correlacionado: ya hay {n} {'abierta' if n == 1 else 'abiertas'} en "
            f"{risk_group_label(group)} (máximo {mx}){why}. Cierra alguna o cambia el límite en Ajustes › Riesgo de "
            "cartera.", "group")

    def _check_max_positions(self, account_id: int, cap: int) -> None:
        n = sum(1 for p in self._open.values() if p["account_id"] == account_id)
        if cap > 0 and n >= cap:
            raise PaperRiskBlocked(f"ya hay {n} {'posición abierta' if n == 1 else 'posiciones abiertas'} "
                                   f"(máximo {cap})", "max_positions")

    def _check_open_risk(self, account_id: int, account: dict, new_risk: float) -> None:
        if account_id not in RISK_ACCOUNTS:
            return
        cap = self._risk_limits()["max_open_risk_pct"]
        if cap <= 0 or new_risk <= 0:
            return
        equity = float(account["equity"])
        if equity <= 0:
            raise PaperRiskBlocked("Sin capital disponible en la cuenta demo para abrir más riesgo.", "open_risk")
        open_risk = self._open_risk(account_id)
        allowed = equity * cap / 100.0
        if open_risk + new_risk <= allowed * (1.0 + 1e-9):
            return
        room = allowed - open_risk
        hint = (f" Como mucho puedes arriesgar {fmt_pct(room / equity * 100.0, 2, False)} en esta orden." if room > 0
                else " Cierra alguna posición o protégela (stop en punto de equilibrio) antes de abrir otra.")
        raise PaperRiskBlocked(
            f"Riesgo abierto máximo superado: con esta orden el riesgo total sería "
            f"{fmt_pct((open_risk + new_risk) / equity * 100.0, 2, False)} del capital (máximo {fmt_pct(cap, 1, False)}; "
            f"las posiciones abiertas ya suman {fmt_pct(open_risk / equity * 100.0, 2, False)}).{hint}", "open_risk")

    async def _risk_detail(self, account_id: int) -> dict | None:
        try:
            return await self.risk_state(account_id)
        except Exception:
            return None

    # ------------------------------------------------------------------ orders

    async def open_position(
        self,
        account_id: int = 1,
        symbol: str = "",
        side: str = "long",
        tf: str | None = None,
        qty: float | None = None,
        risk_pct: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        force: bool = False,
        origin: str = "manual",
        price: float | None = None,
        *,
        gate: dict | None = None,
        check_gate: bool = True,
        time_s: int | None = None,
        tps: list[float | None] | None = None,
        slippage: bool = True,
        order: dict | None = None,
        max_positions: int | None = None,
        max_exposure_pct: float | None = None,
        contracts: bool = False,
        margin_pct: float | None = None,
        instrument: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Open a paper position. Returns {"position": Position, "gate": GateResult | None}.

        Accounts 1 and 3 are subject to the portfolio-risk rules (PaperRiskBlocked, never forced). `slippage=False`:
        the fill is exactly `price` (a limit order). `order`: the pending order being filled, marked filled in the
        same transaction (PaperError when it stopped being pending meanwhile, e.g. cancelled by the user).
        `max_positions`: cap on the account's open positions, checked atomically with the insert (PaperRiskBlocked
        rule 'max_positions'): bars of several symbols close at once and their decisions run concurrently.
        `instrument`: an option's contract (NEXUS's own account, kind 'option', see _option_plan): `qty` = contracts
        × 100 and `price` = its theoretical premium; levels on the premium, whole contracts under the exposure cap,
        and its gate decision logged on the underlying. `context`: what NEXUS saw at the entry (its trader's
        regime, score and the gate's score), kept in meta.context for its journal (app.sim.journal)."""
        side = _norm_side(side)
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            raise PaperError("Falta el símbolo")
        if account_id not in ACCOUNTS:
            raise PaperError(f"La cuenta {account_id} no existe")
        try:
            tf_n = normalize_tf(tf or self._setting("paper.auto_tf", "1h") or "1h")
        except ValueError as exc:
            raise PaperError(str(exc)) from None
        if origin not in ORIGINS:
            origin = "manual"
        sl = safe_float(sl)
        tp = safe_float(tp)
        qty = safe_float(qty)
        if qty is not None and qty <= 0:
            raise PaperError("La cantidad debe ser mayor que 0")
        risk = safe_float(risk_pct)
        if risk is None:
            risk = float(self._setting("risk.per_trade_pct", 1.0) or 1.0)
        if not 0 < risk <= 100:
            raise PaperError("El riesgo por operación debe estar entre 0 y 100 %")

        ref = safe_float(price)
        if ref is None:
            ref = await self.last_price(symbol)
        if ref is None or ref <= 0:
            raise PaperError(f"No hay precio disponible para {symbol}")
        if account_id in RISK_ACCOUNTS:  # fast checks first: no gate evaluation for an order the rules refuse anyway
            await self._check_daily(account_id)
            self._check_group(account_id, symbol)

        if gate is None and check_gate:
            gate = await self._gate_for(symbol, tf_n, side, entry=ref, sl=sl, tp=tp, origin=origin)
        if not isinstance(gate, dict):
            gate = None
        if gate is not None and gate.get("verdict") == "AVOID" and not force:
            summary = gate.get("summary") or "el filtro desaconseja esta operación"
            raise PaperRejected(gate, f"Operación bloqueada por el filtro ({gate_line(gate)}): {summary}")

        s = side_sign(side)
        fill = M.slipped_price(ref, s, self._slip_pct(), opening=True) if slippage else float(ref)
        stop = await self._choose_stop(symbol, tf_n, s, fill, sl, gate)
        risk_unit = abs(fill - stop)
        targets = self._choose_targets(s, fill, risk_unit, sl, tp, tps, gate)

        # an index future (contracts=True): whole contracts, the money at the stop = points × multiplier per contract
        spec = futures_spec(symbol) if contracts and qty is None and instrument is None else None
        instrument = dict(instrument) if isinstance(instrument, dict) else None  # an option: sized by the caller
        option = instrument is not None and instrument.get("kind") == "option"
        if option and qty is None:
            raise PaperError("Faltan los contratos de la opción")
        if spec is not None:
            per_contract = risk_unit * float(spec["multiplier"])
            budget = await self.equity(account_id) * risk / 100.0
            n = int(math.floor(budget / per_contract + 1e-9)) if per_contract > 0 else 0
            if n < 1:
                raise PaperRiskBlocked(
                    f"1 contrato {spec['root']} ({spec['name']}) arriesga {fmt_num(per_contract, 2)} con este stop y "
                    f"su riesgo por operación es {fmt_num(budget, 2)}: no cabe ni uno.", "contracts")
            instrument = {"kind": "future", "root": spec["root"], "name": spec["name"],
                          "multiplier": float(spec["multiplier"]), "contracts": n, "margin": float(spec["margin"])}
            qty = n * float(spec["multiplier"])
        if qty is None:
            eq = await self.equity(account_id)
            qty = M.position_size(eq, risk, fill, stop, max_leverage=MAX_LEVERAGE)
        if qty is None or qty <= 0 or not math.isfinite(qty):
            raise PaperError("Tamaño de posición nulo: revisa el riesgo, el stop y el saldo")
        fee = qty * fill * self._fee_pct(symbol) / 100.0
        atr = await self._atr_estimate(symbol, tf_n, risk_unit, origin)
        entry_time = int(time_s) if time_s is not None else now_s()
        # The gate decision is judged over the trade's time stop counted from its own bar (a replay decision is
        # historical, a synthetic one may lie ahead of the clock): never from the wall clock. Same target_at
        # convention as the watcher and the scanner (service_watch.gate_target_ms).
        decision_time = int(safe_float(gate.get("time")) or entry_time) if gate else entry_time
        target_at = gate_target_ms(decision_time, tf_n, int(self._setting("nexus.time_stop_bars", 48) or 48))
        meta = {
            "initial_sl": stop,
            "risk_unit": risk_unit,
            "atr": atr,
            "fees": fee,
            "realized_gross": 0.0,
            "high_water": fill,
            "low_water": fill,
            "last_price": ref,
            "risk_pct": risk,
            # partial targets close thirds: a future needs a multiple of 3 contracts for them
            "partial": bool(self._setting("paper.partial_tp", True)) and (
                instrument is None or int(instrument["contracts"]) % 3 == 0),
            "instrument": instrument,
            "advice": None,
            "warnings": [],
            "gate_summary": gate.get("summary") if gate else None,
            "forced": bool(force and gate is not None and gate.get("verdict") == "AVOID"),
        }
        if isinstance(context, dict):  # for its journal: the volatility of the market at the entry, % of the price
            meta["context"] = {**context, "atr_pct": (round(atr / fill * 100.0, 4) if atr and fill > 0 and not option
                                                      else None)}
        if order is not None:
            meta.update({"order_id": order["id"], "order_kind": order["kind"], "order_price": order["price"]})
        verdict = gate.get("verdict") if gate else None
        gate_score = safe_float(gate.get("score")) if gate else None
        async with self._lock:
            if order is not None and self._orders.get(order["id"]) is not order:
                raise PaperError(f"La orden #{order['id']} ya no está pendiente")
            if max_positions:
                self._check_max_positions(account_id, int(max_positions))
            if margin_pct is not None and instrument is not None and instrument.get("kind") == "future":
                # the margin budget, atomic with the insert like the risk rules
                fit = self._margin_room(account_id, instrument, float(margin_pct), await self.equity(account_id))
                if fit < int(instrument["contracts"]):
                    instrument["contracts"] = fit
                    qty = fit * float(instrument["multiplier"])
                    fee = qty * fill * self._fee_pct(symbol) / 100.0
                    meta["fees"] = fee
                    meta["partial"] = bool(meta["partial"]) and fit % 3 == 0
            if max_exposure_pct is not None and asset_class(symbol) not in MARGIN_CLASSES:
                capped = self._cap_exposure(account_id, symbol, qty, fill, float(max_exposure_pct),
                                            await self.equity(account_id))
                if capped < qty:  # a smaller position that fits the cash account
                    if option:  # whole contracts only
                        mult = float(instrument["multiplier"])
                        fit = int(math.floor(capped / mult + 1e-9))
                        if fit < 1:
                            raise PaperRiskBlocked(
                                f"Exposición máxima: la prima de 1 contrato ({fmt_num(fill * mult, 2)}) no cabe en el "
                                f"efectivo libre ({fmt_num(capped * fill, 2)}).", "exposure")
                        instrument["contracts"] = fit
                        meta["partial"] = bool(meta["partial"]) and fit % 3 == 0
                        capped = fit * mult
                    qty = capped
                    fee = qty * fill * self._fee_pct(symbol) / 100.0
                    meta["fees"] = fee
                    meta["exposure_capped"] = True
            if account_id in RISK_ACCOUNTS:  # authoritative check, atomic with the insert (no other order opens)
                await self._check_daily(account_id)
                self._check_group(account_id, symbol)
                self._check_open_risk(account_id, await self.account(account_id), qty * risk_unit)
            async with self.rt.db.transaction() as tx:
                pid = await tx.execute(
                    "INSERT INTO paper_positions(account_id, symbol, tf, side, qty, qty_open, entry_price, entry_time, sl, "
                    "tp1, tp2, tp3, trail, tp_hits, status, pnl, pnl_pct, origin, gate_verdict, gate_score, meta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 'open', ?, 0, ?, ?, ?, ?)",
                    (account_id, symbol, tf_n, side, qty, qty, fill, entry_time, stop, targets[0], targets[1],
                     targets[2], -fee, origin, verdict, gate_score, json_dumps(meta)),
                )
                await tx.execute(
                    "INSERT INTO paper_fills(position_id, account_id, time, side, qty, price, fee, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'Apertura')",
                    (pid, account_id, entry_time, "buy" if s > 0 else "sell", qty, fill, fee),
                )
                await tx.execute(
                    "UPDATE paper_accounts SET balance = balance - ?, updated_at=? WHERE id=?",
                    (fee, now_ms(), account_id),
                )
                if gate is not None:
                    g_sym, g_side, g_levels = symbol, side, (fill, stop, targets[0], targets[1])
                    if option:  # decided on the underlying: logged there with the gate's levels (these are premiums)
                        g_sym = str(instrument["underlying"]).upper()
                        g_side = "long" if instrument.get("right") == "call" else "short"
                        g_levels = (safe_float(gate.get("entry")) or safe_float(instrument.get("spot")),
                                    safe_float(gate.get("sl")), safe_float(gate.get("tp1")), safe_float(gate.get("tp2")))
                    await tx.execute(
                        "INSERT OR IGNORE INTO gate_log(symbol, tf, time, direction, verdict, score, win_prob, entry, sl, "
                        "tp1, tp2, checks, taken, source, created_at, target_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                        (
                            g_sym, tf_n, decision_time, g_side, verdict or "WAIT", gate_score or 0.0,
                            safe_float(gate.get("win_prob")), *g_levels,
                            json_dumps(gate_checks_vector(gate)), self._gate_source(g_sym, origin), now_ms(), target_at,
                        ),
                    )
                if order is not None:
                    order_meta = dict(order.get("meta") or {})
                    order_meta.update({"fill_gate": verdict, "fill_gate_score": gate_score, "fill_ref": ref,
                                       "fill_slippage": bool(slippage)})
                    await tx.execute(
                        "UPDATE paper_orders SET status='filled', position_id=?, fill_price=?, fill_time=?, updated_at=?, "
                        "meta=? WHERE id=?",
                        (pid, fill, entry_time, now_ms(), json_dumps(order_meta), order["id"]),
                    )
            pos = {
                "id": pid, "account_id": account_id, "symbol": symbol, "tf": tf_n, "side": side, "qty": qty,
                "qty_open": qty, "entry_price": fill, "entry_time": entry_time, "sl": stop, "tp1": targets[0],
                "tp2": targets[1], "tp3": targets[2], "trail": None, "tp_hits": 0, "status": "open",
                "exit_price": None, "exit_time": None, "pnl": -fee, "pnl_pct": 0.0, "r_multiple": None,
                "exit_reason": None, "origin": origin, "gate_verdict": verdict, "gate_score": gate_score, "meta": meta,
            }
            self._open[pid] = pos
            if order is not None:
                order.update({"status": "filled", "position_id": pid, "fill_price": fill, "fill_time": entry_time,
                              "updated_at": now_ms()})
                self._orders.pop(order["id"], None)
                self._watch_from.pop(order["id"], None)
        if origin != "sim":
            traded = str(instrument["underlying"]).upper() if option else symbol
            if not option:  # an option is priced from its underlying, never from a cached price of its own
                self._prices.setdefault(symbol, (ref, time.monotonic()))
            self._stale_warned.discard(traded)  # a new position gets its own "sin precio en vivo" notice
        forced = " · FORZADA contra el filtro" if meta["forced"] else ""
        tps_txt = " · ".join(f"TP{i + 1} {fmt_price(t)}" for i, t in enumerate(targets) if t is not None)
        via = ""
        if order is not None:
            via = (f" · {ORDER_LABEL.get((side, order['kind']), 'orden').lower()} #{order['id']} a "
                   f"{fmt_price(order['price'])} ejecutada")
        data: dict[str, Any] = {"position_id": pid, "account_id": account_id, "symbol": symbol, "side": side}
        if order is not None:
            data["order_id"] = order["id"]
        if option:
            n = int(instrument["contracts"])
            head = (f"Abierto {instrument.get('name') or symbol} ({instrument.get('right')}) · {n} "
                    f"{'contrato' if n == 1 else 'contratos'} @ {fmt_price(fill)} por acción (prima teórica)")
        else:
            head = f"Abierto {symbol} {SIDE_LABEL[side]} {fmt_qty(qty)} @ {fmt_price(fill)}"
        self._emit(
            "trade",
            self._source(origin),
            head + f" · SL {fmt_price(stop)}"
            + (f" · {tps_txt}" if tps_txt else "") + (f" · Filtro {gate_line(gate)}" if gate else "") + forced + via,
            data,
        )
        await self.publish(account_id)
        if config.BACKGROUND and origin != "sim":
            await self._sync_subscriptions()
        return {"position": self.position_dict(pos), "gate": gate}

    async def _gate_for(self, symbol: str, tf: str, side: str, *, entry: float, sl: float | None, tp: float | None,
                        origin: str) -> dict | None:
        """The Trade Gate for an idea (None when the engine is not loaded or the evaluation failed: logged)."""
        gate, _why = await self._gate_eval(symbol, tf, side, entry=entry, sl=sl, tp=tp, origin=origin)
        return gate

    async def _gate_eval(self, symbol: str, tf: str, side: str, *, entry: float, sl: float | None,
                         tp: float | None, origin: str) -> tuple[dict | None, str]:
        """(GateResult, "") or (None, Spanish reason why the gate could not be consulted; failures are logged)."""
        nexus = self.rt.nexus
        if nexus is None or not hasattr(nexus, "gate"):
            return None, "el motor NEXUS no está cargado"
        try:
            gate = await asyncio.wait_for(nexus.gate(symbol, tf, side, entry=entry, sl=sl, tp=tp), timeout=90)
        except asyncio.TimeoutError:
            if self._log.should_log(f"gate:{symbol}", 60):
                self._emit("warn", self._source(origin), f"No se pudo evaluar el filtro para {symbol}: sin respuesta en 90 s")
            return None, "no respondió en 90 s"
        except Exception as exc:
            if self._log.should_log(f"gate:{symbol}", 60):
                self._emit("warn", self._source(origin),
                           f"No se pudo evaluar el filtro para {symbol}: {type(exc).__name__}: {exc}")
            return None, f"error al evaluarlo: {exc}" if str(exc) else f"error al evaluarlo ({type(exc).__name__})"
        if not isinstance(gate, dict):
            return None, "respuesta no válida"
        return gate, ""

    async def _choose_stop(self, symbol: str, tf: str, s: int, fill: float, sl: float | None, gate: dict | None) -> float:
        side_word = "por debajo" if s > 0 else "por encima"
        if sl is not None:
            if s * (fill - sl) <= 0:
                raise PaperError(f"El stop ({fmt_price(sl)}) debe estar {side_word} del precio de entrada "
                                 f"({fmt_price(fill)})")
            return float(sl)
        gsl = safe_float(gate.get("sl")) if gate else None
        if gsl is not None and s * (fill - gsl) > 0:
            return gsl
        atr = None
        if self.rt.hub is not None:
            try:
                cs = await asyncio.wait_for(self.rt.hub.get_candles(symbol, tf, 60), timeout=20)
                atr = atr_from_candles(cs)
            except Exception:
                atr = None
        dist = atr * float(self._setting("nexus.sl_atr", 2.0) or 2.0) if atr else fill * 0.02
        return fill - s * dist

    def _choose_targets(self, s: int, fill: float, risk_unit: float, sl: float | None, tp: float | None,
                        tps: list[float | None] | None, gate: dict | None) -> list[float | None]:
        def valid(x: Any) -> float | None:
            v = safe_float(x)
            return v if v is not None and s * (v - fill) > 0 else None

        if tps is not None:
            out = [valid(x) for x in list(tps)[:3]]
            out += [None] * (3 - len(out))
            if any(v is not None for v in out):
                return out
        if tp is not None:
            v = valid(tp)
            if v is None:
                raise PaperError(f"El objetivo ({fmt_price(tp)}) debe estar {'por encima' if s > 0 else 'por debajo'} "
                                 f"del precio de entrada ({fmt_price(fill)})")
            return [v, None, None]
        if sl is None and gate is not None:
            out = [valid(gate.get(k)) for k in ("tp1", "tp2", "tp3")]
            if out[0] is not None:
                return out
        mults = self._setting("nexus.tp_r", [1.0, 2.0, 3.0]) or [1.0, 2.0, 3.0]
        out = [fill + s * float(m) * risk_unit for m in list(mults)[:3]]
        return out + [None] * (3 - len(out))

    async def _atr_estimate(self, symbol: str, tf: str, risk_unit: float, origin: str) -> float:
        sl_atr = float(self._setting("nexus.sl_atr", 2.0) or 2.0)
        fallback = risk_unit / max(sl_atr, 0.1)
        if origin == "sim" or self.rt.hub is None or opt.is_option(symbol):  # an option has no candles of its own
            return fallback
        try:
            cs = await asyncio.wait_for(self.rt.hub.get_candles(symbol, tf, 60), timeout=15)
            atr = atr_from_candles(cs)
        except Exception:
            atr = None
        return atr if atr else fallback

    # ------------------------------------------------------------------ pending orders (limit / stop)

    def _order_expiry(self, expiry: Any, bars: Any, when: Any, tf: str) -> tuple[str, int | None, int | None]:
        """(expiry, expire_bars, expire_time): 'gtc' (until cancelled), 'bars' (N bars of tf from now) or 'date'."""
        text = str(expiry or "").strip().lower()
        if not text:
            text = "date" if when is not None else "bars" if bars is not None else "gtc"
        text = {"hasta cancelar": "gtc", "hasta cancelarla": "gtc", "velas": "bars", "fecha": "date"}.get(text, text)
        now = now_s()
        if text == "gtc":
            return "gtc", None, None
        if text == "bars":
            n = _finite_or_none(bars, "las velas de validez")
            if n is None or n != int(n) or not 1 <= n <= MAX_EXPIRY_BARS:
                raise PaperError(f"Indica cuántas velas es válida la orden: un número entero de 1 a {MAX_EXPIRY_BARS}")
            return "bars", int(n), now + int(n) * _bar_seconds(tf)
        if text == "date":
            t = _finite_or_none(when, "la fecha de caducidad")
            if t is None:
                raise PaperError("Falta la fecha de caducidad de la orden")
            if t > 1e11:  # unix milliseconds
                t /= 1000.0
            if t <= now + 60:
                raise PaperError("La fecha de caducidad debe estar en el futuro (al menos dentro de 1 minuto)")
            if t > now + MAX_EXPIRY_DAYS * DAY_S:
                raise PaperError(f"La caducidad puede estar como mucho a {MAX_EXPIRY_DAYS} días")
            return "date", None, int(t)
        raise PaperError("Caducidad no válida: usa 'gtc' (hasta cancelarla), 'bars' (N velas) o 'date' (una fecha)")

    @staticmethod
    def _check_order_side(s: int, kind: str, level: float, market: float) -> None:
        lvl, mkt = fmt_price(level), fmt_price(market)
        if kind == "limit" and s > 0 and level >= market:
            raise PaperError(f"Una compra límite va por debajo del precio actual ({mkt}): a {lvl} se ejecutaría ya. "
                             f"Para comprar si el precio sube hasta {lvl}, usa una compra stop.")
        if kind == "stop" and s > 0 and level <= market:
            raise PaperError(f"Una compra stop va por encima del precio actual ({mkt}). Para comprar más barato, a "
                             f"{lvl}, usa una compra límite.")
        if kind == "limit" and s < 0 and level <= market:
            raise PaperError(f"Una venta límite va por encima del precio actual ({mkt}): a {lvl} se ejecutaría ya. "
                             f"Para vender si el precio cae hasta {lvl}, usa una venta stop.")
        if kind == "stop" and s < 0 and level >= market:
            raise PaperError(f"Una venta stop va por debajo del precio actual ({mkt}). Para vender más caro, a "
                             f"{lvl}, usa una venta límite.")

    async def place_order(
        self,
        account_id: int = 1,
        symbol: str = "",
        side: str = "long",
        kind: str = "limit",
        price: float | None = None,
        tf: str | None = None,
        qty: float | None = None,
        risk_pct: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        expiry: str | None = None,
        expire_bars: int | None = None,
        expire_time: int | None = None,
        force: bool = False,
        origin: str = "manual",
    ) -> dict:
        """Leave a pending limit / stop entry order in the demo account. Returns {"order": PaperOrder, "gate"}.

        The gate is asked at the order's own price (NO TOMAR -> PaperRejected unless `force`) and asked AGAIN when the
        order fills, where a NO TOMAR cancels it. The risk rules are checked now (daily loss, group, estimated risk)
        and again at the fill."""
        side = _norm_side(side)
        kind = {"limite": "limit", "límite": "limit", "lmt": "limit"}.get(str(kind or "").strip().lower(),
                                                                          str(kind or "").strip().lower())
        if kind not in ORDER_KINDS:
            raise PaperError("Tipo de orden no válido: usa 'limit' (límite) o 'stop'")
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            raise PaperError("Falta el símbolo")
        if account_id != 1:
            raise PaperError("Las órdenes pendientes solo existen en la cuenta demo Principal")
        try:
            tf_n = normalize_tf(tf or self._setting("paper.auto_tf", "1h") or "1h")
        except ValueError as exc:
            raise PaperError(str(exc)) from None
        if origin not in ("manual", "auto", "script"):
            origin = "manual"
        level = _finite_or_none(price, "el precio de la orden")
        if level is None or level <= 0:
            raise PaperError("Indica el precio de la orden (mayor que 0)")
        sl = _finite_or_none(sl, "el stop")
        tp = _finite_or_none(tp, "el objetivo")
        qty = _finite_or_none(qty, "la cantidad")
        risk = _finite_or_none(risk_pct, "el riesgo")
        if qty is not None and qty <= 0:
            raise PaperError("La cantidad debe ser mayor que 0")
        if risk is not None and not 0 < risk <= 100:
            raise PaperError("El riesgo por operación debe estar entre 0 y 100 %")
        s = side_sign(side)
        if sl is not None and s * (level - sl) <= 0:
            raise PaperError(f"El stop ({fmt_price(sl)}) debe estar {'por debajo' if s > 0 else 'por encima'} del precio "
                             f"de la orden ({fmt_price(level)})")
        if tp is not None and s * (tp - level) <= 0:
            raise PaperError(f"El objetivo ({fmt_price(tp)}) debe estar {'por encima' if s > 0 else 'por debajo'} del "
                             f"precio de la orden ({fmt_price(level)})")
        exp, exp_bars, exp_time = self._order_expiry(expiry, expire_bars, expire_time, tf_n)
        if sum(1 for o in self._orders.values() if o["account_id"] == account_id) >= MAX_PENDING_ORDERS:
            raise PaperError(f"Ya hay {MAX_PENDING_ORDERS} órdenes pendientes: cancela alguna antes de dejar otra")
        market = await self.last_price(symbol)
        if market is None or market <= 0:
            raise PaperError(f"No hay precio disponible para {symbol}: hace falta para comprobar el lado de la orden")
        self._check_order_side(s, kind, level, market)
        account = await self.account(account_id)
        await self._check_daily(account_id, account)
        self._check_group(account_id, symbol)

        gate = await self._gate_for(symbol, tf_n, side, entry=level, sl=sl, tp=tp, origin=origin)
        if gate is not None and gate.get("verdict") == "AVOID" and not force:
            summary = gate.get("summary") or "el filtro desaconseja esta operación"
            raise PaperRejected(gate, f"Orden bloqueada por el filtro ({gate_line(gate)}) a {fmt_price(level)}: {summary}")
        # estimated risk at the fill: the sizing target (risk % of equity), or a fixed size times the stop distance
        equity = float(account["equity"])
        if qty is None:
            estimate = equity * (risk if risk is not None else float(self._setting("risk.per_trade_pct", 1.0) or 1.0)) / 100.0
        else:
            gsl = safe_float(gate.get("sl")) if gate else None
            stop = sl if sl is not None else gsl if gsl is not None and s * (level - gsl) > 0 else level * (1 - s * 0.02)
            estimate = qty * abs(level - stop)
        self._check_open_risk(account_id, account, estimate)

        ts = now_ms()
        verdict = gate.get("verdict") if gate else None
        gate_score = safe_float(gate.get("score")) if gate else None
        meta = {"forced": bool(force and verdict == "AVOID"), "gate_summary": gate.get("summary") if gate else None,
                "market_at_placement": market}
        async with self._lock:
            oid = await self.rt.db.execute(
                "INSERT INTO paper_orders(account_id, symbol, tf, side, kind, price, qty, risk_pct, sl, tp, expiry, "
                "expire_bars, expire_time, status, origin, gate_verdict, gate_score, created_at, updated_at, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (account_id, symbol, tf_n, side, kind, level, qty, risk, sl, tp, exp, exp_bars, exp_time, origin,
                 verdict, gate_score, ts, ts, json_dumps(meta)),
            )
            order = {"id": int(oid), "account_id": account_id, "symbol": symbol, "tf": tf_n, "side": side,
                     "kind": kind, "price": level, "qty": qty, "risk_pct": risk, "sl": sl, "tp": tp, "expiry": exp,
                     "expire_bars": exp_bars, "expire_time": exp_time, "status": "pending", "origin": origin,
                     "gate_verdict": verdict, "gate_score": gate_score, "position_id": None, "fill_price": None,
                     "fill_time": None, "reason": None, "cancelled_by": None, "created_at": ts, "updated_at": ts,
                     "meta": meta}
            self._orders[order["id"]] = order
            self._watch_from[order["id"]] = now_s()
        self._prices.setdefault(symbol, (market, time.monotonic()))
        levels = " · ".join(x for x in (f"SL {fmt_price(sl)}" if sl is not None else "",
                                        f"objetivo {fmt_price(tp)}" if tp is not None else "") if x)
        self._emit(
            "info", "paper",
            f"Orden demo #{order['id']}: {ORDER_LABEL[(side, kind)].lower()} {symbol} a {fmt_price(level)}"
            + (f" · {levels}" if levels else " · niveles de NEXUS al ejecutarse") + f" · {self._expiry_text(order)}"
            + (f" · Filtro {gate_line(gate)}" if gate else "") + (" · FORZADA contra el filtro" if meta["forced"] else ""),
            {"order_id": order["id"], "account_id": account_id, "symbol": symbol, "side": side, "kind": kind},
        )
        await self.publish(account_id)
        if config.BACKGROUND:
            await self._sync_subscriptions()
        return {"order": self.order_dict(order), "gate": gate}

    async def cancel_orders(self, order_id: int | None = None, symbol: str | None = None, all: bool = False, *,
                            account_id: int = 1) -> list[dict]:
        """Cancel pending orders: one by id, every one of a symbol, or all of the account."""
        if order_id is None and not symbol and not all:
            raise PaperError("Indica qué cancelar: un id de orden, un símbolo o 'all'")
        if order_id is not None:
            order = self._orders.get(int(order_id))
            if order is None or order["account_id"] != account_id:
                raise PaperError(f"No hay una orden pendiente con id {order_id}")
            targets = [order]
        elif all:
            targets = [o for o in self._orders.values() if o["account_id"] == account_id]
        else:
            sym = str(symbol).upper()
            targets = [o for o in self._orders.values() if o["account_id"] == account_id and o["symbol"] == sym]
            if not targets:
                raise PaperError(f"No hay órdenes pendientes en {sym}")
        out: list[dict] = []
        for order in sorted(targets, key=lambda o: o["id"]):
            if await self._end_order(order, "Cancelada por el usuario", "user"):
                out.append(self.order_dict(order))
        if out:
            await self.publish(account_id)
            if config.BACKGROUND:
                await self._sync_subscriptions()
        elif order_id is not None:
            # it stopped being pending while waiting for the engine (its fill was being booked): never report a
            # cancellation that did not happen
            raise PaperError(f"No hay una orden pendiente con id {order_id}: se acaba de ejecutar, cancelar o caducar")
        return out

    async def _end_order(self, order: dict, reason: str, by: str, *, status: str = "cancelled",
                         level: str = "info") -> bool:
        """Mark a pending order cancelled / expired (no-op when it is no longer pending). True when it changed."""
        async with self._lock:
            if self._orders.get(order["id"]) is not order:
                return False
            ts = now_ms()
            cancelled_by = by if status == "cancelled" else None
            await self.rt.db.execute(
                "UPDATE paper_orders SET status=?, reason=?, cancelled_by=?, updated_at=? WHERE id=? AND status='pending'",
                (status, reason, cancelled_by, ts, order["id"]),
            )
            order.update({"status": status, "reason": reason, "cancelled_by": cancelled_by, "updated_at": ts})
            self._orders.pop(order["id"], None)
            self._watch_from.pop(order["id"], None)
        word = "caducada" if status == "expired" else "cancelada"
        self._emit(level, "paper",
                   f"Orden demo #{order['id']} {word}: {ORDER_LABEL.get((order['side'], order['kind']), '').lower()} "
                   f"{order['symbol']} a {fmt_price(order['price'])} · {reason}",
                   {"order_id": order["id"], "account_id": order["account_id"], "status": status, "by": by})
        return True

    async def _expire_orders(self, now: int | None = None) -> int:
        """Expire the pending orders whose validity ended (never one whose fill is being decided)."""
        t = now_s() if now is None else int(now)
        due = [o for o in self._orders.values() if o.get("expire_time") is not None and t >= int(o["expire_time"])
               and o["id"] not in self._executing]
        n = 0
        for order in sorted(due, key=lambda o: o["id"]):
            if order.get("expiry") == "bars":
                bars = order.get("expire_bars")
                passed = "pasó 1 vela" if bars == 1 else f"pasaron {bars} velas"
                reason = f"Caducada: {passed} de {order.get('tf') or '1h'} sin ejecutarse"
            else:
                reason = "Caducada: llegó su fecha límite sin ejecutarse"
            n += int(await self._end_order(order, reason, "expiry", status="expired"))
        if n:
            await self.publish(1)
            if config.BACKGROUND:
                await self._sync_subscriptions()
        return n

    def _check_orders(self, sym: str, high: float, low: float, close: float, *, open_: float | None,
                      bar_time: int | None, bar_s: int, live: bool, fill_time: int) -> int:
        """Trigger the pending orders of `sym` touched / crossed by this live price update (bar_time None = a polled
        price; replayed bars go to `_orders_missed_live` instead). Each triggered order is decided in its own task
        (gate at the fill price), never inside the feed listener."""
        sim_symbol = sym.startswith("SIM:")
        started = 0
        for order in [o for o in self._orders.values() if o["symbol"] == sym]:
            oid = order["id"]
            if oid in self._executing:
                continue
            watched = self._watch_from.get(oid, int(order["created_at"] // 1000))
            exp = order.get("expire_time")
            moment = now_s() if live or bar_time is None else bar_time
            if exp is not None and moment >= int(exp):
                continue  # out of its validity (the monitor expires it)
            if bar_time is not None and not sim_symbol:
                if not live and bar_time + bar_s <= watched:
                    continue  # a replayed bar that ended before the order was watched
                use_hl = bar_time >= watched
            else:
                use_hl = bar_time is not None  # synthetic bars come after the order; a polled price is a close
            ref = order_trigger(side_sign(order["side"]), order["kind"], float(order["price"]), high, low, close,
                                open_=open_ if use_hl else None, use_hl=use_hl)
            if ref is None or ref <= 0:
                continue
            self._executing.add(oid)
            self._spawn(self._execute_order(oid, ref, int(fill_time)))
            started += 1
        return started

    def _orders_missed_live(self, sym: str, high: float, low: float, close: float, *, open_: float | None,
                            bar_time: int, bar_s: int) -> int:
        """A final bar that did not arrive live (replayed after a feed stall, or delayed data stamped in market time)
        never fills a pending entry of `sym`: the gate would be asked now about a fill dated in the past, with prices
        that came after it, and the new position would miss the rest of the replay. As after a restart, the orders are
        judged by close only until a bar that begins after now; an order whose level that bar reached gets one notice
        (at most every 6 h) and stays pending. Returns how many such orders were noticed."""
        now = now_s()
        noticed = 0
        for order in [o for o in self._orders.values() if o["symbol"] == sym]:
            oid = order["id"]
            watched = self._watch_from.get(oid, int(order["created_at"] // 1000))
            if bar_time + bar_s <= watched:
                continue  # a bar that ended before the order was watched
            self._watch_from[oid] = max(watched, now)
            if oid in self._executing:
                continue
            use_hl = bar_time >= watched
            ref = order_trigger(side_sign(order["side"]), order["kind"], float(order["price"]), high, low, close,
                                open_=open_ if use_hl else None, use_hl=use_hl)
            if ref is None or not self._log.should_log(f"order-missed:{oid}", 6 * 3600):
                continue
            noticed += 1
            self._emit(
                "info", "paper",
                f"Orden demo #{oid} ({ORDER_LABEL.get((order['side'], order['kind']), 'orden').lower()} {sym} a "
                f"{fmt_price(order['price'])}): el precio llegó a ese nivel en una vela de 1m que no llegó en vivo (la "
                f"de {self._when(int(bar_time))}, recuperada tras un corte del feed o con datos retrasados). Las "
                "órdenes pendientes solo se ejecutan con precios en vivo, así que sigue pendiente.",
                {"order_id": oid, "account_id": order["account_id"], "symbol": sym, "bar_time": int(bar_time)},
            )
        return noticed

    async def _execute_order(self, oid: int, ref: float, fill_time: int) -> None:
        """Fill a triggered pending order: risk rules, the gate at the fill price (NO TOMAR cancels the order), then
        the position exactly like a market one (SL / TP relative to the fill). Any refusal cancels the order with a
        Spanish reason; a limit fills at `ref` (no slippage), a stop at market (slippage)."""
        order = self._orders.get(oid)
        try:
            if order is None or order["status"] != "pending":
                return
            try:
                # the risk rules at the trigger, before asking the gate (an order they refuse needs no verdict);
                # open_position checks them again, atomically with the insert
                await self._check_daily(order["account_id"])
                self._check_group(order["account_id"], order["symbol"])
            except PaperRiskBlocked as blk:
                await self._end_order(order, f"No se ejecutó al llegar a {fmt_price(ref)}: {blk.message}", "risk",
                                      level="warn")
            else:
                gate, why = await self._gate_eval(order["symbol"], order.get("tf") or "1h", order["side"], entry=ref,
                                                  sl=order.get("sl"), tp=order.get("tp"),
                                                  origin=order.get("origin") or "manual")
                if gate is None:
                    # A pending order is a promise to ask the filter again at the fill: without its verdict (engine
                    # missing, error, timeout) it is cancelled, never opened blind.
                    await self._end_order(
                        order,
                        f"Cancelada al llegar a {fmt_price(ref)}: no se pudo consultar el filtro ({why}); una orden "
                        "pendiente nunca se abre sin su veredicto",
                        "gate", level="warn")
                else:
                    try:
                        await self.open_position(
                            order["account_id"], order["symbol"], order["side"], order.get("tf"),
                            qty=order.get("qty"), risk_pct=order.get("risk_pct"), sl=order.get("sl"),
                            tp=order.get("tp"), force=False, origin=order.get("origin") or "manual", price=ref,
                            gate=gate, check_gate=False, time_s=fill_time, slippage=order["kind"] == "stop",
                            order=order,
                        )
                    except PaperRejected as rej:
                        summary = (rej.gate or {}).get("summary") or "el filtro desaconseja la operación"
                        await self._end_order(
                            order,
                            f"Cancelada al ejecutarse a {fmt_price(ref)}: el filtro dice {gate_line(rej.gate)} "
                            f"({summary})",
                            "gate", level="warn")
                    except PaperRiskBlocked as blk:
                        await self._end_order(order, f"No se ejecutó al llegar a {fmt_price(ref)}: {blk.message}",
                                              "risk", level="warn")
                    except PaperError as exc:
                        await self._end_order(order, f"No se ejecutó al llegar a {fmt_price(ref)}: {exc}", "levels",
                                              level="warn")
                    else:
                        return  # filled: open_position published the account and synced the subscriptions
            # refused: the order left the pending list -> the page and the live subscriptions follow
            await self.publish(order["account_id"])
            if config.BACKGROUND:
                await self._sync_subscriptions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._log.should_log(f"order:{oid}", 300):
                self._emit("error", "paper", f"Orden demo #{oid}: no se pudo ejecutar: {type(exc).__name__}: {exc}")
        finally:
            self._executing.discard(oid)

    # ------------------------------------------------------------------ fills & closing

    async def _apply_fills(self, pos: dict, fills: list[Fill], time_s: int, st: LevelState | None = None,
                           *, emit: bool = True) -> bool:
        """Book fills / level changes for a position (caller holds self._lock). True when the position closed."""
        if not fills and st is None:
            return False
        meta = pos["meta"]
        s = side_sign(pos["side"])
        fee_pct, slip = self._fee_pct(pos["symbol"]), self._slip_pct()
        closed = False
        rows: list[tuple] = []
        delta_balance = 0.0
        events: list[str] = []
        for f in fills:
            qty = min(f.qty, pos["qty_open"])
            if qty <= 0:
                continue
            px = M.slipped_price(f.price, s, slip, opening=False) if f.market else f.price
            fee = qty * px * fee_pct / 100.0
            gross = s * (px - pos["entry_price"]) * qty
            meta["realized_gross"] = float(meta.get("realized_gross", 0.0)) + gross
            meta["fees"] = float(meta.get("fees", 0.0)) + fee
            delta_balance += gross - fee
            pos["qty_open"] = max(pos["qty_open"] - qty, 0.0)
            rows.append((pos["id"], pos["account_id"], time_s, "sell" if s > 0 else "buy", qty, px, fee, f.label or f.reason))
            if f.kind == "close" or pos["qty_open"] <= pos["qty"] * 1e-9:
                closed = True
                pos["qty_open"] = 0.0
                pos["status"] = "closed"
                pos["exit_price"] = px
                pos["exit_time"] = time_s
                pos["exit_reason"] = f.reason
                meta["exit_label"] = f.label or EXIT_LABEL.get(f.reason, f.reason)
            else:
                r_part = s * (px - pos["entry_price"]) / max(float(meta.get("risk_unit") or 0.0), 1e-12)
                extra = " · stop a punto de equilibrio" if f.reason == "TP1" else " · stop dinámico activado" if f.reason == "TP2" else ""
                events.append(f"{self._pos_label(pos)}: {f.reason} alcanzado ({fmt_r(r_part)}) · "
                              f"1/3 cerrado a {fmt_price(px)}{extra}")
        if st is not None:
            pos["sl"] = st.sl
            pos["trail"] = st.trail
            pos["tp_hits"] = st.tp_hits
            meta["high_water"] = st.high_water
            meta["low_water"] = st.low_water
        risk_unit = float(meta.get("risk_unit") or 0.0)
        net = float(meta.get("realized_gross", 0.0)) - float(meta.get("fees", 0.0))
        pos["pnl"] = net
        if closed:
            notional = pos["qty"] * pos["entry_price"]
            pos["pnl_pct"] = net / notional * 100.0 if notional > 0 else 0.0
            # net of entry + exit fees, like the engine's and the backtest's R (same sign as the P&L)
            pos["r_multiple"] = net / (pos["qty"] * risk_unit) if risk_unit > 0 and pos["qty"] > 0 else None
            # its journal: how far it went against / in favour (R) and why it ended so (app.sim.journal)
            meta["journal"] = J.close_entry(
                side=pos["side"], entry=pos["entry_price"], risk_unit=risk_unit, high_water=meta.get("high_water"),
                low_water=meta.get("low_water"), exit_price=pos["exit_price"], r=pos.get("r_multiple"),
                reason=pos["exit_reason"], entry_time=pos["entry_time"], exit_time=pos["exit_time"])
        async with self.rt.db.transaction() as tx:
            if rows:
                await tx.executemany(
                    "INSERT INTO paper_fills(position_id, account_id, time, side, qty, price, fee, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            if delta_balance:
                await tx.execute("UPDATE paper_accounts SET balance = balance + ?, updated_at=? WHERE id=?",
                                 (delta_balance, now_ms(), pos["account_id"]))
            await tx.execute(
                "UPDATE paper_positions SET qty_open=?, sl=?, trail=?, tp_hits=?, status=?, exit_price=?, exit_time=?, "
                "pnl=?, pnl_pct=?, r_multiple=?, exit_reason=?, meta=? WHERE id=?",
                (pos["qty_open"], pos["sl"], pos["trail"], pos["tp_hits"], pos["status"], pos["exit_price"],
                 pos["exit_time"], pos["pnl"], pos.get("pnl_pct") or 0.0, pos.get("r_multiple"), pos["exit_reason"],
                 json_dumps(meta), pos["id"]),
            )
        source = self._source(pos)
        if emit:
            for msg in events:
                self._emit("trade", source, msg, {"position_id": pos["id"], "account_id": pos["account_id"]})
        if closed:
            self._open.pop(pos["id"], None)
            self._warned.pop(pos["id"], None)
        if closed and emit:
            self._emit(
                "trade",
                source,
                f"Cerrado {self._pos_label(pos)} {fmt_r(pos.get('r_multiple'))} "
                f"({fmt_pct(pos.get('pnl_pct'), 2)} · {fmt_num(pos['pnl'], 2, True)}) · {meta.get('exit_label')}",
                {"position_id": pos["id"], "account_id": pos["account_id"], "pnl": pos["pnl"],
                 "r_multiple": pos.get("r_multiple"), "reason": pos["exit_reason"]},
            )
        if delta_balance < 0 and pos["account_id"] in RISK_ACCOUNTS:
            try:  # a realized loss: record the day's low now (the daily block latches on it and warns once)
                await self._daily(pos["account_id"])
            except Exception:
                pass
        return closed

    async def _close(self, pos: dict, price: float, reason: str, label: str | None, time_s: int, *,
                     market: bool = True, emit: bool = True) -> dict:
        fill = Fill("close", reason, float(price), pos["qty_open"], market, label or EXIT_LABEL.get(reason, reason))
        await self._apply_fills(pos, [fill], time_s, emit=emit)
        return self.position_dict(pos)

    async def close_position(
        self,
        position_id: int | None = None,
        symbol: str | None = None,
        all: bool = False,
        *,
        account_id: int = 1,
        reason: str = "MANUAL",
        label: str | None = None,
        price: float | None = None,
        time_s: int | None = None,
    ) -> list[dict]:
        """Close at market: one position by id, every position of a symbol, or all (of the account)."""
        if position_id is None and not symbol and not all:
            raise PaperError("Indica qué cerrar: un id de posición, un símbolo o 'all'")
        if position_id is not None:
            pos = self._open.get(int(position_id))
            if pos is None or pos["account_id"] != account_id:
                raise PaperError(f"No hay una posición abierta con id {position_id}")
            targets = [pos]
        elif all:
            targets = [p for p in self._open.values() if p["account_id"] == account_id]
        else:
            sym = str(symbol).upper()  # an option also closes by its underlying's symbol
            targets = [p for p in self._open.values()
                       if p["account_id"] == account_id and sym in (p["symbol"], traded_symbol(p))]
            if not targets:
                raise PaperError(f"No hay posiciones abiertas en {sym}")
        closed: list[dict] = []
        for pos in sorted(targets, key=lambda p: p["id"]):
            px = safe_float(price)
            if px is None:
                px = self._position_price(pos) if pos["origin"] == "sim" else await self.last_price(pos["symbol"])
            if px is None:
                px = safe_float(pos["meta"].get("last_price"))
            if px is None:
                raise PaperError(f"No hay precio para cerrar {pos['symbol']}")
            async with self._lock:
                if pos["id"] not in self._open:
                    continue
                closed.append(await self._close(pos, px, reason, label, int(time_s or now_s())))
        if closed:
            await self.publish(account_id)
            if config.BACKGROUND:
                await self._sync_subscriptions()
        return closed

    async def reset(self, initial_balance: float | None = None, account_id: int = 1) -> dict:
        amount = safe_float(initial_balance)
        if amount is None:
            amount = self._initial_size(account_id)
        if amount <= 0:
            raise PaperError("El saldo inicial debe ser mayor que 0")
        await self.ensure_accounts()
        async with self._lock:
            for pid in [pid for pid, p in self._open.items() if p["account_id"] == account_id]:
                self._open.pop(pid, None)
                self._warned.pop(pid, None)
            for oid in [oid for oid, o in self._orders.items() if o["account_id"] == account_id]:
                self._orders.pop(oid, None)
                self._watch_from.pop(oid, None)
            self._day_low.pop(account_id, None)
            self._day_block_warned.pop(account_id, None)
            async with self.rt.db.transaction() as tx:
                await tx.execute("DELETE FROM paper_fills WHERE account_id=?", (account_id,))
                await tx.execute("DELETE FROM paper_positions WHERE account_id=?", (account_id,))
                await tx.execute("DELETE FROM paper_orders WHERE account_id=?", (account_id,))
                await tx.execute("DELETE FROM meta WHERE key=?", (f"paper.day_low.{account_id}",))
                await tx.execute(
                    "UPDATE paper_accounts SET balance=?, initial_balance=?, updated_at=? WHERE id=?",
                    (amount, amount, now_ms(), account_id),
                )
        if account_id == 1:
            self._emit("info", "paper", f"Cuenta demo reiniciada con saldo {fmt_num(amount, 2)}")
        elif account_id == AUTOPILOT_ACCOUNT:
            self._emit("info", "paper", f"Cuenta NEXUS automática reiniciada con saldo {fmt_num(amount, 2)}")
            self._autopilot_log.clear()
            self._autopilot_last.clear()
            self._autopilot_save_soon()
        await self.publish(account_id)
        if config.BACKGROUND:
            await self._sync_subscriptions()
        return await self.snapshot(account_id)

    # ------------------------------------------------------------------ price processing

    def _level_state(self, pos: dict) -> LevelState:
        meta = pos["meta"]
        return LevelState(
            side=side_sign(pos["side"]),
            entry=pos["entry_price"],
            qty=pos["qty"],
            qty_open=pos["qty_open"],
            sl=pos["sl"],
            tps=[pos.get("tp1"), pos.get("tp2"), pos.get("tp3")],
            tp_hits=int(pos.get("tp_hits") or 0),
            trail=pos.get("trail"),
            atr=float(meta.get("atr") or 0.0),
            trail_mult=float(self._setting("nexus.trail_atr", 3.0) or 3.0),
            high_water=float(meta.get("high_water") or pos["entry_price"]),
            low_water=float(meta.get("low_water") or pos["entry_price"]),
            partial=bool(meta.get("partial", True)),
        )

    async def _process_price(self, pos: dict, high: float, low: float, close: float, *, open_: float | None,
                             use_hl: bool, time_s: int, bar_time: int | None = None, bar_s: int = 60) -> bool:
        """Run level management for one position (caller holds self._lock). Returns True if it closed.

        `bar_time` / `bar_s` = open time and length of the bar this update belongs to (bar_time None for a polled
        price). A stop moved by a fill (break-even, trailing) takes effect from the next bar: the hub re-sends the
        forming 1m bar with its running high/low, so later updates of the bar in which the stop moved (for a move
        made by a polled price, the bar containing that moment) check the stop against the close only, since their
        extremes may predate the move. On a real market (bar times follow the clock and never rewind) a bar that
        ended before that moment, delivered late when the feed comes back after a stall, does not check the stop at
        all: even its close predates the move. A simulated series may restart earlier (new session), so there only
        the bar of the move is restricted.
        """
        meta = pos["meta"]
        meta["last_price"] = close
        st = self._level_state(pos)
        before = (st.sl, st.trail, st.tp_hits)
        stop_before = st.effective_stop()
        moved_at = safe_float(meta.get("stop_moved_bar"))
        real = pos["origin"] != "sim" and not pos["symbol"].startswith("SIM:")
        stop_hl = check_stop = True
        if bar_time is not None and moved_at is not None:
            if real:
                stop_hl = bar_time > moved_at
                check_stop = bar_time + bar_s > moved_at
            else:
                stop_hl = not (moved_at - bar_s < bar_time <= moved_at)
        fills = process_bar(st, high, low, close, open_=open_ if stop_hl else None, use_hl=use_hl, stop_hl=stop_hl,
                            check_stop=check_stop)
        # water marks only matter for the trailing stop: keep them in memory, persisted with the next write
        meta["high_water"] = st.high_water
        meta["low_water"] = st.low_water
        if st.qty_open > 0 and st.effective_stop() != stop_before:
            moved = int(bar_time if bar_time is not None else time_s)
            if real and moved_at is not None:
                moved = max(moved, int(moved_at))  # a late bar never moves the moment back
            meta["stop_moved_bar"] = moved
        if not fills and before == (st.sl, st.trail, st.tp_hits):
            return False
        return await self._apply_fills(pos, fills, time_s, st)

    async def _on_candle(self, symbol: str, tf: str, candle: dict, closed: bool) -> None:
        """Hub listener: live updates of every subscribed (symbol, tf)."""
        try:
            sym = str(symbol).upper()
            close = safe_float((candle or {}).get("close"))
            if close is None or close <= 0:
                return
            candle_time = int(safe_float(candle.get("time")) or 0)
            bar_s = _bar_seconds(tf)
            is_sim_symbol = sym.startswith("SIM:")  # synthetic bar times: not comparable with the clock
            # a real bar that ended minutes ago (closed market: the last session's bar sent right after subscribing)
            # is not a live price: it neither refreshes the cached price nor moves the levels
            live = is_sim_symbol or not candle_time or candle_time + bar_s >= now_s() - QUOTE_STALE_S
            # A final bar delivered late (after a feed stall the hub replays the missed bars, oldest first, as
            # closed=True with the exchange's final OHLC) is not a live price, but its high/low still decide the
            # stops and targets crossed meanwhile, filled at the end of that bar. Only bars that end after a
            # position was opened count for it (never the last session's bar of a closed market). It never fills a
            # pending entry order (see _orders_missed_live).
            catch_up = closed and not live and candle_time > 0
            if live:
                self._prices[sym] = (close, time.monotonic())
                self._stale_warned.discard(sym)
            if (live or catch_up) and (is_sim_symbol or tf == "1m"):
                # the positions on this market: an option follows the bars of its underlying
                positions = [p for p in self._open.values() if traded_symbol(p) == sym and p["origin"] != "sim"
                             and (live or candle_time + bar_s > int(p["entry_time"]))]
                if positions:
                    changed_accounts: set[int] = set()
                    bar_time = candle_time or now_s()
                    fill_time = now_s() if live else candle_time + bar_s
                    high = safe_float(candle.get("high")) or close
                    low = safe_float(candle.get("low")) or close
                    async with self._lock:
                        for pos in positions:
                            if pos["id"] not in self._open:
                                continue
                            use_hl = bar_time >= int(pos["entry_time"])
                            n_before = (pos["qty_open"], pos["tp_hits"])
                            bar = (safe_float(candle.get("open")), high, low, close)
                            inst = option_inst(pos)
                            if inst is not None:  # the stock's bar as a bar of the option's theoretical premium
                                pos["meta"]["spot"] = close
                                bar = option_bar(inst, *bar, fill_time)
                            await self._process_price(pos, bar[1], bar[2], bar[3], open_=bar[0], use_hl=use_hl,
                                                      time_s=fill_time, bar_time=bar_time, bar_s=bar_s)
                            if (pos["qty_open"], pos["tp_hits"]) != n_before:
                                changed_accounts.add(pos["account_id"])
                    for aid in changed_accounts:
                        await self.publish(aid)
                if self._orders:
                    if catch_up:  # a replayed final bar is not a live price: it never fills a pending entry
                        self._orders_missed_live(sym, safe_float(candle.get("high")) or close,
                                                 safe_float(candle.get("low")) or close, close,
                                                 open_=safe_float(candle.get("open")), bar_time=candle_time,
                                                 bar_s=bar_s)
                    else:
                        self._check_orders(sym, safe_float(candle.get("high")) or close,
                                           safe_float(candle.get("low")) or close, close,
                                           open_=safe_float(candle.get("open")), bar_time=candle_time or now_s(),
                                           bar_s=bar_s, live=live, fill_time=now_s())
            if closed and candle_time:
                for prof in self._auto_profiles():
                    if sym not in prof.symbols or tf != prof.tf:
                        continue
                    key = (prof.account_id, sym, tf)
                    if self._auto_bar.get(key) != candle_time:  # each closed bar once (the hub may resend it)
                        self._auto_bar[key] = candle_time
                        self._spawn(self._auto_check(sym, tf, candle_time, prof))
        except Exception as exc:
            if self._log.should_log("listener", 120):
                self._emit("warn", "paper", f"Error gestionando posiciones con el feed: {type(exc).__name__}: {exc}")

    async def sim_bar(self, symbol: str, tf: str, bar: dict, *, account_id: int = 2) -> list[dict]:
        """Simulator hook: manage the simulator's positions with a revealed bar. Returns closed positions."""
        sym = symbol.upper()
        bar_time = int(bar["time"])
        closed: list[dict] = []
        async with self._lock:
            for pos in [p for p in self._open.values()
                        if p["account_id"] == account_id and p["symbol"] == sym and p["origin"] == "sim"]:
                use_hl = bar_time >= int(pos["entry_time"])
                if await self._process_price(pos, float(bar["high"]), float(bar["low"]), float(bar["close"]),
                                             open_=float(bar["open"]), use_hl=use_hl,
                                             time_s=bar_time + tf_seconds(tf), bar_time=bar_time,
                                             bar_s=tf_seconds(tf)):
                    closed.append(self.position_dict(pos))
        return closed

    def sim_positions(self, symbol: str | None = None, account_id: int = 2) -> list[dict]:
        return [p for p in self.open_positions(account_id, symbol) if p["origin"] == "sim"]

    def advise(self, position_id: int, current: dict | None, price: float | None = None) -> dict | None:
        """Attach NEXUS advice/warnings to an open position; returns the assessment (see assess_position)."""
        pos = self._open.get(position_id)
        if pos is None or not isinstance(current, dict):
            return None
        meta = pos["meta"]
        inst = option_inst(pos)
        if inst is not None:  # NEXUS's exit rules on the underlying (its levels are on the stock, not the premium)
            info = assess_option(inst, current)
        else:
            st = self._level_state(pos)
            px = price if price is not None else (self._position_price(pos) or safe_float(current.get("price")))
            info = assess_position(
                pos["side"], px, st.effective_stop(), st.next_target(), float(meta.get("risk_unit") or 0.0),
                current, float(self._setting("nexus.exit_threshold", 0.0) or 0.0),
                sigma=SIGMA_ATR * float(meta.get("atr") or 0.0), horizon=int(self._setting("nexus.horizon", 5) or 5),
            )
        meta["advice"] = info["advice"]
        meta["warnings"] = info["warnings"]
        return info

    # ------------------------------------------------------------------ NEXUS advice / early exit

    async def refresh_advice(self, *, force: bool = False) -> int:
        """Attach live NEXUS advice to open positions (≤ 1/min per symbol). Returns how many NEXUS recommends closing.

        Live advice never closes anything (it follows the forming bar, so a "cerrar" there is provisional): manual
        positions, and auto ones the auto-trader no longer follows, get a warning event; the auto-trader closes its
        own positions on NEXUS's decision for the bar that just closed (`_auto_check`).
        """
        nexus = self.rt.nexus
        if nexus is None or not hasattr(nexus, "current"):
            return 0
        by_key: dict[tuple[str, str], list[dict]] = {}
        for pos in self._open.values():
            if pos["origin"] == "sim":
                continue
            # an option: the analysis of its underlying
            by_key.setdefault((traded_symbol(pos), pos.get("tf") or "1h"), []).append(pos)
        exits = 0
        mono = time.monotonic()
        for (symbol, tf), positions in by_key.items():
            if not force and mono - self._advice_at.get(f"{symbol}|{tf}", -1e18) < ADVICE_EVERY_S:
                continue
            self._advice_at[f"{symbol}|{tf}"] = mono
            try:
                current = await asyncio.wait_for(nexus.current(symbol, tf), timeout=90)
            except Exception as exc:
                if self._log.should_log(f"advice:{symbol}", 600):
                    self._emit("debug", "paper", f"Sin consejo NEXUS para {symbol}: {type(exc).__name__}")
                continue
            for pos in positions:
                if pos["id"] not in self._open:
                    continue
                if not isinstance(current, dict):  # no current analysis (e.g. outdated data): drop the old advice
                    pos["meta"]["advice"] = "Sin consejo NEXUS: no hay un análisis actual de este mercado"
                    pos["meta"]["warnings"] = []
                    continue
                price = self._position_price(pos) or safe_float(current.get("price"))
                info = self.advise(pos["id"], current, price)
                if info is None:
                    continue
                if not info["early_exit"]:
                    self._warned.pop(pos["id"], None)
                    continue
                exits += 1
                if self._auto_follows(pos):
                    who = "el auto-trading" if pos["account_id"] == 1 else ACCOUNTS.get(pos["account_id"], "NEXUS")
                    pos["meta"]["advice"] = f"{info['advice']}; {who} decide al cierre de la vela"
                    continue
                last = self._warned.get(pos["id"])
                if last is None or mono - last >= WARN_REPEAT_S:
                    self._warned[pos["id"]] = mono
                    why = (current.get("action_label") or current.get("action") if info.get("exit_by") == "signal"
                           else f"valor esperado de mantener {fmt_r(info['ev_r'], 2)} con el score en contra")
                    inst = option_inst(pos)
                    what = (f"{inst.get('name')}: NEXUS recomienda CERRAR el {inst.get('right')}" if inst is not None
                            else f"{pos['symbol']}: NEXUS recomienda CERRAR el {SIDE_LABEL[pos['side']]}")
                    self._emit(
                        "warn",
                        "paper",
                        f"⚠ {what} ({why})",
                        {"position_id": pos["id"], "account_id": pos["account_id"], "action": current.get("action"),
                         "ev_r": info["ev_r"]},
                    )
        return exits

    # ------------------------------------------------------------------ automatic traders (accounts 1 and 3)

    async def _auto_check(self, symbol: str, tf: str, upto: int | None = None,
                          profile: AutoProfile | None = None) -> None:
        """Automatic trading on a closed bar of the trader's timeframe: the user's auto-trading (default) or NEXUS's
        own account (`profile`). NEXUS decides on the bar that just closed (`upto` = its open time), never on the
        seconds-old forming bar, where a decision taken at the close already looks filled: the trader closes its
        position there on NEXUS's exit rules (see assess_position), then a BUY / SELL with gate TOMAR opens a new one
        (one position per symbol and account, within the profile's position cap and the portfolio risk rules). When
        several bars close at once (the feed catching up after a stall) every bar's exit is followed, but only the
        newest one may open a trade: an older entry signal is stale."""
        prof = profile or self._user_auto_profile()
        if prof is None or self.rt.nexus is None:
            return
        acct = prof.account_id
        own = acct == AUTOPILOT_ACCOUNT  # NEXUS's own account keeps a decision log
        who = "Auto-trading" if acct == 1 else prof.label
        seq, reads = self._autopilot_seq, self._autopilot_reads
        try:
            async with self._auto_locks.setdefault((acct, symbol), asyncio.Lock()):
                mine = [p for p in self._open.values() if p["account_id"] == acct and traded_symbol(p) == symbol]
                followed = [p for p in mine if self._follows(prof, p)]
                if mine and not followed:
                    return  # the user's own position: no pyramiding and nothing for the auto-trader to manage
                if not followed and not self._newest_bar(symbol, tf, upto, acct):
                    return  # flat, and a newer bar has closed meanwhile: entries are decided on that one
                current = await asyncio.wait_for(self.rt.nexus.current(symbol, tf, upto=upto), timeout=90)
                if own and prof.entries:
                    self._autopilot_read(symbol, tf, upto, current)
                if not isinstance(current, dict):
                    if own and self._log.should_log(f"ap-nodata:{symbol}:{tf}", 6 * 3600):
                        self._autopilot_note(symbol, "info", f"Sin análisis de NEXUS en la vela cerrada ({tf}): faltan "
                                                             "datos de este mercado; lo reintenta en la próxima")
                    return
                for pos in followed:
                    await self._auto_exit(pos, current)
                if not prof.entries:
                    return  # an exits-only trader (a position from before a change of timeframe)
                if not self._newest_bar(symbol, tf, upto, acct):
                    return  # a newer bar has closed meanwhile: entries are decided on that one
                if any(p["account_id"] == acct and traded_symbol(p) == symbol for p in self._open.values()):
                    return  # one position per market (an option counts on its underlying)
                action = current.get("action")
                if action == "BUY":
                    side = "long"
                elif action == "SELL" and self._setting("nexus.allow_short", True):
                    side = "short"
                else:
                    return
                cfg = self._autopilot_cfg() if own else None
                if cfg is not None and not in_dollars(symbol):  # its account is in USD
                    if self._log.should_log(f"ap-usd:{symbol}", 6 * 3600):
                        ticker = symbol.partition(":")[2] or symbol
                        self._autopilot_note(symbol, "skip", f"{SIDE_LABEL[side]} descartado: {ticker} cotiza en "
                                                             f"{quote_currency(symbol)}, no en dólares, y la cuenta de "
                                                             "NEXUS es en USD")
                    return
                if cfg is not None:
                    brake = self._brake(await self.account(acct), cfg["max_drawdown_pct"])
                    if brake["active"]:
                        text = (f"{SIDE_LABEL[side]} descartado: freno de pérdidas activo (la cuenta está un "
                                f"{fmt_pct(brake['drawdown_pct'], 1, False)} por debajo de su saldo inicial; límite "
                                f"{fmt_pct(brake['limit_pct'], 0, False)}). Reiníciala o sube el límite para que vuelva a "
                                "abrir operaciones.")
                        if self._log.should_log("autopilot-brake", 6 * 3600):
                            self._emit("warn", "paper", f"{prof.label}: freno de pérdidas activo: no abre operaciones "
                                                        "nuevas", {"account_id": acct})
                        if self._log.should_log(f"ap-brake:{symbol}", 6 * 3600):
                            self._autopilot_note(symbol, "skip", text)
                        return
                    if not await self._market_live(symbol):
                        if self._log.should_log(f"ap-closed:{symbol}", 6 * 3600):
                            self._autopilot_note(symbol, "skip", f"{SIDE_LABEL[side]} descartado: el mercado está cerrado "
                                                                 "o no hay precio en vivo; decide de nuevo en la próxima "
                                                                 "vela con el mercado abierto")
                        return
                try:
                    if prof.max_positions > 0:
                        self._check_max_positions(acct, prof.max_positions)
                    await self._check_daily(acct)
                    self._check_group(acct, symbol)
                except PaperRiskBlocked as blk:
                    if self._log.should_log(f"auto-risk:{acct}:{symbol}:{blk.rule}", 3600):
                        self._emit("info", "paper", f"{prof.label}: {symbol} {SIDE_LABEL[side]} descartado: {blk.message}")
                    if own:
                        self._autopilot_note(symbol, "skip", f"{SIDE_LABEL[side]} descartado: {blk.message}")
                    return
                gate = ((current.get("gate") or {}).get(side)) or None
                if not isinstance(gate, dict) or gate.get("verdict") != "TAKE":
                    text = (f"{SIDE_LABEL[side]} descartado por el filtro ({gate_line(gate)})"
                            + (f": {gate.get('summary')}" if isinstance(gate, dict) and gate.get("summary") else ""))
                    self._emit("info", "paper", f"{prof.label}: {symbol} {text}")
                    if own:
                        self._autopilot_note(symbol, "skip", text)
                    return
                # a stock or fund may be traded with an option (autopilot.stock_instrument): a call or a put
                plan: dict[str, Any] | None = None
                what = SIDE_LABEL[side]
                # what NEXUS saw on this bar, for the journal of its trades
                context = {"regime": str(current.get("regime") or "") or None,
                           "score": safe_float(current.get("score")), "prob_up": safe_float(current.get("prob_up")),
                           "gate_score": safe_float(gate.get("score")),
                           "win_prob": safe_float(gate.get("win_prob"))} if own else None
                if cfg is not None and self._uses_options(cfg, symbol, side):
                    what = "Call" if side == "long" else "Put"
                if cfg is not None and cfg["learn_guard"]:  # what it learned from its losses
                    keys = J.context_keys(regime=current.get("regime"), ts=now_s(), gate_score=gate.get("score"),
                                          bet=side, symbol=symbol)
                    hit = next((p for p in await self._learned_pauses() if keys.get(p["dimension"]) == p["key"]), None)
                    if hit is not None:
                        if self._log.should_log(f"ap-guard:{symbol}:{hit['dimension']}:{hit['key']}", 6 * 3600):
                            self._autopilot_note(symbol, "skip", f"{what} descartado: {self._guard_text(hit)}")
                        return
                try:
                    if cfg is not None and what in ("Call", "Put"):
                        plan = await self._option_plan(symbol, side, cfg)
                        res = await self.open_position(
                            acct, plan["symbol"], "long", tf, qty=plan["qty"], sl=plan["sl"], tps=plan["tps"],
                            price=plan["premium"], origin=prof.origin, gate=gate, check_gate=False,
                            risk_pct=prof.risk_pct, max_positions=prof.max_positions or None,
                            max_exposure_pct=cfg["max_exposure_pct"] or None, instrument=plan["instrument"],
                            context=context)
                    else:
                        res = await self.open_position(
                            acct, symbol, side, tf, origin=prof.origin, gate=gate, check_gate=False,
                            risk_pct=prof.risk_pct, max_positions=prof.max_positions or None,
                            max_exposure_pct=(cfg["max_exposure_pct"] or None) if cfg is not None else None,
                            contracts=cfg is not None, margin_pct=cfg["futures_margin_pct"] if cfg is not None else None,
                            context=context)
                except PaperRiskBlocked as blk:  # no option fits / lost the race for the last free slot / risk room
                    if self._log.should_log(f"auto-risk:{acct}:{symbol}:{blk.rule}", 3600):
                        self._emit("info", "paper", f"{prof.label}: {symbol} {what} descartado: {blk.message}")
                    if own:
                        self._autopilot_note(symbol, "skip", f"{what} descartado: {blk.message}")
                    return
                if own:
                    pos = (res or {}).get("position") or {}
                    inst = pos.get("instrument") or {}
                    if plan is not None:
                        n = int(inst.get("contracts") or 0)
                        self._autopilot_note(
                            symbol, "open",
                            f"Compra {n} {'contrato' if n == 1 else 'contratos'} {what.lower()} {inst.get('name')} a "
                            f"{fmt_price(pos.get('entry_price'))} por acción (prima teórica; delta "
                            f"{fmt_num(inst.get('delta'), 2)}, volatilidad histórica "
                            f"{fmt_pct((safe_float(inst.get('sigma')) or 0.0) * 100.0, 0, False)}) · stop de la prima "
                            f"{fmt_price(pos.get('sl'))} · filtro {gate_line(gate)}")
                    else:
                        self._autopilot_note(symbol, "open",
                                             f"Abre {SIDE_LABEL[side].lower()} a {fmt_price(pos.get('entry_price'))} · "
                                             f"SL {fmt_price(pos.get('sl'))} · filtro {gate_line(gate)}")
        except PaperRiskBlocked as blk:
            if self._log.should_log(f"auto-risk:{acct}:{symbol}:{blk.rule}", 3600):
                self._emit("info", "paper", f"{prof.label}: {symbol} descartado: {blk.message}")
            if own:
                self._autopilot_note(symbol, "skip", f"Descartado: {blk.message}")
        except (PaperError, PaperRejected) as exc:
            self._emit("warn", "paper", f"{who} {symbol}: {exc}")
            if own:
                self._autopilot_note(symbol, "info", f"No se pudo operar: {exc}")
        except Exception as exc:
            if own and prof.entries:
                self._autopilot_read(symbol, tf, upto, None, failed=True)
            if self._log.should_log(f"auto:{acct}:{symbol}", 300):
                self._emit("error", "paper", f"{who} {symbol}: {type(exc).__name__}: {exc}")
                if own:
                    why = "no respondió en 90 s" if isinstance(exc, asyncio.TimeoutError) else (
                        f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
                    self._autopilot_note(symbol, "info", f"No se pudo analizar ({why}); lo reintenta en la próxima vela")
        finally:
            if own and (self._autopilot_seq != seq or self._autopilot_reads != reads):
                await self.publish(AUTOPILOT_ACCOUNT)  # a new read or decision: the view shows it at once

    def _autopilot_read(self, symbol: str, tf: str, upto: int | None, current: Any, *, failed: bool = False) -> None:
        """What NEXUS said on the last bar it decided for a symbol of its own account (AutopilotLast), shown next to
        the symbol: most bars give no entry (ESPERAR / MANTENER), which the decision log does not list."""
        if isinstance(current, dict):
            action = str(current.get("action") or "") or None
            label = str(current.get("action_label") or action or "—")
        else:
            action, label = None, "Error al analizar" if failed else "Sin análisis (faltan datos)"
        self._autopilot_reads += 1
        self._autopilot_last[symbol] = {"bar_time": int(upto) * 1000 if upto else now_ms(), "tf": tf,
                                        "action": action, "label": label}
        self._autopilot_save_soon()

    def _newest_bar(self, symbol: str, tf: str, upto: int | None, account_id: int = 1) -> bool:
        """False when a newer closed bar of (symbol, tf) has been handed to that account's trader since bar `upto`."""
        return upto is None or self._auto_bar.get((account_id, symbol, tf), upto) == upto

    async def _auto_exit(self, pos: dict, current: dict) -> bool:
        """Close an automatic trader's position when NEXUS's closed-bar decision leaves its side or its early-exit rule
        holds for the position (assess_position `early_exit`). True when it closed."""
        price = self.cached_price(pos["symbol"], PRICE_STALE_S) or await self.last_price(pos["symbol"])
        info = self.advise(pos["id"], current, price)
        if not info or not info["early_exit"]:
            return False
        if price is None:
            if self._log.should_log(f"auto-exit:{pos['symbol']}", 300):
                who = "Auto-trading" if pos["account_id"] == 1 else ACCOUNTS.get(pos["account_id"], "Auto-trading")
                self._emit("warn", "paper", f"{who} {pos['symbol']}: NEXUS recomienda cerrar, pero no hay "
                                            "precio para cerrar la posición")
            return False
        reverse = str(current.get("action") or "") in ("BUY", "SELL")
        label = EXIT_LABEL["REVERSE"] if reverse else "Cierre anticipado"
        async with self._lock:
            if pos["id"] not in self._open:
                return False
            await self._close(pos, price, "REVERSE" if reverse else "EARLY", label, now_s())
        if pos["account_id"] == AUTOPILOT_ACCOUNT:
            inst = option_inst(pos)
            what = (f"el {inst.get('right')} {inst.get('name')}" if inst is not None
                    else SIDE_LABEL[pos["side"]].lower())
            self._autopilot_note(traded_symbol(pos), "close",
                                 f"Cierra {what} a {fmt_price(price)} ({label.lower()}, {fmt_r(pos.get('r_multiple'))})")
        await self.publish(pos["account_id"])
        if config.BACKGROUND:
            await self._sync_subscriptions()
        return True

    # ------------------------------------------------------------------ NEXUS's own account (autopilot)

    async def _autopilot_settle(self) -> None:
        """Re-read NEXUS's settings once a burst of changes made elsewhere (Ajustes, terminal) has settled."""
        try:
            while time.monotonic() - self._autopilot_changed < AUTOPILOT_SETTLE_S:
                await asyncio.sleep(AUTOPILOT_SETTLE_S)
            await self._refresh_autopilot(force=True)
            await self.publish(AUTOPILOT_ACCOUNT)
        finally:
            self._autopilot_settling = False

    def _autopilot_toggled(self, cfg: dict[str, Any]) -> None:
        if cfg["enabled"]:
            listed = ", ".join(cfg["symbols"]) or "una lista vacía (añade símbolos)"
            what = {"scanner": "los mejores candidatos del escáner",
                    "both": f"{listed} y los mejores candidatos del escáner"}.get(cfg["source"], listed)
            self._emit("info", "paper", f"NEXUS automática activada ({cfg['tf']}): opera sola con {what}; "
                                        "solo entra con señal de NEXUS y filtro TOMAR")
            self._autopilot_note("—", "info", "Activada")
        else:
            self._emit("info", "paper", "NEXUS automática pausada: sus posiciones abiertas siguen con su stop y "
                                        "sus objetivos")
            self._autopilot_note("—", "info", "Pausada: las posiciones abiertas siguen con su stop y sus objetivos")

    async def _refresh_autopilot(self, *, force: bool = False) -> None:
        """Candidates of NEXUS's own account: its list, or the best TOMAR opportunities (gate TOMAR + NEXUS signal) of
        the newest finished scan. A new candidate is decided at once on its last closed bar, then on every close."""
        cfg = self._autopilot_cfg()
        was, self._autopilot_on = self._autopilot_on, cfg["enabled"]
        if was is not None and was != cfg["enabled"]:
            self._autopilot_toggled(cfg)
        if not cfg["enabled"]:
            self._autopilot_tf = None
            if self._autopilot_watch or self._autopilot_scan is not None:
                self._autopilot_watch, self._autopilot_scan = {}, None
                if config.BACKGROUND:
                    await self._sync_subscriptions()  # paused: only the open positions stay subscribed (1m)
                await self.publish(AUTOPILOT_ACCOUNT)
            return
        if not force and time.monotonic() - self._autopilot_checked < AUTOPILOT_REFRESH_S:
            return
        self._autopilot_checked = time.monotonic()
        watch: dict[str, dict[str, Any]] = {}
        if cfg["source"] in ("list", "both"):
            for sym in cfg["symbols"]:
                watch[sym] = {"why": "lista", "direction": None}
        if cfg["source"] == "list":
            self._autopilot_scan = None
        else:
            run, rows = None, []
            scanner = self.rt.scanner
            if scanner is not None and hasattr(scanner, "results"):
                try:
                    run, rows = await scanner.results()
                except Exception as exc:
                    if self._log.should_log("autopilot-scan", 600):
                        self._emit("warn", "paper", f"NEXUS automática: no se pudo leer el último escaneo: {exc}")
            state = run.get("status") if run is not None else None
            done = rows if state == "done" else []
            # its candidates, in dollars only (its account is in USD): the TOMAR opportunities first, then the markets
            # the gate approves while NEXUS has no entry signal there yet (the likeliest next entries); either way it
            # only enters on NEXUS's signal at a bar close with the gate TOMAR
            opps = [r for r in done if r.get("opportunity") and in_dollars(str(r.get("symbol") or ""))]
            favorable = [r for r in done if not r.get("opportunity") and r.get("deep") and r.get("verdict") == "TAKE"
                         and in_dollars(str(r.get("symbol") or ""))]
            # the newest finished scan, or one still running when none has finished (a failed one is not a source)
            self._autopilot_scan = None if state not in ("done", "running") else {
                "id": int(run["id"]), "finished_at": run.get("finished_at") if state == "done" else None,
                "opportunities": len(opps)}
            for row in (opps + favorable)[: cfg["scanner_top"]]:
                direction = row.get("direction")
                watch.setdefault(str(row["symbol"]).upper(), {  # a listed symbol stays "lista"
                    "why": "escáner", "direction": direction if direction in ("long", "short") else None})
        # a new candidate (or every one, after a change of timeframe) is decided at once on its last closed bar
        fresh_tf = self._autopilot_tf != cfg["tf"]
        self._autopilot_tf = cfg["tf"]
        new = [sym for sym in watch if fresh_tf or sym not in self._autopilot_watch]
        changed = watch != self._autopilot_watch
        self._autopilot_watch = watch
        held = {traded_symbol(p) for p in self._open.values() if p["account_id"] == AUTOPILOT_ACCOUNT}
        gone = [s for s in self._autopilot_last if s not in watch and s not in held]
        for sym in gone:
            self._autopilot_last.pop(sym, None)
        if gone:
            self._autopilot_save_soon()
        if config.BACKGROUND:
            await self._sync_subscriptions()
        prof = self._autopilot_profile()
        if prof is not None:
            last_closed = floor_time(now_s(), prof.tf) - tf_seconds(prof.tf)
            for sym in new:
                key = (AUTOPILOT_ACCOUNT, sym, prof.tf)
                if sym.startswith("SIM:") or self._auto_bar.get(key, -1) >= last_closed:
                    continue  # synthetic markets have no wall-clock bars; a bar already decided is not decided twice
                self._auto_bar[key] = last_closed
                self._spawn(self._auto_check(sym, prof.tf, last_closed, prof))
        if changed:
            await self.publish(AUTOPILOT_ACCOUNT)

    def autopilot_status(self) -> dict[str, Any]:
        """AutopilotStatus: settings, what it watches (and why), the scan its candidates came from, its decisions."""
        cfg = self._autopilot_cfg()
        def last(sym: str) -> dict[str, Any] | None:
            read = self._autopilot_last.get(sym)
            return dict(read) if read is not None and read["tf"] == cfg["tf"] else None

        watching = [{"symbol": sym, **info, "last": last(sym)} for sym, info in sorted(self._autopilot_watch.items())]
        listed = set(self._autopilot_watch)
        for pos in sorted(self._open.values(), key=lambda p: p["id"]):
            sym = traded_symbol(pos)  # an option: the stock NEXUS follows for it
            if pos["account_id"] == AUTOPILOT_ACCOUNT and sym not in listed:
                inst = option_inst(pos)
                direction = ("long" if inst.get("right") == "call" else "short") if inst is not None else pos["side"]
                watching.append({"symbol": sym, "why": "posición abierta", "direction": direction, "last": last(sym)})
                listed.add(sym)
        return {
            "enabled": cfg["enabled"],
            "account_id": AUTOPILOT_ACCOUNT,
            "source": cfg["source"],
            "tf": cfg["tf"],
            "symbols": cfg["symbols"],
            "max_positions": cfg["max_positions"],
            "risk_pct": cfg["risk_pct"],
            "scanner_top": cfg["scanner_top"],
            "watching": watching,
            "scan": dict(self._autopilot_scan) if self._autopilot_scan else None,
            "log": list(self._autopilot_log),
            "alerts": bool(self._setting("alerts.autopilot", False)),
            "alert_channels": self._alert_channels(),
            "initial_balance": cfg["initial_balance"],
            "max_exposure_pct": cfg["max_exposure_pct"],
            "max_drawdown_pct": cfg["max_drawdown_pct"],
            "futures_margin_pct": cfg["futures_margin_pct"],
            "learn_guard": cfg["learn_guard"],
            "paused_contexts": [dict(p) for p in self._guard_cache[1]] if self._guard_cache and cfg["learn_guard"] else [],
            "health": self._autopilot_health(cfg),
            "weekly": dict(self._ap_weekly) if self._ap_weekly else None,
            "stock_instrument": cfg["stock_instrument"],
            "options_premium_pct": cfg["options_premium_pct"],
            "brake": None,  # filled by autopilot_snapshot (it needs the account's equity)
            "note": AUTOPILOT_NOTE,
        }

    # ------------------------------------------------------------------ its health and weekly report

    async def _meta_get(self, key: str) -> Any:
        try:
            return await self.rt.db.fetchval("SELECT value FROM meta WHERE key=?", (key,))
        except Exception:
            return None

    async def _meta_put(self, key: str, value: str) -> None:
        try:
            await self.rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        except Exception as exc:
            if self._log.should_log(f"meta:{key}", 600):
                self._emit("warn", "paper", f"NEXUS automática: no se pudo guardar {key}: {exc}")

    async def _load_autopilot_health(self) -> None:
        """Its last heartbeat and weekly report: a heartbeat older than two bars while it was on means the server was
        stopped (or the PC asleep) — noted once, with an alert."""
        weekly = json_loads(await self._meta_get(WEEKLY_KEY), None)
        self._ap_weekly = weekly if isinstance(weekly, dict) else None
        week = safe_float(await self._meta_get(WEEKLY_WEEK_KEY))
        self._ap_weekly_week = int(week) if week is not None else None
        cfg = self._autopilot_cfg()
        beat = safe_float(await self._meta_get(HEARTBEAT_KEY))
        if cfg["enabled"] and beat is not None:
            self._check_gap(int(beat), now_s(), cfg)
        self._ap_seen = now_s() if cfg["enabled"] else None

    def _check_gap(self, last_s: int, now: int, cfg: dict[str, Any]) -> bool:
        """A stop between two passes of its monitor (server stopped, PC asleep): noted in its log and alerted."""
        tf_s = _bar_seconds(cfg["tf"])
        gap = int(now) - int(last_s)
        if gap <= max(2 * tf_s, int(2 * HEARTBEAT_SAVE_S) + 60):
            return False
        self._ap_downtime = {"from": int(last_s) * 1000, "to": int(now) * 1000}
        bars = gap // tf_s
        text = (f"Estuvo detenida {_age_text(gap)} (de {self._when(int(last_s))} a {self._when(int(now))}): no decidió "
                f"en unas {bars} {'vela' if bars == 1 else 'velas'} de {cfg['tf']}. Para que opere sola, el servidor tiene "
                "que seguir en marcha y el PC sin suspenderse.")
        self._autopilot_note("—", "info", text)
        self._emit("warn", "paper", f"NEXUS automática: {text[0].lower()}{text[1:]}",
                   {"account_id": AUTOPILOT_ACCOUNT,
                    "notify": {"category": "autopilot", "kind": "downtime", "priority": "warn"}})
        return True

    def _autopilot_health(self, cfg: dict[str, Any], now: int | None = None) -> dict[str, Any]:
        """AutopilotHealth: its last decision and the 24/7 markets it watches with no decision for over three bars
        (their candles stopped arriving: feed down, no connection, server stopped). Markets with sessions (stocks…)
        are left out: no decision while they are closed is normal."""
        now = int(now if now is not None else now_s())
        tf_s = _bar_seconds(cfg["tf"])
        held = {traded_symbol(p) for p in self._open.values() if p["account_id"] == AUTOPILOT_ACCOUNT}
        watched = sorted(set(self._autopilot_watch) | held)
        reads = [r for r in self._autopilot_last.values() if r.get("tf") == cfg["tf"]]
        last = max((int(r["bar_time"]) + tf_s * 1000 for r in reads), default=None)  # the close of its newest bar
        stale = []
        if cfg["enabled"]:
            for sym in watched:
                read = self._autopilot_last.get(sym)
                if (sym.startswith("SIM:") or asset_class(sym) not in ALWAYS_OPEN_CLASSES or read is None
                        or read.get("tf") != cfg["tf"]):
                    continue
                if now > int(read["bar_time"]) // 1000 + 3 * tf_s + HEALTH_GRACE_S:
                    stale.append(sym)
        if not cfg["enabled"]:
            text = "Pausada: no decide."
        elif not watched:
            text = "No vigila ningún mercado ahora mismo: no hay nada que decidir."
        elif stale:
            tickers = ", ".join(s.partition(":")[2] or s for s in stale[:5])
            text = (f"Sin decisiones recientes en {tickers}: sus velas no llegan (feed de precios caído, sin conexión o "
                    "el servidor estuvo detenido).")
        else:
            text = f"Funciona: decide al cierre de cada vela de {cfg['tf']}."
        down = self._ap_downtime
        if down is not None and now * 1000 - int(down["to"]) > DOWNTIME_SHOW_S * 1000:
            down = None
        return {"ok": not stale, "last_decision": last, "stale": stale, "downtime": dict(down) if down else None,
                "text": text}

    async def _autopilot_watchdog(self, now: int | None = None) -> None:
        """While it is on, on every pass of the monitor: its heartbeat (a gap = the server or the PC stopped), the 24/7
        markets without recent decisions (noted and alerted once per incident) and its weekly report."""
        now = int(now if now is not None else now_s())
        cfg = self._autopilot_cfg()
        if not cfg["enabled"]:
            self._ap_seen, self._ap_stale_warned = None, False
            return
        if self._ap_seen is not None:
            self._check_gap(self._ap_seen, now, cfg)
        self._ap_seen = now
        if time.monotonic() - self._ap_seen_saved >= HEARTBEAT_SAVE_S:
            self._ap_seen_saved = time.monotonic()
            await self._meta_put(HEARTBEAT_KEY, str(now))
        health = self._autopilot_health(cfg, now)
        if health["stale"] and not self._ap_stale_warned:
            self._ap_stale_warned = True
            self._autopilot_note("—", "info", health["text"])
            self._emit("warn", "paper", f"NEXUS automática: {health['text'][0].lower()}{health['text'][1:]}",
                       {"account_id": AUTOPILOT_ACCOUNT,
                        "notify": {"category": "autopilot", "kind": "stale", "priority": "warn"}})
        elif not health["stale"] and self._ap_stale_warned:
            self._ap_stale_warned = False
            self._autopilot_note("—", "info", "Vuelve a decidir con normalidad")
        await self._weekly_if_due(now)

    async def _weekly_if_due(self, now: int) -> None:
        """On Mondays (00:00 UTC on), its report of the week that just ended: kept for its view and sent as an alert
        (category autopilot, opt-in). The first time it only takes note of the week, so a new account does not
        report an empty week it never ran."""
        week = now - (now - 4 * 86400) % WEEK_S  # Monday 00:00 UTC (1970-01-05 was a Monday)
        prev = week - WEEK_S
        if self._ap_weekly_week is not None and self._ap_weekly_week >= prev:
            return
        first = self._ap_weekly_week is None
        self._ap_weekly_week = prev
        await self._meta_put(WEEKLY_WEEK_KEY, str(prev))
        if first:
            return
        report = await self._weekly_report(prev, week)
        self._ap_weekly = report
        await self._meta_put(WEEKLY_KEY, json_dumps(report))
        self._autopilot_note("—", "info", report["text"])
        self._emit("info", "paper", f"NEXUS automática · informe semanal: {report['text']}",
                   {"account_id": AUTOPILOT_ACCOUNT,
                    "notify": {"category": "autopilot", "kind": "weekly", "priority": "info"}})
        await self.publish(AUTOPILOT_ACCOUNT)

    async def _weekly_report(self, start_s: int, end_s: int) -> dict[str, Any]:
        """AutopilotWeekly of [start_s, end_s): its closed trades as «x de n», their R and P&L and the most repeated
        cause of its losses."""
        rows = await self.rt.db.fetchall(
            "SELECT r_multiple, pnl, meta FROM paper_positions WHERE account_id=? AND status='closed' "
            "AND origin='autopilot' AND exit_time >= ? AND exit_time < ?", (AUTOPILOT_ACCOUNT, int(start_s), int(end_s)))
        r = [float(x["r_multiple"]) for x in rows if safe_float(x.get("r_multiple")) is not None]
        n, wins = len(r), sum(1 for v in r if v > 0)
        pnl = sum(float(x.get("pnl") or 0.0) for x in rows)
        causes = [((json_loads(x.get("meta"), {}) or {}).get("journal") or {}).get("cause") for x in rows
                  if safe_float(x.get("r_multiple")) is not None and float(x["r_multiple"]) <= 0]
        causes = [c for c in causes if c]
        d1 = time.strftime("%d/%m", time.gmtime(int(start_s)))
        d2 = time.strftime("%d/%m", time.gmtime(int(end_s) - 1))
        if n == 0:
            text = f"Semana del {d1} al {d2}: ninguna operación cerrada."
        else:
            text = (f"Semana del {d1} al {d2}: {n} {'operación cerrada' if n == 1 else 'operaciones cerradas'}, "
                    f"{wins} de {n} ganadoras, R total {fmt_r(sum(r), 2)} ({fmt_num(pnl, 2, True)} $).")
            if causes:
                cause, k = Counter(causes).most_common(1)[0]
                text += f" Lo más repetido en sus pérdidas: «{J.CAUSE_LABEL.get(cause, cause)}» ({k} de {len(causes)})."
        text += " Operaciones simuladas; no es asesoría financiera."
        return {"week_start": int(start_s) * 1000, "week_end": int(end_s) * 1000, "trades": n, "wins": wins,
                "total_r": round(sum(r), 4) if n else None, "mean_r": round(sum(r) / n, 4) if n else None,
                "pnl": round(pnl, 2), "text": text, "generated_at": now_ms()}

    def _alert_channels(self) -> int:
        """Alert channels (Telegram, email) on and complete: 0 means its alerts would reach nobody."""
        svc = getattr(self.rt, "alerts", None)
        try:
            return int(svc.active_channels()) if svc is not None and hasattr(svc, "active_channels") else 0
        except Exception:
            return 0

    async def autopilot_snapshot(self) -> dict[str, Any]:
        """AutopilotSnapshot: NEXUS's own account, its positions, history, portfolio risk and status."""
        account = await self.account(AUTOPILOT_ACCOUNT)
        try:
            risk = await self.risk_state(AUTOPILOT_ACCOUNT, account)
        except Exception:
            risk = None
        status = self.autopilot_status()
        status["brake"] = self._brake(account, status["max_drawdown_pct"])
        if status["learn_guard"]:
            try:
                status["paused_contexts"] = await self._learned_pauses()
            except Exception:
                pass
        return {
            "account": account,
            "positions": self.open_positions(AUTOPILOT_ACCOUNT),
            "history": await self.history(AUTOPILOT_ACCOUNT),
            "risk": risk,
            "autopilot": status,
        }

    @staticmethod
    def _brake(account: dict, limit_pct: float) -> dict[str, Any]:
        """AutopilotBrake: its loss brake (equity this % below the initial balance stops new entries)."""
        initial = float(account.get("initial_balance") or 0.0)
        equity = float(account.get("equity") or 0.0)
        drawdown = max(0.0, (1.0 - equity / initial) * 100.0) if initial > 0 else 0.0
        return {"limit_pct": float(limit_pct), "drawdown_pct": round(drawdown, 4),
                "active": bool(limit_pct > 0 and drawdown >= limit_pct)}

    async def _market_live(self, symbol: str) -> bool:
        """A non-24/7 market (stocks, funds, futures, forex…) is open and its quote is a live price."""
        if asset_class(symbol) in ALWAYS_OPEN_CLASSES or symbol.upper().startswith("SIM:"):
            return True
        hub = self.rt.hub
        if hub is None or not hasattr(hub, "quotes"):
            return False
        try:
            got = await asyncio.wait_for(hub.quotes([symbol]), timeout=20)
        except Exception:
            return False
        q = got.get(symbol) if isinstance(got, dict) else None
        return isinstance(q, dict) and q.get("market_open") is not False and quote_is_live(q, now_ms())

    def _margin_room(self, account_id: int, instrument: dict, pct: float, equity: float) -> int:
        """Whole contracts of a future that fit the account's margin budget (pct % of equity, approximate initial
        margins of app.engine.contracts); PaperRiskBlocked 'margin' when not even one fits."""
        cap = max(float(equity), 0.0) * float(pct) / 100.0
        used = 0.0
        for p in self._open.values():
            inst = (p.get("meta") or {}).get("instrument") or {}
            if p["account_id"] == account_id and inst.get("kind") == "future":
                used += float(p["qty_open"]) / float(inst["multiplier"]) * float(inst["margin"])
        room = cap - used
        margin = float(instrument["margin"])
        n = int(math.floor(room / margin + 1e-9)) if margin > 0 else 0
        if n < 1:
            if pct <= 0:
                raise PaperRiskBlocked("No opera futuros: su margen máximo en futuros es 0 %.", "margin")
            raise PaperRiskBlocked(
                f"El margen de 1 contrato {instrument['root']} (~{fmt_num(margin, 0)}, aproximado) supera lo disponible "
                f"para garantías: {fmt_num(max(room, 0.0), 0)} de {fmt_num(cap, 0)} ({fmt_pct(pct, 0, False)} del "
                "capital).", "margin")
        return n

    def _cap_exposure(self, account_id: int, symbol: str, qty: float, fill: float, pct: float,
                      equity: float) -> float:
        """Units within a cash account's exposure (all but futures, an option at its premium, ≤ pct % of equity):
        the same qty, a smaller one that fits, or PaperRiskBlocked 'exposure' when what is left is under a tenth of
        the order."""
        cap = max(float(equity), 0.0) * float(pct) / 100.0
        used = sum(float(p["qty_open"]) * float(self._position_price(p) or p["entry_price"])
                   for p in self._open.values()
                   if p["account_id"] == account_id and asset_class(p["symbol"]) not in MARGIN_CLASSES)
        room = cap - used
        want = float(qty) * float(fill)
        if want <= room:
            return qty
        if room <= 0.1 * want:
            raise PaperRiskBlocked(
                f"Exposición máxima: ya tiene {fmt_num(used, 2)} invertidos de {fmt_num(cap, 2)} "
                f"({fmt_pct(pct, 0, False)} del capital), así que no cabe otra posición.", "exposure")
        return room / float(fill)

    @staticmethod
    def _uses_options(cfg: dict[str, Any], symbol: str, side: str) -> bool:
        """NEXUS's own account trades this signal with an option: a stock or fund in mode «options» (a call to bet on
        a rise, a put on a fall), or in mode «auto» to bet on a fall (a cash account cannot sell shares short)."""
        if asset_class(symbol) not in OPTION_CLASSES:
            return False
        mode = cfg.get("stock_instrument") or "auto"
        return mode == "options" or (mode == "auto" and side == "short")

    async def _hist_vol(self, symbol: str) -> float | None:
        """Annualized volatility of the last 20 daily closes of `symbol` (an option's theoretical price), or None."""
        hub = self.rt.hub
        if hub is None:
            return None
        try:
            cs = await asyncio.wait_for(hub.get_candles(symbol, "1d", 60), timeout=20)
        except Exception:
            return None
        if cs is None or len(cs) == 0:
            return None
        return opt.historical_vol(list(cs.close))

    async def _option_plan(self, symbol: str, side: str, cfg: dict[str, Any]) -> dict[str, Any]:
        """The option NEXUS's own account buys for a signal on a stock (`side` = its bet on the stock): a call or a
        put of the first monthly expiry at least 30 days away, the at-the-money strike or the first one further out
        whose contract fits its premium budget (autopilot.options_premium_pct of its equity, |delta| ≥ 0.30), priced
        with Black-Scholes and the stock's 20-day historical volatility. Levels on the premium: stop −50 %, targets
        +50 / +100 / +150 %. PaperRiskBlocked 'option' with the reason when none fits."""
        right = "call" if side == "long" else "put"
        ticker = symbol.partition(":")[2] or symbol
        pct = float(cfg.get("options_premium_pct") or 0.0)
        if pct <= 0:
            why = (" y en una cuenta de efectivo no vende acciones en corto"
                   if cfg.get("stock_instrument") == "auto" else "")
            raise PaperRiskBlocked(f"no compra opciones (su prima máxima por operación es 0 %){why}.", "option")
        spot = await self.last_price(symbol)
        if spot is None or spot <= 0:
            raise PaperRiskBlocked(f"no hay precio de {ticker} para valorar la opción.", "option")
        sigma = await self._hist_vol(symbol)
        if sigma is None:
            raise PaperRiskBlocked(f"faltan {opt.VOL_WINDOW + 1} cierres diarios de {ticker} para estimar su "
                                   "volatilidad (el precio teórico de la opción).", "option")
        now = now_s()
        expiry = opt.monthly_expiry(datetime.fromtimestamp(now, timezone.utc).date())
        budget = await self.equity(AUTOPILOT_ACCOUNT) * pct / 100.0
        pick = opt.choose_option(spot, right, opt.years_to(expiry, now), sigma, budget) or {"none": True}
        if pick.get("none"):
            cheapest = pick.get("cheapest")
            raise PaperRiskBlocked(
                (f"ningún {right} de {ticker} cabe en su presupuesto de primas ({fmt_num(budget, 2)}, "
                 f"{fmt_pct(pct, 0, False)} del capital): el más barato con delta de al menos "
                 f"{fmt_num(opt.MIN_DELTA, 2)} cuesta {fmt_num(cheapest, 2)} por contrato (precio teórico)."
                 if cheapest is not None else
                 f"ningún {right} de {ticker} con delta de al menos {fmt_num(opt.MIN_DELTA, 2)} y una prima de al "
                 f"menos {fmt_num(opt.MIN_PREMIUM, 2)} por acción."), "option")
        premium, n, strike = float(pick["premium"]), int(pick["contracts"]), float(pick["strike"])
        instrument = {"kind": "option", "name": opt.option_name(symbol, expiry, right, strike),
                      "multiplier": float(opt.MULTIPLIER), "contracts": n, "underlying": symbol, "right": right,
                      "strike": strike, "expiry": expiry.isoformat(), "sigma": round(float(sigma), 6),
                      "delta": round(float(pick["delta"]), 4), "spot": float(spot)}
        return {"symbol": opt.option_symbol(symbol, expiry, right, strike), "premium": premium,
                "qty": float(n * opt.MULTIPLIER), "sl": premium * (1.0 - opt.STOP_FRACTION),
                "tps": [premium * m for m in opt.TARGETS], "instrument": instrument}

    async def _expire_options(self, now: int | None = None) -> int:
        """Option positions leave options.EXIT_DAYS days before their expiry (time decay speeds up near the end) at
        their theoretical premium with a live price of the underlying; one that reaches its expiry anyway (no live
        price meanwhile) settles at its intrinsic value with the last known price. Returns how many closed."""
        now = int(now if now is not None else now_s())
        closed = 0
        changed: set[int] = set()
        for pos in sorted(self._open.values(), key=lambda p: p["id"]):
            inst = option_inst(pos)
            if inst is None or pos["origin"] == "sim":
                continue
            try:
                expiry = date.fromisoformat(str(inst.get("expiry")))
            except ValueError:
                continue
            left = opt.days_to(expiry, now)
            if left > opt.EXIT_DAYS:
                continue
            expired = left <= 0
            spot = self.cached_price(str(inst["underlying"]), PRICE_STALE_S)
            if spot is None and not expired:
                continue  # it waits for a live price of the stock (an open market) to leave at a fair premium
            if spot is None:
                spot = safe_float(pos["meta"].get("spot")) or safe_float(inst.get("spot")) or 0.0
            price = option_premium(inst, spot, now)
            label = ("Vencimiento: liquidada a su valor intrínseco" if expired
                     else f"Cierre a {opt.EXIT_DAYS} días del vencimiento")
            async with self._lock:
                if pos["id"] not in self._open:
                    continue
                await self._close(pos, price, "EXPIRY", label, now, market=not expired)
            closed += 1
            changed.add(pos["account_id"])
            if pos["account_id"] == AUTOPILOT_ACCOUNT:
                self._autopilot_note(traded_symbol(pos), "close",
                                     f"Cierra el {inst.get('right')} {inst.get('name')} a "
                                     f"{fmt_price(pos.get('exit_price'))} por acción: {label[0].lower()}{label[1:]} "
                                     f"({fmt_r(pos.get('r_multiple'))})")
        for aid in changed:
            await self.publish(aid)
        if changed and config.BACKGROUND:
            await self._sync_subscriptions()
        return closed

    async def set_autopilot(self, *, enabled: Any = None, source: Any = None, symbols: Any = None, tf: Any = None,
                            max_positions: Any = None, risk_pct: Any = None, scanner_top: Any = None,
                            alerts: Any = None, max_exposure_pct: Any = None,
                            max_drawdown_pct: Any = None, futures_margin_pct: Any = None,
                            stock_instrument: Any = None, options_premium_pct: Any = None,
                            learn_guard: Any = None) -> dict[str, Any]:
        """Change the settings of NEXUS's own account (only the given fields); Spanish PaperError on a bad value.
        `alerts`: its trades as alerts through the user's channels (setting alerts.autopilot, opt-in)."""
        values: dict[str, Any] = {}
        if enabled is not None:
            values["autopilot.enabled"] = bool(enabled)
        if alerts is not None:
            values["alerts.autopilot"] = bool(alerts)
        if learn_guard is not None:
            values["autopilot.learn_guard"] = bool(learn_guard)
        if source is not None:
            src = str(source).strip().lower()
            if src not in AUTOPILOT_SOURCES:
                raise PaperError("Fuente no válida: usa «scanner» (escáner), «list» (lista fija) o «both» (las dos)")
            values["autopilot.source"] = src
        if symbols is not None:
            if not isinstance(symbols, (list, tuple)):
                raise PaperError("La lista de símbolos no es válida")
            syms = list(dict.fromkeys(str(x).strip().upper() for x in symbols if str(x).strip()))
            if len(syms) > 30:
                raise PaperError("Como máximo 30 símbolos en la lista")
            values["autopilot.symbols"] = syms
        if tf is not None:
            try:
                values["autopilot.tf"] = normalize_tf(tf)
            except ValueError as exc:
                raise PaperError(str(exc)) from None
        if stock_instrument is not None:
            mode = str(stock_instrument).strip().lower()
            if mode not in STOCK_INSTRUMENTS:
                raise PaperError("Instrumento no válido para acciones: usa «auto» (NEXUS elige), «shares» (acciones) u "
                                 "«options» (opciones)")
            values["autopilot.stock_instrument"] = mode
        for key, value, lo, hi, what, cast in (
            ("max_positions", max_positions, 1, 20, "El máximo de posiciones", int),
            ("risk_pct", risk_pct, 0.1, 5.0, "El riesgo por operación (%)", float),
            ("scanner_top", scanner_top, 1, 30, "El número de candidatos del escáner", int),
            ("max_exposure_pct", max_exposure_pct, 0, 500, "La exposición máxima (%)", float),
            ("max_drawdown_pct", max_drawdown_pct, 0, 90, "El freno de pérdidas (%)", float),
            ("futures_margin_pct", futures_margin_pct, 0, 100, "El margen máximo en futuros (%)", float),
            ("options_premium_pct", options_premium_pct, 0, 50, "La prima máxima por operación (%)", float),
        ):
            if value is None:
                continue
            f = safe_float(value)
            if f is None or not (lo <= f <= hi) or (cast is int and f != int(f)):
                kind = "un número entero" if cast is int else "un número"
                raise PaperError(f"{what} debe ser {kind} entre {fmt_num(lo, 1 if cast is float else 0)} y "
                                 f"{fmt_num(hi, 1 if cast is float else 0)}")
            values[f"autopilot.{key}"] = cast(f)
        if values and self.rt.settings is not None:
            self._autopilot_quiet = True  # every key saved first, then one re-read (never a half-saved state)
            try:
                await self.rt.settings.update(values)
            finally:
                self._autopilot_quiet = False
        await self._refresh_autopilot(force=True)
        await self.publish(AUTOPILOT_ACCOUNT)
        return await self.autopilot_snapshot()

    async def autopilot_comparison(self, *, fresh: bool = False) -> dict[str, Any]:
        """AutopilotComparison: NEXUS's own account against random entries in the same markets and moments
        (app.sim.autopilot_compare). Cached until a trade closes or COMPARE_TTL_S passes; one computation at a time."""
        from app.sim import autopilot_compare

        row = await self.rt.db.fetchone(
            "SELECT COUNT(*) AS n, MAX(id) AS last FROM paper_positions WHERE account_id=? AND status='closed'",
            (AUTOPILOT_ACCOUNT,)) or {}
        key = (int(row.get("n") or 0), int(row.get("last") or 0))

        def cached() -> dict[str, Any] | None:
            c = self._compare_cache
            if c is not None and c[0] == key and time.monotonic() - c[1] < COMPARE_TTL_S:
                return c[2]
            return None

        if not fresh and (hit := cached()) is not None:
            return hit
        async with self._compare_lock:
            if not fresh and (hit := cached()) is not None:
                return hit
            settings = self.rt.settings.all() if self.rt.settings is not None else {}
            result = await autopilot_compare.compare(self.rt, AUTOPILOT_ACCOUNT, settings=settings,
                                                     slippage_pct=self._slip_pct())
            self._compare_cache = (key, time.monotonic(), result)
            return result

    async def autopilot_journal(self, *, fresh: bool = False) -> dict[str, Any]:
        """AutopilotJournal: its newest JOURNAL_LIMIT closed trades grouped by cause, session, regime, news, filter
        score, side and market (app.sim.journal). Cached until a trade closes or COMPARE_TTL_S passes."""
        rows = await self.rt.db.fetchall(
            "SELECT id, symbol, side, entry_price, entry_time, exit_price, exit_time, exit_reason, r_multiple, "
            "gate_score, meta FROM paper_positions WHERE account_id=? AND status='closed' AND origin='autopilot' "
            "AND r_multiple IS NOT NULL ORDER BY id DESC LIMIT ?", (AUTOPILOT_ACCOUNT, JOURNAL_LIMIT))
        key = (len(rows), max((int(r["id"]) for r in rows), default=0))
        c = self._journal_cache
        if not fresh and c is not None and c[0] == key and time.monotonic() - c[1] < COMPARE_TTL_S:
            return c[2]
        trades = self._journal_trades(rows)
        result = await asyncio.to_thread(J.analyze, trades, seed=S_seed("journal", len(trades)))
        result["generated_at"] = now_ms()
        self._journal_cache = (key, time.monotonic(), result)
        return result

    def _journal_trades(self, rows: list[dict]) -> list[dict[str, Any]]:
        """Closed-trade rows (id, symbol, side, entry_price, entry_time, exit_price, exit_time, exit_reason, r_multiple,
        gate_score, meta) as the trades of app.sim.journal; trades closed before the journal get theirs from their
        water marks."""
        trades = []
        for row in rows:
            meta = json_loads(row.get("meta"), {}) or {}
            meta = meta if isinstance(meta, dict) else {}
            journal = meta.get("journal")
            if not isinstance(journal, dict):
                journal = J.close_entry(
                    side=row["side"], entry=row["entry_price"], risk_unit=meta.get("risk_unit"),
                    high_water=meta.get("high_water"), low_water=meta.get("low_water"), exit_price=row["exit_price"],
                    r=row["r_multiple"], reason=row["exit_reason"], entry_time=row["entry_time"],
                    exit_time=row["exit_time"])
            trades.append({"r": row["r_multiple"], "side": row["side"], "symbol": row["symbol"],
                           "entry_time": row["entry_time"], "exit_time": row["exit_time"],
                           "exit_reason": row["exit_reason"], "gate_score": row.get("gate_score"),
                           "context": meta.get("context") if isinstance(meta.get("context"), dict) else None,
                           "journal": journal, "instrument": meta.get("instrument")})
        return trades

    async def _learned_pauses(self) -> list[dict[str, Any]]:
        """Contexts where its trades of the last 90 days lose with strong evidence (app.sim.journal.losing_contexts):
        its trader opens nothing there. Recomputed when a trade closes and every hour (old trades leave the window,
        so a paused context is tried again once its losses are older than 90 days)."""
        since = now_s() - GUARD_WINDOW_S
        rows = await self.rt.db.fetchall(
            "SELECT id, symbol, side, entry_price, entry_time, exit_price, exit_time, exit_reason, r_multiple, "
            "gate_score, meta FROM paper_positions WHERE account_id=? AND status='closed' AND origin='autopilot' "
            "AND r_multiple IS NOT NULL AND exit_time >= ? ORDER BY id DESC LIMIT ?",
            (AUTOPILOT_ACCOUNT, since, JOURNAL_LIMIT))
        key = (len(rows), max((int(r["id"]) for r in rows), default=0), now_s() // 3600)
        if self._guard_cache is not None and self._guard_cache[0] == key:
            return self._guard_cache[1]
        trades = self._journal_trades(rows)
        pauses = await asyncio.to_thread(J.losing_contexts, trades, seed=S_seed("guard", len(trades)))
        self._guard_cache = (key, pauses)
        return pauses

    @staticmethod
    def _guard_text(hit: dict[str, Any]) -> str:
        lo, hi = hit["ci"]
        return (f"aprendió de sus pérdidas que en «{hit['label']}: {hit['bucket_label']}» pierde de forma consistente "
                f"({hit['wins']} de {hit['n']} ganadoras en 90 días, R medio {fmt_r(hit['mean_r'], 2)}, IC 99 % "
                f"{fmt_r(lo, 2)} a {fmt_r(hi, 2)}). Vuelve a probar cuando esas operaciones tengan más de 90 días, o "
                "desactiva «Aprender de sus pérdidas».")

    async def close_autopilot(self, position_id: int | None = None, all: bool = False, *,
                              symbol: str | None = None) -> list[dict]:
        """The user closes NEXUS's positions by hand (one by id, those of a symbol, or all of them): noted in its
        decision log."""
        if position_id is None and not all and not symbol:
            raise PaperError("Indica la posición a cerrar o «all» para cerrarlas todas")
        closed = await self.close_position(position_id, symbol, all, account_id=AUTOPILOT_ACCOUNT)
        for pos in closed:
            inst = option_inst(pos)
            what = f"El {inst.get('right')} {inst.get('name')}, cerrado" if inst is not None else "Cerrada"
            self._autopilot_note(traded_symbol(pos), "close", f"{what} a mano a {fmt_price(pos.get('exit_price'))} "
                                                              f"({fmt_r(pos.get('r_multiple'))})")
        return closed

    async def set_auto(self, enabled: bool, symbols: list[str] | None = None, tf: str | None = None) -> dict:
        values: dict[str, Any] = {"paper.auto_trade": bool(enabled)}
        if symbols is not None:
            values["paper.auto_symbols"] = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if tf is not None:
            try:
                values["paper.auto_tf"] = normalize_tf(tf)
            except ValueError as exc:
                raise PaperError(str(exc)) from None
        if self.rt.settings is not None:
            await self.rt.settings.update(values)
        auto = {
            "enabled": bool(self._setting("paper.auto_trade", False)),
            "symbols": list(self._setting("paper.auto_symbols", []) or []),
            "tf": str(self._setting("paper.auto_tf", "1h")),
        }
        if auto["enabled"]:
            syms = ", ".join(auto["symbols"]) or "ningún símbolo (añade alguno)"
            self._emit("info", "paper", f"Auto-trading activado en {syms} ({auto['tf']}): solo señales TOMAR")
        else:
            self._emit("info", "paper", "Auto-trading desactivado")
        if config.BACKGROUND:
            await self._sync_subscriptions()
        await self.publish(1)
        return auto

    def _on_setting(self, key: str, value: Any) -> None:
        if not self._started:
            return
        try:
            if key.startswith("autopilot."):
                # Several keys are saved one at a time (Ajustes): re-read them once they settle, never half-saved
                # (e.g. enabled before its source and list). set_autopilot re-reads them itself.
                if not self._autopilot_quiet:
                    self._autopilot_changed = time.monotonic()
                    if not self._autopilot_settling:
                        self._autopilot_settling = True
                        self._spawn(self._autopilot_settle())
            elif key.startswith("paper.auto") and config.BACKGROUND:
                self._spawn(self._sync_subscriptions())
        except RuntimeError:
            pass

    async def _sync_subscriptions(self) -> None:
        hub = self.rt.hub
        if hub is None or not hasattr(hub, "subscribe"):
            return
        wanted: set[tuple[str, str, str]] = set()
        for pos in self._open.values():
            if pos["origin"] == "sim":
                continue
            sym = traded_symbol(pos)  # an option: the live bars of its underlying price it
            tf = (pos.get("tf") or "1h") if sym.startswith("SIM:") else "1m"
            wanted.add((sym, tf, "paper"))
        for order in self._orders.values():
            tf = (order.get("tf") or "1h") if order["symbol"].startswith("SIM:") else "1m"
            wanted.add((order["symbol"], tf, "paper"))
        for prof in self._auto_profiles():
            owner = "paper:auto" if prof.account_id == 1 else "paper:autopilot"
            for sym in prof.symbols:
                wanted.add((sym, prof.tf, owner))
        for item in wanted - self._subs:
            try:
                await call_maybe_async(hub.subscribe, *item)
                self._subs.add(item)
            except Exception as exc:
                if self._log.should_log(f"sub:{item[0]}", 600):
                    self._emit("warn", "paper", f"No se pudo seguir {item[0]} en vivo: {exc}")
        for item in self._subs - wanted:
            try:
                await call_maybe_async(hub.unsubscribe, *item)
            except Exception:
                pass
            self._subs.discard(item)

    # ------------------------------------------------------------------ background monitor

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(PUBLISH_EVERY_S)
                await self._expire_orders()
                await self._poll_stale_prices()
                await self._expire_options()
                await self.refresh_advice()
                await self._refresh_autopilot()
                await self._autopilot_watchdog()
                if any(p["account_id"] == 1 for p in self._open.values()):
                    await self.publish(1)
                if any(p["account_id"] == AUTOPILOT_ACCOUNT for p in self._open.values()):
                    await self.publish(AUTOPILOT_ACCOUNT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._log.should_log("monitor", 300):
                    self._emit("error", "paper", f"Monitor de posiciones: {type(exc).__name__}: {exc}")

    async def _poll_stale_prices(self) -> None:
        """Fallback when no live candles arrive: poll the hub quotes and manage the levels with live prices only.

        A stale quote (flagged by the hub or older than QUOTE_STALE_S: venue closed, feed down, only the local cache
        left) never moves a stop or fills a target; the user is told once and the levels wait for a live price.
        """
        symbols = sorted({traded_symbol(p) for p in self._open.values()
                          if p["origin"] != "sim" and self.cached_price(traded_symbol(p), PRICE_STALE_S) is None}
                         | {o["symbol"] for o in self._orders.values()
                            if self.cached_price(o["symbol"], PRICE_STALE_S) is None})
        hub = self.rt.hub
        if not symbols or hub is None or not hasattr(hub, "quotes"):
            return
        try:
            got = await asyncio.wait_for(hub.quotes(symbols), timeout=20)
        except Exception:
            return
        if not isinstance(got, dict):
            return
        changed = False
        now = now_ms()
        for sym in symbols:
            q = got.get(sym)
            price = safe_float(q.get("price")) if isinstance(q, dict) else None
            if price is None or price <= 0:
                continue
            if not quote_is_live(q, now):
                if sym not in self._stale_warned:
                    self._stale_warned.add(sym)
                    ts = safe_float(q.get("ts"))
                    age = f"el último es de hace {_age_text((now - ts) / 1000.0)}" if ts else "solo hay uno antiguo"
                    has_position = any(traded_symbol(p) == sym and p["origin"] != "sim" for p in self._open.values())
                    what = ("el stop y los objetivos se vigilarán" if has_position
                            else "las órdenes pendientes solo se ejecutan con precios en vivo: se vigilarán")
                    self._emit("warn", "paper", f"{sym}: sin precio en vivo ({age}); {what} cuando vuelva a cotizar",
                               {"symbol": sym})
                continue
            self._stale_warned.discard(sym)
            self._prices[sym] = (price, time.monotonic())
            async with self._lock:
                for pos in [p for p in self._open.values() if traded_symbol(p) == sym and p["origin"] != "sim"]:
                    before = (pos["qty_open"], pos["tp_hits"])
                    px = price
                    inst = option_inst(pos)
                    if inst is not None:  # the stock's live price as the option's theoretical premium
                        pos["meta"]["spot"] = price
                        px = option_premium(inst, price, now_s())
                    await self._process_price(pos, px, px, px, open_=None, use_hl=False, time_s=now_s())
                    changed = changed or (pos["qty_open"], pos["tp_hits"]) != before
            if self._orders:
                self._check_orders(sym, price, price, price, open_=None, bar_time=None, bar_s=60, live=True,
                                   fill_time=now_s())
        if changed:
            await self.publish(1)


def _order_from_row(row: dict) -> dict:
    meta = json_loads(row.get("meta"), {}) or {}
    return {
        "id": int(row["id"]),
        "account_id": int(row["account_id"]),
        "symbol": str(row["symbol"]).upper(),
        "tf": row.get("tf") or "1h",
        "side": row["side"],
        "kind": row["kind"],
        "price": float(row["price"]),
        "qty": safe_float(row.get("qty")),
        "risk_pct": safe_float(row.get("risk_pct")),
        "sl": safe_float(row.get("sl")),
        "tp": safe_float(row.get("tp")),
        "expiry": row.get("expiry") or "gtc",
        "expire_bars": int(row["expire_bars"]) if row.get("expire_bars") is not None else None,
        "expire_time": int(row["expire_time"]) if row.get("expire_time") is not None else None,
        "status": row.get("status") or "pending",
        "origin": row.get("origin") or "manual",
        "gate_verdict": row.get("gate_verdict"),
        "gate_score": safe_float(row.get("gate_score")),
        "position_id": int(row["position_id"]) if row.get("position_id") is not None else None,
        "fill_price": safe_float(row.get("fill_price")),
        "fill_time": int(row["fill_time"]) if row.get("fill_time") is not None else None,
        "reason": row.get("reason"),
        "cancelled_by": row.get("cancelled_by"),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]) if row.get("updated_at") is not None else None,
        "meta": meta if isinstance(meta, dict) else {},
    }


def _pos_from_row(row: dict) -> dict:
    meta = json_loads(row.get("meta"), {}) or {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": int(row["id"]),
        "account_id": int(row["account_id"]),
        "symbol": str(row["symbol"]).upper(),
        "tf": row.get("tf") or "1h",
        "side": row["side"],
        "qty": float(row["qty"]),
        "qty_open": float(row["qty_open"]),
        "entry_price": float(row["entry_price"]),
        "entry_time": int(row["entry_time"]),
        "sl": float(row["sl"]) if row.get("sl") is not None else float(row["entry_price"]),
        "tp1": safe_float(row.get("tp1")),
        "tp2": safe_float(row.get("tp2")),
        "tp3": safe_float(row.get("tp3")),
        "trail": safe_float(row.get("trail")),
        "tp_hits": int(row.get("tp_hits") or 0),
        "status": row.get("status") or "open",
        "exit_price": safe_float(row.get("exit_price")),
        "exit_time": int(row["exit_time"]) if row.get("exit_time") is not None else None,
        "pnl": float(row.get("pnl") or 0.0),
        "pnl_pct": float(row.get("pnl_pct") or 0.0),
        "r_multiple": safe_float(row.get("r_multiple")),
        "exit_reason": row.get("exit_reason"),
        "origin": row.get("origin") or "manual",
        "gate_verdict": row.get("gate_verdict"),
        "gate_score": safe_float(row.get("gate_score")),
        "meta": meta,
    }
