"""NEXUS's own demo account (account 3, "NEXUS automática"): off by default; once on it trades by itself on the bar
that just closed (NEXUS BUY / SELL + gate TOMAR opens, NEXUS's exit rules close), apart from the user's account, with
candidates from a fixed list or the newest finished scan's TOMAR opportunities, within its position cap (atomic when
several bars close together) and the portfolio risk rules of its own account. API, WS message and the decision log."""
from __future__ import annotations

import asyncio

import pytest

from app import config as nexus_config
from app.sim import paper as pp
from app.util import floor_time, now_s, tf_seconds
from tests.test_sim_fakes import FakeHub, make_current

SYM = "BINANCE:ETHUSDT"
BTC = "BINANCE:BTCUSDT"
SOL = "BINANCE:SOLUSDT"
H = 3600


def _candle(t: int, c: float = 100.0) -> dict:
    return {"time": t, "open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": 1.0}


def _last_closed(tf: str = "1h") -> int:
    return floor_time(now_s(), tf) - tf_seconds(tf)


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


class BarNexus:
    """NexusService stand-in: `closed[t]` answers the closed bar t, `default` any other closed bar (the test's clock
    and the engine's may straddle an hour); `delay` makes overlapping decisions interleave."""

    def __init__(self, default: dict | None = None, delay: float = 0.0):
        self.default = default
        self.delay = delay
        self.closed: dict[int, dict] = {}
        self.calls: list[tuple[str, int | None]] = []

    async def current(self, symbol: str, tf: str, *, upto: int | None = None) -> dict | None:
        self.calls.append((symbol, upto))
        if self.delay:
            await asyncio.sleep(self.delay)
        if upto is None:
            return None
        return self.closed.get(int(upto), self.default)


class FakeScanner:
    def __init__(self, run: dict | None = None, rows: list[dict] | None = None):
        self.run = run
        self.rows = rows or []

    async def results(self, run_id: int | None = None):
        return self.run, list(self.rows)


def _bare(watching: list[dict]) -> list[dict]:
    """Watched symbols without NEXUS's latest read (`last`, checked on its own)."""
    return [{k: v for k, v in w.items() if k != "last"} for w in watching]


async def _settle(engine: pp.PaperEngine) -> None:
    """Wait for the engine's tasks, including the ones they spawn."""
    for _ in range(50):
        pending = [t for t in engine._tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("the paper engine's tasks never settled")


async def _engine(rt, prices: dict[str, float], nexus: BarNexus) -> pp.PaperEngine:
    await rt.settings.update({"risk.slippage_pct": 0.0})
    rt.hub = FakeHub(prices=prices)
    rt.nexus = nexus
    engine = pp.PaperEngine(rt)
    await engine.start()
    return engine


def test_autopilot_is_off_by_default_with_its_own_account(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("BUY")))
            snap = await engine.autopilot_snapshot()
            acct = snap["account"]
            assert (acct["id"], acct["name"], acct["balance"], acct["auto_trade"]) == (3, "NEXUS automática", 10000.0,
                                                                                      False)
            st = snap["autopilot"]
            assert (st["enabled"], st["account_id"], st["source"], st["tf"], st["symbols"]) == (False, 3, "scanner",
                                                                                               "1h", [])
            assert (st["max_positions"], st["risk_pct"], st["scanner_top"]) == (5, 1.0, 10)
            assert st["watching"] == [] and st["scan"] is None and st["log"] == []
            assert "no encontró ventaja" in st["note"] and "bróker" in st["note"]
            assert snap["positions"] == [] and snap["history"] == [] and snap["risk"]["account_id"] == 3
            assert engine.status()["autopilot"] == {"enabled": False, "open_positions": 0, "watching": 0}
            # a closed bar decides nothing while it is off
            await engine._on_candle(SYM, "1h", _candle(_last_closed()), True)
            await _settle(engine)
            assert rt.nexus.calls == [] and engine.open_positions(3) == []
            await engine.stop()

    asyncio.run(scenario())


def test_list_mode_trades_on_its_own_account_and_follows_nexus_exits(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("BUY")))
            sent: list[tuple[str, dict]] = []
            rt.ws.publish = lambda topic, msg: sent.append((topic, msg))
            snap = await engine.set_autopilot(enabled=True, source="list", symbols=[SYM.lower(), SYM], tf="1h",
                                              risk_pct=0.5)
            assert snap["autopilot"]["symbols"] == [SYM]
            assert _bare(snap["autopilot"]["watching"]) == [{"symbol": SYM, "why": "lista", "direction": None}]
            await _settle(engine)  # a new candidate is decided at once, on its last closed bar
            t = engine._auto_bar[(3, SYM, "1h")]
            # NEXUS's read of that bar is shown next to the symbol
            assert engine.autopilot_status()["watching"][0]["last"] == {"bar_time": t * 1000, "tf": "1h",
                                                                          "action": "BUY", "label": "COMPRAR"}
            assert [upto for _, upto in rt.nexus.calls] in ([_last_closed()], [_last_closed() - H])
            pos = engine.open_positions(3)
            assert len(pos) == 1 and pos[0]["origin"] == "autopilot" and pos[0]["side"] == "long"
            # 0.5 % of the account's 10 000 at risk between the entry and the stop
            assert pos[0]["qty"] * abs(pos[0]["entry_price"] - pos[0]["sl"]) == pytest.approx(50.0, rel=1e-6)
            assert engine.open_positions(1) == [] and await engine.history(1) == []  # the user's account: untouched
            log = engine.autopilot_status()["log"]
            assert [e["kind"] for e in log] == ["open", "info"] and log[1]["text"] == "Activada"
            assert log[0]["symbol"] == SYM and log[0]["text"].startswith("Abre largo a ") and "TOMAR" in log[0]["text"]
            mine = [m for topic, m in sent if topic == "paper" and m.get("type") == "paper_autopilot"]
            assert mine and mine[-1]["account"]["id"] == 3 and len(mine[-1]["positions"]) == 1
            assert mine[-1]["autopilot"]["enabled"] is True and mine[-1]["risk"]["account_id"] == 3
            assert not [m for _, m in sent if m.get("type") == "paper"]  # nothing published as the user's account
            events = [e["message"] for e in rt.bus.recent()]
            assert any(m.startswith("NEXUS automática activada (1h): opera sola con " + SYM) for m in events)
            # its trades say whose they are in the event log (and never reach the user's alerts: account 3)
            assert any(m.startswith(f"NEXUS automática · Abierto {SYM} largo") for m in events)
            # the next closed bar: NEXUS leaves the long -> its own account follows
            rt.nexus.closed[t + H] = make_current("EXIT_LONG", time=t + H, prob_up=0.4, score=-0.3)
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await _settle(engine)
            assert engine.open_positions(3) == []
            hist = await engine.history(3)
            assert len(hist) == 1 and hist[0]["exit_reason"] == "EARLY" and hist[0]["origin"] == "autopilot"
            assert engine.autopilot_status()["log"][0]["kind"] == "close"
            assert engine.autopilot_status()["log"][0]["text"].startswith("Cierra largo a ")
            assert any(e["message"].startswith(f"NEXUS automática · Cerrado {SYM} largo") for e in rt.bus.recent())
            assert (await engine.account(3))["trades"] == 1 and (await engine.account(1))["trades"] == 0
            await engine.stop()

    asyncio.run(scenario())


def test_user_auto_trading_and_nexus_account_are_independent(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("BUY")))
            await engine.set_auto(True, [SYM], "1h")
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            t = engine._auto_bar[(3, SYM, "1h")]
            await engine._on_candle(SYM, "1h", _candle(t), True)  # that bar again: only the user's trader decides
            await _settle(engine)
            assert [p["origin"] for p in engine.open_positions(1)] == ["auto"]
            assert [p["origin"] for p in engine.open_positions(3)] == ["autopilot"]
            assert len(rt.nexus.calls) == 2  # one decision per account, the hub's resent bar is not decided twice
            # the user closes their own position: NEXUS's stays
            await engine.close_position(None, None, True, account_id=1)
            assert engine.open_positions(1) == [] and len(engine.open_positions(3)) == 1
            # NEXUS's exit closes only its own account's position; the user's flat auto-trader opens nothing on it
            rt.nexus.closed[t + H] = make_current("EXIT_LONG", time=t + H, prob_up=0.4, score=-0.3)
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await _settle(engine)
            assert engine.open_positions(1) == [] and engine.open_positions(3) == []
            assert [h["exit_reason"] for h in await engine.history(3)] == ["EARLY"]
            assert [h["exit_reason"] for h in await engine.history(1)] == ["MANUAL"]
            await engine.stop()

    asyncio.run(scenario())


def test_position_cap_holds_when_bars_close_together(runtime_factory, no_background):
    """All the candidates' bars close at the same second: their decisions overlap, yet the cap is never exceeded."""

    async def scenario():
        async with runtime_factory() as rt:
            nexus = BarNexus(default=make_current("BUY"), delay=0.02)
            engine = await _engine(rt, {SYM: 100.0, BTC: 100.0, SOL: 100.0}, nexus)
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM, BTC, SOL], max_positions=2)
            await _settle(engine)
            assert len(nexus.calls) == 3
            assert len(engine.open_positions(3)) == 2
            skips = [e for e in engine.autopilot_status()["log"] if e["kind"] == "skip"]
            assert len(skips) == 1 and "máximo 2" in skips[0]["text"]
            assert skips[0]["text"].startswith("Largo descartado: ya hay 2 posiciones abiertas")
            await engine.stop()

    asyncio.run(scenario())


def test_portfolio_risk_rules_apply_to_nexus_account_on_its_own(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            await rt.settings.update({"paper.max_positions_per_group": 1})
            engine = await _engine(rt, {SYM: 100.0, BTC: 100.0}, BarNexus(default=make_current("BUY"), delay=0.01))
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM, BTC])
            await _settle(engine)
            assert len(engine.open_positions(3)) == 1  # crypto counts as one correlated group (max 1)
            skip = [e for e in engine.autopilot_status()["log"] if e["kind"] == "skip"]
            assert len(skip) == 1 and "grupo correlacionado" in skip[0]["text"]
            # the user's account has its own room: one crypto position opens there
            res = await engine.open_position(1, BTC, "long", "1h", check_gate=False)
            assert res["position"]["account_id"] == 1
            risk3 = await engine.risk_state(3)
            assert risk3["open_positions"] == 1 and risk3["groups"][0]["full"] is True
            await engine.stop()

    asyncio.run(scenario())


def test_gate_must_say_tomar(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            nexus = BarNexus(default=make_current("BUY", long_verdict="WAIT"))
            engine = await _engine(rt, {SYM: 100.0}, nexus)
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            assert engine.open_positions(3) == []
            entry = engine.autopilot_status()["log"][0]
            assert entry["kind"] == "skip" and entry["text"].startswith("Largo descartado por el filtro (ESPERAR")
            # no signal (ESPERAR / MANTENER) is no trade and no log line
            nexus.default = make_current("WAIT")
            t = engine._auto_bar[(3, SYM, "1h")]
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await _settle(engine)
            assert engine.open_positions(3) == [] and len(engine.autopilot_status()["log"]) == 2
            await engine.stop()

    asyncio.run(scenario())


def test_scanner_mode_watches_the_newest_scan_tomar_opportunities(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            nexus = BarNexus(default=make_current("BUY"))
            nexus_short = make_current("SELL", prob_up=0.3, score=-0.5, long_verdict="AVOID", short_verdict="TAKE")
            engine = await _engine(rt, {SYM: 100.0, SOL: 100.0, BTC: 100.0, "BINANCE:ADAUSDT": 100.0}, nexus)
            # a failed scan is no source, and a scan still running is not used until it finishes
            rt.scanner = FakeScanner({"id": 5, "status": "error", "finished_at": 1_759_999_000_000},
                                     [{"symbol": SOL, "opportunity": True, "direction": "long"}])
            snap = await engine.set_autopilot(enabled=True, scanner_top=2)
            assert snap["autopilot"]["watching"] == [] and snap["autopilot"]["scan"] is None
            rt.scanner = FakeScanner({"id": 6, "status": "running", "finished_at": None},
                                     [{"symbol": SOL, "opportunity": True, "direction": "long"}])
            await engine._refresh_autopilot(force=True)
            await _settle(engine)
            st = engine.autopilot_status()
            assert st["watching"] == [] and st["scan"] == {"id": 6, "finished_at": None, "opportunities": 0}
            assert nexus.calls == []
            rows = [
                {"symbol": SOL, "opportunity": True, "direction": "long"},
                {"symbol": BTC, "opportunity": False, "direction": "long"},  # favourable, not an opportunity
                {"symbol": SYM, "opportunity": True, "direction": "short"},
                {"symbol": "BINANCE:ADAUSDT", "opportunity": True, "direction": "long"},  # beyond the top 2
            ]
            rt.scanner = FakeScanner({"id": 7, "status": "done", "finished_at": 1_760_000_000_000}, rows)
            calls_before = len(nexus.calls)

            async def answer(symbol, tf, *, upto=None):  # SYM: NEXUS signals the short the scan found
                nexus.calls.append((symbol, upto))
                return nexus_short if symbol == SYM else nexus.default

            nexus.current = answer
            await engine._refresh_autopilot(force=True)
            await _settle(engine)
            st = engine.autopilot_status()
            assert _bare(st["watching"]) == [{"symbol": SYM, "why": "escáner", "direction": "short"},
                                             {"symbol": SOL, "why": "escáner", "direction": "long"}]
            assert [w["last"]["label"] for w in st["watching"]] == ["VENDER", "COMPRAR"]
            assert st["scan"] == {"id": 7, "finished_at": 1_760_000_000_000, "opportunities": 3}
            assert len(nexus.calls) - calls_before == 2
            sides = {p["symbol"]: p["side"] for p in engine.open_positions(3)}
            assert sides == {SYM: "short", SOL: "long"}
            # a newer scan without SYM: SYM is still watched while NEXUS holds it (its exits are its own)
            rt.scanner = FakeScanner({"id": 8, "status": "done", "finished_at": 1_760_000_100_000}, rows[:1])
            await engine._refresh_autopilot(force=True)
            await _settle(engine)
            st = engine.autopilot_status()
            assert {(w["symbol"], w["why"]) for w in st["watching"]} == {(SOL, "escáner"), (SYM, "posición abierta")}
            assert SYM in engine._autopilot_profile().symbols
            await engine.stop()

    asyncio.run(scenario())


def test_missing_analysis_errors_and_a_new_timeframe(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            nexus = BarNexus(default=None)  # no analysis for the closed bar (e.g. no data for this market)
            engine = await _engine(rt, {SYM: 100.0, BTC: 100.0}, nexus)
            sent: list[dict] = []
            rt.ws.publish = lambda topic, msg: sent.append(msg)
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            entry = engine.autopilot_status()["log"][0]
            assert entry["kind"] == "info" and entry["text"].startswith("Sin análisis de NEXUS en la vela cerrada (1h)")
            read = engine.autopilot_status()["watching"][0]["last"]
            assert (read["action"], read["label"]) == (None, "Sin análisis (faltan datos)")
            assert sent[-1]["type"] == "paper_autopilot" and sent[-1]["autopilot"]["log"][0] == entry  # shown at once

            async def boom(symbol, tf, *, upto=None):
                raise RuntimeError("sin conexión")

            nexus.current = boom
            await engine.set_autopilot(symbols=[SYM, BTC])
            await _settle(engine)
            entry = engine.autopilot_status()["log"][0]
            assert entry["symbol"] == BTC and entry["text"] == ("No se pudo analizar (RuntimeError: sin conexión); lo "
                                                                "reintenta en la próxima vela")
            reads = {w["symbol"]: w["last"] for w in engine.autopilot_status()["watching"]}
            assert reads[BTC]["label"] == "Error al analizar"
            assert engine.open_positions(3) == []
            # a new timeframe: every candidate is decided at once on its own last closed bar
            del nexus.current
            nexus.default = make_current("BUY")
            await engine.set_autopilot(tf="4h")
            await _settle(engine)
            assert sorted((p["symbol"], p["tf"]) for p in engine.open_positions(3)) == [(BTC, "4h"), (SYM, "4h")]
            assert {w["last"]["tf"] for w in engine.autopilot_status()["watching"]} == {"4h"}  # reads of the new tf
            assert engine._auto_bar[(3, SYM, "4h")] in (_last_closed("4h"), _last_closed("4h") - 4 * H)
            await engine.stop()

    asyncio.run(scenario())


def test_pause_keeps_positions_and_reset_clears_only_its_account(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("BUY")))
            await engine.open_position(1, SYM, "long", "1h", check_gate=False)  # the user's own position
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            assert len(engine.open_positions(3)) == 1
            snap = await engine.set_autopilot(enabled=False)
            assert snap["autopilot"]["enabled"] is False and _bare(snap["autopilot"]["watching"]) == [
                {"symbol": SYM, "why": "posición abierta", "direction": "long"}]
            assert engine.autopilot_status()["log"][0]["text"].startswith("Pausada")
            assert engine._auto_profiles() == []  # paused: no decisions; the stop and targets still manage it
            t = engine._auto_bar[(3, SYM, "1h")]
            rt.nexus.closed[t + H] = make_current("EXIT_LONG", time=t + H, prob_up=0.4, score=-0.3)
            await engine._on_candle(SYM, "1h", _candle(t + H), True)
            await _settle(engine)
            assert len(engine.open_positions(3)) == 1
            await engine.reset(None, account_id=3)
            assert engine.open_positions(3) == [] and engine.autopilot_status()["log"] == []
            assert (await engine.account(3))["balance"] == 10000.0
            assert len(engine.open_positions(1)) == 1  # the user's account is untouched
            await engine.stop()

    asyncio.run(scenario())


def test_settings_saved_one_by_one_are_read_once_settled(runtime_factory, no_background, monkeypatch):
    """Ajustes saves the keys one at a time: enabling first must not trade the scanner's candidates before the
    list (also being saved) arrives."""
    monkeypatch.setattr(pp, "AUTOPILOT_SETTLE_S", 0.05)

    async def scenario():
        async with runtime_factory() as rt:
            nexus = BarNexus(default=make_current("BUY"))
            engine = await _engine(rt, {SYM: 100.0, SOL: 100.0}, nexus)
            rt.scanner = FakeScanner({"id": 1, "status": "done", "finished_at": 1}, [
                {"symbol": SOL, "opportunity": True, "direction": "long"}])
            # each key is in memory as soon as its own save starts: enabled + list first, the source last
            await rt.settings.update({"autopilot.enabled": True, "autopilot.symbols": [SYM],
                                      "autopilot.source": "list"})
            await _settle(engine)
            assert [s for s, _ in nexus.calls] == [SYM]
            assert [p["symbol"] for p in engine.open_positions(3)] == [SYM]
            assert engine.autopilot_status()["log"][-1]["text"] == "Activada"
            await engine.stop()

    asyncio.run(scenario())


def test_decision_log_and_reads_survive_a_restart(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus(default=make_current("BUY")))
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            log, reads = engine.autopilot_status()["log"], dict(engine._autopilot_last)
            assert [e["kind"] for e in log] == ["open", "info"] and reads[SYM]["label"] == "COMPRAR"
            await engine.stop()
            again = pp.PaperEngine(rt)  # the server restarts on the same database
            await again.start()
            assert again.autopilot_status()["log"] == log and again._autopilot_last == reads
            await again._refresh_autopilot(force=True)  # the monitor rebuilds its candidates, each with its read
            await _settle(again)
            assert again.autopilot_status()["watching"][0]["last"]["label"] == "COMPRAR"
            await again.reset(None, account_id=3)  # a reset empties them, and that is saved too
            await _settle(again)
            await again.stop()
            third = pp.PaperEngine(rt)
            await third.start()
            assert third.autopilot_status()["log"] == [] and third._autopilot_last == {}
            await third.stop()
            # a damaged saved state never breaks the start: bad rows are skipped
            await rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("autopilot.state", (
                '{"log": [{"ts": "x"}, {"ts": 1, "symbol": "A:B", "kind": "weird", "text": "t"}, '
                '{"ts": 2, "symbol": "C:D", "kind": "skip", "text": "ok"}], "reads": {"C:D": {"bar_time": 5, '
                '"tf": "7x", "label": "L"}, "e:f": {"bar_time": 6, "tf": "1h", "action": "", "label": "ESPERAR"}}}')))
            fourth = pp.PaperEngine(rt)
            await fourth.start()
            assert fourth.autopilot_status()["log"] == [{"ts": 2, "symbol": "C:D", "kind": "skip", "text": "ok"}]
            assert fourth._autopilot_last == {"E:F": {"bar_time": 6, "tf": "1h", "action": None, "label": "ESPERAR"}}
            await rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("autopilot.state", "{no"))
            fifth = pp.PaperEngine(rt)
            await fifth.start()
            assert fifth.autopilot_status()["log"] == []
            await fourth.stop()
            await fifth.stop()

    asyncio.run(scenario())


def test_positions_from_before_a_timeframe_change_keep_nexus_exits(runtime_factory, no_background):
    """After autopilot.tf changes, a position opened on the old timeframe keeps NEXUS's exits on its own bars (an
    exits-only trader); the new timeframe never adds a second position on that symbol."""

    async def scenario():
        async with runtime_factory() as rt:
            nexus = BarNexus(default=make_current("BUY"))
            engine = await _engine(rt, {SYM: 100.0}, nexus)
            await engine.set_autopilot(enabled=True, source="list", symbols=[SYM])
            await _settle(engine)
            pos = engine.open_positions(3)[0]
            assert pos["tf"] == "1h"
            t1 = engine._auto_bar[(3, SYM, "1h")]
            await engine.set_autopilot(tf="4h")
            await _settle(engine)
            assert [(p.tf, p.entries, sorted(p.symbols)) for p in engine._auto_profiles()] == [
                ("4h", True, [SYM]), ("1h", False, [SYM])]
            assert engine._auto_follows(engine._open[pos["id"]])
            # a 4h close never touches the 1h position, nor opens a second one (BUY again)
            t4 = engine._auto_bar[(3, SYM, "4h")]
            await engine._on_candle(SYM, "4h", _candle(t4 + 4 * H), True)
            await _settle(engine)
            assert [p["id"] for p in engine.open_positions(3)] == [pos["id"]]
            # its own 1h close: NEXUS's confirmed opposite signal closes it, and nothing opens on 1h any more
            nexus.closed[t1 + H] = make_current("SELL", time=t1 + H, prob_up=0.3, score=-0.5, long_verdict="AVOID",
                                                short_verdict="TAKE")
            await engine._on_candle(SYM, "1h", _candle(t1 + H), True)
            await _settle(engine)
            assert engine.open_positions(3) == []
            assert [h["exit_reason"] for h in await engine.history(3)] == ["REVERSE"]
            assert [p.tf for p in engine._auto_profiles()] == ["4h"]  # no exits-only trader left
            await engine.stop()

    asyncio.run(scenario())


def test_set_autopilot_rejects_bad_values_in_spanish(runtime_factory, no_background):
    async def scenario():
        async with runtime_factory() as rt:
            engine = await _engine(rt, {SYM: 100.0}, BarNexus())
            bad = [
                ({"source": "brokers"}, "Fuente no válida"),
                ({"max_positions": 0}, "El máximo de posiciones debe ser un número entero entre 1 y 20"),
                ({"max_positions": 2.5}, "número entero"),
                ({"risk_pct": 9}, "El riesgo por operación (%) debe ser un número entre 0,1 y 5,0"),
                ({"scanner_top": 31}, "entre 1 y 30"),
                ({"symbols": "BINANCE:ETHUSDT"}, "no es válida"),
                ({"symbols": [f"BINANCE:X{i}USDT" for i in range(31)]}, "Como máximo 30"),
                ({"tf": "7x"}, ""),
            ]
            for kwargs, text in bad:
                with pytest.raises(pp.PaperError) as err:
                    await engine.set_autopilot(**kwargs)
                assert text in str(err.value)
            assert engine.autopilot_status()["enabled"] is False and rt.settings.get("autopilot.source") == "scanner"
            await engine.stop()

    asyncio.run(scenario())


def test_autopilot_api(api_client):
    c = api_client
    r = c.get("/api/paper/autopilot")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account"]["id"] == 3 and body["account"]["name"] == "NEXUS automática"
    assert body["autopilot"]["enabled"] is False and body["positions"] == [] and body["history"] == []
    assert body["risk"]["account_id"] == 3
    r = c.post("/api/paper/autopilot", json={"source": "brokers"})
    assert r.status_code == 400 and r.json()["detail"].startswith("Fuente no válida")
    r = c.post("/api/paper/autopilot", json={"risk_pct": 50})
    assert r.status_code == 400 and "riesgo" in r.json()["detail"]
    # a synthetic market: nothing is decided until its next closed bar (no network in the tests)
    r = c.post("/api/paper/autopilot", json={"enabled": True, "source": "list", "symbols": ["sim:nexus"], "tf": "4h",
                                             "max_positions": 3, "risk_pct": 0.5, "scanner_top": 4})
    assert r.status_code == 200, r.text
    st = r.json()["autopilot"]
    assert (st["enabled"], st["source"], st["symbols"], st["tf"]) == (True, "list", ["SIM:NEXUS"], "4h")
    assert (st["max_positions"], st["risk_pct"], st["scanner_top"]) == (3, 0.5, 4)
    assert c.get("/api/settings").json()["settings"]["autopilot.tf"] == "4h"
    status = c.get("/api/health").json()
    assert status["services"]["paper"]["autopilot"]["enabled"] is True
    r = c.post("/api/paper/autopilot/close", json={})
    assert r.status_code == 400 and "all" in r.json()["detail"]
    r = c.post("/api/paper/autopilot/close", json={"all": True})
    assert r.status_code == 200 and r.json()["positions"] == []
    r = c.post("/api/paper/autopilot/close", json={"position_id": 999})  # unknown, or another account's position
    assert r.status_code == 404 and r.json()["detail"] == "No hay una posición abierta con id 999"
    r = c.post("/api/paper/autopilot/reset", json={"initial_balance": 5000})
    assert r.status_code == 200 and r.json()["account"]["balance"] == 5000.0
    assert c.get("/api/paper/account").json()["account"]["balance"] == 10000.0  # the user's account: untouched
    r = c.post("/api/paper/autopilot", json={"enabled": False})
    assert r.status_code == 200 and r.json()["autopilot"]["enabled"] is False
