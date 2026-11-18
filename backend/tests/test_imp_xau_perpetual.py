"""Gold like MetaTrader's XAUUSD for NEXUS automática: Binance's XAUUSDT «TradFi» perpetual (no spot pair) served by
Binance USDⓈ-M futures (provider binance_um, a fallback right after the first spot pair of a `.P` route), traded as a
perpetual whose 0.01 lot is 1 troy ounce, in the same risk group as PAX Gold."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import config
from app.data import providers as P
from app.data import providers_http as H
from app.data.symbols import route_note, route_symbol
from app.engine import contracts as C
from app.engine import lots as L
from app.sim import paper as pp
from app.util import http_client
from tests.test_imp_autopilot import BarNexus, _settle
from tests.test_imp_autopilot_perpetuals import _signal
from tests.test_sim_fakes import FakeHub

XAU = "BINANCE:XAUUSDT.P"
# Real answer of fapi.binance.com/fapi/v1/klines?symbol=XAUUSDT&interval=1h (2026-11-17 03:00..04:00 UTC)
XAU_1H = [
    [1794913200000, "3860.17", "3877.29", "3859.90", "3874.60", "14214.710", 1794916799999, "54999931.92369", 43952,
     "8196.364", "31712932.62870", "0"],
    [1794916800000, "3874.59", "3875.39", "3860.66", "3864.85", "16312.993", 1794920399999, "63078421.16710", 52537,
     "8644.201", "33421825.97938", "0"],
]


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(H, "LIMITS", {k: (0.0, 4) for k in H.LIMITS})
    monkeypatch.setattr(config, "OFFLINE", False)


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(config, "BACKGROUND", False)
    monkeypatch.setattr(config, "OFFLINE", True)


def test_routes_a_perpetual_to_binance_futures_after_its_first_spot_pair():
    route = route_symbol(XAU)
    assert [(v.provider, v.native) for v in route.venues[:2]] == [("binance", "XAUUSDT"), ("binance_um", "XAUUSDT")]
    futures = route.venues[1]
    assert futures.tv_symbol == XAU and route_note(route, futures) is None  # the instrument itself, not an equivalent
    assert route_note(route, route.venues[0]) == "Datos de BINANCE:XAUUSDT (par equivalente)"
    # a coin with a spot pair keeps it first; spot symbols and dated futures never get the futures source
    assert route_symbol("BINANCE:BTCUSDT.P").venues[0].provider == "binance"
    assert all(v.provider != "binance_um" for v in route_symbol("BINANCE:BTCUSDT").venues)
    assert all(v.provider != "binance_um" for v in route_symbol("BINANCE:BTCUSD25Z2026").venues)
    assert P.supports("binance_um", "4h") and P.to_native("binance_um", "XAU/USDT") == "XAUUSDT"


def test_xauusdt_candles_come_from_binance_futures_when_spot_has_no_pair(online):
    hosts: list[str] = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.host == "fapi.binance.com":
            assert req.url.path == "/fapi/v1/klines" and req.url.params["symbol"] == "XAUUSDT"
            return httpx.Response(200, json=XAU_1H)
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})  # no XAUUSDT spot pair

    async def go():
        async with http_client(transport=httpx.MockTransport(handler)) as c:
            return await P.fetch_venues(route_symbol(XAU).venues, "1h", 2, client=c)

    rows, venue = asyncio.run(go())
    assert venue.provider == "binance_um" and hosts[:2] == ["data-api.binance.vision", "fapi.binance.com"]
    assert [r["time"] for r in rows] == [1794913200, 1794916800] and rows[-1]["close"] == pytest.approx(3864.85)


def test_xau_is_gold_in_lots_and_one_group_with_pax_gold():
    spec = C.perpetual_spec(XAU)
    assert spec == {"root": "XAUUSDT.P", "name": "Oro XAUUSD (perpetuo XAU/USDT)", "multiplier": 0.001,
                    "perpetual": True}
    assert L.lot_spec(XAU) == {"size": 100.0, "unit": "oz"}  # 1 lot = 100 oz, like MetaTrader's XAUUSD
    assert L.lot_spec("BINANCE:XAGUSDT.P") == {"size": 5000.0, "unit": "oz"}
    assert L.step_units(100.0, spec["multiplier"]) == pytest.approx(1.0)  # 0,01 lot = 1 oz = 1 000 contracts
    assert pp.risk_group(XAU, False) == pp.risk_group("BINANCE:PAXGUSDT.P", False) == "crypto:XAU"
    assert pp.risk_group_label("crypto:XAU") == "Oro (XAU)"
    assert pp.risk_group("BINANCE:BTCUSDT.P", False) == "crypto:BTC"


def test_autopilot_trades_xauusd_in_lots(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"risk.slippage_pct": 0.0, "paper.max_open_risk_pct": 0})
            rt.hub = FakeHub(prices={XAU: 100.0})
            rt.nexus = BarNexus(default=_signal("BUY", 2.0))
            engine = pp.PaperEngine(rt)
            await engine.start()
            # 1 % of 1 000 = 10 $ at a 2-point stop → 5 oz = 0,05 lot (5 000 contracts of 0,001 oz)
            await engine.set_autopilot(enabled=True, source="list", symbols=[XAU], lots=True)
            await _settle(engine)
            [pos] = engine.open_positions(3)
            assert pos["qty"] == pytest.approx(5.0) and pos["instrument"]["contracts"] == 5000
            assert pos["instrument"]["name"] == "Oro XAUUSD (perpetuo XAU/USDT)"
            assert pos["lot"] == {"lots": 0.05, "size": 100.0, "unit": "oz"}
            text = " ".join(e["text"] for e in engine.autopilot_status()["log"])
            assert "0,05 lotes (5,000 XAU) con 10x" in text
            await engine.stop()

    asyncio.run(scenario())
