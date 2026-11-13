"""Mirror of NEXUS automática in an Alpaca PAPER account (app.broker) against a fake Alpaca (httpx MockTransport: no
network). Only the paper URL with the user's keys; orders with the same quantity and direction; partial exits and
closes that follow NEXUS; idempotent client ids across a restart; what is not mirrored and why (synthetic markets,
crypto shorts, fractional shorts, a closed US market, old positions, other asset classes); rejected orders; the price
difference with the simulation as «x de n»; never a secret in status, events or the terminal; nothing at all while it
is off or offline. Plus its API, terminal command and the services table."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import config as nexus_config
from app.broker import alpaca as alpaca_mod
from app.broker.mirror import MirrorError, MirrorService, alpaca_symbol
from app.util import now_s
from tests.test_imp_autopilot import BarNexus, _engine, no_background  # noqa: F401 (fixture)
from tests.test_sim_fakes import make_current

AAPL, TSLA, BTC, ETH = "NASDAQ:AAPL", "NASDAQ:TSLA", "BINANCE:BTCUSDT", "BINANCE:ETHUSDT"
KEY_ID, SECRET = "PKTESTKEY123", "secreto-de-prueba-456"
PRICES = {AAPL: 100.0, TSLA: 200.0, BTC: 50_000.0, ETH: 2_000.0, "SIM:NEXUS": 10.0}


class FakeAlpaca:
    """Alpaca's paper API, scripted: orders fill at `fill_price` right away (unless auto_fill is off), positions
    follow the fills (Alpaca keeps `crypto_fee` of the coins bought), `reject` answers an error for a symbol."""

    def __init__(self, market_open: bool = True):
        self.market_open = market_open
        self.requests: list[httpx.Request] = []
        self.orders: dict[str, dict] = {}
        self.by_cid: dict[str, str] = {}
        self.positions: dict[str, float] = {}
        self.fill_price = {"AAPL": 100.05, "TSLA": 200.0, "BTC/USD": 49_990.0, "ETH/USD": 2_000.0}
        self.reject: dict[str, tuple[int, str]] = {}
        self.auto_fill = True
        self.crypto_fee = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.scheme == "https" and request.url.host == "paper-api.alpaca.markets", request.url
        assert request.headers["APCA-API-KEY-ID"] == KEY_ID and request.headers["APCA-API-SECRET-KEY"] == SECRET
        path = request.url.path
        if path == "/v2/account":
            return httpx.Response(200, json={"equity": "100000", "cash": "99000.5", "buying_power": "200000",
                                             "status": "ACTIVE", "currency": "USD", "account_number": "PA3SECRETO"})
        if path == "/v2/clock":
            return httpx.Response(200, json={"is_open": self.market_open})
        if path == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": s, "qty": str(q)} for s, q in self.positions.items()])
        if path == "/v2/orders" and request.method == "POST":
            body = json.loads(request.content)
            cid = body["client_order_id"]
            if cid in self.by_cid:
                return httpx.Response(422, json={"message": "client_order_id must be unique"})
            if body["symbol"] in self.reject:
                status, message = self.reject[body["symbol"]]
                return httpx.Response(status, json={"message": message})
            oid = f"orden-{len(self.orders) + 1}"
            self.orders[oid] = {"id": oid, "client_order_id": cid, "status": "accepted", "filled_qty": "0",
                                "filled_avg_price": None, **body}
            self.by_cid[cid] = oid
            snapshot = dict(self.orders[oid])
            if self.auto_fill:
                self.fill(oid)
            return httpx.Response(200, json=snapshot)
        if path == "/v2/orders:by_client_order_id":
            return httpx.Response(200, json=self.orders[self.by_cid[request.url.params["client_order_id"]]])
        if path.startswith("/v2/orders/"):
            return httpx.Response(200, json=self.orders[path.rsplit("/", 1)[1]])
        return httpx.Response(404, json={"message": "no existe"})

    def fill(self, oid: str) -> None:
        o = self.orders[oid]
        qty, sym = float(o["qty"]), o["symbol"]
        o.update(status="filled", filled_qty=o["qty"], filled_avg_price=str(self.fill_price[sym]))
        key = sym.replace("/", "")
        buy = o["side"] == "buy"
        got = qty * (1.0 - self.crypto_fee) if "/" in sym and buy else qty
        self.positions[key] = round(self.positions.get(key, 0.0) + (got if buy else -got), 9)
        if abs(self.positions[key]) < 1e-12:
            del self.positions[key]

    def posted(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    def install(self, monkeypatch) -> None:
        def factory(timeout: float = 15.0, **kw):
            return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), headers=kw.pop("headers", None) or {},
                                     timeout=timeout)

        monkeypatch.setattr(alpaca_mod, "http_client", factory)


async def _setup(rt, monkeypatch, fake: FakeAlpaca, *, enabled: bool = True):
    monkeypatch.setattr(nexus_config, "OFFLINE", False)  # the mirror never sends anything offline
    fake.install(monkeypatch)
    engine = await _engine(rt, dict(PRICES), BarNexus(default=make_current("WAIT")))
    rt.paper = engine
    await rt.settings.update({"paper.max_open_risk_pct": 0, "paper.max_positions_per_group": 0,
                              "broker.alpaca_enabled": enabled, "broker.alpaca_key_id": KEY_ID,
                              "broker.alpaca_secret_key": SECRET})
    svc = MirrorService(rt)
    await svc.start()
    return engine, svc


async def _open(engine, symbol: str, side: str = "long", qty: float = 1.0, *, ago_s: int = 0) -> int:
    price = PRICES[symbol]
    sl = price * (0.95 if side == "long" else 1.05)
    res = await engine.open_position(3, symbol, side, "1h", qty=qty, sl=sl, origin="autopilot", check_gate=False,
                                     price=price, time_s=now_s() - ago_s if ago_s else None)
    return int(res["position"]["id"])


def _entry(status: dict, pos_id: int) -> dict:
    return next(e for e in status["entries"] if e["pos_id"] == pos_id)


def test_symbols_alpaca_can_trade_and_why_the_others_are_not_mirrored():
    assert alpaca_symbol(AAPL) == ("AAPL", "stock", "")
    assert alpaca_symbol("AMEX:SPY") == ("SPY", "stock", "")
    assert alpaca_symbol(BTC) == ("BTC/USD", "crypto", "")
    assert alpaca_symbol("COINBASE:ETHUSD") == ("ETH/USD", "crypto", "")
    for symbol, why in (("BINANCE:BTCUSDT.P", "derivado"), ("OPT:NASDAQ:AAPL:C:200:20261218", "opción teórica"),
                        ("SIM:NEXUS", "sintético"), ("CME_MINI:MES1!", "futures"), ("FX:EURUSD", "forex"),
                        ("BMV:AMXL", "EE. UU."), ("BINANCE:ETHBTC", "dólares"), ("OTC:GBTC", "EE. UU.")):
        sym, kind, reason = alpaca_symbol(symbol)
        assert sym is None and kind == "" and why in reason, (symbol, reason)
    assert "desactivado" in alpaca_symbol(AAPL, stocks=False)[2] and "desactivado" in alpaca_symbol(BTC, crypto=False)[2]
    assert alpaca_mod.PAPER_URL == "https://paper-api.alpaca.markets"
    assert alpaca_mod.qty_text(0.0123) == "0.0123" and alpaca_mod.qty_text(3.0) == "3"


def test_mirror_follows_nexus_opens_partials_and_closes_and_measures_the_difference(runtime_factory, no_background,
                                                                                     monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeAlpaca()
            fake.crypto_fee = 0.0025
            engine, svc = await _setup(rt, monkeypatch, fake)
            aapl = await _open(engine, AAPL, qty=2.5)
            btc = await _open(engine, BTC, qty=0.002)
            await svc.sync()
            posted = fake.posted()
            assert posted == [
                {"symbol": "AAPL", "side": "buy", "type": "market", "time_in_force": "day", "qty": "2.5",
                 "client_order_id": f"nexus-{aapl}-o1"},
                {"symbol": "BTC/USD", "side": "buy", "type": "market", "time_in_force": "gtc", "qty": "0.002",
                 "client_order_id": f"nexus-{btc}-o1"},
            ]
            st = await svc.overview()
            a, b = _entry(st, aapl), _entry(st, btc)
            assert (a["status"], a["status_label"], a["fill_entry"], a["held"]) == ("open", "Abierta en Alpaca", 100.05, 2.5)
            assert a["slip_entry_bps"] == 5.0 and b["slip_entry_bps"] == -2.0  # AAPL paid more, BTC got a better price
            assert st["connected"] and st["account"] == {"equity": 100000.0, "cash": 99000.5, "buying_power": 200000.0,
                                                         "status": "ACTIVE", "currency": "USD"}
            # NEXUS takes half at its first target: the mirror sells the same share of what it bought
            engine._open[aapl]["qty_open"] = 1.25  # (the engine's partial fill, done by hand for the test)
            await svc.sync()
            assert fake.posted()[-1] == {"symbol": "AAPL", "side": "sell", "type": "market", "time_in_force": "day",
                                         "qty": "1.25", "client_order_id": f"nexus-{aapl}-r2"}
            assert _entry(await svc.overview(), aapl)["held"] == 1.25
            await svc.sync()  # nothing new to do: no order
            assert len(fake.posted()) == 3
            # NEXUS closes both: the mirror closes its part (crypto: what Alpaca holds after its fee)
            fake.fill_price["AAPL"] = 101.9
            await engine.close_position(position_id=aapl, account_id=3, price=102.0)
            await engine.close_position(position_id=btc, account_id=3, price=51_000.0)
            await svc.sync()
            closes = fake.posted()[3:]
            assert closes == [
                {"symbol": "AAPL", "side": "sell", "type": "market", "time_in_force": "day", "qty": "1.25",
                 "client_order_id": f"nexus-{aapl}-c3"},
                {"symbol": "BTC/USD", "side": "sell", "type": "market", "time_in_force": "gtc", "qty": "0.001995",
                 "client_order_id": f"nexus-{btc}-c2"},
            ]
            st = await svc.overview()
            a = _entry(st, aapl)
            # its exit price is the average of every exit (half at 100,05 with the partial, half at 101,9 with the close);
            # the exit difference compares the final close with NEXUS's exit
            assert (a["status"], a["held"], a["sim_exit"]) == ("closed", 0.0, 102.0)
            assert a["fill_exit"] == pytest.approx((100.05 + 101.9) / 2)
            assert a["slip_exit_bps"] == pytest.approx(9.8, abs=0.01)  # closed 0,1 below NEXUS's exit: worse
            assert a["mirror_result_pct"] == pytest.approx(((100.05 + 101.9) / 2 - 100.05) / 100.05 * 100, abs=1e-3)
            assert a["sim_result_pct"] is not None and fake.positions == {}
            stats = st["stats"]
            assert (stats["entries"], stats["mirrored"], stats["closed"], stats["slip_entry_n"]) == (2, 2, 2, 2)
            assert stats["slip_entry_bps"] == 1.5 and stats["result_n"] == 2
            assert stats["text"].startswith("2 de 2 posiciones replicadas · entrada: 1,5 pb peor que la simulación")
            assert "muestra pequeña (2 cerradas): sin conclusiones" in stats["text"]
            # no secret anywhere it shows or logs
            events = json.dumps([e for e in rt.bus.recent(200)], ensure_ascii=False, default=str)
            for secret in (KEY_ID, SECRET, "PA3SECRETO"):
                assert secret not in json.dumps(st) and secret not in events and secret not in json.dumps(svc.status())
            assert any("Espejo en Alpaca: abre AAPL" in e["message"] for e in rt.bus.recent(200, source="broker"))
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_what_is_not_mirrored_says_why_and_a_rejected_order_is_kept(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeAlpaca()
            fake.reject["TSLA"] = (403, "insufficient buying power")
            engine, svc = await _setup(rt, monkeypatch, fake)
            ids = {
                "sim": await _open(engine, "SIM:NEXUS", qty=10),
                "crypto_short": await _open(engine, ETH, "short", qty=0.05),
                "tiny_short": await _open(engine, AAPL, "short", qty=0.5),
                "old": await _open(engine, BTC, qty=0.001, ago_s=600),
                "rejected": await _open(engine, TSLA, qty=1),
            }
            await svc.sync()
            st = await svc.overview()
            reasons = {k: _entry(st, pid)["reason"] for k, pid in ids.items()}
            assert "sintético" in reasons["sim"] and "cortos en cripto" in reasons["crypto_short"]
            assert "cortos fraccionados" in reasons["tiny_short"] and "más de 2 minutos" in reasons["old"]
            assert "403" in reasons["rejected"] and "insufficient buying power" in reasons["rejected"]
            assert {_entry(st, pid)["status"] for k, pid in ids.items() if k != "rejected"} == {"skipped"}
            assert _entry(st, ids["rejected"])["status_label"] == "Rechazada por Alpaca"
            assert [p["symbol"] for p in fake.posted()] == ["TSLA"]  # the only one it tried
            assert st["stats"]["skipped"] == 4 and st["stats"]["rejected"] == 1 and st["stats"]["mirrored"] == 0
            # they stay recorded once while NEXUS holds them: no retries, no new orders
            await svc.sync()
            assert len(fake.posted()) == 1
            # the US market closed: a stock position is not mirrored (it would fill at the next open)
            fake.market_open = False
            aapl = await _open(engine, AAPL, qty=1)
            btc = await _open(engine, BTC, qty=0.001)
            await svc.sync()
            st = await svc.overview()
            assert "mercado de EE. UU. cerrado" in _entry(st, aapl)["reason"] and _entry(st, btc)["status"] == "open"
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_a_restart_never_doubles_an_order_and_a_close_waits_for_the_market(runtime_factory, no_background,
                                                                           monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeAlpaca()
            fake.auto_fill = False
            engine, svc = await _setup(rt, monkeypatch, fake)
            aapl = await _open(engine, AAPL, qty=3)
            await svc.sync()
            assert _entry(await svc.overview(), aapl)["status_label"] == "Orden enviada"
            await svc.stop()
            # restart: the working order is found again by its id, then filled
            svc2 = MirrorService(rt)
            await svc2.start()
            await svc2.sync()
            assert len(fake.posted()) == 1 and _entry(await svc2.overview(), aapl)["status"] == "working"
            fake.fill(next(iter(fake.orders)))
            await svc2.sync()
            assert _entry(await svc2.overview(), aapl)["status"] == "open"
            # a crash right after sending (its state not saved): the same client id finds the same order
            await rt.db.execute("DELETE FROM meta WHERE key='broker.mirror'")
            tsla = await _open(engine, TSLA, qty=1)
            svc3 = MirrorService(rt)
            await svc3.start()
            await svc3.sync()  # sends TSLA; AAPL is new to it but its order exists: 422 -> the same order
            cids = [p["client_order_id"] for p in fake.posted()]
            assert cids.count(f"nexus-{aapl}-o1") == 2 and len(fake.orders) == 2  # tried again, never a second order
            assert f"nexus-{tsla}-o1" in cids
            # NEXUS closes AAPL with the US market closed: the mirror waits, then closes when it opens
            fake.auto_fill = True
            fake.fill(fake.by_cid[f"nexus-{tsla}-o1"])
            await svc3.sync()
            fake.market_open = False
            await engine.close_position(position_id=aapl, account_id=3, price=101.0)
            await svc3.sync()
            st = await svc3.overview()
            assert _entry(st, aapl)["status"] == "waiting" and "cierre pendiente" in _entry(st, aapl)["reason"]
            fake.market_open = True
            await svc3.sync()
            assert _entry(await svc3.overview(), aapl)["status"] == "closed"
            assert fake.posted()[-1]["client_order_id"] == f"nexus-{aapl}-c2"
            await svc3.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_off_offline_or_without_keys_it_sends_nothing(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeAlpaca()
            engine, svc = await _setup(rt, monkeypatch, fake, enabled=False)
            await _open(engine, AAPL, qty=1)
            await svc.sync()
            st = await svc.overview()
            assert (st["enabled"], st["connected"], st["entries"], fake.requests) == (False, False, [], [])
            with pytest.raises(MirrorError, match="desactivado"):
                await svc.sync_now()
            await rt.settings.update({"broker.alpaca_enabled": True, "broker.alpaca_secret_key": ""})
            st = await svc.overview()
            assert "Faltan las claves" in st["error"] and fake.requests == []
            with pytest.raises(MirrorError, match="Faltan las claves"):
                await svc.sync_now()
            await rt.settings.update({"broker.alpaca_secret_key": SECRET})
            monkeypatch.setattr(nexus_config, "OFFLINE", True)
            await svc.sync()
            assert "sin conexión" in (await svc.overview())["error"] and fake.requests == []
            with pytest.raises(MirrorError, match="sin conexión"):
                await svc.sync_now()
            # wrong keys: told plainly, without echoing them
            monkeypatch.setattr(nexus_config, "OFFLINE", False)
            monkeypatch.setattr(alpaca_mod, "http_client", lambda timeout=15.0, **kw: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "unauthorized"}))))
            st = await svc.sync_now()
            assert "rechazó las claves" in st["error"] and KEY_ID not in st["error"] and not st["connected"]
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_api_terminal_and_services_table(api_client):
    st = api_client.get("/api/broker/status").json()
    assert (st["enabled"], st["paper"], st["provider"], st["entries"]) == (False, True, "alpaca", [])
    assert st["stats"]["text"] == "Todavía no hay posiciones de NEXUS automática que replicar"
    r = api_client.post("/api/broker/sync")
    assert r.status_code == 400 and "desactivado" in r.json()["detail"]
    res = api_client.post("/api/terminal", json={"text": "espejo"}).json()
    assert res["ok"] and "Espejo desactivado" in "\n".join(line["text"] for line in res["lines"])
    assert api_client.post("/api/terminal", json={"text": "espejo raro"}).json()["ok"] is False
    api_client.put("/api/settings", json={"broker.alpaca_key_id": KEY_ID, "broker.alpaca_secret_key": SECRET})
    health = api_client.get("/api/health").json()
    assert health["services"]["broker"]["enabled"] is False and KEY_ID not in json.dumps(health)
    shown = api_client.post("/api/terminal", json={"text": "settings broker.alpaca_secret_key"}).json()
    assert SECRET not in json.dumps(shown)  # a secret: masked in the terminal
    assert api_client.post("/api/terminal", json={"text": "status"}).json()["ok"]
