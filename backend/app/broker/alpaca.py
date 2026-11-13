"""Client of Alpaca's paper-trading API (https://docs.alpaca.markets): ONLY the practice environment.

The base URL is the constant PAPER_URL and is not a setting, so no key can send an order to a live account (Alpaca's
live keys are not valid on the paper API). Plain requests through app.util.http_client (CLAUDE.md rule 4) with the
user's own keys in Alpaca's official headers, never following redirects (the keys stay on PAPER_URL). The keys never
appear in errors, events or status. Market orders only: what NEXUS automática simulated, sent as is.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.util import http_client

PAPER_URL = "https://paper-api.alpaca.markets"
TIMEOUT_S = 15.0


class AlpacaError(Exception):
    """A Spanish message for the user; `status` is the HTTP status (0 = no answer)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = int(status)


def qty_text(qty: float) -> str:
    """Alpaca takes quantities as strings: up to 9 decimals, no trailing zeros ("0.0123", "3")."""
    text = f"{float(qty):.9f}".rstrip("0").rstrip(".")
    return text or "0"


class AlpacaClient:
    def __init__(self, key_id: str, secret: str, timeout: float = TIMEOUT_S) -> None:
        self._headers = {"APCA-API-KEY-ID": str(key_id or ""), "APCA-API-SECRET-KEY": str(secret or ""),
                         "Accept": "application/json"}
        self.timeout = float(timeout)

    async def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        try:
            async with http_client(timeout=self.timeout, headers=self._headers) as client:
                r = await client.request(method, PAPER_URL + path, params=params, json=body, follow_redirects=False)
        except httpx.TimeoutException:
            raise AlpacaError("Alpaca no respondió a tiempo.") from None
        except httpx.HTTPError as exc:
            raise AlpacaError(f"No se pudo conectar con Alpaca ({type(exc).__name__}).") from None
        if r.status_code == 401:
            raise AlpacaError("Alpaca rechazó las claves: usa las de tu cuenta de práctica (paper trading).", 401)
        if r.status_code >= 300:
            detail = ""
            try:
                data = r.json()
                detail = str((data or {}).get("message") or "")[:200] if isinstance(data, dict) else ""
            except ValueError:
                pass
            raise AlpacaError(f"Alpaca respondió {r.status_code}" + (f": {detail}" if detail else "") + ".",
                              r.status_code)
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            raise AlpacaError("Alpaca respondió algo que no es JSON.", r.status_code) from None

    async def account(self) -> dict:
        data = await self._request("GET", "/v2/account")
        return data if isinstance(data, dict) else {}

    async def clock(self) -> dict:
        """{"is_open": bool, "next_open", "next_close", ...}: whether the US stock market trades now."""
        data = await self._request("GET", "/v2/clock")
        return data if isinstance(data, dict) else {}

    async def positions(self) -> list[dict]:
        data = await self._request("GET", "/v2/positions")
        return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []

    async def submit_market(self, *, symbol: str, side: str, qty: float, tif: str, client_order_id: str) -> dict:
        """A market order (side buy | sell, time in force day for stocks, gtc for crypto)."""
        body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": tif, "qty": qty_text(qty),
                "client_order_id": client_order_id}
        data = await self._request("POST", "/v2/orders", body=body)
        return data if isinstance(data, dict) else {}

    async def order(self, order_id: str) -> dict:
        data = await self._request("GET", f"/v2/orders/{order_id}")
        return data if isinstance(data, dict) else {}

    async def order_by_client_id(self, client_order_id: str) -> dict:
        data = await self._request("GET", "/v2/orders:by_client_order_id",
                                   params={"client_order_id": client_order_id})
        return data if isinstance(data, dict) else {}
