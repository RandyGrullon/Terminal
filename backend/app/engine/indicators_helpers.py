"""Shared causal helpers for indicator implementations: forward fill, event-held levels, calendar groups,
running group statistics and contiguous sliding windows. Every helper only looks backwards in time."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from app.engine import ta

if TYPE_CHECKING:  # pragma: no cover - typing only (avoids a circular import with indicators_core)
    from app.engine.indicators_core import Bars

NAN = float("nan")


def ffill(x: np.ndarray) -> np.ndarray:
    """Forward-fills NaN with the last finite value (causal)."""
    x = ta.f64(x)
    ok = np.isfinite(x)
    if ok.all() or not ok.any():
        return x.copy()
    idx = np.where(ok, np.arange(x.shape[0]), 0)
    np.maximum.accumulate(idx, out=idx)
    out = x[idx]
    out[: int(np.argmax(ok))] = NAN
    return out


def hold_events(n: int, at: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Level that becomes ``values[k]`` at bar ``at[k]`` and is held until the next event (NaN before the first)."""
    out = np.full(n, NAN)
    at = np.asarray(at, dtype=np.int64)
    values = ta.f64(values)
    ok = (at >= 0) & (at < n)
    if np.any(ok):
        out[at[ok]] = values[ok]  # later events at the same bar win (assignment order)
    return ffill(out)


def mask(x: np.ndarray, cond: np.ndarray) -> np.ndarray:
    """``x`` where ``cond`` is true, NaN elsewhere (used to split a series by state/colour)."""
    return np.where(np.asarray(cond, dtype=bool), ta.f64(x), NAN)


@dataclass(frozen=True)
class Groups:
    """Consecutive runs of equal period ids (bars must be ascending in time)."""

    gid: np.ndarray  # group index of every bar
    starts: np.ndarray  # first bar of every group
    ends: np.ndarray  # last bar of every group


def groups_of(ids: np.ndarray) -> Groups:
    ids = np.asarray(ids)
    n = ids.shape[0]
    if n == 0:
        z = np.zeros(0, dtype=np.int64)
        return Groups(z, z, z)
    new = np.empty(n, dtype=bool)
    new[0] = True
    new[1:] = ids[1:] != ids[:-1]
    starts = np.flatnonzero(new)
    ends = np.append(starts[1:] - 1, n - 1)
    gid = np.cumsum(new) - 1
    return Groups(gid.astype(np.int64), starts.astype(np.int64), ends.astype(np.int64))


def group_cumsum(x: np.ndarray, g: Groups) -> np.ndarray:
    """Running sum of ``x`` that restarts at every group (causal)."""
    x = ta.f64(x)
    if x.shape[0] == 0:
        return x.copy()
    c = np.cumsum(x)
    before = (c - x)[g.starts]  # cumulative sum just before each group starts
    return c - before[g.gid]


def group_running(x: np.ndarray, g: Groups, fn: np.ufunc) -> np.ndarray:
    """Running max/min (``np.maximum`` / ``np.minimum``) of ``x`` restarting at every group (causal)."""
    x = ta.f64(x)
    out = np.empty_like(x)
    for s, e in zip(g.starts.tolist(), g.ends.tolist()):
        fn.accumulate(x[s : e + 1], out=out[s : e + 1])
    return out


def previous_group_ohlc(b: Bars, g: Groups) -> dict[str, np.ndarray]:
    """Open/high/low/close of the previous (completed) period for every bar; NaN in the first period."""
    n = b.n
    out = {k: np.full(n, NAN) for k in ("open", "high", "low", "close")}
    if n == 0 or g.starts.shape[0] < 2:
        return out
    go = b.open[g.starts]
    gh = np.maximum.reduceat(b.high, g.starts)
    gl = np.minimum.reduceat(b.low, g.starts)
    gc = b.close[g.ends]
    prev = g.gid - 1
    ok = prev >= 0
    for key, arr in (("open", go), ("high", gh), ("low", gl), ("close", gc)):
        out[key][ok] = arr[prev[ok]]
    return out


def ma_of(kind: str, x: np.ndarray, n: int, volume: np.ndarray | None = None) -> np.ndarray:
    """Moving average by type name (SMA/EMA/WMA/HMA/RMA/VWMA)."""
    return ta.ma(kind, x, n, volume=volume)


def rolling_apply_windows(x: np.ndarray, n: int) -> np.ndarray | None:
    """Contiguous sliding windows (m - n + 1, n) over ``x`` or None when there are fewer than ``n`` values.

    The copy makes per-row reductions bit-identical whatever the series length (exact causality).
    """
    x = ta.f64(x)
    n = max(1, int(n))
    if x.shape[0] < n:
        return None
    return np.ascontiguousarray(np.lib.stride_tricks.sliding_window_view(x, n))
