"""Alerts (pure parts): which bus events become external alerts, their priority and text ("simulado" for the demo
account), the message composition, quiet hours across midnight in the user's zone, dedupe and the hourly limit."""
from __future__ import annotations

from app.alerts.classify import Alert, classify, collapse, compose, footer, one_line
from app.alerts.policy import DEDUPE_S, Policy, Prefs, in_window, local_minutes, parse_hhmm
from app.config import DEFAULT_SETTINGS
from tests.test_imp_alerts_fakes import (early_exit_event, ev, macro_event, paper_close_event, paper_open_event,
                                         signal_event, utc)


def getter(**overrides):
    values = {**DEFAULT_SETTINGS, **{k.replace("__", "."): v for k, v in overrides.items()}}
    return lambda key, default=None: values.get(key, default)


# ----------------------------------------------------------------------------- classification


def test_settings_defaults_are_opt_in():
    assert DEFAULT_SETTINGS["alerts.telegram_enabled"] is False and DEFAULT_SETTINGS["alerts.email_enabled"] is False
    assert DEFAULT_SETTINGS["alerts.telegram_token"] == "" and DEFAULT_SETTINGS["alerts.smtp_password"] == ""
    assert DEFAULT_SETTINGS["alerts.min_level"] == "warn" and DEFAULT_SETTINGS["alerts.smtp_port"] == 587


def test_macro_alerts_map_level_to_priority():
    red = classify(macro_event("red", "event_soon"))
    assert red is not None and red.category == "macro" and red.priority == "critical"
    assert red.kind == "macro_event_soon" and red.text.startswith("🔴 Decisión de tipos de la Fed en 15 min")
    amber = classify(macro_event("amber", "event_soon", "🟡 IPC de EE. UU. en 1 h: prepárate."))
    assert amber.priority == "warn"
    green = classify(macro_event("green", "blackout_end", "🟢 Terminó la ventana sin operar: puedes operar."))
    assert green.priority == "info" and green.kind == "macro_blackout_end"
    # other semáforo events (provider errors) are not alerts
    assert classify(ev("macro", "warn", "Semáforo: no se pudo leer VIX (timeout)", None)) is None
    assert classify(ev("macro", "warn", "Semáforo: calendario no disponible", {"x": 1})) is None


def test_nexus_entries_exits_early_exits_and_ignored_events():
    buy = classify(signal_event(action="BUY"))
    assert buy.category == "signals" and buy.kind == "entry" and buy.priority == "warn"
    assert buy.symbol == "BINANCE:BTCUSDT" and buy.tf == "1h" and buy.text.startswith("📈 BINANCE:BTCUSDT 1h → COMPRAR")
    sell = classify(signal_event(action="SELL"))
    assert sell.text.startswith("📉") and sell.kind == "entry"
    ex = classify(signal_event(action="EXIT_LONG"))
    assert ex.kind == "exit" and ex.priority == "warn"
    early = classify(early_exit_event())
    assert early.kind == "early_exit" and early.priority == "critical" and early.text.startswith("⚠ BINANCE:ETHUSDT")
    # rejected signals (info), synthetic markets and HOLD actions are not forwarded
    assert classify(ev("nexus", "info", "BINANCE:BTCUSDT 1h: señal de compra rechazada por el filtro",
                       {"symbol": "BINANCE:BTCUSDT", "tf": "1h", "kind": "REJECTED_LONG"})) is None
    assert classify(signal_event(symbol="SIM:NEXUS")) is None
    assert classify(ev("nexus", "signal", "x", {"symbol": "BINANCE:BTCUSDT", "tf": "1h", "action": "HOLD_LONG"})) is None
    assert classify(ev("nexus", "warn", "No se pudo guardar la señal", None)) is None


def test_paper_trades_say_simulated_and_stops_are_critical_when_losing():
    opened = classify(paper_open_event())
    assert opened.category == "paper" and opened.kind == "opened" and opened.priority == "warn"
    assert opened.text.startswith("🧪 Cuenta demo (simulado): Abierto BINANCE:BTCUSDT")
    stop = classify(paper_close_event(reason="SL", pnl=-98.12))
    assert stop.kind == "stop" and stop.priority == "critical" and "simulado" in stop.text
    be = classify(paper_close_event(reason="SL", pnl=0.0))  # break-even stop: not a loss
    assert be.kind == "stop" and be.priority == "warn"
    trail = classify(paper_close_event(reason="TRAIL", pnl=55.0))
    assert trail.kind == "stop" and trail.priority == "warn"
    tp = classify(paper_close_event(reason="TP3", pnl=120.0))
    assert tp.kind == "closed" and tp.priority == "warn" and tp.text.startswith("🏁 Cuenta demo (simulado)")
    partial = classify(ev("paper", "trade", "BINANCE:BTCUSDT largo: TP1 alcanzado (+1,00 R) · 1/3 cerrado a 66.000",
                          {"position_id": 7, "account_id": 1}))
    assert partial.kind == "partial" and partial.priority == "info"
    # the simulator (source sim, account 2) and non-trade paper events are not forwarded
    assert classify(paper_close_event(account=2)) is None
    assert classify({**paper_open_event(), "source": "sim"}) is None
    assert classify(ev("paper", "info", "Auto-trading activado", {"x": 1})) is None


def test_nexus_automatica_trades_have_their_own_opt_in_category():
    """Account 3 (NEXUS automática) trades are category 'autopilot': labelled as NEXUS's own simulated trades, the
    paper engine's 'NEXUS automática · ' prefix not repeated, same priorities as the user's demo account."""
    opened = classify(ev("paper", "trade", "NEXUS automática · Abierto BINANCE:ETHUSDT largo 2 @ 3.000 · SL 2.950",
                         {"position_id": 9, "account_id": 3, "symbol": "BINANCE:ETHUSDT", "side": "long"}))
    assert (opened.category, opened.kind, opened.priority) == ("autopilot", "opened", "warn")
    assert opened.text == "🤖 NEXUS automática (simulado): Abierto BINANCE:ETHUSDT largo 2 @ 3.000 · SL 2.950"
    stop = classify(paper_close_event(reason="SL", pnl=-12.0, account=3))
    assert (stop.category, stop.kind, stop.priority) == ("autopilot", "stop", "critical")
    assert stop.text.startswith("🛑 NEXUS automática (simulado): Cerrado") and stop.key.startswith("autopilot|7|stop|")
    tp = classify(paper_close_event(reason="TP3", pnl=30.0, account=3))
    assert (tp.category, tp.kind, tp.priority) == ("autopilot", "closed", "warn")
    partial = classify(ev("paper", "trade", "NEXUS automática · BINANCE:ETHUSDT largo: TP1 alcanzado",
                          {"position_id": 9, "account_id": 3}))
    assert (partial.category, partial.kind, partial.priority) == ("autopilot", "partial", "info")
    assert classify(ev("paper", "info", "NEXUS automática activada (1h)", {"account_id": 3})) is None  # not a trade
    text, subject = compose([opened])
    assert text.endswith("— NEXUS · operación simulada de la cuenta propia de NEXUS, sin dinero real; la validación "
                         "publicada no encontró ventaja") and subject.startswith("NEXUS · 🤖 NEXUS automática")
    # the opt-in hook can use the category too, and its text always says it is simulated
    hook = classify(ev("paper", "info", "Resumen del día", {"notify": {"category": "autopilot", "priority": "info"}}))
    assert hook.category == "autopilot" and hook.text == "NEXUS automática (simulado): Resumen del día"


def test_opt_in_hook_and_garbage_events():
    hook = classify(ev("paper", "warn", "Límite de pérdida diaria alcanzado: auto-trading en pausa",
                       {"notify": {"priority": "critical", "kind": "risk_limit"}}))
    assert hook.category == "paper" and hook.priority == "critical" and hook.kind == "risk_limit"
    assert hook.text.startswith("Cuenta demo (simulado): Límite de pérdida diaria")
    generic = classify(ev("validation", "warn", "Informe listo", {"notify": {"category": "signals"}}))
    assert generic.category == "signals" and generic.priority == "warn"
    assert classify(ev("validation", "warn", "x", {"notify": {"category": "nope"}})) is None
    assert classify(ev("other", "warn", "x", {"notify": True})) is None  # no category for an unknown source
    for junk in (None, 1, "x", {}, {"data": None}, {"source": "macro", "data": {"alert": "x"}}):
        assert classify(junk) is None


def test_compose_single_batch_footer_and_collapse():
    buy = classify(signal_event(action="BUY"))
    text, subject = compose([buy])
    assert text.splitlines()[0] == buy.text
    assert text.splitlines()[-1] == "— NEXUS · señal del modelo, no es asesoría financiera"
    assert subject.startswith("NEXUS · 📈 BINANCE:BTCUSDT 1h → COMPRAR")
    red = classify(macro_event("red"))
    stop = classify(paper_close_event())
    text, subject = compose([buy, stop, red], ["Además se omitieron 2 avisos por el límite de 20 por hora."])
    lines = text.splitlines()
    assert lines[0] == "NEXUS · 3 avisos" and subject.startswith("NEXUS · 3 avisos: 🛑 Cuenta demo (simulado)")
    # most severe first (stable: critical stop, critical semáforo, then the warn entry)
    assert lines[1].startswith("🛑") and lines[2].startswith("🔴") and lines[3].startswith("📈")
    assert lines[-2].startswith("Además se omitieron 2 avisos")
    assert lines[-1] == ("— NEXUS · operación simulada de la cuenta demo, sin dinero real · semáforo de mercado · "
                         "señal del modelo, no es asesoría financiera")
    two = [classify(signal_event(action="BUY")), classify(signal_event("BINANCE:ETHUSDT", action="SELL"))]
    assert footer(two) == "— NEXUS · señales del modelo, no son asesoría financiera"
    # exit + early exit of the same market in one batch -> only the critical early exit
    ex = classify(signal_event("BINANCE:ETHUSDT", action="EXIT_LONG"))
    early = classify(early_exit_event("BINANCE:ETHUSDT"))
    assert [a.kind for a in collapse([ex, early, buy])] == ["early_exit", "entry"]
    note = Alert("signals", "entry", "warn", "📈 NASDAQ:AAPL 1h → COMPRAR", "k", notes=["datos con ~15 min de retraso"])
    assert note.line() == "📈 NASDAQ:AAPL 1h → COMPRAR · datos con ~15 min de retraso"
    assert one_line("a\n  b\tc") == "a b c" and len(one_line("x" * 5000)) == 600


# ----------------------------------------------------------------------------- policy


def test_quiet_hours_across_midnight_in_user_zone():
    assert parse_hhmm("23:00") == 1380 and parse_hhmm("7:05") == 425 and parse_hhmm("24:00") is None
    assert parse_hhmm("ab") is None and parse_hhmm("") is None
    assert in_window(1390, 1380, 420) and in_window(10, 1380, 420) and not in_window(600, 1380, 420)
    assert in_window(600, 540, 1020) and not in_window(1020, 540, 1020)
    # 2026-07-01 05:30 UTC = 01:30 in New York (EDT) -> inside 23:00–07:00 there, outside for Tokyo (14:30)
    now = utc(2026, 7, 1, 5, 30)
    assert local_minutes(now, "America/New_York") == 90
    ny = Prefs.read(getter(alerts__quiet_enabled=True, ui__timezone="America/New_York"))
    assert ny.quiet_active(now) and ny.tz_label() == "America/New_York"
    tokyo = Prefs.read(getter(alerts__quiet_enabled=True, ui__timezone="Asia/Tokyo"))
    assert not tokyo.quiet_active(now)
    off = Prefs.read(getter(ui__timezone="America/New_York"))
    assert not off.quiet_active(now)
    bad = Prefs.read(getter(alerts__quiet_enabled=True, alerts__quiet_start="25:00"))
    assert not bad.quiet_enabled and bad.problems and "no válidas" in bad.problems[0]
    same = Prefs.read(getter(alerts__quiet_enabled=True, alerts__quiet_start="07:00", alerts__quiet_end="07:00"))
    assert not same.quiet_enabled and "misma hora" in same.problems[0]
    assert Prefs.read(getter(ui__timezone="Mars/Olympus")).tz_label() == "hora del servidor"


def test_prefs_sanitize_levels_limits_and_symbols():
    p = Prefs.read(getter(alerts__min_level="loud", alerts__max_per_hour=100000,
                          alerts__signal_symbols=["binance:btcusdt", " ", "NASDAQ:AAPL"]))
    assert p.min_level == "warn" and p.problems and p.max_per_hour == 200
    assert p.signal_symbols == {"BINANCE:BTCUSDT", "NASDAQ:AAPL"}
    assert Prefs.read(getter(alerts__max_per_hour=0)).max_per_hour == 1
    crit = Prefs.read(getter(alerts__min_level="critical"))
    assert crit.passes_level("critical") and not crit.passes_level("warn")
    assert Prefs.read(getter(alerts__min_level="info")).passes_level("info")


def test_dedupe_rate_limit_notes_and_persistence():
    pol = Policy()
    assert not pol.duplicate("k", 1000.0) and pol.duplicate("k", 1000.0 + DEDUPE_S - 1)
    assert not pol.duplicate("k", 1000.0 + DEDUPE_S + 1)  # window over
    for i in range(3):
        pol.record_sent(2000.0 + i)
    assert pol.used(2010.0) == 3 and not pol.room(2010.0, 3) and pol.room(2010.0, 4)
    assert pol.used(2000.0 + 3600.5) == 2  # the oldest left the rolling hour
    pol.omitted.update(rate=2, quiet=1)
    notes, taken = pol.take_notes(20, quiet_now=True)  # still quiet: only the limit note
    assert notes == ["Además se omitieron 2 avisos por el límite de 20 por hora."] and pol.omitted == {"rate": 0,
                                                                                                       "quiet": 1}
    pol.give_back(taken)
    notes, taken = pol.take_notes(20, quiet_now=False)
    assert len(notes) == 2 and notes[1] == "Durante las horas de silencio se omitió 1 aviso."
    restored = Policy()
    pol.duplicate("fresh", 5000.0)
    restored.load(pol.dump(), 5000.0)
    assert "fresh" in restored.seen and restored.omitted == {"rate": 0, "quiet": 0}
    restored.load({"seen": {"old": 1.0}, "sent": [1.0, "x"], "omitted": {"rate": True}}, 5000.0)
    assert restored.seen == {} and list(restored.sent) == []
