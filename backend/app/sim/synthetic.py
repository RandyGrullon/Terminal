"""Synthetic market generator (deterministic per seed).

Markov regime switching (bull / bear / range / volatile / crash) + GARCH(1,1)-style volatility clustering +
jumps + mean reversion in ranges. OHLC comes from 12 intrabar sub-steps, volume grows with |return|.
Used by the market simulator, Monte Carlo, offline tests and the SIM:* symbols.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from app.data.base import CandleSet
from app.util import floor_time, now_s, tf_seconds

REGIMES: dict[str, dict[str, float]] = {
    # drift per bar (log), volatility multiplier, mean-reversion strength toward the anchor
    "bull": {"drift": 0.0011, "vol_mult": 0.9, "mr": 0.0},
    "bear": {"drift": -0.0012, "vol_mult": 1.1, "mr": 0.0},
    "range": {"drift": 0.0, "vol_mult": 0.7, "mr": 0.08},
    "volatile": {"drift": 0.0, "vol_mult": 2.2, "mr": 0.015},
    "crash": {"drift": -0.007, "vol_mult": 3.0, "mr": 0.0},
}
# Regime switch weights for "auto" mode (row = from, values = relative weights to the target regimes).
_TRANSITIONS: dict[str, dict[str, float]] = {
    "bull": {"range": 0.5, "volatile": 0.25, "bear": 0.25},
    "bear": {"range": 0.5, "volatile": 0.3, "bull": 0.2},
    "range": {"bull": 0.4, "bear": 0.35, "volatile": 0.25},
    "volatile": {"range": 0.4, "bull": 0.25, "bear": 0.25, "crash": 0.1},
    "crash": {"volatile": 0.6, "range": 0.4},
}
_MEAN_DURATION = {"bull": 160, "bear": 140, "range": 120, "volatile": 60, "crash": 12}
_SUBSTEPS = 12


@dataclass
class SyntheticConfig:
    seed: int = 7
    start_price: float = 100.0
    tf: str = "1h"
    regime: str = "auto"  # auto | bull | bear | range | volatile | crash
    vol: float = 0.011  # base per-bar volatility (log)
    drift: float = 0.0  # extra per-bar drift
    jump_prob: float = 0.004  # per bar
    base_volume: float = 1000.0
    start_time: int | None = None  # open time of the first bar (unix s)
    symbol: str = "SIM:NEXUS"


def _decimals(price: float) -> int:
    if price >= 1000:
        return 2
    if price >= 10:
        return 3
    if price >= 1:
        return 4
    return 6


class SyntheticMarket:
    def __init__(self, cfg: SyntheticConfig | None = None, **kw):
        self.cfg = cfg or SyntheticConfig(**kw)
        self.rng = np.random.default_rng(self.cfg.seed)
        self.tf_s = tf_seconds(self.cfg.tf)
        start = self.cfg.start_time if self.cfg.start_time is not None else now_s() - 2000 * self.tf_s
        self.next_time = floor_time(start, self.cfg.tf)
        self.price = float(self.cfg.start_price)
        self.anchor = self.price
        self.fixed = self.cfg.regime != "auto"
        self._regime = self.cfg.regime if self.fixed else "range"
        if self._regime not in REGIMES:
            raise ValueError(f"Régimen desconocido: {self._regime}")
        self.var = (self.cfg.vol * REGIMES[self._regime]["vol_mult"]) ** 2
        self.bars = 0

    @property
    def regime(self) -> str:
        return self._regime

    def set_regime(self, name: str) -> None:
        if name == "auto":
            self.fixed = False
            return
        if name not in REGIMES:
            raise ValueError(f"Régimen desconocido: {name}")
        self.fixed = True
        self._switch(name)

    def _switch(self, name: str) -> None:
        self._regime = name
        if name == "range":
            self.anchor = self.price

    def _maybe_switch(self) -> None:
        if self.fixed:
            return
        if self.rng.random() < 1.0 / _MEAN_DURATION[self._regime]:
            targets = _TRANSITIONS[self._regime]
            names = list(targets)
            weights = np.array([targets[n] for n in names], dtype=float)
            self._switch(names[int(self.rng.choice(len(names), p=weights / weights.sum()))])

    def next_bar(self) -> dict:
        self._maybe_switch()
        p = REGIMES[self._regime]
        base_var = (self.cfg.vol * p["vol_mult"]) ** 2
        alpha, beta = 0.08, 0.9
        omega = base_var * (1 - alpha - beta)
        sigma = math.sqrt(self.var)
        sub_sigma = sigma / math.sqrt(_SUBSTEPS)
        mu = (p["drift"] + self.cfg.drift) / _SUBSTEPS

        price = self.price
        path = [price]
        for _ in range(_SUBSTEPS):
            mr = p["mr"] * (math.log(self.anchor) - math.log(price)) / _SUBSTEPS
            shock = self.rng.standard_normal() * sub_sigma
            if self.rng.random() < 0.04:  # fat tails
                shock *= 2.5
            jump = 0.0
            if self.rng.random() < self.cfg.jump_prob / _SUBSTEPS:
                bias = -1.5 if self._regime == "crash" else 0.0
                jump = (self.rng.standard_normal() + bias) * sigma * 3.0
            price = price * math.exp(mu + mr + shock + jump - 0.5 * sub_sigma * sub_sigma)
            path.append(price)

        o, c = path[0], path[-1]
        wick = abs(self.rng.normal(0.0, sub_sigma * 0.35))
        h = max(path) * (1 + wick)
        lo = min(path) * (1 - abs(self.rng.normal(0.0, sub_sigma * 0.35)))
        r = math.log(c / o)
        self.var = min(max(omega + alpha * r * r + beta * self.var, base_var * 0.2), base_var * 25)
        if self._regime != "range":
            self.anchor = self.anchor * 0.98 + c * 0.02
        z = abs(r) / max(sigma, 1e-9)
        volume = self.cfg.base_volume * (0.6 + 0.6 * z) * float(np.exp(self.rng.normal(0.0, 0.35)))
        if self._regime in ("volatile", "crash"):
            volume *= 1.8

        d = _decimals(c)
        bar = {
            "time": int(self.next_time),
            "open": round(o, d),
            "high": round(h, d),
            "low": round(lo, d),
            "close": round(c, d),
            "volume": round(volume, 2),
        }
        bar["high"] = max(bar["high"], bar["open"], bar["close"])
        bar["low"] = min(bar["low"], bar["open"], bar["close"])
        self.next_time += self.tf_s
        self.price = c
        self.bars += 1
        return bar

    def history(self, n: int) -> list[dict]:
        return [self.next_bar() for _ in range(int(n))]


def generate_candleset(
    n: int = 1500,
    seed: int = 7,
    tf: str = "1h",
    regime: str = "auto",
    start_price: float = 100.0,
    symbol: str = "SIM:NEXUS",
    end_time: int | None = None,
    vol: float = 0.011,
) -> CandleSet:
    """Deterministic synthetic history of `n` closed bars ending before `end_time` (default: now)."""
    tf_s = tf_seconds(tf)
    end = floor_time(end_time if end_time is not None else now_s(), tf)
    start = end - int(n) * tf_s
    market = SyntheticMarket(
        SyntheticConfig(seed=seed, start_price=start_price, tf=tf, regime=regime, start_time=start, symbol=symbol, vol=vol)
    )
    return CandleSet.from_rows(market.history(n), symbol, tf, provider="synthetic", history="synthetic")
