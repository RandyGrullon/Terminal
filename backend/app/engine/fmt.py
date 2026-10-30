"""Spanish number formatting for engine messages (advice, gate details, terminal output).

1234.5 -> "1.234,5" · percentages "+1,2 %" · probabilities "64 %" · R multiples "−0,4R".
Negative numbers use the typographic minus sign (U+2212).
"""
from __future__ import annotations

import math
from typing import Any

MINUS = "−"

ACTION_LABEL = {
    "BUY": "COMPRAR",
    "SELL": "VENDER",
    "HOLD_LONG": "MANTENER LARGO",
    "HOLD_SHORT": "MANTENER CORTO",
    "WAIT": "ESPERAR",
    "EXIT_LONG": "CERRAR LARGO",
    "EXIT_SHORT": "CERRAR CORTO",
}
REGIME_LABEL = {
    "trend_up": "Tendencia alcista",
    "trend_down": "Tendencia bajista",
    "range": "Lateral",
    "volatile": "Volátil",
    "unknown": "Desconocido",
}
VERDICT_LABEL = {"TAKE": "TOMAR", "WAIT": "ESPERAR", "AVOID": "NO TOMAR"}
SIDE_LABEL = {"long": "largo", "short": "corto"}
EXIT_LABEL = {
    "SL": "Stop loss",
    "BE": "Stop en punto de equilibrio",
    "TRAIL": "Stop dinámico",
    "TP3": "Objetivo 3",
    "EARLY": "Salida anticipada",
    "REVERSE": "Señal contraria",
    "TIME": "Tiempo agotado",
    "SIGNAL": "Señal de salida",
    "OPEN": "Abierta",
}


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def num(x: Any, digits: int = 2, signed: bool = False) -> str:
    v = _finite(x)
    if v is None:
        return "—"
    digits = max(0, int(digits))
    body = f"{abs(v):,.{digits}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    is_zero = round(abs(v), digits) == 0
    if v < 0 and not is_zero:
        return MINUS + body
    if signed and v > 0 and not is_zero:
        return "+" + body
    return body


def price_decimals(p: float | None) -> int:
    a = abs(p or 0.0)
    if a >= 10000:
        return 0
    if a >= 1000:
        return 1
    if a >= 100:
        return 2
    if a >= 10:
        return 3
    if a >= 1:
        return 4
    if a >= 0.01:
        return 5
    return 8


def price(x: Any) -> str:
    v = _finite(x)
    return "—" if v is None else num(v, price_decimals(v))


def pct(x: Any, digits: int = 1, signed: bool = True) -> str:
    """Value already in percent units: 1.234 -> '+1,2 %'."""
    s = num(x, digits, signed)
    return s if s == "—" else f"{s} %"


def prob(p: Any, digits: int = 0) -> str:
    v = _finite(p)
    return "—" if v is None else f"{num(v * 100.0, digits)} %"


def rr(x: Any, digits: int = 1, signed: bool = True) -> str:
    s = num(x, digits, signed)
    return s if s == "—" else f"{s}R"


def ratio(x: Any, digits: int = 2) -> str:
    return num(x, digits)
