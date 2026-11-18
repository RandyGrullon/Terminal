"""The GenXAU #3 / #4 study script (app/scripting/examples/genxau_macd_h4.py) follows the research's rules on 30m bars:
entries only in the London-New York overlap (12–16 UTC) and never in XM's rollover hours, on Friday from 21:00 or on
Monday before 02:00 of its server (GMT+2 / GMT+3 with the US DST); nothing left open past Friday 23:00 of the server;
a 50 % partial take-profit; the time limit of each variant; and its label says it failed the holdout."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.scripting.examples import EXAMPLES_DIR
from app.scripting.runtime import Bars, ScriptContext, build_namespace, collect_result, make_print
from app.scripting.validator import build_code
from app.sim.synthetic import generate_candleset

CODE = (EXAMPLES_DIR / "genxau_macd_h4.py").read_text(encoding="utf-8")
END = int(datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc).timestamp())  # a Friday, after the week's close


def _server(ts: int) -> datetime:
    """XM's server time: GMT+3 while the US is on daylight saving time, else GMT+2 (as in GenXAU)."""
    from zoneinfo import ZoneInfo

    dst = bool(datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).dst())
    return datetime.fromtimestamp(ts + (3 if dst else 2) * 3600, timezone.utc)


def _run(variant: str) -> dict:
    cs = generate_candleset(n=6000, seed=11, tf="30m", end_time=END, symbol="SIM:NEXUS")
    bars = Bars(cs.time, cs.open, cs.high, cs.low, cs.close, cs.volume)
    code_obj, errors = build_code(CODE)
    assert not errors, errors
    ctx = ScriptContext(bars, {"variante": variant}, symbol="SIM:NEXUS", tf="30m")
    ns = build_namespace(ctx)
    ns.update(np=np, math=math, print=make_print(ctx.stdout))
    exec(code_obj, ns)
    res = collect_result(ctx)
    return res


@pytest.mark.parametrize("variant,max_bars", [("#3", 24), ("#4", 8)])
def test_genxau_rules_on_30m_bars(variant, max_bars):
    res = _run(variant)
    st = res["strategy"]
    assert "falló el holdout — solo estudio" in res["meta"]["title"]
    trades = st["trades"]
    assert len(trades) >= 10
    closed = [t for t in trades if t["exit_reason"] != "OPEN"]
    for t in trades:
        utc = datetime.fromtimestamp(t["entry_time"], timezone.utc)
        assert 12 <= utc.hour < 16, utc  # the London-New York overlap only
        srv = _server(t["entry_time"])
        assert not (srv.hour >= 23 or srv.hour < 1), srv  # never around XM's rollover
        assert not (srv.weekday() == 4 and srv.hour >= 21) and not (srv.weekday() == 0 and srv.hour < 2), srv
        assert srv.weekday() < 5, srv  # nor on a weekend (the data here trades 24/7, as Binance's gold perpetual)
    for t in closed:
        assert t["bars"] <= max_bars  # the time limit of the variant
        # nothing held past Friday 23:00 of the server (the forced close fills at that bar's open)
        a, b = _server(t["entry_time"]), _server(t["exit_time"])
        friday = (a - timedelta(days=a.weekday())).replace(hour=0, minute=0, second=0) + timedelta(days=4, hours=23)
        assert b <= friday, (a, b)
    assert any(t["exit_reason"] == "TIME" for t in closed)
    assert any(m["kind"] == "TP1" and m["text"] == "TP 50 %" for m in st["markers"])  # the partial take-profit
    days: dict[int, int] = {}
    for t in trades:
        days[t["entry_time"] // 86400] = days.get(t["entry_time"] // 86400, 0) + 1
    assert max(days.values()) <= 4 and st["params"]["max_entries_per_day"] == 4
    if variant == "#4":
        assert not any(t["exit_reason"] == "REVERSE" for t in closed)  # #4 never reverses


def test_the_study_label_is_printed_and_the_h4_approximation_said():
    out = str(_run("#3").get("stdout") or "")
    assert "FALLÓ EL HOLDOUT" in out and "+1,9R, PF 1,06" in out and "EMA 400" in out
