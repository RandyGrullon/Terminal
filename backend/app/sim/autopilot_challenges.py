"""NEXUS automática's challenge accounts («cuentas de reto»): extra simulated accounts, each with its own balance, a
target equity, an optional deadline and a loss brake, traded by the same brain as account 3.

They share account 3's candidates and NEXUS's closed-bar signals (and the PyChart strategies it follows), but each one
sizes its trades with its own equity, in the aggressive mode (app.sim.autopilot_goal.AGGRESSIVE: 5m/15m/1h, 4 % per
trade in MT5 lots, 20x), with its own positions, portfolio-risk limits (its daily loss limit is its brake), decision log
and result. The paper engine ends each one by itself: target reached, deadline, or the brake hit with nothing left
open. Then it closes its trades and keeps the result. They have no buy-and-hold portfolio and no broker mirror. They
pause with account 3 (they trade its candidates). Nothing here is validated: a target does not create an edge.

Pure helpers only (validation, the saved record, the view). The engine side lives in app.sim.paper.
"""
from __future__ import annotations

from typing import Any

from app.sim.common import fmt_num
from app.util import safe_float

CHALLENGES_KEY = "autopilot.challenges"  # meta: {id: record} of every challenge account
FIRST_ID = 101  # their paper_accounts ids (1-3 are the fixed accounts)
MAX_RUNNING = 12
STATUSES = ("running", "reached", "time", "brake", "cancelled")
DEFAULT_BRAKE_PCT = 50.0
MIN_INITIAL = 1.0
MAX_INITIAL = 10_000_000.0
MAX_MULT = 100.0  # a target at most 100 times the initial balance
MAX_UNTIL_S = 90 * 86400
MIN_UNTIL_S = 60
NOTE = ("Cada cuenta de reto opera con el mismo cerebro que NEXUS automática (sus candidatos y señales), en modo "
        "agresivo y con su propio saldo: el tamaño de cada operación sale de su capital, así que una cuenta pequeña solo "
        "cabe en monedas baratas (el lote mínimo de BTC u oro arriesga demasiado). Un reto no crea ventaja: la "
        "validación no la encontró. Simulado, sin dinero real; no es asesoría financiera.")


def money(v: float) -> str:
    """'10 $' / '1.000 $' / '12,50 $' (no decimals when whole)."""
    v = float(v)
    return f"{fmt_num(v, 0 if abs(v - round(v)) < 1e-9 else 2)} $"


def default_name(initial: float, target: float) -> str:
    return f"Reto {money(initial)} → {money(target)}"


def parse_challenge(initial: Any, target: Any, until: Any, brake_pct: Any, name: Any, *, now: int,
                    taken: set[str]) -> dict[str, Any]:
    """A new challenge from the user's values (ValueError in Spanish on a bad one)."""
    ini = safe_float(initial)
    if ini is None or not (MIN_INITIAL <= ini <= MAX_INITIAL):
        raise ValueError(f"El saldo inicial debe estar entre {money(MIN_INITIAL)} y {money(MAX_INITIAL)}")
    tgt = safe_float(target)
    if tgt is None or tgt <= ini:
        raise ValueError("La meta debe ser mayor que el saldo inicial")
    if tgt > ini * MAX_MULT:
        raise ValueError(f"La meta puede ser como mucho {fmt_num(MAX_MULT, 0)} veces el saldo inicial "
                         f"({money(ini * MAX_MULT)})")
    deadline: int | None = None
    if until not in (None, "", 0):
        u = safe_float(until)
        if u is None:
            raise ValueError("La hora límite no es válida")
        if u > 1e12:  # milliseconds
            u /= 1000.0
        deadline = int(u)
        if deadline < now + MIN_UNTIL_S:
            raise ValueError("La hora límite tiene que ser al menos 1 minuto más tarde que ahora")
        if deadline > now + MAX_UNTIL_S:
            raise ValueError("La hora límite puede ser como mucho dentro de 90 días")
    brake = DEFAULT_BRAKE_PCT if brake_pct in (None, "") else safe_float(brake_pct)
    if brake is None or not (5.0 <= brake <= 95.0):
        raise ValueError("El freno de pérdidas debe estar entre el 5 % y el 95 %")
    label = str(name or "").strip() or default_name(ini, tgt)
    if len(label) > 40:
        raise ValueError("El nombre puede tener como mucho 40 caracteres")
    if label.lower() in {t.lower() for t in taken}:
        raise ValueError(f"Ya hay una cuenta de reto llamada «{label}»")
    return {"name": label, "initial": round(float(ini), 2), "target": round(float(tgt), 2), "until": deadline,
            "brake_pct": round(float(brake), 2), "aggressive": True, "started_at": int(now), "status": "running",
            "ended_at": None, "result": None}


def load_records(raw: Any) -> dict[int, dict[str, Any]]:
    """The saved challenge accounts (malformed ones are skipped)."""
    out: dict[int, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, rec in raw.items():
        try:
            aid = int(key)
        except (TypeError, ValueError):
            continue
        if aid < FIRST_ID or not isinstance(rec, dict):
            continue
        ini, tgt, started = (safe_float(rec.get(k)) for k in ("initial", "target", "started_at"))
        if ini is None or tgt is None or started is None or not isinstance(rec.get("name"), str):
            continue
        until, brake, ended = safe_float(rec.get("until")), safe_float(rec.get("brake_pct")), safe_float(rec.get("ended_at"))
        out[aid] = {"id": aid, "name": rec["name"], "initial": float(ini), "target": float(tgt),
                    "until": int(until) if until else None,
                    "brake_pct": float(brake) if brake is not None else DEFAULT_BRAKE_PCT,
                    "aggressive": rec.get("aggressive") is not False, "started_at": int(started),
                    "status": rec.get("status") if rec.get("status") in STATUSES else "running",
                    "ended_at": int(ended) if ended else None,
                    "result": rec.get("result") if isinstance(rec.get("result"), dict) else None}
    return out


def goal_like(rec: dict[str, Any]) -> dict[str, Any]:
    """The record as an app.sim.autopilot_goal goal (for goal_result)."""
    return {"target": rec["target"], "until": rec["until"], "started_at": rec["started_at"],
            "start_equity": rec["initial"], "aggressive": rec["aggressive"], "then": "pause"}


def challenge_view(rec: dict[str, Any], *, account: dict[str, Any], positions: list[dict], history: list[dict],
                   log: list[dict], brake: dict[str, Any], now: int) -> dict[str, Any]:
    """AutopilotChallenge: one challenge account, its progress from its initial balance to its target."""
    equity = float(account.get("equity") or 0.0)
    span = rec["target"] - rec["initial"]
    done = (equity - rec["initial"]) / span * 100.0 if span > 0 else 0.0
    running = rec["status"] == "running"
    return {
        "id": rec["id"],
        "name": rec["name"],
        "status": rec["status"],
        "initial": rec["initial"],
        "target": rec["target"],
        "until": rec["until"],
        "brake_pct": rec["brake_pct"],
        "aggressive": rec["aggressive"],
        "started_at": rec["started_at"],
        "ended_at": rec["ended_at"],
        "account": account,
        "positions": positions,
        "history": history,
        "log": log,
        "brake": brake,
        "progress_pct": round(min(max(done, 0.0), 100.0), 2),
        "remaining": round(max(rec["target"] - equity, 0.0), 2),
        "seconds_left": max(int(rec["until"]) - int(now), 0) if rec["until"] and running else None,
        "result": rec["result"],
    }
