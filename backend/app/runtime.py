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
        self.macro: Any = None  # app.macro.service.MacroService (market conditions / semáforo)
        self.nexus: Any = None  # app.engine.service.NexusService
        self.scanner: Any = None  # app.sim.scanner.ScannerService
        self.paper: Any = None  # app.sim.paper.PaperEngine
        self.evaluator: Any = None  # app.sim.evaluator.PredictionEvaluator
        self.sim: Any = None  # app.sim.simulator.Simulator
        self.scripts: Any = None  # app.scripting.service.ScriptService
        self.maintenance: Any = None  # app.maintenance.Maintenance
        self.alerts: Any = None  # app.alerts.service.AlertService (Telegram / email)
        self.validation: Any = None  # app.validation.service.ValidationService (walk-forward reports)
        self.auth: Any = None  # app.auth.SupabaseVerifier (cloud mode only)
        # "single": the only instance (SQLite or no background work) · "leader": holds the PostgreSQL leader lock and
        # runs the services · "standby": another instance holds it; only /api/health answers (see app.main.Leadership)
        self.role: str = "single"
        self.leader: Any = None  # app.main.Leadership (PostgreSQL + background work only)
        self.started_at: float = time.time()
        self.load_errors: list[str] = []
        self.services: list[tuple[str, Any]] = []

    def reset(self) -> None:
        self.__init__()


rt = Runtime()
