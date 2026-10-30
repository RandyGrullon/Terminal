"""Online learner of the NEXUS engine (feedback loop, out-of-sample by construction).

- Hedge / multiplicative weights over the experts, one weight vector per regime, with fixed-share mixing so the
  ensemble recovers after regime changes.
- Online logistic regression (AdaGrad + L2) on [expert signals, regime one-hot, volatility rank, bias] followed by
  an online Platt layer and a cold-start shrink toward 0.5 → calibrated ``prob_up``.
- Hit-rate / Brier accumulators of the probabilities it actually published (evaluated when the label arrives).
The state is plain JSON (lists / numbers) so it can be stored in ``model_state`` and used as a prior.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from app.engine.experts import EXPERT_IDS
from app.engine.regime import REGIMES

STATE_VERSION = 1
CAL_BINS = 10
ROLLING_EVERY = 25
ROLLING_MAX = 200
EWM_ALPHA = 0.02
COLD_START = 10.0
CAL_LR = 0.05
REL_ALPHA = 0.01
REL_PRIOR_VAR = 0.0025
REL_PRIOR_SLOPE = 0.3
PRIOR_DIRECT = 0.7
LAYOUT = "pm-hedge+interactions"


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 40.0)))
    e = math.exp(max(z, -40.0))
    return e / (1.0 + e)


def feature_dim(n_experts: int) -> int:
    return 2 * n_experts + len(REGIMES) + 2


def feature_matrix(signals: np.ndarray, regime: np.ndarray, vol_rank: np.ndarray) -> np.ndarray:
    """Learner inputs per bar: [signals (E), signals × trend flag (E), regime one-hot (4), vol rank − 0.5, bias].

    The trend flag is +1 in trending regimes, −1 in ranges and 0 otherwise, so one coefficient per expert can learn
    "follow it in trends, fade it in ranges"."""
    n, e = signals.shape
    if n == 0:
        return np.zeros((0, feature_dim(e)))
    onehot = np.zeros((n, len(REGIMES)))
    ok = regime >= 0
    onehot[np.flatnonzero(ok), regime[ok]] = 1.0
    flag = np.where((regime == 0) | (regime == 1), 1.0, np.where(regime == 2, -1.0, 0.0))
    vr = np.where(np.isfinite(vol_rank), vol_rank, 0.5) - 0.5
    return np.column_stack([signals, signals * flag[:, None], onehot, vr, np.ones(n)])


class NexusLearner:
    def __init__(self, n_experts: int = len(EXPERT_IDS), learning_rate: float = 0.08):
        self.n_experts = int(n_experts)
        self.dim = feature_dim(self.n_experts)
        self.set_learning_rate(learning_rate)
        # Hedge over 2E experts: each expert and its inverse ("fade it"), with a trend-following prior.
        self.prior = np.concatenate([np.full(self.n_experts, PRIOR_DIRECT), np.full(self.n_experts, 1.0 - PRIOR_DIRECT)])
        self.prior /= self.prior.sum()
        self.hedge = np.tile(self.prior, (len(REGIMES), 1))
        self.theta = np.zeros(self.dim)
        self.grad_sq = np.zeros(self.dim)
        self.cal_a = 1.0
        self.cal_b = 0.0
        self.cal_sq = [0.0, 0.0]  # AdaGrad accumulators of the Platt layer
        # Reliability layer: EWM regression of the outcomes on the raw probabilities published at the time
        # (out-of-sample). The published probability keeps the model's recent average level (m_x) and only the
        # fraction β = cov/var of its variation that the outcomes confirmed.
        self.rel_mx = 0.5
        self.rel_my = 0.5
        self.rel_xy = REL_PRIOR_SLOPE * REL_PRIOR_VAR
        self.rel_xx = REL_PRIOR_VAR
        self.updates = 0
        self.trained_until: int | None = None
        self.memory_bars = 0
        self.n_eval = 0
        self.hits = 0
        self.brier_sum = 0.0
        self.hit_ewm: float | None = None
        self.brier_ewm: float | None = None
        self.calib = np.zeros((CAL_BINS, 3))  # count, sum predicted, sum observed
        self.rolling: list[list[float]] = []

    def set_learning_rate(self, learning_rate: float) -> None:
        lr = float(learning_rate) if learning_rate and learning_rate > 0 else 0.08
        self.eta = min(max(3.0 * lr, 0.05), 1.0)  # Hedge
        self.lr_logit = min(max(lr, 0.01), 1.0)  # AdaGrad base step
        self.share = 0.02  # fixed-share mixing
        self.l2 = 3e-3

    # ------------------------------------------------------------------ prediction

    def weights_for(self, regime_code: int) -> np.ndarray:
        """Net weight per expert (direct − inverse), in [-1, 1]; negative = the expert is faded."""
        row = self.hedge[regime_code] if 0 <= regime_code < len(REGIMES) else self.hedge.mean(axis=0)
        e = self.n_experts
        return row[:e] - row[e:]

    def predict(self, x: np.ndarray, signals: np.ndarray, regime_code: int) -> tuple[float, float, float]:
        """(weighted vote in [-1, 1], Platt probability before the reliability shrink, calibrated prob_up)."""
        vote = float(self.weights_for(regime_code) @ signals)
        z = float(self.theta @ x)
        raw = sigmoid(self.cal_a * z + self.cal_b)
        k = self.updates / (self.updates + COLD_START)
        p = self.rel_mx + self.reliability() * (raw - self.rel_mx)  # keep the model's level, shrink the noise
        p = 0.5 + k * (p - 0.5)
        return max(-1.0, min(1.0, vote)), raw, min(max(p, 0.03), 0.97)

    def reliability(self) -> float:
        """0..1 slope of the outcomes on the raw probabilities (0 = their variation carries no proven skill)."""
        return min(max(self.rel_xy / self.rel_xx, 0.0), 1.0) if self.rel_xx > 1e-12 else 0.0

    def agreement(self, signals: np.ndarray, regime_code: int) -> float:
        w = self.weights_for(regime_code)
        den = float(np.abs(w) @ np.abs(signals))
        return abs(float(w @ signals)) / den if den > 1e-9 else 0.0

    # ------------------------------------------------------------------ learning

    def update(self, x: np.ndarray, signals: np.ndarray, regime_code: int, up: int, published_p: float,
               label_time: int, raw_p: float | None = None) -> None:
        """Learn the label of a past bar: `up` = 1 when close[t+h] > close[t]; `published_p` = what we said then and
        `raw_p` = the Platt probability before the reliability shrink at that time (both out-of-sample)."""
        y = 1.0 if up else -1.0
        if 0 <= regime_code < len(REGIMES):
            ys = y * signals
            loss = np.concatenate([(1.0 - ys) * 0.5, (1.0 + ys) * 0.5])
            w = self.hedge[regime_code] * np.exp(-self.eta * loss)
            w /= max(float(w.sum()), 1e-300)
            self.hedge[regime_code] = (1.0 - self.share) * w + self.share * self.prior
        z = float(self.theta @ x)
        p = sigmoid(z)
        g = (p - float(up)) * x + self.l2 * self.theta
        self.grad_sq += g * g
        self.theta -= self.lr_logit * g / (np.sqrt(self.grad_sq) + 1e-8)
        pc = sigmoid(self.cal_a * z + self.cal_b)
        err = pc - float(up)
        ga, gb = err * max(min(z, 5.0), -5.0), err
        self.cal_sq[0] += ga * ga
        self.cal_sq[1] += gb * gb
        self.cal_a = min(max(self.cal_a - CAL_LR * ga / (self.cal_sq[0] ** 0.5 + 1e-8), 0.05), 3.0)
        self.cal_b = min(max(self.cal_b - CAL_LR * gb / (self.cal_sq[1] ** 0.5 + 1e-8), -1.0), 1.0)
        x_obs = raw_p if raw_p is not None and raw_p == raw_p else pc
        dx, dy = x_obs - self.rel_mx, float(up) - self.rel_my
        self.rel_mx += REL_ALPHA * dx
        self.rel_my += REL_ALPHA * dy
        self.rel_xy = (1.0 - REL_ALPHA) * (self.rel_xy + REL_ALPHA * dx * dy)
        self.rel_xx = (1.0 - REL_ALPHA) * (self.rel_xx + REL_ALPHA * dx * dx)
        self._evaluate(published_p, int(up), label_time)
        self.updates += 1
        self.trained_until = int(label_time)

    def _evaluate(self, p: float, up: int, t: int) -> None:
        hit = 1 if (p >= 0.5) == (up == 1) else 0
        brier = (p - up) ** 2
        self.n_eval += 1
        self.hits += hit
        self.brier_sum += brier
        self.hit_ewm = float(hit) if self.hit_ewm is None else self.hit_ewm + EWM_ALPHA * (hit - self.hit_ewm)
        self.brier_ewm = brier if self.brier_ewm is None else self.brier_ewm + EWM_ALPHA * (brier - self.brier_ewm)
        b = min(int(p * CAL_BINS), CAL_BINS - 1)
        self.calib[b, 0] += 1.0
        self.calib[b, 1] += p
        self.calib[b, 2] += up
        if self.n_eval % ROLLING_EVERY == 0:
            self.rolling.append([int(t), round(self.hit_ewm, 4), round(self.brier_ewm, 4)])
            if len(self.rolling) > ROLLING_MAX:
                del self.rolling[: len(self.rolling) - ROLLING_MAX]

    # ------------------------------------------------------------------ metrics

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.n_eval if self.n_eval else None

    @property
    def brier(self) -> float | None:
        return self.brier_sum / self.n_eval if self.n_eval else None

    def skill(self) -> float:
        """0..1: how much better than a coin the recent probabilities were (0.3 while cold)."""
        if self.n_eval < 20 or self.brier_ewm is None:
            return 0.3
        return min(max((0.25 - self.brier_ewm) / 0.05, 0.0), 1.0)

    def weights_dict(self) -> dict[str, dict[str, float]]:
        """regime -> expert -> net weight (direct − inverse; negative = faded)."""
        out: dict[str, dict[str, float]] = {}
        for r in range(len(REGIMES)):
            net = self.weights_for(r)
            out[REGIMES[r]] = {EXPERT_IDS[e]: round(float(net[e]), 4) for e in range(self.n_experts)}
        return out

    def calibration(self) -> list[dict[str, float]]:
        out = []
        for b in range(CAL_BINS):
            cnt, sp, sy = self.calib[b]
            if cnt > 0:
                out.append({"bin": b, "predicted": round(sp / cnt, 4), "observed": round(sy / cnt, 4), "count": int(cnt)})
        return out

    # ------------------------------------------------------------------ persistence

    def to_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "experts": list(EXPERT_IDS[: self.n_experts]),
            "regimes": list(REGIMES),
            "layout": LAYOUT,
            "hedge": [[round(float(v), 8) for v in row] for row in self.hedge],
            "theta": [round(float(v), 8) for v in self.theta],
            "grad_sq": [round(float(v), 8) for v in self.grad_sq],
            "cal_a": round(self.cal_a, 6),
            "cal_b": round(self.cal_b, 6),
            "cal_sq": [round(v, 6) for v in self.cal_sq],
            "rel_mx": round(self.rel_mx, 8),
            "rel_my": round(self.rel_my, 8),
            "rel_xy": round(self.rel_xy, 10),
            "rel_xx": round(self.rel_xx, 10),
            "updates": int(self.updates),
            "trained_until": self.trained_until,
            "memory_bars": int(self.memory_bars),
            "n_eval": int(self.n_eval),
            "hits": int(self.hits),
            "brier_sum": round(self.brier_sum, 6),
            "hit_ewm": None if self.hit_ewm is None else round(self.hit_ewm, 6),
            "brier_ewm": None if self.brier_ewm is None else round(self.brier_ewm, 6),
            "hit_rate": None if self.hit_rate is None else round(self.hit_rate, 6),
            "brier": None if self.brier is None else round(self.brier, 6),
            "calib": [[round(float(v), 6) for v in row] for row in self.calib],
            "rolling": [list(r) for r in self.rolling],
        }

    @classmethod
    def from_state(cls, state: dict | None, learning_rate: float = 0.08) -> "NexusLearner":
        """Rebuild from `to_state()`; incompatible or corrupt parts fall back to fresh values."""
        model = cls(len(EXPERT_IDS), learning_rate)
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return model
        same_layout = (state.get("experts") == list(EXPERT_IDS) and state.get("regimes") == list(REGIMES)
                       and state.get("layout") == LAYOUT)
        try:
            if same_layout:
                hedge = np.asarray(state.get("hedge"), dtype=float)
                theta = np.asarray(state.get("theta"), dtype=float)
                grad_sq = np.asarray(state.get("grad_sq"), dtype=float)
                if hedge.shape == model.hedge.shape and np.all(np.isfinite(hedge)) and np.all(hedge > 0):
                    model.hedge = hedge / hedge.sum(axis=1, keepdims=True)
                if theta.shape == model.theta.shape and np.all(np.isfinite(theta)):
                    model.theta = theta
                if grad_sq.shape == model.grad_sq.shape and np.all(np.isfinite(grad_sq)) and np.all(grad_sq >= 0):
                    model.grad_sq = grad_sq
                model.cal_a = float(min(max(float(state.get("cal_a", 1.0)), 0.05), 3.0))
                model.cal_b = float(min(max(float(state.get("cal_b", 0.0)), -1.0), 1.0))
                cal_sq = state.get("cal_sq")
                if isinstance(cal_sq, list) and len(cal_sq) == 2 and all(isinstance(v, (int, float)) and v >= 0
                                                                          for v in cal_sq):
                    model.cal_sq = [float(cal_sq[0]), float(cal_sq[1])]
                rxy, rxx = state.get("rel_xy"), state.get("rel_xx")
                if isinstance(rxy, (int, float)) and isinstance(rxx, (int, float)) and math.isfinite(rxy)                         and math.isfinite(rxx) and rxx > 0:
                    model.rel_xy, model.rel_xx = float(rxy), float(rxx)
                rmx, rmy = state.get("rel_mx"), state.get("rel_my")
                if isinstance(rmx, (int, float)) and isinstance(rmy, (int, float)) and 0 <= rmx <= 1 and 0 <= rmy <= 1:
                    model.rel_mx, model.rel_my = float(rmx), float(rmy)
                model.updates = max(int(state.get("updates") or 0), 0)
            tu = state.get("trained_until")
            model.trained_until = int(tu) if isinstance(tu, (int, float)) and math.isfinite(tu) else None
            model.memory_bars = max(int(state.get("memory_bars") or 0), 0)
            model.n_eval = max(int(state.get("n_eval") or 0), 0)
            model.hits = min(max(int(state.get("hits") or 0), 0), model.n_eval)
            model.brier_sum = max(float(state.get("brier_sum") or 0.0), 0.0)
            he, be = state.get("hit_ewm"), state.get("brier_ewm")
            model.hit_ewm = float(he) if isinstance(he, (int, float)) and math.isfinite(he) else None
            model.brier_ewm = float(be) if isinstance(be, (int, float)) and math.isfinite(be) else None
            calib = np.asarray(state.get("calib"), dtype=float)
            if calib.shape == model.calib.shape and np.all(np.isfinite(calib)):
                model.calib = calib
            rolling = state.get("rolling") or []
            model.rolling = [[int(r[0]), float(r[1]), float(r[2])] for r in rolling
                             if isinstance(r, (list, tuple)) and len(r) == 3][-ROLLING_MAX:]
        except (TypeError, ValueError):
            return cls(len(EXPERT_IDS), learning_rate)
        return model
