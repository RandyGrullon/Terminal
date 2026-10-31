"""Default settings of the scanner-model feature (merged into DEFAULT_SETTINGS).

Offline replay of the scanner model (app.sim.evaluator replay_report, terminal `skill replay`): each resolved scanner
prediction keeps its packed feature vector for `scanner.replay_days` days, so a replay can re-predict those scans with
models that only learned from scans whose horizon had already ended. The evaluator drops older vectors every hour (and
the vector of a row that expired without a price at once). The code clamps the value to 0..30 days; 0 = the vector is
dropped as soon as the row is resolved (no replay). Storage: about 3 MB a day with the default background scan.
"""
from __future__ import annotations

DEFAULTS: dict = {
    # a week holds 42 independent 4 h windows (30 are needed before the replay can judge v2 against v1; 3 days never
    # reach them); the evaluator and `skill replay` default to the same 7 days (evaluator.REPLAY_DAYS_DEFAULT)
    "scanner.replay_days": 7,
}
