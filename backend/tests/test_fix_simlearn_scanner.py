"""Fix round (simlearn): scanner deep mode and storage.

- stale-delisted-pairs-served-live: outdated series get no deep verdict, SL/TP or gate_log row.
- gate-take-without-signal (I8): a gate TOMAR counts as "oportunidad TOMAR" only with the NEXUS signal in that
  direction; otherwise the row reads "Filtro favorable · sin señal NEXUS".
- replay-gatelog-horizon: the scanner's gate_log target counts from the decision bar.
- db-growth-scanner-retention: compact predictions.features and scan_results.data.
"""
from __future__ import annotations

import asyncio
import json

from app import config as nexus_config
from app.sim import scanner as sc
from app.util import now_s
from tests.test_sim_fakes import FakeHub, FakeNexus, make_current, scan_row

H = 3600


def _resp(action: str, *, long_verdict: str = "AVOID", short_verdict: str = "AVOID", time: int | None = None,
          **extra) -> dict:
    bar = time if time is not None else now_s() // H * H
    cur = make_current(action, long_verdict=long_verdict, short_verdict=short_verdict, time=bar,
                       score=0.4 if action in ("BUY", "HOLD_LONG") else -0.4 if action == "SELL" else 0.05)
    return {"ok": True, "current": cur, "prediction": {"expected_return_pct": 1.0}, **extra}


def _rows() -> list[dict]:
    return [scan_row("BINANCE:BTCUSDT", value=3e9), scan_row("BINANCE:ETHUSDT", value=2e9),
            scan_row("BINANCE:SOLUSDT", value=1e9), scan_row("BINANCE:XRPUSDT", value=9e8),
            scan_row("BINANCE:ADAUSDT", value=8e8)]


async def _deep_scan(rt, monkeypatch, analyze_map: dict[str, dict]) -> tuple[dict, list[dict]]:
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", False)
    rt.hub = FakeHub(scan_rows=_rows())
    rt.nexus = FakeNexus(analyze_map=analyze_map)
    svc = sc.ScannerService(rt)
    await svc.start()
    run_id = await svc.run_scan("crypto", "1h", 50, deep=True, deep_top=10, wait=True)
    run, results = await svc.results(run_id)
    await svc.stop()
    return run, results


def test_outdated_series_get_no_deep_verdict_nor_gate_log(runtime_factory, monkeypatch):
    weeks_ago = now_s() // H * H - 22 * 86400

    async def scenario():
        async with runtime_factory() as rt:
            run, results = await _deep_scan(rt, monkeypatch, {
                # newest bar 22 days old (venue stopped trading) although NEXUS answered ok
                "BINANCE:BTCUSDT": _resp("BUY", long_verdict="TAKE", time=weeks_ago),
                # the hub flagged the set as outdated
                "BINANCE:ETHUSDT": _resp("BUY", long_verdict="TAKE", stale=True,
                                         note="Datos desactualizados: última vela hace 3 días"),
                # NEXUS itself refused it (contract I2)
                "BINANCE:SOLUSDT": {"ok": False, "current": None,
                                    "message": "Datos desactualizados: última vela hace 5 días"},
                # live series: analysed normally
                "BINANCE:XRPUSDT": _resp("BUY", long_verdict="TAKE"),
            })
            by = {r["symbol"]: r for r in results}
            for sym in ("BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:SOLUSDT"):
                row = by[sym]
                assert row["deep"] is False and row["verdict"] is None and row["sl"] is None
                assert "Datos desactualizados" in row["reasons"][0]
            assert "hace 22 días" in by["BINANCE:BTCUSDT"]["reasons"][0]
            assert "hace 3 días" in by["BINANCE:ETHUSDT"]["reasons"][0]
            assert by["BINANCE:XRPUSDT"]["deep"] is True and by["BINANCE:XRPUSDT"]["opportunity"] is True
            logged = [r["symbol"] for r in await rt.db.fetchall("SELECT symbol FROM gate_log")]
            assert logged == ["BINANCE:XRPUSDT"]

    asyncio.run(scenario())


def test_stale_reason_rules():
    bar = now_s() // H * H
    assert sc.stale_reason(_resp("BUY", time=bar), "1h", "crypto", now_s()) is None
    assert sc.stale_reason(_resp("BUY", time=bar - 3 * H), "1h", "crypto", bar + 30) is None  # ≤ 3 bars: tolerated
    assert "hace 5 horas" in sc.stale_reason(_resp("BUY", time=bar - 5 * H), "1h", "crypto", bar + 30)
    # stocks / forex close at night and on weekends: only the hub/engine flag counts there
    assert sc.stale_reason(_resp("BUY", time=bar - 60 * H), "1h", "america", bar) is None
    assert sc.stale_reason({"ok": False, "message": "x"}, "1h", "crypto", bar) is None  # apply_deep reports it


def test_gate_take_without_nexus_signal_is_not_an_opportunity(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            run, results = await _deep_scan(rt, monkeypatch, {
                "BINANCE:BTCUSDT": _resp("WAIT", long_verdict="TAKE"),  # gate TOMAR, NEXUS waits
                "BINANCE:ETHUSDT": _resp("SELL", long_verdict="TAKE"),  # gate TOMAR long, NEXUS sells
                "BINANCE:SOLUSDT": _resp("HOLD_LONG", long_verdict="TAKE"),  # already long: no new entry signal
            })
            by = {r["symbol"]: r for r in results}
            for sym, action in (("BINANCE:BTCUSDT", "WAIT"), ("BINANCE:ETHUSDT", "SELL"),
                                ("BINANCE:SOLUSDT", "HOLD_LONG")):
                row = by[sym]
                assert row["deep"] is True and row["verdict"] == "TAKE" and row["direction"] == "long"
                assert row["opportunity"] is False and row["nexus_action"] == action
                assert row["reasons"][0].startswith("Filtro favorable · sin señal NEXUS (TOMAR 78/100")
            assert "ninguna oportunidad TOMAR con señal NEXUS (3 con filtro favorable sin señal NEXUS)" in run["message"]
            # ranking: no TOMAR tier for them (quick score × confidence decides)
            assert [r["rank"] for r in results] == list(range(1, len(results) + 1))
            scores = [abs(r["score"]) * r["confidence"] for r in results]
            assert scores == sorted(scores, reverse=True)

            # one aligned TOMAR among them: the only "oportunidad TOMAR", ranked first
            run2, results2 = await _deep_scan(rt, monkeypatch, {
                "BINANCE:BTCUSDT": _resp("WAIT", long_verdict="TAKE"),
                "BINANCE:ADAUSDT": _resp("BUY", long_verdict="TAKE"),
            })
            assert results2[0]["symbol"] == "BINANCE:ADAUSDT" and results2[0]["opportunity"] is True
            assert run2["message"].endswith("1 oportunidad TOMAR · mejor: BINANCE:ADAUSDT (TOMAR 78/100) · "
                                            "1 con filtro favorable sin señal NEXUS")

    asyncio.run(scenario())


def test_scanner_gate_log_target_counts_from_the_decision_bar(runtime_factory, monkeypatch):
    bar = now_s() // H * H

    async def scenario():
        async with runtime_factory() as rt:
            await _deep_scan(rt, monkeypatch, {"BINANCE:BTCUSDT": _resp("BUY", long_verdict="TAKE", time=bar)})
            row = await rt.db.fetchone("SELECT time, target_at FROM gate_log")
            horizon = int(rt.settings.get("nexus.time_stop_bars", 48))
            assert row["time"] == bar and row["target_at"] == (bar + (horizon + 1) * H) * 1000

    asyncio.run(scenario())


def test_quick_signal_flags_an_outdated_series(runtime_factory, monkeypatch):
    """A local read of a series the hub flags as outdated is marked stale and says so first (contract I2): it is
    never shown as the current market."""
    from app.sim.synthetic import generate_candleset

    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)  # local candles only

    async def scenario():
        async with runtime_factory() as rt:
            live = generate_candleset(n=300, seed=4, symbol="X:LIVE")
            old = generate_candleset(n=300, seed=4, symbol="X:OLD").with_meta(
                stale=True, note="Datos desactualizados: última vela hace 22 días")
            rt.hub = FakeHub(candles={("X:LIVE", "1h"): live, ("X:OLD", "1h"): old})
            svc = sc.ScannerService(rt)
            fresh = await svc.quick_signal("X:LIVE", "1h")
            assert fresh["reasons"][0].startswith("Lectura local") and "stale" not in fresh
            outdated = await svc.quick_signal("X:OLD", "1h")
            assert outdated["stale"] is True and outdated["price"] == float(old.close[-1])
            assert outdated["reasons"][0] == ("Datos desactualizados: última vela hace 22 días: "
                                              "esta lectura usa esas velas antiguas")
            assert outdated["reasons"][1].startswith("Lectura local")

    asyncio.run(scenario())


def test_compact_features_roundtrip():
    feats = {"rec_60": 0.123456, "macd_60": -1.0, "volatility": 2.5}
    raw = sc.encode_features(feats)
    data = json.loads(raw)
    assert data["v"] == 2 and data["f"] == sc.FEATURE_SIG and len(data["x"]) == len(sc.FEATURE_NAMES)
    assert sc.decode_features(raw) == {"rec_60": 0.1235, "macd_60": -1.0, "volatility": 2.5}
    assert len(raw) < 330  # the v1 dict with names was ~670 bytes
    assert sc.decode_features(json.dumps({**data, "f": "otra-lista"})) is None  # other feature list: unusable
    assert sc.decode_features(json.dumps({"v": 1, "x": {"rec_60": 0.5}})) == {"rec_60": 0.5}  # legacy rows
    assert sc.decode_features(None) is None and sc.decode_features("no json") is None


def test_scan_results_store_only_what_has_no_column(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            run, results = await _deep_scan(rt, monkeypatch, {"BINANCE:BTCUSDT": _resp("BUY", long_verdict="TAKE")})
            stored = await rt.db.fetchall("SELECT symbol, data FROM scan_results")
            for rec in stored:
                data = json.loads(rec["data"])
                assert not set(data) & set(sc.RESULT_COLUMNS)
                assert None not in data.values()
            btc = next(r for r in results if r["symbol"] == "BINANCE:BTCUSDT")
            assert btc["deep"] and btc["verdict"] == "TAKE" and btc["opportunity"] is True and btc["sl"] == 98.0
            assert btc["description"] == "BTCUSDT" and btc["reasons"]
            # rows stored before this change (the whole ScanRow in `data`) still read the same
            legacy = dict(btc, rank=7)
            legacy.pop("nexus_action")
            legacy.pop("opportunity")
            await rt.db.execute(
                "INSERT INTO scan_results(run_id, symbol, rank, score, prob_up, action, confidence, price, change_pct, "
                "volume, verdict, gate_score, data) VALUES (999, 'X:Y', 7, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (btc["score"], btc["prob_up"], btc["action"], btc["confidence"], btc["price"], btc["change_pct"],
                 btc["volume"], btc["verdict"], btc["gate_score"], json.dumps(legacy)),
            )
            rec = await rt.db.fetchone(
                f"SELECT {', '.join(sc.RESULT_COLUMNS)}, data FROM scan_results WHERE run_id=999")
            old = sc.result_row(rec)
            assert old["opportunity"] is None and old["nexus_action"] is None and not sc.is_opportunity(old)
            assert {k: v for k, v in old.items() if k not in ("symbol", "rank", "opportunity", "nexus_action")} == \
                {k: v for k, v in btc.items() if k not in ("symbol", "rank", "opportunity", "nexus_action")}

    asyncio.run(scenario())
