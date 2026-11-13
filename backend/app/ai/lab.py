"""Strategy lab of the copilot (app.ai.tools `referencia_pychart` and `probar_estrategia`). Pure functions: no network,
no database (the run itself is ScriptService.run, in the PyChart sandbox).

- `reference_text()`: the PyChart reference (app.scripting.reference) compacted for the model's context: every
  signature, the documentation of the sections a strategy needs and one bundled example strategy without comments.
- `summarize_run(result, ...)`: what the model gets of a run of its script: the errors with their line (to fix them),
  or the backtest split by the candles into a development period (the first 70 %) and a check period (the rest).
  Each period compares its closed trades with random entries in the same period with the same direction, the same
  duration and the same costs (the commission and slippage of the script's strategy(...)): d = result − random
  reference per trade, with its 95 % interval by a block bootstrap by UTC day (the trades of one day share the
  market, so they are drawn together). The project's rules: nothing concluded below 30 trades nor when the interval
  includes 0; never "prueba" nor "garantía".
"""
from __future__ import annotations

import functools
import math
import re
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

from app.scripting.examples import EXAMPLES_DIR
from app.scripting.reference import REFERENCE
from app.validation import stats as S
from app.validation.verdict import MIN_TRADES

DEV_SHARE = 0.7  # development period: the first 70 % of the candles; check period: the last 30 %
RANDOM_DRAWS = 200  # random entries per trade of the strategy
BOOTSTRAP_RUNS = 2000
DOC_SECTIONS = ("Declaración", "Series de precios", "Entradas (input.*)", "Valores vacíos (NaN)",
                "Estrategia (backtest)", "Python en PyChart", "Noticias (calendario económico)")
DOC_MAX = 420
EXAMPLE_FILE = "ema_cross.py"
MAX_ERRORS = 3
TITLE_RE = re.compile(r"""^\s*(?:strategy|indicator)\(\s*(['"])(.+?)\1""", re.MULTILINE)

VERDICT_LABEL = {
    "none": "Sin operaciones cerradas",
    "small": "Muestra pequeña: sin conclusiones",
    "noci": "Sin intervalo: operaciones en muy pocos días distintos",
    "unclear": "Sin diferencia clara con entrar al azar",
    "above": "Por encima de entrar al azar en este periodo",
    "below": "Por debajo de entrar al azar en este periodo",
}
REFERENCE_NOTE = ("Referencia: entradas al azar en el mismo periodo, con la misma dirección, la misma duración y los "
                  "mismos costes (comisión y deslizamiento) que cada operación.")
NOTE = ("Si ajustas la estrategia mirando estos resultados, el periodo de comprobación deja de ser independiente: "
        "confírmala en otro mercado o temporalidad. Cada variante probada es otra oportunidad de salir «por encima» "
        "por suerte. Con menos de 30 operaciones no hay conclusiones. Son datos pasados: no es una garantía ni "
        "asesoría financiera.")
FIX_HINT = "Corrige el script y vuelve a probarlo; si dudas de una función, consulta referencia_pychart."


# ----------------------------------------------------------------------------- the reference for the model

def _clip_doc(text: Any) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= DOC_MAX:
        return t
    cut = t.rfind(". ", 0, DOC_MAX)
    return t[: cut + 1] if cut > 80 else t[: DOC_MAX - 1] + "…"


@functools.lru_cache(maxsize=1)
def example_script() -> str:
    """The bundled EMA-cross strategy without its comment lines (the model only needs the code)."""
    code = (EXAMPLES_DIR / EXAMPLE_FILE).read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = [ln for ln in code.split("\n") if not ln.lstrip().startswith("#")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@functools.lru_cache(maxsize=1)
def reference_text() -> str:
    """Every PyChart signature by section, the docs of the sections a strategy needs and an example strategy."""
    lines = ["PyChart: scripts de Python al estilo de Pine Script. Las series (open, high, low, close, volume, time) "
             "son arrays de numpy alineados con las velas: se opera con ellas de forma vectorizada."]
    for sec in REFERENCE.get("sections") or []:
        title = str(sec.get("title") or "")
        items = [i for i in sec.get("items") or [] if isinstance(i, dict)]
        if title.startswith("Colores"):
            lines.append(f"## {title}: " + ", ".join(str(i.get("signature")) for i in items))
            continue
        lines.append(f"## {title}")
        for item in items:
            doc = _clip_doc(item.get("doc")) if title in DOC_SECTIONS else ""
            lines.append(f"- {item.get('signature')}" + (f": {doc}" if doc else ""))
    lines.append("## Ejemplo completo de estrategia")
    lines.append(example_script())
    return "\n".join(lines)


def script_title(code: Any) -> str:
    """The title of its strategy(...) / indicator(...) declaration ("Estrategia" without one)."""
    m = TITLE_RE.search(str(code or ""))
    return m.group(2).strip()[:80] if m else "Estrategia"


# ----------------------------------------------------------------------------- the summary of a run

def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(v: Any, nd: int = 2) -> float | None:
    x = _num(v)
    return None if x is None else round(x, nd)


def _ci(ci: Sequence[float] | None) -> list[float | None] | None:
    return [_r(ci[0], 3), _r(ci[1], 3)] if ci else None


def _iso(ts: Any) -> str | None:
    x = _num(ts)
    return None if x is None else datetime.fromtimestamp(int(x), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def random_references(trades: Sequence[tuple[int, int]], opens: np.ndarray, closes: np.ndarray, lo: int, hi: int, *,
                      cost_pct: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """For each (side, bars) trade: the mean result (% of the position, after costs) of RANDOM_DRAWS random entries in
    the bars [lo, hi) with its direction and duration (entry at the open of a random bar j, exit at the close of bar
    j + bars − 1), and the share of them that won."""
    mean = np.full(len(trades), np.nan)
    win = np.full(len(trades), np.nan)
    span = hi - lo
    if span <= 0:
        return mean, win
    for k, (side, bars) in enumerate(trades):
        b = int(min(max(int(bars), 1), span))
        j = rng.integers(lo, hi - b + 1, size=RANDOM_DRAWS)
        entry = opens[j]
        ok = entry > 0
        if not ok.any():
            continue
        res = side * (closes[j + b - 1][ok] / entry[ok] - 1.0) * 100.0 - cost_pct
        mean[k] = float(res.mean())
        win[k] = float((res > 0).mean())
    return mean, win


def _verdict(n: int, ci: Sequence[float] | None) -> str:
    if n == 0:
        return "none"
    if n < MIN_TRADES:
        return "small"
    if not ci:
        return "noci"
    if ci[0] > 0:
        return "above"
    if ci[1] < 0:
        return "below"
    return "unclear"


def period_summary(name: str, rows: Sequence[tuple[int, int, int, float]], times: np.ndarray, opens: np.ndarray,
                   closes: np.ndarray, lo: int, hi: int, *, cost_pct: float, seed: int) -> dict[str, Any]:
    """One period [lo, hi) of the candles. rows: its closed trades as (entry_time, side ±1, bars, result %)."""
    n = len(rows)
    out: dict[str, Any] = {
        "periodo": name, "desde": _iso(times[lo]) if hi > lo else None, "hasta": _iso(times[hi - 1]) if hi > lo else None,
        "velas": max(hi - lo, 0), "operaciones": n,
        "comprar_y_mantener_pct": _r((closes[hi - 1] / opens[lo] - 1.0) * 100.0) if hi > lo and opens[lo] > 0 else None,
    }
    if n == 0:
        out.update({"veredicto": VERDICT_LABEL["none"], "veredicto_clave": "none"})
        return out
    pnl = np.array([r[3] for r in rows], dtype=float)
    ref, ref_win = random_references([(r[1], r[2]) for r in rows], opens, closes, lo, hi, cost_pct=cost_pct,
                                     rng=np.random.default_rng(seed))
    d = pnl - ref
    days = np.array([int(r[0]) // 86400 for r in rows])
    ci_pnl = S.block_bootstrap_mean_ci(pnl, days, runs=BOOTSTRAP_RUNS, seed=seed)
    ci_d = S.block_bootstrap_mean_ci(d, days, runs=BOOTSTRAP_RUNS, seed=seed + 1)
    key = _verdict(n, ci_d)
    wins = int((pnl > 0).sum())
    out.update({
        "ganadoras": f"{wins} de {n}",
        "ganadoras_al_azar_pct": _r(np.nanmean(ref_win) * 100.0, 1) if np.isfinite(ref_win).any() else None,
        "resultado_medio_pct": _r(pnl.mean(), 3),
        "ic95_resultado_pct": _ci(ci_pnl),
        "azar_medio_pct": _r(np.nanmean(ref), 3) if np.isfinite(ref).any() else None,
        "diferencia_con_azar_pct": _r(np.nanmean(d), 3) if np.isfinite(d).any() else None,
        "ic95_diferencia_pct": _ci(ci_d),
        "veredicto": VERDICT_LABEL[key],
        "veredicto_clave": key,
    })
    return out


def _errors(result: Any) -> list[dict[str, Any]]:
    raw = result.get("errors") if isinstance(result, dict) else None
    errs = [e for e in raw or [] if isinstance(e, dict)][:MAX_ERRORS]
    return [{"linea": e.get("line"), "columna": e.get("col"), "tipo": e.get("kind"),
             "mensaje": str(e.get("message") or "")[:400]} for e in errs] or [{"mensaje": "El script falló sin detalle."}]


def summarize_run(result: Any, *, symbol: str, tf: str, seed: int = 0) -> dict[str, Any]:
    """The model's view of a ScriptRunResult: its errors, or the strategy's backtest in two periods against random
    entries. The trades still open at the last candle are left out of the statistics."""
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "errores": _errors(result), "consejo": FIX_HINT}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    strat = result.get("strategy")
    if meta.get("kind") != "strategy" or not isinstance(strat, dict):
        return {"ok": False, "errores": [{"mensaje": "El script es un indicador: para probarlo como estrategia declara "
                                                    "strategy(...) y usa strategy.entry / exit / close."}],
                "consejo": FIX_HINT}
    candles = [c for c in result.get("candles") or [] if isinstance(c, dict)]
    if len(candles) < 2:
        return {"ok": False, "errores": [{"mensaje": "Sin velas suficientes para el backtest."}], "consejo": FIX_HINT}
    times = np.array([int(c["time"]) for c in candles], dtype=np.int64)
    opens = np.array([float(c["open"]) for c in candles], dtype=float)
    closes = np.array([float(c["close"]) for c in candles], dtype=float)
    params = strat.get("params") if isinstance(strat.get("params"), dict) else {}
    fee = _num(params.get("fee_pct")) or 0.0
    slip = _num(params.get("slippage_pct")) or 0.0
    trades = [t for t in strat.get("trades") or [] if isinstance(t, dict)]
    closed = [t for t in trades if t.get("exit_time") is not None and t.get("exit_reason") != "OPEN"
              and _num(t.get("pnl_pct")) is not None and _num(t.get("entry_time")) is not None]
    n = len(candles)
    split = min(max(int(n * DEV_SHARE), 1), n - 1)
    split_time = int(times[split])
    rows = [(int(t["entry_time"]), 1 if t.get("side") != "short" else -1, int(_num(t.get("bars")) or 1),
             float(t["pnl_pct"])) for t in closed]
    metrics = strat.get("metrics") if isinstance(strat.get("metrics"), dict) else {}
    wins = sum(1 for r in rows if r[3] > 0)
    cost = 2.0 * (fee + slip)
    out: dict[str, Any] = {
        "ok": True,
        "estrategia": meta.get("title"),
        "simbolo": symbol, "temporalidad": tf, "velas": n, "desde": _iso(times[0]), "hasta": _iso(times[-1]),
        "costes": {"comision_pct_por_lado": _r(fee, 4), "deslizamiento_pct": _r(slip, 4)},
        "total": {
            "operaciones": len(rows), "ganadoras": f"{wins} de {len(rows)}",
            "rentabilidad_pct": _r(metrics.get("total_return_pct")),
            "comprar_y_mantener_pct": _r(metrics.get("buy_hold_return_pct")),
            "max_caida_pct": _r(metrics.get("max_dd_pct")),
            "factor_beneficio": _r(metrics.get("profit_factor")),
            "abierta_al_final": len(trades) > len(closed),
        },
        "periodos": [
            period_summary("desarrollo (primer 70 % de las velas)", [r for r in rows if r[0] < split_time], times,
                           opens, closes, 0, split, cost_pct=cost, seed=seed),
            period_summary("comprobación (último 30 % de las velas)", [r for r in rows if r[0] >= split_time], times,
                           opens, closes, split, n, cost_pct=cost, seed=seed + 7),
        ],
        "referencia": REFERENCE_NOTE,
        "nota": NOTE,
    }
    stdout = str(result.get("stdout") or "").strip()
    if stdout:
        out["consola"] = stdout[:300]
    return out
