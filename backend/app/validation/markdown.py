"""Spanish Markdown tables of a ValidationReport (used by the CLI to write the tables of docs/VALIDATION.md)."""
from __future__ import annotations

import time
from typing import Any

from app.sim.common import fmt_num, fmt_pct
from app.validation.stats import MIN_CI_N
from app.validation.verdict import MIN_TRADES, UNCERTAIN_TEXT, ci_for, r2

CI_LEGEND = (f"IC 95 %: sin intervalo con menos de {MIN_CI_N} operaciones; «{UNCERTAIN_TEXT}» con menos de "
             f"{MIN_TRADES} (sin conclusiones por debajo de {MIN_TRADES}).")
DD_LEGEND = ("«DD por operaciones» es la caída máxima de la curva de operaciones cerradas en orden temporal, con el "
             "mismo riesgo por operación: la misma medida para NEXUS y para la mediana de las secuencias al azar. La "
             "cuenta se valora vela a vela (pérdidas latentes de la operación abierta, límite de apalancamiento), así "
             "que su caída no es comparable con la del azar; sí con la de comprar y mantener, también vela a vela.")


def _date(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def _num(x: Any, digits: int = 2) -> str:
    return fmt_num(x, digits) if isinstance(x, (int, float)) else "—"


def _pct(x: Any, digits: int = 1) -> str:
    return fmt_pct(x, digits) if isinstance(x, (int, float)) else "—"


def _dd(x: Any) -> str:
    return fmt_pct(x, 1, signed=False) if isinstance(x, (int, float)) else "—"


GATE_LABEL = {"better": "mejora", "worse": "empeora", "unclear": "sin evidencia de efecto",
              "insufficient": "muestra insuficiente"}


def _band(band: Any) -> str:
    return f"{r2(band[0])} / {r2(band[1])} / {r2(band[2])}" if isinstance(band, list) and len(band) == 3 else "—"


def _row(cells: list[Any]) -> str:
    return "| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |"


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    """A GFM table preceded by a blank line (some renderers need it after a heading)."""
    return ["", _row(headers), _row(["---"] * len(headers)), *(_row(r) for r in rows)]


def report_markdown(rep: dict) -> str:
    series = rep.get("series") or []
    out: list[str] = []
    out.append("### Datos")
    out += _table(["Serie", "Velas", "Desde", "Hasta", "Prueba desde", "Huecos", "Comisión/lado"],
                  [[f"{s['symbol']} {s['tf']}", s["bars"], _date(s["start_time"]), _date(s["end_time"]),
                    _date(s["test_start_time"]), "—" if s.get("gaps") is None else s["gaps"],
                    f"{_num(s['fee_pct'], 3)} %"] for s in series])
    out.append("")
    out.append("### NEXUS + filtro frente a las referencias (R por operación, después de costes)")
    rows = []
    for s in series:
        o, ov = s["overall"]["nexus"], s["overall"]
        rnd = ov["random"]
        rows.append([f"{s['symbol']} {s['tf']}", f"{o['wins']} de {o['trades']}", r2(o.get("expectancy_r")),
                     ci_for(o.get("ci95"), o["trades"]), _num(o.get("profit_factor")),
                     f"{r2(ov['ungated'].get('expectancy_r'))} ({ov['ungated']['trades']})",
                     r2(ov["mechanical"].get("expectancy_r")), _band(rnd.get("expectancy_band")),
                     _num(rnd.get("percentile"), 0), _num(rnd.get("percentile_mechanical"), 0),
                     s["verdict"]["label"]])
    out += _table(["Serie", "Ganadas", "Expectativa", "IC 95 %", "PF", "Sin filtro (ops)", "Mismas salidas",
                   "Azar p5 / p50 / p95", "Percentil", "Percentil mismas salidas", "Veredicto"], rows)
    out += ["", CI_LEGEND]
    out.append("")
    out.append("### NEXUS sin filtro frente a su propio azar (mismo número de operaciones y mezcla largo/corto)")
    rows = []
    for s in series:
        u, ru = s["overall"]["ungated"], s["overall"].get("random_ungated") or {}
        vu = s.get("verdict_ungated") or {}
        rows.append([f"{s['symbol']} {s['tf']}", f"{u['wins']} de {u['trades']}", r2(u.get("expectancy_r")),
                     ci_for(u.get("ci95"), u["trades"]), _num(u.get("profit_factor")), _band(ru.get("expectancy_band")),
                     _num(ru.get("percentile"), 0),
                     f"{s.get('folds_positive_ungated', 0)} de {s.get('folds_with_trades_ungated', 0)}",
                     vu.get("label", "—")])
    out += _table(["Serie", "Ganadas", "Expectativa", "IC 95 %", "PF", "Azar p5 / p50 / p95", "Percentil",
                   "Periodos positivos", "Veredicto"], rows)
    out.append("")
    out.append("### Cuenta simulada (1 % de riesgo por operación) y comprar y mantener")
    rows = []
    for s in series:
        o, bh, rnd = s["overall"]["nexus"], s["overall"]["buy_hold"], s["overall"]["random"]
        rows.append([f"{s['symbol']} {s['tf']}", _pct(o.get("return_pct")), _dd(o.get("max_dd_pct")),
                     _num(o.get("sharpe")), _dd(o.get("trade_max_dd_pct") if o.get("trades") else None),
                     _dd(rnd.get("trade_max_dd_p50")), _pct(bh.get("return_pct")), _dd(bh.get("max_dd_pct")),
                     _num(bh.get("sharpe"))])
    out += _table(["Serie", "Retorno de la cuenta", "DD de la cuenta (vela a vela)", "Sharpe de la cuenta",
                   "DD por operaciones NEXUS", "DD por operaciones azar (mediana)", "Comprar y mantener",
                   "DD comprar y mantener", "Sharpe comprar y mantener"], rows)
    out += ["", DD_LEGEND]
    out.append("")
    out.append("### Periodos walk-forward (expectativa de NEXUS + filtro · operaciones)")
    width = max((len(s.get("folds") or []) for s in series), default=0)
    rows = []
    for s in series:
        cells = [f"{s['symbol']} {s['tf']}"]
        for f in s.get("folds") or []:
            n = f["nexus"]
            cells.append(f"{r2(n.get('expectancy_r'))} · {n['trades']}" if n["trades"] else "— · 0")
        cells += [""] * (width - len(s.get("folds") or []))
        cells.append(f"{s.get('folds_positive', 0)} de {s.get('folds_with_trades', 0)}")
        rows.append(cells)
    out += _table(["Serie", *[f"P{i + 1}" for i in range(width)], "Periodos positivos"], rows)
    out.append("")
    out.append("### Efecto del filtro «¿Tomo esta operación?»")
    def gate_ci(ge: dict) -> str:
        return ci_for(ge.get("ci95"), min(int(ge.get("trades_gated") or 0), int(ge.get("trades_ungated") or 0)))

    out += _table(["Serie", "Con filtro", "Sin filtro", "Diferencia", "IC 95 %", "Lectura"],
                  [[f"{s['symbol']} {s['tf']}", f"{r2(s['overall']['nexus'].get('expectancy_r'))} "
                    f"({s['gate_effect']['trades_gated']})", f"{r2(s['overall']['ungated'].get('expectancy_r'))} "
                    f"({s['gate_effect']['trades_ungated']})", r2(s["gate_effect"].get("diff_r")),
                    gate_ci(s["gate_effect"]),
                    GATE_LABEL.get(s["gate_effect"]["code"], s["gate_effect"]["code"])] for s in series])
    out.append("")
    out.append("### Conjunto (intervalos por bloques de tiempo entre activos)")
    out += _table(["Grupo", "Series", "Activos (≈ independientes)", "ρ medio diario", "Operaciones", "Expectativa",
                   "IC 95 % por bloques", "IC 95 % si fueran independientes", "Exceso sobre azar (IC 95 %)",
                   "Series positivas", "Veredicto"],
                  [[g["label"], g["series"], f"{g['assets']} (≈ {_num(g.get('effective_assets'), 1)})",
                    _num(g.get("avg_correlation")), g["trades"], r2(g.get("expectancy_r")),
                    ci_for(g.get("ci95_blocks"), g["trades"]), ci_for(g.get("ci95_iid"), g["trades"]),
                    f"{r2(g.get('excess_r'))} ({ci_for(g.get('excess_ci95'), g['trades'])})",
                    f"{g['positive_series']} de {g['series']}", g["verdict"]["label"]] for g in rep.get("groups") or []])
    out.append("")
    out.append("### Conjunto, NEXUS sin filtro")
    rows = []
    for g in rep.get("groups") or []:
        u = g.get("ungated") or {}
        n_u = u.get("trades", 0)
        rows.append([g["label"], n_u, r2(u.get("expectancy_r")), ci_for(u.get("ci95_blocks"), n_u),
                     ci_for(u.get("ci95_iid"), n_u), f"{r2(u.get('excess_r'))} ({ci_for(u.get('excess_ci95'), n_u)})",
                     f"{u.get('positive_series', 0)} de {g['series']}", (u.get("verdict") or {}).get("label", "—")])
    out += _table(["Grupo", "Operaciones", "Expectativa", "IC 95 % por bloques", "IC 95 % si fueran independientes",
                   "Exceso sobre azar (IC 95 %)", "Series positivas", "Veredicto"], rows)
    out.append("")
    out.append("### Entradas frente a salidas (mismas entradas con las salidas mecánicas; IC 95 % por bloques)")
    rows = []
    for g in rep.get("groups") or []:
        for name, v in (("con filtro", g), ("sin filtro", g.get("ungated") or {})):
            n_v = v.get("trades", 0)
            rows.append([g["label"], name, n_v, r2(v.get("expectancy_r")), r2(v.get("mechanical_r")),
                         f"{r2(v.get('mechanical_excess_r'))} ({ci_for(v.get('mechanical_excess_ci95'), n_v)})",
                         f"{r2(v.get('exit_effect_r'))} ({ci_for(v.get('exit_effect_ci95'), n_v)})"])
    out += _table(["Grupo", "NEXUS", "Operaciones", "R con sus salidas", "R con salidas mecánicas",
                   "Entradas sobre el azar (mismas salidas)", "Efecto de las salidas del modelo"], rows)
    errors = rep.get("errors") or []
    if errors:
        out.append("")
        out.append("### Series sin validar")
        out += [f"- {e.get('symbol')} {e.get('tf')}: {e.get('message')}" for e in errors]
    return "\n".join(out) + "\n"
