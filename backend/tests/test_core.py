"""Tests for the LEAD foundation: util, CandleSet, synthetic market, settings, events, terminal, app wiring."""
from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from app.data.base import CandleSet, merge_candles, resample
from app.sim.synthetic import SyntheticMarket, generate_candleset
from app.terminal import parse
from app.util import TTLCache, clean_json, floor_time, normalize_tf, tf_seconds, to_list
from tests.conftest import FIXED_END


def test_normalize_tf_aliases():
    assert normalize_tf("1H") == "1h"
    assert normalize_tf("60") == "1h"
    assert normalize_tf("240") == "4h"
    assert normalize_tf("D") == "1d"
    assert normalize_tf("W") == "1w"
    assert normalize_tf("m15") == "15m"
    with pytest.raises(ValueError):
        normalize_tf("1M")
    with pytest.raises(ValueError):
        normalize_tf("7x")


def test_floor_time_week_starts_monday():
    t = floor_time(1_760_000_000, "1w")
    assert (t - 345600) % 604800 == 0
    assert floor_time(3661, "1h") == 3600


def test_clean_json_handles_numpy_and_nan():
    out = clean_json({"a": np.float64("nan"), "b": np.arange(3), "c": (1, float("inf")), "d": np.bool_(True)})
    assert out == {"a": None, "b": [0, 1, 2], "c": [1, None], "d": True}
    assert to_list(np.array([1.0, np.nan])) == [1.0, None]


def test_ttl_cache():
    c = TTLCache(10, maxsize=2)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    assert c.get("a") is None and c.get("c") == 3


def test_candleset_from_rows_sorts_dedupes_and_appends():
    rows = [
        {"time": 7200, "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 1},
        {"time": 3600, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1},
        {"time": 7200, "open": 2, "high": 4, "low": 1, "close": 3, "volume": 2},
    ]
    cs = CandleSet.from_rows(rows, "X:Y", "1h")
    assert cs.time.tolist() == [3600, 7200]
    assert cs.close.tolist() == [1.5, 3.0]  # last duplicate wins
    cs2 = cs.append({"time": 7200, "open": 2, "high": 5, "low": 1, "close": 4, "volume": 3})
    assert len(cs2) == 2 and cs2.last_close == 4.0
    cs3 = cs2.append({"time": 10800, "open": 4, "high": 5, "low": 3, "close": 4.5})
    assert len(cs3) == 3 and cs3.volume[-1] == 0.0
    assert cs3.upto(0).last_time == 3600
    assert cs3.tail(2).time.tolist() == [7200, 10800]


def test_resample_to_4h(candles):
    four = resample(candles, "4h")
    assert len(four) in (len(candles) // 4, len(candles) // 4 + 1)
    i = 5
    start = four.time[i]
    mask = (candles.time >= start) & (candles.time < start + tf_seconds("4h"))
    assert math.isclose(four.high[i], candles.high[mask].max())
    assert math.isclose(four.low[i], candles.low[mask].min())
    assert math.isclose(four.close[i], candles.close[mask][-1])


def test_merge_candles():
    a = [{"time": 1, "close": 1}, {"time": 2, "close": 2}]
    b = [{"time": 2, "close": 9}, {"time": 3, "close": 3}]
    assert [c["close"] for c in merge_candles(a, b)] == [1, 9, 3]


def test_synthetic_is_deterministic_and_valid():
    a = generate_candleset(n=500, seed=42, end_time=FIXED_END)
    b = generate_candleset(n=500, seed=42, end_time=FIXED_END)
    assert np.array_equal(a.close, b.close)
    assert len(a) == 500
    assert np.all(a.high >= np.maximum(a.open, a.close))
    assert np.all(a.low <= np.minimum(a.open, a.close))
    assert np.all(np.diff(a.time) == 3600)
    assert a.last_time < FIXED_END


def test_synthetic_regimes_have_direction():
    bull = generate_candleset(n=600, seed=1, regime="bull", end_time=FIXED_END)
    bear = generate_candleset(n=600, seed=1, regime="bear", end_time=FIXED_END)
    assert bull.close[-1] > bull.close[0]
    assert bear.close[-1] < bear.close[0]
    m = SyntheticMarket(seed=3, regime="range")
    m.set_regime("volatile")
    assert m.regime == "volatile"


def test_terminal_parser_flags():
    name, args, flags = parse('scan crypto --deep 1h --limit 50 --regime=bull "two words"')
    assert name == "scan"
    assert args == ["crypto", "1h", "two words"]
    assert flags == {"deep": True, "limit": "50", "regime": "bull"}


def test_settings_and_events(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            assert rt.settings.get("nexus.horizon") == 5
            await rt.settings.set("nexus.horizon", "7")
            assert rt.settings.get("nexus.horizon") == 7
            await rt.settings.set("nexus.allow_short", "no")
            assert rt.settings.get("nexus.allow_short") is False
            await rt.settings.set("scanner.markets", "crypto, america")
            assert rt.settings.get("scanner.markets") == ["crypto", "america"]
            await rt.settings.set("ui.anything", {"x": 1})
            with pytest.raises(KeyError):
                await rt.settings.set("nope.key", 1)
            with pytest.raises(ValueError):
                await rt.settings.set("nexus.horizon", "abc")
            ev = rt.bus.emit("signal", "test", "hola", {"v": float("nan")})
            assert ev["data"] == {"v": None}
            await rt.bus.flush()
            assert await rt.db.fetchval("SELECT COUNT(*) FROM events") == 1
            async with rt.db.transaction() as tx:
                await tx.execute("INSERT INTO watchlist(symbol, position, added_at) VALUES ('A:B', 0, 0)")
            assert await rt.db.fetchval("SELECT COUNT(*) FROM watchlist") == 1

    asyncio.run(scenario())


def test_app_boots_offline_and_core_endpoints(api_client):
    r = api_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "services" in body
    r = api_client.post("/api/terminal", json={"text": "help"})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = api_client.post("/api/terminal", json={"text": "nonexistent_cmd"})
    assert r.json()["ok"] is False
    r = api_client.put("/api/settings", json={"risk.per_trade_pct": 2})
    assert r.status_code == 200 and r.json()["settings"]["risk.per_trade_pct"] == 2.0
    r = api_client.put("/api/settings", json={"bad.key": 1})
    assert r.status_code == 400
    assert api_client.get("/api/db/stats").json()["tables"]["settings"] >= 1
    assert api_client.get("/api/unknown-endpoint-xyz").status_code in (200, 404)  # SPA fallback or 404
    assert api_client.get("/api/nope").status_code == 404


def test_untrusted_host_is_rejected(api_client):
    r = api_client.get("/api/health", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_websocket_hello_and_command(api_client):
    with api_client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        ws.send_json({"type": "command", "id": "c1", "text": "echo hola"})
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "command_result":
                assert msg["id"] == "c1" and msg["lines"][0]["text"] == "hola"
                break
        else:  # pragma: no cover
            pytest.fail("no command_result")


def test_websocket_rejects_foreign_origin(api_client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with api_client.websocket_connect("/ws", headers={"origin": "https://evil.example.com"}) as ws:
            ws.receive_json()
