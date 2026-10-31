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
# Explicit opt-ins for risky cloud setups (the server refuses to start without them):
# - any account of the Supabase project may sign in (no NEXUS_ALLOWED_EMAILS allowlist);
# - a public server keeps its data in the container's local SQLite file (only safe with a mounted volume).
ALLOW_ANY_SUPABASE_USER = os.environ.get("NEXUS_ALLOW_ANY_SUPABASE_USER", "0") == "1"
ALLOW_LOCAL_DB = os.environ.get("NEXUS_ALLOW_LOCAL_DB", "0") == "1"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return min(max(value, lo), hi)


# Memory cap of each PyChart sandbox process (keep it well below the host/VM memory; 2 scripts may run at once).
SCRIPT_MEMORY_MB = _env_int("NEXUS_SCRIPT_MEMORY_MB", 1024, 128, 8192)

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
    "risk.fee_pct": 0.1,  # % per side for crypto and any asset class missing from risk.fee_pct_by_class
    # Typical round-trip cost per side (commission + spread) by asset class (app.data.symbols.asset_class_of):
    # a single crypto-sized fee would make the gate veto every low-volatility market (forex, indices...).
    "risk.fee_pct_by_class": {
        "forex": 0.005,
        "index": 0.01,
        "futures": 0.01,
        "bond": 0.01,
        "commodity": 0.02,
        "cfd": 0.02,
        "stock": 0.02,
        "fund": 0.02,
    },
    "risk.slippage_pct": 0.02,
    # NEXUS engine
    # Calibrated on real 1h crypto data: |score| p90 ≈ 0.21-0.24, p99 ≈ 0.35-0.53. 0.20 yields ~30 candidates per
    # 1500 bars, which the Trade Gate then filters (TOMAR / ESPERAR / NO TOMAR with the reason on the chart).
    "nexus.entry_threshold": 0.2,
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
    # Market conditions (macro semáforo: calendario económico + volatilidad)
    "macro.enabled": True,
    "macro.alerts": True,
    "macro.block_before_min": 30,
    "macro.block_after_min": 30,
    "macro.warn_before_min": 120,
    "macro.vix_caution": 22.0,
    "macro.vix_danger": 30.0,
    "macro.currencies": ["USD"],
    # UI (free-form "ui.*" keys are also accepted)
    "ui.language": "es",
    "ui.timezone": "",  # IANA zone of the user's browser (set by the UI); used for clock times in messages
    "ui.chart_indicators": [],
}

# Feature modules contribute their own defaults (alerts, paper risk limits, real-time data, scanner model, validation).
from app.settings_ext import EXTRA_DEFAULTS as _EXTRA_DEFAULTS  # noqa: E402

DEFAULT_SETTINGS.update(_EXTRA_DEFAULTS)
