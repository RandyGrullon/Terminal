"""engine-repaint: the recursive primitives, the features, the regime hysteresis and the expert memories continue
bit for bit from a stored state, and finite windows no longer depend on where the series starts."""
from __future__ import annotations

import numpy as np
import pytest

from app.engine import ta
from app.engine.experts import expert_signals
from app.engine.features import (
    PIVOT_LEFT, REC_KEYS, RESUME_LOOKBACK, SR_MAX_AGE, compute_features, feature_state, restore_point,
)
from app.engine.regime import detect_regime
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

# every per-bar array of Features (the swing levels only from the bar whose stored value is carried)
ARRAYS = ("ema_fast", "ema_slow", "ema_mid", "ema_trend", "rsi", "macd", "macd_signal", "macd_hist", "atr",
          "atr_pct", "atr_rank", "sigma", "adx", "pdi", "mdi", "bb_mid", "bb_z", "bb_width", "st_line", "st_dir",
          "dc_hi", "dc_lo", "srsi_k", "srsi_d", "obv_slope", "vol_z", "er", "lr_t", "lr_slope", "range_atr",
          "htf_trend", "div_bear", "div_bull")


@pytest.fixture(scope="module")
def cs():
    return generate_candleset(n=1500, seed=4, tf="1h", end_time=FIXED_END, symbol="SIM:PRIM")


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    """Bitwise equality (NaN == NaN)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return a.shape == b.shape and bool(np.all((a == b) | (np.isnan(a) & np.isnan(b))))


def test_recursive_filters_continue_from_a_stored_state(cs):
    c, h, lo, v = cs.close, cs.high, cs.low, cs.volume
    k = 400
    full = ta.ema(c, 200)
    assert _same(ta.ema(c[k:], 200, init=ta.last_finite(full, k)), full[k:])
    full = ta.rma(c, 14)
    assert _same(ta.rma(c[k:], 14, init=full[k]), full[k:])
    rsi, up, dn = ta.rsi_parts(c, 14)
    assert _same(ta.rsi_parts(c[k:], 14, init=(up[k], dn[k]))[0], rsi[k:])
    line, sig, hist, ef, es = ta.macd_parts(c, 12, 26, 9)
    got = ta.macd_parts(c[k:], 12, 26, 9, init=(ef[k], es[k], sig[k]))
    assert _same(got[0], line[k:]) and _same(got[1], sig[k:]) and _same(got[2], hist[k:])
    pdi, mdi, adx, tr_n, pdm_n, mdm_n = ta.adx_dmi_parts(h, lo, c, 14)
    got = ta.adx_dmi_parts(h[k:], lo[k:], c[k:], 14, init=(tr_n[k], pdm_n[k], mdm_n[k], adx[k]))
    assert all(_same(x, y[k:]) for x, y in zip(got[:3], (pdi, mdi, adx)))
    line, direction, a10, fu, fd = ta.supertrend_parts(h, lo, c, 10, 3.0)
    got = ta.supertrend_parts(h[k:], lo[k:], c[k:], 10, 3.0, init=(a10[k], fu[k], fd[k], direction[k]))
    assert _same(got[0], line[k:]) and _same(got[1], direction[k:])
    obv = ta.obv(c, v)
    assert _same(ta.obv(c[k:], v[k:], init=obv[k]), obv[k:])
    events = np.sign(np.sin(np.arange(len(c)) / 7.0)) * (np.arange(len(c)) % 11 == 0)
    full = ta.decay_events(events, 0.9)
    assert _same(ta.decay_events(events, 0.9, init=(k, full[k]))[k:], full[k:])
    # the public helpers keep their results (they are the cold path of the same code)
    assert _same(ta.supertrend(h, lo, c, 10, 3.0)[0], line) and _same(ta.adx_dmi(h, lo, c, 14)[2], adx)
    assert _same(ta.macd(c)[1], sig) and _same(ta.rsi(c, 14), rsi)
    assert _same(ta.stochrsi(c)[0], ta.stoch_of(rsi)[0])


def test_exact_rolling_sums_do_not_depend_on_the_series_start(cs):
    x = np.asarray(cs.close, dtype=float) * 1000.0  # large values: the running sums round differently per start
    for n in (3, 20, 50):
        ref = ta.rolling_sum(x, n, exact=True)
        for start in (1, 7, 133, 600):
            assert _same(ta.rolling_sum(x[start:], n, exact=True)[n - 1:], ref[start + n - 1:])
        assert np.allclose(ref[n - 1:], ta.rolling_sum(x, n)[n - 1:], rtol=1e-12)
    y = x.copy()
    y[300] = np.nan
    s = ta.rolling_sum(y, 20, exact=True)
    assert np.isnan(s[300:320]).all() and np.isfinite(s[320]) and np.isfinite(s[299])
    # %K == %D when both are pinned at 100: no crossover from rounding noise
    k, d = ta.stoch_of(np.full(60, 70.0) + np.arange(60), 14, 3, 3, exact=True)
    assert np.all(k[20:] == 100.0) and np.all(d[20:] == 100.0)


def test_lookback_covers_every_finite_window_of_a_processed_bar():
    assert RESUME_LOOKBACK >= SR_MAX_AGE + PIVOT_LEFT + 1 and RESUME_LOOKBACK > 200


def test_features_resume_bit_for_bit_from_a_restore_point(cs):
    full = compute_features(cs)
    for anchor in (499, 777, 1300):
        point = restore_point(full, anchor)
        assert point is not None
        w, rec = point
        assert anchor + 1 - w >= RESUME_LOOKBACK and set(REC_KEYS) <= set(rec)
        assert int(full.time[w]) // (4 * 3600) != int(full.time[w - 1]) // (4 * 3600)  # first bar of its HTF bucket
        swing = (anchor - w, full.value("swing_low", anchor, None), full.value("swing_high", anchor, None))
        part = compute_features(cs.slice(w), {"rec": rec, "swing": swing})
        first = anchor + 1 - w  # the first bar a resumed run processes: its finite windows lie after bar w
        assert np.isnan(part.atr_rank[:29]).all()  # the bars before only feed those windows
        for name in ARRAYS:
            assert _same(getattr(part, name)[first:], getattr(full, name)[w + first:]), (anchor, name)
        for name in ("swing_low", "swing_high"):
            assert _same(getattr(part, name)[anchor - w:], getattr(full, name)[anchor:]), (anchor, name)
        # and the state read back at a later bar is the same one
        assert feature_state(part, 600 - (w % 4)) == feature_state(full, w + 600 - (w % 4))


def test_no_restore_point_before_the_filters_warm_up(cs):
    f = compute_features(cs.slice(0, 700))
    assert feature_state(f, 0) is None  # a cold window's first bucket may be partial
    assert feature_state(f, 120) is None or not np.isfinite(f.ema_trend[120])  # EMA 200 not started
    assert restore_point(f, 300) is None  # 300 + 1 - RESUME_LOOKBACK < 200 bars after the cold start
    assert restore_point(f, 600) is not None


def test_regime_hysteresis_and_expert_memories_continue_from_a_stored_state(cs):
    f = compute_features(cs)
    reg, pend, cnt = detect_regime(f, states=True)
    k = 900
    again = detect_regime(f, init=(k, int(reg[k]), pend[k], cnt[k]))
    assert np.array_equal(again[k:], reg[k:]) and np.array_equal(again, detect_regime(f))
    garbled = detect_regime(f, init=(k, 2, 0, 1))
    assert garbled[k] == 2  # the stored state wins at bar k
    sig, decay = expert_signals(f, reg, with_decay=True)
    assert np.array_equal(sig, expert_signals(f, reg))
    sig2, decay2 = expert_signals(f, reg, decay_init=(k, decay[k].tolist()), with_decay=True)
    assert _same(sig2[k:], sig[k:]) and _same(decay2[k:], decay[k:])
