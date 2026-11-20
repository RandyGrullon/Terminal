"""Mirror of NEXUS automática in a MetaTrader 5 account of any broker (app.broker.mt5_mirror) against a fake MetaTrader5
package (no terminal, no network). The broker's own symbol names found by themselves or from the user's map; NEXUS's
units turned into lots with the broker's contract size, on its step, capped by broker.mt5_max_lots; closes against the
position's ticket; a client id never sent twice; and the REAL-account gate: a real account receives nothing unless the
user switched broker.mt5_allow_real on AND typed that exact account number. Plus its API and terminal command."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from app import config as nexus_config
from app.broker import mt5 as mt5lib
from app.broker.mirror import MirrorError
from app.broker.mt5_mirror import Mt5MirrorService, bases_of, parse_symbol_map, pick_symbol
from tests.test_imp_autopilot import BarNexus, _engine, no_background  # noqa: F401 (fixture)
from tests.test_sim_fakes import make_current

XAU, BTC_P, AAPL, ETHBTC = "OANDA:XAUUSD", "BINANCE:BTCUSDT.P", "NASDAQ:AAPL", "BINANCE:ETHBTC"
PRICES = {XAU: 3700.0, BTC_P: 50_000.0, AAPL: 100.0, ETHBTC: 0.05}


class FakeMt5:
    TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TIME_GTC = 1, 0, 1, 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2

    def __init__(self, *, kind: int = 0, login: int = 5550001, algo: bool = True) -> None:
        self.kind, self.login, self.algo = kind, login, algo
        self.requests: list[dict] = []
        self.deals: list[NS] = []
        self.inits = 0
        self.symbols = {  # name -> (contract size, min, step)
            "GOLD": (100.0, 0.01, 0.01), "BTCUSD": (1.0, 0.01, 0.01), "EURUSD": (100_000.0, 0.01, 0.01),
        }
        self._ticket = 1000

    def initialize(self, path: str | None = None) -> bool:
        self.inits += 1
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self):
        return (1, "ok")

    def terminal_info(self):
        return NS(trade_allowed=self.algo, connected=True) if self.inits else None

    def account_info(self):
        return NS(login=self.login, server="Broker-Demo" if self.kind == 0 else "Broker-Real", company="Broker Ltd",
                  currency="USD", balance=10_000.0, equity=10_010.0, margin_free=9_000.0, leverage=500,
                  trade_mode=self.kind, trade_allowed=True, trade_expert=True, name="Nombre Del Titular")

    def symbols_get(self):
        return [NS(name=n) for n in self.symbols]

    def symbol_info(self, name: str):
        if name not in self.symbols:
            return None
        size, vmin, step = self.symbols[name]
        return NS(visible=True, volume_min=vmin, volume_step=step, volume_max=50.0, trade_contract_size=size,
                  filling_mode=2, trade_mode=4, digits=2)

    def symbol_select(self, name: str, on: bool) -> bool:
        return True

    def symbol_info_tick(self, name: str):
        return NS(ask=3701.0, bid=3699.0) if name == "GOLD" else NS(ask=50_010.0, bid=49_990.0)

    def order_send(self, req: dict):
        self.requests.append(dict(req))
        self._ticket += 1
        position = req.get("position") or self._ticket
        price = req["price"]
        self.deals.append(NS(ticket=self._ticket + 5000, order=self._ticket, position_id=position, volume=req["volume"],
                             price=price, magic=req["magic"], comment=req["comment"]))
        return NS(retcode=10009, order=self._ticket, deal=self._ticket + 5000, volume=req["volume"], price=price,
                  comment="Request executed")

    def history_deals_get(self, *args, ticket: int | None = None, **kw):
        if ticket is not None:
            return [d for d in self.deals if d.ticket == ticket]
        return list(self.deals)


async def _setup(rt, monkeypatch, fake: FakeMt5, **settings):
    monkeypatch.setattr(nexus_config, "OFFLINE", False)
    monkeypatch.setattr(mt5lib, "load_module", lambda: fake)
    monkeypatch.setattr(mt5lib, "available", lambda: True)
    monkeypatch.setattr(mt5lib.Mt5Client.__init__, "__defaults__", ("", lambda: fake))
    engine = await _engine(rt, dict(PRICES), BarNexus(default=make_current("WAIT")))
    rt.paper = engine
    await rt.settings.update({"paper.max_open_risk_pct": 0, "paper.max_positions_per_group": 0,
                              "broker.mt5_enabled": True, "broker.mt5_account": "10k", **settings})
    svc = Mt5MirrorService(rt)
    await svc.start()
    return engine, svc


async def _open(engine, symbol: str, side: str = "long", qty: float = 1.0) -> int:
    price = PRICES[symbol]
    sl = price * (0.95 if side == "long" else 1.05)
    res = await engine.open_position(3, symbol, side, "1h", qty=qty, sl=sl, origin="autopilot", check_gate=False,
                                     price=price)
    return int(res["position"]["id"])


def _entry(status: dict, pos_id: int) -> dict:
    return next(e for e in status["entries"] if e["pos_id"] == pos_id)


def test_symbols_of_any_broker():
    assert bases_of("BINANCE:XAUUSDT.P") == ("XAU", "") and bases_of(XAU) == ("XAU", "")
    assert bases_of(AAPL) == ("AAPL", "") and bases_of("SIM:NEXUS")[0] is None
    assert "dólares" in bases_of(ETHBTC)[1]
    assert pick_symbol("XAU", ["XAUUSD.m", "XAUEUR"]) == "XAUUSD.m"
    assert pick_symbol("XAU", ["XAUUSDm", "XAUEUR"]) == "XAUUSDm"
    assert pick_symbol("XAU", ["GOLD", "EURUSD"]) == "GOLD"
    assert pick_symbol("XAU", ["XAUEUR"]) is None  # never a gold quoted in another currency
    assert pick_symbol("AAPL", ["AAPL.OQ"]) == "AAPL.OQ"
    assert pick_symbol("XAU", ["GOLD"], {"XAU": "GOLD#"}) == "GOLD#"
    assert parse_symbol_map("xau=GOLD#, btc = BTCUSD.m ; bad") == {"XAU": "GOLD#", "BTC": "BTCUSD.m"}


def test_the_real_account_gate():
    cfg = {"allow_real": False, "real_login": ""}
    demo = {"kind": "demo", "login": 1, "server": "S", "connected": True, "trade_allowed": True,
            "terminal_trade_allowed": True}
    real = {**demo, "kind": "real", "login": 777}
    assert Mt5MirrorService.permission(demo, cfg) is None
    assert Mt5MirrorService.permission({**demo, "kind": "contest"}, cfg) is None
    assert "REAL y no autorizaste" in Mt5MirrorService.permission(real, cfg)
    assert "no es la que autorizaste" in Mt5MirrorService.permission(real, {"allow_real": True, "real_login": "778"})
    assert Mt5MirrorService.permission(real, {"allow_real": True, "real_login": "777"}) is None
    assert "Trading algorítmico" in Mt5MirrorService.permission({**demo, "terminal_trade_allowed": False}, cfg)
    # the switch is off by default: the app never turns it on
    from app.config import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["broker.mt5_allow_real"] is False and DEFAULT_SETTINGS["broker.mt5_enabled"] is False


def test_demo_account_follows_nexus_in_lots(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeMt5(kind=0)
            engine, svc = await _setup(rt, monkeypatch, fake)
            gold = await _open(engine, XAU, qty=4.0)  # 4 oz = 0,04 lots of 100 oz
            big = await _open(engine, BTC_P, "short", qty=0.5)  # 0,5 lots > the 0,1 cap
            stock = await _open(engine, AAPL, qty=1.0)  # the fake broker has no AAPL
            await svc.sync()
            sent = {r["symbol"]: r for r in fake.requests}
            assert sent["GOLD"]["volume"] == pytest.approx(0.04) and sent["GOLD"]["type"] == FakeMt5.ORDER_TYPE_BUY
            assert sent["GOLD"]["comment"] == f"nexus-{gold}-o1" and sent["GOLD"]["magic"] == mt5lib.MAGIC
            assert sent["BTCUSD"]["volume"] == pytest.approx(0.1) and sent["BTCUSD"]["type"] == FakeMt5.ORDER_TYPE_SELL
            assert "position" not in sent["GOLD"]
            st = await svc.overview()
            assert _entry(st, gold)["status"] == "open" and _entry(st, gold)["mt5_symbol"] == "GOLD"
            assert _entry(st, stock)["status"] == "skipped" and "Ajustes → MetaTrader 5" in _entry(st, stock)["reason"]
            assert st["provider"] == "mt5" and st["paper"] is True and st["blocked"] is None
            assert st["account"]["login"] == 5550001 and st["account"]["kind"] == "demo"
            assert "Nombre Del Titular" not in str(st)  # never the holder's name
            # NEXUS closes the gold: a sell against that position's ticket
            ticket = fake.requests[0]["position"] if "position" in fake.requests[0] else None
            await engine.close_position(position_id=gold, account_id=3, price=3710.0)
            await svc.sync()
            close = fake.requests[-1]
            assert close["symbol"] == "GOLD" and close["type"] == FakeMt5.ORDER_TYPE_SELL
            assert close["volume"] == pytest.approx(0.04) and close["position"] and close["comment"] == f"nexus-{gold}-c2"
            assert ticket is None
            assert _entry(await svc.overview(), gold)["status"] == "closed"
            # a lost state never doubles an order: the client id is found in the terminal's deals
            n = len(fake.requests)
            svc2 = Mt5MirrorService(rt)
            svc2._entries.clear()
            res = await svc2._client(svc2._cfg()).send_market(symbol="GOLD", buy=True, volume=0.04,
                                                               client_order_id=f"nexus-{gold}-o1")
            assert len(fake.requests) == n and res["filled_qty"] == pytest.approx(0.04)
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_a_real_account_gets_nothing_unless_authorized_for_that_number(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeMt5(kind=2, login=777)
            engine, svc = await _setup(rt, monkeypatch, fake)
            await _open(engine, XAU, qty=4.0)
            await svc.sync()
            assert fake.requests == []
            st = await svc.overview()
            assert st["paper"] is False and st["account"]["kind"] == "real"
            assert "REAL y no autorizaste" in st["blocked"] and "No opera" in st["error"]
            # switched on for ANOTHER real account: still nothing
            await rt.settings.update({"broker.mt5_allow_real": True, "broker.mt5_real_login": "778"})
            await svc.sync()
            assert fake.requests == []
            # the user's own switch for this exact number: it follows NEXUS (a new position, opened from now)
            await rt.settings.update({"broker.mt5_real_login": "777"})
            await _open(engine, XAU, qty=2.0)
            await svc.sync()
            assert [r["volume"] for r in fake.requests] == [pytest.approx(0.02)]
            # the cap holds on a real account too
            await rt.settings.update({"broker.mt5_max_lots": 0})
            await _open(engine, XAU, qty=3.0)
            await svc.sync()
            assert len(fake.requests) == 1
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_off_offline_or_without_the_package_it_sends_nothing(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeMt5()
            engine, svc = await _setup(rt, monkeypatch, fake, **{"broker.mt5_enabled": False})
            await _open(engine, XAU, qty=4.0)
            await svc.sync()
            assert fake.requests == [] and fake.inits == 0
            with pytest.raises(MirrorError, match="desactivado"):
                await svc.sync_now()
            await rt.settings.update({"broker.mt5_enabled": True})
            monkeypatch.setattr(nexus_config, "OFFLINE", True)
            await svc.sync()
            assert fake.requests == []
            monkeypatch.setattr(nexus_config, "OFFLINE", False)
            monkeypatch.setattr(mt5lib, "available", lambda: False)
            with pytest.raises(MirrorError, match="Windows"):
                await svc.sync_now()
            assert "Windows" in (await svc.overview())["error"]
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_api_and_terminal(api_client):
    st = api_client.get("/api/broker/mt5/status").json()
    assert (st["enabled"], st["provider"], st["entries"]) == (False, "mt5", [])
    r = api_client.post("/api/broker/mt5/sync")
    assert r.status_code == 400 and "MetaTrader 5" in r.json()["detail"]
    res = api_client.post("/api/terminal", json={"text": "espejo mt5"}).json()
    assert res["ok"] and "Espejo en MetaTrader 5 desactivado" in "\n".join(l["text"] for l in res["lines"])
    assert api_client.get("/api/health").json()["services"]["broker_mt5"]["enabled"] is False


def test_by_default_it_copies_the_1k_account(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeMt5(kind=0)
            engine, svc = await _setup(rt, monkeypatch, fake, **{"broker.mt5_account": "1k"})
            await _open(engine, XAU, qty=4.0)  # a trade of the 10k account: not copied
            await svc.sync()
            assert fake.requests == [] and "No hay cuenta de 1k" in svc._error
            snap = await engine.create_twin()
            twin = snap["twin"]["id"]
            res = await engine.open_position(twin, XAU, "long", "15m", qty=1.0, sl=3690.0, origin="autopilot",
                                             check_gate=False, price=PRICES[XAU])
            await svc.sync()
            assert [(r["symbol"], r["volume"], r["comment"]) for r in fake.requests] == [
                ("GOLD", 0.01, f"nexus-{res['position']['id']}-o1")]
            assert (await svc.overview())["source_account"] == "1k"
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())
