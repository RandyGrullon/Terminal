"""Semáforo alerts (MacroAlert in types.ts), deduplicated:

- level changes of the global semáforo (escalations always; the same level is not re-announced within 10 min,
  so a VIX hovering around a threshold does not flap);
- T−60, T−15 and T−0 minutes before each high-impact event of the alert currencies (setting macro.currencies);
- the end of a no-trade window (blackout).
`AlertTracker.check()` is pure given its inputs and the injected `now_s`, so it is tested with a fake clock.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.macro import fmt
from app.macro.conditions_events import collapse, event_name

MARKS_MIN = (60, 15, 0)
LEVEL_COOLDOWN_S = 600
EVENT_NOW_GRACE_S = 300  # T−0 alert still raised up to 5 min after the release (loop jitter, restarts)
KEEP_S = 3 * 86400
LEVEL_RANK = {"green": 0, "amber": 1, "red": 2}
DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}


def alert(kind: str, level: str, message: str) -> dict:
    return {"kind": kind, "level": level, "message": message}


class AlertTracker:
    def __init__(self) -> None:
        self.sent: dict[str, int] = {}
        self.level: str | None = None
        self.blackout: dict | None = None

    def _once(self, key: str, now_s: int) -> bool:
        if key in self.sent:
            return False
        self.sent[key] = now_s
        return True

    def _prune(self, now_s: int) -> None:
        for key in [k for k, t in self.sent.items() if now_s - t > KEEP_S]:
            del self.sent[key]

    # ------------------------------------------------------------------ level

    def _level_alert(self, cond: dict, now_s: int, cfg: SimpleNamespace) -> dict | None:
        new, old = cond["level"], self.level
        self.level = new
        if new == old or (old is None and new == "green"):
            return None
        key = f"level:{new}"
        last = self.sent.get(key)
        escalation_to_red = new == "red" and old != "red"
        if last is not None and now_s - last < LEVEL_COOLDOWN_S and not escalation_to_red:
            return None
        self.sent[key] = now_s
        rising = old is None or LEVEL_RANK[new] > LEVEL_RANK[old]
        nxt = cond.get("next_event")
        event_driven = cond["blackout"]["active"] or (nxt is not None and nxt["countdown_s"] <= cfg.warn)
        kind = "volatility" if rising and new != "green" and not event_driven else "level"
        if new == "green":
            message = f"{DOT[new]} {cond['label']}: {cond['headline']}"
        else:
            message = f"{DOT[new]} {cond['label']}: {cond['headline']} {cond['advice']}"
        return alert(kind, new, message)

    # ------------------------------------------------------------------ events

    def _event_alerts(self, events: list[dict], now_s: int, cfg: SimpleNamespace,
                      currencies: set[str]) -> list[tuple[str, dict]]:
        """(event name, alert) for every mark reached since the last check."""
        out: list[tuple[str, dict]] = []
        wanted = [e for e in events if e.get("impact") == "high" and str(e.get("currency")) in currencies
                  and -EVENT_NOW_GRACE_S <= int(e["time"]) - now_s <= MARKS_MIN[0] * 60]
        for c in collapse(wanted):
            t, name = int(c["time"]), event_name(c)
            delta = t - now_s
            mark = self._mark(delta)
            if mark is None or not self._once(f"{c['id']}:{mark}", now_s):
                continue
            # Older marks of the same event are implied once a later one fired (no burst after a restart).
            for m in MARKS_MIN:
                if m > mark:
                    self.sent.setdefault(f"{c['id']}:{m}", now_s)
            out.append((name, self._event_message(c, name, mark, delta, cfg)))
        return out

    @staticmethod
    def _mark(delta: int) -> int | None:
        """The latest mark already reached: 60 when T−60 ≥ delta > T−15, 15 until T−0, 0 up to 5 min after."""
        if delta < -EVENT_NOW_GRACE_S:
            return None
        for m in reversed(MARKS_MIN):  # 0, 15, 60
            if delta <= m * 60:
                return m
        return None

    @staticmethod
    def _event_message(c: dict, name: str, mark: int, delta: int, cfg: SimpleNamespace) -> dict:
        t = int(c["time"])
        if mark == 0:
            end = fmt.clock(t + cfg.after, cfg.tz)
            return alert("event_now", "red", f"🔴 {name}: se publica ahora. No abras posiciones nuevas hasta las {end}.")
        when = fmt.until(delta)
        if delta <= cfg.before:
            return alert("event_soon", "red", f"🔴 {name} {when}: no abras posiciones nuevas.")
        start = fmt.clock(t - cfg.before, cfg.tz)
        return alert("event_soon", "amber",
                     f"🟡 {name} {when}: prepárate, desde las {start} no abras posiciones nuevas.")

    # ------------------------------------------------------------------ blackout end

    def _blackout_alert(self, cond: dict, now_s: int) -> dict | None:
        prev, cur = self.blackout, cond["blackout"]
        self.blackout = dict(cur) if cur["active"] else None
        if prev is None or cur["active"]:
            return None
        if not self._once(f"blackout_end:{prev.get('until')}", now_s):
            return None
        event = fmt_event(prev.get("event"))
        level = cond["level"]
        tail = "puedes volver a operar con normalidad." if level == "green" else f"semáforo en {cond['label'].lower()}."
        return alert("blackout_end", level, f"{DOT[level]} Terminó la ventana sin operar{event}: {tail}")

    # ------------------------------------------------------------------ entry point

    def check(self, cond: dict, events: list[dict], now_s: int, cfg: SimpleNamespace,
              currencies: set[str] | None = None) -> list[dict]:
        """Alerts to raise now for the global conditions `cond` and the merged calendar `events`."""
        now_s = int(now_s)
        self._prune(now_s)
        out: list[dict] = []
        lvl = self._level_alert(cond, now_s, cfg)
        end = self._blackout_alert(cond, now_s)
        if end is not None and lvl is not None and lvl["level"] == end["level"]:
            lvl = None  # "the window ended" already tells the new level
        if lvl is not None:
            out.append(lvl)
        if end is not None:
            out.append(end)
        for name, item in self._event_alerts(events, now_s, cfg, set(currencies or cfg.currencies)):
            if lvl is not None and lvl["level"] != "green" and name in lvl["message"]:
                continue  # the level alert already announces this event (e.g. app started mid-countdown)
            out.append(item)
        return out


def fmt_event(title: str | None) -> str:
    return f" ({title})" if title else ""
