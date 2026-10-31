"""Default settings contributed by feature modules (one file per feature), merged into config.DEFAULT_SETTINGS.

Each module only defines a plain ``DEFAULTS`` dict and must not import ``app.config`` (no import cycles).
"""
from __future__ import annotations

from . import alerts, paper_risk, realtime, scanner_model, validation

EXTRA_DEFAULTS: dict = {}
for _module in (alerts, paper_risk, realtime, scanner_model, validation):
    EXTRA_DEFAULTS.update(_module.DEFAULTS)
