"""Trade journal of NEXUS automática (account 3): what each trade was like at its entry and what happened to it, and
the groups of its closed trades by cause, session, regime, news, filter score, side and market, as «x de n» with the
mean R and its 95 % interval. It describes, it does not promise: no group is called positive or negative below 30
trades or while its interval includes zero (CLAUDE.md rule 2).

The engine (app.sim.paper) stores `meta.context` when its trader opens a trade (NEXUS's regime, score and P(up) and the
gate's score at that bar) and `meta.journal` when it closes (`close_entry`: how far it went against and in favour, in
R, and the cause). The session and the distance to the principal US news (CPI, employment report, FOMC: official dates
of app.macro.history and the forward schedules) are computed from the entry time, so older trades get them too.
Pure: no network, no database.
"""
from __future__ import annotations

import bisect
import datetime as dt
from collections import Counter
from functools import lru_cache
from typing import Any, Callable, Iterable

import numpy as np

from app.engine.costs import asset_class
from app.engine.fmt import REGIME_LABEL
from app.macro import fomc, history, schedules
from app.macro.fomc import NEW_YORK
from app.util import safe_float
from app.validation import stats as S
from app.validation.verdict import MIN_TRADES, ci_text, r2

NEWS_NEAR_MIN = 60  # an entry within ±60 minutes of a principal US release counts as «near news»
BOOTSTRAP_RUNS = 2000
MAX_INSIGHTS = 6
NEXUS_EXITS = ("EARLY", "REVERSE", "SIGNAL", "TIME")

SESSION_LABEL = {
    "asia": "Asia (18–3 h de Nueva York)",
    "londres": "Londres (3–8 h)",
    "nueva_york": "Nueva York (8–17 h)",
    "pausa": "Entre sesiones (17–18 h)",
    "fin_de_semana": "Fin de semana",
}
CAUSE_LABEL = {
    "objetivo": "Llegó a su objetivo",
    "parcial_equilibrio": "Cobró un objetivo y salió en equilibrio",
    "stop_dinamico": "Stop dinámico con ganancia",
    "salida_nexus_ganancia": "NEXUS la cerró con ganancia",
    "en_contra": "En contra desde el principio",
    "stop_tras_favor": "Fue a favor, pero tocó el stop",
    "devolvio": "Ganaba 1R o más y lo devolvió",
    "salida_nexus_perdida": "NEXUS la cerró antes, en pérdida",
    "vencimiento": "Vencimiento de la opción",
    "manual": "Cerrada a mano",
    "otra": "Otra salida",
}
NEWS_LABEL = {"cerca": f"A menos de {NEWS_NEAR_MIN} min de una noticia principal", "lejos": "Lejos de noticias"}
SIDE_LABEL = {"long": "Largo (apuesta al alza)", "short": "Corto (apuesta a la baja)"}
MARKET_LABEL = {"crypto": "Cripto", "stock": "Acciones", "fund": "Fondos y ETF", "futures": "Futuros", "option": "Opciones",
                "forex": "Forex", "commodity": "Materias primas", "index": "Índices", "cfd": "CFD", "bond": "Bonos",
                "other": "Otros"}
GATE_LABEL = {"80": "Filtro 80 o más", "70": "Filtro 70–79", "bajo": "Filtro por debajo de 70"}
UNKNOWN = "Sin datos"
VERDICT_LABEL = {
    "none": "Sin operaciones",
    "small": "Muestra pequeña",
    "unclear": "Sin diferencia clara",
    "loses": "Negativo con evidencia",
    "wins": "Positivo con evidencia",
}
NOTE = (f"Diario de las operaciones cerradas de NEXUS automática (simuladas). «x de n» = ganadoras de n; el R lleva "
        "comisiones. Intervalo del 95 % por bootstrap por días (las operaciones del mismo día no cuentan como "
        f"independientes); ningún grupo se califica con menos de {MIN_TRADES} operaciones ni cuando su intervalo "
        "incluye el cero. Describe lo que pasó: no garantiza lo que pasará.")


# ----------------------------------------------------------------------------- one trade

def session_of(ts_s: int) -> str:
    """Market session of a moment, by New York time: Asia 18–3 h, Londres 3–8 h, Nueva York 8–17 h, entre sesiones
    17–18 h; from Friday 17 h to Sunday 18 h, «fin de semana» (only crypto trades then)."""
    t = dt.datetime.fromtimestamp(int(ts_s), NEW_YORK)
    wd, h = t.weekday(), t.hour + t.minute / 60.0
    if wd == 5 or (wd == 6 and h < 18) or (wd == 4 and h >= 17):
        return "fin_de_semana"
    if h >= 18 or h < 3:
        return "asia"
    if h < 8:
        return "londres"
    if h < 17:
        return "nueva_york"
    return "pausa"


@lru_cache(maxsize=1)
def _news_times() -> tuple[int, ...]:
    """Unix seconds of every principal US release known: the past official dates (history.py) and the forward
    schedules (BLS CPI and employment at 08:30 New York, FOMC decision and press conference)."""
    times = {int(e["time"]) for e in history.history_events()}
    for day in tuple(schedules.BLS_CPI) + tuple(schedules.BLS_EMPLOYMENT):
        times.add(schedules.local_time(day, 8, 30, NEW_YORK))
    times.update(int(e["time"]) for e in fomc.fomc_events(None, None))
    return tuple(sorted(times))


def news_minutes(ts_s: int) -> float | None:
    """Signed minutes from the nearest principal US release to `ts_s` (negative: the release came before), None when
    `ts_s` is outside the calendar's coverage (history.from_s() to the end of the forward schedules)."""
    t = int(ts_s)
    if t < history.from_s() or t > history.until_s():
        return None
    times = _news_times()
    i = bisect.bisect_left(times, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(times):
            d = (t - times[j]) / 60.0
            if best is None or abs(d) < abs(best):
                best = d
    return best


def excursions(side: str, entry: Any, risk_unit: Any, high_water: Any, low_water: Any,
               exit_price: Any = None) -> tuple[float | None, float | None]:
    """(MAE, MFE) in R: how far the price went against the trade and in its favour (≥ 0), from the water marks of its
    management; the exit price counts too (the bar that hit the stop does not move the water marks)."""
    ru, e = safe_float(risk_unit), safe_float(entry)
    hi, lo = safe_float(high_water), safe_float(low_water)
    if not ru or ru <= 0 or e is None or hi is None or lo is None:
        return None, None
    x = safe_float(exit_price)
    if x is not None:
        hi, lo = max(hi, x), min(lo, x)
    if side == "short":
        mfe, mae = (e - lo) / ru, (hi - e) / ru
    else:
        mfe, mae = (hi - e) / ru, (e - lo) / ru
    return round(max(mae, 0.0), 4), round(max(mfe, 0.0), 4)


def cause_of(r: Any, reason: Any, mfe: float | None) -> str:
    """Why the trade ended as it did (CAUSE_LABEL keys), from its R, exit reason and favourable excursion."""
    rr, why = safe_float(r), str(reason or "").upper()
    if rr is None:
        return "otra"
    if why == "EXPIRY":
        return "vencimiento"
    if why == "MANUAL":
        return "manual"
    if rr > 0:
        if why.startswith("TP"):
            return "objetivo"
        if why == "TRAIL":
            return "stop_dinamico"
        if why == "SL":
            return "parcial_equilibrio"
        return "salida_nexus_ganancia" if why in NEXUS_EXITS else "otra"
    if mfe is not None and mfe >= 1.0:
        return "devolvio"
    if why in ("SL", "TRAIL"):
        return "en_contra" if mfe is None or mfe < 0.25 else "stop_tras_favor"
    return "salida_nexus_perdida" if why in NEXUS_EXITS else "otra"


def close_entry(*, side: str, entry: Any, risk_unit: Any, high_water: Any, low_water: Any, exit_price: Any, r: Any,
                reason: Any, entry_time: Any, exit_time: Any) -> dict[str, Any]:
    """`meta.journal` of a trade that just closed: MAE / MFE in R, its cause and how long it was held."""
    mae, mfe = excursions(side, entry, risk_unit, high_water, low_water, exit_price)
    held = None
    if safe_float(entry_time) is not None and safe_float(exit_time) is not None:
        held = max(int(float(exit_time)) - int(float(entry_time)), 0)
    return {"mae_r": mae, "mfe_r": mfe, "cause": cause_of(r, reason, mfe), "held_s": held}


# ----------------------------------------------------------------------------- the journal

def _bet(t: dict) -> str:
    inst = t.get("instrument") if isinstance(t.get("instrument"), dict) else {}
    if inst.get("kind") == "option":  # its bet on the stock: a call bets on a rise, a put on a fall
        return "long" if inst.get("right") == "call" else "short"
    return "short" if t.get("side") == "short" else "long"


def _news_key(t: dict) -> str | None:
    m = news_minutes(int(t["entry_time"])) if safe_float(t.get("entry_time")) is not None else None
    if m is None:
        return None
    return "cerca" if abs(m) <= NEWS_NEAR_MIN else "lejos"


def _gate_key(t: dict) -> str | None:
    g = safe_float(t.get("gate_score"))
    if g is None:
        g = safe_float((t.get("context") or {}).get("gate_score"))
    if g is None:
        return None
    return "80" if g >= 80 else "70" if g >= 70 else "bajo"


def _regime_key(t: dict) -> str | None:
    reg = (t.get("context") or {}).get("regime")
    return str(reg) if isinstance(reg, str) and reg and reg != "unknown" else None


def _cause_key(t: dict) -> str:
    j = t.get("journal") if isinstance(t.get("journal"), dict) else {}
    cause = j.get("cause")
    return cause if cause in CAUSE_LABEL else cause_of(t.get("r"), t.get("exit_reason"), safe_float(j.get("mfe_r")))


# dimension -> (title, key of a trade, label of a key)
DIMENSIONS: tuple[tuple[str, str, Callable[[dict], str | None], Callable[[str], str]], ...] = (
    ("cause", "Causa", _cause_key, lambda k: CAUSE_LABEL.get(k, k)),
    ("session", "Sesión de entrada", lambda t: session_of(int(t["entry_time"])), lambda k: SESSION_LABEL.get(k, k)),
    ("regime", "Régimen de NEXUS", _regime_key, lambda k: REGIME_LABEL.get(k, k)),
    ("news", "Noticias de EE. UU.", _news_key, lambda k: NEWS_LABEL.get(k, k)),
    ("gate", "Puntuación del filtro", _gate_key, lambda k: GATE_LABEL.get(k, k)),
    ("side", "Dirección", _bet, lambda k: SIDE_LABEL.get(k, k)),
    ("market", "Mercado", lambda t: asset_class(str(t.get("symbol") or "")), lambda k: MARKET_LABEL.get(k, k)),
)


def _verdict(n: int, ci: tuple[float, float] | None) -> str:
    if n <= 0:
        return "none"
    if n < MIN_TRADES:
        return "small"
    if ci is None:
        return "unclear"
    if ci[1] < 0:
        return "loses"
    if ci[0] > 0:
        return "wins"
    return "unclear"


def _stats(trades: list[dict], seed: int) -> dict[str, Any]:
    r = np.array([float(t["r"]) for t in trades], dtype=float)
    n = int(r.size)
    ci = None
    if n >= S.MIN_CI_N:
        days = np.array([int(t["exit_time"]) // 86400 for t in trades])
        ci = S.block_bootstrap_mean_ci(r, days, runs=BOOTSTRAP_RUNS, seed=seed)
    verdict = _verdict(n, ci)
    return {"n": n, "wins": int((r > 0).sum()), "mean_r": float(r.mean()) if n else None,
            "ci95": [float(ci[0]), float(ci[1])] if ci else None, "verdict": verdict,
            "verdict_label": VERDICT_LABEL[verdict]}


def _mean(values: Iterable[float | None]) -> float | None:
    v = [float(x) for x in values if x is not None]
    return float(np.mean(v)) if v else None


def _insights(trades: list[dict], groups: list[dict]) -> list[str]:
    n = len(trades)
    if n == 0:
        return ["Aún no hay operaciones cerradas de NEXUS automática que analizar."]
    out: list[str] = []
    if n < MIN_TRADES:
        out.append(f"Con {n} {'operación cerrada' if n == 1 else 'operaciones cerradas'} todavía no se puede concluir "
                   f"nada: hacen falta al menos {MIN_TRADES}.")
    losers = [t for t in trades if float(t["r"]) <= 0]
    if len(losers) >= 3:
        cause, k = Counter(_cause_key(t) for t in losers).most_common(1)[0]
        out.append(f"{k} de {len(losers)} pérdidas: «{CAUSE_LABEL.get(cause, cause)}».")
    gave_back = sum(1 for t in trades if _cause_key(t) == "devolvio")
    if gave_back >= 2:
        out.append(f"{gave_back} operaciones llegaron a ir 1R o más a favor y acabaron en pérdida o en cero.")
    lose_mfe = _mean((t.get("journal") or {}).get("mfe_r") for t in losers)
    win_mae = _mean((t.get("journal") or {}).get("mae_r") for t in trades if float(t["r"]) > 0)
    if lose_mfe is not None and win_mae is not None and len(losers) >= 5:
        out.append(f"Antes de cerrar, las perdedoras fueron de media {r2(lose_mfe)} a favor y las ganadoras "
                   f"{r2(-win_mae)} en contra: sirve para revisar stops y objetivos, no es una regla.")
    news = next((g for g in groups if g["dimension"] == "news"), None)
    near = next((b for b in (news or {}).get("buckets", []) if b["key"] == "cerca"), None)
    if near and near["n"] >= 3:
        out.append(f"Cerca de noticias (±{NEWS_NEAR_MIN} min): {near['wins']} de {near['n']} ganadoras, R medio "
                   f"{r2(near['mean_r'])}.")
    for g in groups:
        for b in g["buckets"]:
            if b["verdict"] in ("loses", "wins"):
                side = "bajo cero" if b["verdict"] == "loses" else "sobre cero"
                out.append(f"{g['label']} «{b['label']}»: R medio {r2(b['mean_r'])}, IC 95 % {ci_text(b['ci95'])} "
                           f"({side}) en {b['n']} operaciones.")
    return out[:MAX_INSIGHTS]


def analyze(trades: Iterable[dict], *, seed: int = 0) -> dict[str, Any]:
    """AutopilotJournal (without generated_at) of closed trades = [{r, side, symbol, entry_time, exit_time,
    exit_reason, gate_score, context, journal, instrument}] (r = R net of fees)."""
    rows = [t for t in trades if safe_float(t.get("r")) is not None and safe_float(t.get("exit_time")) is not None
            and safe_float(t.get("entry_time")) is not None]
    total = _stats(rows, seed)
    groups = []
    for dim, title, key_of, label_of in DIMENSIONS:
        buckets: dict[str, list[dict]] = {}
        for t in rows:
            key = key_of(t)
            buckets.setdefault(key if key else "unknown", []).append(t)
        items = []
        for k, ts in buckets.items():
            st = _stats(ts, S.seed_for(seed, dim, k))
            items.append({"key": k, "label": UNKNOWN if k == "unknown" else label_of(k), **st})
        items.sort(key=lambda b: (b["key"] == "unknown", -b["n"], b["label"]))
        groups.append({"dimension": dim, "label": title, "buckets": items})
    losers = [t for t in rows if float(t["r"]) <= 0]
    winners = [t for t in rows if float(t["r"]) > 0]
    return {
        **{k: total[k] for k in ("wins", "mean_r", "ci95", "verdict", "verdict_label")},
        "trades": total["n"],
        "mae_r_losers": _mean((t.get("journal") or {}).get("mae_r") for t in losers),
        "mfe_r_losers": _mean((t.get("journal") or {}).get("mfe_r") for t in losers),
        "mae_r_winners": _mean((t.get("journal") or {}).get("mae_r") for t in winners),
        "mfe_r_winners": _mean((t.get("journal") or {}).get("mfe_r") for t in winners),
        "groups": groups,
        "insights": _insights(rows, groups),
        "note": NOTE,
    }
