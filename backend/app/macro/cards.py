"""MacroMetric cards (types.ts): at most 6 per scope, each with a value, its change, a level that matches the
semáforo rules and one short Spanish note. Pure functions over the raw metrics snapshot (metrics.py)."""
from __future__ import annotations

from typing import Any

from app.macro import fmt
from app.util import safe_float

ES_WARN, ES_DANGER = 1.5, 3.0  # S&P 500 futures |Δ%|
NQ_WARN, NQ_DANGER = 2.0, 4.0  # Nasdaq 100 futures |Δ%| (more volatile than the S&P)
VIX_CHANGE_WARN, VIX_CHANGE_DANGER = 12.0, 25.0  # VIX intraday Δ%
DXY_WARN = 0.8  # |Δ%|
US10Y_WARN_BP = 10.0  # |Δ| basis points
MOVE_WARN = 120.0
OIL_BIG_MOVE = 3.0
FNG_LOW, FNG_HIGH = 15, 85
FUNDING_EXTREME = 0.0005  # 0.05 % per 8 h (normal ≈ 0.01 %)


def _card(mid: str, label: str, value: Any, display: str, change_pct: Any = None, change_display: str = "",
          level: str = "ok", note: str = "") -> dict:
    return {"id": mid, "label": label, "value": safe_float(value), "display": display,
            "change_pct": safe_float(change_pct), "change_display": change_display, "level": level, "note": note}


def _worse(a: str, b: str) -> str:
    order = {"ok": 0, "warn": 1, "danger": 2}
    return a if order[a] >= order[b] else b


# ----------------------------------------------------------------------------- levels (shared with the rules)


def vix_level(q: dict, caution: float, danger: float, session_closed: bool) -> str:
    v, ch = safe_float(q.get("value")), safe_float(q.get("change_pct"))
    level = "ok"
    if v is not None:
        level = "danger" if v >= danger else "warn" if v >= caution else "ok"
    if ch is not None and not session_closed:
        level = _worse(level, "danger" if ch >= VIX_CHANGE_DANGER else "warn" if ch >= VIX_CHANGE_WARN else "ok")
    return level


def move_level(change: Any, warn: float, danger: float | None, session_closed: bool) -> str:
    ch = safe_float(change)
    if ch is None or session_closed:
        return "ok"
    if danger is not None and abs(ch) >= danger:
        return "danger"
    return "warn" if abs(ch) >= warn else "ok"


def funding_8h(f: dict) -> float | None:
    rate = safe_float(f.get("rate"))
    if rate is None:
        return None
    interval = safe_float(f.get("interval_h"))
    return rate * 8.0 / interval if interval and interval > 0 else rate


def fng_class(value: int) -> str:
    if value <= 24:
        return "miedo extremo"
    if value <= 44:
        return "miedo"
    if value <= 55:
        return "neutral"
    if value <= 75:
        return "codicia"
    return "codicia extrema"


# ----------------------------------------------------------------------------- cards


def vix_card(q: dict, caution: float, danger: float, closed: bool) -> dict:
    level = vix_level(q, caution, danger, closed)
    v = safe_float(q.get("value"))
    by_level = vix_level({"value": v}, caution, danger, True)  # level reached by the value alone
    if level == by_level:
        note = {"danger": "Volatilidad extrema: no operes o reduce mucho el tamaño",
                "warn": "Volatilidad alta: reduce el tamaño", "ok": "Volatilidad normal"}[level]
    else:
        note = {"danger": "Se dispara hoy: no operes o reduce mucho el tamaño",
                "warn": "Subiendo con fuerza hoy: reduce el tamaño"}[level]
    if closed:
        note += " · bolsa cerrada"
    return _card("vix", "Miedo en bolsa (VIX)", q.get("value"), fmt.num(q.get("value"), 2), q.get("change_pct"),
                 fmt.pct(q.get("change_pct")), level, note)


def sp_card(es: dict, nq: dict | None, closed: bool) -> dict:
    level = move_level(es.get("change_pct"), ES_WARN, ES_DANGER, closed)
    parts = []
    if closed:
        parts.append("Mercado cerrado (último cierre)")
    elif level != "ok":
        parts.append("Movimiento extremo" if level == "danger" else "Sesión muy movida")
    if nq and nq.get("change_pct") is not None:
        parts.append(f"Nasdaq {fmt.pct(nq.get('change_pct'))}")
    note = " · ".join(parts) or "Sesión tranquila"
    return _card("us_futures", "Futuros S&P 500", es.get("value"), fmt.num(es.get("value"), 2), es.get("change_pct"),
                 fmt.pct(es.get("change_pct")), level, note)


def nasdaq_card(nq: dict, closed: bool) -> dict:
    ch = safe_float(nq.get("change_pct"))
    level = move_level(ch, NQ_WARN, NQ_DANGER, closed)
    if closed:
        note = "Mercado cerrado (último cierre)"
    elif level == "ok":
        note = "Bolsa tecnológica sin sobresaltos"
    else:
        note = "Caída fuerte: presión sobre cripto" if (ch or 0) < 0 else "Subida fuerte: apetito por riesgo"
    return _card("us_futures", "Futuros Nasdaq 100", nq.get("value"), fmt.num(nq.get("value"), 2), ch,
                 fmt.pct(ch), level, note)


def dxy_card(q: dict, closed: bool) -> dict:
    ch = safe_float(q.get("change_pct"))
    level = move_level(ch, DXY_WARN, None, closed)
    if level == "warn":
        note = "Dólar muy fuerte hoy: cuidado con forex y oro" if (ch or 0) > 0 else "Dólar cayendo con fuerza"
    else:
        note = "Dólar estable"
    return _card("dxy", "Dólar (DXY)", q.get("value"), fmt.num(q.get("value"), 2), ch, fmt.pct(ch, 2), level, note)


def us10y_card(q: dict, closed: bool) -> dict:
    ch_abs = safe_float(q.get("change_abs"))
    change_bp = ch_abs * 100.0 if ch_abs is not None else None
    level = move_level(change_bp, US10Y_WARN_BP, None, closed)
    note = "Tipos muy movidos: afecta a bolsa y dólar" if level == "warn" else "Tipos estables"
    return _card("us10y", "Bono EE. UU. 10 años", q.get("value"), f"{fmt.num(q.get('value'), 2)} %",
                 q.get("change_pct"), fmt.bp(change_bp), level, note)


def move_card(q: dict) -> dict:
    v = safe_float(q.get("value"))
    level = "warn" if v is not None and v >= MOVE_WARN else "ok"
    note = "Volatilidad alta en bonos" if level == "warn" else "Volatilidad de bonos normal"
    return _card("move", "Volatilidad de bonos (MOVE)", v, fmt.num(v, 1), q.get("change_pct"),
                 fmt.pct(q.get("change_pct")), level, note)


def oil_card(q: dict) -> dict:
    ch = safe_float(q.get("change_pct"))
    if ch is not None and abs(ch) >= OIL_BIG_MOVE:
        note = "Subida fuerte del crudo: presión inflacionaria" if ch > 0 else "Caída fuerte del crudo"
    else:
        note = "Petróleo estable"
    return _card("oil", "Petróleo WTI", q.get("value"), fmt.num(q.get("value"), 2), ch, fmt.pct(ch), "ok", note)


def fng_card(fng: dict, rules_apply: bool) -> dict:
    v = int(fng.get("value") or 0)
    extreme = v <= FNG_LOW or v >= FNG_HIGH
    level = "warn" if extreme and rules_apply else "ok"
    if v <= FNG_LOW:
        note = "Pánico: posibles giros bruscos, opera pequeño"
    elif v >= FNG_HIGH:
        note = "Euforia: riesgo de corrección"
    else:
        note = "Sentimiento sin extremos"
    return _card("crypto_fng", "Miedo/codicia cripto", v, f"{v} · {fng_class(v)}", None, "", level, note)


def funding_card(funding: dict) -> dict | None:
    btc = funding.get("BTC")
    if not btc:
        return None
    rate8 = funding_8h(btc)
    if rate8 is None:
        return None
    level = "warn" if abs(rate8) >= FUNDING_EXTREME else "ok"
    if level == "warn":
        note = ("Exceso de largos apalancados: riesgo de liquidaciones" if rate8 > 0
                else "Exceso de cortos: riesgo de squeeze alcista")
    else:
        note = "Apalancamiento equilibrado"
    eth = funding.get("ETH")
    eth8 = funding_8h(eth) if eth else None
    change = f"ETH {fmt.num(eth8 * 100, 3)} %" if eth8 is not None else ""
    return _card("funding", "Funding BTC (8 h)", rate8 * 100, f"{fmt.num(rate8 * 100, 3)} %", None, change, level, note)


def dominance_card(q: dict) -> dict:
    ch = safe_float(q.get("change_pct"))
    if ch is not None and ch >= 0.5:
        note = "Sube: el dinero se refugia en BTC"
    elif ch is not None and ch <= -0.5:
        note = "Baja: el dinero rota hacia altcoins"
    else:
        note = "Estable"
    return _card("btc_dominance", "Dominancia BTC", q.get("value"), f"{fmt.num(q.get('value'), 1)} %", ch,
                 fmt.pct(ch), "ok", note)


def build_cards(metrics: dict, *, crypto: bool, caution: float, danger: float, closed: bool,
                vix_closed: bool) -> list[dict]:
    """≤ 6 cards: global/other scopes → VIX, S&P futures (+Nasdaq), DXY, 10-year yield, MOVE (or oil), crypto
    Fear & Greed; crypto scope → VIX, Nasdaq futures, DXY, Fear & Greed, BTC funding, BTC dominance."""
    q = metrics.get("quotes") or {}
    fng = metrics.get("fng")
    cards: list[dict | None] = [vix_card(q["vix"], caution, danger, vix_closed) if "vix" in q else None]
    if crypto:
        cards.append(nasdaq_card(q["nq"], closed) if "nq" in q else None)
        cards.append(dxy_card(q["dxy"], closed) if "dxy" in q else None)
        cards.append(fng_card(fng, True) if fng else None)
        cards.append(funding_card(metrics.get("funding") or {}))
        cards.append(dominance_card(q["btc_d"]) if "btc_d" in q else None)
    else:
        cards.append(sp_card(q["es"], q.get("nq"), closed) if "es" in q else None)
        cards.append(dxy_card(q["dxy"], closed) if "dxy" in q else None)
        cards.append(us10y_card(q["us10y"], closed) if "us10y" in q else None)
        cards.append(move_card(q["move"]) if "move" in q else oil_card(q["oil"]) if "oil" in q else None)
        cards.append(fng_card(fng, False) if fng else None)
    return [c for c in cards if c is not None][:6]
