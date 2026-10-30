"""Calendar rules of the semáforo: blackout around high-impact events (chained when windows overlap), the
'event soon' warning window and the residual-volatility window after an event. Pure functions."""
from __future__ import annotations

from types import SimpleNamespace

from app.macro import fmt
from app.macro.calendar import IMPACT_RANK
from app.macro.titles import short_title

POST_EVENT_S = 2 * 3600  # amber up to 2 h after a high-impact event
# Keywords that make an event the headline of its cluster (several releases at the same minute).
KEY_TITLES = (
    "Federal Funds Rate", "FOMC Statement", "Main Refinancing Rate", "Official Bank Rate", "BOJ Policy Rate",
    "Overnight Rate", "Cash Rate", "SNB Policy Rate", "Non-Farm Employment Change", "Core CPI", "CPI",
    "Core PCE", "GDP", "Press Conference", "Unemployment Rate", "Retail Sales", "ISM", "PMI",
)
COMPANION_WORDS = ("Statement", "Report", "Projections", "Votes", "Summary")


def importance(ev: dict) -> tuple[int, int]:
    title = str(ev.get("title") or "")
    key = next((len(KEY_TITLES) - i for i, k in enumerate(KEY_TITLES) if k in title), 0)
    return IMPACT_RANK.get(ev.get("impact"), 0), key


def _companion(ev: dict) -> bool:
    """Documents published together with a rate decision (statement, report...) are not separate data."""
    title = str(ev.get("title") or "")
    return any(w in title for w in COMPANION_WORDS)


def collapse(events: list[dict]) -> list[dict]:
    """One representative per (currency, minute) cluster, the most important one, with 'more' = how many other
    data releases of the cluster it hides (companion documents not counted). Sorted by time."""
    clusters: dict[tuple[str, int], list[dict]] = {}
    for ev in events:
        clusters.setdefault((str(ev.get("currency") or ""), int(ev["time"]) // 60), []).append(ev)
    out = []
    for group in clusters.values():
        best = max(group, key=importance)
        rep = dict(best)
        rep["more"] = sum(1 for e in group if e is not best and not _companion(e))
        out.append(rep)
    out.sort(key=lambda e: (int(e["time"]), -IMPACT_RANK.get(e.get("impact"), 0)))
    return out


def event_name(cluster: dict) -> str:
    name = short_title(str(cluster.get("title_es") or cluster.get("title") or ""))
    more = int(cluster.get("more") or 0)
    if more == 1:
        return f"{name} y 1 dato más"
    if more > 1:
        return f"{name} y {more} datos más"
    return name


def _finding(level: str, prio: int, reason: str, headline: str) -> dict:
    return {"level": level, "prio": prio, "reason": reason, "headline": headline}


def blackout_until(clusters: list[dict], in_window: list[dict], cfg: SimpleNamespace) -> int:
    """End of the no-trade window, extended while the next event's window overlaps it (FOMC 14:00 + 14:30)."""
    until = max(int(c["time"]) + cfg.after for c in in_window)
    changed = True
    while changed:
        changed = False
        for c in clusters:
            t = int(c["time"])
            if t - cfg.before <= until < t + cfg.after:
                until = t + cfg.after
                changed = True
    return until


def event_findings(high: list[dict], now_s: int, cfg: SimpleNamespace) -> tuple[list[dict], dict, dict | None]:
    """(findings, blackout, next 'soon' cluster) for the high-impact events relevant to the scope."""
    clusters = collapse(high)
    findings: list[dict] = []
    blackout = {"active": False, "until": None, "event": None}
    until: int | None = None
    in_window = [c for c in clusters if -cfg.after <= int(c["time"]) - now_s <= cfg.before]
    if in_window:
        until = blackout_until(clusters, in_window, cfg)
        upcoming = [c for c in in_window if int(c["time"]) - now_s > -60]
        main = upcoming[0] if upcoming else in_window[-1]
        name, delta = event_name(main), int(main["time"]) - now_s
        if delta > 30:
            reason = f"{name} {fmt.until(delta)}"
            headline = f"{reason}: espera a que pase la volatilidad."
        elif delta > -60:
            reason = f"{name}: se publica ahora"
            headline = f"{name} se publica ahora: no operes hasta que pase la volatilidad."
        else:
            reason = f"{name} {fmt.until(delta)}"
            headline = f"{reason}: el mercado aún está digiriendo el dato."
        findings.append(_finding("red", 0, reason, headline))
        blackout = {"active": True, "until": until, "event": main.get("title_es") or main.get("title")}
    soon_list = [
        c for c in clusters
        if cfg.before < int(c["time"]) - now_s <= cfg.warn and (until is None or int(c["time"]) - cfg.before > until)
    ]
    soon = soon_list[0] if soon_list else None
    if soon is not None:
        reason = f"{event_name(soon)} {fmt.until(int(soon['time']) - now_s)}"
        findings.append(_finding("amber", 1, reason, f"{reason}: prepárate y no entres justo antes del dato."))
    if not blackout["active"]:
        recent = [c for c in clusters if -POST_EVENT_S <= int(c["time"]) - now_s < -cfg.after]
        if recent:
            c = recent[-1]
            reason = f"{event_name(c)} {fmt.until(int(c['time']) - now_s)}: volatilidad residual"
            findings.append(_finding("amber", 2, reason, f"{reason}, opera con cautela."))
    return findings, blackout, soon
