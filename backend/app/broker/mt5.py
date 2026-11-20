"""MetaTrader 5 client of the MT5 mirror (app.broker.mt5_mirror): the official `MetaTrader5` Python package, which
attaches to the MT5 terminal running on this Windows machine and to the account the user logged in there.

NEXUS never stores or sends an MT5 password: the user opens MT5 and logs in to whichever broker and account they
want; this client only attaches to that session (`initialize()`, optionally with the terminal's path). The package
is Windows-only and a single global connection, so every call runs in a worker thread under one lock.

`account()` reports the account type (demo / contest / real): the mirror refuses to send anything to a REAL account
unless the user switched that on for that exact account number (app.broker.mt5_mirror)."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable

from app.broker.common import BrokerError

MAGIC = 842003  # identifies NEXUS's orders in the terminal (expert «magic number»)
DEVIATION = 20  # points of slippage accepted on a market order
DEDUP_DAYS = 7  # how far back a client id is looked up before sending (a restart never doubles an order)
RETCODE_DONE = (10008, 10009, 10010)  # placed, done, done partially
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
TRADE_MODE = {0: "demo", 1: "contest", 2: "real"}
TRADE_MODE_ES = {"demo": "demo", "contest": "concurso", "real": "REAL"}
RETCODE_ES = {
    10004: "recotización", 10006: "orden rechazada", 10013: "petición no válida", 10014: "volumen no válido",
    10015: "precio no válido", 10016: "stops no válidos", 10017: "trading desactivado para la cuenta",
    10018: "mercado cerrado", 10019: "margen insuficiente", 10021: "sin precios", 10024: "demasiadas peticiones",
    10026: "el servidor desactivó el trading automático", 10027: "el terminal tiene desactivado el trading algorítmico",
    10030: "modo de ejecución no admitido",
}

_LOCK = threading.Lock()


def load_module() -> Any:
    """The MetaTrader5 package, or a BrokerError (status 0: nothing was sent) when it cannot be used here."""
    try:
        import MetaTrader5  # type: ignore[import-not-found]
    except ImportError:
        raise BrokerError("MetaTrader 5 solo funciona en Windows con el paquete «MetaTrader5» instalado "
                          "(pip install MetaTrader5) y el terminal de MT5 abierto.") from None
    return MetaTrader5


def available() -> bool:
    try:
        load_module()
        return True
    except BrokerError:
        return False


def _asdict(obj: Any) -> dict:
    if obj is None:
        return {}
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())
    if isinstance(obj, dict):
        return dict(obj)
    return dict(vars(obj)) if hasattr(obj, "__dict__") else {}


class Mt5Client:
    """Async wrapper of the MetaTrader5 package (one terminal, one account: the one logged in in MT5)."""

    def __init__(self, path: str = "", module_loader: Callable[[], Any] = load_module) -> None:
        self.path = str(path or "").strip()
        self._loader = module_loader
        self._mt5: Any = None
        self._orders: dict[Any, dict] = {}  # results of the orders sent by this process, by order id

    # ------------------------------------------------------------------ plumbing

    async def _call(self, fn: Callable[[Any], Any]) -> Any:
        def run() -> Any:
            with _LOCK:
                mt5 = self._connect()
                return fn(mt5)

        return await asyncio.to_thread(run)

    def _connect(self) -> Any:
        mt5 = self._mt5 or self._loader()
        if self._mt5 is None or mt5.terminal_info() is None:
            ok = mt5.initialize(path=self.path) if self.path else mt5.initialize()
            if not ok:
                code = mt5.last_error() if hasattr(mt5, "last_error") else None
                raise BrokerError("No se pudo conectar con MetaTrader 5: abre el terminal, inicia sesión en tu cuenta "
                                  f"y deja la ventana abierta ({code}).")
            self._mt5 = mt5
        return mt5

    async def close(self) -> None:
        if self._mt5 is not None:
            mt5, self._mt5 = self._mt5, None
            await asyncio.to_thread(lambda: mt5.shutdown())

    # ------------------------------------------------------------------ account and symbols

    async def account(self) -> dict:
        """The logged-in account: login, server, company, currency, balance, equity, free margin, leverage, its type
        (demo / contest / real) and whether algorithmic trading is allowed (account and terminal). Never the holder's
        name."""
        def read(mt5: Any) -> dict:
            info = _asdict(mt5.account_info())
            if not info:
                raise BrokerError("MetaTrader 5 no tiene una cuenta conectada: inicia sesión en el terminal.")
            term = _asdict(mt5.terminal_info())
            kind = TRADE_MODE.get(int(info.get("trade_mode", 2)), "real")  # unknown → treated as real
            return {
                "login": int(info.get("login") or 0), "server": str(info.get("server") or ""),
                "company": str(info.get("company") or ""), "currency": str(info.get("currency") or "USD"),
                "balance": float(info.get("balance") or 0.0), "equity": float(info.get("equity") or 0.0),
                "margin_free": float(info.get("margin_free") or 0.0), "leverage": int(info.get("leverage") or 0),
                "kind": kind,
                "trade_allowed": bool(info.get("trade_allowed", True)) and bool(info.get("trade_expert", True)),
                "terminal_trade_allowed": bool(term.get("trade_allowed", False)),
                "connected": bool(term.get("connected", False)),
            }

        return await self._call(read)

    async def symbol_names(self) -> list[str]:
        return await self._call(lambda mt5: [s.name for s in (mt5.symbols_get() or [])])

    async def symbol(self, name: str) -> dict | None:
        """Lot rules of a symbol (selected in Market Watch when hidden), or None when the broker does not list it."""
        def read(mt5: Any) -> dict | None:
            info = _asdict(mt5.symbol_info(name))
            if not info:
                return None
            if not info.get("visible", True):
                mt5.symbol_select(name, True)
            return {"name": name, "volume_min": float(info.get("volume_min") or 0.01),
                    "volume_step": float(info.get("volume_step") or 0.01),
                    "volume_max": float(info.get("volume_max") or 0.0),
                    "contract_size": float(info.get("trade_contract_size") or 1.0),
                    "filling_mode": int(info.get("filling_mode") or 0),
                    "trade_mode": int(info.get("trade_mode", 4)),  # 0 = trading disabled for this symbol
                    "digits": int(info.get("digits") or 2)}

        return await self._call(read)

    # ------------------------------------------------------------------ orders

    async def order_by_client_id(self, cid: str) -> dict:
        found = await self._find(cid)
        if found is None:
            raise BrokerError(f"MetaTrader 5 no tiene la orden {cid}", 404)
        return found

    async def order(self, order_id: Any) -> dict:
        """A sent order as {status, filled_qty, filled_avg_price, position}: from this session, or else from the
        terminal's deals of the last DEDUP_DAYS (after a restart)."""
        hit = self._orders.get(order_id)
        if hit is not None:
            return hit

        def read(mt5: Any) -> dict | None:
            now = time.time()
            deals = [d for d in (mt5.history_deals_get(now - DEDUP_DAYS * 86400, now + 86400) or [])
                     if int(getattr(d, "order", 0) or 0) == int(order_id)]
            if not deals:
                return None
            vol = sum(float(d.volume) for d in deals)
            return {"id": int(order_id), "status": "filled", "filled_qty": vol,
                    "filled_avg_price": sum(float(d.price) * float(d.volume) for d in deals) / vol if vol else None,
                    "position": int(getattr(deals[0], "position_id", 0) or 0)}

        hit = await self._call(read)
        if hit is None:  # never filled: a market order that is not in the history was not executed
            return {"id": order_id, "status": "rejected", "filled_qty": 0.0, "filled_avg_price": None}
        self._orders[order_id] = hit
        return hit

    async def _find(self, cid: str) -> dict | None:
        """A deal of NEXUS with this client id in the last DEDUP_DAYS (the comment carries it), as a filled order."""
        def read(mt5: Any) -> dict | None:
            now = time.time()
            deals = mt5.history_deals_get(now - DEDUP_DAYS * 86400, now + 86400) or []
            mine = [d for d in deals if getattr(d, "magic", None) == MAGIC and getattr(d, "comment", "") == cid]
            if not mine:
                return None
            vol = sum(float(d.volume) for d in mine)
            price = sum(float(d.price) * float(d.volume) for d in mine) / vol if vol else None
            return {"id": int(mine[0].order), "status": "filled", "filled_qty": vol, "filled_avg_price": price,
                    "position": int(getattr(mine[0], "position_id", 0) or 0)}

        hit = await self._call(read)
        if hit is not None:
            self._orders[hit["id"]] = hit
        return hit

    async def send_market(self, *, symbol: str, buy: bool, volume: float, client_order_id: str,
                          position: int | None = None) -> dict:
        """A market order (BUY / SELL) for `volume` lots; `position` closes or reduces that position (hedging
        accounts). A client id already sent is never sent again: its first fill is returned."""
        existing = await self._find(client_order_id)
        if existing is not None:
            return existing

        def send(mt5: Any) -> dict:
            info = _asdict(mt5.symbol_info(symbol))
            tick = _asdict(mt5.symbol_info_tick(symbol))
            price = tick.get("ask") if buy else tick.get("bid")
            if not info or not price:
                raise BrokerError(f"MetaTrader 5 no tiene precio de {symbol} ahora (¿mercado cerrado?)", 400)
            filling = int(info.get("filling_mode") or 0)
            req: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
                "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": float(price),
                "deviation": DEVIATION, "magic": MAGIC, "comment": client_order_id[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": (mt5.ORDER_FILLING_FOK if filling & SYMBOL_FILLING_FOK
                                 else mt5.ORDER_FILLING_IOC if filling & SYMBOL_FILLING_IOC
                                 else mt5.ORDER_FILLING_RETURN),
            }
            if position:
                req["position"] = int(position)
            res = mt5.order_send(req)
            if res is None:
                raise BrokerError(f"MetaTrader 5 no respondió a la orden ({mt5.last_error()})")  # status 0: retry
            data = _asdict(res)
            code = int(data.get("retcode") or 0)
            if code not in RETCODE_DONE:
                why = RETCODE_ES.get(code, str(data.get("comment") or "sin detalle"))
                raise BrokerError(f"MetaTrader 5 rechazó la orden de {symbol}: {why} (código {code})", 400)
            pos_id = int(position or 0)
            deal = int(data.get("deal") or 0)
            if not pos_id and deal:
                deals = mt5.history_deals_get(ticket=deal) or []
                pos_id = int(getattr(deals[0], "position_id", 0) or 0) if deals else 0
            return {"id": int(data.get("order") or deal), "status": "filled",
                    "filled_qty": float(data.get("volume") or volume), "filled_avg_price": float(data.get("price") or price),
                    "position": pos_id or int(data.get("order") or 0)}

        out = await self._call(send)
        self._orders[out["id"]] = out
        return out
