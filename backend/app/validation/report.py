"""ValidationReport assembly (frontend/src/api/types-validation.ts): series results + groups + honest summary.

The stored / served report is JSON-safe, rounded (4 decimals) and free of the internal per-trade keys ("_*").
"""
from __future__ import annotations

import math
from typing import Any

from app.sim.common import fmt_num
from app.util import clean_json
from app.validation.aggregate import aggregate
from app.validation.stats import MIN_CI_N
from app.validation.verdict import EDGE_PERCENTILE, MIN_TRADES
from app.validation.walkforward import MIN_FOLD_BARS, WFConfig

REPORT_VERSION = 1
DISCLAIMER = ("Validación con datos históricos y operaciones simuladas: no garantiza resultados futuros. No es asesoría "
              "financiera.")


def method_lines(cfg: WFConfig) -> list[str]:
    return [
        "NEXUS se ejecuta de forma causal sobre todo el historial: aprende en línea y cada decisión solo usa las velas "
        "cerradas hasta ese momento; el filtro parte de sus pesos iniciales (sin lo aprendido en vivo). Todas las "
        "decisiones son fuera de muestra.",
        f"Tras {fmt_num(cfg.warmup_bars, 0)} velas de calentamiento, las decisiones se reparten en {cfg.folds} "
        f"periodos consecutivos (walk-forward; menos si la serie es corta: cada periodo tiene al menos {MIN_FOLD_BARS} "
        "velas de decisión); cada operación cuenta en el periodo de su vela de decisión.",
        "Mismas reglas de riesgo en todas las variantes: niveles de NEXUS (stop, objetivos 1-3 con cierres parciales, "
        "stop en equilibrio y dinámico), cierre por tiempo, comisión de la clase de activo y deslizamiento de "
        f"{fmt_num(cfg.slippage_pct, 2)} % por ejecución.",
        "Referencias: NEXUS sin filtro, comprar y mantener, y entradas al azar (Monte Carlo de "
        f"{fmt_num(cfg.random_runs, 0)} secuencias con las mismas operaciones por periodo, la misma mezcla largo/corto "
        "y las salidas mecánicas). «Mismas salidas» repite las entradas de NEXUS con esas salidas mecánicas. NEXUS sin "
        "filtro se compara con su propio azar (su número de operaciones y su mezcla largo/corto): con más operaciones, "
        "dice si la señal de NEXUS por sí sola tiene ventaja.",
        "Expectativa = R medio por operación después de costes, con intervalo del 95 % por bootstrap (sin intervalo "
        f"con menos de {MIN_CI_N} operaciones; con menos de {MIN_TRADES} es muy incierto); percentil = posición de "
        "NEXUS dentro de las secuencias al azar.",
        "Caídas máximas: la cuenta y comprar y mantener se valoran vela a vela; la comparación con el azar usa la "
        "caída de las operaciones cerradas en orden temporal, la misma medida para NEXUS y para el azar.",
        f"«Indicios de ventaja» solo con {MIN_TRADES} operaciones o más, intervalo por encima de 0, percentil "
        f"{EDGE_PERCENTILE:g} o más y resultado positivo en la mayoría de los periodos (con operaciones en al menos "
        "dos). En conjunto, los intervalos se "
        "calculan por bloques de tiempo entre activos, porque las criptomonedas se mueven juntas.",
        "Con muchas series, alguna puede salir favorable por pura casualidad (comparaciones múltiples): un resultado "
        "aislado no basta.",
    ]


def round_floats(obj: Any, digits: int = 4) -> Any:
    if isinstance(obj, float):
        return round(obj, digits) if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: round_floats(v, digits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_floats(v, digits) for v in obj]
    return obj


def public_series(series: dict) -> dict:
    return {k: v for k, v in series.items() if not k.startswith("_")}


def _pick(d: Any, keys: tuple[str, ...]) -> dict:
    return {k: d.get(k) for k in keys} if isinstance(d, dict) else {}


def compact_report(report: dict) -> dict:
    """A smaller copy for the repository (docs/validation/): every overall figure, verdict and group, with each
    walk-forward period reduced to its key numbers."""
    out = dict(report)
    series = []
    for s in report.get("series") or []:
        row = dict(s)
        row["folds"] = [{
            **_pick(f, ("index", "start_time", "end_time", "bars")),
            "nexus": _pick(f.get("nexus"), ("trades", "wins", "expectancy_r")),
            "ungated": _pick(f.get("ungated"), ("trades", "wins", "expectancy_r")),
            "buy_hold": _pick(f.get("buy_hold"), ("return_pct",)),
            "random": _pick(f.get("random"), ("expectancy_r", "percentile")),
        } for f in s.get("folds") or []]
        series.append(row)
    out["series"] = series
    out["compact"] = True
    return round_floats(out, 3)


def summary_for(series: list[dict], groups: list[dict], errors: list[dict]) -> dict[str, Any]:
    """Headline verdict: the only series when there is one, else the "all" group (or the only timeframe group)."""
    if len(series) == 1:
        return {"scope": f"{series[0]['symbol']} {series[0]['tf']}", **series[0]["verdict"]}
    if groups:
        top = groups[-1]
        return {"scope": top["key"], **top["verdict"]}
    reason = errors[0]["message"] if errors else "no se validó ninguna serie"
    return {"scope": "none", "code": "insufficient", "label": "Sin evidencia de ventaja",
            "text": f"Sin evidencia de ventaja: no hay resultados ({reason})."}


def build_report(series: list[dict], errors: list[dict], *, request: dict, config: WFConfig, started_at: int,
                 finished_at: int, source: str = "app") -> dict[str, Any]:
    """Full ValidationReport (JSON-safe). `started_at` / `finished_at` in unix ms."""
    groups = aggregate(series, runs=config.bootstrap_runs, seed=config.seed)
    report = {
        "version": REPORT_VERSION,
        "source": source,
        "started_at": int(started_at),
        "generated_at": int(finished_at),
        "duration_s": max(0.0, (int(finished_at) - int(started_at)) / 1000.0),
        "request": request,
        "config": config.to_dict(),
        "summary": summary_for(series, groups, errors),
        "groups": groups,
        "series": [public_series(s) for s in series],
        "errors": errors,
        "method": method_lines(config),
        "disclaimer": DISCLAIMER,
    }
    return round_floats(clean_json(report))
