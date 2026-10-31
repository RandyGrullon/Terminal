"""Lead integration fixes of the review round: delayed TradingView quotes, non-finite terminal flags, fees by class."""
from __future__ import annotations

from app.data.tradingview_parse import row_quote
from app.engine.costs import asset_class, fee_pct_for
from app.settings import SettingsStore
from app.terminal import CommandContext

ROW = {"symbol": "NASDAQ:AAPL", "close": 231.5, "change": 0.4, "change_abs": 0.9, "volume": 1e6, "high": 232.0,
       "low": 229.8, "open": 230.1}


def test_delayed_quote_announces_its_delay():
    q = row_quote({**ROW, "update_mode": "delayed_streaming_900"}, 1_700_000_000_000)
    assert q["delay_s"] == 900 and q["ts"] == 1_700_000_000_000


def test_realtime_quote_has_no_delay_key():
    assert "delay_s" not in row_quote({**ROW, "update_mode": "streaming"}, 1)
    assert "delay_s" not in row_quote(ROW, 1)


def test_terminal_flags_ignore_non_finite_values():
    ctx = CommandContext("candles btc", "candles", ["btc"], {"limit": "inf", "risk": "nan", "big": "1e999", "ok": "12"})
    assert ctx.flag_int("limit", 300) == 300
    assert ctx.flag_float("risk", 1.0) == 1.0
    assert ctx.flag_float("big", 2.0) == 2.0
    assert ctx.flag_int("ok", 0) == 12


def test_fee_by_asset_class_and_fallback():
    settings = {"risk.fee_pct": 0.075, "risk.fee_pct_by_class": {"forex": 0.004}}
    assert fee_pct_for("FX:EURUSD", settings) == 0.004
    assert fee_pct_for("BINANCE:BTCUSDT", settings) == 0.075  # crypto uses risk.fee_pct
    assert fee_pct_for("NASDAQ:AAPL", settings) == 0.075  # class missing from the map -> risk.fee_pct
    assert asset_class("SIM:NEXUS") == "crypto"


def test_fee_map_setting_rejects_unknown_classes_and_non_finite_values():
    assert SettingsStore.coerce("risk.fee_pct_by_class", '{"forex": 0.003}')["forex"] == 0.003
    for bad in ({"crypto": 0.1}, {"forex": float("nan")}, "[1, 2]"):
        try:
            SettingsStore.coerce("risk.fee_pct_by_class", bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


# ----------------------------------------------------------------------------- paper: bars replayed after a stall


def _paper_candle(t: int, o: float, h: float, l: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def test_paper_stops_crossed_during_a_feed_stall_fill_from_the_replayed_bars(runtime_factory, monkeypatch):
    """The hub replays the bars missed during a stall as closed=True (final exchange OHLC, oldest first). They are
    no live price, but a stop crossed meanwhile must fill, at the end of the bar that crossed it."""
    import asyncio
    import time

    from app import config as nexus_config
    from app.sim import paper as pp
    from tests.test_sim_fakes import FakeHub, FakeNexus

    sym = "BINANCE:BTCUSDT"
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)
    clock = [1_800_000_000]
    monkeypatch.setattr(pp, "now_s", lambda: clock[0])

    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.fee_pct": 0.1, "risk.slippage_pct": 0.0})
            rt.hub = FakeHub(prices={sym: 100.0})
            rt.nexus = FakeNexus("TAKE")
            engine = pp.PaperEngine(rt)
            await engine.start()
            pos = (await engine.open_position(1, sym, "long", "1h"))["position"]
            assert pos["sl"] == 98.0
            b0 = int(pos["entry_time"]) // 60 * 60 + 60
            engine._prices[sym] = (100.0, time.monotonic() - 30)
            clock[0] = b0 + 600  # the feed comes back 10 minutes later and replays b0..b0+4min, closed
            await engine._on_candle(sym, "1m", _paper_candle(b0, 100.0, 100.3, 99.2, 99.4), True)
            assert engine.open_positions(1)[0]["status"] == "open"
            await engine._on_candle(sym, "1m", _paper_candle(b0 + 60, 99.4, 99.5, 97.6, 97.9), True)  # crosses 98
            assert engine.open_positions(1) == []
            closed = (await engine.history(1))[0]
            assert closed["exit_reason"] == "SL" and closed["exit_price"] == 98.0
            assert closed["exit_time"] == b0 + 120  # end of the bar that crossed the stop, not the replay time
            assert engine._prices[sym][0] == 100.0  # a replayed bar is not a live price
            await engine.stop()

    asyncio.run(scenario())


def test_paper_ignores_replayed_bars_that_ended_before_the_entry(runtime_factory, monkeypatch):
    import asyncio

    from app import config as nexus_config
    from app.sim import paper as pp
    from tests.test_sim_fakes import FakeHub, FakeNexus

    sym = "BINANCE:BTCUSDT"
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)

    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.fee_pct": 0.1, "risk.slippage_pct": 0.0})
            rt.hub = FakeHub(prices={sym: 100.0})
            rt.nexus = FakeNexus("TAKE")
            engine = pp.PaperEngine(rt)
            await engine.start()
            pos = (await engine.open_position(1, sym, "long", "1h"))["position"]
            old = int(pos["entry_time"]) // 60 * 60 - 3600  # e.g. the last session's bar of a closed market
            await engine._on_candle(sym, "1m", _paper_candle(old, 97.5, 97.6, 97.0, 97.2), True)
            assert engine.open_positions(1)[0]["status"] == "open"
            await engine.stop()

    asyncio.run(scenario())
