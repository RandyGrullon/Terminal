"""Global configuration: paths, network binding, runtime flags and default settings."""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "1.0.0"
APP_NAME = "NEXUS Terminal"

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent
DATA_DIR = Path(os.environ.get("NEXUS_DATA_DIR", str(ROOT_DIR / "data"))).resolve()
DB_PATH = Path(os.environ.get("NEXUS_DB_PATH", str(DATA_DIR / "nexus.db"))).resolve()
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

HOST = os.environ.get("NEXUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NEXUS_PORT", "8420"))

# NEXUS_OFFLINE=1     -> no network calls at all (hub serves DB cache + SIM:* only). Used by tests.
# NEXUS_BACKGROUND=0  -> services must not start background loops (recorder, scanner, watchers...).
OFFLINE = os.environ.get("NEXUS_OFFLINE", "0") == "1"
BACKGROUND = os.environ.get("NEXUS_BACKGROUND", "1") != "0"

# ----------------------------------------------------------------------------- cloud mode (Supabase + hosting)
# NEXUS_DATABASE_URL=postgresql://...  -> PostgreSQL (Supabase) instead of the local SQLite file.
DATABASE_URL = os.environ.get("NEXUS_DATABASE_URL", "").strip()
DB_SCHEMA = os.environ.get("NEXUS_DB_SCHEMA", "nexus").strip() or "nexus"
# NEXUS_AUTH=supabase -> every API/WS call needs a Supabase session (mandatory when the server is public).
AUTH_MODE = os.environ.get("NEXUS_AUTH", "none").strip().lower()
SUPABASE_URL = os.environ.get("NEXUS_SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("NEXUS_SUPABASE_ANON_KEY", "").strip()
ALLOWED_EMAILS = [e.strip().lower() for e in os.environ.get("NEXUS_ALLOWED_EMAILS", "").split(",") if e.strip()]
# Browser origins allowed to call the API and open the WebSocket (e.g. https://nexus-terminal.vercel.app).
CORS_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("NEXUS_CORS_ORIGINS", "").split(",") if o.strip()]
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Honest identification for every outbound request (never impersonate a browser).
USER_AGENT = f"NexusTerminal/{VERSION} (+local research tool)"

DEFAULT_WATCHLIST = [
    "BINANCE:BTCUSDT",
    "BINANCE:ETHUSDT",
    "BINANCE:SOLUSDT",
    "BINANCE:BNBUSDT",
    "BINANCE:XRPUSDT",
    "NASDAQ:AAPL",
    "NASDAQ:NVDA",
    "FX:EURUSD",
    "OANDA:XAUUSD",
    "SIM:NEXUS",
]

DEFAULT_SETTINGS: dict = {
    # Risk / account
    "risk.account_size": 10000.0,
    "risk.per_trade_pct": 1.0,
    "risk.fee_pct": 0.1,
    "risk.slippage_pct": 0.02,
    # NEXUS engine
    "nexus.entry_threshold": 0.35,
    "nexus.exit_threshold": 0.0,
    "nexus.horizon": 5,
    "nexus.sl_atr": 2.0,
    "nexus.tp_r": [1.0, 2.0, 3.0],
    "nexus.trail_atr": 3.0,
    "nexus.learning_rate": 0.08,
    "nexus.allow_short": True,
    "nexus.min_bars": 150,
    "nexus.default_limit": 1000,
    "nexus.time_stop_bars": 48,
    # Trade gate ("¿tomo esta operación?")
    "gate.enabled": True,
    "gate.take_score": 65.0,
    "gate.wait_score": 45.0,
    "gate.min_rr": 1.5,
    "gate.min_prob": 0.52,
    # Watchlist monitoring
    "watch.enabled": True,
    "watch.tf": "1h",
    # Scanner
    "scanner.enabled": True,
    "scanner.interval_s": 900,
    "scanner.markets": ["crypto"],
    "scanner.tf": "1h",
    "scanner.limit": 300,
    "scanner.deep_top": 12,
    "scanner.horizon_s": 14400,
    # Local candle recorder (TradingView snapshots -> local OHLCV history)
    "recorder.enabled": True,
    "recorder.interval_s": 20,
    # Data providers
    "providers.tradingview_enabled": True,
    "providers.twelvedata_key": "",
    "providers.alphavantage_key": "",
    "providers.polygon_key": "",
    "providers.crypto_default": "binance",
    # Paper trading
    "paper.auto_trade": False,
    "paper.auto_symbols": [],
    "paper.auto_tf": "1h",
    "paper.partial_tp": True,
    # Simulator
    "sim.default_speed": 4.0,
    # UI (free-form "ui.*" keys are also accepted)
    "ui.language": "es",
    "ui.chart_indicators": [],
}
