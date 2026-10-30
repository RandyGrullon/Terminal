"""Shared helpers for the simulation package (BE-SIM): Spanish formatting, engine adapters and small utilities.

The NEXUS engine (`app.engine.*`) and the market-data hub (`app.data.hub`) are owned by other modules, so every
access to them goes through the lazy adapters below: this package must import and degrade gracefully even when
those modules are missing or fail to load.
"""
from __future__ import annotations

import copy
import dataclasses
import importlib
import inspect
import json
import math
from typing import Any, Iterable

from app.config import DEFAULT_SETTINGS
from app.util import safe_float

MINUS = "−"

VERDICT_LABEL = {"TAKE": "TOMAR", "WAIT": "ESPERAR", "AVOID": "NO TOMAR"}
ACTION_LABEL = {
    "BUY": "COMPRAR",
    "SELL": "VENDER",
    "HOLD_LONG": "MANTENER LARGO",
    "HOLD_SHORT": "MANTENER CORTO",
    "WAIT": "ESPERAR",
    "EXIT_LONG": "CERRAR LARGO",
    "EXIT_SHORT": "CERRAR CORTO",
}
REGIME_LABEL = {
    "trend_up": "Tendencia alcista",
    "trend_down": "Tendencia bajista",
    "range": "Lateral",
    "volatile": "Volátil",
    "unknown": "Desconocido",
}
SIDE_LABEL = {"long": "largo", "short": "corto"}
EXIT_LABEL = {
    "SL": "Stop loss",
    "BE": "Stop en punto de equilibrio",
    "TRAIL": "Stop dinámico",
    "TP1": "Objetivo 1",
    "TP2": "Objetivo 2",
    "TP3": "Objetivo 3",
    "EARLY": "Salida anticipada",
    "REVERSE": "Señal contraria",
    "TIME": "Tiempo agotado",
    "SIGNAL": "Señal de salida",
    "MANUAL": "Manual",
    "OPEN": "Abierta",
}
# Engine exit marker kinds -> paper exit reason codes.
MARKER_EXIT_REASON = {
    "EXIT_SL": "SL",
    "EXIT_TRAIL": "TRAIL",
    "EXIT_EARLY": "EARLY",
    "EXIT_REVERSE": "REVERSE",
    "EXIT_TIME": "TIME",
    "EXIT_SIGNAL": "SIGNAL",
    "TP3": "TP3",
}
DISCLAIMER = "Resultados simulados con datos históricos: no garantizan resultados futuros."


# ----------------------------------------------------------------------------- formatting (Spanish style)


def fmt_num(x: Any, digits: int = 2, signed: bool = False) -> str:
    """1234.5 -> '1.234,50'; signed adds '+' to positives; negatives use the typographic minus."""
    v = safe_float(x)
    if v is None:
        return "—"
    body = f"{abs(v):,.{digits}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    is_zero = round(abs(v), digits) == 0
    if v < 0 and not is_zero:
        return MINUS + body
    if signed and v > 0 and not is_zero:
        return "+" + body
    return body


def fmt_pct(x: Any, digits: int = 1, signed: bool = True) -> str:
    """Value already in percent units: 8.43 -> '+8,4 %'."""
    s = fmt_num(x, digits, signed)
    return s if s == "—" else f"{s} %"


def fmt_prob(p: Any, digits: int = 0) -> str:
    """0.643 -> '64 %'."""
    v = safe_float(p)
    return "—" if v is None else f"{fmt_num(v * 100.0, digits)} %"


def fmt_r(x: Any, digits: int = 1) -> str:
    s = fmt_num(x, digits, True)
    return s if s == "—" else f"{s}R"


def price_decimals(price: float | None) -> int:
    a = abs(price or 0.0)
    if a >= 1000:
        return 2
    if a >= 10:
        return 2
    if a >= 1:
        return 4
    if a >= 0.01:
        return 5
    return 8


def fmt_price(x: Any) -> str:
    v = safe_float(x)
    return "—" if v is None else fmt_num(v, price_decimals(v))


def fmt_qty(q: Any) -> str:
    v = safe_float(q)
    if v is None:
        return "—"
    a = abs(v)
    digits = 2 if a >= 100 else 4 if a >= 1 else 6 if a >= 0.001 else 8
    return fmt_num(v, digits)


def rating_label(r: float | None) -> str:
    """TradingView 'Recommend.*' value (-1..1) -> Spanish label."""
    if r is None:
        return "sin datos"
    if r > 0.5:
        return "Compra fuerte"
    if r > 0.1:
        return "Compra"
    if r >= -0.1:
        return "Neutral"
    if r >= -0.5:
        return "Venta"
    return "Venta fuerte"


# ----------------------------------------------------------------------------- generic helpers


def to_plain(obj: Any) -> Any:
    """dataclass / object with to_dict() / dict -> plain dict (recursively for lists)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, (list, tuple)):
        return [to_plain(x) for x in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    fn = getattr(obj, "to_dict", None)
    if callable(fn):
        return fn()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


def json_loads(text: Any, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def json_dumps(obj: Any) -> str:
    from app.util import clean_json

    return json.dumps(clean_json(obj), ensure_ascii=False, separators=(",", ":"))


def side_sign(side: str) -> int:
    return 1 if str(side).lower() == "long" else -1


def finite(x: Any) -> float | None:
    """float(x) when finite, else None."""
    return safe_float(x)


def mean_or_none(values: Iterable[float]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return sum(vals) / len(vals) if vals else None


async def call_maybe_async(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a service method that may be sync or async (e.g. hub.subscribe / add_listener) and return its result."""
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


# ----------------------------------------------------------------------------- settings


def current_settings() -> dict[str, Any]:
    """Live settings (rt.settings) or the defaults when the runtime is not initialised (pure tests)."""
    from app.runtime import rt

    if rt.settings is not None:
        try:
            return rt.settings.all()
        except Exception:  # pragma: no cover - defensive
            pass
    return copy.deepcopy(DEFAULT_SETTINGS)


def setting(key: str, default: Any = None) -> Any:
    from app.runtime import rt

    if rt.settings is not None:
        try:
            return rt.settings.get(key, default)
        except Exception:  # pragma: no cover - defensive
            pass
    return copy.deepcopy(DEFAULT_SETTINGS.get(key, default))


# ----------------------------------------------------------------------------- NEXUS engine adapters


class EngineUnavailable(RuntimeError):
    """The NEXUS engine module cannot be imported (missing or broken)."""


def load_engine() -> tuple[Any, Any]:
    """Lazy import of the pure engine API: (run_nexus, NexusParams)."""
    try:
        mod = importlib.import_module("app.engine.nexus")
        run_nexus, nexus_params_cls = mod.run_nexus, mod.NexusParams
    except Exception as exc:  # module missing or failing while other engineers work on it
        raise EngineUnavailable(f"Motor NEXUS no disponible ({type(exc).__name__}: {exc})") from exc
    return run_nexus, nexus_params_cls


def engine_available() -> bool:
    try:
        load_engine()
        return True
    except EngineUnavailable:
        return False


def build_nexus_params(
    nexus_params_cls: Any,
    settings: dict[str, Any],
    overrides: Any = None,
    *,
    gate_enabled: bool | None = None,
) -> tuple[Any, list[str]]:
    """NexusParams from settings + user overrides. Returns (params, ignored_override_keys).

    Overrides may use settings keys ("nexus.sl_atr", "gate.take_score") or bare names ("sl_atr", "take_score"):
    known names are applied to the settings dict (coerced to the default's type) so `from_settings` reads them;
    unknown bare names are passed as keyword overrides and silently dropped if the engine rejects them.
    `gate_enabled=False` disables the Trade Gate (used for the ungated comparison run).
    """
    from app.settings import SettingsStore

    if overrides is not None and not isinstance(overrides, dict):
        # an already-built NexusParams instance
        params = overrides
        if gate_enabled is not None:
            params = _with_gate(params, gate_enabled)
        return params, []

    base = copy.deepcopy(settings or DEFAULT_SETTINGS)
    kwargs: dict[str, Any] = {}
    for raw_key, value in (overrides or {}).items():
        key = str(raw_key)
        target = None
        if key in DEFAULT_SETTINGS:
            target = key
        elif f"nexus.{key}" in DEFAULT_SETTINGS:
            target = f"nexus.{key}"
        elif f"gate.{key}" in DEFAULT_SETTINGS:
            target = f"gate.{key}"
        if target is not None:
            try:
                base[target] = SettingsStore.coerce(target, value)
            except (KeyError, ValueError):
                raise ValueError(f"Parámetro no válido: {key}={value!r}") from None
        else:
            kwargs[key] = value
    if gate_enabled is not None:
        base["gate.enabled"] = bool(gate_enabled)

    # Contract: NexusParams is a dataclass and from_settings(settings, **overrides) accepts its field names.
    fields = _param_fields(nexus_params_cls)
    ignored: list[str] = []
    if fields is not None:
        ignored = sorted(k for k in kwargs if k not in fields)
        kwargs = {k: v for k, v in kwargs.items() if k in fields}
    try:
        params = nexus_params_cls.from_settings(base, **kwargs)
    except TypeError:
        ignored = sorted(set(ignored) | set(kwargs))
        params = nexus_params_cls.from_settings(base)
    if gate_enabled is not None:
        params = _with_gate(params, gate_enabled)
    return params, ignored


def _param_fields(nexus_params_cls: Any) -> set[str] | None:
    """Field names of a dataclass NexusParams (None when the class is not a dataclass)."""
    if isinstance(nexus_params_cls, type) and dataclasses.is_dataclass(nexus_params_cls):
        return {f.name for f in dataclasses.fields(nexus_params_cls)}
    return None


def _with_gate(params: Any, enabled: bool) -> Any:
    """Force the gate flag on a NexusParams instance whatever the field is called."""
    for attr in ("gate_enabled", "use_gate", "gate"):
        if isinstance(getattr(params, attr, None), bool):
            if dataclasses.is_dataclass(params):
                try:
                    return dataclasses.replace(params, **{attr: enabled})
                except (TypeError, ValueError):
                    pass
            try:
                setattr(params, attr, enabled)
            except (AttributeError, TypeError):
                pass
            return params
    return params


def params_to_dict(params: Any) -> dict[str, Any]:
    fn = getattr(params, "to_dict", None)
    plain = fn() if callable(fn) else to_plain(params)
    return dict(plain) if isinstance(plain, dict) else {}


def nexus_to_response(res: Any, cs: Any) -> dict[str, Any]:
    """NexusResult -> NexusResponse-like dict (trades, markers, stats, current, bars, prediction, learning)."""
    if isinstance(res, dict):
        return res
    fn = getattr(res, "to_response", None)
    if callable(fn):
        out = fn(cs)
        out = to_plain(out)
        if isinstance(out, dict):
            return out
    out: dict[str, Any] = {}
    for name in ("trades", "markers", "stats", "current", "bars", "prediction", "learning", "params"):
        if hasattr(res, name):
            out[name] = to_plain(getattr(res, name))
    return out


def run_engine(cs: Any, params: Any, prior: Any = None, gate_state: dict | None = None) -> tuple[dict[str, Any], Any]:
    """Run NEXUS synchronously (call from a worker thread). Returns (response_dict, snapshots).

    Contract: run_nexus(cs, params, prior=None, *, gate_state=None). `gate_state` (the learned GateLearner state)
    is only passed when given. An engine that is still incomplete (ImportError at call time) → EngineUnavailable.
    """
    run_nexus, _ = load_engine()
    kwargs: dict[str, Any] = {}
    if prior is not None:
        kwargs["prior"] = prior
    if gate_state is not None:
        kwargs["gate_state"] = gate_state
    try:
        try:
            res = run_nexus(cs, params, **kwargs)
        except TypeError as exc:
            if "gate_state" not in kwargs or "gate_state" not in str(exc):
                raise
            kwargs.pop("gate_state")  # an engine build without learned gate weights
            res = run_nexus(cs, params, **kwargs)
    except ImportError as exc:
        raise EngineUnavailable(f"Motor NEXUS no disponible (instalación incompleta: {exc})") from exc
    return nexus_to_response(res, cs), getattr(res, "snapshots", None)


def normalize_snapshots(snaps: Any) -> list[tuple[int, Any]]:
    """Engine snapshots (dict time->state, list of (time, state) or list of {"time", "state"}) -> sorted list."""
    out: list[tuple[int, Any]] = []
    if not snaps:
        return out
    try:
        if isinstance(snaps, dict):
            items = list(snaps.items())
        else:
            items = list(snaps)
        for item in items:
            if isinstance(item, dict):
                t = item.get("time", item.get("t"))
                state = item.get("state", item)
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                t, state = item
            else:
                continue
            t_val = safe_float(t)
            if t_val is not None:
                out.append((int(t_val), state))
    except Exception:  # pragma: no cover - unknown structure
        return []
    out.sort(key=lambda x: x[0])
    return out


def gate_checks_vector(gate: dict | None) -> dict[str, int | None]:
    """GateResult.checks -> {check_id: 1 | 0 | None} (the gate_log JSON format)."""
    out: dict[str, int | None] = {}
    for chk in (gate or {}).get("checks") or []:
        if not isinstance(chk, dict) or not chk.get("id"):
            continue
        passed = chk.get("passed")
        out[str(chk["id"])] = None if passed is None else (1 if passed else 0)
    return out


def gate_line(gate: dict | None) -> str:
    """'TOMAR 78/100' style label for a GateResult."""
    if not gate:
        return "sin filtro"
    verdict = str(gate.get("verdict") or "")
    label = gate.get("verdict_label") or VERDICT_LABEL.get(verdict, verdict)
    score = safe_float(gate.get("score"))
    return f"{label} {score:.0f}/100" if score is not None else str(label)
