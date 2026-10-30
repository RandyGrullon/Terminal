"""Semáforo rules (conditions.assess), scopes and Spanish formatting — pure and offline."""
from __future__ import annotations

import pytest

from app.config import DEFAULT_SETTINGS
from app.macro import calendar as cal
from app.macro import conditions as C
from app.macro import fmt, fomc
from app.macro.titles import spanish_title
from tests.test_macro_fakes import FF_SAMPLE, calm_quotes, metrics_with

NOW = fomc.ny_time("2026-11-04", 12, 0)  # Wednesday 12:00 New York, US session open
SATURDAY = fomc.ny_time("2026-11-07", 12, 0)
SETTINGS = {**DEFAULT_SETTINGS, "ui.timezone": "America/New_York"}


def ev(title: str, currency: str, minutes: float, impact: str = "high", now: int = NOW) -> dict:
    t = int(now + minutes * 60)
    return {"id": cal.event_id(currency, title, t), "title": title, "title_es": spanish_title(title, currency),
            "currency": currency, "time": t, "impact": impact, "forecast": None, "previous": None,
            "source": "forexfactory"}


def quotes(**over: dict) -> dict:
    q = calm_quotes()
    for k, v in over.items():
        q[k] = {**q.get(k, {}), **v}
    return q


def run(events=(), q=None, scope=None, now=NOW, fng=50, funding=0.0001, **kw):
    return C.assess(list(events), metrics_with(now, q, fng=fng, funding=funding), now, SETTINGS,
                    scope or C.global_scope(), **kw)


CASES = [
    # id, events, quotes overrides, scope symbol, fng, funding, level, blackout, reason fragment
    ("cpi_in_20m", [ev("CPI m/m", "USD", 20)], {}, None, 50, 0.0001, "red", True, "IPC de EE. UU. en 20 min"),
    ("cpi_in_90m", [ev("CPI m/m", "USD", 90)], {}, None, 50, 0.0001, "amber", False, "IPC de EE. UU. en 1 h 30 min"),
    ("cpi_10m_ago", [ev("CPI m/m", "USD", -10)], {}, None, 50, 0.0001, "red", True, "IPC de EE. UU. hace 10 min"),
    ("cpi_45m_ago", [ev("CPI m/m", "USD", -45)], {}, None, 50, 0.0001, "amber", False, "volatilidad residual"),
    ("cpi_3h_ago", [ev("CPI m/m", "USD", -180)], {}, None, 50, 0.0001, "green", False, ""),
    ("cpi_now", [ev("CPI m/m", "USD", 0)], {}, None, 50, 0.0001, "red", True, "se publica ahora"),
    ("medium_soon", [ev("CPI m/m", "USD", 10, "medium")], {}, None, 50, 0.0001, "green", False, ""),
    ("other_ccy", [ev("CPI m/m", "JPY", 10)], {}, None, 50, 0.0001, "green", False, ""),
    ("vix_31", [], {"vix": {"value": 31.0}}, None, 50, 0.0001, "red", False, "VIX 31,0"),
    ("vix_23", [], {"vix": {"value": 23.0}}, None, 50, 0.0001, "amber", False, "VIX 23,0"),
    ("vix_jump_26", [], {"vix": {"value": 20.0, "change_pct": 26.0}}, None, 50, 0.0001, "red", False, "(+26 %)"),
    ("vix_jump_13", [], {"vix": {"value": 18.0, "change_pct": 13.0}}, None, 50, 0.0001, "amber", False, "se dispara"),
    ("es_crash", [], {"es": {"change_pct": -3.2}}, None, 50, 0.0001, "red", False, "Futuros S&P 500 -3,2 %"),
    ("es_move", [], {"es": {"change_pct": 1.6}}, None, 50, 0.0001, "amber", False, "Futuros S&P 500 +1,6 %"),
    ("dxy_move", [], {"dxy": {"change_pct": -0.9}}, None, 50, 0.0001, "amber", False, "Dólar (DXY) -0,90 %"),
    ("us10y_move", [], {"us10y": {"change_abs": 0.12}}, None, 50, 0.0001, "amber", False, "Bono 10 años +12 pb"),
    ("move_high", [], {"move": {"value": 125.0}}, None, 50, 0.0001, "amber", False, "MOVE 125"),
    ("calm", [], {}, None, 50, 0.0001, "green", False, ""),
    ("crypto_fear", [], {}, "BINANCE:BTCUSDT", 10, 0.0001, "amber", False, "Miedo/codicia cripto 10"),
    ("crypto_greed", [], {}, "BINANCE:BTCUSDT", 90, 0.0001, "amber", False, "euforia"),
    ("crypto_funding", [], {}, "BINANCE:BTCUSDT", 50, 0.0009, "amber", False, "Funding BTC 0,090 %"),
    ("global_ignores_fng", [], {}, None, 10, 0.0009, "green", False, ""),
    ("crypto_nasdaq", [], {"nq": {"change_pct": -2.5}}, "BINANCE:BTCUSDT", 50, 0.0001, "amber", False, "Nasdaq -2,5 %"),
    ("eurusd_ecb", [ev("Main Refinancing Rate", "EUR", 10)], {}, "FX:EURUSD", 50, 0.0001, "red", True,
     "Decisión de tipos del BCE en 10 min"),
    ("global_ignores_ecb", [ev("Main Refinancing Rate", "EUR", 10)], {}, None, 50, 0.0001, "green", False, ""),
    ("sim", [ev("CPI m/m", "USD", 5)], {"vix": {"value": 45.0}}, "SIM:NEXUS", 50, 0.0001, "green", False, ""),
]


@pytest.mark.parametrize("cid,events,qover,symbol,fng,funding,level,blackout,fragment", CASES, ids=[c[0] for c in CASES])
def test_rules_table(cid, events, qover, symbol, fng, funding, level, blackout, fragment):
    scope = C.scope_for_symbol(symbol) if symbol else C.global_scope()
    cond = run(events, quotes(**qover), scope, fng=fng, funding=funding)
    assert cond["level"] == level, (cid, cond["headline"], cond["reasons"])
    assert cond["label"] == C.LEVEL_LABEL[level]
    assert cond["blackout"]["active"] is blackout
    if fragment:
        assert any(fragment in r for r in cond["reasons"]), cond["reasons"]
        assert fragment in cond["headline"]
    assert len(cond["reasons"]) <= 3 and len(cond["metrics"]) <= 6 and len(cond["events"]) <= 5
    assert cond["stale"] is False


def test_blackout_advice_and_window():
    cond = run([ev("CPI m/m", "USD", 20)])
    t = NOW + 20 * 60
    assert cond["blackout"] == {"active": True, "until": t + 30 * 60, "event": "IPC de EE. UU. (mensual)"}
    assert cond["headline"] == "IPC de EE. UU. en 20 min: espera a que pase la volatilidad."
    assert cond["advice"] == "No abras posiciones nuevas hasta las 12:50 (30 min después del dato)."
    assert cond["next_event"]["title"] == "CPI m/m" and cond["next_event"]["countdown_s"] == 1200


def test_amber_soon_advice_and_green_text():
    cond = run([ev("Non-Farm Employment Change", "USD", 90)])
    assert cond["advice"] == ("Reduce el tamaño a la mitad y usa stops más amplios; "
                              "no abras nada nuevo desde las 13:00.")
    calm = run([ev("CPI m/m", "USD", 26 * 60)])
    assert calm["headline"] == "Sin eventos de alto impacto en las próximas 2 h y volatilidad normal (VIX 15,2)."
    assert calm["advice"] == "Condiciones normales: sigue tu plan y respeta el filtro NEXUS."
    assert calm["reasons"] == ["Próximo evento importante: IPC de EE. UU. (mañana 14:00)"]
    vix = run([], quotes(vix={"value": 31.2, "change_pct": 28.0}))
    assert vix["headline"] == "VIX 31,2 (+28 %): volatilidad extrema."
    assert vix["advice"].startswith("Volatilidad extrema: no abras posiciones nuevas")
    assert run([], quotes(es={"change_pct": 1.7}))["advice"] == "Reduce el tamaño a la mitad y usa stops más amplios."


def test_fomc_blackout_chains_decision_and_press_conference():
    cond = run([ev("Federal Funds Rate", "USD", 20), ev("FOMC Statement", "USD", 20),
                ev("FOMC Press Conference", "USD", 50)])
    assert cond["level"] == "red"
    assert cond["blackout"]["until"] == NOW + 80 * 60  # press conference + 30 min
    assert cond["headline"].startswith("Decisión de tipos de la Fed en 20 min")  # statement is not "1 dato más"
    assert len(cond["reasons"]) == 1  # the press conference is inside the same window: no extra warning


def test_cluster_counts_extra_releases():
    cond = run([ev("CPI m/m", "USD", 15), ev("Core CPI m/m", "USD", 15), ev("CPI y/y", "USD", 15)])
    assert cond["reasons"][0] == "IPC subyacente de EE. UU. y 2 datos más en 15 min"
    assert len(cond["events"]) == 1


def test_weekend_skips_daily_change_rules_but_not_levels():
    weekend = quotes(vix={"value": 18.0, "change_pct": 30.0}, es={"change_pct": -3.5}, dxy={"change_pct": 1.2})
    cond = run([], weekend, now=SATURDAY)
    assert cond["level"] == "green", cond["reasons"]
    assert "Mercado cerrado" in next(m for m in cond["metrics"] if m["id"] == "us_futures")["note"]
    assert run([], quotes(vix={"value": 31.0}), now=SATURDAY)["level"] == "red"


def test_no_data_and_partial_data():
    cond = C.assess([], {}, NOW, SETTINGS, C.global_scope(), calendar_stale=True)
    assert cond["level"] == "amber" and cond["stale"] is True
    assert cond["headline"] == C.NO_DATA_HEADLINE and cond["metrics"] == []
    assert "Sin datos macro" in cond["advice"]
    fomc_now = C.assess([ev("FOMC Statement", "USD", 10)], {}, NOW, SETTINGS, C.global_scope(), calendar_stale=True)
    assert fomc_now["level"] == "red"  # what we know still counts
    only_calendar = C.assess([ev("CPI m/m", "USD", 600)], {}, NOW, SETTINGS, C.global_scope())
    assert only_calendar["level"] == "green" and only_calendar["stale"] is True
    assert "Sin datos de volatilidad: solo se evalúa el calendario" in only_calendar["reasons"]
    old = metrics_with(NOW - 3600)
    assert C.assess([], old, NOW, SETTINGS, C.global_scope())["stale"] is True


def test_metric_cards_per_scope():
    cond = run([], quotes(vix={"value": 23.5, "change_pct": 4.0}, us10y={"change_abs": -0.008}))
    ids = [m["id"] for m in cond["metrics"]]
    assert ids == ["vix", "us_futures", "dxy", "us10y", "move", "crypto_fng"]
    vix = cond["metrics"][0]
    assert vix == {"id": "vix", "label": "Miedo en bolsa (VIX)", "value": 23.5, "display": "23,50", "change_pct": 4.0,
                   "change_display": "+4,0 %", "level": "warn", "note": "Volatilidad alta: reduce el tamaño"}
    assert cond["metrics"][3]["change_display"] == "-0,8 pb"
    assert "Nasdaq +0,4 %" in cond["metrics"][1]["note"]
    crypto = run([], scope=C.scope_for_symbol("BINANCE:ETHUSDT"))
    assert [m["id"] for m in crypto["metrics"]] == ["vix", "us_futures", "dxy", "crypto_fng", "funding",
                                                   "btc_dominance"]
    assert crypto["metrics"][1]["label"] == "Futuros Nasdaq 100"
    assert crypto["metrics"][4]["display"] == "0,010 %"
    no_move = quotes()
    no_move.pop("move")
    assert run([], no_move)["metrics"][4]["id"] == "oil"
    jump = run([], quotes(vix={"value": 21.2, "change_pct": 23.6}))["metrics"][0]
    assert jump["level"] == "warn" and jump["note"] == "Subiendo con fuerza hoy: reduce el tamaño"
    spike = run([], quotes(vix={"value": 21.2, "change_pct": 31.0}))["metrics"][0]
    assert spike["level"] == "danger" and spike["note"].startswith("Se dispara hoy")


def test_far_events_do_not_fill_the_list():
    far = [ev("FOMC Statement", "USD", 90 * 1440), ev("CPI m/m", "USD", 30 * 1440)]
    cond = run(far)
    assert [e["title"] for e in cond["events"]] == ["CPI m/m"]
    assert cond["next_event"]["title"] == "CPI m/m"


def test_events_list_prefers_high_impact_and_fills_with_medium():
    events = cal.parse_feed(FF_SAMPLE)
    now = fomc.ny_time("2026-10-28", 13, 40)
    cond = C.assess(cal.merge_fomc(events, now - 86400, now + 60 * 86400), metrics_with(now), now, SETTINGS,
                    C.global_scope())
    assert cond["level"] == "red"
    titles = [e["title"] for e in cond["events"]]
    # the statement published with the rate decision is collapsed into it; December's meeting follows
    assert titles == ["Federal Funds Rate", "FOMC Press Conference", "Core PCE Price Index m/m", "FOMC Statement",
                      "FOMC Press Conference"]
    assert cond["events"][3]["source"] == "federalreserve"
    assert all(e["currency"] == "USD" for e in cond["events"])
    assert [e["time"] for e in cond["events"]] == sorted(e["time"] for e in cond["events"])


@pytest.mark.parametrize("symbol,info,sid,currencies", [
    (None, None, "global", []),
    ("SIM:NEXUS", None, "sim", []),
    ("BINANCE:BTCUSDT", None, "crypto", ["USD"]),
    ("FX:EURUSD", None, "forex:EUR/USD", ["EUR", "USD"]),
    ("FX:USDJPY", None, "forex:USD/JPY", ["USD", "JPY"]),
    ("OANDA:XAUUSD", None, "commodities", ["USD"]),
    ("NASDAQ:AAPL", None, "us_equities", ["USD"]),
    ("CME_MINI:ES1!", None, "futures", ["USD"]),
    ("TVC:DXY", None, "indices", ["USD"]),
    ("BME:SAN", None, "stocks:EUR", ["EUR", "USD"]),
    ("XETR:DAX", {"type": "index", "currency": "EUR"}, "indices:EUR", ["EUR", "USD"]),
])
def test_scope_for_symbol(symbol, info, sid, currencies):
    scope = C.scope_for_symbol(symbol, info)
    assert scope["id"] == sid and scope["currencies"] == currencies
    assert scope["crypto"] is (sid == "crypto") and scope["sim"] is (sid == "sim")


def test_spanish_formatting():
    assert fmt.num(28293.5, 1) == "28.293,5" and fmt.num(7568, 0) == "7568" and fmt.num(-0.004, 2) == "0,00"
    assert fmt.pct(-2.4) == "-2,4 %" and fmt.pct(29.67) == "+29,7 %" and fmt.pct(None) == ""
    assert fmt.bp(-0.8) == "-0,8 pb" and fmt.bp(12.3) == "+12 pb"
    assert fmt.duration(5400) == "1 h 30 min" and fmt.duration(7200) == "2 h" and fmt.duration(20) == "menos de 1 min"
    assert fmt.until(1430) == "en 24 min" and fmt.until(-600) == "hace 10 min" and fmt.until(10) == "ahora"
    assert fmt.clock(fomc.ny_time("2026-11-04", 9, 0), "America/New_York") == "9:00"
    assert fmt.day_clock(NOW + 86400, NOW, "America/New_York") == "mañana 12:00"
    assert fmt.day_clock(NOW + 2 * 86400, NOW, "America/New_York") == "vie 12:00"
