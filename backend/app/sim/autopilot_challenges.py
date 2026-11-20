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


# The 1k account («cuenta de 1.000 $», kind "twin"): a second account of NEXUS automática that trades the same
# candidates and signals (gold today) with its own balance and a small-account mindset: protect the capital first,
# because the minimum lot (0,01 = 1 oz of gold) is coarse next to 1.000 $. No target, no deadline, no aggressive mode;
# its brake stops new entries but does not end it. These override the saved settings for it only.
TWIN = "twin"
TWIN_INITIAL = 1000.0
TWIN_NAME = "NEXUS 1k"
TWIN_BRAKE_PCT = 25.0
TWIN_TARGET_MULT = 10.0  # its challenge: 1.000 $ -> 10.000 $ (it compounds: the vault never takes its gains)
TWIN_PROFILE: dict[str, Any] = {
    "autopilot.tfs": ["5m", "15m", "1h"],
    "autopilot.risk_pct": 2.0,
    "autopilot.lots": True,
    "autopilot.lot_max_risk_pct": 3.0,  # the minimum lot only when it risks at most 3 % of the account
    "autopilot.perp_leverage": 10.0,
    "autopilot.max_positions": 2,
}
TWIN_RISK: dict[str, Any] = {"daily_loss_limit_pct": 4.0, "max_open_risk_pct": 6.0, "max_positions_per_group": 2}
TWIN_LOTS_MIN = 1000.0  # below this the minimum lot (0,01 = 1 oz of gold) does not fit: fractional size instead
TWIN_MAX_RUNNING = 6
TWIN_TEXT = ("Mentalidad 1k: primero proteger el capital. Mismas señales que la de 10k, pero como mucho 2 operaciones "
             "a la vez, 2 % de riesgo por operación (el lote mínimo de 0,01 solo si arriesga 3 % o menos), "
             "apalancamiento 10x, freno del día al 4 % y freno total al 25 %; nunca entra en modo agresivo.")


def is_twin(rec: dict[str, Any] | None) -> bool:
    return bool(rec) and rec.get("kind") == TWIN


def twin_lots(rec: dict[str, Any] | None) -> bool:
    """Sized in MT5 lots (whole steps of 0,01) only from TWIN_LOTS_MIN up; a smaller account trades fractional size
    (0,001 oz of gold, like Binance's perpetual), because its minimum lot would risk far more than 3 % (500 $) or not
    even fit its margin (100 $)."""
    return float((rec or {}).get("initial") or TWIN_INITIAL) >= TWIN_LOTS_MIN


def twin_profile(rec: dict[str, Any] | None) -> dict[str, Any]:
    """TWIN_PROFILE as it applies to this account (lots or fractional size)."""
    out = dict(TWIN_PROFILE)
    if not twin_lots(rec):
        out["autopilot.lots"] = False
    return out


def twin_text(rec: dict[str, Any] | None) -> str:
    """Its mindset, in Spanish."""
    if twin_lots(rec):
        return TWIN_TEXT
    ini = money(float((rec or {}).get("initial") or 0.0))
    return (f"Mentalidad de cuenta pequeña ({ini}): primero proteger el capital. Mismas señales que la de 10k, como "
            "mucho 2 operaciones a la vez, 2 % de riesgo por operación con tamaño fraccionario (desde 0,001 oz de oro, "
            "como el perpetuo de Binance: en MT5 el lote mínimo de 0,01 no cabría en esta cuenta), apalancamiento 10x, "
            "freno del día al 4 % y freno total al 25 %; nunca entra en modo agresivo.")


def parse_twin_target(target: Any, initial: float) -> float:
    """The 1k account's goal (ValueError in Spanish): above its initial balance, at most MAX_MULT times it."""
    tgt = safe_float(target)
    if tgt is None or tgt <= initial:
        raise ValueError("La meta debe ser mayor que el saldo inicial de la cuenta")
    if tgt > initial * MAX_MULT:
        raise ValueError(f"La meta puede ser como mucho {fmt_num(MAX_MULT, 0)} veces el saldo inicial")
    return round(float(tgt), 2)


def parse_twin(initial: Any, name: Any, *, now: int, taken: set[str], target: Any = None) -> dict[str, Any]:
    """The 1k account's record (ValueError in Spanish on a bad value); its goal defaults to 10× its balance."""
    ini = safe_float(initial if initial not in (None, "") else TWIN_INITIAL)
    if ini is None or not (MIN_INITIAL <= ini <= MAX_INITIAL):
        raise ValueError(f"El saldo inicial debe estar entre {money(MIN_INITIAL)} y {money(MAX_INITIAL)}")
    goal = parse_twin_target(target if target not in (None, "") else ini * TWIN_TARGET_MULT, float(ini))
    label = str(name or "").strip() or TWIN_NAME
    if len(label) > 40:
        raise ValueError("El nombre puede tener como mucho 40 caracteres")
    if label.lower() in {t.lower() for t in taken}:
        raise ValueError(f"Ya hay una cuenta llamada «{label}»")
    return {"name": label, "initial": round(float(ini), 2), "target": goal, "until": None,
            "brake_pct": TWIN_BRAKE_PCT, "aggressive": False, "kind": TWIN, "started_at": int(now),
            "status": "running", "ended_at": None, "result": None, "reached_at": None}


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
        twin = rec.get("kind") == TWIN
        if ini is None or (tgt is None and not twin) or started is None or not isinstance(rec.get("name"), str):
            continue
        until, brake, ended = safe_float(rec.get("until")), safe_float(rec.get("brake_pct")), safe_float(rec.get("ended_at"))
        out[aid] = {"id": aid, "name": rec["name"], "initial": float(ini),
                    "target": float(tgt) if tgt is not None else None, "kind": TWIN if twin else "challenge",
                    "until": int(until) if until else None,
                    "brake_pct": float(brake) if brake is not None else DEFAULT_BRAKE_PCT,
                    "aggressive": False if twin else rec.get("aggressive") is not False, "started_at": int(started),
                    "status": rec.get("status") if rec.get("status") in STATUSES else "running",
                    "ended_at": int(ended) if ended else None,
                    "reached_at": int(safe_float(rec.get("reached_at"))) if safe_float(rec.get("reached_at")) else None,
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
    target = rec.get("target")
    span = target - rec["initial"] if target is not None else 0.0
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
        "progress_pct": round(min(max(done, 0.0), 100.0), 2) if target is not None else None,
        "remaining": round(max(target - equity, 0.0), 2) if target is not None else None,
        "kind": rec.get("kind") or "challenge",
        "reached_at": rec.get("reached_at"),
        "mindset": twin_text(rec) if rec.get("kind") == TWIN else None,
        "seconds_left": max(int(rec["until"]) - int(now), 0) if rec["until"] and running else None,
        "result": rec["result"],
    }
