"""Mirror of NEXUS automática on Binance's TESTNETS (`rt.broker_binance`): practice exchanges with test funds.

The same reconcile as the Alpaca mirror (app.broker.mirror.MirrorService: every SYNC_S seconds, deterministic client
order ids, partial exits and closes that follow NEXUS, the price difference with the simulation as «x de n»), with
Binance's rules:

- spot crypto pairs quoted in dollars (BTCUSDT…, also from another exchange's symbol such as COINBASE:ETHUSD) are
  bought on the spot testnet, long only (a spot wallet cannot sell short), and sold when NEXUS sells;
- crypto perpetuals (`BINANCE:BTCUSDT.P`…) go to USDⓈ-M futures on Binance's Demo Trading (its futures testnet),
  long and short, on isolated margin with NEXUS's leverage (autopilot.perp_leverage); reductions and closes are
  reduce-only orders;
- each quantity is rounded down to the testnet's lot step, and an order under its minimum quantity or notional is
  not sent (the reason is recorded); a symbol the testnet does not list is not mirrored;
- stocks, ETFs, CFDs, index futures, theoretical options, futures with an expiry and synthetic markets are not
  mirrored (the testnets only have crypto).

Off by default; it needs the user's practice keys (the spot testnet, testnet.binance.vision, and Demo Trading,
demo.binance.com, issue separate ones; a market without its keys is skipped with the reason) and only talks to app.broker.binance.TESTNET_SPOT_URL / TESTNET_FUTURES_URL,
constants, never settings. Nothing is mirrored in NEXUS_OFFLINE mode. State in table `meta` (key
broker.binance_mirror), no schema change.
"""
from __future__ import annotations

import time
from typing import Any

from app import config
from app.broker.binance import BinanceTestnetClient, floor_step
from app.broker.mirror import QTY_EPS, MirrorService
from app.data.symbols import is_sim, split_pair, strip_derivative
from app.engine.costs import asset_class

STATE_KEY = "broker.binance_mirror"
SPOT_QUOTES = ("USDT", "USDC", "USD", "FDUSD")  # USD and FDUSD are bought against USDT on the testnet
FILTERS_TTL_S = 3600.0
NOTE = ("Testnet de Binance y Demo Trading (práctica): órdenes reales en entornos de pruebas con fondos de prueba, "
        "nunca dinero real. NEXUS automática decide y simula; el espejo solo repite sus posiciones de cripto al contado y "
        "perpetuos para medir la diferencia de precio que la simulación no ve. El libro de órdenes del testnet es "
        "más fino que el real: sus precios pueden alejarse del mercado. Con menos de 30 operaciones, sin "
        "conclusiones. No es asesoría financiera.")


def binance_symbol(symbol: str, *, spot: bool = True, perps: bool = True) -> tuple[str | None, str, str]:
    """(testnet symbol, kind "crypto" (spot) | "perp", "") or (None, "", why it is not mirrored)."""
    sym = str(symbol or "").strip().upper()
    if not sym or is_sim(sym):
        return None, "", "mercado sintético: no existe en Binance"
    cls = asset_class(sym)
    if cls == "option":
        return None, "", "opción teórica de NEXUS automática: no se replica"
    if cls != "crypto":
        return None, "", f"clase de activo «{cls}»: el testnet de Binance solo tiene cripto"
    ticker = sym.partition(":")[2] or sym
    base, derivative = strip_derivative(ticker)
    pair = split_pair(base)
    if derivative:
        if not ticker.endswith(".P"):
            return None, "", "futuro con vencimiento: el espejo solo replica perpetuos"
        if not perps:
            return None, "", "replicar perpetuos está desactivado"
        if not pair or pair[1] not in ("USDT", "USDC", "USD"):
            return None, "", "perpetuo que no cotiza en dólares"
        return f"{pair[0]}{'USDC' if pair[1] == 'USDC' else 'USDT'}", "perp", ""
    if not spot:
        return None, "", "replicar cripto al contado está desactivado"
    if not pair or pair[1] not in SPOT_QUOTES:
        return None, "", "par de cripto que no cotiza en dólares"
    return f"{pair[0]}{'USDC' if pair[1] == 'USDC' else 'USDT'}", "crypto", ""


class BinanceMirrorService(MirrorService):
    NAME = "Binance Testnet"
    PROVIDER = "binance_testnet"
    STATE_KEY = STATE_KEY
    TASK_NAME = "broker-binance-mirror"
    SETTINGS_HINT = "Ajustes → Testnet de Binance (práctica)"
    NOTE = NOTE

    def __init__(self, rt: Any) -> None:
        super().__init__(rt)
        self._filters: dict[tuple[str, str], tuple[float, dict | None]] = {}
        self._prepared: dict[str, int] = {}  # perp symbol -> leverage already set on the testnet

    # ------------------------------------------------------------------ settings

    def _cfg(self) -> dict[str, Any]:
        keys = {name: str(self._setting(f"broker.binance_{name}", "") or "").strip()
                for name in ("spot_key", "spot_secret", "futures_key", "futures_secret")}
        enabled = bool(self._setting("broker.binance_enabled", False))
        has_spot = bool(keys["spot_key"] and keys["spot_secret"])
        has_futures = bool(keys["futures_key"] and keys["futures_secret"])
        configured = has_spot or has_futures
        return {"enabled": enabled, "configured": configured, "ready": enabled and configured and not config.OFFLINE,
                **keys, "has_spot": has_spot, "has_futures": has_futures,
                # only compared in memory, to know when the cached account belongs to other keys
                "key_id": f"{keys['spot_key']}|{keys['futures_key']}",
                "stocks": False,
                "crypto": bool(self._setting("broker.binance_mirror_spot", True)),
                "perps": bool(self._setting("broker.binance_mirror_perps", True))}

    def _client(self, cfg: dict[str, Any]) -> BinanceTestnetClient:
        return BinanceTestnetClient(cfg["spot_key"], cfg["spot_secret"], cfg["futures_key"], cfg["futures_secret"])

    def _leverage(self) -> int:
        try:
            lev = float(self._setting("autopilot.perp_leverage", 10.0) or 10.0)
        except (TypeError, ValueError):
            lev = 10.0
        return int(min(max(round(lev), 1), 50))

    # ------------------------------------------------------------------ venue hooks

    def _map_symbol(self, symbol: str, cfg: dict[str, Any]) -> tuple[str | None, str, str]:
        sym, kind, why = binance_symbol(symbol, spot=cfg["crypto"], perps=cfg["perps"])
        if sym and kind == "perp" and not cfg["has_futures"]:
            return None, "", "faltan las claves de Demo Trading (futuros)"
        if sym and kind == "crypto" and not cfg["has_spot"]:
            return None, "", "faltan las claves del testnet de contado"
        return sym, kind, why

    @staticmethod
    def _market(e: dict) -> str:
        return "perp" if e.get("kind") == "perp" else "spot"

    async def _lot(self, e: dict, client: BinanceTestnetClient) -> dict | None:
        """The testnet's lot filters for the entry's symbol (cached an hour; None = it does not list it)."""
        key = (self._market(e), str(e["alpaca_symbol"]))
        hit = self._filters.get(key)
        if hit is None or time.monotonic() - hit[0] > FILTERS_TTL_S:
            hit = (time.monotonic(), await client.filters(*key))
            self._filters[key] = hit
        return hit[1]

    async def _open_qty(self, e: dict, client: BinanceTestnetClient, qty: float) -> float:
        lot = await self._lot(e, client)
        e["lot_step"] = (lot or {}).get("step")
        return floor_step(qty, e["lot_step"])

    async def _skip_reason(self, e: dict, client: BinanceTestnetClient, qty: float) -> str | None:
        lot = await self._lot(e, client)
        venue = "Demo Trading (futuros)" if self._market(e) == "perp" else "el testnet de contado"
        if lot is None:
            return f"{venue} de Binance no lista {e['alpaca_symbol']}"
        if e["kind"] == "crypto" and e["side"] == "short":
            return "al contado no hay cortos: una billetera solo vende lo que tiene"
        if qty < QTY_EPS or (lot.get("min_qty") and qty < float(lot["min_qty"]) - QTY_EPS):
            return f"cantidad por debajo del mínimo del testnet ({lot.get('min_qty')})"
        notional = qty * e["sim_entry"]
        if lot.get("min_notional") and notional < float(lot["min_notional"]):
            return (f"orden de {notional:.2f} $: por debajo del mínimo del testnet "
                    f"({float(lot['min_notional']):.0f} $)").replace(".", ",")
        return None

    async def _send(self, client: BinanceTestnetClient, e: dict, buy: bool, qty: float, cid: str,
                    purpose: str) -> dict:
        market = self._market(e)
        if market == "perp" and purpose == "open":
            lev = self._leverage()
            if self._prepared.get(e["alpaca_symbol"]) != lev:
                await client.prepare_perp(e["alpaca_symbol"], lev)
                self._prepared[e["alpaca_symbol"]] = lev
        return await client.submit_market(market=market, symbol=e["alpaca_symbol"], side="buy" if buy else "sell",
                                          qty=qty, client_order_id=cid,
                                          reduce_only=market == "perp" and purpose != "open")

    @staticmethod
    def _duplicate(exc: Any) -> bool:
        return False  # the client looks the client id up before sending: a repeated order cannot happen

    async def _sellable(self, e: dict, qty: float, held_in_alpaca: Any) -> float:
        """Spot: never sell more coins than the testnet account holds (its fee may come out of them); every
        quantity on the lot step."""
        if e.get("kind") == "crypto" and e["side"] == "long":
            base = str(e["alpaca_symbol"])
            for quote in ("USDT", "USDC"):
                if base.endswith(quote):
                    base = base[: -len(quote)]
                    break
            held = await held_in_alpaca(base)
            if held is not None:
                qty = min(qty, held)
        return floor_step(qty, e.get("lot_step"))
