"""Long history for strategy research: the CSV importer (QuantDataManager / MetaTrader / Dukascopy-like formats, a
time-zone offset, resampling 1m → 5m / 1h with partial buckets dropped, never overwriting a stored bar, files only from
data/import), Binance's official dumps (spot and USDⓈ-M paths, µs timestamps, a missing month skipped; no network:
httpx MockTransport), maintenance keeping imported candles, and the research itself (PyChart strategies and NEXUS
judged in development and check periods against random entries; a «candidata» only above random in both), with its
API and terminal commands."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile

import httpx
import pytest

from app import config as nexus_config
from app.data import importer as imp
from app.maintenance import Maintenance
from app.scripting.service import ScriptService
from app.sim import research as rs
from app.sim.synthetic import generate_candleset

CROSS = '''strategy("Cruce de prueba", initial_capital=10000, risk_pct=1, fee_pct=0.1)
rapida = ta.ema(close, 5)
lenta = ta.ema(close, 20)
strategy.entry("L", "long", when=ta.crossover(rapida, lenta))
strategy.close("L", when=ta.crossunder(rapida, lenta))
'''


def _minutes(start: int, n: int, *, price: float = 100.0) -> list[str]:
    """MT4-like rows "YYYY.MM.DD,HH:MM,O,H,L,C,V" every minute from `start` (unix s)."""
    import datetime as dt

    out = []
    for i in range(n):
        t = dt.datetime.fromtimestamp(start + 60 * i, dt.timezone.utc)
        p = price + i * 0.01
        out.append(f"{t:%Y.%m.%d},{t:%H:%M},{p:.2f},{p + 0.5:.2f},{p - 0.5:.2f},{p + 0.1:.2f},10")
    return out


def test_parse_the_usual_formats_and_a_time_zone():
    rows = list(imp.parse_csv(["<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<TICKVOL>",
                               "2024.01.02,00:00,1.10,1.20,1.00,1.15,10"]))
    assert rows == [(1704153600, 1.10, 1.20, 1.00, 1.15, 10.0)]
    # Dukascopy-style: one date-time column, semicolons, day first; the file in UTC+2
    rows = list(imp.parse_csv(["Gmt time;Open;High;Low;Close;Volume", "02.01.2024 02:00:00.000;1;2;0,5;1,5;3"],
                              tz_offset_h=2))
    assert rows == [(1704153600, 1.0, 2.0, 0.5, 1.5, 3.0)]
    # header-less MetaTrader and unix milliseconds
    assert list(imp.parse_csv(["2024.01.02,00:01,1,2,0.5,1.5,7"]))[0][0] == 1704153660
    assert list(imp.parse_csv(["1704153600000,1,2,0.5,1.5,7"]))[0][0] == 1704153600
    # a high below the open is widened (some exports round it), bad lines are counted
    stats: dict = {}
    rows = list(imp.parse_csv(["date,time,open,high,low,close", "20240102,0000,2,1.9,1,1.5", "x,y,z,1,2,3"], stats=stats))
    assert rows[0][2] == 2.0 and stats == {"bad": 1, "read": 1}
    with pytest.raises(imp.ImportError_, match="No reconozco las columnas"):
        list(imp.parse_csv(["fecha;precio", "2024-01-02;1"]))


def test_resampling_drops_partial_buckets():
    start = 1704153600 + 2 * 60  # 00:02: the first 5m bucket (00:00) is only covered in part
    rows = [(start + 60 * i, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 1.0) for i in range(13)]  # 00:02 .. 00:14
    r = imp.Resampler("5m", 60)
    out = [b for b in (r.add(x) for x in rows) if b] + [b for b in [r.flush()] if b]
    assert [b[0] - 1704153600 for b in out] == [300, 600]  # 00:05 and 00:10, both complete
    first = out[0]
    assert (first[1], first[2], first[3], first[4], first[5]) == (4.0, 9.0, 3.5, 8.5, 5.0)
    assert imp.source_seconds([0, 60, 180, 240]) == 60
    assert imp.months_back(2, now=1794670000) == [(2026, 9), (2026, 10)]


def test_import_a_csv_keeps_stored_bars_and_only_reads_data_import(runtime_factory, tmp_path, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            monkeypatch.setattr(imp, "IMPORT_DIR", tmp_path)
            start = 1704153600  # 2024-01-02 00:00 UTC
            (tmp_path / "eurusd.csv").write_text("\n".join(["Date,Time,Open,High,Low,Close,Volume", *_minutes(start, 180)]))
            # a recorded 1h bar at 01:00 is kept as it is
            await rt.db.execute("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                                "VALUES ('FX:EURUSD', '1h', ?, 9, 9, 9, 9, 1, 'recorder')", (start + 3600,))
            res = await imp.import_csv(rt.db, imp.resolve_import_path("eurusd.csv"), "FX:EURUSD", tfs=["5m", "1h", "4h"])
            assert res["bars"] == {"5m": 36, "1h": 3, "4h": 0}  # 3 hours: no complete 4h bucket
            assert res["stored"]["5m"]["bars"] == 36 and res["stored"]["1h"]["bars"] == 2  # the recorded one is not import
            kept = await rt.db.fetchone("SELECT open, source FROM candles WHERE symbol='FX:EURUSD' AND tf='1h' AND time=?",
                                        (start + 3600,))
            assert (kept["open"], kept["source"]) == (9.0, "recorder")
            first = await rt.db.fetchone("SELECT open, high, low, close, volume FROM candles WHERE symbol='FX:EURUSD' "
                                         "AND tf='1h' AND time=?", (start,))
            assert first["open"] == 100.0 and first["close"] == pytest.approx(100.69) and first["volume"] == 600.0
            for bad in ("..\\nexus.db", str(tmp_path.parent / "x.csv"), "no-existe.csv", ""):
                with pytest.raises(imp.ImportError_):
                    imp.resolve_import_path(bad)
            with pytest.raises(imp.ImportError_, match="mercado"):
                await imp.import_csv(rt.db, tmp_path / "eurusd.csv", "EURUSD")

    asyncio.run(scenario())


def test_maintenance_keeps_imported_candles(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            old = 1_600_000_000
            for src in ("import", "binance", None):
                await rt.db.execute("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                                    "VALUES (?, '5m', ?, 1, 1, 1, 1, 1, ?)", (f"X:{src}", old, src))
            await Maintenance(rt).run_once(now=1_794_000_000_000)
            left = [r["symbol"] for r in await rt.db.fetchall("SELECT symbol FROM candles WHERE tf='5m' AND time=?", (old,))]
            assert left == ["X:import"]

    asyncio.run(scenario())


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("k.csv", text)
    return buf.getvalue()


def test_binance_dumps_spot_and_futures_paths(runtime_factory, monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.url.host == "data.binance.vision"
        if "2026-09" in request.url.path:
            return httpx.Response(404)
        if "/spot/" in request.url.path:  # recent spot files: µs timestamps and no header
            return httpx.Response(200, content=_zip("1790000000000000,1,2,0.5,1.5,3,0,0,0,0,0,0\n"))
        return httpx.Response(200, content=_zip("open_time,open,high,low,close,volume\n1790000000000,1,2,0.5,1.5,3\n"))

    def factory(timeout: float = 15.0, **kw):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    async def scenario():
        async with runtime_factory() as rt:
            monkeypatch.setattr(nexus_config, "OFFLINE", False)
            monkeypatch.setattr(imp, "http_client", factory)
            monkeypatch.setattr(imp, "now_s", lambda: 1794670000)  # November 2026: Sept and Oct are the last months
            spot = await imp.import_binance(rt.db, "BINANCE:BTCUSDT", months=2, tfs=["1h"])
            perp = await imp.import_binance(rt.db, "BINANCE:BTCUSDT.P", months=2, tfs=["1h"])
            assert spot["bars"] == {"1h": 1} and spot["missing_months"] == 1
            assert perp["symbol"] == "BINANCE:BTCUSDT.P" and perp["stored"]["1h"]["from"] == 1790000000
            assert any("/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-10.zip" in u for u in seen)
            assert any("/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-10.zip" in u for u in seen)
            with pytest.raises(imp.ImportError_, match="BINANCE"):
                imp.binance_target("OKX:BTCUSDT")
            monkeypatch.setattr(nexus_config, "OFFLINE", True)
            with pytest.raises(imp.ImportError_, match="sin conexión"):
                await imp.import_binance(rt.db, "BINANCE:BTCUSDT", months=1)

    asyncio.run(scenario())


async def _store_synthetic(rt, symbol: str, n: int) -> None:
    cs = generate_candleset(n=n, seed=11, end_time=1_790_000_000, symbol=symbol)
    rows = [(symbol, "1h", int(t), float(o), float(h), float(lo), float(c), float(v), "import")
            for t, o, h, lo, c, v in zip(cs.time, cs.open, cs.high, cs.low, cs.close, cs.volume)]
    await rt.db.executemany("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def test_research_judges_each_strategy_in_two_periods(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            rt.scripts = ScriptService(rt)
            await rt.scripts.start()
            mine = await rt.scripts.create("Cruce de prueba", CROSS)  # only this one runs: the test stays fast
            await _store_synthetic(rt, "SIM:LARGO", 1500)
            svc = rs.ResearchService(rt)
            rt.research = svc
            with pytest.raises(rs.ResearchError, match="mercado"):
                svc.start_research("BTCUSDT", "1h")
            svc.start_research("SIM:LARGO", "1h", nexus=False, script_ids=[int(mine["id"])])
            with pytest.raises(rs.ResearchError, match="Ya hay un trabajo"):
                svc.start_research("SIM:LARGO", "1h")
            snap = await svc.wait()
            job = snap["job"]
            assert job["state"] == "done", job
            rep = job["result"]
            assert (rep["simbolo"], rep["temporalidad"], rep["velas"]) == ("SIM:LARGO", "1h", 1500)
            [cross] = rep["estrategias"]
            assert cross["nombre"] == "Cruce de prueba" and cross["ok"] and cross["fuente"] == "pychart"
            dev, chk = cross["periodos"]
            assert dev["periodo"].startswith("desarrollo") and chk["periodo"].startswith("comprobación")
            assert cross["candidata"] == (dev["veredicto_clave"] == "above" and chk["veredicto_clave"] == "above")
            assert rep["conclusion"] and "no es una garantía" in rep["nota"]
            assert (await svc.reports())[0]["simbolo"] == "SIM:LARGO"  # kept across restarts (meta)
            # too little history: the job ends with the reason
            svc.start_research("SIM:CORTO", "1h", nexus=False)
            job = (await svc.wait())["job"]
            assert job["state"] == "error" and "hacen falta al menos 1000" in job["error"]
            await rt.scripts.stop()

    asyncio.run(scenario())


def test_research_api_and_terminal(api_client):
    st = api_client.get("/api/research").json()
    assert st["busy"] is False and isinstance(st["reports"], list)
    r = api_client.post("/api/research/run", json={"symbol": "BTCUSDT", "tf": "1h"})
    assert r.status_code == 400 and "mercado" in r.json()["detail"]
    r = api_client.post("/api/research/import/file", json={"file": "../nexus.db", "symbol": "FX:EURUSD"})
    assert r.status_code == 400 and "carpeta" in r.json()["detail"]
    res = api_client.post("/api/terminal", json={"text": "importar"}).json()
    assert res["ok"] and "Carpeta de importación" in json.dumps(res, ensure_ascii=False)
    res = api_client.post("/api/terminal", json={"text": "investigar estado"}).json()
    assert res["ok"]
    assert api_client.post("/api/terminal", json={"text": "investigar"}).json()["ok"] is False
    assert api_client.get("/api/health").json()["services"]["research"]["running"] is False
