"""NEXUS automática, phase 3: THEORETICAL stock options (app.engine.options). Black-Scholes prices with the 20-day
historical volatility, the monthly expiry and the strike NEXUS buys within its premium budget, the instrument it
uses for a signal on a stock (autopilot.stock_instrument: auto = shares to bet on a rise and a put on a fall, since
a cash account cannot sell short; options = calls and puts; shares = shares both ways), and how an option position
is managed: priced from its underlying, levels on the premium (stop −50 %, targets +50 / +100 / +150 %), whole
contracts under the cash exposure, NEXUS's exit signals on the stock and the close 7 days before its expiry."""
from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import date

import numpy as np
import pytest

from app.data.base import CandleSet
from app.engine import options as opt
from app.engine.costs import asset_class
from app.sim import autopilot_compare
from app.sim import paper as pp
from tests.test_imp_autopilot import H, SessionHub, _candle, _settle, no_background  # noqa: F401 (fixture)
from tests.test_sim_fakes import make_current

F, AAPL = "NYSE:F", "NASDAQ:AAPL"


def _daily(symbol: str, price: float, ret: float = 0.02, n: int = 41) -> CandleSet:
    """`n` daily closes ending at `price` whose log returns alternate ±ret: a 20-day volatility of
    ret·√(20/19)·√252 (≈ 32,6 % with 2 %)."""
    logs = np.array([0.0 if (n - 1 - i) % 2 == 0 else ret for i in range(n)])
    close = price * np.exp(logs)
    t = np.arange(n, dtype=np.int64) * 86400 + 1_700_000_000
    return CandleSet(symbol, "1d", t, close, close, close, close, np.ones(n))


class StockNexus:
    """NexusService stand-in that answers each symbol's closed bar with its own analysis (None while live)."""

    def __init__(self, by_symbol: dict[str, dict]):
        self.by_symbol = dict(by_symbol)

    async def current(self, symbol: str, tf: str, *, upto: int | None = None) -> dict | None:
        return None if upto is None else self.by_symbol.get(symbol)


def _sell(price: float) -> dict:
    return make_current("SELL", price=price, prob_up=0.3, score=-0.5, long_verdict="AVOID", short_verdict="TAKE")


async def _engine(rt, prices: dict[str, float], nexus: StockNexus, **settings) -> pp.PaperEngine:
    await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_positions_per_group": 5, **settings})
    rt.hub = SessionHub(prices=prices, open_markets=set(prices),
                        candles={(s, "1d"): _daily(s, p) for s, p in prices.items()})
    rt.nexus = nexus
    engine = pp.PaperEngine(rt)
    await engine.start()
    return engine


async def _next_bar(engine: pp.PaperEngine, symbol: str, price: float) -> None:
    """The next 1h bar of `symbol` closes: its trader decides on it (a watched symbol is decided once per bar)."""
    t = engine._auto_bar[(3, symbol, "1h")]
    await engine._on_candle(symbol, "1h", _candle(t + H, price), True)
    await _settle(engine)


async def _open_put(engine: pp.PaperEngine, symbol: str = F) -> dict:
    """An option position opened the way NEXUS's trader opens one (its plan for a bet on a fall)."""
    plan = await engine._option_plan(symbol, "short", engine._autopilot_cfg())
    res = await engine.open_position(3, plan["symbol"], "long", "1h", qty=plan["qty"], sl=plan["sl"], tps=plan["tps"],
                                     price=plan["premium"], origin="autopilot", check_gate=False,
                                     instrument=plan["instrument"])
    return res["position"]


def test_black_scholes_prices_parity_greeks_and_the_historical_volatility():
    # the textbook values (S = K = 100, one year, 20 % of volatility, 5 % of rate)
    assert opt.bs_price("call", 100, 100, 1.0, 0.2, 0.05) == pytest.approx(10.4506, abs=1e-3)
    assert opt.bs_price("put", 100, 100, 1.0, 0.2, 0.05) == pytest.approx(5.5735, abs=1e-3)
    for s, k, t, sigma in ((100, 90, 0.3, 0.25), (20, 22, 0.1, 0.6), (230, 230, 0.12, 0.3)):
        c, p = opt.bs_price("call", s, k, t, sigma), opt.bs_price("put", s, k, t, sigma)
        assert c - p == pytest.approx(s - k * math.exp(-opt.RATE * t), abs=1e-9)  # put-call parity
        dc, dp = opt.bs_delta("call", s, k, t, sigma), opt.bs_delta("put", s, k, t, sigma)
        assert 0 < dc < 1 and -1 < dp < 0 and dc - dp == pytest.approx(1.0)
        assert opt.bs_price("call", s * 1.01, k, t, sigma) > c and opt.bs_price("put", s * 1.01, k, t, sigma) < p
    # at (or after) the expiry an option is worth its intrinsic value
    assert opt.bs_price("call", 105, 100, 0.0, 0.3) == 5.0 and opt.bs_price("put", 105, 100, 0.0, 0.3) == 0.0
    assert opt.bs_price("put", 95, 100, 0.0, 0.3) == 5.0
    closes = list(_daily("X:Y", 12.0).close)
    assert opt.historical_vol(closes) == pytest.approx(0.02 * math.sqrt(20 / 19) * math.sqrt(252), rel=1e-9)
    assert opt.historical_vol(closes[-20:]) is None  # 21 closes needed for 20 returns


def test_expiries_strikes_symbols_and_the_strike_it_picks():
    assert opt.third_friday(2026, 12) == date(2026, 12, 18) and opt.third_friday(2026, 11) == date(2026, 11, 20)
    assert opt.monthly_expiry(date(2026, 11, 6)) == date(2026, 12, 18)  # 20/11 is only 14 days away
    assert opt.monthly_expiry(date(2026, 11, 18)) == date(2026, 12, 18)  # exactly 30 days
    assert opt.monthly_expiry(date(2026, 12, 1)) == date(2027, 1, 15)
    assert opt.strike_grid(231.4, "call") == [230.0, 235.0, 240.0, 245.0]
    assert opt.strike_grid(231.4, "put") == [230.0, 225.0, 220.0, 215.0]
    assert opt.strike_grid(19.8, "put") == [20.0, 19.5, 19.0, 18.5]
    sym = opt.option_symbol(AAPL, date(2026, 12, 18), "call", 230.0)
    assert sym == "OPT:NASDAQ:AAPL:20261218:C:230"
    assert opt.parse_option_symbol(sym) == {"underlying": AAPL, "expiry": date(2026, 12, 18), "right": "call",
                                            "strike": 230.0}
    assert opt.option_symbol(F, date(2026, 12, 18), "put", 12.5) == "OPT:NYSE:F:20261218:P:12.5"
    assert opt.option_name(F, date(2026, 12, 18), "put", 12.5) == "F 12,5 P 18/12/2026"
    for bad in (AAPL, "OPT:NASDAQ:AAPL", "OPT:NASDAQ:AAPL:2026-12-18:C:230", "OPT:NASDAQ:AAPL:20261218:X:230"):
        assert opt.parse_option_symbol(bad) is None
    assert asset_class(sym) == "option" and pp.risk_group(sym) == "stock"  # an option counts with its stock
    # the at-the-money strike when one contract fits the budget, otherwise a cheaper one further out (|delta| ≥ 0,30)
    t, sigma = 0.12, 0.3
    atm = opt.bs_price("call", 100.0, 100.0, t, sigma) * 100
    pick = opt.choose_option(100.0, "call", t, sigma, atm * 2.5)
    assert (pick["strike"], pick["contracts"]) == (100.0, 2) and pick["cost"] == pytest.approx(atm)
    out = opt.choose_option(100.0, "call", t, sigma, atm * 0.8)
    assert out["strike"] > 100.0 and out["cost"] <= atm * 0.8 and out["delta"] >= opt.MIN_DELTA
    none = opt.choose_option(100.0, "call", t, sigma, 10.0)
    assert none["none"] is True and none["cheapest"] > 10.0


def test_auto_buys_a_put_to_bet_on_a_fall_and_shares_to_bet_on_a_rise(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            nexus = StockNexus({F: _sell(12.0), AAPL: make_current("BUY", price=100.0)})
            # the open-risk cap off: the put's risk (half its premium) depends on the days left to its expiry
            engine = await _engine(rt, {F: 12.0, AAPL: 100.0}, nexus, **{"paper.max_open_risk_pct": 0})
            st = (await engine.autopilot_snapshot())["autopilot"]
            assert (st["stock_instrument"], st["options_premium_pct"]) == ("auto", 10.0)
            await engine.set_autopilot(enabled=True, source="list", symbols=[F, AAPL])
            await _settle(engine)
            held = {pp.traded_symbol(engine._open[p["id"]]): p for p in engine.open_positions(3)}
            assert set(held) == {F, AAPL}, engine.autopilot_status()["log"]
            put, shares = held[F], held[AAPL]
            assert shares["instrument"] is None and shares["side"] == "long" and shares["symbol"] == AAPL
            inst = put["instrument"]
            assert put["symbol"].startswith("OPT:NYSE:F:") and put["symbol"].endswith(":P:12")
            assert put["side"] == "long"  # it only buys options
            assert (inst["kind"], inst["right"], inst["strike"], inst["underlying"], inst["multiplier"]) == (
                "option", "put", 12.0, F, 100.0)
            assert inst["name"].startswith("F 12 P ") and inst["sigma"] == pytest.approx(0.3257, abs=1e-3)
            expiry = date.fromisoformat(inst["expiry"])
            assert expiry.weekday() == 4 and 15 <= expiry.day <= 21 and opt.days_to(expiry, pp.now_s()) >= 29
            premium = put["entry_price"]
            expected = opt.bs_price("put", 12.0, 12.0, opt.years_to(expiry, pp.now_s()), inst["sigma"])
            assert premium == pytest.approx(expected, rel=1e-3)
            # its premium budget: 10 % of the ~1 000 $ equity (AAPL's fee may be booked first), in whole contracts of
            # 100 shares: as many as fit
            cost = premium * 100
            assert inst["contracts"] >= 1 and inst["contracts"] * cost <= 100.0 < (inst["contracts"] + 1) * cost + 1.0
            assert put["qty"] == inst["contracts"] * 100
            assert put["sl"] == pytest.approx(premium * 0.5) and put["tp1"] == pytest.approx(premium * 1.5)
            assert (put["tp2"], put["tp3"]) == (pytest.approx(premium * 2.0), pytest.approx(premium * 2.5))
            log = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert re.search(r"Compra \d+ contratos? put F 12 P", log) and "prima teórica" in log
            assert "volatilidad histórica 33 %" in log
            assert any("Abierto F 12 P" in e["message"] and "(put)" in e["message"] for e in rt.bus.recent())
            # the gate decision is logged on the stock, with the gate's own levels (not premiums)
            rows = await rt.db.fetchall("SELECT symbol, direction, entry, taken FROM gate_log ORDER BY symbol")
            assert [(r["symbol"], r["direction"], r["entry"], r["taken"]) for r in rows] == [
                (AAPL, "long", 100.0, 1), (F, "short", 12.0, 1)]
            # priced from its stock: a fall of F to 10 lifts the put (time decay included)
            engine._prices[F] = (10.0, time.monotonic())
            [now_put] = [p for p in engine.open_positions(3) if p["instrument"]]
            value = opt.bs_price("put", 10.0, 12.0, opt.years_to(expiry, pp.now_s()), inst["sigma"])
            assert now_put["price"] == pytest.approx(value, rel=1e-3) and now_put["price"] > premium + 1.4
            # its fees are those of an option (a % of the premium), its cash exposure the premium paid
            assert engine._fee_pct(put["symbol"]) == opt.FEE_PCT
            # the watched list shows the stock, never the option's symbol
            assert {w["symbol"] for w in engine.autopilot_status()["watching"]} == {F, AAPL}
            await engine.stop()

    asyncio.run(scenario())


def test_options_mode_buys_calls_shares_mode_sells_short_and_no_option_that_does_not_fit(runtime_factory,
                                                                                          no_background):
    async def scenario():
        async with runtime_factory() as rt:
            nexus = StockNexus({F: make_current("BUY", price=12.0), AAPL: _sell(230.0)})
            engine = await _engine(rt, {F: 12.0, AAPL: 230.0}, nexus)
            await engine.set_autopilot(enabled=True, source="list", symbols=[F, AAPL], stock_instrument="options")
            await _settle(engine)
            [call] = engine.open_positions(3)
            assert call["instrument"]["right"] == "call" and call["instrument"]["underlying"] == F
            # AAPL: even the cheapest put allowed costs far more than its budget (10 % of its equity: ~100 $, less
            # when F's call and its fee were booked first, both bars are decided concurrently)
            log = engine.autopilot_status()["log"]
            [skip] = [e for e in log if e["symbol"] == AAPL]
            assert skip["kind"] == "skip" and skip["text"].startswith("Put descartado: ningún put de AAPL cabe en su "
                                                                     "presupuesto de primas (")
            assert ", 10 % del capital): el más barato con delta de al menos 0,30 cuesta" in skip["text"]
            assert "por contrato (precio teórico)" in skip["text"]
            await engine.close_autopilot(all=True)
            assert any("El call F 12 C" in e["text"] and "cerrado a mano" in e["text"]
                       for e in engine.autopilot_status()["log"])
            # shares both ways: a SELL is a (simulated) short sale of shares
            await engine.set_autopilot(symbols=[AAPL], stock_instrument="shares")
            await _next_bar(engine, AAPL, 230.0)
            [short] = engine.open_positions(3)
            assert (short["symbol"], short["side"], short["instrument"]) == (AAPL, "short", None)
            await engine.close_autopilot(all=True)
            # auto with no premium allowed: a fall is not traded (no puts, no short sales in a cash account)
            await engine.set_autopilot(stock_instrument="auto", options_premium_pct=0)
            await _next_bar(engine, AAPL, 230.0)
            assert engine.open_positions(3) == []
            assert engine.autopilot_status()["log"][0]["text"] == (
                "Put descartado: no compra opciones (su prima máxima por operación es 0 %) y en una cuenta de "
                "efectivo no vende acciones en corto.")
            with pytest.raises(pp.PaperError, match="Instrumento no válido"):
                await engine.set_autopilot(stock_instrument="futuros")
            with pytest.raises(pp.PaperError, match="prima máxima"):
                await engine.set_autopilot(options_premium_pct=80)
            # without 21 daily closes there is no volatility, so no theoretical price and no option
            rt.hub.candles.pop((F, "1d"))
            nexus.by_symbol[F] = _sell(12.0)
            await engine.set_autopilot(symbols=[F], options_premium_pct=10)
            await _next_bar(engine, F, 12.0)
            assert engine.open_positions(3) == []
            assert any(e["symbol"] == F and "faltan 21 cierres diarios de F" in e["text"]
                       for e in engine.autopilot_status()["log"])
            await engine.stop()

    asyncio.run(scenario())


def test_an_option_follows_its_stock_its_premium_levels_and_nexus_exit_signals(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            nexus = StockNexus({F: _sell(12.0)})
            engine = await _engine(rt, {F: 12.0}, nexus)
            await engine.set_autopilot(enabled=True, source="list", symbols=[F])
            await _settle(engine)
            [put] = engine.open_positions(3)
            premium, n = put["entry_price"], put["instrument"]["contracts"]
            assert engine._auto_follows(engine._open[put["id"]])  # its trader follows it through the stock
            await engine._sync_subscriptions()
            assert (F, "1m", "paper") in rt.hub.subs and not any(s.startswith("OPT:") for s, _, _ in rt.hub.subs)
            # a 1m bar of the stock down to 6: the put is worth more than twice its premium → TP2 (whole position:
            # partial targets need a multiple of 3 contracts)
            t = pp.now_s() + 1
            await engine._on_candle(F, "1m", {"time": t, "open": 12.0, "high": 12.05, "low": 6.0, "close": 6.5,
                                              "volume": 1}, False)
            [h] = await engine.history(3)
            assert h["exit_reason"] == ("TP2" if n % 3 else "TP3")
            if n % 3:
                assert h["exit_price"] == pytest.approx(premium * 2.0)
                fees = opt.FEE_PCT / 100 * (premium + 2.0 * premium)
                assert h["r_multiple"] == pytest.approx((premium - fees) / (0.5 * premium), abs=0.01)
            assert any(e["message"].startswith("NEXUS automática · Cerrado F 12 P") for e in rt.bus.recent())
            # a new put; NEXUS turns to BUY on the next closed bar: it leaves the bet on a fall and buys shares
            await engine.reset(account_id=3)
            await _next_bar(engine, F, 12.0)
            [put] = engine.open_positions(3)
            nexus.by_symbol[F] = make_current("BUY", price=12.0)
            await _next_bar(engine, F, 12.0)
            [h] = await engine.history(3)
            assert (h["id"], h["exit_reason"]) == (put["id"], "REVERSE")
            [shares] = engine.open_positions(3)
            assert (shares["symbol"], shares["side"], shares["instrument"]) == (F, "long", None)
            log = [e["text"] for e in engine.autopilot_status()["log"]]
            assert any(x.startswith("Cierra el put F 12 P") and "señal contraria" in x for x in log)
            # live advice on an option speaks of its stock
            info = pp.assess_option({"right": "call", "underlying": F}, make_current("EXIT_LONG", price=12.0))
            assert info["early_exit"] and info["advice"] == "Cerrar ya: NEXUS deja su apuesta al alza en F (CERRAR LARGO)"
            hold = pp.assess_option({"right": "put", "underlying": F}, make_current("HOLD_SHORT", price=12.0))
            assert not hold["early_exit"] and hold["advice"].startswith("Mantener: NEXUS no da señal de salida en F")
            await engine.stop()

    asyncio.run(scenario())


def test_its_stop_on_the_premium_and_whole_contracts_under_the_cash_exposure(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            nexus = StockNexus({F: _sell(12.0)})
            # 25 % of premium budget (several contracts), but only 8 % of the equity may be invested
            engine = await _engine(rt, {F: 12.0}, nexus, **{"paper.max_open_risk_pct": 0,
                                                            "paper.daily_loss_limit_pct": 0})
            await engine.set_autopilot(enabled=True, source="list", symbols=[F], options_premium_pct=25,
                                       max_exposure_pct=8)
            await _settle(engine)
            [put] = engine.open_positions(3)
            premium = put["entry_price"]
            fit = int(math.floor(80.0 / (premium * 100) + 1e-9))
            assert put["instrument"]["contracts"] == fit < int(250.0 // (premium * 100))
            assert engine._open[put["id"]]["meta"]["exposure_capped"] is True
            # the stock jumps to 20: the put's premium falls through its stop (−50 %), filled at the stop
            t = pp.now_s() + 1
            await engine._on_candle(F, "1m", {"time": t, "open": 12.0, "high": 20.0, "low": 11.95, "close": 19.0,
                                              "volume": 1}, False)
            [h] = await engine.history(3)
            assert h["exit_reason"] == "SL" and h["exit_price"] == pytest.approx(premium * 0.5)
            fees = opt.FEE_PCT / 100 * 1.5 * premium
            assert h["r_multiple"] == pytest.approx((-0.5 * premium - fees) / (0.5 * premium), abs=0.01)
            # with less free cash than one contract costs, nothing is bought
            await engine.set_autopilot(max_exposure_pct=1)
            await _next_bar(engine, F, 12.0)
            assert engine.open_positions(3) == []
            assert any(e["text"].startswith("Put descartado: Exposición máxima") for e in engine.autopilot_status()["log"])
            await engine.stop()

    asyncio.run(scenario())


def test_an_option_leaves_seven_days_before_its_expiry_or_settles_at_its_intrinsic_value(runtime_factory,
                                                                                        no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {F: 12.0}, StockNexus({}))
            pos = await _open_put(engine)
            inst = pos["instrument"]
            end = opt.expiry_ts(date.fromisoformat(inst["expiry"]))
            assert await engine._expire_options(now=end - 8 * 86400) == 0  # still more than 7 days to go
            engine._prices[F] = (11.0, time.monotonic())
            assert await engine._expire_options(now=end - 6 * 86400) == 1
            [h] = await engine.history(3)
            assert (h["exit_reason"], h["exit_label"]) == ("EXPIRY", "Cierre a 7 días del vencimiento")
            left = opt.years_to(date.fromisoformat(inst["expiry"]), end - 6 * 86400)
            assert h["exit_price"] == pytest.approx(opt.bs_price("put", 11.0, 12.0, left, inst["sigma"]), rel=1e-6)
            # no live price of the stock: it waits, unless the expiry passed (then its intrinsic value, last price 10)
            engine._prices[F] = (12.0, time.monotonic())  # a new put at-the-money at 12
            pos = await _open_put(engine)
            assert pos["instrument"]["strike"] == 12.0
            engine._prices.pop(F, None)
            engine._open[pos["id"]]["meta"]["spot"] = 10.0
            assert await engine._expire_options(now=end - 3 * 86400) == 0
            assert await engine._expire_options(now=end + 3600) == 1
            h = (await engine.history(3))[0]
            assert h["exit_price"] == pytest.approx(2.0) and h["exit_label"] == "Vencimiento: liquidada a su valor intrínseco"
            await engine.stop()

    asyncio.run(scenario())


def test_the_comparison_with_random_entries_leaves_options_out(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.db.execute(
                "INSERT INTO paper_positions(account_id, symbol, tf, side, qty, qty_open, entry_price, entry_time, sl, "
                "tp_hits, status, pnl, pnl_pct, r_multiple, origin, meta) VALUES (3, ?, '1h', 'long', 100, 0, 0.5, "
                "1760000000, 0.25, 0, 'closed', 10, 20, 0.4, 'autopilot', '{}')", ("OPT:NYSE:F:20261218:P:12",))
            out = await autopilot_compare.compare(rt, 3)
            assert out["trades"] == 0 and "Las operaciones con opciones no entran" in out["note"]

    asyncio.run(scenario())
