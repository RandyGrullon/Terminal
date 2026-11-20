"""The vault («vault» de ahorro) of NEXUS automática: when autopilot.vault is on, the realized balance of the 10k
account (3) and of the 1k account above their initial balance is moved here, so each account keeps trading from its
base and what it won is set aside. It is a number that never trades: no positions, no risk, nothing taken back after a
loss. Simulated money like the accounts; it is kept in the table `meta` (key autopilot.vault), no schema change.

Pure helpers (the record and its view); the engine side is app.sim.paper (_vault_tick).
"""
from __future__ import annotations

from typing import Any

from app.util import safe_float

VAULT_KEY = "autopilot.vault"
MIN_SWEEP = 0.01  # $: smaller excesses wait
KEEP_ENTRIES = 200
SHOW_ENTRIES = 20
NOTE = ("El vault guarda la ganancia ya cerrada de la cuenta de 10k y de la de 1k por encima de su saldo inicial: cada "
        "cuenta vuelve a su base y sigue operando, y lo ganado queda aparte. No opera y una pérdida posterior no le "
        "quita nada. Dinero simulado, como las cuentas; no es asesoría financiera.")


def load(raw: Any) -> dict[str, Any]:
    """The saved vault (a fresh one when missing or malformed)."""
    out: dict[str, Any] = {"total": 0.0, "by_account": {}, "entries": []}
    if not isinstance(raw, dict):
        return out
    out["total"] = round(float(safe_float(raw.get("total")) or 0.0), 2)
    by = raw.get("by_account")
    if isinstance(by, dict):
        for k, v in by.items():
            amount = safe_float(v)
            if amount is not None:
                out["by_account"][str(k)] = round(float(amount), 2)
    for e in raw.get("entries") or []:
        if isinstance(e, dict) and safe_float(e.get("amount")) is not None and safe_float(e.get("ts")) is not None:
            out["entries"].append({"ts": int(e["ts"]), "account_id": int(safe_float(e.get("account_id")) or 0),
                                   "amount": round(float(e["amount"]), 2),
                                   "base": round(float(safe_float(e.get("base")) or 0.0), 2)})
    out["entries"] = out["entries"][-KEEP_ENTRIES:]
    return out


def add(vault: dict[str, Any], account_id: int, amount: float, ts: int, base: float) -> None:
    """Book one sweep (amount > 0) into the vault record, in place."""
    amount = round(float(amount), 2)
    key = str(int(account_id))
    vault["by_account"][key] = round(float(vault["by_account"].get(key, 0.0)) + amount, 2)
    vault["total"] = round(float(vault.get("total") or 0.0) + amount, 2)
    vault["entries"] = [*vault.get("entries", []), {"ts": int(ts), "account_id": int(account_id), "amount": amount,
                                                    "base": round(float(base), 2)}][-KEEP_ENTRIES:]


def main_goal_view(account: dict[str, Any], vault: dict[str, Any], target: Any) -> dict[str, Any] | None:
    """AutopilotMainGoal: the 10k account's goal (autopilot.main_target, 0 = none). Its total is the account's equity
    plus what it put in the vault (with the vault on, its balance stays at its base: the gains are set aside there)."""
    tgt = safe_float(target)
    if not tgt or tgt <= 0:
        return None
    saved = float(vault.get("by_account", {}).get(str(int(account.get("id") or 3)), 0.0))
    initial = float(account.get("initial_balance") or 0.0)
    total = round(float(account.get("equity") or 0.0) + saved, 2)
    span = tgt - initial
    done = (total - initial) / span * 100.0 if span > 0 else 0.0
    return {"target": round(float(tgt), 2), "total": total, "saved": round(saved, 2), "initial": round(initial, 2),
            "progress_pct": round(min(max(done, 0.0), 100.0), 2), "remaining": round(max(tgt - total, 0.0), 2),
            "reached": total >= tgt}


def view(vault: dict[str, Any], *, enabled: bool, names: dict[int, str]) -> dict[str, Any]:
    """AutopilotVault: on/off, the total, what each account put in (also accounts it no longer sweeps) and the last
    sweeps, newest first."""
    by = [{"account_id": int(k), "name": names.get(int(k)) or f"Cuenta {k}", "amount": v}
          for k, v in sorted(vault.get("by_account", {}).items(), key=lambda kv: int(kv[0]))]
    for aid, name in names.items():
        if str(aid) not in vault.get("by_account", {}):
            by.append({"account_id": aid, "name": name, "amount": 0.0})
    entries = [{**e, "name": names.get(e["account_id"]) or f"Cuenta {e['account_id']}"}
               for e in reversed(vault.get("entries", [])[-SHOW_ENTRIES:])]
    return {"enabled": bool(enabled), "total": round(float(vault.get("total") or 0.0), 2),
            "by_account": sorted(by, key=lambda r: r["account_id"]), "entries": entries, "note": NOTE}
