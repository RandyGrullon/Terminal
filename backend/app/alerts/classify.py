"""Bus event -> external alert (pure): which events may reach Telegram / email, their category, priority and a short
Spanish line, plus the text of the message that carries one or several alerts.

Forwarded events (anything else is ignored):
- semáforo (source 'macro', data.alert = MacroAlert): level changes, T−60 / T−15 / T−0 of high-impact events and the
  end of a no-trade window. red -> critical, amber -> warn, green -> info.
- NEXUS watcher (source 'nexus'): entries BUY / SELL and exits EXIT_LONG / EXIT_SHORT (level 'signal') -> warn; early
  exits (level 'warn', data.code EARLY_EXIT) -> critical. Synthetic markets (SIM:*) are never forwarded.
- demo account (source 'paper', level 'trade'; never the simulator's source 'sim' / account 2): account 1 (the
  user's) in category 'paper', account 3 (NEXUS automática, NEXUS's own account) in category 'autopilot' (opt-in, off
  by default). Opened and closed -> warn, stops (SL / trailing) -> critical when the trade lost money, else warn;
  partial targets -> info. The text always says "simulado".
- any module can opt in with data.notify = {category: macro|signals|paper|autopilot, priority?: info|warn|critical,
  kind?, text?} (or True: category from the source, priority from the level).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

CATEGORIES = ("macro", "signals", "paper", "autopilot")
PRIORITIES = ("info", "warn", "critical")
PRIORITY_RANK = {"info": 0, "warn": 1, "critical": 2}
CATEGORY_LABEL = {"macro": "Semáforo de mercado", "signals": "Señales NEXUS", "paper": "Cuenta demo",
                  "autopilot": "NEXUS automática"}
CATEGORY_INLINE = {"macro": "semáforo de mercado", "signals": "señales NEXUS", "paper": "cuenta demo",
                   "autopilot": "NEXUS automática"}  # mid-sentence
PRIORITY_LABEL = {"info": "Todos", "warn": "Importantes y críticos", "critical": "Solo críticos"}
MAX_LINE = 600
BATCH_LINE = 280  # per alert in a message with several (12 × 280 + header and footer < Telegram's 4096)

MACRO_PRIORITY = {"red": "critical", "amber": "warn", "green": "info"}
# Alerts that announce the semáforo's current state form one dedupe group: a new state replaces the previous one, so
# red -> amber -> red within the dedupe window still announces the second red (Policy.supersede).
MACRO_STATE_KINDS = ("level", "volatility", "blackout_end")
MACRO_STATE_GROUP = "macro|state"
SIGNAL_ACTIONS = {
    "BUY": ("entry", "📈"),
    "SELL": ("entry", "📉"),
    "EXIT_LONG": ("exit", "⏹"),
    "EXIT_SHORT": ("exit", "⏹"),
}
STOP_REASONS = ("SL", "TRAIL")
DEMO = "Cuenta demo (simulado)"
AUTOPILOT = "NEXUS automática (simulado)"
AUTOPILOT_ACCOUNT = 3  # NEXUS's own demo account (app.sim.paper.AUTOPILOT_ACCOUNT; this module stays pure)
AUTOPILOT_PREFIX = "NEXUS automática · "  # the paper engine's prefix of that account's events
SOURCE_CATEGORY = {"macro": "macro", "nexus": "signals", "paper": "paper"}
LEVEL_PRIORITY = {"error": "critical", "warn": "warn", "signal": "warn", "trade": "warn"}
_SPACES = re.compile(r"\s+")

# Last line of a message, by category (singular, plural).
FOOTER = {
    "macro": ("semáforo de mercado", "semáforo de mercado"),
    "signals": ("señal del modelo, no es asesoría financiera", "señales del modelo, no son asesoría financiera"),
    "paper": ("operación simulada de la cuenta demo, sin dinero real",
              "operaciones simuladas de la cuenta demo, sin dinero real"),
    "autopilot": ("operación simulada de la cuenta propia de NEXUS, sin dinero real; la validación publicada no "
                  "encontró ventaja",
                  "operaciones simuladas de la cuenta propia de NEXUS, sin dinero real; la validación publicada no "
                  "encontró ventaja"),
}


@dataclass
class Alert:
    category: str  # macro | signals | paper | autopilot
    kind: str  # e.g. macro_event_soon, entry, exit, early_exit, opened, closed, stop, partial
    priority: str  # info | warn | critical
    text: str  # one Spanish line, emoji first
    key: str  # dedupe key
    symbol: str | None = None
    tf: str | None = None
    event_id: int | None = None
    ts: int = 0  # event time (unix ms)
    notes: list[str] = field(default_factory=list)  # extra details appended to the line (e.g. delayed data)
    group: str | None = None  # a newer alert of the same group replaces this one in the dedupe memory

    def line(self) -> str:
        return " · ".join([self.text, *self.notes]) if self.notes else self.text


def one_line(text: Any, limit: int = MAX_LINE) -> str:
    s = _SPACES.sub(" ", str(text or "")).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def rank(priority: str) -> int:
    return PRIORITY_RANK.get(priority, 0)


# ----------------------------------------------------------------------------- classification


def classify(ev: Any) -> Alert | None:
    """The external alert carried by a bus event, or None when the event is not forwarded."""
    if not isinstance(ev, dict):
        return None
    data = ev.get("data")
    if not isinstance(data, dict):
        return None
    source = str(ev.get("source") or "")
    if data.get("notify"):
        return _opt_in(ev, source, data)
    if source == "macro":
        return _macro(ev, data)
    if source == "nexus":
        return _nexus(ev, data)
    if source == "paper":
        return _paper(ev, data)
    return None


def _base(ev: dict) -> dict:
    eid = ev.get("id")
    ts = _num(ev.get("ts"))
    return {"event_id": eid if isinstance(eid, int) else None, "ts": int(ts) if ts is not None else 0}


def _macro(ev: dict, data: dict) -> Alert | None:
    item = data.get("alert")
    if not isinstance(item, dict):
        return None  # other semáforo events (provider errors...) are not alerts
    level = str(item.get("level") or "")
    kind = str(item.get("kind") or "level")
    text = one_line(item.get("message") or ev.get("message"))
    if level not in MACRO_PRIORITY or not text:
        return None
    group = MACRO_STATE_GROUP if kind in MACRO_STATE_KINDS else None
    return Alert("macro", f"macro_{kind}", MACRO_PRIORITY[level], text, f"macro|{kind}|{text}", group=group,
                 **_base(ev))


def _nexus(ev: dict, data: dict) -> Alert | None:
    symbol = str(data.get("symbol") or "").strip().upper()
    tf = str(data.get("tf") or "").strip()
    if not symbol or not tf or symbol.startswith("SIM:"):
        return None
    text = one_line(ev.get("message"))
    if not text:
        return None
    level = ev.get("level")
    group = f"nexus|{symbol}|{tf}"  # the latest signal of a market replaces its previous one (BUY -> EXIT -> BUY)
    if level == "signal":
        action = str(data.get("action") or "")
        if action not in SIGNAL_ACTIONS:
            return None
        kind, emoji = SIGNAL_ACTIONS[action]
        return Alert("signals", kind, "warn", f"{emoji} {text}", f"nexus|{symbol}|{tf}|{action}",
                     symbol=symbol, tf=tf, group=group, **_base(ev))
    if level == "warn" and data.get("code") == "EARLY_EXIT":
        line = text if text.startswith("⚠") else f"⚠ {text}"
        return Alert("signals", "early_exit", "critical", line, f"nexus|{symbol}|{tf}|EARLY_EXIT",
                     symbol=symbol, tf=tf, group=group, **_base(ev))
    return None


def _paper(ev: dict, data: dict) -> Alert | None:
    if ev.get("level") != "trade":
        return None
    account = data.get("account_id")
    if account is None or account == 1:
        category, label, mark = "paper", DEMO, "🧪"
    elif account == AUTOPILOT_ACCOUNT:
        category, label, mark = "autopilot", AUTOPILOT, "🤖"
    else:
        return None  # account 2 belongs to the simulator
    text = one_line(ev.get("message"))
    if category == "autopilot" and text.startswith(AUTOPILOT_PREFIX):
        text = text[len(AUTOPILOT_PREFIX):]  # the label says whose trade it is
    if not text:
        return None
    pid = data.get("position_id")
    symbol = str(data.get("symbol") or "").strip().upper() or None
    base = _base(ev)
    if "reason" in data:
        reason = str(data.get("reason") or "")
        if reason in STOP_REASONS:
            pnl = _num(data.get("pnl"))
            priority = "critical" if pnl is not None and pnl < 0 else "warn"
            return Alert(category, "stop", priority, f"🛑 {label}: {text}", f"{category}|{pid}|stop|{text}",
                         symbol=symbol, **base)
        return Alert(category, "closed", "warn", f"🏁 {label}: {text}", f"{category}|{pid}|closed|{text}",
                     symbol=symbol, **base)
    if "side" in data and symbol:
        return Alert(category, "opened", "warn", f"{mark} {label}: {text}", f"{category}|{pid}|opened|{text}",
                     symbol=symbol, **base)
    return Alert(category, "partial", "info", f"🎯 {label}: {text}", f"{category}|{pid}|partial|{text}",
                 symbol=symbol, **base)


def _opt_in(ev: dict, source: str, data: dict) -> Alert | None:
    spec = data.get("notify")
    if spec is True:
        spec = {}
    if not isinstance(spec, dict):
        return None
    category = str(spec.get("category") or SOURCE_CATEGORY.get(source) or "")
    if category not in CATEGORIES:
        return None
    priority = str(spec.get("priority") or "")
    if priority not in PRIORITIES:
        priority = LEVEL_PRIORITY.get(str(ev.get("level")), "info")
    text = one_line(spec.get("text") or ev.get("message"))
    if not text:
        return None
    if category == "paper" and "simulad" not in text.lower():
        text = f"{DEMO}: {text}"
    elif category == "autopilot" and "simulad" not in text.lower():
        text = f"{AUTOPILOT}: {text}"
    symbol = str(data.get("symbol") or "").strip().upper() or None
    kind = one_line(spec.get("kind") or "custom", 40)
    return Alert(category, kind, priority, text, f"notify|{source}|{kind}|{text}", symbol=symbol,
                 tf=str(data.get("tf") or "") or None, **_base(ev))


def collapse(alerts: list[Alert]) -> list[Alert]:
    """Within one batch: an exit and an early exit of the same market are one alert (the critical early exit)."""
    early = {(a.symbol, a.tf) for a in alerts if a.kind == "early_exit"}
    return [a for a in alerts if not (a.kind == "exit" and (a.symbol, a.tf) in early)]


# ----------------------------------------------------------------------------- messages


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def footer(alerts: list[Alert]) -> str:
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a.category] = counts.get(a.category, 0) + 1
    parts = [FOOTER[c][0 if n == 1 else 1] for c, n in counts.items() if c in FOOTER]
    return "— NEXUS · " + " · ".join(parts) if parts else "— NEXUS"


def compose(alerts: list[Alert], notes: list[str] | tuple[str, ...] = ()) -> tuple[str, str]:
    """(text, subject) of one message with `alerts` (most severe first) and extra notes before the footer."""
    ordered = sorted(alerts, key=lambda a: -rank(a.priority))
    if len(ordered) == 1:
        lines = [ordered[0].line()]
        subject = f"NEXUS · {one_line(ordered[0].line(), 110)}"
    else:  # shorter lines, so a full batch stays within one Telegram message
        lines = [f"NEXUS · {plural(len(ordered), 'aviso', 'avisos')}"] + [one_line(a.line(), BATCH_LINE)
                                                                          for a in ordered]
        subject = f"NEXUS · {plural(len(ordered), 'aviso', 'avisos')}: {one_line(ordered[0].line(), 80)}"
    lines += [n for n in notes if n]
    lines.append(footer(ordered))
    return "\n".join(lines), subject


def top_priority(alerts: list[Alert]) -> str:
    return max((a.priority for a in alerts), key=rank, default="info")
