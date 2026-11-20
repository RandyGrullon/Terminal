"""The server's own status (app.system_monitor): where it runs, its starts with the downtime before each one (a crash
leaves no clean stop), the internet probe and its events, the battery parsers and the API."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config as nexus_config
from app import system_monitor as SM


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_spans_and_battery_parsers():
    assert [SM.fmt_span(s) for s in (5, 125, 7500, 90000)] == ["5 s", "2 min", "2 h 05 min", "1 d 1 h"]
    b = SM.parse_termux_battery(json.dumps({"percentage": 87, "status": "CHARGING", "plugged": "PLUGGED_USB",
                                            "temperature": 31.2, "health": "GOOD"}))
    assert b == {"percent": 87.0, "charging": True, "plugged": True, "temperature_c": 31.2, "health": "GOOD",
                 "source": "termux-api"}
    assert SM.parse_termux_battery("no json") is None and SM.parse_termux_battery("{}") is None
    host = SM.host_info()
    assert host["kind"] in ("android", "windows", "linux", "mac") and host["label"] and host["python"]


def test_a_crash_is_told_from_a_clean_stop(runtime_factory, no_background, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            mon = SM.SystemMonitor(rt)
            await mon.start()  # first start ever: nothing before it
            assert mon._starts[-1]["down_ms"] is None
            await mon.stop()  # clean stop
            again = SM.SystemMonitor(rt)
            await again.start()
            assert again._starts[-1]["clean"] is True and len(again._starts) == 2
            # a crash: the last heartbeat was 10 minutes ago and not clean
            past = SM.now_ms() - 600_000
            await rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                (SM.HEARTBEAT_KEY, json.dumps({"ts": past, "clean": False})))
            third = SM.SystemMonitor(rt)
            await third.start()
            last = third._starts[-1]
            assert last["clean"] is False and last["down_ms"] >= 600_000
            assert any("caído 10 min sin apagarse bien" in e["message"] for e in rt.bus.recent(50, source="system"))
            view = await third.view()
            assert view["starts"][0]["at"] == last["at"] and view["net"]["state"] == "off"
            assert view["host"]["label"] and view["uptime_s"] >= 0
            assert (view["battery"] is None) == (view["battery_note"] is not None)

    asyncio.run(scenario())


def test_the_internet_probe_and_its_events(runtime_factory, no_background, monkeypatch):
    class FakeResponse:
        status_code = 200

    class FakeClient:
        fail = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert url == SM.PROBE_URL
            if FakeClient.fail:
                raise OSError("down")
            return FakeResponse()

    monkeypatch.setattr(SM, "http_client", lambda timeout=10.0: FakeClient())

    async def scenario():
        async with runtime_factory() as rt:
            monkeypatch.setattr(nexus_config, "OFFLINE", False)
            mon = SM.SystemMonitor(rt)
            assert (await mon.probe())["ok"] is True
            FakeClient.fail = True
            bad = await mon.probe()
            assert bad["ok"] is False and "sin respuesta" in bad["error"]
            FakeClient.fail = False
            await mon.probe()
            msgs = [e["message"] for e in rt.bus.recent(50, source="system")]
            assert any(m.startswith("Internet: no se pudo llegar") for m in msgs)
            assert any(m.startswith("Internet: volvió la conexión") for m in msgs)
            view = await mon.view()
            assert view["net"]["state"] == "ok" and view["net"]["checks_1h"] == 3 and view["net"]["fails_1h"] == 1

    asyncio.run(scenario())


def test_api(api_client):
    st = api_client.get("/api/system/status").json()
    assert {"host", "started_at", "uptime_s", "starts", "net", "battery", "battery_note", "memory_mb",
            "db_size_bytes", "server_time"} <= set(st)
    assert api_client.get("/api/health").json()["services"]["sysmon"]["running"] is True
