"""Mirror of NEXUS automática on Binance's TESTNETS (app.broker.binance_mirror) against a fake testnet (httpx
MockTransport: no network). Only the two testnet URLs, each with its own key and a valid HMAC signature; spot crypto
long only on the spot testnet, perpetuals long and short on the futures testnet with isolated margin, NEXUS's leverage
and reduce-only closes; quantities on the lot step and the minimums respected; idempotent client ids across a lost
state; what is not mirrored and why; never a secret in status, events or the terminal; nothing at all while it is
off, offline or without the keys of a market. Plus its API and terminal command."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from urllib.parse import parse_qsl

import httpx
import pytest

from app import config as nexus_config
from app.broker import binance as binance_mod
from app.broker.binance import floor_step, qty_text
from app.broker.binance_mirror import BinanceMirrorService, binance_symbol
from app.broker.mirror import MirrorError
from tests.test_imp_autopilot import BarNexus, _engine, no_background  # noqa: F401 (fixture)
from tests.test_sim_fakes import make_current

BTC, BTC_P, XRP_P, ETH, AAPL = "BINANCE:BTCUSDT", "BINANCE:BTCUSDT.P", "BINANCE:XRPUSDT.P", "BINANCE:ETHUSDT", "NASDAQ:AAPL"
SPOT_KEY, SPOT_SECRET = "spotKEY123testnet", "spot-secreto-456"
FUT_KEY, FUT_SECRET = "futKEY789testnet", "fut-secreto-012"
DOGE = "BINANCE:DOGEUSDT"  # not listed by the fake spot testnet
PRICES = {BTC: 50_000.0, BTC_P: 50_000.0, XRP_P: 1.0, ETH: 2_000.0, AAPL: 100.0, DOGE: 0.2}
SPOT_HOST, FUT_HOST = "testnet.binance.vision", "demo-fapi.binance.com"  # spot testnet · Demo Trading futures
LOTS = {  # symbol -> (step, min qty, min notional)
    ("spot", "BTCUSDT"): (0.00001, 0.00001, 5.0), ("spot", "ETHUSDT"): (0.0001, 0.0001, 5.0),
    ("perp", "BTCUSDT"): (0.001, 0.001, 100.0), ("perp", "XRPUSDT"): (0.1, 0.1, 5.0),
}


class FakeBinance:
    """Both testnets, scripted: market orders fill at once at `price`, the spot account follows its fills (keeping
    `spot_fee` of the coins bought), and every signed request must carry the right key and a valid signature."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.orders: dict[tuple[str, str], dict] = {}  # (market, client id) -> order
        self.balances = {"USDT": 10_000.0}
        self.positions: dict[str, float] = {}
        self.price = {("spot", "BTCUSDT"): 50_010.0, ("spot", "ETHUSDT"): 2_000.0,
                      ("perp", "BTCUSDT"): 49_990.0, ("perp", "XRPUSDT"): 1.0}
        self.margin_set: set[str] = set()
        self.leverage: dict[str, int] = {}
        self.spot_fee = 0.0

    @staticmethod
    def _err(code: int, msg: str, status: int = 400) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "msg": msg})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        assert request.url.scheme == "https" and host in (SPOT_HOST, FUT_HOST), request.url
        market = "spot" if host == SPOT_HOST else "perp"
        raw = request.url.query.decode()
        params = dict(parse_qsl(raw))
        if "exchangeInfo" in path:
            return self._info(market, params.get("symbol"))
        key, secret = (SPOT_KEY, SPOT_SECRET) if market == "spot" else (FUT_KEY, FUT_SECRET)
        assert request.headers["X-MBX-APIKEY"] == key
        text, _, sig = raw.rpartition("&signature=")
        assert sig == hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()
        assert "timestamp" in params and request.content == b""  # signed params in the query, nothing in the body
        if path == "/api/v3/account":
            return httpx.Response(200, json={"canTrade": True, "accountType": "SPOT", "uid": 999111,
                                             "balances": [{"asset": a, "free": str(q), "locked": "0"}
                                                          for a, q in self.balances.items()]})
        if path == "/fapi/v2/account":
            return httpx.Response(200, json={"totalMarginBalance": "15000.5", "availableBalance": "14000.25"})
        if path == "/fapi/v1/marginType":
            if params["symbol"] in self.margin_set:
                return self._err(-4046, "No need to change margin type.")
            self.margin_set.add(params["symbol"])
            return httpx.Response(200, json={"code": 200, "msg": "success"})
        if path == "/fapi/v1/leverage":
            self.leverage[params["symbol"]] = int(params["leverage"])
            return httpx.Response(200, json={"symbol": params["symbol"], "leverage": int(params["leverage"])})
        if path in ("/api/v3/order", "/fapi/v1/order"):
            if request.method == "GET":
                return self._get(market, params)
            return self._post(market, params)
        return self._err(-1000, "no existe", 404)

    def _info(self, market: str, symbol: str | None) -> httpx.Response:
        rows = []
        for (m, sym), (step, min_qty, min_notional) in LOTS.items():
            if m != market or (symbol and sym != symbol):
                continue
            notional = ({"filterType": "NOTIONAL", "minNotional": str(min_notional)} if market == "spot"
                        else {"filterType": "MIN_NOTIONAL", "notional": str(min_notional)})
            rows.append({"symbol": sym, "status": "TRADING", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": f"{step:.8f}", "minQty": f"{min_qty:.8f}"}, notional]})
        if symbol and not rows:
            return self._err(-1121, "Invalid symbol.")
        return httpx.Response(200, json={"symbols": rows})

    def _get(self, market: str, params: dict) -> httpx.Response:
        cid = params.get("origClientOrderId")
        for (m, c), o in self.orders.items():
            if m == market and o["symbol"] == params["symbol"] and (c == cid or str(o["orderId"]) == params.get("orderId")):
                return httpx.Response(200, json=o)
        return self._err(-2013, "Order does not exist.")

    def _post(self, market: str, params: dict) -> httpx.Response:
        sym, side, qty = params["symbol"], params["side"], float(params["quantity"])
        step, min_qty, min_notional = LOTS[(market, sym)]
        assert abs(qty / step - round(qty / step)) < 1e-6, f"{qty} is not on the lot step {step}"
        assert params["type"] == "MARKET" and qty >= min_qty
        price = self.price[(market, sym)]
        assert qty * price >= min_notional
        oid = len(self.orders) + 1
        order = {"symbol": sym, "orderId": oid, "clientOrderId": params["newClientOrderId"], "status": "FILLED",
                 "executedQty": params["quantity"], "side": side, "reduceOnly": params.get("reduceOnly") == "true"}
        if market == "perp":
            order["avgPrice"] = str(price)
            self.positions[sym] = round(self.positions.get(sym, 0.0) + (qty if side == "BUY" else -qty), 8)
        else:
            order["cummulativeQuoteQty"] = str(qty * price)
            base = sym[:-4]
            got = qty * (1.0 - self.spot_fee) if side == "BUY" else -qty
            self.balances[base] = round(self.balances.get(base, 0.0) + got, 8)
            self.balances["USDT"] -= qty * price if side == "BUY" else -qty * price
        self.orders[(market, params["newClientOrderId"])] = order
        return httpx.Response(200, json=order)

    def posted(self, market: str | None = None) -> list[dict]:
        out = []
        for r in self.requests:
            if r.method != "POST" or not r.url.path.endswith("/order"):
                continue
            if market and (r.url.host == SPOT_HOST) != (market == "spot"):
                continue
            p = dict(parse_qsl(r.url.query.decode()))
            out.append({k: v for k, v in p.items() if k not in ("timestamp", "recvWindow", "signature")})
        return out

    def install(self, monkeypatch) -> None:
        def factory(timeout: float = 15.0, **kw):
            return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), headers=kw.pop("headers", None) or {},
                                     timeout=timeout)

        monkeypatch.setattr(binance_mod, "http_client", factory)


async def _setup(rt, monkeypatch, fake: FakeBinance, *, enabled: bool = True, futures: bool = True,
                 spot: bool = True):
    monkeypatch.setattr(nexus_config, "OFFLINE", False)  # the mirror never sends anything offline
    fake.install(monkeypatch)
    engine = await _engine(rt, dict(PRICES), BarNexus(default=make_current("WAIT")))
    rt.paper = engine
    await rt.settings.update({"paper.max_open_risk_pct": 0, "paper.max_positions_per_group": 0,
                              "broker.binance_enabled": enabled,
                              "broker.binance_spot_key": SPOT_KEY if spot else "",
                              "broker.binance_spot_secret": SPOT_SECRET if spot else "",
                              "broker.binance_futures_key": FUT_KEY if futures else "",
                              "broker.binance_futures_secret": FUT_SECRET if futures else "",
                              "autopilot.perp_leverage": 10})
    svc = BinanceMirrorService(rt)
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


def test_symbols_the_testnets_can_trade_and_why_the_others_are_not_mirrored():
    assert binance_symbol(BTC) == ("BTCUSDT", "crypto", "")
    assert binance_symbol("COINBASE:ETHUSD") == ("ETHUSDT", "crypto", "")
    assert binance_symbol("BINANCE:SOLUSDC") == ("SOLUSDC", "crypto", "")
    assert binance_symbol(BTC_P) == ("BTCUSDT", "perp", "")
    assert binance_symbol("BYBIT:XRPUSDT.P") == ("XRPUSDT", "perp", "")
    for symbol, why in ((AAPL, "«stock»"), ("CME_MINI:MES1!", "«futures»"), ("CAPITALCOM:AAPL", "«cfd»"),
                        ("OPT:NASDAQ:AAPL:C:200:20261218", "opción teórica"), ("SIM:NEXUS", "sintético"),
                        ("BINANCE:ETHBTC", "dólares"), ("DERIBIT:BTCUSD25Z2026", "vencimiento"),
                        ("BINANCE:BTCEUR.P", "dólares")):
        sym, kind, reason = binance_symbol(symbol)
        assert sym is None and kind == "" and why in reason, (symbol, reason)
    assert "desactivado" in binance_symbol(BTC, spot=False)[2] and "desactivado" in binance_symbol(BTC_P, perps=False)[2]
    # the base URLs are constants: no setting can point the mirror at a live exchange
    assert binance_mod.TESTNET_SPOT_URL == f"https://{SPOT_HOST}"
    assert binance_mod.TESTNET_FUTURES_URL == f"https://{FUT_HOST}"
    assert qty_text(0.0123) == "0.0123" and qty_text(85.0) == "85" and floor_step(0.0123456, 0.00001) == 0.01234


def test_spot_long_and_perpetuals_both_ways_follow_nexus(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeBinance()
            fake.spot_fee = 0.001
            engine, svc = await _setup(rt, monkeypatch, fake)
            spot = await _open(engine, BTC, qty=0.0123456)  # off the lot step: rounded down to 0.01234
            perp = await _open(engine, BTC_P, "short", qty=0.012)
            await svc.sync()
            assert fake.posted("spot") == [{"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                                            "quantity": "0.01234", "newClientOrderId": f"nexus-{spot}-o1",
                                            "newOrderRespType": "FULL"}]
            assert fake.posted("perp") == [{"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.012",
                                            "newClientOrderId": f"nexus-{perp}-o1", "newOrderRespType": "RESULT"}]
            # isolated margin and NEXUS's leverage before the first futures order
            assert fake.margin_set == {"BTCUSDT"} and fake.leverage == {"BTCUSDT": 10}
            st = await svc.overview()
            s, p = _entry(st, spot), _entry(st, perp)
            assert (s["status"], s["status_label"], s["alpaca_symbol"], s["held"]) == (
                "open", "Abierta en Binance Testnet", "BTCUSDT", 0.01234)
            assert s["slip_entry_bps"] == pytest.approx(2.0)  # bought 10 above NEXUS's 50 000: worse
            assert p["slip_entry_bps"] == pytest.approx(2.0)  # the short sold 10 below NEXUS's price: worse too
            assert (st["provider"], st["paper"], st["mirror_crypto"], st["mirror_perps"], st["mirror_stocks"]) == (
                "binance_testnet", True, True, True, False)
            assert st["connected"] and st["account"] == {"equity": 15000.5, "cash": pytest.approx(10_000 - 0.01234 * 50_010),
                                                         "buying_power": 14000.25, "status": "ACTIVE",
                                                         "currency": "USDT"}
            # NEXUS closes both: the perpetual with a reduce-only buy, the spot with what the wallet holds (after fee)
            await engine.close_position(position_id=perp, account_id=3, price=49_000.0)
            await engine.close_position(position_id=spot, account_id=3, price=51_000.0)
            await svc.sync()
            assert fake.posted("perp")[-1] == {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.012",
                                               "newClientOrderId": f"nexus-{perp}-c2", "newOrderRespType": "RESULT",
                                               "reduceOnly": "true"}
            assert fake.posted("spot")[-1]["side"] == "SELL"
            assert fake.posted("spot")[-1]["quantity"] == "0.01232"  # 0.01234 less its 0,1 % fee, on the step
            assert fake.positions == {"BTCUSDT": 0.0} and len(fake.leverage) == 1  # no leverage call to close
            st = await svc.overview()
            assert {_entry(st, spot)["status"], _entry(st, perp)["status"]} == {"closed"}
            assert st["stats"]["text"].startswith("2 de 2 posiciones replicadas")
            # no secret anywhere it shows or logs
            events = json.dumps([e for e in rt.bus.recent(200)], ensure_ascii=False, default=str)
            for secret in (SPOT_KEY, SPOT_SECRET, FUT_KEY, FUT_SECRET, "999111"):
                assert secret not in json.dumps(st) and secret not in events and secret not in json.dumps(svc.status())
            assert any("Espejo en Binance Testnet: abre BTCUSDT" in e["message"]
                       for e in rt.bus.recent(200, source="broker"))
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_what_is_not_mirrored_says_why(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeBinance()
            engine, svc = await _setup(rt, monkeypatch, fake)
            short_spot = await _open(engine, BTC, "short", qty=0.01)
            stock = await _open(engine, AAPL, qty=1.0)
            tiny = await _open(engine, XRP_P, qty=1.0)  # 1 $ of notional: under the testnet's 5 $
            unlisted = await _open(engine, DOGE, qty=100.0)
            await svc.sync()
            assert fake.posted() == []
            st = await svc.overview()
            assert "al contado no hay cortos" in _entry(st, short_spot)["reason"]
            assert "«stock»" in _entry(st, stock)["reason"]
            assert "por debajo del mínimo del testnet (5 $)" in _entry(st, tiny)["reason"]
            assert "el testnet de contado de Binance no lista DOGEUSDT" in _entry(st, unlisted)["reason"]
            assert {e["status"] for e in st["entries"]} == {"skipped"}
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_a_lost_state_never_doubles_an_order(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeBinance()
            engine, svc = await _setup(rt, monkeypatch, fake)
            perp = await _open(engine, BTC_P, qty=0.005)
            await svc.sync()
            assert len(fake.posted("perp")) == 1
            await svc.stop()
            # the server stops before its state is saved: a new mirror finds the same order by its client id
            await rt.db.execute("DELETE FROM meta WHERE key='broker.binance_mirror'")
            again = BinanceMirrorService(rt)
            await again.start()
            await again.sync()
            assert len(fake.posted("perp")) == 1
            assert _entry(await again.overview(), perp)["status"] == "open"
            await again.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_off_offline_or_without_the_keys_of_a_market_it_sends_nothing(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            fake = FakeBinance()
            engine, svc = await _setup(rt, monkeypatch, fake, enabled=False)
            await _open(engine, BTC_P, qty=0.005)
            await svc.sync()
            assert fake.requests == []
            with pytest.raises(MirrorError, match="desactivado"):
                await svc.sync_now()
            # on, with only the spot keys: the perpetual waits for its keys, the spot pair is bought
            await rt.settings.update({"broker.binance_enabled": True, "broker.binance_futures_key": "",
                                      "broker.binance_futures_secret": ""})
            spot = await _open(engine, ETH, qty=0.5)
            await svc.sync()
            st = await svc.overview()
            assert [e["status"] for e in st["entries"] if e["pos_id"] == spot] == ["open"]
            assert any("faltan las claves de Demo Trading (futuros)" in (e["reason"] or "") for e in st["entries"])
            assert all(r.url.host == SPOT_HOST for r in fake.requests)
            # offline: nothing at all
            monkeypatch.setattr(nexus_config, "OFFLINE", True)
            before = len(fake.requests)
            await _open(engine, BTC, qty=0.001)
            await svc.sync()
            assert len(fake.requests) == before
            with pytest.raises(MirrorError, match="sin conexión"):
                await svc.sync_now()
            await svc.stop()
            await engine.stop()

    asyncio.run(scenario())


def test_api_and_terminal(api_client):
    st = api_client.get("/api/broker/binance/status").json()
    assert (st["enabled"], st["paper"], st["provider"], st["entries"]) == (False, True, "binance_testnet", [])
    r = api_client.post("/api/broker/binance/sync")
    assert r.status_code == 400 and "Testnet de Binance" in r.json()["detail"]
    res = api_client.post("/api/terminal", json={"text": "espejo binance"}).json()
    assert res["ok"] and "Espejo en el testnet de Binance desactivado" in "\n".join(l["text"] for l in res["lines"])
    assert api_client.post("/api/terminal", json={"text": "espejo binance raro"}).json()["ok"] is False
    assert api_client.get("/api/broker/status").json()["provider"] == "alpaca"  # Alpaca's mirror is unchanged
    api_client.put("/api/settings", json={"broker.binance_spot_key": SPOT_KEY, "broker.binance_spot_secret": SPOT_SECRET,
                                          "broker.binance_futures_key": FUT_KEY,
                                          "broker.binance_futures_secret": FUT_SECRET})
    health = api_client.get("/api/health").json()
    assert health["services"]["broker_binance"]["enabled"] is False
    for secret in (SPOT_KEY, SPOT_SECRET, FUT_KEY, FUT_SECRET):
        assert secret not in json.dumps(health)
        shown = api_client.post("/api/terminal", json={"text": "settings broker.binance_futures_secret"}).json()
        assert secret not in json.dumps(shown)
