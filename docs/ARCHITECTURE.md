# NEXUS Terminal — Architecture & Contracts (source of truth)

NEXUS Terminal is a **local-first trading research terminal**: it browses every TradingView asset, charts it
with any indicator, runs the self-learning **NEXUS** signal engine (buy / sell / hold / exit, SL/TP "hasta aquí"
lines, early-exit detection) and — above all — a **Trade Gate** that tells the user **when NOT to take a trade and
when to take it**. It has a PyChart (Python, Pine-like) script compiler, a live terminal, a market scanner with
predictions that are later scored (feedback loop), a market simulator (synthetic + replay), backtests, Monte Carlo
and paper trading. Everything is persisted in a local SQLite DB created automatically on start.

JSON shapes: **`frontend/src/api/types.ts` is canonical.** Python must emit exactly those shapes (snake_case).

---------------------------------------------------------------------------------------------------------------------
## 0. Golden rules (apply to every contributor)

1. **Local only**: server binds `127.0.0.1:8420` (env `NEXUS_HOST`, `NEXUS_PORT`). DB at `data/nexus.db`
   (env `NEXUS_DB_PATH`). One command starts everything (`start.bat` / `start.ps1` / `start.sh`).
2. **Spanish UI**: every user-facing string (labels, terminal output, reasons, warnings, errors in `detail`) is Spanish.
   Code, identifiers and comments in English.
3. **No real money**: simulation / paper trading only. Never implement broker order routing.
4. **Legit data access only**: official/public endpoints with plain requests. NEVER spoof browser `User-Agent`,
   `Origin`, `Referer` or cookies; never bypass bot checks, captchas or JS challenges; never use `yfinance` /
   `curl_cffi` impersonation; never scrape HTML. Use the shared HTTP client `app.util.http_client()` (honest UA
   `NexusTerminal/1.0`, **system trust store via `truststore`** — the user is behind a corporate TLS proxy, so
   plain `httpx.AsyncClient()` with certifi FAILS; always use the factory).
5. **Causality**: any value at bar `i` uses only bars `<= i`. Entries/exits in backtests execute at the next bar's
   open (or intrabar at SL/TP levels, SL first when both are touched in the same bar). Tests must prove no lookahead.
6. **Never crash the app**: providers/services catch their errors, emit an event (`warn`/`error`, rate-limited)
   and degrade gracefully. A missing optional module (`rt.x is None`) must be handled.
7. **JSON hygiene**: no NaN/Infinity on the wire (use `app.util.clean_json`). Bar times = unix seconds;
   `ts`/`*_at` = unix milliseconds.
8. **Dependencies are fixed**: backend = fastapi, uvicorn[standard], httpx, numpy 2.x, pandas 3.x, aiosqlite,
   truststore, pytest (Python 3.13). Frontend = react 18, lightweight-charts **5.x**, @uiw/react-codemirror +
   @codemirror/lang-python + @codemirror/theme-one-dark, zustand 4, lucide-react 0.460. Do not add packages.
9. **Honesty**: no indicator predicts the future with certainty. Show probabilities, confidence and measured
   out-of-sample accuracy; never claim guaranteed profits. Include the disclaimer where results are shown.

---------------------------------------------------------------------------------------------------------------------
## 1. Repository layout & ownership

```
start.bat  start.ps1  start.sh  README.md  docs/ARCHITECTURE.md                     [LEAD]
backend/requirements.txt                                                            [LEAD]
backend/app/__init__.py config.py util.py db.py schema.sql settings.py events.py     [LEAD]
backend/app/ws.py terminal.py runtime.py main.py api/__init__.py api/system.py       [LEAD]
backend/app/data/__init__.py data/base.py                                           [LEAD]
backend/app/sim/__init__.py sim/synthetic.py                                         [LEAD]
backend/app/engine/__init__.py  backend/app/scripting/__init__.py                    [LEAD] (empty)
backend/tests/conftest.py                                                           [LEAD]

backend/app/data/*  (all other files)  + app/api/market.py  + tests/test_data_*.py       [BE-DATA]
backend/app/engine/*                    + app/api/nexus.py   + tests/test_engine_*.py     [BE-ENGINE]
backend/app/sim/*   (all but synthetic) + app/api/sim.py     + tests/test_sim_*.py        [BE-SIM]
backend/app/scripting/*                 + app/api/scripts.py + tests/test_scripting_*.py  [BE-SCRIPT]

frontend/ index.html vite.config.ts tsconfig.json package.json                       [LEAD]
frontend/src/main.tsx App.tsx styles.css store.ts                                    [LEAD]
frontend/src/api/{types,client,ws}.ts  src/lib/{format,chart}.ts  src/components/ui.tsx   [LEAD]

frontend/src/pages/ChartPage.tsx  src/components/chart/**  src/components/nexus/**              [FE-CHART]
frontend/src/components/terminal/**  src/components/SymbolSearch.tsx
         src/pages/{MarketsPage,ScannerPage,PaperPage}.tsx  src/components/{markets,scanner,paper}/** [FE-OPS]
frontend/src/pages/SimulationPage.tsx  src/components/sim/**                                    [FE-SIM]
frontend/src/pages/{StudioPage,BrainPage,SettingsPage}.tsx  src/components/{studio,brain,settings}/** [FE-STUDIO]
```

**Rules for agents**
- Edit only files you own (create new files freely inside your owned folders). If you need a change in a LEAD
  file, do not edit it: work around it in your files and list the request in your final report.
- Never run `pip install`, `uv`, `npm install` or `vite build`. Everything needed is installed.
- Backend python: `backend/.venv/Scripts/python.exe` (Windows). Run your tests from `backend/`:
  `.venv/Scripts/python.exe -m pytest tests/test_<prefix>_*.py -q`.
- Frontend typecheck from `frontend/`: `npx tsc --noEmit -p tsconfig.json` (errors in files you don't own may
  exist while others are still working — only your files must be clean).
- If you start the server for manual tests use your port and a DB under `backend/.tmp/` (gitignored; do NOT use
  the system temp/scratchpad dir — Windows long paths are disabled on this machine and fail), and stop it after:
  BE-DATA 8431 · BE-ENGINE 8432 · BE-SIM 8433 · BE-SCRIPT 8434 ·
  `NEXUS_PORT=<port> NEXUS_DB_PATH=.tmp/<agent>.db .venv/Scripts/python.exe -m app.main` (from `backend/`).
- Runtime flags (read them at call time as `from app import config; config.OFFLINE` so tests can monkeypatch):
  `config.OFFLINE` → never touch the network (hub serves DB cache + `SIM:*` only); `config.BACKGROUND` → when
  False do not start background loops (recorder, scanner loop, watchers, evaluator loop, paper monitor).
  The `api_client` test fixture sets OFFLINE=True and BACKGROUND=False.
- Atomic multi-statement writes: `async with rt.db.transaction() as tx: await tx.execute(...)` (paper trading).
- Network calls in unit tests must be optional: mark them `@pytest.mark.network` and skip unless
  `NEXUS_NET_TESTS=1` (the provided conftest handles the marker). Core logic must be testable offline
  (use `app.sim.synthetic.generate_candleset`).

---------------------------------------------------------------------------------------------------------------------
## 2. Runtime & services

`from app.runtime import rt` — service locator filled during the FastAPI lifespan.

| attr | class | owner | module |
|---|---|---|---|
| `rt.db` | `Database` | LEAD | `app.db` |
| `rt.settings` | `SettingsStore` | LEAD | `app.settings` |
| `rt.bus` | `EventBus` | LEAD | `app.events` |
| `rt.ws` | `WSManager` | LEAD | `app.ws` |
| `rt.hub` | `MarketDataHub` | BE-DATA | `app.data.hub` |
| `rt.nexus` | `NexusService` | BE-ENGINE | `app.engine.service` |
| `rt.scanner` | `ScannerService` | BE-SIM | `app.sim.scanner` |
| `rt.paper` | `PaperEngine` | BE-SIM | `app.sim.paper` |
| `rt.evaluator` | `PredictionEvaluator` | BE-SIM | `app.sim.evaluator` |
| `rt.sim` | `Simulator` | BE-SIM | `app.sim.simulator` |
| `rt.scripts` | `ScriptService` | BE-SCRIPT | `app.scripting.service` |

**Service protocol**: `__init__(self, rt)` (no I/O), `async start()` (returns fast; spawn loops with
`asyncio.create_task`), `async stop()` (cancel tasks, close clients), `status() -> dict` (JSON-safe, must include
`"running": bool`). All services are constructed first (table order), then started in table order; stopped in
reverse. Loops: `try/except` per iteration, emit errors (rate-limited), `await asyncio.sleep(...)`.
Routers: `app.api.<module>` exposes `router = APIRouter()` (paths are absolute, e.g. `@router.get("/api/candles")`).
Terminal commands: `app.<pkg>.commands` registers commands at import time.
Failing imports are logged (`/api/health.load_errors`) and the rest keeps working.

---------------------------------------------------------------------------------------------------------------------
## 3. Foundation APIs (LEAD — import them, don't modify)

### `app.config`
`VERSION`, `ROOT_DIR`, `BACKEND_DIR`, `DATA_DIR`, `DB_PATH`, `FRONTEND_DIST`, `HOST`, `PORT`, `USER_AGENT`,
`DEFAULT_SETTINGS` (dict, see §4), `DEFAULT_WATCHLIST` (list of symbols).

### `app.util`
- `TF_SECONDS` (dict), `TIMEFRAMES` (ordered list), `tf_seconds(tf)`, `normalize_tf(text)` (accepts `60`, `1H`,
  `h1`, `D`, `1D`, `W`, `240`…; raises `ValueError`), `floor_time(ts, tf)` (weeks align to Monday 00:00 UTC).
- `now_s()`, `now_ms()`.
- `clean_json(obj)` — recursively converts numpy types/arrays, NaN/inf → None, tuples → lists, datetime → iso.
- `to_list(arr, decimals=None)` — numpy array → list with None for NaN.
- `parse_symbol("BINANCE:BTCUSDT") -> ("BINANCE", "BTCUSDT")` (no prefix → `("", ticker)`).
- `http_client(timeout=15.0, **kw) -> httpx.AsyncClient` — **the only way to make HTTP calls**.
- `RateLimiter(min_interval_s, max_concurrency=1)` — `async with limiter: ...`.
- `TTLCache(ttl_s, maxsize=512)` — `get(key)` (None if missing/expired), `set(key, value)`.
- `safe_float(x, default=None)`, `clamp(x, lo, hi)`, `RateLimitedLog` (`should_log(key, every_s) -> bool`).

### `app.db` (aiosqlite, WAL, one connection + asyncio.Lock)
```python
await rt.db.execute(sql, params=()) -> int          # lastrowid, commits
await rt.db.executemany(sql, seq_of_params) -> None # commits
await rt.db.fetchall(sql, params=()) -> list[dict]
await rt.db.fetchone(sql, params=()) -> dict | None
await rt.db.fetchval(sql, params=(), default=None)
await rt.db.table_counts() -> dict[str, int]
```
Schema: `app/schema.sql` (tables listed in §5). Add columns only via LEAD. JSON blobs are TEXT (`json.dumps`).

### `app.settings`
`rt.settings.get(key, default=None)`, `rt.settings.all()`, `await rt.settings.set(key, value)`,
`await rt.settings.update(dict)`. Values coerced to the default's type; unknown keys accepted only with `ui.` prefix.

### `app.events`
`rt.bus.emit(level, source, message, data=None) -> dict` (sync; from threads use `emit_threadsafe`).
Levels: `debug info success warn error signal trade learn scan system`.
Event JSON: `{"id","ts","level","source","message","data"}`. Every event is persisted (table `events`), printed to
the server console and published on WS topic `events`. Messages: one line, Spanish, informative, e.g.
`"BINANCE:BTCUSDT 1h → COMPRAR · score 0.52 · p↑ 64% · Filtro: TOMAR 78/100"`.
`rt.bus.recent(limit=200, level=None, source=None)`; `rt.bus.add_listener(fn)` (sync fn(event)).

### `app.ws`
`rt.ws.publish(topic, message)` — non-blocking fan-out. Topics: `events sim paper scanner ui` (default-subscribed)
and `market:{SYMBOL}:{tf}` (e.g. `market:BINANCE:BTCUSDT:1h`) on demand. When a client subscribes to a market
topic, WSManager calls `rt.hub.subscribe(symbol, tf, owner="ws:<id>")`, and unsubscribes on leave/disconnect.
Protocol: see `ServerMessage` in types.ts. Client → server: `subscribe {topic}`, `unsubscribe {topic}`,
`command {id, text}`, `ping`.

### `app.terminal`
```python
from app.terminal import command, CommandContext, CommandError
@command("scan", help="Escanea un mercado", usage="scan [mercado] [tf] [--deep] [--limit N]",
         group="Escáner", aliases=("escanear",))
async def _scan(ctx: CommandContext) -> None:
    market = ctx.arg(0, "crypto"); deep = ctx.flag("deep", False); limit = ctx.flag_int("limit", 200)
    sym = await ctx.symbol(ctx.arg(0))      # "btc" -> "BINANCE:BTCUSDT" via rt.hub.resolve
    ctx.print("texto", "scan"); ctx.table(["Símbolo", "Score"], [["BTC", "0.52"]])
    ctx.action("open_chart", symbol=sym, tf="1h"); ctx.data = {...}
    ctx.fail("mensaje")                     # -> ok=false
```
Core commands (LEAD): `help status services events settings db version clear echo`.
Command result = `CommandResult` in types.ts. Long jobs: start a task, print "iniciado", report via events.

### `app.data.base`
```python
class CandleSet:                     # ascending, unique times; numpy float64 arrays (time is int64)
    symbol, tf, provider, history ("full"|"keyed"|"recorder"|"synthetic"|"none"), note
    time, open, high, low, close, volume
    __len__, last_time, last_close, is_empty
    from_rows(rows, symbol, tf, provider="", history="full", note=None)   # rows: dicts or (t,o,h,l,c,v)
    empty(symbol, tf, note=None, provider="", history="none")
    tail(n), slice(i0, i1), upto(index_inclusive), append(candle_dict) -> CandleSet (new object)
    to_list() -> list[Candle dict];  to_frame() -> pandas.DataFrame
resample(cs, tf) -> CandleSet         # to a higher tf (last bucket may be partial)
merge_candles(old: list[dict], new: list[dict]) -> list[dict]  # by time, new wins
class ProviderError(Exception)
```

### `app.sim.synthetic`
`generate_candleset(n=1500, seed=7, tf="1h", regime="auto", start_price=100.0, symbol="SIM:NEXUS",
end_time=None) -> CandleSet` (deterministic) and `SyntheticMarket(SyntheticConfig)` with `.next_bar() -> dict`,
`.history(n) -> list[dict]`, `.regime`, `.set_regime(name)`. Regimes: `auto bull bear range volatile crash`.
Markov regime switching + GARCH(1,1)-like volatility clustering + jumps + mean reversion in `range`.

---------------------------------------------------------------------------------------------------------------------
## 4. Settings (DEFAULT_SETTINGS in app/config.py)

```
risk.account_size 10000 · risk.per_trade_pct 1.0 · risk.fee_pct 0.1 · risk.slippage_pct 0.02
nexus.entry_threshold 0.35 · nexus.exit_threshold 0.0 · nexus.horizon 5 · nexus.sl_atr 2.0
nexus.tp_r [1.0, 2.0, 3.0] · nexus.trail_atr 3.0 · nexus.learning_rate 0.08 · nexus.allow_short true
nexus.min_bars 150 · nexus.default_limit 1000 · nexus.time_stop_bars 48
gate.enabled true · gate.take_score 65 · gate.wait_score 45 · gate.min_rr 1.5 · gate.min_prob 0.52
watch.enabled true · watch.tf "1h"
scanner.enabled true · scanner.interval_s 900 · scanner.markets ["crypto"] · scanner.tf "1h"
scanner.limit 300 · scanner.deep_top 12 · scanner.horizon_s 14400
recorder.enabled true · recorder.interval_s 20
providers.tradingview_enabled true · providers.twelvedata_key "" · providers.alphavantage_key ""
providers.polygon_key "" · providers.crypto_default "binance"
paper.auto_trade false · paper.auto_symbols [] · paper.auto_tf "1h" · paper.partial_tp true
sim.default_speed 4 · ui.language "es" · ui.chart_indicators []
```

---------------------------------------------------------------------------------------------------------------------
## 5. Database (app/schema.sql)

`meta settings symbols candles watchlist model_state signals predictions gate_log scan_runs scan_results
paper_accounts paper_positions paper_fills events scripts backtests` — see the file for columns.
- `candles(symbol, tf, time, open, high, low, close, volume, source)` PK (symbol, tf, time). Cache + recorder.
- `model_state(kind, key, state JSON, updates, trained_until, updated_at)` PK (kind, key).
  kinds: `nexus` (key `SYMBOL|tf`), `scanner` (key `market|tf|horizon`), `gate` (key `global`).
- `predictions` are written at creation time and resolved later by the evaluator → honest out-of-sample stats.
- `gate_log` stores every gate decision (verdict + checks) and its later outcome → the gate learns which checks work.
- `paper_accounts`: id 1 = "Principal", id 2 = "Simulación" (created on start if missing).

---------------------------------------------------------------------------------------------------------------------
## 6. REST API (all under /api; errors → `{"detail": "<español>"}`)

### 6.1 System [LEAD]
`GET /api/health` → Health · `GET /api/settings` → `{settings}` · `PUT /api/settings` body `{key: value}` →
`{settings}` · `GET /api/events?limit&level&source` → `{events}` · `GET /api/terminal/commands` → `{commands}` ·
`POST /api/terminal {text}` → CommandResult · `GET /api/db/stats` → DbStats · `POST /api/db/vacuum`.

### 6.2 Market [BE-DATA]
```
GET    /api/markets                                    -> {markets: MarketInfo[]}
GET    /api/symbols/search?q=&market=&limit=30          -> {results: SearchResult[]}
GET    /api/universe?market=&offset=&limit=&sort=&order=&q=&type=  -> UniverseResponse
GET    /api/candles?symbol=&tf=&limit=                  -> CandlesResponse
GET    /api/quotes?symbols=A,B                          -> {quotes: {symbol: Quote}}
GET    /api/symbol?symbol=                              -> SymbolDetail
GET    /api/watchlist                                   -> {items: WatchItem[]}
POST   /api/watchlist {symbol}                          -> {items}
DELETE /api/watchlist?symbol=                           -> {items}
```
Hub python API (used by other modules):
```python
await rt.hub.get_candles(symbol, tf, limit=500) -> CandleSet   # empty set + note when no data (never raises for that)
await rt.hub.search(q, market=None, limit=30) -> list[dict]
await rt.hub.resolve(text) -> str | None                       # "btc"->"BINANCE:BTCUSDT", "aapl"->"NASDAQ:AAPL"
await rt.hub.symbol_info(symbol) -> dict                       # SymbolInfo
await rt.hub.quotes(symbols) -> dict[str, dict]                # Quote by symbol
await rt.hub.last_price(symbol) -> float | None
await rt.hub.universe(market, offset=0, limit=100, sort="volume", order="desc", q=None, type_=None) -> dict
await rt.hub.tv_scan(market, columns, *, filters=None, sort_by=None, order="desc", limit=300, offset=0,
                     tickers=None) -> {"total": int, "rows": [{"symbol": str, <column>: value, ...}]}
rt.hub.subscribe(symbol, tf, owner) ; rt.hub.unsubscribe(symbol, tf, owner)   # ref-counted live feed
rt.hub.add_listener(cb)        # async cb(symbol, tf, candle: dict, closed: bool) — called on every live update
await rt.hub.push_candle(symbol, tf, candle, closed)          # simulator feeds SIM:* bars through the hub
await rt.hub.watchlist() -> list[str]
```

### 6.3 NEXUS, Gate & Indicators [BE-ENGINE]
```
GET  /api/nexus?symbol=&tf=&limit=                        -> NexusResponse
GET  /api/nexus/model?symbol=&tf=                         -> NexusModel
POST /api/nexus/reset {symbol, tf}                        -> {ok: true}
GET  /api/gate/check?symbol=&tf=&direction=long|short[&entry=&sl=&tp=]   -> GateResult
GET  /api/indicators/catalog                              -> {indicators: IndicatorSpec[]}
POST /api/indicators/compute {symbol, tf, limit, items: IndicatorRequestItem[]} -> IndicatorComputeResponse
```
Python API:
```python
from app.engine.nexus import NexusParams, run_nexus   # pure & deterministic
res = run_nexus(cs, NexusParams(...), prior=None)    # -> NexusResult
res.to_response(cs) -> dict (NexusResponse minus provider/note/quick which the service fills)
res.current, res.trades, res.markers, res.bars, res.prediction, res.learning, res.stats, res.snapshots
NexusParams.from_settings(rt.settings.all(), **overrides)
from app.engine.gate import evaluate_gate            # see §8
from app.engine import ta                            # numpy TA functions (causal)
from app.engine.indicators import CATALOG, compute_indicator(id, cs, params) -> dict[str, np.ndarray]
await rt.nexus.analyze(symbol, tf, limit=None) -> dict    # NexusResponse (cached by last bar time)
await rt.nexus.current(symbol, tf) -> dict | None         # NexusCurrent
await rt.nexus.gate(symbol, tf, direction, entry=None, sl=None, tp=None) -> dict   # GateResult
```

### 6.4 Simulation, Paper, Scanner, Learning [BE-SIM]
```
POST /api/backtest {symbol, tf, limit=2000, params={}, initial_capital=10000, risk_pct=1, fee_pct=0.1,
                    slippage_pct=0.02, use_gate=true}                                  -> BacktestResult
POST /api/montecarlo {mode: bootstrap|synthetic, symbol?, tf?, paths=300, bars=500, seed=1,
                      initial_capital=10000}                                            -> MonteCarloResult
POST /api/sim/start SimStartRequest  -> {state}     POST /api/sim/stop -> {state}
POST /api/sim/config {speed?, regime?, paused?, auto_trade?} -> {state}
GET  /api/sim/state  -> SimSnapshot
GET  /api/paper/account                     -> PaperSnapshot (account 1)
POST /api/paper/order {symbol, side: long|short, tf?, qty?, risk_pct?, sl?, tp?, force?}
     -> {position, gate}; if gate verdict is AVOID and not force -> HTTP 409 detail {message, gate}
POST /api/paper/close {position_id? | symbol? | all?} -> {closed: Position[]}
POST /api/paper/reset {initial_balance?} -> PaperSnapshot
POST /api/paper/auto {enabled, symbols?, tf?} -> {auto: {enabled, symbols, tf}}
POST /api/scanner/run {market, tf, limit, deep, deep_top} -> {run_id}
GET  /api/scanner/results?run_id=        -> {run: ScanRun | null, results: ScanRow[]}   (default: latest)
GET  /api/scanner/runs?limit=20          -> {runs: ScanRun[]}
GET  /api/scanner/quick?symbol=&tf=      -> QuickSignal
GET  /api/predictions?source=&symbol=&resolved=&limit= -> {predictions: PredictionRow[], stats: PredictionStats}
GET  /api/learning/summary               -> LearningSummary
```
Python API: `await rt.scanner.quick_signal(symbol, tf) -> dict | None` (QuickSignal);
`from app.sim.backtest import run_backtest`; `rt.paper.snapshot(account_id=1)`; `rt.sim.state()`.

### 6.5 PyChart scripts [BE-SCRIPT]
```
GET /api/scripts -> {scripts: ScriptMeta[]}      GET /api/scripts/{id} -> Script
POST /api/scripts {name, code, description?} -> Script     PUT /api/scripts/{id} -> Script
DELETE /api/scripts/{id} -> {ok}
POST /api/scripts/compile {code} -> CompileResult
POST /api/scripts/run {code, symbol, tf, limit=1000, inputs={}} -> ScriptRunResult
GET  /api/scripts/reference -> ScriptReference
```

---------------------------------------------------------------------------------------------------------------------
## 7. NEXUS adaptive engine (BE-ENGINE)

Pure function `run_nexus(cs, params, prior)` processes bars **sequentially and causally**:

1. **Indicators** (vectorized, causal): EMA 9/21/55/200, RSI 14, MACD 12/26/9, ATR 14, ADX/DMI 14, Bollinger 20/2,
   Supertrend 10/3, Donchian 20, StochRSI, OBV, volume z-score, Kaufman efficiency ratio, linear-regression slope
   t-stat, swing pivots (support/resistance), higher-timeframe trend (resample ×4, aligned causally: a HTF bar is
   only visible after it closes).
2. **Experts** (≥ 10), each a signal in [-1, 1]: trend ribbon, MACD momentum, RSI (momentum in trends,
   mean-reversion in ranges), Bollinger z (regime-aware), Donchian breakout, Supertrend, ADX/DMI, volume/OBV
   confirmation, regression slope, HTF trend, StochRSI cross, price action (engulfing / rejection at S/R).
3. **Regime**: `trend_up | trend_down | range | volatile` from ADX, efficiency ratio, slope and ATR percentile.
4. **Learning (feedback)**: the label for bar `i` is the sign of `close[i+h] - close[i]` (h = `nexus.horizon`),
   known only at bar `i+h`. At bar `j` the engine updates with bar `j-h`:
   - **Hedge / multiplicative weights per regime** with fixed-share mixing (so it recovers after regime changes).
   - **Online logistic regression** (AdaGrad, L2) on [expert signals, regime one-hot, vol percentile, bias] →
     calibrated `prob_up`.
   - Tracks hit-rate and Brier score of its own past predictions (out-of-sample by construction).
   `score = 0.5 * weighted_vote + 0.5 * (2·prob_up − 1)`; `confidence` from agreement + calibration quality.
5. **Prior / memory**: `prior` = snapshot of learner state. The service persists periodic snapshots
   (`res.snapshots`, every ~100 bars, keyed by bar time) and passes as prior the latest snapshot whose time
   `<=` the first bar of the window → long memory across sessions with **zero lookahead**.
6. **State machine** (hysteresis + cooldown): flat → candidate long when `score >= entry_threshold` (short when
   `<= -entry_threshold` and allow_short). **Every candidate goes through the Trade Gate (§8)**: TAKE → enter at
   next bar open; WAIT / AVOID → no trade, emit marker `REJECTED_LONG/SHORT` with the main reason.
   In position: `HOLD_*` while healthy; exits: SL (`EXIT_SL`), trailing (`EXIT_TRAIL`), TP3, opposite TAKE signal
   (`EXIT_REVERSE`), time stop (`EXIT_TIME`), score crossing `exit_threshold` against (`EXIT_SIGNAL`) and
   **early exit** (`EXIT_EARLY`) when the expected value of holding turns negative:
   `ev_hold_r = p_dir·(dist_to_next_tp/R) − (1 − p_dir)·(dist_to_sl/R)`; also on bearish divergence, volatility shock
   against the position, or regime flip. Every exit carries a Spanish reason.
7. **Levels ("hasta aquí")**: `SL = entry ∓ max(sl_atr·ATR, distance beyond the last swing ± 0.25 ATR)`, R = |entry−SL|,
   `TP1..3 = entry ± tp_r·R`. TP1 → close 1/3 + SL to break-even; TP2 → close 1/3 + chandelier trailing
   (`trail_atr·ATR`); TP3 → close rest. Per-bar arrays `pos_entry/pos_sl/pos_tp1..3` (null when flat).
8. **Prediction**: horizon h; `prob_up`; cone p10..p90 using analog forecasting (empirical forward returns of past
   bars with similar score/regime) falling back to a volatility (√t) cone.
9. **Outputs**: exactly `NexusResponse` (bars, markers, trades, current, prediction, learning, stats). `current`
   always includes `gate.long` and `gate.short` evaluated at the last bar, `advice` (one actionable Spanish line)
   and `warnings`.

Spanish labels: actions BUY=COMPRAR, SELL=VENDER, HOLD_LONG=MANTENER LARGO, HOLD_SHORT=MANTENER CORTO,
WAIT=ESPERAR, EXIT_LONG=CERRAR LARGO, EXIT_SHORT=CERRAR CORTO. Regimes trend_up=Tendencia alcista,
trend_down=Tendencia bajista, range=Lateral, volatile=Volátil. Verdicts TAKE=TOMAR, WAIT=ESPERAR, AVOID=NO TOMAR.

**NexusService** (`app.engine.service`): `analyze()` (hub candles → run_nexus with stored prior → persist snapshots
in `model_state` → fill provider/note/quick → cache by last bar). When candles < `nexus.min_bars`: `ok=false`,
friendly `message`, and `quick = await rt.scanner.quick_signal(...)` so ANY TradingView asset still gets a read.
Watcher: subscribes the hub for watchlist symbols at `watch.tf`; on each closed bar recomputes, publishes
`{"type":"nexus",...}` on `market:{symbol}:{tf}`, writes `signals` when the action changes, writes a `predictions`
row (source `nexus`), writes `gate_log` rows for candidates, and emits events (`signal` level for entries/exits,
`warn` for early-exit advice, `info` for rejected signals with the reason).

## 8. Trade Gate — "¿Tomo esta operación?" (BE-ENGINE, `app.engine.gate`)

`evaluate_gate(ctx, direction, params, *, entry=None, sl=None, tp=None, gate_state=None) -> dict` (GateResult).
`ctx` = everything computed at bar i (indicators, regime, prob_up, score, analog stats, S/R, HTF trend, recent
trades). Checks (id → Spanish label; `critical` ones force AVOID when failed):
| id | passes when | critical |
|---|---|---|
| `trend_alignment` | direction agrees with HTF trend | |
| `regime` | not choppy (ER ≥ 0.25 or ADX ≥ 18) for trend entries | ✓ if volatile+choppy |
| `momentum` | MACD hist & RSI side agree | |
| `room_to_target` | nearest opposing S/R ≥ 1.5 R away | |
| `risk_reward` | RR(TP2) ≥ `gate.min_rr` after fees | ✓ when < 1.2 |
| `overextension` | |close−EMA21| ≤ 2.5 ATR and RSI not exhausted (≤75 long / ≥25 short) | |
| `volatility` | ATR percentile in [8, 95] and no shock bar (range > 3 ATR) | ✓ on shock |
| `volume` | volume z ≥ −0.5 | |
| `probability` | learned P(direction) ≥ `gate.min_prob` | |
| `edge_history` | analog win-rate ≥ 50% with n ≥ 8 (else neutral) | |
| `expected_value` | EV(R) > 0.05 after fees | ✓ when ≤ 0 |
| `recent_performance` | not ≥ 4 consecutive losses (circuit breaker) | |
| `confluence` | ≥ 60% of weighted experts agree | |
Score = Σ weight·pass / Σ weight over non-neutral checks → 0..100. Verdict: TAKE if score ≥ `gate.take_score`
and no critical failure; WAIT if ≥ `gate.wait_score` or the main failure is "wait-able" (overextension, momentum,
volume) ; else AVOID. Check weights are **learned**: online logistic regression on the check vector predicting
"trade hit TP1 before SL", trained from closed trades (backtest/live) and resolved `gate_log` rows; persisted in
`model_state(kind='gate', key='global')`. `reasons_for` / `reasons_against` / `summary` are Spanish and concrete
(numbers: "Resistencia a 0.6 ATR (R:R 1.1)"). The user can check any idea: `GET /api/gate/check` with custom
entry/SL/TP. Terminal: `check btc long [--sl X --tp Y]`.

## 9. Indicator library (BE-ENGINE, `app.engine.indicators`)

`CATALOG: list[IndicatorSpec dict]` with **≥ 70 indicators**, all causal numpy implementations, Spanish names and
descriptions, categories `Tendencia, Momento, Volatilidad, Volumen, Soportes/Resistencias, Estadística, NEXUS`.
Must include at least: SMA, EMA, WMA, HMA, DEMA, TEMA, TRIMA, KAMA, ZLEMA, ALMA, VWMA, SMMA/RMA, LSMA, McGinley,
T3, VWAP (session + rolling), Supertrend, Parabolic SAR, Ichimoku, Bollinger, Keltner, Donchian, Envelopes,
Linear-regression channel, Chandelier exit, Pivot points (classic/fibonacci/camarilla), ZigZag, Williams
fractals, MA ribbon; RSI, Stochastic, StochRSI, MACD, CCI, Williams %R, ROC, Momentum, TRIX, TSI, Ultimate osc,
Awesome osc, Accelerator osc, KST, PPO, DPO, Coppock, Connors RSI, Fisher transform, Schaff trend cycle, Elder ray,
Balance of power, CMO, RVI, Aroon (+osc), ADX/DMI, Vortex, Choppiness, Mass index; ATR, NATR, %B, BB width,
historical volatility, stdev, Ulcer index, Chaikin volatility; Volume+MA, OBV, A/D line, CMF, Chaikin osc, MFI,
Force index, EOM, Volume osc, PVT, NVI/PVI, Klinger, relative volume; Z-score, Hurst, Efficiency ratio, Correlation
vs. time (R²); NEXUS score, NEXUS probability. Each spec: params with defaults/min/max, outputs with colors and
type, `levels` for oscillators.

## 10. Scanner & market-wide learning (BE-SIM, `app.sim.scanner`)

- **Quick mode (all assets)**: one `rt.hub.tv_scan` per market (paged, ≤ `limit` rows sorted by volume / liquidity)
  pulling TradingView technical columns at several timeframes (e.g. `RSI|60`, `MACD.macd|240`, `Recommend.All`,
  `ADX`, `BB.upper/lower`, `EMA20/50`, `SMA200`, `Perf.W`, `volume`, `average_volume_10d_calc`, `change`).
  Features are normalized and fed to an **online logistic model per (market, tf, horizon)** → `prob_up`,
  `score = 2p−1` blended with a rule-based prior while the model is cold (few updates). Every scanned asset gets a
  `predictions` row (source `scanner`, features stored as JSON) to be resolved after `scanner.horizon_s`.
- **Deep mode**: top-N candidates with full history (crypto exchanges / keyed providers) run full NEXUS + Gate →
  `verdict`, `gate_score`, SL/TP, regime.
- Background loop every `scanner.interval_s` for `scanner.markets`; results in `scan_runs/scan_results`; WS
  `scanner` messages; terminal events summarizing the top opportunities ("TOMAR") and the rejected ones.
- `quick_signal(symbol, tf)` works for **any** TradingView symbol (single-ticker scan).

**PredictionEvaluator** (`app.sim.evaluator`): every 60 s resolves due predictions (prices via `rt.hub.quotes` /
candles), writes outcome, updates the scanner model with the stored features (SGD step), resolves `gate_log` rows,
updates the gate learner, and emits `learn` events ("Modelo escáner crypto|1h: 37 predicciones resueltas · acierto
58.3% (rolling 200)").

## 11. Simulator, replay, backtest, Monte Carlo, paper (BE-SIM)

- **Simulator** (`rt.sim`): one session at a time. `synthetic` (SyntheticMarket, symbol `SIM:NEXUS`) or `replay`
  (real history revealed bar by bar — NEXUS sees only revealed bars). Speed 0.5–20 bars/s; pause/resume; regime can
  be changed live. Each bar: hub `push_candle` → NEXUS on the revealed window → auto paper trading on account 2
  (only gate TAKE signals) → WS `sim` message (`bar`, `current`, new `markers`, `positions`) + events.
- **Backtest**: `run_backtest(cs, params, ...)` uses run_nexus outputs (signals are causal), sizes positions by
  risk % of equity and SL distance, applies fees & slippage, partial TPs, returns BacktestResult; with
  `use_gate=true` also runs the ungated variant → `ungated` + `gate_lift_pct` so the user SEES what the filter adds.
- **Monte Carlo**: `bootstrap` (resample trade R-multiples/returns from a backtest of the symbol) or `synthetic`
  (run NEXUS on N synthetic markets with different seeds/regimes; keep it fast — cap bars/paths). Percentile fan,
  distributions, P(profit), P(ruin), VaR/CVaR.
- **PaperEngine** (`rt.paper`): accounts 1 (Principal) & 2 (Simulación). Orders at market with SL/TP from the
  NEXUS gate levels when not given; sizing by `risk.per_trade_pct`. Subscribes hub (`owner="paper"`) for open
  positions; manages SL/TP/partials/trailing/early-exit advice (`advice` + `warnings` from `rt.nexus.current`);
  auto-trade mode for `paper.auto_symbols` at `paper.auto_tf` takes only TAKE verdicts; publishes WS `paper` and
  `trade` events ("Cerrado BTC largo +2.1R · TP2").

## 12. PyChart (BE-SCRIPT) — Python scripts with a Pine-like API

Scripts are plain Python executed in a **separate subprocess** (`subprocess.run` via `asyncio.to_thread`, timeout
10 s, `-I` isolated mode, cwd = temp dir) after **AST validation**: allowed imports only `math`, `numpy`/`np`,
`statistics`; no dunder attribute access, no `open/exec/eval/compile/__import__/globals/locals/vars/getattr/
setattr/delattr/input/breakpoint/help/memoryview`, no numpy/pandas file or ctypes APIs. Restricted builtins.
Errors map to the user's line numbers. API surface (vectorized numpy series aligned with bars):
`open high low close volume time hl2 hlc3 ohlc4 bar_index`, `indicator(title, overlay=True)`,
`strategy(title, initial_capital=10000, risk_pct=1, fee_pct=0.1)`, `input.int/float/bool/string/select/source`,
`ta.*` (sma ema wma hma rma vwma rsi macd stoch atr tr bb kc donchian supertrend sar adx dmi cci mfi obv roc mom
stdev variance highest lowest highestbars lowestbars change crossover crossunder cross rising falling pivothigh
pivotlow barssince valuewhen cum linreg correlation percentrank median vwap wpr cmo tsi), `nz na fixnan`,
`plot(series, title, color, linewidth, style, pane)`, `plotshape(cond, title, location, color, shape, text)`,
`hline(price, title, color, style)`, `bgcolor(cond, color)`, `alertcondition(cond, title, message)`,
`strategy.entry(id, direction, when, qty_pct)`, `strategy.exit(id, from_entry, stop, limit, trail_atr)`,
`strategy.close(id, when)`, `color.*` constants, `print()` → stdout (shown in the terminal). Ship ≥ 8 example
scripts (EMA cross strategy, RSI divergence, Supertrend, Bollinger squeeze, MACD + volume, Ichimoku, VWAP bands,
"NEXUS-lite" mini ensemble). Scripts stored in table `scripts` (examples seeded on start, `is_example=1`).

---------------------------------------------------------------------------------------------------------------------
## 13. Frontend (React 18 + TS + Vite 5 + lightweight-charts 5)

Shell (LEAD): header (logo, `<SymbolSearch/>`, timeframe, WS status, clock), left nav, page area, bottom
`<Terminal/>` panel (resizable, `Ctrl+\``), toasts. Pages are default-exported components without props:
`pages/{ChartPage,MarketsPage,ScannerPage,SimulationPage,PaperPage,StudioPage,BrainPage,SettingsPage}.tsx`.
`components/terminal/Terminal.tsx` and `components/SymbolSearch.tsx` are default exports too.

Foundation modules (LEAD):
- `api/types.ts` (contract) · `api/client.ts` (`api.*` typed REST calls, throws `ApiError{status, message, data}`)
- `api/ws.ts` (`socket`: `connect()`, `on(type, fn) -> off`, `subscribe(topic) -> off` (ref-counted, resubscribed on
  reconnect), `command(text) -> Promise<CommandResult>`, `status`, `onStatus(fn)`; `marketTopic(symbol, tf)`)
- `store.ts` (zustand `useApp`: page, symbol, tf, openChart, terminal open/height, wsStatus, toasts/notify;
  `runUiAction(action)` applies `open_chart` / `navigate` / `notify` actions from command results)
- `lib/format.ts` (formatters + Spanish label maps + tones) · `lib/chart.ts` (lightweight-charts v5 helpers:
  `useChart()` hook, `createBaseChart`, `ts()`/`fromTs()` time conversion (ALWAYS use them), `toCandleData`,
  `toVolumeData`, `toLineData`, `toHistogramData`, `toMarkers`, `addCandles`, `addVolume`, `addLine`,
  `addSeriesForOutput` + `outputData` (indicator outputs), `MarkerLayer`, `LevelLines` + `positionLevels(pos)`
  ("SL · HASTA AQUÍ" price lines), `TradeLevelsLayer` (per-bar pos_* step lines), `ConeLayer`, `trimPanes`).
  v5 API: `chart.addSeries(LineSeries, opts, paneIndex)`, `createSeriesMarkers`, `chart.panes()`,
  `pane.setStretchFactor()`; never use v4 APIs (`addLineSeries`, `series.setMarkers`).
- `components/ui.tsx` (Button, IconButton, Card, Badge, Tabs, Select, TextInput, NumberInput, Toggle, Stat, Meter,
  Spinner, Empty, ErrorBox, Modal, VerdictBadge, ActionBadge, Disclaimer)
- `styles.css` (dark theme tokens: `--bg --panel --panel-2 --border --text --muted --accent --pos --neg --warn
  --info --purple`, utility classes `.row .col .grid .gap-* .muted .pos .neg .mono .scroll .page .card`).

Visual language: dark TradingView-like, dense but readable, green `--pos` = long/up/TOMAR, red `--neg` =
short/down/NO TOMAR, amber `--warn` = ESPERAR/advertencias. Every page handles loading, empty and error states.

---------------------------------------------------------------------------------------------------------------------
## 14. Cloud mode (hybrid: Vercel UI + Supabase Postgres/Auth + always-on backend container)

- **Database**: `app.db.open_database()` returns SQLite (`Database`) or, when `NEXUS_DATABASE_URL` is set,
  `app.db_pg.PostgresDatabase` (asyncpg pool, schema `NEXUS_DB_SCHEMA`=`nexus`, RLS on, `schema_pg.sql`).
  Same API. Module SQL stays in the SQLite dialect and is translated (`?`→`$n`, INSERT OR IGNORE/REPLACE →
  ON CONFLICT, IFNULL/INSTR/GROUP_CONCAT/ROUND(x,n)/scalar MAX-MIN/strftime(...,'unixepoch')/json_extract,
  LIKE→ILIKE, LIMIT -1); params are coerced to server types; NUMERIC → float; INSERTs into identity tables
  return the id. **Portability rules for module SQL**: no PRAGMA / sqlite_master / rowid / `SUM(<boolean
  expression>)` (use `SUM(CASE WHEN … THEN 1 ELSE 0 END)`), no type-mixing in comparisons, prefer explicit
  `ON CONFLICT(...) DO UPDATE` upserts, never touch `rt.db.conn` directly. Run the whole suite on Postgres with
  `NEXUS_TEST_PG_URL=postgresql://... pytest` (embedded server without Docker: `tools/pg_start.py`).
- **Auth**: `NEXUS_AUTH=supabase` → `app.auth.AuthMiddleware` requires a Supabase access token on every `/api`
  request (except `/api/health`, `/api/auth/config`, CORS preflight) and on `/ws?token=`; validated against
  Supabase `/auth/v1/user`, cached, restricted to `NEXUS_ALLOWED_EMAILS`. The server refuses to bind a public
  interface without auth. CORS / WS origins: `NEXUS_CORS_ORIGINS`.
- **Frontend**: `src/lib/config.ts` (`VITE_API_BASE`), `src/auth/session.ts` (GoTrue REST: password login,
  refresh, logout, multi-tab sync), `src/auth/AuthGate.tsx` (login screen when `/api/auth/config` says
  `supabase`). `api/client.ts` adds the bearer token (refresh + retry on 401); `api/ws.ts` adds `?token=`.
- **Deploy**: `Dockerfile` + `railway.json` / `fly.toml` (backend, 1 always-on instance, EU region),
  `frontend/vercel.json`, `.env.example`, step-by-step guide in `docs/DEPLOY.md`.
- **Retention**: `app.maintenance` prunes fine-grained candles, old events and resolved learning rows.

---------------------------------------------------------------------------------------------------------------------
## 15. Market conditions — "semáforo de mercado" (MACRO-BE / MACRO-FE)

Goal (user's words): not a screen full of data — only the alerts and data needed to know **when to trade and when
not** (Fed/FOMC, CPI, jobs, futures, volatility). Output = `MarketConditions` (types.ts): a level
`green` (Operar con normalidad) / `amber` (Precaución) / `red` (No operar ahora), one headline, one advice line,
≤ 3 reasons, a `blackout` window around high-impact events, the next relevant event with countdown, ≤ 5 upcoming
events and ≤ 6 metrics.

Sources (verified with plain requests from this network):
- Economic calendar: `https://nfs.faireconomy.media/ff_calendar_thisweek.json` (current week only; fields title,
  country (= currency), date (ISO with offset), impact High/Medium/Low/Holiday, forecast, previous). Refresh at most
  every 60 min, persist the last good copy in `model_state(kind='macro', key='calendar')`.
- FOMC (official federalreserve.gov schedule; decision 14:00 America/New_York on day 2, press conference 14:30):
  2026: Jan 27-28, Mar 17-18*, Apr 28-29, Jun 16-17*, Jul 28-29, Sep 15-16*, Oct 27-28, Dec 8-9*;
  2027 (tentative): Jan 26-27, Mar 16-17*, Apr 27-28, Jun 8-9*, Jul 27-28, Sep 14-15*, Oct 26-27, Dec 7-8*
  (* = with Summary of Economic Projections: more volatile).
- Quotes via `rt.hub.tv_scan("global", columns, tickers=[...])`: TVC:VIX, CME_MINI:ES1!, CME_MINI:NQ1!, TVC:DXY,
  TVC:US10Y, TVC:US02Y, TVC:MOVE, NYMEX:CL1!, COMEX:GC1! (columns close, change, change_abs, high, low).
- Crypto: Fear & Greed `https://api.alternative.me/fng/?limit=1` (60 min cache), OKX funding
  `https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` (10 min cache).

Rules (settings `macro.*`): **red** = high-impact event of a relevant currency within
[−block_after_min, +block_before_min] (blackout), or VIX ≥ vix_danger, or VIX intraday ≥ +25 %, or S&P futures
|Δ| ≥ 3 %. **amber** = high-impact event within warn_before_min (or ≤ 2 h after one), VIX ≥ vix_caution or
VIX Δ ≥ +12 %, S&P futures |Δ| ≥ 1.5 %, DXY |Δ| ≥ 0.8 %, US10Y Δ ≥ 10 pb, MOVE ≥ 120; crypto scope also
Fear & Greed ≤ 15 / ≥ 85 and extreme funding. Scope by symbol: crypto → USD events + crypto metrics; forex
EURUSD → EUR + USD events; US stocks / indices / commodities → USD; others → own currency + USD high impact.
No data at all → amber "Sin datos macro: opera con cautela" with `stale: true`.

Backend (`app/macro/*`, `app/api/macro.py`): `rt.macro` = `MacroService(rt)`; `await rt.macro.conditions(symbol=None)
-> MarketConditions`; `await rt.macro.calendar(days=7, impacts=("high","medium")) -> list[MacroEvent]`;
`await rt.macro.gate_check(symbol, direction) -> GateCheck | None` (id `macro`, label "Calendario y volatilidad",
critical when red) — merged into the Trade Gate by the NEXUS service. Alerts: level changes and T−60 / T−15 / T−0
minutes before high-impact events → bus events (source `macro`, level `warn`) + WS topic `macro`
(`{"type":"macro","conditions","alert"}`). REST: `GET /api/macro?symbol=`, `GET /api/macro/calendar?days=&impact=`.
Terminal: `macro [símbolo]` (alias `mercado`, `semaforo`), `calendario [días] [--all]` (alias `agenda`).

Frontend (`components/macro/*`): `MarketPulse` (default export, header pill: colored dot + label + countdown to
the next high-impact event; click → compact panel with headline, advice, reasons, ≤ 6 metrics, ≤ 5 events) and
`MacroBadge` (`symbol` prop, scoped conditions for the chart toolbar). Toasts for WS `macro` alerts.

---------------------------------------------------------------------------------------------------------------------
## 16. Verification checklist (what "done" means)

- `start.ps1` from a clean clone: creates venv, installs deps, builds UI, starts server, opens browser; DB created
  and seeded; services running; `/api/health` ok.
- Chart page shows BINANCE:BTCUSDT 1h with NEXUS markers, SL/TP lines, gate verdicts; indicators can be added.
- Terminal: `help`, `status`, `signal btc`, `check eth long`, `scan crypto`, `backtest btc 1h`, `sim start`.
- Backtest reports gated vs ungated metrics; no-lookahead tests pass; scripting sandbox blocks escapes.
