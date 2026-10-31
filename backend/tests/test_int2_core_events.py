"""Integration round 2 (core): the event bus API the services rely on, and its users.

- EventBus.remove_listener (public) and EventBus.running (started, not stopped, loop open);
- AlertService.stop detaches through remove_listener, never through the bus's private list;
- the PyChart launcher announces the OS isolation on stderr when the bus is not running (it used to vanish), and
  starts the worker with `-I -B` (no __pycache__ written next to the code);
- settings_ext.scanner_model: `scanner.replay_days` (default 7) is a real setting the evaluator reads.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap

from app.events import EventBus
from app.runtime import rt
from app.scripting import sandbox


def test_remove_listener_is_public_and_tolerant():
    bus = EventBus(console=False)
    seen: list[str] = []

    def listener(ev: dict) -> None:
        seen.append(ev["message"])

    bus.add_listener(listener)
    bus.emit("info", "test", "uno")
    bus.remove_listener(listener)
    bus.remove_listener(listener)  # not registered any more: no error
    bus.remove_listener(lambda ev: None)
    bus.emit("info", "test", "dos")
    assert seen == ["uno"]


def test_running_is_true_only_between_start_and_stop(runtime_factory):
    bus = EventBus(console=False)
    assert bus.running is False  # never started: emit_threadsafe would drop events
    bus.emit_threadsafe("info", "test", "antes de start")  # documented: nothing happens, no error
    assert bus.recent() == []

    async def scenario():
        async with runtime_factory() as runtime:
            assert runtime.bus.running is True
            other = EventBus(console=False)
            await other.start(runtime.db)
            assert other.running is True
            await other.stop()
            assert other.running is False
            return other

    stopped = asyncio.run(scenario())
    assert stopped.running is False  # and its loop is closed now


class _PublicOnlyBus:
    """A bus with the public listener API only: touching the private list fails the test."""

    def __init__(self) -> None:
        self.added: list = []
        self.removed: list = []

    def add_listener(self, cb) -> None:
        self.added.append(cb)

    def remove_listener(self, cb) -> None:
        self.removed.append(cb)

    def emit(self, *args, **kwargs) -> dict:
        return {}

    @property
    def _listeners(self):
        raise AssertionError("AlertService touched the bus's private listener list")


def test_alert_service_detaches_through_remove_listener(runtime_factory):
    from app.alerts.service import AlertService

    async def scenario():
        async with runtime_factory() as runtime:
            real_bus = runtime.bus
            fake = _PublicOnlyBus()
            runtime.bus = fake
            try:
                svc = AlertService(runtime)
                await svc.start()
                assert fake.added == [svc._on_event]
                await svc.stop()
                assert fake.removed == [svc._on_event]
            finally:
                runtime.bus = real_bus

    asyncio.run(scenario())


def test_isolation_report_falls_back_to_stderr_when_the_bus_does_not_run(monkeypatch, capsys):
    idle = EventBus(console=False)  # e.g. a standby instance's bus: never started
    monkeypatch.setattr(rt, "bus", idle)
    sandbox._announce("Aislamiento del sistema de PyChart (prueba): sin bus activo", {"worker": {}})
    assert capsys.readouterr().err == "Aislamiento del sistema de PyChart (prueba): sin bus activo\n"
    assert idle.recent() == []


def test_isolation_report_reaches_a_running_bus_from_the_launcher_thread(runtime_factory, monkeypatch, capsys):
    async def scenario():
        async with runtime_factory() as runtime:
            monkeypatch.setattr(rt, "bus", runtime.bus)
            await asyncio.to_thread(sandbox._announce, "Aislamiento del sistema de PyChart (prueba): activo", {})
            for _ in range(50):
                if runtime.bus.recent(5, source="scripts"):
                    break
                await asyncio.sleep(0.01)
            return [e["message"] for e in runtime.bus.recent(5, source="scripts")]

    assert asyncio.run(scenario()) == ["Aislamiento del sistema de PyChart (prueba): activo"]
    assert capsys.readouterr().err == ""


def test_worker_starts_isolated_and_writes_no_bytecode(tmp_path, monkeypatch):
    """The real launcher (_launch) with a stand-in worker that imports a module from a fresh folder: under -B no
    __pycache__ appears there (under -I alone it would, since -I ignores PYTHONDONTWRITEBYTECODE)."""
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "probe_mod.py").write_text("VALUE = 42\n", encoding="utf-8")
    worker = tmp_path / "fake_worker.py"
    worker.write_text(textwrap.dedent(f"""
        import json, sys
        sys.stdin.buffer.read()
        sys.path.insert(0, {str(mods)!r})
        import probe_mod
        print(json.dumps({{"isolated": sys.flags.isolated, "no_bytecode": sys.flags.dont_write_bytecode,
                          "value": probe_mod.VALUE}}))
    """), encoding="utf-8")
    monkeypatch.setattr(sandbox, "WORKER_PATH", worker)
    proc = sandbox._launch(b"{}", 30.0)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    out = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    assert out == {"isolated": 1, "no_bytecode": 1, "value": 42}
    assert proc.args[:3] == [sys.executable, "-I", "-B"]
    assert not (mods / "__pycache__").exists()


def test_scanner_replay_days_is_a_setting_with_the_evaluator_default(runtime_factory):
    from app.config import DEFAULT_SETTINGS
    from app.settings_ext import EXTRA_DEFAULTS
    from app.settings_ext.scanner_model import DEFAULTS
    from app.sim.evaluator import REPLAY_DAYS_DEFAULT, PredictionEvaluator

    assert DEFAULTS == {"scanner.replay_days": 7}
    assert EXTRA_DEFAULTS["scanner.replay_days"] == DEFAULT_SETTINGS["scanner.replay_days"] == REPLAY_DAYS_DEFAULT

    async def scenario():
        async with runtime_factory() as runtime:
            evaluator = PredictionEvaluator(runtime)
            assert evaluator._replay_days() == 7.0
            assert await runtime.settings.set("scanner.replay_days", "3") == 3  # a known key now, coerced to int
            assert evaluator._replay_days() == 3.0
            await runtime.settings.set("scanner.replay_days", 45)
            assert evaluator._replay_days() == 30.0  # the evaluator clamps to 0..30
            await runtime.settings.set("scanner.replay_days", 0)
            assert evaluator._replay_days() == 0.0  # 0 = vectors dropped at resolution

    asyncio.run(scenario())
