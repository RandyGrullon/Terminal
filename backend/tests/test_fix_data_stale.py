"""DATA fixes (I2 stale data): candle sets whose newest bar is far too old are flagged stale with a Spanish note
and the real source of the rows; quotes from the local cache carry the real time of the price and `stale`;
numeric query parameters reject NaN / inf with a Spanish 400."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.api import market as market_api
from app.data import hub as hub_mod
from app.data.providers_http import DataProviderError
from app.util import floor_time
from tests.test_data_hub_fakes import Clock, FakeProviders, bar, make_hub

BTC = "BINANCE:BTCUSDT"
BOBA = "HTX:BOBAUSDT"
AAPL = "NASDAQ:AAPL"
H = 3600
D = 86400
INSERT = ("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")


async def insert(rt, symbol: str, tf: str, times: list[int], source: str, close: float | None = None) -> None:
    rows = []
    for t in times:
        c = bar(t)
        rows.append((symbol, tf, t, c["open"], c["high"], c["low"], close if close is not None else c["close"],
                     c["volume"], source))
    await rt.db.executemany(INSERT, rows)


class DeadVenues(FakeProviders):
    """KuCoin stopped returning recent bars (pair delisted), the other venues do not list the pair."""

    async def fetch_candles(self, provider, native, tf, limit, *, client, end_time=None):
        self.calls.append({"provider": provider, "native": native, "tf": tf, "limit": int(limit),
                           "end_time": end_time})
        if provider == "kucoin":
            return []
        raise DataProviderError(f"{provider}: par no encontrado ({native})", provider=provider, kind="not_found")


def test_delisted_pair_cache_is_stale_and_names_real_source(runtime_factory, monkeypatch):
    clock = Clock()
    fake = DeadVenues(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            last = floor_time(clock(), "1h") - 22 * D  # the venue stopped trading 22 days ago
            await insert(rt, BOBA, "1h", [last - i * H for i in range(200)], "kucoin")
            cs = await hub.get_candles(BOBA, "1h", 100)  # after a restart: nothing known in memory
            assert len(cs) == 100 and cs.stale is True
            assert cs.note.startswith("Datos desactualizados: última vela hace 22 días")
            assert "No se pudo actualizar" in cs.note
            assert cs.provider == "kucoin" and cs.history == "full"  # the rows came from KuCoin, not Binance
            row = await rt.db.fetchone("SELECT provider FROM symbols WHERE symbol = ?", (BOBA,))
            assert row["provider"] == "kucoin" and hub.history_kind(BOBA) == ("kucoin", "full")
            calls = len(fake.calls)
            again = await hub.get_candles(BOBA, "1h", 100)  # definitive answers: backoff, no hammering
            assert again.stale and len(fake.calls) == calls

    asyncio.run(scenario())


def test_fresh_series_are_not_stale(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock)

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            cs = await hub.get_candles(BTC, "1h", 50)
            assert cs.stale is False and cs.note is None
            sim = await hub.get_candles("SIM:NEXUS", "1h", 50)
            assert sim.stale is False
            # stocks: a weekend is normal, eight days without bars is not
            now_bar = floor_time(clock(), "1d")
            await insert(rt, AAPL, "1d", [now_bar - 3 * D - i * D for i in range(20)], "recorder")
            cs = await hub.get_candles(AAPL, "1d", 20)
            assert cs.history == "recorder" and cs.stale is False
            await rt.db.execute("DELETE FROM candles WHERE symbol = ?", (AAPL,))
            await insert(rt, AAPL, "1d", [now_bar - 8 * D - i * D for i in range(20)], "recorder")
            cs = await hub.get_candles(AAPL, "1d", 20)
            assert cs.stale is True and cs.note.startswith("Datos desactualizados: última vela hace 8 días")

    asyncio.run(scenario())


def test_stale_note_thresholds():
    now = 1_760_000_400 + 1800
    t = floor_time(now, "1h")
    assert hub_mod.stale_note(t, "1h", now, continuous=True) is None
    assert hub_mod.stale_note(t - 3 * H, "1h", now, continuous=True) is None  # 3 bars behind: not yet
    assert hub_mod.stale_note(t - 5 * H, "1h", now, continuous=True) == "Datos desactualizados: última vela hace 6 horas"
    m = floor_time(now, "1m")
    assert hub_mod.stale_note(m - 10 * 60, "1m", now, continuous=True) is None  # quiet minutes of an illiquid pair
    assert hub_mod.stale_note(m - 3 * H, "1m", now, continuous=True).endswith("hace 3 horas")
    assert hub_mod.stale_note(t - 5 * H, "1h", now, continuous=False) is None  # a night of a stock market
    assert hub_mod.ago_text(60) == "1 minuto" and hub_mod.ago_text(3600 * 1.2) == "72 minutos"
    assert hub_mod.ago_text(3 * H) == "3 horas" and hub_mod.ago_text(D * 1.0) == "24 horas"
    assert hub_mod.ago_text(2 * D) == "2 días" and hub_mod.ago_text(H) == "60 minutos"


# ----------------------------------------------------------------------------- quotes


def test_local_quote_has_real_time_and_stale_flag(runtime_factory, monkeypatch):
    clock = Clock()
    fake = FakeProviders(clock, fail={p: DataProviderError("sin red", provider=p, kind="network")
                                      for p in ("binance", "okx", "coinbase", "kraken", "kucoin")})

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=fake, clock=clock) as (rt, hub):
            old = floor_time(clock(), "1h") - 9 * H  # deep scan earlier today, nothing since
            await rt.db.execute(INSERT, ("BINANCE:SOLUSDT", "1h", old - H, 149.0, 150.0, 148.0, 149.5, 10.0, "binance"))
            await rt.db.execute(INSERT, ("BINANCE:SOLUSDT", "1h", old, 150.0, 151.0, 149.0, 150.5, 10.0, "binance"))
            q = (await hub.quotes(["BINANCE:SOLUSDT"]))["BINANCE:SOLUSDT"]  # TradingView unavailable
            assert q["price"] == 150.5 and q["stale"] is True
            # not "now": the newest bar may have been stored while forming, only its open is certain (= the end of
            # the final bar before it)
            assert q["ts"] == old * 1000
            assert await hub.last_price("BINANCE:SOLUSDT", allow_stale=False) is None
            assert await hub.last_price("BINANCE:SOLUSDT") == 150.5  # default kept for manual actions
            # a bar that is still forming: only its open time is certain, but it is recent
            m = floor_time(clock(), "1m")
            await rt.db.execute(INSERT, (AAPL, "1m", m, 200.0, 201.0, 199.0, 200.5, 5.0, "recorder"))
            q = (await hub.quotes([AAPL]))[AAPL]
            assert q["price"] == 200.5 and "stale" not in q and q["ts"] == m * 1000
            assert await hub.last_price(AAPL, allow_stale=False) == 200.5
            # a live update beats the cache
            hub.subscribe("BINANCE:SOLUSDT", "1h", "t")
            await hub._dispatch("BINANCE:SOLUSDT", "1h", {**bar(old + 9 * H), "close": 155.0}, False)
            q = (await hub.quotes(["BINANCE:SOLUSDT"]))["BINANCE:SOLUSDT"]
            assert q["price"] == 155.0 and "stale" not in q

    asyncio.run(scenario())


def test_exchange_price_of_dead_venue_is_not_live(runtime_factory, monkeypatch):
    clock = Clock()

    class OldBars(FakeProviders):
        async def fetch_latest(self, provider, native, tf, *, client, retries=2, timeout=None):
            t = floor_time(self.clock(), "1m") - 3 * H
            return [bar(t - 60), bar(t)]

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, providers=OldBars(clock), clock=clock) as (_rt, hub):
            assert await hub._exchange_price(BTC) is None
            assert await hub.last_price(BTC, allow_stale=False) is None

    asyncio.run(scenario())


# ----------------------------------------------------------------------------- API parameters


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "1e999", "Infinity", "abc"])
def test_int_param_rejects_non_finite(value):
    with pytest.raises(HTTPException) as err:
        market_api._int_param(value, "limit", 500, 1, 5000)
    assert err.value.status_code == 400 and "número entero" in err.value.detail
    assert market_api._int_param("12.7", "limit", 500, 1, 5000) == 12
    assert market_api._int_param("1e9", "limit", 500, 1, 5000) == 5000


def test_api_rejects_inf_limits_with_spanish_400(api_client):
    for path, params in (("/api/candles", {"symbol": "SIM:NEXUS", "tf": "1h", "limit": "inf"}),
                         ("/api/universe", {"market": "crypto", "offset": "inf"}),
                         ("/api/symbols/search", {"q": "btc", "limit": "-inf"}),
                         ("/api/candles", {"symbol": "SIM:NEXUS", "tf": "1h", "limit": "nan"})):
        r = api_client.get(path, params=params)
        assert r.status_code == 400, (path, r.status_code, r.text)
        assert "número entero" in r.json()["detail"]


def test_hub_numeric_arguments_reject_inf(runtime_factory, monkeypatch):
    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, offline=True) as (_rt, hub):
            for bad in (float("inf"), float("nan")):
                with pytest.raises(hub_mod.ProviderError):
                    await hub.get_candles("SIM:NEXUS", "1h", bad)
                with pytest.raises(hub_mod.ProviderError):
                    await hub.universe("crypto", offset=bad)
                with pytest.raises(hub_mod.ProviderError):
                    await hub.search("btc", limit=bad)

    asyncio.run(scenario())


def test_candles_api_flags_stale_sets(runtime_factory, monkeypatch):
    clock = Clock()

    async def scenario():
        async with make_hub(runtime_factory, monkeypatch, offline=True, clock=clock) as (rt, _hub):
            last = floor_time(clock(), "1h") - 10 * D
            await insert(rt, BTC, "1h", [last - i * H for i in range(30)], "binance")
            data = await market_api.get_candles(symbol=BTC, tf="1h", limit="20")
            assert data["stale"] is True and data["note"].startswith("Datos desactualizados: última vela hace 10 días")
            assert len(data["candles"]) == 20
            fresh = await market_api.get_candles(symbol="SIM:NEXUS", tf="1h", limit="5")
            assert "stale" not in fresh  # the canonical CandlesResponse keys when the data is fine

    asyncio.run(scenario())
