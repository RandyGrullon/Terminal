"""NEXUS adaptive signal engine — public API (pure, deterministic and causal).

    from app.engine.nexus import NexusParams, run_nexus
    res = run_nexus(cs, NexusParams.from_settings(rt.settings.all()), prior=None)
    res.to_response(cs)  # NexusResponse minus provider/note/quick (filled by NexusService)

The decision at bar i only uses data up to the close of bar i. Historical entries/exits decided at a close fill at
the next bar's open; for the last bar the action is what the user should do NOW (reference price = last close).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from app.config import DEFAULT_SETTINGS

_SETTINGS_PREFIX = {
    "entry_threshold": "nexus.entry_threshold",
    "exit_threshold": "nexus.exit_threshold",
    "horizon": "nexus.horizon",
    "sl_atr": "nexus.sl_atr",
    "tp_r": "nexus.tp_r",
    "trail_atr": "nexus.trail_atr",
    "learning_rate": "nexus.learning_rate",
    "allow_short": "nexus.allow_short",
    "min_bars": "nexus.min_bars",
    "default_limit": "nexus.default_limit",
    "time_stop_bars": "nexus.time_stop_bars",
    "gate_enabled": "gate.enabled",
    "take_score": "gate.take_score",
    "wait_score": "gate.wait_score",
    "min_rr": "gate.min_rr",
    "min_prob": "gate.min_prob",
    "fee_pct": "risk.fee_pct",
}


def _f(x: Any, default: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return min(max(v, lo), hi)


@dataclass
class NexusParams:
    entry_threshold: float = 0.35
    exit_threshold: float = 0.0
    horizon: int = 5
    sl_atr: float = 2.0
    tp_r: tuple[float, float, float] = (1.0, 2.0, 3.0)
    trail_atr: float = 3.0
    learning_rate: float = 0.08
    allow_short: bool = True
    min_bars: int = 150
    default_limit: int = 1000
    time_stop_bars: int = 48
    gate_enabled: bool = True
    take_score: float = 65.0
    wait_score: float = 45.0
    min_rr: float = 1.5
    min_prob: float = 0.52
    fee_pct: float = 0.1

    def __post_init__(self) -> None:
        self.entry_threshold = _f(self.entry_threshold, 0.35, 0.01, 0.99)
        self.exit_threshold = _f(self.exit_threshold, 0.0, -0.5, 0.9)
        self.horizon = int(_f(self.horizon, 5, 1, 200))
        self.sl_atr = _f(self.sl_atr, 2.0, 0.2, 20.0)
        tps = list(self.tp_r) if isinstance(self.tp_r, (list, tuple)) else [1.0, 2.0, 3.0]
        tps = [_f(x, 1.0, 0.05, 50.0) for x in tps[:3]]
        while len(tps) < 3:
            tps.append((tps[-1] if tps else 0.0) + 1.0)
        tps.sort()
        self.tp_r = (tps[0], tps[1], tps[2])
        self.trail_atr = _f(self.trail_atr, 3.0, 0.2, 20.0)
        self.learning_rate = _f(self.learning_rate, 0.08, 0.001, 1.0)
        self.allow_short = bool(self.allow_short)
        self.min_bars = int(_f(self.min_bars, 150, 30, 100000))
        self.default_limit = int(_f(self.default_limit, 1000, 100, 20000))
        self.time_stop_bars = int(_f(self.time_stop_bars, 48, 2, 100000))
        self.gate_enabled = bool(self.gate_enabled)
        self.take_score = _f(self.take_score, 65.0, 0.0, 100.0)
        self.wait_score = _f(self.wait_score, 45.0, 0.0, 100.0)
        self.min_rr = _f(self.min_rr, 1.5, 0.0, 20.0)
        self.min_prob = _f(self.min_prob, 0.52, 0.0, 1.0)
        self.fee_pct = _f(self.fee_pct, 0.1, 0.0, 10.0)

    @classmethod
    def from_settings(cls, settings: dict | None, **overrides: Any) -> "NexusParams":
        """Params from settings keys (nexus.*, gate.*, risk.fee_pct) + overrides by field name.

        Unknown override names raise TypeError (callers rely on it to report ignored keys)."""
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(k for k in overrides if k not in names)
        if unknown:
            raise TypeError(f"Parámetros NEXUS desconocidos: {', '.join(unknown)}")
        src = settings if isinstance(settings, dict) else {}
        kwargs: dict[str, Any] = {}
        for name, key in _SETTINGS_PREFIX.items():
            if key in src and src[key] is not None:
                kwargs[name] = src[key]
            elif key in DEFAULT_SETTINGS:
                kwargs[name] = DEFAULT_SETTINGS[key]
        kwargs.update(overrides)
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["tp_r"] = list(self.tp_r)
        return d


@dataclass
class NexusResult:
    symbol: str
    tf: str
    history: str
    params: NexusParams
    ok: bool
    message: str | None
    bars: dict[str, list]
    markers: list[dict]
    trades: list[dict]
    current: dict | None
    prediction: dict | None
    learning: dict
    stats: dict
    # learner state AFTER processing each snapshot bar: {"time", "state", "kind": "grid" | "last"} (see nexus_loop)
    snapshots: list[dict] = field(default_factory=list)
    state: dict | None = None
    context: Any = None  # GateContext of the last bar (lets the service re-check user entry/SL/TP ideas)
    decision_gate: dict | None = None  # gate learner state applied at the last bar (prior chain + live state)
    # anchor snapshot of the bar before the first processed one of a run that started cold (kind "origin"):
    # a later run anchored there replays this run's path (NexusService stores it when the run had no anchor)
    origin: dict | None = None
    # gate decisions of the last bar taken with the global gate state that the bar would not show once it is history
    # (nexus_decide.gate_record); the service stores those of a bar-close decision and passes them back as live_gates
    live_gates: list[dict] = field(default_factory=list)

    def to_response(self, cs: Any = None) -> dict[str, Any]:
        """NexusResponse without provider/note/quick (the service fills them)."""
        return {
            "symbol": getattr(cs, "symbol", None) or self.symbol,
            "tf": getattr(cs, "tf", None) or self.tf,
            "history": getattr(cs, "history", None) or self.history,
            "ok": bool(self.ok),
            "message": self.message,
            "params": self.params.to_dict(),
            "bars": self.bars,
            "markers": self.markers,
            "trades": self.trades,
            "current": self.current,
            "prediction": self.prediction,
            "learning": self.learning,
            "stats": self.stats,
        }


def run_nexus(cs: Any, params: NexusParams | None = None, prior: dict | None = None, *,
              gate_state: dict | None = None, vetoed_entries: Any = None, live_gates: Any = None) -> NexusResult:
    """Run the engine over the candle set. `prior` = a snapshot state (see NexusResult.snapshots). An anchor
    snapshot whose resume block fits (`app.engine.nexus_resume.resume_fit`: same engine version and path params,
    and the candle set starts at the bar its features restart from) is continued exactly: the run processes from
    the bar after the anchor and reproduces the stored path bit for bit; any other prior only seeds the learners.
    `gate_state` = the global GateLearner state (model_state kind='gate'), pooled into the gate weights of the LAST
    bar only (historical decisions use the causal prior chain + what the run learns).

    `vetoed_entries` = entries the market-conditions check (semáforo) vetoed when they were the live decision:
    [{"time": decision bar open time, "direction": "long" | "short", "level": "red" | "amber", "detail": str}].
    That candidate is rejected (REJECTED_* marker with the macro reason) instead of opening a position. The veto
    was known at the close of its bar, so the run stays causal and deterministic for the same inputs.

    `live_gates` = gate decisions taken at a bar's close with the global gate state pooled in (NexusResult.live_gates
    of that bar-close run): at that bar the candidate is decided as it was then (verdict, score, reasons, check
    vector), so a bar-close decision never changes once the bar is history. Known at that close: still causal."""
    from app.engine.nexus_loop import NexusRun

    if params is None:
        params = NexusParams()
    elif isinstance(params, dict):
        names = {f.name for f in dataclasses.fields(NexusParams)}
        params = NexusParams(**{k: v for k, v in params.items() if k in names})
    elif not isinstance(params, NexusParams):
        raise TypeError("params debe ser NexusParams")
    return NexusRun(cs, params, prior, gate_state, vetoed_entries, live_gates).run()
