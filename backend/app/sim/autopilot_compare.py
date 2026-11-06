"""NEXUS automática frente a entrar al azar: an honest, paired comparison of its closed trades with random entries in
the same markets and moments, with the method of the published validation (docs/VALIDATION.md, app.validation).

Reference of each closed trade of NEXUS's own account (account 3, origin 'autopilot'): the mean R of the random
entries decided at each of the REF_WINDOW_BARS bars up to its own decision bar, long and short, on the same symbol
and timeframe, with NEXUS's default levels and the validation's mechanical exits (app.validation.mechanics: SL
first, partial targets with break-even, chandelier trail, time stop), the asset class's commission and the demo
account's slippage. NEXUS's own trades keep NEXUS's model exits (early exit, reverse), which random entries cannot
have: the validation's main comparison does the same.

Statistics: d = R(NEXUS) − R(reference) per trade; its 95 % interval is a block bootstrap by UTC day of the decisions
(the trades of one day share the market, so they are drawn together, never counted as independent evidence). Verdicts
follow app.validation.verdict: nothing is concluded below 30 compared trades nor without an interval, and "por encima
/ por debajo de entrar al azar" needs the whole interval on one side of 0. Never "prueba" nor "garantía".

The pure part (`references`, `summarize`) touches no network nor database; `compare` reads the trades from the
database and the candles from rt.hub.
"""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from typing import Any, Sequence

import numpy as np

from app.engine.costs import fee_pct_for
from app.engine.features import WARMUP_BARS, compute_features
from app.engine.nexus import NexusParams
from app.util import floor_time, now_ms, now_s, safe_float, tf_seconds
from app.validation import stats as S
from app.validation.mechanics import random_entry_table
from app.validation.verdict import MIN_TRADES, NO_EDGE_LABEL, NOT_A_GUARANTEE, ci_for, ci_text, r2

REF_WINDOW_BARS = 48  # random entries at each of the 48 bars up to a decision (the engine's time stop span)
MIN_REF_ENTRIES = 10  # a trade's reference needs this many random entries with a known outcome
MAX_TRADES = 200  # newest closed trades compared
MAX_BARS = 5000  # candles per market and request (app.data.hub.MAX_LIMIT)
BOOTSTRAP_RUNS = 2000
VERDICT_LABEL = {
    "none": "Sin datos",
    "insufficient": "Aún sin evidencia",
    "no_edge": NO_EDGE_LABEL,
    "better": "Por encima de entrar al azar",
    "worse": "Por debajo de entrar al azar",
}
NOTE = (f"Referencia de cada operación: entradas al azar, en largo y en corto, en las {REF_WINDOW_BARS} velas anteriores "
        "a su decisión, en el mismo mercado y temporalidad, con los niveles por defecto de NEXUS, sus salidas mecánicas "
        "(stop, objetivos parciales, stop dinámico y por tiempo), comisiones y deslizamiento; las operaciones de NEXUS "
        "conservan sus salidas del modelo. Intervalo del 95 % por bootstrap por días. Es una medición sobre operaciones "
        "simuladas, no una garantía; la validación publicada (docs/VALIDATION.md) no encontró ventaja. Las "
        "operaciones con opciones no entran: su R se mide sobre una prima teórica, no sobre el mercado.")


def decision_bar(entry_time: int, tf: str) -> int:
    """The closed bar NEXUS's trader decided on (its open time): the last bar already closed at the entry."""
    return floor_time(int(entry_time), tf) - tf_seconds(tf)


def references(f: Any, times: Sequence[int], decisions: Sequence[int], params: Any, *, fee_pct: float,
               slippage_pct: float, window: int = REF_WINDOW_BARS) -> list[tuple[float, int, int] | None]:
    """Per decision bar (open time): (mean R, winning entries, entries) of the random entries decided at the `window`
    bars up to it (long and short), or None when that bar is not in the data or too few outcomes are known."""
    t = np.asarray(times, dtype=np.int64)
    first = int(getattr(f, "warmup", 0)) + 1
    idx: list[int | None] = []
    for d in decisions:
        i = int(np.searchsorted(t, int(d)))
        idx.append(i if i < t.size and int(t[i]) == int(d) and i >= first else None)
    found = [i for i in idx if i is not None]
    if not found:
        return [None] * len(idx)
    lo = max(min(found) - int(window), first)
    table = random_entry_table(f, lo, max(found) + 1, params, fee_pct=fee_pct, slippage_pct=slippage_pct)
    out: list[tuple[float, int, int] | None] = []
    for i in idx:
        if i is None:
            out.append(None)
            continue
        a = max(i - int(window), lo)
        vals = np.concatenate([table[1][a:i + 1], table[-1][a:i + 1]])
        vals = vals[np.isfinite(vals)]
        out.append(None if vals.size < MIN_REF_ENTRIES else (float(vals.mean()), int((vals > 0).sum()), int(vals.size)))
    return out


def _verdict(total: int, n: int, days: int, diff: float | None, ci: Sequence[float] | None) -> tuple[str, str]:
    if n == 0:
        if total == 0:
            return "none", "Aún no hay operaciones cerradas de NEXUS automática que comparar."
        return "none", (f"Ninguna de sus {total} operaciones cerradas tiene referencia todavía: faltan las velas de su "
                        "mercado o el resultado de las entradas al azar aún no se conoce.")
    trades = f"{n} {'operación comparada' if n == 1 else 'operaciones comparadas'}"
    if n < MIN_TRADES:
        return "insufficient", (f"Aún sin evidencia: con {trades} no se puede concluir nada (hacen falta "
                                f"{MIN_TRADES}). Por ahora, {r2(diff)} por operación frente a entrar al azar "
                                f"({ci_for(ci, n)}).")
    if not ci:
        return "insufficient", (f"Aún sin evidencia: sus {trades} se reparten en {days} "
                                f"{'día' if days == 1 else 'días'}; hacen falta al menos {S.MIN_BLOCKS} para un "
                                "intervalo que no cuente como independientes las operaciones del mismo día.")
    lo, hi = float(ci[0]), float(ci[1])
    if lo > 0:
        return "better", (f"En este periodo, NEXUS automática ganó de media {r2(abs(diff)).lstrip('+')} por operación "
                          "más que entrar al "
                          f"azar en los mismos mercados (IC 95 %: {ci_text(ci)}, {trades}). {NOT_A_GUARANTEE} La "
                          "validación publicada no encontró ventaja: un buen periodo no la demuestra.")
    if hi < 0:
        return "worse", (f"En este periodo, NEXUS automática quedó por debajo de entrar al azar: {r2(diff)} por "
                         f"operación (IC 95 %: {ci_text(ci)}, {trades}).")
    return "no_edge", (f"Sin evidencia de ventaja: la diferencia con entrar al azar ({r2(diff)} por operación, IC 95 %: "
                       f"{ci_text(ci)}, {trades}) incluye el cero.")


def summarize(rows: Sequence[dict], *, seed: int = 0) -> dict[str, Any]:
    """AutopilotComparison (without generated_at) of `rows` = [{r: R of NEXUS's trade, ref: (mean R, winning entries,
    entries) of its random entries or None, day: UTC day of its decision}]."""
    paired = [x for x in rows if x.get("ref") is not None and safe_float(x.get("r")) is not None
              and math.isfinite(float(x["r"]))]
    n = len(paired)
    r = np.array([float(x["r"]) for x in paired], dtype=float)
    ref = np.array([float(x["ref"][0]) for x in paired], dtype=float)
    d = r - ref
    days = [int(x["day"]) for x in paired]
    n_days = len(set(days))
    diff = float(d.mean()) if n else None
    ci = None
    if n >= S.MIN_CI_N:
        ci = S.block_bootstrap_mean_ci(d, np.asarray(days), runs=BOOTSTRAP_RUNS, seed=seed)
    verdict, text = _verdict(len(rows), n, n_days, diff, ci)
    return {
        "window_bars": REF_WINDOW_BARS,
        "trades": len(rows),
        "compared": n,
        "missing": len(rows) - n,
        "days": n_days,
        "nexus": {"n": n, "wins": int((r > 0).sum()), "mean_r": float(r.mean()) if n else None},
        "random": {"entries": int(sum(x["ref"][2] for x in paired)), "wins": int(sum(x["ref"][1] for x in paired)),
                   "mean_r": float(ref.mean()) if n else None},
        "diff_r": diff,
        "ci95": [float(ci[0]), float(ci[1])] if ci else None,
        "verdict": verdict,
        "verdict_label": VERDICT_LABEL[verdict],
        "text": text,
        "note": NOTE,
    }


async def compare(rt: Any, account_id: int, *, settings: dict | None = None, slippage_pct: float = 0.0,
                  limit: int = MAX_TRADES) -> dict[str, Any]:
    """The comparison of the newest `limit` closed trades of NEXUS's own trader (account `account_id`, origin
    'autopilot'). Markets whose candles do not cover a trade leave it without a reference (counted in `missing`)."""
    rows = await rt.db.fetchall(
        "SELECT id, symbol, tf, entry_time, r_multiple FROM paper_positions WHERE account_id=? AND status='closed' "
        "AND origin='autopilot' AND r_multiple IS NOT NULL AND symbol NOT LIKE 'OPT:%' ORDER BY id DESC LIMIT ?",
        (int(account_id), int(limit)))
    trades = [dict(row) for row in rows if safe_float(row["r_multiple"]) is not None]
    base = dict(settings or {})
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        t["tf"] = str(t.get("tf") or "1h")
        t["decision"] = decision_bar(int(t["entry_time"]), t["tf"])
        t["ref"] = None
        groups[(str(t["symbol"]), t["tf"])].append(t)
    now = now_s()
    for (symbol, tf), items in groups.items():
        tf_s = tf_seconds(tf)
        oldest = min(t["decision"] for t in items) - (REF_WINDOW_BARS + WARMUP_BARS + 5) * tf_s
        want = int(min(MAX_BARS, max((now - oldest) // tf_s + 2, 100)))
        hub = getattr(rt, "hub", None)
        if hub is None:
            continue
        try:
            cs = await asyncio.wait_for(hub.get_candles(symbol, tf, want), timeout=45)
        except Exception:
            continue
        if cs is None or len(cs) == 0:
            continue
        closed = int(np.searchsorted(np.asarray(cs.time, dtype=np.int64), now - tf_s, side="right"))
        cs = cs.slice(0, closed)  # closed bars only: the forming one is not a known outcome
        if len(cs) <= WARMUP_BARS + 2:
            continue
        fee = float(fee_pct_for(symbol, base))
        params = NexusParams.from_settings({**base, "risk.fee_pct": fee}, gate_enabled=True)

        def work(cs=cs, items=items, params=params, fee=fee):
            f = compute_features(cs)
            return references(f, cs.time, [t["decision"] for t in items], params, fee_pct=fee,
                              slippage_pct=slippage_pct)

        try:
            refs = await asyncio.to_thread(work)
        except Exception:
            continue
        for t, ref in zip(items, refs):
            t["ref"] = ref
    out = summarize([{"r": float(t["r_multiple"]), "ref": t["ref"], "day": int(t["decision"]) // 86400}
                     for t in trades], seed=S.seed_for("autopilot", len(trades)))
    out["generated_at"] = now_ms()
    return out
