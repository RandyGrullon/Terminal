"""Pure, deterministic semáforo rules (docs/ARCHITECTURE.md §15): events + metrics + settings → MarketConditions.

red   = high-impact event of a relevant currency within [−block_after, +block_before] min (blackout), VIX ≥
        vix_danger, VIX intraday ≥ +25 %, S&P futures |Δ| ≥ 3 %.
amber = high-impact event within warn_before min or ≤ 2 h after one, VIX ≥ vix_caution or Δ ≥ +12 %, S&P futures
        |Δ| ≥ 1.5 %, DXY |Δ| ≥ 0.8 %, US10Y |Δ| ≥ 10 pb, MOVE ≥ 120; crypto scope also Nasdaq futures |Δ| ≥ 2 %,
        Fear & Greed ≤ 15 / ≥ 85 and extreme BTC funding. No data at all → amber + stale.
Daily-change rules are skipped while the US session is closed (weekend), when changes are Friday's.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.macro import cards as C
from app.macro import fmt
from app.macro.calendar import IMPACT_RANK, public_event
from app.macro.conditions_events import APPROX_NOTE, collapse, event_findings
from app.macro.fomc import NEW_YORK
from app.macro.metrics import QUOTES_STALE_S
from app.macro.scope import global_scope, scope_for_symbol
from app.macro.titles import short_title
from app.util import safe_float

__all__ = ["assess", "scope_for_symbol", "global_scope", "LEVEL_LABEL", "NO_DATA_HEADLINE", "settings_view"]

LEVEL_LABEL = {"green": "Operar con normalidad", "amber": "Precaución", "red": "No operar ahora"}
LEVEL_RANK = {"green": 0, "amber": 1, "red": 2}
MAX_REASONS, MAX_EVENTS = 3, 5
MEDIUM_HORIZON_S = 48 * 3600
HIGH_HORIZON_S = 60 * 86400  # the next FOMC meeting is never further away
GREEN_ADVICE = "Condiciones normales: sigue tu plan y respeta el filtro NEXUS."
NO_DATA_HEADLINE = "Sin datos macro: opera con cautela."
NO_BLACKOUT = {"active": False, "until": None, "event": None}
METRIC_PRIO = 10  # findings of metric_findings have prio >= 10; calendar findings (conditions_events) 0-2


def settings_view(settings: dict | None) -> SimpleNamespace:
    s = settings or {}

    def number(key: str, default: float) -> float:
        v = safe_float(s.get(key))
        return default if v is None else max(0.0, v)

    cur = s.get("macro.currencies") or ["USD"]
    if isinstance(cur, str):
        cur = [c for c in cur.replace(";", ",").split(",")]
    currencies = [str(c).strip().upper() for c in cur if str(c).strip()] or ["USD"]
    return SimpleNamespace(
        before=int(number("macro.block_before_min", 30) * 60), after=int(number("macro.block_after_min", 30) * 60),
        warn=int(number("macro.warn_before_min", 120) * 60), vix_caution=number("macro.vix_caution", 22.0),
        vix_danger=number("macro.vix_danger", 30.0), currencies=currencies, tz=s.get(fmt.TZ_SETTING) or None,
    )


def us_session_closed(now_s: int) -> bool:
    """US index futures closed: Friday 17:00 → Sunday 18:00 New York time (daily changes are Friday's)."""
    d = dt.datetime.fromtimestamp(now_s, NEW_YORK)
    minutes = d.hour * 60 + d.minute
    return d.weekday() == 5 or (d.weekday() == 4 and minutes >= 17 * 60) or (d.weekday() == 6 and minutes < 18 * 60)


def vix_session_closed(now_s: int) -> bool:
    """The VIX index is not computed from Friday 17:00 until Monday 03:15 New York time."""
    d = dt.datetime.fromtimestamp(now_s, NEW_YORK)
    return us_session_closed(now_s) or d.weekday() == 6 or (d.weekday() == 0 and d.hour * 60 + d.minute < 195)


def _finding(level: str, prio: int, reason: str, headline: str | None = None) -> dict:
    return {"level": level, "prio": prio, "reason": reason, "headline": headline or f"{reason}."}


def metric_findings(metrics: dict, cfg: SimpleNamespace, crypto: bool, now_s: int) -> list[dict]:
    q = metrics.get("quotes") or {}
    closed, vclosed = us_session_closed(now_s), vix_session_closed(now_s)
    out: list[dict] = []
    vix = q.get("vix") or {}
    v, ch = safe_float(vix.get("value")), safe_float(vix.get("change_pct"))
    if v is not None:
        tag = f"VIX {fmt.num(v, 1)}"
        if ch is not None and not vclosed and abs(ch) >= 1:
            tag += f" ({fmt.pct(ch, 0)})"
        level = C.vix_level(vix, cfg.vix_caution, cfg.vix_danger, vclosed)
        if level == "danger":
            out.append(_finding("red", 10, f"{tag}: volatilidad extrema"))
        elif level == "warn":
            text = "volatilidad alta" if v >= cfg.vix_caution else "la volatilidad se dispara"
            out.append(_finding("amber", 11, f"{tag}: {text}"))
    es = q.get("es") or {}
    level = C.move_level(es.get("change_pct"), C.ES_WARN, C.ES_DANGER, closed)
    if level != "ok":
        text = "movimiento extremo" if level == "danger" else "sesión muy movida"
        out.append(_finding("red" if level == "danger" else "amber", 12,
                            f"Futuros S&P 500 {fmt.pct(es.get('change_pct'))}: {text}"))
    nq = q.get("nq") or {}
    if crypto and C.move_level(nq.get("change_pct"), C.NQ_WARN, None, closed) == "warn":
        nch = safe_float(nq.get("change_pct")) or 0.0
        out.append(_finding("amber", 13, f"Futuros Nasdaq {fmt.pct(nch)}: {'caída' if nch < 0 else 'subida'} fuerte"))
    dxy = q.get("dxy") or {}
    if C.move_level(dxy.get("change_pct"), C.DXY_WARN, None, closed) == "warn":
        out.append(_finding("amber", 14, f"Dólar (DXY) {fmt.pct(dxy.get('change_pct'), 2)}: movimiento fuerte"))
    us10y = q.get("us10y") or {}
    ch_abs = safe_float(us10y.get("change_abs"))
    if ch_abs is not None and C.move_level(ch_abs * 100, C.US10Y_WARN_BP, None, closed) == "warn":
        out.append(_finding("amber", 15, f"Bono 10 años {fmt.bp(ch_abs * 100)}: tipos muy movidos"))
    move = safe_float((q.get("move") or {}).get("value"))
    if move is not None and move >= C.MOVE_WARN:
        out.append(_finding("amber", 16, f"MOVE {fmt.num(move, 0)}: volatilidad alta en bonos"))
    if crypto:
        fng = metrics.get("fng") or {}
        fv = safe_float(fng.get("value"))
        if fv is not None and fv <= C.FNG_LOW:
            out.append(_finding("amber", 17, f"Miedo/codicia cripto {int(fv)}: pánico, posibles giros bruscos"))
        elif fv is not None and fv >= C.FNG_HIGH:
            out.append(_finding("amber", 17, f"Miedo/codicia cripto {int(fv)}: euforia, riesgo de corrección"))
        btc = (metrics.get("funding") or {}).get("BTC")
        rate8 = C.funding_8h(btc) if btc else None
        if rate8 is not None and abs(rate8) >= C.FUNDING_EXTREME:
            out.append(_finding("amber", 18, f"Funding BTC {fmt.num(rate8 * 100, 3)} %: apalancamiento extremo"))
    return out


def _advice(level: str, blackout: dict, soon: dict | None, cfg: SimpleNamespace, no_data: bool) -> str:
    if level == "red" and blackout["active"]:
        after_min = cfg.after // 60
        when = f"{after_min} min después del dato" if after_min else "cuando se publique el dato"
        if APPROX_NOTE in str(blackout.get("event") or ""):
            when += "; la hora del dato es aproximada"
        return f"No abras posiciones nuevas hasta las {fmt.clock(blackout['until'], cfg.tz)} ({when})."
    if level == "red":
        return "Volatilidad extrema: no abras posiciones nuevas y protege las abiertas con su stop."
    if level == "amber" and soon is not None:  # a known release (e.g. an official date) still sets the window
        start = fmt.clock(int(soon["time"]) - cfg.before, cfg.tz)
        return f"Reduce el tamaño a la mitad y usa stops más amplios; no abras nada nuevo desde las {start}."
    if no_data:
        return "Sin datos macro: reduce el tamaño a la mitad y revisa el calendario antes de operar."
    if level == "amber":
        return "Reduce el tamaño a la mitad y usa stops más amplios."
    return GREEN_ADVICE


def _pick_events(rel: list[dict], now_s: int, cfg: SimpleNamespace) -> list[dict]:
    clusters = collapse([e for e in rel if e.get("impact") in ("high", "medium")])
    picked = [c for c in clusters if c["impact"] == "high"
              and -cfg.after <= int(c["time"]) - now_s <= HIGH_HORIZON_S][:MAX_EVENTS]
    if len(picked) < MAX_EVENTS:
        meds = [c for c in clusters if c["impact"] == "medium" and 0 <= int(c["time"]) - now_s <= MEDIUM_HORIZON_S]
        picked += meds[: MAX_EVENTS - len(picked)]
    picked.sort(key=lambda c: (int(c["time"]), -IMPACT_RANK.get(c["impact"], 0)))
    return [public_event(c, now_s) for c in picked]


def sim_conditions(scope: dict, now_s: int) -> dict:
    return {
        "level": "green", "label": LEVEL_LABEL["green"], "headline": "Mercado simulado: sin eventos macro",
        "advice": GREEN_ADVICE, "reasons": [], "scope": "sim", "symbol": scope.get("symbol"),
        "blackout": dict(NO_BLACKOUT), "next_event": None, "events": [], "metrics": [],
        "updated_at": int(now_s) * 1000, "stale": False,
    }


def assess(events: list[dict], metrics: dict | None, now_s: int, settings: dict | None, scope: dict | None = None,
           *, calendar_stale: bool = False) -> dict:
    """MarketConditions (types.ts) for `scope` at unix second `now_s`. `events` = merged calendar (any currency)."""
    scope = scope or global_scope()
    now_s = int(now_s)
    if scope.get("sim"):
        return sim_conditions(scope, now_s)
    cfg = settings_view(settings)
    metrics = metrics or {}
    currencies = set(scope.get("currencies") or cfg.currencies) | {"ALL"}
    rel = [e for e in events or [] if str(e.get("currency") or "").upper() in currencies]
    high = [e for e in rel if e.get("impact") == "high"]
    crypto = bool(scope.get("crypto"))

    findings, blackout, soon = event_findings(high, now_s, cfg)
    findings += metric_findings(metrics, cfg, crypto, now_s)
    quotes = metrics.get("quotes") or {}
    quotes_at = metrics.get("quotes_at")
    quotes_stale = not quotes or quotes_at is None or now_s * 1000 - int(quotes_at) > QUOTES_STALE_S * 1000
    no_data = bool(calendar_stale) and quotes_stale
    findings.sort(key=lambda f: (-LEVEL_RANK[f["level"]], f["prio"]))
    level = max((f["level"] for f in findings), key=LEVEL_RANK.__getitem__, default="green")
    if no_data and level == "green":
        level = "amber"

    reasons = [f["reason"] for f in findings][:MAX_REASONS]
    if no_data:
        # without the weekly feed nor fresh quotes, a calendar finding (official dates: it sets the window) or a red
        # one still leads; an amber reading of stale quotes does not (it stays behind "Sin datos macro")
        lead = next((f for f in findings if f["prio"] < METRIC_PRIO or f["level"] == "red"), None)
        headline = lead["headline"] if lead else NO_DATA_HEADLINE
    elif findings:
        headline = findings[0]["headline"]
    else:
        horizon = fmt.duration(cfg.warn)
        vix = safe_float((quotes.get("vix") or {}).get("value")) if not quotes_stale else None
        headline = f"Sin eventos de alto impacto en las próximas {horizon}"
        headline += f" y volatilidad normal (VIX {fmt.num(vix, 1)})." if vix is not None else "."
    next_clusters = [c for c in collapse(high) if int(c["time"]) - now_s >= -60]
    next_event = public_event(next_clusters[0], now_s) if next_clusters else None
    if level == "green" and next_event is not None:
        reasons.append(f"Próximo evento importante: {short_title(next_event['title_es'])} "
                       f"({fmt.day_clock(next_event['time'], now_s, cfg.tz)})")
    if calendar_stale and not no_data and len(reasons) < MAX_REASONS:
        reasons.append("Calendario semanal sin actualizar: solo se evalúan las fechas oficiales")
    if quotes_stale and not no_data and len(reasons) < MAX_REASONS:
        reasons.append("Sin datos de volatilidad: solo se evalúa el calendario")
    if no_data and len(reasons) < MAX_REASONS:
        reasons.append("Sin calendario semanal ni cotizaciones recientes: solo fechas oficiales")

    return {
        "level": level, "label": LEVEL_LABEL[level], "headline": headline,
        "advice": _advice(level, blackout, soon, cfg, no_data), "reasons": reasons[:MAX_REASONS],
        "scope": scope.get("id") or "global", "symbol": scope.get("symbol"), "blackout": blackout,
        "next_event": next_event, "events": _pick_events(rel, now_s, cfg),
        "metrics": C.build_cards(metrics, crypto=crypto, caution=cfg.vix_caution, danger=cfg.vix_danger,
                                 closed=us_session_closed(now_s), vix_closed=vix_session_closed(now_s)),
        "updated_at": now_s * 1000, "stale": bool(calendar_stale or quotes_stale),
    }
