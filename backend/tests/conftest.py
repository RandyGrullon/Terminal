"""Shared pytest fixtures.

- Offline by default: tests marked `@pytest.mark.network` only run with NEXUS_NET_TESTS=1.
- `runtime_factory`: `async with runtime_factory() as rt:` gives a temp SQLite DB + settings + event bus (no services).
- `api_client`: FastAPI TestClient over a temp DB with NEXUS_OFFLINE / no background loops (full app incl. services).
- Synthetic candle fixtures (deterministic).
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("NEXUS_CONSOLE_EVENTS", "0")

from app import config as nexus_config  # noqa: E402
from app.sim.synthetic import generate_candleset  # noqa: E402

FIXED_END = 1_760_000_400  # deterministic "now" for synthetic data (aligned to the hour)


def pytest_configure(config):
    config.addinivalue_line("markers", "network: needs internet access (run with NEXUS_NET_TESTS=1)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("NEXUS_NET_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="network test (set NEXUS_NET_TESTS=1)")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def candles():
    """1200 bars of 1h synthetic data with regime switching."""
    return generate_candleset(n=1200, seed=11, tf="1h", end_time=FIXED_END)


@pytest.fixture
def trending_candles():
    return generate_candleset(n=800, seed=3, tf="1h", regime="bull", end_time=FIXED_END)


@pytest.fixture
def ranging_candles():
    return generate_candleset(n=800, seed=5, tf="1h", regime="range", end_time=FIXED_END)


@pytest.fixture
def runtime_factory(tmp_path):
    @asynccontextmanager
    async def factory():
        from app.db import Database
        from app.events import EventBus
        from app.runtime import rt
        from app.settings import SettingsStore
        from app.ws import WSManager

        rt.reset()
        rt.db = await Database.open(tmp_path / "test.db")
        await rt.db.init_schema()
        rt.settings = SettingsStore(rt.db)
        await rt.settings.load()
        rt.ws = WSManager(rt)
        rt.bus = EventBus(console=False)
        await rt.bus.start(rt.db)
        try:
            yield rt
        finally:
            await rt.bus.stop()
            await rt.db.close()
            rt.reset()

    return factory


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(nexus_config, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(nexus_config, "OFFLINE", True)
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    from app.main import create_app
    from app.runtime import rt

    rt.reset()
    with TestClient(create_app()) as client:
        yield client
    rt.reset()
