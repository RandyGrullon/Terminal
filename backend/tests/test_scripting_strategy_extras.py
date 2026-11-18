"""PyChart strategy options added for the GenXAU study (#3 / #4): a partial take-profit with breakeven, a time limit,
entries that do not reverse an open position and a cap of entries per UTC day. All off by default: the existing
behaviour is unchanged."""
from __future__ import annotations

import pytest

from app.scripting.runtime import ScriptRuntimeError
from tests.test_scripting_core_strategy import HEADER, T0, make_bars, run


def test_partial_take_profit_then_breakeven_stop():
    # entry at 100 (bar 3), +1.5 at bar 4 (partial 50 % at 101.5), back to the entry at bar 6 (breakeven stop)
    bars = make_bars(overrides={4: (100.0, 102.0, 99.5, 101.0), 6: (100.5, 100.6, 99.0, 99.5)})
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2)\n"
             "strategy.exit('X', 'L', stop=97, limit=110, partial_limit=101.5, partial_pct=50, breakeven=True)\n", bars)
    (tr,) = st["trades"]
    assert tr["exit_reason"] == "BE" and tr["exit_label"] == "Stop en punto de equilibrio" and tr["exit_price"] == 100.0
    assert tr["tp1"] == 101.5 and tr["tp2"] == 110.0  # the partial level, then the final target
    # half at +1.5 and half at 0, risk 3 per unit: +0.25R on the whole position
    assert tr["r_multiple"] == pytest.approx(0.25, abs=1e-6)
    assert tr["pnl"] > 0 and tr["qty"] == pytest.approx(10000 * 0.01 / 3)
    kinds = [m["kind"] for m in st["markers"]]
    assert kinds == ["BUY", "TP1", "EXIT_SL"] and st["markers"][1]["text"] == "TP 50 %"


def test_partial_then_final_target_and_r_of_the_whole_position():
    bars = make_bars(overrides={4: (100.0, 102.0, 99.5, 101.5), 5: (101.5, 104.5, 101.0, 104.0)})
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2)\n"
             "strategy.exit('X', 'L', stop=97, limit=104, partial_limit=101.5, breakeven=True)\n", bars)
    (tr,) = st["trades"]
    assert tr["exit_reason"] == "TP1" and tr["exit_label"] == "Objetivo final"
    assert tr["r_multiple"] == pytest.approx((0.5 * 1.5 + 0.5 * 4.0) / 3.0, abs=1e-4)  # rounded to 4 decimals


def test_time_limit_closes_at_the_open_after_n_bars():
    st = run(HEADER + "strategy.entry('L', strategy.long, when=bar_index == 2)\n"
             "strategy.exit('X', 'L', stop=90, max_bars=3)\n", make_bars())
    (tr,) = st["trades"]
    # in at the open of bar 3; bars 3, 4, 5 held; out at the open of bar 6
    assert tr["exit_reason"] == "TIME" and tr["exit_time"] == T0 + 6 * 3600 and tr["bars"] == 3
    assert st["markers"][-1]["kind"] == "EXIT_TIME"


def test_reverse_false_only_enters_when_flat():
    code = ("strategy.entry('L', strategy.long, when=bar_index == 2, reverse={r})\n"
            "strategy.entry('S', strategy.short, when=bar_index == 4, reverse={r})\n"
            "strategy.exit('X', stop=np.where(bar_index < 100, 90.0, np.nan), max_bars=6)\n")
    flips = run(HEADER + code.format(r=True), make_bars())
    stays = run(HEADER + code.format(r=False), make_bars())
    assert [t["side"] for t in flips["trades"]] == ["long", "short"]  # the default: an opposite entry reverses
    assert [t["side"] for t in stays["trades"]] == ["long"] and stays["trades"][0]["exit_reason"] == "TIME"


def test_max_entries_per_day_caps_the_entries_of_a_utc_day():
    hdr = "strategy('T', initial_capital=10000, risk_pct=1, fee_pct=0, max_entries_per_day=2)\n"
    code = ("strategy.entry('L', strategy.long, when=(bar_index % 2) == 0)\n"
            "strategy.exit('X', 'L', stop=90, max_bars=1)\n")
    capped = run(hdr + code, make_bars(n=24))
    free = run(HEADER + code, make_bars(n=24))
    days = {}
    for t in capped["trades"]:
        days[t["entry_time"] // 86400] = days.get(t["entry_time"] // 86400, 0) + 1
    assert max(days.values()) <= 2 and len(free["trades"]) > len(capped["trades"])
    assert capped["params"]["max_entries_per_day"] == 2 and free["params"]["max_entries_per_day"] is None


def test_bad_values_are_refused_in_spanish():
    for code, msg in (("strategy.exit('X', partial_limit=101, partial_pct=100)", "partial_pct"),
                      ("strategy.exit('X', max_bars=0)", "max_bars"),
                      ("strategy.exit('X')", "max_bars o partial_limit")):
        with pytest.raises(ScriptRuntimeError, match=msg):
            run(HEADER + code + "\n", make_bars())
    with pytest.raises(ScriptRuntimeError, match="max_entries_per_day"):
        run("strategy('T', max_entries_per_day=1.5)\n", make_bars())
