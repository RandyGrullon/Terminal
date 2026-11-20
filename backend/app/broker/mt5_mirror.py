"""Mirror of NEXUS automática in a MetaTrader 5 account of ANY broker (`rt.broker_mt5`), demo or real.

The same reconcile as the other mirrors (app.broker.mirror.MirrorService: every SYNC_S seconds, deterministic client
ids, partial exits and closes that follow NEXUS, the price difference with the simulation as «x de n»), sent through
the MetaTrader5 package to the MT5 terminal open on this Windows machine (app.broker.mt5.Mt5Client):

- the account is whichever one the user logged in to in MT5: NEXUS never stores or sends an MT5 password;
- DEMO and contest accounts are mirrored as soon as the mirror is on;
- a REAL account is never touched unless the user switched on `broker.mt5_allow_real` AND typed that account's number
  in `broker.mt5_real_login`: another real account (or the switch off) means no order at all, with the reason. The
  switch is the user's own decision, made by hand in Ajustes; nothing in the app turns it on;
- every order is capped at `broker.mt5_max_lots` lots (all account types);
- symbols are matched to the broker's names automatically (gold: XAUUSD, GOLD, XAUUSD.m, XAUUSDm…; crypto: BTCUSD…;
  stocks: the ticker) or by the user's map `broker.mt5_symbols` ({"XAU": "GOLD"}); NEXUS's units become lots with
  the broker's contract size, rounded down to its step; under its minimum lot the position is not mirrored;
- the buy-and-hold portfolio, theoretical options and synthetic markets are not mirrored.

The package is Windows-only and needs the terminal open (with «Trading algorítmico» on): the mirror does not run in
the cloud image. Off by default. State in table `meta` (key broker.mt5_mirror), no schema change.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

from app import config
from app.broker import mt5 as mt5lib
from app.broker.common import BrokerError
from app.broker.mirror import QTY_EPS, MirrorError, MirrorService
from app.data.symbols import is_sim, split_pair, strip_derivative
from app.util import now_ms

STATE_KEY = "broker.mt5_mirror"
SYMBOLS_TTL_S = 600.0
ACCOUNT_TTL_S = 10.0
DOLLAR_QUOTES = ("USD", "USDT", "USDC", "FDUSD")
ALIASES = {"XAU": ("XAUUSD", "GOLD"), "XAG": ("XAGUSD", "SILVER"), "XPT": ("XPTUSD", "PLATINUM"),
           "XPD": ("XPDUSD", "PALLADIUM")}
SETTINGS_HINT = "Ajustes → MetaTrader 5"
NOTE = ("Cuenta de MetaTrader 5 de tu bróker, la que tengas abierta en el terminal. En una cuenta demo las órdenes no "
        "mueven dinero; en una cuenta REAL sí, y solo se envían si tú lo autorizaste para ese número de cuenta. NEXUS "
        "automática decide y simula; el espejo repite sus posiciones para medir la diferencia con el precio, el "
        "spread y la ejecución de tu bróker. La validación no encontró evidencia de ventaja: puede perder. Con menos "
        "de 30 operaciones, sin conclusiones. No es asesoría financiera.")


def bases_of(symbol: str) -> tuple[str | None, str]:
    """(base asset to look for in MT5, "") or (None, why it is not mirrored)."""
    sym = str(symbol or "").strip().upper()
    if not sym or is_sim(sym):
        return None, "mercado sintético: no existe en MT5"
    _, _, ticker = sym.partition(":")
    ticker = ticker or sym
    plain, _ = strip_derivative(ticker)
    pair = split_pair(plain)
    if pair and pair[1] in DOLLAR_QUOTES:
        return pair[0], ""
    if pair:
        return None, f"cotiza en {pair[1]}: el espejo solo busca mercados en dólares"
    return plain, ""


def candidates(base: str) -> list[str]:
    """The names a broker usually gives to that market, most likely first."""
    out = list(ALIASES.get(base, ()))
    for name in (f"{base}USD", base):
        if name not in out:
            out.append(name)
    return out


def _suffix_ok(suffix: str) -> bool:
    """A broker's decoration of a symbol name: «.m», «m», «#», «.OQ», «-ECN»… but never a currency (XAU + EUR)."""
    if not suffix or len(suffix) > 4:
        return False
    if suffix[0] in ".#_-+!":
        return True
    return len(suffix) == 1 and suffix.isalpha()


def pick_symbol(base: str, names: list[str], user_map: dict[str, Any] | None = None) -> str | None:
    """The broker's symbol for a base: the user's map, an exact name, or a name with a short suffix (XAUUSD.m)."""
    mapped = (user_map or {}).get(base)
    if isinstance(mapped, str) and mapped.strip():
        return mapped.strip()
    by_upper = {n.upper(): n for n in names}
    for cand in candidates(base):
        if cand in by_upper:
            return by_upper[cand]
    for cand in candidates(base):
        near = sorted((n for n in names if n.upper().startswith(cand) and _suffix_ok(n[len(cand):])), key=len)
        if near:
            return near[0]
    return None


def parse_symbol_map(text: Any) -> dict[str, str]:
    """"XAU=GOLD, BTC=BTCUSD.m" (or a dict) -> {"XAU": "GOLD", "BTC": "BTCUSD.m"}; bad pairs are ignored."""
    if isinstance(text, dict):
        return {str(k).strip().upper(): str(v).strip() for k, v in text.items() if str(k).strip() and str(v).strip()}
    out: dict[str, str] = {}
    for part in re.split(r"[,;\n]+", str(text or "")):
        key, sep, value = part.partition("=")
        if sep and key.strip() and value.strip():
            out[key.strip().upper()] = value.strip()
    return out


def floor_lots(lots: float, step: float) -> float:
    if step <= 0:
        return lots
    return round(math.floor(lots / step + 1e-9) * step, 8)


class Mt5MirrorService(MirrorService):
    NAME = "MetaTrader 5"
    PROVIDER = "mt5"
    STATE_KEY = STATE_KEY
    TASK_NAME = "broker-mt5-mirror"
    SETTINGS_HINT = SETTINGS_HINT
    NOTE = NOTE

    def __init__(self, rt: Any) -> None:
        super().__init__(rt)
        self._mt5: mt5lib.Mt5Client | None = None
        self._names: tuple[float, list[str]] | None = None
        self._symbols: dict[str, dict | None] = {}
        self._acct_cache: tuple[float, dict] | None = None
        self._blocked: str | None = None

    async def stop(self) -> None:
        await super().stop()
        if self._mt5 is not None:
            try:
                await self._mt5.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ settings

    def _cfg(self) -> dict[str, Any]:
        user_map = parse_symbol_map(self._setting("broker.mt5_symbols", "") or "")
        try:
            max_lots = float(self._setting("broker.mt5_max_lots", 0.1) or 0.0)
        except (TypeError, ValueError):
            max_lots = 0.1
        available = mt5lib.available()
        enabled = bool(self._setting("broker.mt5_enabled", False))
        return {"enabled": enabled, "configured": available, "ready": enabled and available and not config.OFFLINE,
                "path": str(self._setting("broker.mt5_path", "") or ""),
                "symbols": {str(k).upper(): v for k, v in user_map.items()} if isinstance(user_map, dict) else {},
                "max_lots": max(max_lots, 0.0),
                "allow_real": bool(self._setting("broker.mt5_allow_real", False)),
                "real_login": str(self._setting("broker.mt5_real_login", "") or "").strip(),
                "key_id": "mt5", "stocks": True, "crypto": True, "perps": True}

    def _client(self, cfg: dict[str, Any]) -> mt5lib.Mt5Client:
        if self._mt5 is None or self._mt5.path != cfg["path"]:
            self._mt5 = mt5lib.Mt5Client(cfg["path"])
        return self._mt5

    # ------------------------------------------------------------------ the real-account gate

    @staticmethod
    def permission(acct: dict, cfg: dict) -> str | None:
        """None when orders may go to this account; otherwise why not (Spanish)."""
        if not acct.get("connected", True):
            return "el terminal de MT5 no está conectado al servidor de tu bróker"
        if not acct.get("trade_allowed", True):
            return "tu bróker no permite trading automático en esta cuenta"
        if not acct.get("terminal_trade_allowed", True):
            return "activa «Trading algorítmico» en la barra de MetaTrader 5"
        if acct.get("kind") != "real":
            return None
        login = str(acct.get("login") or "")
        if not cfg["allow_real"]:
            return (f"la cuenta {login} ({acct.get('server')}) es REAL y no autorizaste operar en real: no se envía "
                    f"nada (actívalo tú en {SETTINGS_HINT} si de verdad lo quieres)")
        if cfg["real_login"] != login:
            return (f"la cuenta {login} ({acct.get('server')}) es REAL y no es la que autorizaste "
                    f"({cfg['real_login'] or 'ninguna'}): no se envía nada")
        return None

    async def _read_account(self, cfg: dict, refresh: bool = False) -> dict:
        hit = self._acct_cache
        if hit is None or refresh or time.monotonic() - hit[0] > ACCOUNT_TTL_S:
            hit = (time.monotonic(), await self._client(cfg).account())
            self._acct_cache = hit
        return hit[1]

    async def sync(self) -> None:
        cfg = self._cfg()
        if not cfg["ready"] or getattr(self.rt, "paper", None) is None:
            return
        try:
            acct = await self._read_account(cfg, refresh=True)
        except BrokerError as exc:
            self._error = str(exc)
            self._last_sync_ms = now_ms()
            if self._log.should_log("mt5-account", 600):
                self._emit("warn", f"Espejo en {self.NAME}: {exc}")
            return
        why = self.permission(acct, cfg)
        if why:
            self._blocked = why
            self._error = f"No opera: {why}."
            self._last_sync_ms = now_ms()
            if self._log.should_log(f"mt5-blocked:{acct.get('login')}", 600):
                self._emit("warn", f"Espejo en {self.NAME}: no opera: {why}.")
            # what NEXUS opens while the account cannot be used is never sent later (a real account authorized
            # afterwards only gets the positions opened from then on)
            async with self._lock:
                for pos in self.rt.paper.open_positions(3):
                    pid = int(pos["id"])
                    if pid not in self._entries:
                        e = await MirrorService._new(self, {**pos, "symbol": "SIM:BLOCKED"}, cfg, None, None)
                        e.update({"symbol": pos["symbol"], "reason": f"abierta mientras el espejo no podía operar: {why}"})
                        self._entries[pid] = e
                await self._save()
            return
        self._blocked = None
        self._login = acct.get("login")
        await super().sync()

    # ------------------------------------------------------------------ venue hooks

    async def _new(self, pos: dict, cfg: dict, client: Any, market_open: Any) -> dict:
        if pos.get("hold"):
            e = await super()._new({**pos, "symbol": "SIM:HOLD"}, cfg, client, market_open)
            e.update({"symbol": pos["symbol"], "reason": "cartera de comprar y mantener: no se replica"})
            return e
        inst = pos.get("instrument") or {}
        if inst.get("kind") == "future" and not inst.get("perpetual"):
            e = await super()._new({**pos, "symbol": "SIM:FUTURE"}, cfg, client, market_open)
            e.update({"symbol": pos["symbol"], "reason": "futuro de índice con vencimiento: en MT5 sería otro contrato"})
            return e
        if inst.get("kind") == "option":
            e = await super()._new({**pos, "symbol": "SIM:OPTION"}, cfg, client, market_open)
            e.update({"symbol": pos["symbol"], "reason": "opción teórica: no existe en MT5"})
            return e
        e = await super()._new(pos, cfg, client, market_open)
        e["login"] = getattr(self, "_login", None)
        return e

    def _map_symbol(self, symbol: str, cfg: dict[str, Any]) -> tuple[str | None, str, str]:
        base, why = bases_of(symbol)
        return (base, "mt5", "") if base else (None, "", why)

    async def _resolve(self, e: dict, client: mt5lib.Mt5Client, cfg: dict) -> dict | None:
        """The broker's symbol and lot rules for the entry (the base is in e['alpaca_symbol'] until resolved)."""
        if e.get("mt5_symbol"):
            return self._symbols.get(e["mt5_symbol"])
        base = str(e.get("alpaca_symbol") or "")
        if self._names is None or time.monotonic() - self._names[0] > SYMBOLS_TTL_S:
            self._names = (time.monotonic(), await client.symbol_names())
        name = pick_symbol(base, self._names[1], cfg["symbols"])
        if not name:
            e["mt5_base"] = base
            return None
        if name not in self._symbols:
            self._symbols[name] = await client.symbol(name)
        info = self._symbols[name]
        if info is not None:
            e.update({"mt5_base": base, "mt5_symbol": name, "alpaca_symbol": name})
        return info

    async def _open_qty(self, e: dict, client: mt5lib.Mt5Client, qty: float) -> float:
        info = await self._resolve(e, client, self._cfg())
        if info is None:
            return 0.0
        lots = qty / info["contract_size"] if info["contract_size"] > 0 else qty
        e["sim_lots"] = round(lots, 6)
        cap = self._cfg()["max_lots"]
        if cap > 0:
            lots = min(lots, cap)
        if info.get("volume_max"):
            lots = min(lots, info["volume_max"])
        e["lot_step"] = info["volume_step"]
        return floor_lots(lots, info["volume_step"])

    async def _skip_reason(self, e: dict, client: mt5lib.Mt5Client, qty: float) -> str | None:
        info = await self._resolve(e, client, self._cfg())
        if info is None:
            return (f"tu bróker no tiene {e.get('mt5_base') or e.get('alpaca_symbol')} en MT5 con un nombre conocido: "
                    f"escríbelo en {SETTINGS_HINT} → símbolos")
        if info.get("trade_mode") == 0:
            return f"tu bróker no permite operar {info['name']} ahora"
        if self._cfg()["max_lots"] <= 0:
            return "el tope de lotes por orden es 0"
        if qty < info["volume_min"] - QTY_EPS:
            return (f"{e.get('sim_lots', 0):g} lotes en NEXUS: por debajo del mínimo de tu bróker "
                    f"({info['volume_min']:g} lotes)").replace(".", ",")
        return None

    async def _send(self, client: mt5lib.Mt5Client, e: dict, buy: bool, qty: float, cid: str, purpose: str) -> dict:
        if purpose != "open" and e.get("login") and getattr(self, "_login", None) != e["login"]:
            # the user switched accounts in MT5: never close something on another account (status 0 = retry later)
            raise BrokerError(f"MT5 está en otra cuenta ({getattr(self, '_login', None)}); esta posición se abrió en "
                              f"la {e['login']}: vuelve a esa cuenta para que el espejo la cierre")
        res = await client.send_market(symbol=e["alpaca_symbol"], buy=buy, volume=qty, client_order_id=cid,
                                       position=e.get("ticket") if purpose != "open" else None)
        if purpose == "open" and res.get("position"):
            e["ticket"] = res["position"]
        return res

    @staticmethod
    def _duplicate(exc: Any) -> bool:
        return False  # the client looks the client id up before sending

    async def _sellable(self, e: dict, qty: float, held_in_alpaca: Any) -> float:
        return floor_lots(min(qty, e.get("held") or qty), float(e.get("lot_step") or 0.01))

    # ------------------------------------------------------------------ read

    def _public(self, e: dict) -> dict:
        out = super()._public(e)
        out["mt5_symbol"] = e.get("mt5_symbol")
        return out

    async def _account_info(self, cfg: dict, refresh: bool) -> dict:
        acct = await self._read_account(cfg, refresh)
        return {"equity": acct["equity"], "cash": acct["balance"], "buying_power": acct["margin_free"],
                "status": mt5lib.TRADE_MODE_ES.get(acct["kind"], acct["kind"]), "currency": acct["currency"]}

    async def overview(self, refresh: bool = False) -> dict[str, Any]:
        cfg = self._cfg()
        if cfg["enabled"] and not cfg["configured"]:
            out = await super().overview(refresh)
            out["error"] = ("MetaTrader 5 solo funciona en Windows con el paquete «MetaTrader5» y el terminal abierto "
                            "(no en la nube).")
            return out
        out = await super().overview(refresh)
        out["max_lots"] = cfg["max_lots"]
        out["allow_real"] = cfg["allow_real"]
        out["blocked"] = self._blocked
        acct = None
        if out.get("connected"):
            try:
                acct = await self._read_account(cfg)
            except BrokerError:
                acct = None
        if acct:
            out["paper"] = acct["kind"] != "real"
            out["account"] = {**(out.get("account") or {}), "login": acct["login"], "server": acct["server"],
                              "company": acct["company"], "kind": acct["kind"], "leverage": acct["leverage"]}
            why = self.permission(acct, cfg)
            out["blocked"] = why
            if why and not out.get("error"):
                out["error"] = f"No opera: {why}."
        return out

    async def sync_now(self) -> dict[str, Any]:
        cfg = self._cfg()
        if not cfg["enabled"]:
            raise MirrorError(f"El espejo está desactivado: actívalo en {SETTINGS_HINT}.")
        if not cfg["configured"]:
            raise MirrorError("MetaTrader 5 solo funciona en Windows con el paquete «MetaTrader5» y el terminal abierto.")
        if config.OFFLINE:
            raise MirrorError("Modo sin conexión (NEXUS_OFFLINE): el espejo no envía nada.")
        await self.sync()
        return await self.overview(refresh=True)
