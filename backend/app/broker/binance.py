"""Client of Binance's PRACTICE environments (test funds, never money): the spot testnet at TESTNET_SPOT_URL and
USDⓈ-M futures at TESTNET_FUTURES_URL, Binance's Demo Trading (its documented futures testnet REST base since the old
testnet.binancefuture.com moved to demo.binance.com; keys from demo.binance.com → API Management).

Both base URLs are constants and not settings, so no key can send an order to a live account (the testnet and Demo
Trading keys are their own, not valid on the live exchange, and the other way round). Signed requests (HMAC-SHA256 of
the query string, as Binance documents) through app.util.http_client (CLAUDE.md rule 4) with the user's key in the
official X-MBX-APIKEY header, never following redirects (the keys stay on the testnet). The keys never appear in
errors, events or status. Market orders only: what NEXUS automática simulated, sent as is.

Binance only refuses a repeated client order id while that order is still open, so `submit_market` first asks for
an order with that id and returns it when it exists: a restart never doubles an order. Orders come back normalized to
the mirror's shape: {id, status (lowercase: new | partially_filled | filled | canceled | rejected | expired),
filled_qty, filled_avg_price}, with an `id` "<spot|perp>:<SYMBOL>:<orderId>" that `order()` can query again.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.broker.common import BrokerError
from app.util import http_client, safe_float

TESTNET_SPOT_URL = "https://testnet.binance.vision"
TESTNET_FUTURES_URL = "https://demo-fapi.binance.com"  # Demo Trading: USDⓈ-M futures with test funds
TIMEOUT_S = 15.0
RECV_WINDOW_MS = 10_000
AUTH_CODES = (-1022, -2014, -2015)  # bad signature, bad key format, invalid key / IP / permissions
UNKNOWN_ORDER = -2013  # "Order does not exist."
NO_CHANGE = (-4046, -4048)  # margin type already set / cannot change with an open position
STATUS = {"NEW": "new", "PARTIALLY_FILLED": "partially_filled", "FILLED": "filled", "CANCELED": "canceled",
          "PENDING_CANCEL": "canceled", "REJECTED": "rejected", "EXPIRED": "expired", "EXPIRED_IN_MATCH": "expired"}
MARKET_LABEL = {"spot": "contado", "perp": "futuros"}
# where each market's keys come from, in the messages: the spot testnet and Demo Trading (futures)
VENUE_LABEL = {"spot": "el testnet de contado", "perp": "Demo Trading (futuros)"}
KEYS_FROM = {"spot": "testnet.binance.vision", "perp": "demo.binance.com → API Management"}


class BinanceError(BrokerError):
    """A Spanish message for the user; `status` is the HTTP status (0 = no answer, 401 = the keys) and `code` the
    Binance error code when it sent one."""

    def __init__(self, message: str, status: int = 0, code: int | None = None):
        super().__init__(message, status)
        self.code = code


def qty_text(qty: float) -> str:
    """Binance takes quantities as plain decimals: up to 8 places, no trailing zeros ("0.012", "85")."""
    text = f"{float(qty):.8f}".rstrip("0").rstrip(".")
    return text or "0"


def floor_step(qty: float, step: float | None) -> float:
    """`qty` rounded down to a multiple of the exchange's lot step (no step: as is)."""
    if not step or step <= 0:
        return float(qty)
    return round(math.floor(float(qty) / step + 1e-9) * step, 8)


def _filters(info: dict) -> dict[str, float | None]:
    """{step, min_qty, min_notional} of an exchangeInfo symbol (spot: LOT_SIZE + NOTIONAL / MIN_NOTIONAL; futures:
    MARKET_LOT_SIZE or LOT_SIZE + MIN_NOTIONAL)."""
    by_type = {f.get("filterType"): f for f in info.get("filters") or [] if isinstance(f, dict)}
    lot = by_type.get("MARKET_LOT_SIZE") or by_type.get("LOT_SIZE") or {}
    notional = by_type.get("NOTIONAL") or by_type.get("MIN_NOTIONAL") or {}
    step = safe_float(lot.get("stepSize"))
    return {"step": step if step and step > 0 else None, "min_qty": safe_float(lot.get("minQty")),
            "min_notional": safe_float(notional.get("minNotional") or notional.get("notional"))}


class BinanceTestnetClient:
    """One client for both testnets: each market needs its own keys (the spot and futures testnets issue separate
    ones). A market without keys answers BinanceError(401) before sending anything."""

    def __init__(self, spot_key: str = "", spot_secret: str = "", futures_key: str = "", futures_secret: str = "",
                 timeout: float = TIMEOUT_S) -> None:
        self._keys = {"spot": (str(spot_key or ""), str(spot_secret or "")),
                      "perp": (str(futures_key or ""), str(futures_secret or ""))}
        self.timeout = float(timeout)

    def has(self, market: str) -> bool:
        key, secret = self._keys.get(market, ("", ""))
        return bool(key and secret)

    async def _request(self, market: str, method: str, path: str, params: dict | None = None, *,
                       signed: bool = True) -> Any:
        base = TESTNET_SPOT_URL if market == "spot" else TESTNET_FUTURES_URL
        key, secret = self._keys[market]
        headers = {"Accept": "application/json"}
        query = dict(params or {})
        if signed:
            if not (key and secret):
                raise BinanceError(f"Faltan las claves de {VENUE_LABEL[market]} de Binance.", 401)
            query.update({"timestamp": int(time.time() * 1000), "recvWindow": RECV_WINDOW_MS})
            text = urlencode(query)
            text += "&signature=" + hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = key
        else:
            text = urlencode(query)
        url = base + path + (f"?{text}" if text else "")
        try:
            async with http_client(timeout=self.timeout, headers=headers) as client:
                r = await client.request(method, url, follow_redirects=False)
        except httpx.TimeoutException:
            raise BinanceError("Binance Testnet no respondió a tiempo.") from None
        except httpx.HTTPError as exc:
            raise BinanceError(f"No se pudo conectar con Binance Testnet ({type(exc).__name__}).") from None
        data: Any = None
        try:
            data = r.json() if r.content else None
        except ValueError:
            if r.status_code < 300:
                raise BinanceError("Binance Testnet respondió algo que no es JSON.", r.status_code) from None
        if r.status_code >= 300:
            code = int(data["code"]) if isinstance(data, dict) and isinstance(data.get("code"), int) else None
            detail = str(data.get("msg") or "")[:200] if isinstance(data, dict) else ""
            if r.status_code == 401 or code in AUTH_CODES:
                raise BinanceError(f"Binance rechazó las claves de {MARKET_LABEL[market]}: usa las de {VENUE_LABEL[market]} "
                                   f"({KEYS_FROM[market]}), no las de una cuenta real, y comprueba sus permisos.", 401,
                                   code)
            raise BinanceError(f"Binance Testnet respondió {r.status_code}" + (f" ({code})" if code is not None else "")
                               + (f": {detail}" if detail else "") + ".", r.status_code, code)
        return data

    # ------------------------------------------------------------------ market data

    async def filters(self, market: str, symbol: str) -> dict[str, float | None] | None:
        """Lot step, minimum quantity and minimum notional of `symbol` on that testnet; None when it does not list
        it."""
        path = "/api/v3/exchangeInfo" if market == "spot" else "/fapi/v1/exchangeInfo"
        try:
            data = await self._request(market, "GET", path, {"symbol": symbol} if market == "spot" else None,
                                       signed=False)
        except BinanceError as exc:
            if exc.code == -1121 or exc.status == 400:  # invalid symbol
                return None
            raise
        for info in (data or {}).get("symbols") or []:
            if isinstance(info, dict) and info.get("symbol") == symbol:
                status = str(info.get("status") or "TRADING")
                return _filters(info) if status == "TRADING" else None
        return None

    # ------------------------------------------------------------------ account

    async def account(self) -> dict:
        """The practice account, normalized: futures margin balance as equity (USDT), spot USDT as cash and the
        futures available balance as buying power (each market only when it has keys)."""
        out: dict[str, Any] = {"equity": None, "cash": None, "buying_power": None, "status": "ACTIVE",
                               "currency": "USDT"}
        if self.has("perp"):
            fut = await self._request("perp", "GET", "/fapi/v2/account") or {}
            out["equity"] = safe_float(fut.get("totalMarginBalance"))
            out["buying_power"] = safe_float(fut.get("availableBalance"))
        if self.has("spot"):
            spot = await self._request("spot", "GET", "/api/v3/account", {"omitZeroBalances": "true"}) or {}
            usdt = next((b for b in spot.get("balances") or [] if b.get("asset") == "USDT"), {})
            out["cash"] = (safe_float(usdt.get("free")) or 0.0) + (safe_float(usdt.get("locked")) or 0.0)
            if out["equity"] is None:
                out["equity"] = out["cash"]
            if spot.get("canTrade") is False:
                out["status"] = "SIN PERMISO DE OPERAR"
        return out

    async def clock(self) -> dict:
        return {"is_open": True}  # crypto trades 24/7

    async def positions(self) -> list[dict]:
        """What the spot account holds, by asset: [{"symbol": "BTC", "qty": free}] (the mirror never sells more)."""
        if not self.has("spot"):
            return []
        spot = await self._request("spot", "GET", "/api/v3/account", {"omitZeroBalances": "true"}) or {}
        return [{"symbol": str(b.get("asset") or ""), "qty": safe_float(b.get("free"))}
                for b in spot.get("balances") or [] if isinstance(b, dict)]

    async def prepare_perp(self, symbol: str, leverage: int) -> None:
        """Isolated margin and NEXUS's leverage on `symbol` before its first order (already set: nothing to do)."""
        try:
            await self._request("perp", "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        except BinanceError as exc:
            if exc.code not in NO_CHANGE:
                raise
        await self._request("perp", "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    # ------------------------------------------------------------------ orders

    @staticmethod
    def _normalize(market: str, symbol: str, data: Any) -> dict:
        d = data if isinstance(data, dict) else {}
        filled = safe_float(d.get("executedQty")) or 0.0
        if market == "perp":
            price = safe_float(d.get("avgPrice"))
        else:
            quote = safe_float(d.get("cummulativeQuoteQty"))
            price = quote / filled if quote and filled > 0 else None
        return {"id": f"{market}:{symbol}:{d.get('orderId')}", "client_order_id": d.get("clientOrderId"),
                "status": STATUS.get(str(d.get("status") or "").upper(), str(d.get("status") or "new").lower()),
                "filled_qty": filled, "filled_avg_price": price if price and price > 0 else None}

    async def _get(self, market: str, symbol: str, **ref: Any) -> dict | None:
        path = "/api/v3/order" if market == "spot" else "/fapi/v1/order"
        try:
            data = await self._request(market, "GET", path, {"symbol": symbol, **ref})
        except BinanceError as exc:
            if exc.code == UNKNOWN_ORDER:
                return None
            raise
        return self._normalize(market, symbol, data)

    async def submit_market(self, *, market: str, symbol: str, side: str, qty: float, client_order_id: str,
                            reduce_only: bool = False) -> dict:
        """A market order (side buy | sell), or the one already sent with this client id."""
        found = await self._get(market, symbol, origClientOrderId=client_order_id)
        if found is not None:
            return found
        params: dict[str, Any] = {"symbol": symbol, "side": side.upper(), "type": "MARKET", "quantity": qty_text(qty),
                                  "newClientOrderId": client_order_id}
        if market == "perp":
            params["newOrderRespType"] = "RESULT"
            if reduce_only:
                params["reduceOnly"] = "true"
            path = "/fapi/v1/order"
        else:
            params["newOrderRespType"] = "FULL"
            path = "/api/v3/order"
        return self._normalize(market, symbol, await self._request(market, "POST", path, params))

    async def order(self, order_id: str) -> dict:
        market, symbol, oid = (str(order_id).split(":", 2) + ["", ""])[:3]
        if market not in ("spot", "perp") or not symbol or not oid:
            raise BinanceError("Identificador de orden no válido.", 400)
        found = await self._get(market, symbol, orderId=oid)
        if found is None:
            raise BinanceError("Binance Testnet ya no tiene esa orden.", 400, UNKNOWN_ORDER)
        return found
