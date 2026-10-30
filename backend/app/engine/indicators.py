"""NEXUS indicator library: public API.

    from app.engine.indicators import CATALOG, get_spec, compute_indicator, IndicatorError

- ``CATALOG``: list of IndicatorSpec dicts (exact shape of ``frontend/src/api/types.ts``), ordered by category
  (Tendencia, Momento, Volatilidad, Volumen, Soportes/Resistencias, Estadística, NEXUS).
- ``get_spec(id)``: a copy of one spec or None.
- ``compute_indicator(id, cs, params)``: validates / coerces / clamps the parameters and returns
  ``{output_key: float64 array aligned with cs.time}`` (NaN during warm-up). Every value at bar ``i`` only uses
  bars ``<= i``. Raises ``IndicatorError`` (Spanish message) for unknown ids, invalid parameters or failures.
- ``compute_items(cs, items)``: convenience for ``POST /api/indicators/compute`` → IndicatorComputeResponse
  (errors are reported per item, never raised).

The implementations live in the category modules ``indicators_*.py``; importing them registers the indicators.
"""
from __future__ import annotations

import copy
from typing import Any

import numpy as np

from app.engine import (  # noqa: F401 - imported for their registration side effects (catalog order)
    indicators_levels,
    indicators_ma,
    indicators_momentum,
    indicators_nexus,
    indicators_oscillators,
    indicators_stats,
    indicators_trend,
    indicators_volatility,
    indicators_volume,
)
from app.engine.indicators_core import CATEGORIES, REGISTRY, Bars, Entry, IndicatorError, coerce_params, finalize

__all__ = [
    "CATALOG",
    "CATEGORIES",
    "IndicatorError",
    "catalog_ids",
    "compute_indicator",
    "compute_items",
    "get_spec",
    "normalize_params",
]


def _build_catalog() -> list[dict]:
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    entries = sorted(REGISTRY.values(), key=lambda e: rank[e.spec["category"]])  # stable within a category
    return [copy.deepcopy(e.spec) for e in entries]


CATALOG: list[dict] = _build_catalog()
_FOLDED_IDS: dict[str, str] = {s["id"].casefold(): s["id"] for s in CATALOG}


def catalog_ids() -> list[str]:
    return [s["id"] for s in CATALOG]


def _resolve(indicator_id: Any) -> str | None:
    if not isinstance(indicator_id, str):
        return None
    key = indicator_id.strip()
    if key in REGISTRY:
        return key
    return _FOLDED_IDS.get(key.casefold())


def _entry(indicator_id: Any) -> Entry:
    rid = _resolve(indicator_id)
    if rid is None:
        shown = str(indicator_id)[:60] if indicator_id is not None else ""
        raise IndicatorError(f"Indicador desconocido: «{shown}»")
    return REGISTRY[rid]


def get_spec(indicator_id: str) -> dict | None:
    """IndicatorSpec dict for ``indicator_id`` (case-insensitive) or None."""
    rid = _resolve(indicator_id)
    return copy.deepcopy(REGISTRY[rid].spec) if rid is not None else None


def normalize_params(indicator_id: str, params: dict | None = None) -> dict:
    """Validated parameters (defaults filled, numbers clamped); raises IndicatorError when invalid."""
    return coerce_params(_entry(indicator_id).spec, params)


def compute_indicator(indicator_id: str, cs: Any, params: dict | None = None) -> dict[str, np.ndarray]:
    """``{output_key: float64 array}`` aligned with ``cs.time`` for every output of the indicator."""
    entry = _entry(indicator_id)
    p = coerce_params(entry.spec, params)
    n = len(cs)
    if n == 0:
        return {o["key"]: np.zeros(0) for o in entry.spec["outputs"]}
    if entry.needs_volume and not np.any(np.nan_to_num(np.asarray(cs.volume, dtype=np.float64)) > 0):
        raise IndicatorError(f"«{entry.spec['name']}» necesita datos de volumen y este activo no los publica")
    try:
        with np.errstate(all="ignore"):
            raw = entry.fn(Bars(cs), p)
    except IndicatorError:
        raise
    except Exception as exc:  # defensive: a numeric corner case must not crash the request
        raise IndicatorError(f"No se pudo calcular «{entry.spec['name']}»: {exc}") from exc
    return finalize(entry.spec, raw, n)


def _to_json_list(arr: np.ndarray) -> list[float | None]:
    return [v if v == v else None for v in np.asarray(arr, dtype=np.float64).tolist()]


def compute_items(cs: Any, items: Any) -> dict:
    """IndicatorComputeResponse ``{time, results: [{uid, id, outputs, error}]}`` for IndicatorRequestItem dicts."""
    time = [int(t) for t in np.asarray(cs.time, dtype=np.int64).tolist()]
    results: list[dict] = []
    for raw in items if isinstance(items, (list, tuple)) else []:
        item = raw if isinstance(raw, dict) else {}
        uid = str(item.get("uid") or "")
        iid = str(item.get("id") or "")
        try:
            if not iid:
                raise IndicatorError("Petición de indicador inválida: falta el id")
            arrays = compute_indicator(iid, cs, item.get("params"))
            results.append({"uid": uid, "id": iid, "outputs": {k: _to_json_list(v) for k, v in arrays.items()}, "error": None})
        except IndicatorError as exc:
            results.append({"uid": uid, "id": iid, "outputs": {}, "error": str(exc)})
        except Exception:  # never let one indicator break the whole response
            results.append({"uid": uid, "id": iid, "outputs": {}, "error": "Error interno al calcular el indicador"})
    return {"time": time, "results": results}
