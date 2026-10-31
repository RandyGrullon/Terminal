"""Default settings of the validation feature (merged into DEFAULT_SETTINGS).

Walk-forward validation of NEXUS + Trade Gate (app.validation): the default request of the "Validar" button and of the
terminal command `validar`, plus the method parameters. The risk per trade and the slippage come from risk.*.
"""
from __future__ import annotations

DEFAULTS: dict = {
    "validation.symbols": ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:SOLUSDT", "BINANCE:XRPUSDT",
                           "BINANCE:BNBUSDT"],
    "validation.tfs": ["1h", "4h"],
    "validation.bars": 3000,  # closed bars per series (1000..5000)
    "validation.folds": 5,  # consecutive walk-forward periods after the warm-up (2..10)
    "validation.warmup_bars": 500,  # first bars whose decisions are not scored (the model is still cold)
    "validation.random_runs": 1000,  # Monte Carlo sequences of random entries per series
}
