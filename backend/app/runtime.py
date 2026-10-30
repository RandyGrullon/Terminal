"""Service locator. Filled during the FastAPI lifespan (see app.main). Attributes may be None if a module
failed to load, so cross-module calls must be guarded: `if rt.hub is not None: ...`."""
from __future__ import annotations

import time
from typing import Any


class Runtime:
    def __init__(self) -> None:
        self.db: Any = None  # app.db.Database
        self.settings: Any = None  # app.settings.SettingsStore
        self.bus: Any = None  # app.events.EventBus
        self.ws: Any = None  # app.ws.WSManager
        self.hub: Any = None  # app.data.hub.MarketDataHub
        self.nexus: Any = None  # app.engine.service.NexusService
        self.scanner: Any = None  # app.sim.scanner.ScannerService
        self.paper: Any = None  # app.sim.paper.PaperEngine
        self.evaluator: Any = None  # app.sim.evaluator.PredictionEvaluator
        self.sim: Any = None  # app.sim.simulator.Simulator
        self.scripts: Any = None  # app.scripting.service.ScriptService
        self.maintenance: Any = None  # app.maintenance.Maintenance
        self.auth: Any = None  # app.auth.SupabaseVerifier (cloud mode only)
        self.started_at: float = time.time()
        self.load_errors: list[str] = []
        self.services: list[tuple[str, Any]] = []

    def reset(self) -> None:
        self.__init__()


rt = Runtime()
