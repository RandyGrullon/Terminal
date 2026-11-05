# NEXUS Terminal — Architecture & Contracts (source of truth)

NEXUS Terminal is a **local-first trading research terminal**: it browses every TradingView asset, charts it
with any indicator, runs the self-learning **NEXUS** signal engine (buy / sell / hold / exit, SL/TP "hasta aquí"
lines, early-exit detection) and — above all — a **Trade Gate** that tells the user **when NOT to take a trade and
when to take it**. It has a PyChart (Python, Pine-like) script compiler, a live terminal, a market scanner with
predictions that are later scored (feedback loop), a market simulator (synthetic + replay), backtests, Monte Carlo
and paper trading (market, limit and stop demo orders under portfolio risk limits). It forwards the important events
to the user's own Telegram / e-mail (§16) and measures itself honestly: a walk-forward validation against random
entries and buy and hold (§17, `docs/VALIDATION.md`: so far **no evidence of an edge**) and a skill report of every
prediction against the reference (§10). Everything is persisted in a local SQLite DB created and migrated
automatically on start (PostgreSQL / Supabase in cloud mode, §14). CI runs every check on Linux on each push (§18).

JSON shapes: **`frontend/src/api/types.ts` is canonical** (feature types live in `frontend/src/api/types-<feature>.ts`,
re-exported by types.ts, §13). Python must emit exactly those shapes (snake_case).

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
7. **JSON hygiene**: no NaN/Infinity on the wire (use `app.util.clean_json`) nor accepted as input (settings →
   400, the market API's numeric query params → 400, user entry/SL/TP of the gate → 400, terminal numeric flags
   fall back to their default). Bar times = unix seconds; `ts`/`*_at` = unix milliseconds.
8. **Dependencies are fixed**: backend = fastapi, uvicorn[standard], httpx, numpy 2.x, pandas 3.x, aiosqlite,
   asyncpg, truststore (Python 3.13); tests = pytest. Frontend = react 18, lightweight-charts **5.x**,
   @uiw/react-codemirror + @codemirror/lang-python + @codemirror/theme-one-dark, zustand 4, lucide-react 0.460.
   Do not add packages.
   - `backend/requirements.txt`: ranges capped below the next major, including `starlette<2` and `websockets<18`
     (both imported directly). `backend/requirements-dev.txt` = runtime + pytest (the start scripts install it;
     never the image).
   - `backend/requirements.lock`: exact runtime pins for the Docker image, generated offline from `backend/.venv`
     by `tools/lock_requirements.py` (`--check` exits 1 when out of date); uvloop is deliberately not installed.
     The Dockerfile installs only the lock (`pip install --no-deps`), then runs `pip check`: a package missing from
     the lock fails the build instead of pulling an untested version.
   - Tests: the default suite checks that the lock is complete and within the ranges; the exact lock == venv
     comparison is opt-in (`NEXUS_CHECK_LOCK_ENV=1`, for image builds and dependency bumps; the CI docker job sets it).
   - The server always runs on the standard asyncio loop (`uvicorn.run(..., loop="asyncio")`), also on Linux where
     start.sh installs `uvicorn[standard]`, whose uvloop uvicorn would otherwise pick.
9. **Honesty**: no indicator predicts the future with certainty. Show probabilities, confidence and measured
   out-of-sample accuracy; never claim guaranteed profits. Include the disclaimer where results are shown. UI rules
   for measured evidence ("x de n", n ≥ 30, 95 % interval): §13. Hit rates are compared with the reference (three
   outcomes: 50 % is not chance) and the Brier score with climatology (§10); the product's edge with random entries
   (§17).
10. **Schema changes are migrations**: `app/schema.sql` / `app/schema_pg.sql` are the frozen baseline (v1; a test
    hashes their DDL). Every change is a new numbered, backward-compatible pair in `app/migrations/` (§5).
11. **Feature extensions**: a feature adds its default settings in `app/settings_ext/<feature>.py` (§4) and its
    response types in `frontend/src/api/types-<feature>.ts`, re-exported by types.ts (§13), instead of growing the
    shared files.
12. **Secrets** (provider keys, bot token, SMTP password) live in the `settings` table. Status, health, events, errors
    and the terminal never show them (masked and never logged: §3 `app.terminal`); only `GET /api/settings` returns
    them, to the settings form (Known limitations).

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
backend/app/auth.py db_pg.py hardening.py migrations/* settings_ext/__init__.py     [LEAD]
tools/contract_check.py lock_requirements.py pg_start.py fake_supabase.py           [LEAD]

backend/app/data/*  (all other files)  + app/api/market.py  + tests/test_data_*.py       [BE-DATA]
backend/app/engine/*                    + app/api/nexus.py   + tests/test_engine_*.py     [BE-ENGINE]
backend/app/sim/*   (all but synthetic) + app/api/sim.py     + tests/test_sim_*.py        [BE-SIM]
backend/app/scripting/*                 + app/api/scripts.py + tests/test_scripting_*.py  [BE-SCRIPT]
backend/app/macro/*                     + app/api/macro.py   + tests/test_macro_*.py      [MACRO-BE]
backend/app/alerts/*                    + app/api/alerts.py  + tests/test_imp_alerts_*    [ALERTS]
backend/app/validation/* + app/api/validation.py + docs/VALIDATION.md + docs/validation/  [VALIDATION]
backend/app/settings_ext/<feature>.py   (one DEFAULTS dict per feature, §4)            [feature owner]
.github/workflows/ci.yml  Dockerfile  .dockerignore  .gitattributes  tools/ci/*         [CI]

frontend/ index.html vite.config.ts tsconfig.json package.json                       [LEAD]
frontend/src/main.tsx App.tsx styles.css store.ts                                    [LEAD]
frontend/src/api/{types,client,ws}.ts  src/lib/{format,chart}.ts  src/components/ui.tsx   [LEAD]
frontend/src/api/types-<feature>.ts  (re-exported by types.ts, §13)                  [feature owner]
frontend/src/components/macro/**                                                     [MACRO-FE]

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
| `rt.macro` | `MacroService` | MACRO-BE | `app.macro.service` |
| `rt.nexus` | `NexusService` | BE-ENGINE | `app.engine.service` |
| `rt.scanner` | `ScannerService` | BE-SIM | `app.sim.scanner` |
| `rt.paper` | `PaperEngine` | BE-SIM | `app.sim.paper` |
| `rt.evaluator` | `PredictionEvaluator` | BE-SIM | `app.sim.evaluator` |
| `rt.sim` | `Simulator` | BE-SIM | `app.sim.simulator` |
| `rt.scripts` | `ScriptService` | BE-SCRIPT | `app.scripting.service` |
| `rt.maintenance` | `Maintenance` | LEAD | `app.maintenance` |
| `rt.alerts` | `AlertService` | ALERTS | `app.alerts.service` |
| `rt.validation` | `ValidationService` | VALIDATION | `app.validation.service` |

Also `rt.auth` (`app.auth.SupabaseVerifier`, cloud mode only), `rt.role` and `rt.leader` (below), and
`rt.hub.realtime` (`app.data.realtime.RealtimeQuotes`, owned by the hub, §6.2). `rt.alerts` only reacts to bus events
(no polling), so it also runs with `NEXUS_BACKGROUND=0`; like every service it runs only on the active instance
(§16). `rt.validation` is on demand (no loop): `start()` does nothing, `stop()` cancels a running job, `status()` =
`{running (a job is in progress), state idle|running|done|error|cancelled, job, defaults}` (§17).

**Service protocol**: `__init__(self, rt)` (no I/O), `async start()` (returns fast; spawn loops with
`asyncio.create_task`), `async stop()` (cancel tasks, close clients), `status() -> dict` (JSON-safe, must include
`"running": bool`). All services are constructed first (table order), then started in table order; stopped in
reverse. Loops: `try/except` per iteration, emit errors (rate-limited), `await asyncio.sleep(...)`.
Routers: `app.api.<module>` exposes `router = APIRouter()` (paths are absolute, e.g. `@router.get("/api/candles")`).
Terminal commands: `app.<pkg>.commands` registers commands at import time.
Failing imports are logged (`/api/health.load_errors`) and the rest keeps working.

**Startup** (`app.main.lifespan`): `app.hardening.harden_server_process()` first (§12), then `open_database()` +
`init_schema()` (baseline + migrations, §5), settings, WS manager, event bus, the leader lock (PostgreSQL with
background work), `bus.start()` (not on a standby). Then the start is logged (source `system`):
- 'Iniciando NEXUS Terminal v… · SQLite|PostgreSQL <path> (esquema vN) · <login>' plus, on Linux only,
  ' · servidor no volcable: sí|no';
- 'Base de datos preparada: esquema base y migraciones 0002_paper_orders, 0003_paper_fills_time (esquema v3)' when
  the base schema was created now, or 'Base de datos actualizada al esquema vN: …' when migrations ran now
  (`data.schema` = `rt.db.schema_info`);
- warn 'La base de datos tiene migraciones más nuevas que esta versión de NEXUS (vN): sigue funcionando, pero conviene
  actualizar NEXUS.' for versions this code does not know;
- Linux warn when the server could not be made non-dumpable: 'No se pudo marcar el proceso del servidor como no
  volcable (EPERM): otro proceso del mismo usuario podría leer su entorno y su memoria (/proc/<pid>/environ y mem).';
- the risky-setup warnings of cloud mode (§14), then the command modules, the services and 'NEXUS listo en http://… ·
  N/11 servicios'.

`main()` runs uvicorn with `loop="asyncio"` (§0.8), `proxy_headers` and `forwarded_allow_ips` =
`NEXUS_FORWARDED_ALLOW_IPS` (default 127.0.0.1; the image sets `*`).

**Roles** (`rt.role`): `single` (SQLite, or `config.BACKGROUND` off: the only instance), `leader` (holds the
PostgreSQL leader lock and runs the services) or `standby` (another instance holds it). `rt.leader` =
`app.main.Leadership`, created only with PostgreSQL + background work. In that mode the services are constructed and
started **only by the lock holder**, never on a standby (their `start()` does single-instance recovery, e.g. closing
the simulator's positions or marking running scans interrupted, which would hit the active instance). A standby
retries the lock every 3 s (`LEADER_RETRY_S`) and its bus only logs to the console (its event ids would clash with
the active instance's). **Take-over**: a fresh `SettingsStore` (reloaded from the DB), a fresh persisted `EventBus`,
freshly constructed services, then role `leader`. **Step-down** (the leader probes its lock connection every 5 s,
`LEADER_CHECK_S`, and the connection was lost): role `standby` first (the StandbyGate refuses new work), services
stopped and every `rt.<attr>` set to None, WS clients closed with 1013, lock released, bus flushed and replaced by a
console-only one. Lock, HTTP 503 and WS behaviour: §14.

---------------------------------------------------------------------------------------------------------------------
## 3. Foundation APIs (LEAD — import them, don't modify)

### `app.config`
`VERSION`, `ROOT_DIR`, `BACKEND_DIR`, `DATA_DIR`, `DB_PATH`, `FRONTEND_DIST`, `HOST`, `PORT`, `USER_AGENT`,
`DEFAULT_SETTINGS` (dict, see §4), `DEFAULT_WATCHLIST` (list of symbols), `OFFLINE` / `BACKGROUND` (§1).
Cloud mode (§14): `DATABASE_URL` (`NEXUS_DATABASE_URL`), `DB_SCHEMA` (`NEXUS_DB_SCHEMA`, default `nexus`),
`AUTH_MODE` (`NEXUS_AUTH` none|supabase), `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `ALLOWED_EMAILS`, `CORS_ORIGINS`,
`LOOPBACK_HOSTS`; explicit opt-ins for risky setups that the server otherwise refuses to start with:
`ALLOW_ANY_SUPABASE_USER` (`NEXUS_ALLOW_ANY_SUPABASE_USER=1`: no email allowlist) and `ALLOW_LOCAL_DB`
(`NEXUS_ALLOW_LOCAL_DB=1`: SQLite on a public bind, only with a persistent volume). `SCRIPT_MEMORY_MB`
(`NEXUS_SCRIPT_MEMORY_MB`, default 1024, clamped to 128..8192; invalid → default): memory cap of each PyChart
sandbox process (§12). `DEFAULT_SETTINGS` = the core dict updated at import with `app.settings_ext.EXTRA_DEFAULTS`
(§4). `CORS_ORIGINS` (`NEXUS_CORS_ORIGINS`) is also the allow-list of the CSRF origin check (§14).

### `app.hardening` (standard library only: runs before anything else is set up)
`harden_server_process()` (best effort, never raises; Linux: `prctl(PR_SET_DUMPABLE, 0)`; no-op on Windows / macOS)
and `server_protections() -> {platform, applied, non_dumpable[, error | reason]}` (§12).

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
Schema: `await rt.db.init_schema()` (once, in the lifespan) runs the frozen baseline (`app/schema.sql`, tables in §5)
and the numbered migrations (§5 Migrations) and keeps `rt.db.schema_info` = `{version, latest, baseline:
created|marked|present, applied: ['0002_paper_orders', …] (run now), unknown: [versions newer than this code]}`.
Never edit the baseline files: add a migration. JSON blobs are TEXT (`json.dumps`).

### `app.settings`
`rt.settings.get(key, default=None)`, `rt.settings.all()`, `await rt.settings.set(key, value)`,
`await rt.settings.update(dict)` (validates every value before writing any). Values coerced to the default's type;
unknown keys accepted only with `ui.` prefix (feature keys are known through `app.settings_ext`, §4). Numbers (and
numeric lists such as `nexus.tp_r`) reject booleans, NaN and ±inf; a dict default (`risk.fee_pct_by_class`) takes a
JSON object (or its string) with known keys only and finite numbers, missing keys keep their default value. Invalid →
`ValueError` (`PUT /api/settings` → 400). Beyond types the store checks nothing: feature code clamps or validates the
values it reads (e.g. `scanner.replay_days` 0..30; the alert channels report incomplete data in their status).

### `app.events`
`rt.bus.emit(level, source, message, data=None) -> dict` (sync; from threads use `emit_threadsafe`).
Levels: `debug info success warn error signal trade learn scan system`.
Event JSON: `{"id","ts","level","source","message","data"}`. Every event is persisted (table `events`), printed to
the server console and published on WS topic `events`. Messages: one line, Spanish, informative, e.g.
`"BINANCE:BTCUSDT 1h → COMPRAR · score 0.52 · p↑ 64% · Filtro: TOMAR 78/100"`.
`rt.bus.recent(limit=200, level=None, source=None)`; `rt.bus.add_listener(fn)` (sync fn(event), called after every
emit, errors ignored); `rt.bus.remove_listener(fn)` (undoes it; no error when fn is not registered). `rt.bus.running`
is True only between `start()` and `stop()` while the bus's loop is open: only then are events persisted and does
`emit_threadsafe` deliver (before `start()` it does nothing; once that loop is closed it raises RuntimeError). A bus
that was never started (standby, test runtime) keeps `emit()`'s events in memory and on the console only.

### `app.ws`
`rt.ws.publish(topic, message)` — non-blocking fan-out. Topics: `events sim paper scanner ui macro`
(default-subscribed; `paper` carries `{type 'paper'}` for the user's account 1 and `{type 'paper_autopilot'}` =
AutopilotSnapshot for NEXUS's own account 3) and `market:{SYMBOL}:{tf}` (e.g. `market:BINANCE:BTCUSDT:1h`) on demand. When a client
subscribes to a market topic, WSManager calls `rt.hub.subscribe(symbol, tf, owner="ws:<id>")`, and unsubscribes on
leave/disconnect. Protocol: see `ServerMessage` in types.ts; the server's first message is `hello`. Client → server:
`subscribe {topic}`, `unsubscribe {topic}`, `command {id, text}`, `ping`, and in cloud mode `auth {token}` (a
refreshed access token, `WSManager._reauth`): a token of the same user replaces the socket's token and expiry; a
rejected token or another user's closes the socket with 4401; Supabase unreachable keeps the current token (the
server never acknowledges it). Session checks of open sockets: §14. `rt.ws.close_all(code, reason)` disconnects
every client (step-down to standby).

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
`ctx.flag_int` / `ctx.flag_float` return the default for non-numeric, NaN or ±inf values (`--limit=inf`).
- **Credentials**: `is_secret_setting(key)` = the key's name contains key, token, password, passwd, secret,
  passphrase or credential (`providers.*_key`, `alerts.telegram_token`, `alerts.smtp_password`; `alerts.smtp_user`
  and `alerts.telegram_chat_id` stay visible). `settings` (aliases `ajustes`, `config`) shows a set credential as
  `••••••` (`SECRET_MASK`) in the listing, a single read and the change confirmation (an empty one shows empty), and
  a ValueError about one never repeats the value. `settings clave=valor` (one word) works like `settings clave valor`.
- **Command log**: every line (except `clear help echo`) is logged as the debug event `> <line> (N ms)`, and a command
  that raises as the error event 'Fallo en comando '<line>': …', both through `loggable_command()`, which never
  carries a credential: `> settings alerts.smtp_password ••••••`; a
  `settings` line that names a credential setting anywhere else (`config set alerts.smtp_password …`) is logged as
  `> config ••••••`. Other lines are logged as typed.

### `app.data.base`
```python
class CandleSet:                     # ascending, unique times; numpy float64 arrays (time is int64)
    symbol, tf, provider, history ("full"|"keyed"|"recorder"|"synthetic"|"none"), note
    stale                            # bool: newest bar far older than it should be (§6.2); never use it as live
    time, open, high, low, close, volume
    __len__, last_time, last_close, is_empty
    from_rows(rows, symbol, tf, provider="", history="full", note=None, stale=False)  # rows: dicts or (t,o,h,l,c,v)
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
risk.fee_pct_by_class {forex 0.005, index 0.01, futures 0.01, bond 0.01,
                       commodity 0.02, cfd 0.02, stock 0.02, fund 0.02}
nexus.entry_threshold 0.2 · nexus.exit_threshold 0.0 · nexus.horizon 5 · nexus.sl_atr 2.0
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
sim.default_speed 4
macro.enabled true · macro.alerts true · macro.block_before_min 30 · macro.block_after_min 30
macro.warn_before_min 120 · macro.vix_caution 22 · macro.vix_danger 30 · macro.currencies ["USD"]
ui.language "es" · ui.timezone "" · ui.chart_indicators []
# feature modules (app/settings_ext/<feature>.py)
alerts.macro true · alerts.signals true · alerts.paper true · alerts.min_level "warn" (info|warn|critical)
alerts.signal_symbols [] · alerts.max_per_hour 20 · alerts.quiet_enabled false · alerts.quiet_start "23:00"
alerts.quiet_end "07:00" · alerts.quiet_critical false · alerts.telegram_enabled false · alerts.telegram_token ""
alerts.telegram_chat_id "" · alerts.email_enabled false · alerts.smtp_host "" · alerts.smtp_port 587
alerts.smtp_security "starttls" (starttls|ssl|none) · alerts.smtp_user "" · alerts.smtp_password ""
alerts.email_from "" (empty = the SMTP user's address) · alerts.email_to "" (comma-separated)
paper.max_open_risk_pct 6.0 · paper.max_positions_per_group 3 · paper.crypto_one_group true
paper.daily_loss_limit_pct 3.0                                       (the three numeric limits: 0 = off)
autopilot.enabled false · autopilot.source "scanner" (scanner|list) · autopilot.symbols [] · autopilot.tf "1h"
autopilot.max_positions 5 (1..20) · autopilot.risk_pct 1.0 (0.1..5) · autopilot.scanner_top 10 (1..30)
realtime.enabled true · realtime.twelvedata_credits_min 8 · realtime.twelvedata_credits_day 800 (0 = no daily cap)
realtime.polygon_calls_min 5 · realtime.reserve_pct 25 · realtime.max_symbols 4 · realtime.min_interval_s 15
scanner.replay_days 7                     (int days, clamped 0..30 by the evaluator; 0 = vectors dropped at resolution)
validation.symbols ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:SOLUSDT", "BINANCE:XRPUSDT", "BINANCE:BNBUSDT"]
validation.tfs ["1h", "4h"] · validation.bars 3000 (1000..5000) · validation.folds 5 (2..10)
validation.warmup_bars 500 · validation.random_runs 1000
```
**Feature settings** (`app/settings_ext/`): each feature module (alerts, paper_risk, realtime, scanner_model,
validation) defines a plain `DEFAULTS` dict and must not import `app.config` (no cycles); `settings_ext/__init__.py`
merges them into `EXTRA_DEFAULTS`, and `config.DEFAULT_SETTINGS.update(EXTRA_DEFAULTS)` runs at import, so they are
ordinary known keys (typed, validated, listed by `settings`, `GET /api/settings`) without editing the core dict. A
new feature adds its module there and to both lists of `__init__.py`. Settings UI groups of these keys: 'Alertas'
(`schema-alerts.ts`), 'Riesgo de cartera' (`schema-paper-risk.ts`), 'Tiempo real' (`schema-realtime.ts`),
'Validación' (`schema-validation.ts`: symbols, tfs, bars; `folds`, `warmup_bars` and `random_runs` are deliberately not
in the form, they set how the edge is measured) and `scanner.replay_days` in 'Escáner' (§13).

**Trading costs** (`app.engine.costs`): fees are % **per side** (commission + spread). `asset_class(symbol)` →
`crypto | stock | fund | forex | commodity | index | futures | cfd | bond | other` (`app.data.symbols.asset_class_of`;
`SIM:*` counts as crypto). `fee_pct_for(symbol, settings)` = `risk.fee_pct_by_class[class]` when configured (negative
→ 0), otherwise `risk.fee_pct` (crypto, SIM:*, `other` and any class missing from the map): a single crypto-sized fee
would make the gate veto every low-volatility market. Used by NEXUS (`NexusService.params(symbol)`: gate R:R/EV and
trade R), backtests and Monte Carlo without an explicit fee, simulator replays and paper trading. Settings UI: kind
`fee_classes` (group Riesgo, 0–2 % per class, the classes the server returns); the `risk.fee_pct` help says it
applies to crypto and to classes without their own fee.

---------------------------------------------------------------------------------------------------------------------
## 5. Database (app/schema.sql + app/migrations)

`meta settings symbols candles watchlist model_state signals predictions prediction_daily gate_log scan_runs
scan_results paper_accounts paper_positions paper_fills events scripts backtests` (baseline, `schema.sql`) plus
`schema_migrations` and `paper_orders` (migrations, below) — see the files for columns (`schema_pg.sql` and the
`*.pg.sql` migrations are the PostgreSQL twins, §14).
- `candles(symbol, tf, time, open, high, low, close, volume, source)` PK (symbol, tf, time). Cache + recorder.
- `model_state(kind, key, state JSON, updates, trained_until, updated_at)` PK (kind, key). Kinds:
  - `nexus` (key `SYMBOL|tf`): `{version, snapshots: [{time, state, kind: grid|origin|last}], learning}` (§7 step 5);
    anchor states carry a `resume` block. Size at the default `nexus.horizon` 5: ~5–9 KB per anchor, a full row (60
    anchors + last) ~600 KB, rewritten on every analysis; the label queue grows with the horizon (horizon 200:
    anchors ~57 KB, rows ~3.5 MB, ~150 ms of JSON load + save per analysis). Deleted by `POST /api/nexus/reset` and
    after 90 days without updates;
  - `nexus_veto` (key `SYMBOL|tf`): entries the semáforo vetoed, `{version, entries: [{time, direction, level, detail,
    at}]}` (time = decision bar open, unix s; level red|amber; at = unix ms), newest 200 kept (§7);
  - `nexus_gate` (key `SYMBOL|tf`): bar-close gate decisions (§7), `{version, entries: [{time, direction, verdict,
    score, reason, summary, checks[13], fp, at}]}`, newest 600 kept; the records of the newest recorded bar also keep
    `gate` = the whole GateResult decided at that close (older ones only the digest). Both kinds survive restarts and
    `reset`; the retention deletes one only when it is 90 days old and its `nexus` memory is gone (§14);
  - `scanner` (key `market|tf|horizon_s`): ScannerModel v2 `{version: 2, key, dir, mag, cal_dir, cal_mag, updates,
    scans, skipped, trained_until, last_learned_ms, memory: {t, ctx, means}, recent}` (§10; a v1 OnlineLogit state is
    not reused: that model restarts from zero, its history stays in the statistics);
  - `gate` (key `global` = GateLearner, `check_stats` = per-check counts);
  - `macro` (keys `calendar` = last good weekly feed, `calendar_official` = last good BLS / BEA calendars `{version,
    sources: {bls|bea: {events, kinds, until, fetched_at}}}`, §15);
  - `alerts` (key `state`, §16): `{version: 1, since, counters, recent (≤ 30), last (per channel), last_test, policy:
    {seen, sent, omitted, latest}}`;
  - `validation` (key `latest`, §17): the last ValidationReport (≈ 190 KB for 20 series).
  The retention never touches `scanner`, `gate`, `macro`, `alerts` or `validation` rows.
- `predictions` are written at creation time and resolved later by the evaluator → honest out-of-sample stats.
  Indexes `idx_pred_due (resolved, target_at)`, `idx_pred_sym (symbol, created_at)`, `idx_pred_source (source,
  resolved, target_at)`, `idx_pred_model (model_key, resolved, target_at)`. `features`: scanner rows written by a
  scan are packed v4, `{"v":4,"f":FEATURE_SIG,"g":EXTRA_SIG,"s":CONTEXT_SIG,"q":"<base64 little-endian int16>"}`:
  values ×10 000 (4 decimals), −32768 = missing, in the order FEATURE_NAMES (37), EXTRA_NAMES (vol_d_log, atr_log,
  bbw_log, liq_log), CONTEXT_NAMES (12: ctx_breadth_1h/4h/1d, ctx_mean_4h, ctx_dispersion, ctx_majors_trend/mom/vol,
  reg_trend_up/trend_down/range/volatile); ≈ 0.2 KB per row, a part whose signature does not match is ignored; v2/v3
  JSON lists (`{"v":2,"f":…,"x":[…]}`) and legacy v1 `{"x": {name: value}}` rows are still read. Resolved scanner
  rows keep their vector for `scanner.replay_days` (offline replay, §10): the evaluator drops older vectors hourly,
  and an expired row's at once. NEXUS rows hold `{bar_time, regime}`.
- `prediction_daily(day, source, model_key, bin, n, scored, correct, up, prob_sum, brier_sum)` PK (day, source,
  model_key, bin): per-UTC-day aggregates of resolved predictions (day = UTC day of target_at, unix s; model_key =
  the scanner's `market|tf|horizon_s` or NEXUS `SYMBOL|tf`; bin = calibration bin of prob_up, 10 bins, the same
  thresholds as the SQL ladder). `n` counts resolved rows with or without a verdict, the other counters only rows
  with one. Written by the evaluator in the same transaction that resolves the rows; a one-time backfill (meta key
  `prediction_daily` = '1') folds in rows resolved earlier. Global stats read it, so they survive the pruning (§14).
- `gate_log` stores every gate decision (verdict + checks, `source` watch | scanner | paper | sim | manual) and its
  later outcome (TP1 | SL | NONE, `outcome_r`) → the gate learns which checks work. `target_at` convention: §7
  Watcher; outcome window: §10.
- `scan_results.data` holds only the ScanRow fields that have no column of their own (None values omitted);
  `ScannerService.results()` rebuilds the full row (older rows that stored the whole row still read).
- `paper_accounts`: id 1 = "Principal", id 2 = "Simulación" (created on start if missing).
- `paper_orders` (migration 0002): pending limit / stop entries of the demo account (§11). Columns: `id, account_id,
  symbol, tf, side (long|short), kind (limit|stop), price, qty (NULL = sized at the fill), risk_pct, sl, tp (NULL =
  NEXUS levels relative to the fill), expiry (gtc|bars|date), expire_bars, expire_time (unix s), status
  (pending|filled|cancelled|expired), origin, gate_verdict, gate_score (at placement), position_id, fill_price,
  fill_time (unix s), reason (Spanish), cancelled_by (user|gate|risk|levels|system), created_at, updated_at (ms),
  meta (JSON)`; index `idx_orders_status (account_id, status)`.
- `meta(key, value)` keys: `schema_version` (latest applied migration, informational), `watchlist_seeded`,
  `prediction_daily` (backfill done), `paper.day_low.<account>` = `{day, low}` (the UTC day's worst realized P&L for
  the daily loss limit, §11; removed by a reset of that account).

**Migrations** (`app.migrations`, applied by `init_schema` at every start):
- Version 1 = the **baseline**: `schema.sql` / `schema_pg.sql` as they were when versioning started, **frozen**
  (`tests/test_imp_paper_db_verify.py::test_the_baseline_schema_files_stay_frozen` hashes their DDL). Every later
  change is a numbered pair in `backend/app/migrations/`, `NNNN_name.sqlite.sql` + `NNNN_name.pg.sql` (same number
  and name, contiguous from 0002), written natively for each engine (module SQL elsewhere stays in the SQLite dialect);
  PostgreSQL files may use `{schema}` and run with the search_path set to it; SQLite files are split with
  `sqlite3.complete_statement` (`--` comments allowed). Tables they create go into `db_pg.TABLE_PKS` /
  `IDENTITY_TABLES` (a test guards it).
- `schema_migrations(version PK, name, applied_at ms)`. A database without a baseline row that already has every
  baseline table and index (made by an earlier NEXUS) is only *marked*, no DDL; a fresh one, or an old one missing
  part of the baseline, runs the idempotent baseline script (`schema_info.baseline` = created | marked | present).
- Pending migrations run in version order, each followed by its row, all in ONE transaction (`BEGIN IMMEDIATE` on
  SQLite; inside the `pg_advisory_xact_lock('schema')` that `PostgresDatabase.init_schema` takes first), so instances
  starting together never apply one twice: a failure rolls everything back and aborts the start (`MigrationError`
  names the migration). Nothing pending → no DDL at all (no table lock next to a live instance during a deploy). No
  `CREATE INDEX CONCURRENTLY` (it cannot run in a transaction): keep migrations small.
- A database migrated by a newer NEXUS keeps working: unknown versions are reported (`schema_info.unknown`, a warn at
  start, §2), never undone. Hence migrations must stay **backward compatible** (new tables, nullable columns,
  indexes): during a deploy the previous version may still run against the migrated database (it ignores
  `paper_orders`; restarting, it writes `meta.schema_version` = '1' back, which is informational only:
  `schema_migrations` is authoritative). On PostgreSQL the RLS pass `app/migrations/pg_rls.sql` runs after the
  migrations at every start (§14).
- Current: `0002_paper_orders` (table + `idx_orders_status`), `0003_paper_fills_time` (`idx_fills_time` on
  `paper_fills (account_id, time)`, for the daily loss limit; it briefly takes a SHARE lock on PostgreSQL).
  `GET /api/health` reports `db.schema_version` (§6.1). How to add one: `tests/test_imp_paper_db_migrations.py`.

---------------------------------------------------------------------------------------------------------------------
## 6. REST API (all under /api; errors → `{"detail": "<español>"}`)

Status codes: 400 invalid input (unknown setting / invalid value, bad symbol or tf, NaN/±inf numbers), 403 a change
(POST / PUT / PATCH / DELETE) sent by a page of another origin ('Origen no permitido (…)…', CSRF check, §14), 404 not
found, 409 conflict (gate NO TOMAR on a paper order, a portfolio-risk limit of the demo account (never forced),
simulator state, a validation job, an alert test or a scanner replay already running), 415 `POST /api/alerts/test`
without a JSON body, 422 FastAPI validation ('Parámetros no válidos — …') and NEXUS data that cannot be analysed
(`NexusDataError`: history too short or stale data, e.g. `/api/gate/check`), 429 a second alert test within 5 s, 500
internal, 502 provider failure, 503 module not loaded — and in cloud mode the transient 503s 'No se pudo verificar tu
sesión…' (Supabase unreachable) and 'Instancia en espera…' (standby), both with `Retry-After: 5` (§14) —, 504 NEXUS
analysis timeout. 401 only in cloud mode (§14).

### 6.1 System [LEAD]
`GET /api/health` → Health (includes `role`: single | leader | standby, and `db.schema_version` = the database's
migration version (§5; null before `init_schema`); in cloud mode without a session only the
liveness view `{ok, version, auth: "required", role, server_time}`) · `GET /api/auth/config` → `{mode,
supabase_url, supabase_anon_key}` (public) · `GET /api/settings` → `{settings}` (credentials included, in clear, for
the settings form) · `PUT /api/settings` body
`{key: value}` → `{settings}` (400 on an unknown key or invalid value, nothing written) · `GET
/api/events?limit&level&source` → `{events}` · `GET /api/terminal/commands` → `{commands}` · `POST /api/terminal
{text}` → CommandResult · `GET /api/db/stats` → DbStats · `POST /api/db/vacuum`.

### 6.2 Market [BE-DATA]
```
GET    /api/markets                                    -> {markets: MarketInfo[]}
GET    /api/symbols/search?q=&market=&limit=30          -> {results: SearchResult[]}
GET    /api/universe?market=&offset=&limit=&sort=&order=&q=&type=  -> UniverseResponse
GET    /api/candles?symbol=&tf=&limit=                  -> CandlesResponse
GET    /api/quotes?symbols=A,B                          -> {quotes: {symbol: Quote}}
GET    /api/symbol?symbol=                              -> SymbolDetail
GET    /api/realtime                                    -> RealtimeStatus (types-realtime.ts; 503 without it)
GET    /api/watchlist                                   -> {items: WatchItem[]}
POST   /api/watchlist {symbol}                          -> {items}
DELETE /api/watchlist?symbol=                           -> {items}
```
Numeric query params (`limit`, `offset`) reject NaN/±inf with 400 "El parámetro 'x' debe ser un número entero".
`/api/candles` adds `"stale": true` only for a stale set (below); `provider` = real source of the newest rows
(`candles.source`, or the venue stored in `symbols`).

Hub python API (used by other modules):
```python
await rt.hub.get_candles(symbol, tf, limit=500) -> CandleSet   # empty set + note when no data (never raises for that)
await rt.hub.search(q, market=None, limit=30) -> list[dict]
await rt.hub.resolve(text) -> str | None                       # "btc"->"BINANCE:BTCUSDT", "aapl"->"NASDAQ:AAPL"
await rt.hub.symbol_info(symbol) -> dict                       # SymbolInfo
await rt.hub.quotes(symbols) -> dict[str, dict]                # Quote by symbol (source, realtime, delay_s, ...)
rt.hub.realtime                # RealtimeQuotes: quote(symbol), select(), status(), budget(provider)
await rt.hub.last_price(symbol, *, allow_stale=True) -> float | None  # False: None when only an old cached price
await rt.hub.universe(market, offset=0, limit=100, sort="volume", order="desc", q=None, type_=None) -> dict
await rt.hub.tv_scan(market, columns, *, filters=None, sort_by=None, order="desc", limit=300, offset=0,
                     tickers=None) -> {"total": int, "rows": [{"symbol": str, <column>: value, ...}]}
rt.hub.subscribe(symbol, tf, owner) ; rt.hub.unsubscribe(symbol, tf, owner)   # ref-counted live feed
rt.hub.add_listener(cb)        # async cb(symbol, tf, candle: dict, closed: bool) — called on every live update
await rt.hub.push_candle(symbol, tf, candle, closed)          # simulator feeds SIM:* bars through the hub
await rt.hub.watchlist() -> list[str]
```
Freshness (I2) and data rules:
- **Stale candle sets** (`CandleSet.stale`): the newest bar is more than 3 bars behind AND it ended more than 1 h ago
  (crypto, 24/7) or more than 6 days ago (markets with sessions). The note starts with "Datos desactualizados:
  última vela hace N días|horas|minutos". Consumers never present or use such a set as live (NEXUS ok=false §7,
  scanner skip §10, evaluator §10).
- **Quotes** (optional `source` binance | okx | coinbase | kraken | kucoin | twelvedata | polygon | alphavantage |
  tradingview | cache, `realtime`, `delay_s`, `market_open`; SIM quotes keep exactly their 9 keys). Priority: the
  exchange's live price (crypto followed live, ≤ 15 s old) > a fresh real-time quote of an API-key provider
  (`hub.realtime`) > TradingView > the local live feed / DB cache. `last_price`: live feed > keyed real-time quote >
  TradingView > exchange > cache.
  - `stale: true` only from the local fallback, when the price is older than 2 min, with `ts` = the real time of the
    price (a live update; the end of a final cached bar; only the open of the newest stored bar of a timeframe, which
    may have been stored while forming).
  - TradingView quotes keep `ts` = when TradingView returned them and carry `delay_s` when it announces delayed data
    (`update_mode`, e.g. 900 s for US stocks without a real-time feed), `realtime: true` for 'streaming' data and
    `market_open: false` outside the regular session of US stocks (9:30–16:00 New York, weekdays) and forex (Sunday
    17:00 → Friday 17:00 New York); holidays are not known and a guess never claims 'open'. Never flagged stale.
  - Keyed quotes: `ts` = the provider's time of the price (Twelve Data `last_quote_at`, minute precision);
    `realtime` only while the market is open and the price is ≤ 180 s old (`REALTIME_MAX_AGE_S`), otherwise the
    measured `delay_s`; a closed market gives `market_open: false` (its last price, no real-time claim). A delayed
    keyed price is never preferred over TradingView.
- **Real-time quotes** (`app.data.realtime`, `hub.realtime`, only with a key in Ajustes; plans per the providers'
  public docs, checked Oct 2026): Twelve Data Basic (free) = real time for US equities / ETFs, forex and crypto, 8
  credits/min and 800/day, `/quote` 1 credit per symbol (also in a batch); Polygon (now Massive): stock snapshots not in
  Basic, 15 min delayed on Starter / Developer, real time on Advanced / Business, currency snapshots real time from
  Currencies Starter (still called on api.polygon.io); Alpha Vantage free (25/day, end of day) is never used live.
  - Polled: only symbols of WS chart subscriptions and `paper*` owners (demo positions first), at most
    `realtime.max_symbols`, US stocks / funds / indices and forex / metals only. Interval = max(`min_interval_s`,
    60·cost / (per-minute limit − reserve), remaining open seconds today · cost / quote credits left); quote credits
    per day are capped at (1 − `reserve_pct`) of the plan, always leaving 20 for candles; candle downloads spend the
    same budget and `due()` counts the credits already used this minute; Polygon costs one call per market. Closed
    markets are re-checked every 30 min, and right after the guessed open (a closed quote stops serving once the
    guessed session opened).
  - Errors: 401 → 10 min pause; rate limit → until the next minute, 15 min after a second in a row; daily limit →
    00:00 UTC; 403 / NOT_AUTHORIZED → that market blocked 6 h; a price ≥ 10 min old while open → that symbol blocked
    1 h (TradingView for it); per-symbol batch errors → that symbol blocked 6 h. The key always travels in a header.
    Credit counters are per process.
  - `GET /api/realtime` = RealtimeStatus `{enabled, offline, providers: [{id, label, configured, live, plan, doc_url,
    state sin_clave|sin_tiempo_real|en_espera|activo|limite|plan|error, detail, credits_min, used_min, credits_day,
    used_day}], symbols: [{symbol, provider, interval_s, last_at, ts, realtime, delay_s, market_open, next_in_s}],
    polls, errors, last_error, last_poll_at}`. Terminal: `realtime` (aliases `tiemporeal`, `tiempo-real`, `rt`);
    `quote` has an 'Origen' column, `providers` shows the real-time state, `recorder` the estimated lag.
- **Exchange errors**: only definitive answers from every venue (not_found / unsupported / invalid / no bars) send a
  crypto symbol to the recorder path (10 min). Transient errors (network, rate limit, 5xx, timeout, unknown) return
  an empty set with "Error temporal al consultar los exchanges (…); vuelve a intentarlo en unos segundos" and a
  30 s backoff per (symbol, tf); "no data" answers with a cache get a 5 min backoff. Holes inside cached crypto
  windows are refetched from the exchange (at most every 30 min per key). After a restart a symbol whose own
  exchange is supported starts on that exchange again; the venue stored in `symbols` is used only for
  equivalent-pair routes (e.g. HTX:*).
- **Live feed**: one task per crypto (symbol, tf) (keys still in flight are skipped) plus one for the TradingView
  snapshots; polls use `fetch_latest(retries=0, timeout=5 s)` with an 8 s cap per key. After 5 consecutive failures
  the symbol's live updates come from TradingView for 5 min (candles still from the exchanges; those snapshots are
  not stored). The first poll stores the closed bar only when the cache already reaches that bar, otherwise
  `get_candles(..., 1000)` downloads the gap; after a stall the missed bars (up to 360) are downloaded and emitted
  `closed=True`, oldest first, then the forming bar.
  - Keyed symbols (API-key providers) served in real time skip the TradingView snapshots, but only while a fresh
    real-time quote exists or a poll of it is in flight. Their forming bar uses the provider's alignment
    (`_keyed_stamp`: the stored provider bar of the bucket or the bar shown, widened with each price, volume from
    day-volume deltas; 1d from the quote's day values) and is emitted closed once at the end of its bucket (then
    `_keyed_refresh` downloads the provider's bar). A closed market or a delayed price never opens a bar. WS `candle`
    messages carry the optional `source` / `realtime` / `delay_s` of the price.
- **Recorder** (`app.data.recorder`): `RECORDER_TFS` = 1m 5m 15m 1d; 1h/2h/4h/6h/12h are rebuilt from 15m, 3m from 1m,
  30m from 15m, 1w from 1d (TradingView's 1h/4h bars follow each exchange's session, e.g. 13:30 UTC for US stocks).
  Snapshots are stamped in market time: now − TradingView's announced delay (600–900 s for NASDAQ/NYSE/CME); rows
  of `current_bars()` carry `asof`. They are placed by `app.data.stamping.BarStamper`, shared by the recorder and the
  hub's snapshot feed for 1m/5m/15m/1d, by content first:
  - the bar already followed (same open, volume never lower, high/low only widen) keeps its bucket however late it
    arrives: no phantom bars after the close, frozen bars or a restart while the market is closed;
  - a new TradingView bar (open change, volume reset, narrower range) goes to the next bucket for ≤ 15 min
    timeframes; after a gap the clock corrected by the estimated lag J decides. 1d keeps the clock rule: an early
    rollover (< 90 s) replaces the stored bar, a later one is dropped (e.g. the forex day at 17:00 New York,
    `status().skipped_rollover`);
  - J per symbol = the value covered by most rollover intervals (a_prev − B, a − B] of 5m/15m bars, used once ≥ 3
    agree (≥ 60 %), 0 until then (1m rollovers pin J only modulo 60 s);
  - runs of bars placed after an uncertain start (first snapshot, gap) that J shows to be whole buckets off are
    moved in the DB in one transaction (their own coarse rollovers, the agreed lag for 1m, a new bar right after our
    bucket opened = run one late, a clock jump after a bar that lasted one bucket = run one early; up to 3 earlier
    runs per (symbol, tf) are fixed too). A move onto another stored bar is refused (`_relocate` returns −1);
  - guards: an earlier state of the followed bar (same open, volume not higher, range inside) seen within 15 s, or
    within min(tf/2, 90 s) while it keeps ≥ 50 % of the volume, is dropped (the recorder and the live feed poll
    independently, so answers come back out of order); a run whose rollovers agreed with the lag (coarse: 2
    rollovers; 1m: within 0.25 bucket) is confirmed and never moved again (a later lag change such as a PC clock
    correction only affects new bars); runs started with the lag known are not moved by gaps or late 1m rollovers.
  - Late copies of an earlier bar merge into it; a replaced stored bar is overwritten by its first writer; the seed
    uses recorder rows only. `status()` adds `lag_s`, `corrections`, `relocated`.
- **Keyed series** (API-key providers): TradingView snapshots are published only, never stored (stamped at the
  provider's session offset, e.g. :30 for US stocks at 1h); a live close invalidates the keyed cache; the first
  successful provider fetch of a keyed (symbol, tf) removes that series' rows from other sources.

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
res = run_nexus(cs, NexusParams(...), prior=None, gate_state=None, vetoed_entries=None,
                live_gates=None)                     # -> NexusResult (§7)
res.to_response(cs) -> dict (NexusResponse minus provider/note/quick which the service fills)
res.current, res.trades, res.markers, res.bars, res.prediction, res.learning, res.stats, res.snapshots
res.origin, res.live_gates                           # cold-start anchor; bar-close gate decisions (§7)
NexusParams.from_settings(rt.settings.all(), **overrides)
from app.engine.nexus_resume import resume_fit, path_fingerprint, RESUME_VERSION   # exact resume (§7 step 5)
from app.engine.features import compute_features, feature_state, restore_point     # compute_features(cs, resume=None)
from app.engine.gate import evaluate_gate            # see §8
from app.engine.costs import asset_class, fee_pct_for  # fee per asset class, see §4
from app.engine import ta                            # numpy TA functions (causal)
from app.engine.indicators import CATALOG, compute_indicator(id, cs, params) -> dict[str, np.ndarray]
await rt.nexus.analyze(symbol, tf, limit=None, *, upto=None) -> dict   # NexusResponse (anchored window, cache: §7)
await rt.nexus.current(symbol, tf, *, upto=None) -> dict | None        # NexusCurrent
await rt.nexus.process_closed_bar(symbol, tf, upto=None) -> dict | None   # watcher step (§7)
await rt.nexus.gate(symbol, tf, direction, entry=None, sl=None, tp=None) -> dict   # GateResult
rt.nexus.params(symbol) -> NexusParams               # settings + fee_pct = fee_pct_for(symbol, settings)
```
`upto` = open time (unix s) of the last CLOSED bar to use: later bars are dropped (bar-close decisions of the
watcher and the auto-trader). `/api/nexus` with stale data answers 200 with `ok=false` (§7); `/api/gate/check`
answers 422 for data that cannot be analysed (stale or too short) and 400 for invalid entry/SL/TP.

### 6.4 Simulation, Paper, Scanner, Learning [BE-SIM]
```
POST /api/backtest {symbol, tf, limit=2000, params={}, initial_capital=10000, risk_pct=1, fee_pct?=null,
                    slippage_pct=0.02, use_gate=true}                                  -> BacktestResult
POST /api/montecarlo {mode: bootstrap|synthetic, symbol?, tf?, paths=300, bars=500, seed=1,
                      initial_capital=10000}                                            -> MonteCarloResult
POST /api/sim/start SimStartRequest  -> {state}     POST /api/sim/stop -> {state}
POST /api/sim/config {speed?, regime?, paused?, auto_trade?} -> {state}
GET  /api/sim/state  -> SimSnapshot
GET  /api/paper/account                     -> PaperSnapshot (account 1)
POST /api/paper/order {symbol, side: long|short, tf?, qty?, risk_pct?, sl?, tp?, force?}
     -> {position, gate}; if gate verdict is AVOID and not force -> HTTP 409 detail {message, gate}
GET  /api/paper/orders?status=pending|all&limit=50   -> {orders: PaperOrder[]}  (all = + the latest closed ones)
POST /api/paper/orders {symbol, side, kind: limit|stop, price, tf?, qty?, risk_pct?, sl?, tp?,
                        expiry?: gtc|bars|date, expire_bars?, expire_time? (unix s), force?} -> {order: PaperOrder, gate}
POST /api/paper/orders/cancel {order_id? | symbol? | all?} -> {cancelled: PaperOrder[]}
GET  /api/paper/risk                        -> PaperRisk (account 1)
POST /api/paper/close {position_id? | symbol? | all?} -> {closed: Position[]}
POST /api/paper/reset {initial_balance?} -> PaperSnapshot
POST /api/paper/auto {enabled, symbols?, tf?} -> {auto: {enabled, symbols, tf}}
GET  /api/paper/autopilot                   -> AutopilotSnapshot (account 3, "NEXUS automática")
POST /api/paper/autopilot {enabled?, source?: scanner|list, symbols?, tf?, max_positions?, risk_pct?, scanner_top?}
     -> AutopilotSnapshot (only the given fields change; 400 {detail} in Spanish on a bad value)
POST /api/paper/autopilot/reset {initial_balance?}     -> AutopilotSnapshot (account 3 only)
POST /api/paper/autopilot/close {position_id? | all?}  -> AutopilotSnapshot (400 when neither; 404 unknown id)
POST /api/scanner/run {market, tf, limit, deep, deep_top} -> {run_id}
GET  /api/scanner/results?run_id=        -> {run: ScanRun | null, results: ScanRow[]}   (default: latest)
GET  /api/scanner/runs?limit=20          -> {runs: ScanRun[]}
GET  /api/scanner/quick?symbol=&tf=      -> QuickSignal
GET  /api/predictions?source=&symbol=&resolved=&limit= -> {predictions: PredictionRow[], stats: PredictionStats}
GET  /api/learning/summary               -> LearningSummary
GET  /api/learning/skill?source=scanner|nexus&days=7&model_key=  -> SkillReport
GET  /api/learning/replay?days=&model_key=                       -> scanner replay report (no TS type yet)
```
- **Demo orders** (types in `types-paper-orders.ts`; rules: §11): `POST /api/paper/orders` answers 400 for a price on
  the wrong side of the market, invalid levels or expiry, non-finite numbers or no price, 404 for an unknown symbol,
  409 `{message, gate}` when the gate (asked at the order price) says NO TOMAR unless `force`. Both order endpoints
  answer 409 `{message, code: 'risk_limit', rule: daily_loss|open_risk|group, risk: PaperRisk}` for a portfolio-risk
  limit, **never forced**. Cancel: 400 when nothing is specified, 404 for an unknown id, no pending order of the
  symbol, or an order that stopped being pending while the request waited. The WS `paper` message gains optional
  `orders` (pending) and `risk`; `GET /api/paper/account` is unchanged.
- **NEXUS's own demo account** (types in `types-autopilot.ts`; rules: §11): `AutopilotSnapshot {account:
  PaperAccount (id 3), positions, history: Position[], risk: PaperRisk | null, autopilot: AutopilotStatus}`;
  `AutopilotStatus {enabled, account_id: 3, source, tf, symbols, max_positions, risk_pct, scanner_top, watching:
  AutopilotWatch[] {symbol, why: escáner|lista|posición abierta, direction?, last?: AutopilotLast {bar_time (ms, open
  time of the last decided bar), tf, action (null = no analysis), label} | null}, scan: AutopilotScan {id, finished_at
  (null while running; a failed scan is no source), opportunities} | null, log: AutopilotLogEntry[] {ts (ms), symbol ('—' = not about one symbol), kind:
  open|close|skip|info, text} (newest first, ≤ 30, in memory: emptied by a restart), note}`. 400 texts: 'Fuente no
  válida: usa «scanner» (escáner) o «list» (lista fija)', 'El máximo de posiciones debe ser un número entero entre 1
  y 20', 'El riesgo por operación (%) debe ser un número entre 0,1 y 5,0', 'El número de candidatos del escáner debe
  ser un número entero entre 1 y 30', 'Como máximo 30 símbolos en la lista', an invalid tf. `Position.origin` gains
  'autopilot'. The user can only switch it on/off, configure it, close its positions and reset it: no orders are
  placed by hand in account 3.
- **Skill report** (`app.sim.evaluator.skill_report`, §10): `days` ≤ 0 = every stored row (the UI's 'Todo lo
  guardado' sends 0); a `source` other than empty / scanner / nexus → 400 'Fuente no válida: usa scanner o nexus', a
  non-finite `days` → 400, an unparsable one → 422; `model_key` is trimmed. Cached 60 s (600 s for longer windows or
  all rows), recomputed when new outcomes arrive. `SkillReport {generated_at (ms), days, since (ms, first target time
  covered; null = all), min_windows: 30, groups: SkillGroup[], notes}`; `SkillGroup {id 'scanner:<key>' | 'scanner' |
  'nexus' | 'all', label, source, model_key, n, hits, accuracy, windows, horizon, window, outcomes {up, down, flat},
  reference {kind, label, hits, accuracy}, chance, skill_pp, skill_ci, brier, brier_ref, bss, bss_ci, ece_pp,
  reliability, resolution, by_direction [{direction, label, n, hits, accuracy, base_rate}], daily [{time, n,
  hit_rate, reference, reference_kind, brier, brier_ref}], verdict none|insufficient|no_edge|better|worse,
  verdict_text, brier_text}` (canonical in types.ts).
- **Replay** (`replay_report`, §10): `{generated_at, days, since, min_windows, note, key, rows, scans, with_context,
  horizon, truncated, designs: SkillGroup[] (v2, v1, recorded), comparison {n, windows, diff_pp, diff_ci, brier_diff,
  brier_ci, verdict, text}}`. Default period = the setting `scanner.replay_days` (0 or missing → 7, like the terminal),
  clamped by the evaluator to 1 h..30 days; ≈ 15 s of CPU per week of data in a worker thread; one at a time per
  database: 409 'Ya hay una repetición del escáner en curso: espera a que termine.'
- `LearningSummary.scanner_model.features` lists 50 weights: 37 asset features (relative to the scan's mean),
  `beta_x_mkt` and the 12 market-context weights (same item shape).
- Backtest commission: `fee_pct` omitted/null = `fee_pct_for(symbol, settings)` (`risk.fee_pct_by_class`, falling
  back to `risk.fee_pct`); an explicit value wins (0 is valid; negative → 400). `result.params.fee_pct` is the fee
  used and `params.notes` gains 'Comisión X % por lado: la de <clase> en Ajustes.' (or '… (indicada en la
  petición).'); the engine's gate uses the same cost. Monte Carlo resolves the fee the same way (`SIM:*` → crypto).
- `BacktestMetrics.rejected_resolved` / `NexusStats.rejected_resolved` = rejected signals with a known outcome (the
  denominator of `rejected_would_win_rate`); `gate_lift_pct` is in percentage points.
- `LearningSummary.gate` gains `avoided_judged` / `taken_judged` (denominators of `avoided_would_lose_rate` = no TP1
  before the SL, a loss or a time stop, and of `taken_win_rate`).
- `ScanRow` gains optional `nexus_action` (NEXUS action at the last bar) and `opportunity` (§10; null on rows stored
  before the field). `QuickSignal.stale?` = read computed from outdated candles; `QuickSignal.time` = wall-clock time
  the read was computed (not a bar time).

Python API: `await rt.scanner.quick_signal(symbol, tf) -> dict | None` (QuickSignal);
`from app.sim.backtest import run_backtest`; `rt.paper.snapshot(account_id=1)`; `rt.paper.place_order(...)`,
`cancel_orders(order_id, symbol, all, account_id=1)`, `list_orders(account_id, status, limit)`, `pending_orders`,
`risk_state(1)` (PaperRisk); `autopilot_snapshot()`, `set_autopilot(enabled=, source=, symbols=, tf=, max_positions=,
risk_pct=, scanner_top=)`, `close_autopilot(position_id, all)`, `reset(..., account_id=3)`; `rt.sim.state()`; `from app.sim.evaluator import skill_report, replay_report, ReplayBusy`.

### 6.5 PyChart scripts [BE-SCRIPT]
```
GET /api/scripts -> {scripts: ScriptMeta[]}      GET /api/scripts/{id} -> Script
POST /api/scripts {name, code, description?} -> Script     PUT /api/scripts/{id} -> Script
DELETE /api/scripts/{id} -> {ok}
POST /api/scripts/compile {code} -> CompileResult
POST /api/scripts/run {code, symbol, tf, limit=1000, inputs={}} -> ScriptRunResult
GET  /api/scripts/reference -> ScriptReference
```

### 6.6 Alerts [ALERTS] (types in `types-alerts.ts`; §16)
```
GET  /api/alerts/status                 -> AlertsStatus (channels configured / active, filters, counters, recent
                                           alerts, last test; never a secret)
POST /api/alerts/test {channel?: telegram|email}   -> AlertsTestResponse (per-channel result or Spanish error)
```
The test goes to every **configured** channel, enabled or not (an unconfigured one comes back skipped with the
reason). JSON only (`Content-Type: application/json`, `{}` is enough; otherwise 415: a page of another site cannot
send that without a CORS preflight, so it cannot make the server message the user). 400 unknown channel or bad body,
409 a test is running, 429 within 5 s of the last test, 503 module missing.

### 6.7 Walk-forward validation [VALIDATION] (types in `types-validation.ts`; §17)
```
POST /api/validation/run {symbols?, tfs?, bars?}   -> ValidationStatus   (starts a background job)
GET  /api/validation/status                       -> ValidationStatus
GET  /api/validation/latest                       -> {report: ValidationReport | null}
```
400: an invalid tf, `bars` outside 1000–5000, more than 12 symbols or 24 series, an unknown symbol (`hub.resolve`).
409 'Ya hay una validación en curso…'. 503 when the module or the hub is missing. Omitted fields use the
`validation.*` settings.

---------------------------------------------------------------------------------------------------------------------
## 7. NEXUS adaptive engine (BE-ENGINE)

Pure function `run_nexus(cs, params, prior, *, gate_state=None, vetoed_entries=None) -> NexusResult`
(`app.engine.nexus`) processes bars **sequentially and causally**. `gate_state` = the global GateLearner state
(`model_state` kind `gate`), pooled into the gate weights of the **last bar only** (historical decisions use the
causal prior chain + what the run learns). `vetoed_entries` = entries the semáforo vetoed when they were the live
decision, `[{time (decision bar open, unix s), direction long|short, level red|amber, detail}]`: that candidate is
rejected (`REJECTED_*` marker with the macro reason, gate never TAKE via `macro_merge.veto_gate`, no position), the
learner sees the same samples, and the run stays causal (the veto was known at that bar's close).
`NexusResult.to_response()` is the `NexusResponse` body (without provider/note/quick); the result also carries
`snapshots` (step 5), `state` (final learner state), `context` (GateContext of the last bar, used to re-check a
user's entry/SL/TP idea) and `decision_gate` (gate learner state applied at the last bar). The commission is
`params.fee_pct` = the symbol's asset-class fee (§4, set by `NexusService.params(symbol)`): trade R / pnl, the
per-bar returns and the gate's R:R and EV are net of it.

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
   - **In-run gate label** (edge history, the run's GateLearner updates, `rejected_would_win_rate`): a candidate
     decided at the close of bar i won = TP1 reached before the SL within `time_stop_bars` bars (bars
     i+1..i+time_stop_bars, SL first when both are touched in one bar); reaching neither by then (a time stop) is a
     loss. The same target as the global gate learner (§10).
5. **Prior / memory (anchored windows, exact resume)** (`app.engine.nexus_loop`, `app.engine.nexus_resume`):
   - `prior` = a learner snapshot state, which holds the state AFTER processing its bar.
   - `res.snapshots` sit on an absolute time grid: kind `grid` = the first processed bar of every `SNAPSHOT_EVERY`
     (100)·tf bucket, never the run's last bar (that bar is 'now': an entry fills at its close and the global gate
     state is pooled in); kind `last` = the last bar (learner, gate learner and edge history only). `res.origin` =
     the full anchor of the bar before the first processed one of a cold run.
   - Anchors (`grid` / `origin`) also carry a `resume` block with the run state after their bar: `feat` (where the
     features restart: the states of every recursive filter (EMA 9/21/55/200, MACD, the Wilder averages of RSI / ATR
     / ADX, Supertrend bands, OBV, HTF EMA / ATR) at the first bar of an HTF bucket ≥ `RESUME_LOOKBACK` (256) bars
     before the next bar, or `rec: null` = cold at the chain's start during its first ~460 bars); the swing levels,
     regime hysteresis and expert decay memories; the label queue of the last `horizon` bars and the previous score;
     the open position and its levels, the pending entry / exit, cooldowns and re-evaluation clocks, episode flags,
     the loss streak and closed-trade count, the trade / candidate counters; the gate candidates still unresolved,
     without outcomes (the resumed run recomputes them, so nothing in an anchor depends on later bars); `v`
     (`RESUME_VERSION`), `fp` (hash of RESUME_VERSION + the path params, the commission included) and `made` (ms when
     its run read the stored vetoes and gate records); plus the full edge history (200 per side). Finite windows use
     exact per-window sums, so feature values never depend on where the window starts. A run that writes a snapshot
     continues from exactly that state (plain JSON values, learners reloaded from their rounded stored state):
     resuming == continuing.
   - NexusService stores them in `model_state` kind `nexus` (`merge_snapshots`). Anchors are write-once (a new one
     only ≥ 50 bars from every kept one; ≤ 60 kept, plus the newest `last`), except that an anchor with the current
     `fp` replaces a stored anchor of the same bar written without it. Anchors only for bars before the run's newest
     closed bar; a still-forming bar is only ever stored as `last`. `_save_model` first drops the stale anchors,
     stored and new (`stale_for_vetoes`: a veto or gate record of a bar at or before the anchor was recorded after its
     `made`), then merges, so the run's fresh anchor of the same bar (which applied the veto) takes its place at once;
     the anchor version still bumps whenever a stale anchor is dropped.
   - Every analysis (`anchor_window`) starts its window `WARMUP_BARS − 1` bars before the latest stored anchor that
     is not newer than the newest closed bar and leaves ≥ `limit` bars, and uses that anchor's state as the prior.
     When its resume block fits (`resume_fit`: same version and path params, bars fetched back to its `feat` bar)
     the engine also gets the bars from that `feat` bar and continues the stored path bit for bit; otherwise (another
     version or params, too few bars) the anchor's learner is a plain prior, as before. The first processed bar is
     the one right after the anchor: **zero lookahead**, no bar processed twice or skipped.
   - Result: the history shown equals one uninterrupted run from the chain's cold start. Measured at limit 1000 over
     300 closes (cold, and warm with a trained global gate state): 0/299 runs changed past history across 3 anchor
     steps and the live view always equalled the bar-close decision (before exact resume: 3/299, one per anchor step;
     the older sliding window changed 199 of 199 runs over 200 closes; regression test
     `tests/test_imp_engine_repaint_history.py`, ~2.5 min). At an anchor
     step the processed range starts ~100 bars later: markers of bars that leave the window or fall into the new 60
     warm-up bars disappear, nothing else changes. **Any change to what the engine computes must bump
     `nexus_resume.RESUME_VERSION`** (stored paths of another version are then not resumed). Limits: Known
     limitations (§18).
   - The window has `limit` to `limit` + ~100 bars (≤ +150 after an origin), plus the forming bar in a live view.
   - A run with no fitting anchor uses the plain `limit`-bar window with the latest snapshot ≤ its first bar as
     prior, and stores its own start as an `origin` anchor.
6. **State machine** (hysteresis + cooldown): flat → candidate long when `score >= entry_threshold` (short when
   `<= -entry_threshold` and allow_short). **Every candidate goes through the Trade Gate (§8)**: TAKE → enter at
   next bar open; WAIT / AVOID → no trade, emit marker `REJECTED_LONG/SHORT` with the main reason.
   In position: `HOLD_*` while healthy; exits: SL (`EXIT_SL`), trailing (`EXIT_TRAIL`), TP3, opposite TAKE signal
   (`EXIT_REVERSE`), time stop (`EXIT_TIME`), score crossing `exit_threshold` against (`EXIT_SIGNAL`) and
   **early exit** (`EXIT_EARLY`) when the expected value of holding turns negative:
   `ev_hold_r = p·(dist_to_next_tp/R) − (1 − p)·(dist_to_sl/R)`, where p is the first-passage P(next TP before SL)
   derived from p_dir through the drifted Brownian model (`levels.ev_hold`, per-bar sigma, horizon h), not p_dir
   itself. The engine exits EARLY when `ev_hold_r ≤ EARLY_EV` (−0.10 R) with the score against the position, or when
   EV < 0 with the score not clearly in favour (< 0.05) plus a volatility shock against the position, an RSI
   divergence or a regime flip. Every exit carries a Spanish reason.
7. **Levels ("hasta aquí")**: `SL = entry ∓ max(sl_atr·ATR, distance beyond the last swing ± 0.25 ATR)`, R = |entry−SL|,
   `TP1..3 = entry ± tp_r·R`. TP1 → close 1/3 + SL to break-even; TP2 → close 1/3 + chandelier trailing
   (`trail_atr·ATR`); TP3 → close rest. Per-bar arrays `pos_entry/pos_sl/pos_tp1..3` (null when flat).
8. **Prediction**: horizon h; `prob_up`; cone p10..p90 using analog forecasting (empirical forward returns of past
   bars with similar score/regime) falling back to a volatility (√t) cone.
9. **Outputs**: exactly `NexusResponse` (bars, markers, trades, current, prediction, learning, stats). `current`
   always includes `gate.long` and `gate.short` evaluated at the last bar, `advice` (one actionable Spanish line)
   and `warnings`.
   - Exit warnings (contract I7): `EARLY_EXIT` (critical) and the advice 'Cerrar ahora: …' only on an actual
     `EXIT_LONG` / `EXIT_SHORT` bar (the exit rule above is unchanged). While holding with a negative EV:
     `WATCH_EXIT` (warn) 'Vigila la salida: el valor esperado de mantener es negativo (−x R)[, aunque el score NEXUS
     aún no gira en contra]' (the suffix when EV ≤ −0.10 R) and the HOLD advice 'Mantener el largo y vigilar la
     salida: …'. Not shown next to an entry decided at that close.
   - `NexusStats.sharpe`: per-bar strategy returns of a 1× position over every bar after the warm-up (flat bars = 0),
     net of the commissions charged on the fill bars, annualised with `app.sim.metrics.sharpe_ratio` and
     `periods_per_year(bar times)` like the backtests (null with < 10 returns or < 5 non-zero ones).
     `rejected_resolved` = rejected signals whose outcome is known (denominator of `rejected_would_win_rate`, same
     label as step 4). Terminal `signal` shows that resolved sample next to the rate.

Spanish labels: actions BUY=COMPRAR, SELL=VENDER, HOLD_LONG=MANTENER LARGO, HOLD_SHORT=MANTENER CORTO,
WAIT=ESPERAR, EXIT_LONG=CERRAR LARGO, EXIT_SHORT=CERRAR CORTO. Regimes trend_up=Tendencia alcista,
trend_down=Tendencia bajista, range=Lateral, volatile=Volátil. Verdicts TAKE=TOMAR, WAIT=ESPERAR, AVOID=NO TOMAR.

**NexusService** (`app.engine.service`): `analyze(symbol, tf, limit=None, *, upto=None)` = hub candles → anchored
window (step 5) → `run_nexus` with the anchor's prior, the stored vetoes and gate records in the window → persist
snapshots (`_save_model` / `merge_snapshots`) in `model_state` → fill provider/note/quick.
- **Window**: it fetches `limit + 665` hub candles, at most 5000 (`ANCHOR_SPAN` 664 = 2·`SNAPSHOT_EVERY` +
  `RESUME_SPAN` 464 (`RESUME_LOOKBACK` 256 + `SEED_BARS` 200 + 2·`HTF_FACTOR`), + the forming bar; 1665 at the default
  1000, was `limit + 201`); `limit` defaults to `nexus.default_limit` and is clamped to [`nexus.min_bars`, 5000].
  Exact resume needs those bars (Known limitations). The window is set by the newest CLOSED bar; a
  live view adds the forming bar. With `upto` (open time of the last closed bar) the later bars are dropped,
  fetching again when more than the forming bar was dropped, so a bar-close decision analyses exactly the window of
  a closed-only analysis and of the live view of that moment.
- **Cache**: results are cached 5 s, then by (last bar, close, bars fetched, anchor version) with the same params
  and stored-veto version; the anchor version bumps when the stored anchors change (also when a stale anchor is
  dropped) or on `reset`. Closed-bar results are cached under `(symbol, tf, limit, 'closed')`, apart from the live
  view. The veto and gate-record caches (`_vetoes`, `_gates`) are loaded once per process.
- **Too little data**: candles < `nexus.min_bars` → `ok=false`, friendly `message`, and `quick = await
  rt.scanner.quick_signal(...)` so ANY TradingView asset still gets a read.
- **Stale data** (`cs.stale`, §6.2): the engine is never run. `ok=false`, message '<Datos desactualizados: última
  vela hace N días>. NEXUS no analiza datos desactualizados: sin señal, niveles ni veredicto del filtro hasta que
  lleguen velas nuevas.'; a quick read built from those candles gets `stale: true` and a first reason saying so,
  once ('<Datos desactualizados…>: esta lectura usa esas velas antiguas'); `/api/gate/check` answers 422.
- Also `current(symbol, tf, *, upto=None)` (only `current`), `gate(symbol, tf, direction, entry, sl, tp)`
  (GateResult for a user idea, re-evaluated on the last-bar `context`), `model(symbol, tf)` (NexusModel: learning
  summary, calibration, rolling accuracy, experts) and `reset(symbol, tf)` (deletes the stored memory, anchors
  included; stored vetoes and gate records stay).
- **Semáforo merge**: `analyze` / `current` / `gate` merge the market-conditions check (§15) through
  `app.engine.macro_merge` on a copy (the cached engine result is never mutated): **red** → gate AVOID with score
  ≤ 25 and BUY/SELL become WAIT (open positions get a critical warning, they are not force-closed); **amber** → gate
  score × 0.85 (a TAKE that falls below `gate.take_score` becomes WAIT) plus a warning; **green** → the passed check
  is listed first in the checklist.
- **Semáforo veto** (I3): when the semáforo vetoes the last bar's BUY/SELL (red, or amber pushing that side's gate
  out of TAKE; `macro_merge.vetoed_side`), the engine is re-run with the veto, so no position, levels or later
  MANTENER/CERRAR advice come from that entry. The veto is **stored** (`model_state` kind `nexus_veto`, key
  `SYMBOL|tf`, newest 200 kept, survives restarts and `reset`) only for a run with `upto` (watcher, auto-trader) or
  a live view whose last bar closed at most min(tf, 300 s) ago (`VETO_RECORD_GRACE_S`); every later run then
  rejects it. Any other veto applies to that view only: a forming bar (checked again when it closes) or a market
  sitting on an old close (session closed, weekend). A stored veto matches only the exact (decision bar time,
  direction), so the same idea can come back one or more bars earlier or later as an entry or a 'Mantener'
  position.
- **Bar-close gate decisions** (`model_state` kind `nexus_gate`): the global gate state is pooled into the last bar
  only, so a decision taken at a bar's close can differ from what the same bar shows once it is history (verdict,
  score, first reason, check vector). The engine returns such decisions in `NexusResult.live_gates`
  (`nexus_decide.gate_record`); the service stores those of a bar-close decision (same rule as vetoes: a run with
  `upto`, or a live view ≤ min(tf, 300 s) after the close; newest 600) and every later run replays them
  (`run_nexus(..., live_gates=)`), so the marker, entry or rejection, gate score and learner sample stay as decided
  at the close. While the recorded bar is still the last bar (a market sitting on that close, a second bar-close
  view) the whole stored GateResult is replayed: `current.gate[dir]`, the action, the advice and the marker all show
  the decision taken at the close, not one with a later global gate state; once the bar is history the digest
  applies. Measured before the fix: 10/16 and 16/28 last-bar candidate decisions changed on replay with a trained
  global state.

**Watcher** (`app.engine.service_watch`): subscribes the hub (owner `nexus`) for the watchlist at `watch.tf` and
also handles markets whose `market:SYMBOL:tf` topic has WS subscribers. `_on_candle` passes the closed bar's open
time; the newest bar that closes while one is processing is queued. `process_closed_bar(symbol, tf, upto)` runs the
closed-bar analysis (`force=True`, semáforo merge and veto included) and publishes `{"type":"nexus", current,
markers}` on `market:{symbol}:{tf}`: the live view (forming bar, computed after the closed-bar decision and its veto
are stored) when a chart listens, else the closed-bar result. `signals` (when the action changes), the `predictions`
row (source `nexus`, model_key `SYMBOL|tf`, features `{bar_time, regime}`, target = now + horizon·tf, at most one
per half bar), `gate_log` rows for candidates (source `watch`) and events come from the closed-bar result: `signal`
for entries/exits, `warn` for EARLY_EXIT, `info` for rejected signals with the reason. **gate_log.target_at**, one
convention for every writer (watch, paper incl. replay/sim, scanner): `service_watch.gate_target_ms(decision, tf,
time_stop_bars)` = (floor_time(decision) + (time_stop_bars + 1)·tf)·1000, the close of the time_stop_bars-th bar
after the decision bar, equal to `evaluator.gate_target_s` (§10).

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
| `risk_reward` | RR(TP2) ≥ `gate.min_rr` after fees; every detail names the commission per side | ✓ only when the R:R before fees < 1.2 (`RR_CRITICAL`); a shortfall caused by the commission fails without being critical and the detail says so |
| `overextension` | |close−EMA21| ≤ 2.5 ATR and RSI not exhausted (≤75 long / ≥25 short) | |
| `volatility` | ATR percentile in [8, 95] and no shock bar (range > 3 ATR) | ✓ on shock |
| `volume` | volume z ≥ −0.5 | |
| `probability` | learned P(direction) ≥ `gate.min_prob` | |
| `edge_history` | analog win-rate ≥ 50% with n ≥ 8 (else neutral) | |
| `expected_value` | EV(R) > 0.05 after fees; the detail names the commission when it alone removes the edge ('; sin la comisión (0,1 % por lado) sería +0,12R') | ✓ when ≤ 0 |
| `recent_performance` | not ≥ 4 consecutive losses (circuit breaker) | |
| `confluence` | ≥ 60% of weighted experts agree | |
Score = Σ weight·pass / Σ weight over non-neutral checks → 0..100. Verdict: TAKE if score ≥ `gate.take_score`
and no critical failure; WAIT if ≥ `gate.wait_score` or the main failure is "wait-able" (overextension, momentum,
volume) ; else AVOID. Check weights are **learned**: online logistic regression on the check vector predicting
"trade hit TP1 before SL within `time_stop_bars`" (the label of §7 step 4), trained from closed trades
(backtest/live) and resolved `gate_log` rows (non-simulated only, each decision once, §10); persisted in
`model_state(kind='gate', key='global')`. `reasons_for` / `reasons_against` / `summary` are Spanish and concrete
(numbers: "Resistencia a 0.6 ATR (R:R 1.1)"). Fees: `params.fee_pct` = the asset-class fee (§4), written by
`gate_checks.fee_text` as '0,1 %' / '0,005 %' ("comisión 0,1 % por lado"). The user can check any idea: `GET
/api/gate/check` with custom entry/SL/TP (400 for non-finite or wrong-side levels). Terminal: `check btc long
[--sl X --tp Y]`.

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
  Features are normalized (37 asset features + 4 extras) and each scan gets a **market context** (breadth over
  1h/4h/1d, mean and dispersion of the 4h move, BTC/ETH trend, momentum and volatility (the 5 most liquid rows in
  other markets), regime one-hot). **Scanner model v2** per `market|tf|horizon_s` (`ScannerModel`, `model_state`
  kind `scanner`, §5):
  - direction: P(up) from a scan-level part (bias, context, regime) and a cross-sectional part (the row's inputs
    minus the scan's mean, so a market-wide shift of every indicator is never read as 150 separate signals);
    magnitude: P(|move| > neutral band) from a volatility prior σ = 0.5·Volatility.D·√(h / 1 day) plus learned
    corrections;
  - the prediction is the outcome with the highest chance of being judged right: up (P(up)), down (1 − P(up)) or
    **neutral** (P(move inside the band); band 0.25 %·√hours clamped to 0.1–1.5 %, ±0.5 % at 4 h) → direction 0,
    action WAIT;
  - learning: one structured update per resolved scan (`learn_scan`): one lesson for the scan-level part, at most
    `ROW_CAP` 50 row lessons for the cross-sectional part, every lesson weighted by Δt / horizon (overlapping 4 h
    windows are mostly one move), plain SGD with a 12-lesson half-life on the active scan-level weights;
  - causal Platt recalibration per key after ≥ 300 resolved rows and ≥ 3 independent windows;
  - cold start: blended with the rules prior centred on the scan's median, without its market-wide tilt (fully
    replaced after 1 500 row lessons; the reasons say so). The legacy v1 `OnlineLogit` is kept only for the replay.
  - single-symbol reads and `quick_score_rows` use the last scan's context and means (≤ 6 h old); a read without a
    scan of its own timeframe says so ('… en 4h …'). Every scanned asset gets a `predictions` row (source `scanner`,
    packed features §5) resolved after `scanner.horizon_s`. The run summary adds '· N neutrales · régimen X'; the
    reasons add a 'Mercado: …' line, the model status and 'Acierto reciente x de n · referencia …'.
- **Deep mode**: top-N candidates with full history (crypto exchanges / keyed providers; directional reads first) run
  full NEXUS + Gate → `verdict`, `gate_score`, SL/TP, regime, `nexus_action`, `opportunity`.
  - **I8**: 'Oportunidad TOMAR' (`opportunity` true) = gate TOMAR + NEXUS BUY (long) / SELL (short) at the last bar.
    Any other gate TOMAR reads 'Filtro favorable · sin señal NEXUS (…; NEXUS: <acción>)' and does not count. Ranking
    (`rank_rows`: opportunities first by gate score, then the directional reads, then the neutral ones (ESPERAR),
    each by |score| × confidence), the run summary ('N oportunidad(es) TOMAR … · K con filtro favorable sin señal
    NEXUS'; its 'mejor' is the first directional read, omitted when every read is neutral) and terminal `results
    --take` use `opportunity` (`is_opportunity`; rows stored before the field never count).
  - **Outdated series are skipped**: no verdict, SL/TP or gate_log row. That applies when the hub or the engine
    flags the set (`stale` / ok=false, reason 'NEXUS: Datos desactualizados: …'), or for 24/7 crypto when the newest
    bar is more than 3 bars behind and closed more than 1 h ago (the hub's rule; reason 'NEXUS: Datos
    desactualizados: … (sin análisis profundo)').
  - gate_log rows: source `scanner`, `time` = the decision bar, `target_at` by the common convention (§7 Watcher).
- Background loop every `scanner.interval_s` for `scanner.markets`; results in `scan_runs/scan_results` (compact
  `data`, §5); WS `scanner` messages; terminal events summarizing the top opportunities ("TOMAR") and the rejected
  ones.
- `quick_signal(symbol, tf)` works for **any** TradingView symbol (single-ticker scan), with the local candles as
  fallback. A fallback read from a set the hub flags as stale carries `stale: true` and the first reason
  '<Datos desactualizados…>: esta lectura usa esas velas antiguas'. `time` = when the read was computed.

**PredictionEvaluator** (`app.sim.evaluator`): every 60 s resolves due predictions (live prices via `rt.hub.quotes`,
else candles), writes the outcome, trains the scanner model with the stored features (one structured update per
resolved scan), resolves `gate_log` rows, updates the gate learner, and emits `learn` events ('Modelo escáner
crypto|1h: 6 predicciones resueltas · acierto 33,3 % (2 de 6) · referencia 66,7 % (siempre «alcista») · Brier 0,357 ·
climatología 0,222 · 1 ventana independiente: sin conclusión'; data adds n, reference, reference_kind, brier_ref,
windows). Hourly it drops the feature vectors of scanner rows resolved more than `scanner.replay_days` ago.
- **Live prices only**: quotes with `stale: true` or more than 2 min old are ignored; those rows resolve later from
  candles (the close of the last bar completed at or before the target).
- **Outcomes without information** resolve with `correct` NULL and never train: |return| ≤ the dead band (0.02 ×
  the neutral band ≈ 1 bp at 4 h), no bar completed between creation and target (closed session), or a stale series
  that stops before the target. Rows that cannot be resolved for 7 days expire the same way.
- **Aggregates**: each resolution updates `prediction_daily` in the same transaction. Global stats
  (`prediction_stats` without a symbol, `learning_summary`, calibration) read `prediction_daily`; per-symbol stats
  read the stored rows.
- **gate_log**: the outcome window = bars opening after the decision bar, up to the close of the
  `nexus.time_stop_bars`-th bar after it (`gate_target_s`, computed from the decision bar for every source; the
  stored `target_at` is informational). Won = TP1 before the SL within that window (SL first in one bar; a covered
  window without either is NONE with a mark-to-market R, and not a win). When the data does not reach back to the
  decision bar the outcome is unknown (NONE, R NULL); a feed that never covers the window resolves NONE/NULL after a
  2-day grace. Source `sim` and `SIM:*` rows are resolved but never train the global learner; one decision (symbol,
  tf, bar, direction) logged by several sources is learned once.
- **Gate summary** (`LearningSummary.gate`): each decision counts once and simulated decisions are excluded.
- **learning_summary** cost (output unchanged): the NEXUS memories are listed without their state; a `nexus` row is
  read again only when its version (updated_at, updates, trained_until) changed since the previous summary of the
  same database (cache per process and db object, so several instances on one database stay correct); only the
  compact top-level tail (from the last `,"learning":`) is decoded when it decides hit_rate and brier exactly as the
  whole document would, otherwise the whole document; decoding runs in a worker thread. 100 rows of ~0.6 MB on
  SQLite: 1.1–1.3 s per call before (JSON decoding on the event loop), now ~0.2 s on the first call after a start and
  40–55 ms afterwards (the rest is SQLite reading the columns stored after `state` through its overflow pages).

**Skill against the reference** (`skill_report`; `GET /api/learning/skill`, §6.4; terminal `skill [escáner|nexus|todo]
[--days 7]`, alias `habilidad`; `learning` and `predictions` print the reference too). Predictions have three
outcomes (up, down, neutral), so 50 % is not chance:
- reference = the best constant outcome of the same rows (known only afterwards, so demanding); chance = the hit rate
  expected from the same mix of calls; Brier against climatology (the period's up rate u), BSS = 1 − Brier / u(1−u);
- 95 % intervals: CR1 clustered by independent horizon windows plus a Newey–West (Bartlett) term for neighbouring
  windows, lag max(1, ⌊4(G/100)^(2/9)⌋), t with G − 1 degrees of freedom. No conclusion below 30 windows
  (`MIN_WINDOWS`) or when the interval includes 0; verdicts none | insufficient | no_edge | better | worse.
  `SkillGroup.window` = the length of one independent window (a group mixing horizons is clustered by its longest
  one; records are per target hour, so never under 1 h); the verdict texts use it.
- Groups: one per scanner key, the scanner as a whole (several keys), NEXUS, everything; cached 60 s (600 s beyond 7
  days or for all rows), invalidated on new outcomes.

**Offline replay** (`replay_report`; `GET /api/learning/replay`; `skill replay [--days 7] [--key crypto|1h|14400]`):
re-predicts the stored scans (vectors kept `scanner.replay_days`, §5) with the designs v2 (trained from zero in the
replay), v1 and the recorded predictions, sequentially: every prediction is scored before learning from it and each
scan is predicted only with what was learned from scans whose horizon had already ended (the note says 'Repetición
secuencial: …'). Streamed in 20 000-row pages, at most 300 000 rows (`truncated`), one at a time per database
(`ReplayBusy`), ≈ 15 s of CPU per week of data in a worker thread; `comparison` = paired v2 − v1 difference with the
same clustered intervals. So far **no improvement is demonstrated**: too few independent windows (Known limitations).

## 11. Simulator, replay, backtest, Monte Carlo, paper (BE-SIM)

- **Simulator** (`rt.sim`): one session at a time. `synthetic` (SyntheticMarket, symbol `SIM:NEXUS`) or `replay`
  (real history revealed bar by bar — NEXUS sees only revealed bars). Speed 0.5–20 bars/s; pause/resume; regime can
  be changed live. Each bar: hub `push_candle` → NEXUS on the revealed window → auto paper trading on account 2
  (only gate TAKE signals) → WS `sim` message (`bar`, `current`, new `markers`, `positions`) + events. Replays
  charge the asset-class commission (`fee_pct_for`; `SIM:*` → `risk.fee_pct`).
- **Backtest**: `run_backtest(cs, params, ...)` uses run_nexus outputs (signals are causal), sizes positions by
  risk % of equity and SL distance, applies fees & slippage, partial TPs, returns BacktestResult; with
  `use_gate=true` also runs the ungated variant → `ungated` + `gate_lift_pct` (percentage points) so the user SEES
  what the filter adds. Commission: an explicit `fee_pct` wins, else `fee_pct_for(symbol, settings)` (§6.4).
- **Monte Carlo**: `bootstrap` (resample trade R-multiples/returns from a backtest of the symbol) or `synthetic`
  (run NEXUS on N synthetic markets with different seeds/regimes; keep it fast — cap bars/paths). Percentile fan,
  distributions, P(profit), P(ruin), VaR/CVaR. The fee is resolved like the backtest's.
- **PaperEngine** (`rt.paper`): accounts 1 (Principal), 2 (Simulación) and 3 (NEXUS automática, below; created by
  `ensure_accounts` on an existing database too). Orders at market (Trade Gate first: AVOID
  → 409 unless `force`) or pending limit / stop orders (below), with SL/TP from the NEXUS gate levels when not given;
  sizing by `risk.per_trade_pct`; portfolio risk limits on accounts 1 and 3 (below).
  Subscribes hub (`owner="paper"`, 1m bars for real symbols) for open positions and pending orders; publishes WS
  `paper` (account, positions, optional `orders` and `risk`) and `trade`
  events ("Cerrado BINANCE:BTCUSDT largo +2,1R (+1,25 % · +125,00) · Objetivo 2", R net of fees).
  - **Fees and R**: fees per side come from the asset class (`fee_pct_for`: `risk.fee_pct_by_class`, otherwise
    `risk.fee_pct`). `r_multiple` is net of fees, like the engine and the backtest: closed positions count entry and
    exit fees; open positions count the fees paid so far, with no exit fee on the open part.
  - **Level management**: SL first; TP1 closes 1/3 and moves the stop to break-even; TP2 closes 1/3 and starts the
    chandelier trail; TP3 closes the rest. A stop moved by a fill takes effect from the next bar: later updates of
    the bar in which it moved (the hub re-sends the forming 1m bar with its running high/low), or the live bar
    containing a move made by a polled price, check the stop against the close only; targets keep using the bar's
    high/low. On a real market a bar delivered late that began before the move checks the stop on its close only,
    and one that ended before the move does not check it at all (targets still fill).
  - **Live prices only**: a hub quote flagged `stale` or older than 2 min, or a non-final update of a real-symbol bar
    that ended more than 2 min ago (e.g. the last session's bar of a closed market), never moves a stop or fills a
    target; one warn event '<SÍMBOLO>: sin precio en vivo (…); el stop y los objetivos se vigilarán cuando vuelva a
    cotizar' per episode. Final bars replayed after a feed stall (catch-up, `closed=True`, ending after the position
    opened) still decide the stops/targets crossed meanwhile, filled at the end of that bar.
  - **Advice** (`advice` + `warnings`, ≤ 1/min per symbol, from the live `rt.nexus.current`): the EV of holding is
    the engine's first-passage `levels.ev_hold` with the position's own stop, next target and R, NEXUS `prob_up`,
    sigma ≈ 0.8·ATR of the position's tf and horizon `nexus.horizon`. 'Cerrar ya' + EARLY_EXIT (critical) only on
    NEXUS's own exit rules (`assess_position`): the action leaves the position's side (EXIT_LONG / EXIT_SHORT, or a
    confirmed BUY/SELL the other way) or EV ≤ EARLY_EV (−0.10 R) with the score against. Any other negative EV gives
    WATCH_EXIT (warn) 'Vigila la salida: …'. The engine's warnings about its own simulated trade (EARLY_EXIT,
    WATCH_EXIT, NEAR_SL, TIME_STOP) are not copied onto the position; side-worded ones only when NEXUS holds the
    same side. Without a current analysis: 'Sin consejo NEXUS: …'.
  - **Live advice never closes positions**: manual positions get a warn event naming the reason ('⚠ <símbolo>:
    NEXUS recomienda CERRAR el largo (…)'), and so do auto positions the auto-trader no longer follows (auto-trading
    off, symbol removed, or a different tf).
  - **Auto-trading** (`paper.auto_symbols` at `paper.auto_tf`) decides on each closed bar exactly once with
    `rt.nexus.current(symbol, tf, upto=<open time of the closed bar>)`, one symbol at a time: it first closes its own
    position on the same exit rules ('Cierre anticipado' for EXIT_* or the EV rule; 'Señal contraria' for an
    opposite entry, then the reverse entry), then opens a position on BUY/SELL with gate TAKE (one position per
    symbol). When several bars close at once (feed catching up) every bar's exit is followed, but only the newest
    closed bar may open a trade.
  - **NEXUS automática** (account 3, NEXUS's own demo account; settings `autopilot.*`, **off by default**): the same
    closed-bar trader as auto-trading (`AutoProfile`: account, origin, tf, symbols, position cap, risk %), with its
    own account and origin `autopilot`. Candidates: `source` list = `autopilot.symbols`; scanner = the first
    `autopilot.scanner_top` rows with `opportunity` (gate TOMAR + NEXUS signal, §10) of the newest **finished** scan
    (a running or failed scan gives none), re-read every 60 s by the monitor and at once when its settings change.
    A new candidate is decided at once on its last closed bar (`floor_time(now) − tf`; synthetic `SIM:*` markets wait
    for their next closed bar), then on every close of `autopilot.tf`; symbols it holds stay watched ('posición
    abierta') after a newer scan drops them, so its exits stay its own. Entries need NEXUS BUY / SELL and gate TOMAR
    (ESPERAR / NO TOMAR are logged as discarded), are sized at `autopilot.risk_pct` of the account's equity, and
    respect `autopilot.max_positions` (checked atomically with the insert in `open_position(max_positions=…)`, rule
    `max_positions`: every candidate's bar closes at the same second and their decisions run concurrently) and the
    portfolio risk rules of account 3. Settings changed elsewhere (Ajustes saves one key at a time) are re-read once
    they settle (0.5 s), never half-saved; `set_autopilot` saves every key first and re-reads once. Turning it on or
    off emits an info event and a log entry; paused, its open positions keep their stop and targets (1m feed) but no
    decision is taken. Decision log (`autopilot_status().log`, ≤ 30, in memory): opened (price, SL, gate), discarded
    and why (filter, cap, risk rule), closed (NEXUS's exit or by hand with `close_autopilot`), missing data and errors
    (at most one note per symbol every 6 h / 5 min). Each watched symbol also carries NEXUS's read of its last decided
    bar (`last`: action and label, e.g. ESPERAR — most bars give no entry and are not logged), at the current tf only.
    Its events (opened, closed, warnings) are prefixed 'NEXUS automática · ' in the event log and never become
    alerts (app.alerts.classify forwards account 1 only). WS: `paper`
    topic, `{type: 'paper_autopilot', ...AutopilotSnapshot}` after each change and every 5 s while it holds
    positions. Simulated only: nothing is sent to a broker, and the status `note` repeats that the published
    validation found no edge (docs/VALIDATION.md).
  - **gate_log rows from paper**: `time` = the decision bar (`gate.time`, else the fill time), `target_at` by the
    common convention (§7 Watcher), `source` = `sim` for simulated markets (simulator replay and synthetic trades,
    any `SIM:*` symbol), otherwise `paper`.
  - **Pending orders** (account 1, table `paper_orders`, migration 0002; `place_order` / `cancel_orders` /
    `list_orders` / `pending_orders`): limit / stop entries with an expiry (GTC, N bars of the order's tf, or a date).
    The gate is asked at the order price when it is placed (NO TOMAR → 409 unless `force`) and **again at the fill
    price**: a NO TOMAR then cancels the order with the reason (`cancelled_by` gate). They trigger on live prices
    only (live 1m bars or polled live quotes): a limit when touched (at its price or better, the open on a gap), a
    stop when crossed (at market with slippage, the open on a gap); a gap through the user's SL / TP cancels. A final
    bar that did not arrive live (replayed after a feed stall, or delayed data) never fills one: the gate at the fill
    would be asked now, with prices that came after that bar, and the position would miss the rest of the replay.
    Such an order gets an info notice ('… una vela de 1m que no llegó en vivo …') and stays pending; after a stall,
    a restart or the placement, the bars that began before the feed was watched count by close only. The position
    opens in the same transaction that marks the order filled and is managed exactly like a market one (SL / TP
    relative to the fill, R net of fees). When the gate cannot be consulted at the fill (engine missing, data error,
    90 s timeout) the order is **cancelled**, never opened blind (`cancelled_by` 'gate', warn event, reason 'Cancelada
    al llegar a X: no se pudo consultar el filtro (<causa>); una orden pendiente nunca se abre sin su veredicto' with
    the cause from `PaperEngine._gate_eval`: 'el motor NEXUS no está cargado', 'no respondió en 90 s', 'error al
    evaluarlo: …'). A market order sent while the gate is unavailable still fills, with no verdict recorded (the user
    acts at that moment; a pending order acts later on a promise to ask the filter).
  - **Portfolio risk** (accounts 1 and 3, each on its own: `risk_state(1)` / `risk_state(3)` = PaperRisk; settings `paper.max_open_risk_pct`,
    `paper.max_positions_per_group`, `paper.crypto_one_group`, `paper.daily_loss_limit_pct`, 0 = off): new orders and
    pending fills (manual, auto-trading, terminal) raise `PaperRiskBlocked` (409 `risk_limit`, **never forced**) when
    the total open risk (entry → current stop × open qty, no fees, % of equity) would exceed the cap (`open_risk`),
    when the correlated group (asset class; all crypto as one group by default, one per coin when off; `SIM:*` their
    own group) already holds the maximum of open positions (`group`), or once the realized loss of the UTC day
    reached the limit (`daily_loss`: % of the day-start balance, latched on the day's worst realized P&L until 00:00
    UTC, persisted in meta `paper.day_low.<account>`). At a trigger the daily and group rules are checked before the
    gate; during the daily block an order that reaches its price is cancelled ('No se ejecutó al llegar a X: …'), not
    deferred; every refusal publishes the WS `paper` message and resyncs the hub subscriptions. Closing positions is
    never blocked. The simulator's account 2 has no risk rules.
  - **Terminal** (group 'Cuenta demo', simulated, never a broker): `buy` / `sell`, `close`, `positions`, `account`,
    `auto`, and `limit|stop <símbolo> <precio> [--short] [--sl X] [--tp Y] [--exp gtc|N|Nv|30m|4h|2d|AAAA-MM-DD
    [HH:MM]] [--qty Q] [--risk R] [--tf 1h] [--force]` (aliases `limite`, `límite`): long by default (`--short` or a
    side word such as corto / venta changes it); N or Nv = N bars of the order's tf; a date is read in the
    `ui.timezone` zone, a bare date = 23:59 of that day; prices take a decimal point or the Spanish format the
    terminal prints (95.000,50 · 1,0850), ambiguous values ('95,000'), bad flags and unknown words are refused, never
    ignored; it warns when the price is 50 % or more from the market and opens the chart. `orders [all]` (aliases
    `ordenes`, `órdenes`), `cancel <id|#id|símbolo|all>` (alias `cancelar`), `risk` (alias `riesgo`: open risk against
    its cap, positions per group, the day's realized P&L against the daily limit, pending orders). A risk block
    prints the rule and says `--force` cannot bypass it; `stop` without arguments suggests `sim stop`.

## 12. PyChart (BE-SCRIPT) — Python scripts with a Pine-like API

Scripts are plain Python executed in a fresh, single-use **separate process** (`python -I -B worker.py` started with
`subprocess.Popen` from `asyncio.to_thread`, `close_fds`, minimal environment without NEXUS_* variables, cwd =
`backend/.tmp/sandbox`, timeout 10 s, then killed; `-I` = no PYTHON* variables, user site or script dir on sys.path;
`-B` = no `__pycache__` writes, which `-I` alone would allow next to the code before the audit hook is armed, since
it ignores PYTHONDONTWRITEBYTECODE) after **AST validation**: allowed imports only `math`,
`numpy`/`np` (+ linalg/random/fft), `statistics`; no dunder attribute access, no `open/exec/eval/compile/
__import__/globals/locals/vars/getattr/setattr/delattr/input/breakpoint/help/memoryview`, no numpy/pandas file or
ctypes APIs. Restricted builtins. Errors map to the user's line numbers.
- **Launcher** (`app.scripting.sandbox`): the JSON payload goes to stdin from its own thread; bounded reader threads
  keep at most 48 MB of stdout (`MAX_STDOUT_BYTES`; beyond it the worker is killed) and the last 64 KB of stderr,
  so a script cannot make the server buffer unbounded output. At most 2 runs at once (`ScriptService`), 4 sandbox
  slots.
- **Memory**: the cap `memory_mb` comes in the payload = `config.SCRIPT_MEMORY_MB` (`NEXUS_SCRIPT_MEMORY_MB`, default
  1024, 128..8192; `fly.toml` sets 320 for its 1 GB VM). Windows: job object (process memory = memory_mb, no child
  processes). POSIX: `RLIMIT_AS` = memory_mb + 512 MB (virtual address space, headroom for numpy's mappings),
  `RLIMIT_FSIZE` 0, `RLIMIT_CORE` 0, `RLIMIT_NPROC` 0, and the worker sets its own `oom_score_adj` to 1000 so the
  kernel OOM killer picks it before the server (the real guard on a small VM).
- **OS isolation (Linux, best effort, each step recorded)**, applied by the worker right before the script while it is
  still single-threaded (a failing step never skips the next ones):
  1. `unshare(CLONE_NEWUSER | CLONE_NEWNET)` in one call when the host allows it: an empty network namespace (only
     `lo`, down: no IP route at all, not even to the host's 127.0.0.1; no uid map is written). A refusal (EPERM /
     EACCES / EINVAL / ENOSPC / EUSERS / ENOSYS: container seccomp profile, Ubuntu's AppArmor restriction of
     unprivileged user namespaces, namespace limit, threads) is recorded and the run continues without it. It isolates
     IP only: Unix sockets with a path and the filesystem stay shared.
  2. `PR_SET_NO_NEW_PRIVS` (after the namespace: an LSM may refuse a user-namespace profile transition to a task that
     already has no_new_privs; setting it needs no privilege).
  3. The inherited descriptors above stderr (listed at worker start-up) are closed (normally 0: `close_fds`).
  4. The rlimits and `oom_score_adj` above.
- **Non-dumpable server**: `app.hardening.harden_server_process()` runs first in the lifespan (Linux:
  `prctl(PR_SET_DUMPABLE, 0)`; a no-op on Windows and macOS, where the Windows job object is unchanged), so other
  processes of the same user, above all the workers, cannot read `/proc/<server>/environ` or `/proc/<server>/mem`
  (database URL, provider keys) nor attach a debugger, and the server writes no core dump. Children get a fresh
  dumpable flag at exec (the workers carry no secrets). Reported in the startup line and, when it failed, a warn (§2).
- **Report**: the worker returns what it applied under the internal result key `_os_isolation`; the launcher removes
  it (ScriptRunResult is unchanged), keeps the latest for `sandbox.last_os_isolation()` and logs one Spanish system
  event (source `scripts`) per distinct state, with the server's protection: 'Aislamiento del sistema de PyChart
  (<platform>): …', inactive protections listed as 'no' (e.g. 'sin red a nivel del sistema: no, este host no lo
  permite (EPERM); la red solo la bloquean las capas de Python del sandbox'). When the bus is not running (`running`
  False: standby, test runtime) the line goes to stderr instead of being dropped.
- **Size caps**: the worker's serialized result ≤ 40 MB (`MAX_RESULT_BYTES`, Spanish error past it), exception
  messages ≤ 2000 characters, compiler / API error messages ≤ 4000, event texts ≤ 300.

API surface (vectorized numpy series aligned with bars):
`open high low close volume time hl2 hlc3 ohlc4 bar_index`, `indicator(title, overlay=True)`,
`strategy(title, initial_capital=10000, risk_pct=1, fee_pct=0.1)`, `input.int/float/bool/string/select/source`,
`ta.*` (sma ema wma hma rma vwma rsi macd stoch atr tr bb kc donchian supertrend sar adx dmi cci mfi obv roc mom
stdev variance highest lowest highestbars lowestbars change crossover crossunder cross rising falling pivothigh
pivotlow barssince valuewhen cum linreg correlation percentrank median vwap wpr cmo tsi), `nz na fixnan`,
`plot(series, title, color, linewidth, style, pane)`, `plotshape(cond, title, location, color, shape, text)`,
`hline(price, title, color, style)`, `bgcolor(cond, color)`, `alertcondition(cond, title, message)`,
`strategy.entry(id, direction, when, qty_pct, stop, limit, comment, qty)`,
`strategy.exit(id, from_entry, stop, limit, trail_atr, when, update, comment)`, `strategy.close(id, when)`,
`strategy.close_all(when)`, `color.*` constants, `print()` → stdout (shown in the terminal). Ship ≥ 8 example
scripts (EMA cross strategy, RSI divergence, Supertrend, Bollinger squeeze, MACD + volume, Ichimoku, VWAP bands,
"NEXUS-lite" mini ensemble). Scripts stored in table `scripts` (examples seeded on start, `is_example=1`).

Order semantics (`runtime_strategy` + `runtime_backtest`): a script runs once over the whole series, so orders are
vectorized *rules*. Signals are evaluated at the bar close and filled at the next bar open; `stop` / `limit` (of
`strategy.entry` or `strategy.exit`) are checked intrabar — gaps fill at the open and the **stop wins** when both are
touched in the same bar. Levels are captured on the signal bar and stay fixed for the trade unless
`strategy.exit(..., update=True)` (refreshed on every close, Pine behaviour); `trail_atr=k` adds a chandelier trailing
stop at k × ATR(14) from the best price. One position at a time (no pyramiding); an opposite entry reverses it.
Sandbox detail: numpy is warmed up before the restrictions are applied and only already-loaded numpy internals may be
imported by numpy itself (`ctypeslib`, `f2py`, `distutils`, `testing` and the file-I/O modules stay blocked).
- Imports are handed out as read-only module proxies, and `from <module> import <name>` is resolved through them
  too (`from numpy.random import mtrand` is rejected like `np.random.mtrand`).
- The validator also blocks the non-dunder introspection aliases (Cython / legacy): `func_globals`, `func_code`,
  `func_closure`, `func_dict`, `func_defaults`, `func_kwdefaults`, `func_builtins`, `im_func` / `im_self` /
  `im_class`, `cell_contents`, `obj`; plus `cffi` / `capsule`.
- `ndarray` / `recarray` built over a raw memory buffer (3rd positional argument, or `buffer=` / `buf=`, as
  `np.ndarray(...)` or a from-imported name) are rejected like `np.frombuffer` (an object dtype over raw memory would
  dereference arbitrary bytes as pointers); `np.ndarray((3,))` and `isinstance(x, np.ndarray)` stay allowed.
- The audit hook keeps its state in a closure, so it cannot be disarmed through the worker module's globals.
- Tests: escapes `tests/test_fix_sandbox_*.py` and `tests/test_scripting_security.py`; OS layer
  `tests/test_imp_sandbox_os_hardening.py` and, Linux only, `tests/test_imp_sandbox_os_linux.py` (no socket
  connection when the namespace is available, `/proc/<parent>/environ` and `mem` unreadable from a hardened worker,
  NoNewPrivs = 1, the launcher from a hardened server incl. the timeout kill). A refusal by the host skips with its
  errno (EPERM…), so CI tells a platform failure from a restricted environment (§18).

---------------------------------------------------------------------------------------------------------------------
## 13. Frontend (React 18 + TS + Vite 5 + lightweight-charts 5)

Shell (LEAD): header (logo, `<SymbolSearch/>`, timeframe, WS status, clock), left nav, page area, bottom
`<Terminal/>` panel (resizable, `Ctrl+\``), toasts. Pages are default-exported components without props:
`pages/{ChartPage,MarketsPage,ScannerPage,SimulationPage,PaperPage,StudioPage,BrainPage,SettingsPage}.tsx`.
`components/terminal/Terminal.tsx` and `components/SymbolSearch.tsx` are default exports too.

**Code splitting** (`App.tsx`, `vite.config.ts`):
- Every page is its own chunk: `lazyPage()` = React.lazy plus one shared download promise; `<PageBoundary
  key={page}>` wraps `<Suspense fallback={<PageLoading/>}>` (empty for the first 150 ms, then 'Cargando sección…').
- The section in the URL starts downloading when App.tsx is evaluated, in parallel with AuthGate's first request.
  Menu hover / focus prefetches a section only while the live connection is open and the browser is online
  (`prefetchOnIntent`): the browser keeps a failed module download for the life of the page, so a prefetch during
  a disconnection would break that section until a reload. The click itself always tries.
- A failed download is a `PageLoadError`: 'No se pudo descargar esta sección.' with an explanation (lost connection,
  or the app was updated while the tab was open) and only 'Recargar página' (Chromium never re-requests a failed
  module URL in the same page and React.lazy keeps the rejection); render errors keep 'Reintentar'.
- `manualChunks: vendorChunk`: `vendor-react` and `vendor-icons` (first load), `vendor-charts` (chart sections only),
  `vendor-codemirror` (PyChart only, ~480 kB, the largest chunk); `chunkSizeWarningLimit` 500 kB (a chunk past it is
  a regression to look at). Measured on 31 Oct 2026: single bundle 1,420 kB / 457 kB gzip; now first load 291.7 kB /
  91.0 kB gzip, first visit to #/chart ≈ 604 kB / 195 kB gzip (`node tests/imp-fe-perf.measure.mjs <scratch dir>`
  builds into a scratch folder and refuses `frontend/dist`).
- CSS: the section stylesheets that existed before the split are imported by App.tsx in their pre-split order
  (styles.css last), so the cascade is unchanged. A stylesheet imported only by its section (e.g.
  `brain/validation.css`) loads with it, after styles.css: it must win by specificity, never by load order (the
  validation table rules are scoped with `.table`, e.g. `.table .val-detail-row > td`). The backend serves
  `/assets/*` immutable for a year and every HTML page `no-cache` (§14), so a new build is picked up on the next load.

Foundation modules (LEAD):
- `api/types.ts` (contract; feature types in `api/types-alerts.ts`, `types-validation.ts`, `types-paper-orders.ts`,
  `types-realtime.ts`, re-exported at its end with `export * from`; same contract rules, and
  `tools/contract_check.py` parses them all) · `api/client.ts` (`api.*` typed REST calls, throws
  `ApiError{status, message, data}`; also `api.paper.orders / placeOrder / cancelOrders / risk`, `api.realtime()`,
  `api.macroCalendar({days, impact, currency})`, `api.learningSkill({source, days, model_key})` with days 0 = every
  stored row and omitted = the server's 7)
- `api/ws.ts` (`socket`: `connect()`, `on(type, fn) -> off`, `subscribe(topic) -> off` (ref-counted, resubscribed on
  reconnect), `command(text) -> Promise<CommandResult>`, `status`, `onStatus(fn)`; `marketTopic(symbol, tf)`)
- `store.ts` (zustand `useApp`: page, symbol, tf, openChart, terminal open/height, wsStatus, toasts/notify;
  `runUiAction(action)` applies `open_chart` / `navigate` / `notify` actions from command results). `setPage`
  pushes `#/<page>` history entries; the first entry is normalized with `replaceState`; `popstate` and `hashchange`
  switch the section.
- `lib/format.ts` (formatters + Spanish label maps + tones; `fmtBarTime(sec, tf)` prints 1d/1w bars as their UTC
  date and intraday bars in local time) · `lib/plural.ts` (`isOne`, `pluralWord`, `plural`: Spanish singular /
  plural, "1 vela" but "0 velas") · `lib/chart.ts` (lightweight-charts v5 helpers: `useChart()` hook,
  `createBaseChart`, `toCandleData`, `toVolumeData`, `toLineData`, `toHistogramData`, `toMarkers`, `addCandles`,
  `addVolume`, `addLine`, `addSeriesForOutput` + `outputData` (indicator outputs), `MarkerLayer`, `LevelLines` +
  `positionLevels(pos)` ("SL · HASTA AQUÍ" price lines), `TradeLevelsLayer` (per-bar pos_* step lines),
  `ConeLayer`, `trimPanes`).
  - Time conversion, ALWAYS through `ts(sec, tf?)` / `fromTs(t, tf?)`: intraday bars are shifted to the local
    clock; 1d/1w bars keep their UTC session date (00:00 UTC, weeks on Monday) like exchanges and TradingView.
    `isSessionTf(tf)`, `timeScaleFor(tf)` (timeVisible=false for 1d/1w), type `ChartTf`.
  - The mappers take a required tf: `toCandleData(c, tf)`, `toVolumeData(c, tf)`, `toLineData(t, v, tf)`,
    `toHistogramData(t, v, tf, pos?, neg?)`, `toMarkers(m, tf)`, `outputData(o, t, v, tf)`, `MarkerLayer.set(m,
    tf)`, `TradeLevelsLayer.set(bars, tf)`, `ConeLayer.set(cone, anchor, tf)`.
  - v5 API: `chart.addSeries(LineSeries, opts, paneIndex)`, `createSeriesMarkers`, `chart.panes()`,
    `pane.setStretchFactor()`; never use v4 APIs (`addLineSeries`, `series.setMarkers`).
  - **Chart lifetime**: `useChart` frees the chart with `removeChart(chart)`, which first marks the chart and every
    series of its panes as removed (WeakSets), then calls `chart.remove()` once; `isChartRemoved` /
    `isSeriesRemoved` are exported. `MarkerLayer` (detaches once), `LevelLines`, `TradeLevelsLayer`, `ConeLayer` and
    `trimPanes` do nothing on a removed chart, and components check `isChartRemoved(chart)` in effect cleanups and
    late callbacks. Rule: after `chart.remove()` nothing of that chart may be touched. In lightweight-charts 5 a model
    change on a removed chart (markers detach, `removePriceLine`, `setData`, `applyOptions`) does not throw; it
    schedules an animation-frame redraw of the destroyed widgets, which throws 'Uncaught Error: Object is disposed'
    (fancy-canvas `resizeCanvasElement`), and React runs useChart's cleanup before the cleanups of the effects
    declared after it.
- `components/ui.tsx` (Button, IconButton, Card, Badge, Tabs, Select, TextInput, NumberInput, Toggle, Stat, Meter,
  Spinner, Empty, ErrorBox, Modal, VerdictBadge, ActionBadge, Disclaimer). `Field` labels its NumberInput, Select,
  TextInput, Toggle or SymbolSearch through `useId` and a context (one control per Field); `Field htmlFor` and
  `useFieldId()` serve custom controls; controls accept `id`, `aria-label`, `aria-describedby`. `Modal`:
  `aria-labelledby`, initial focus (body, then footer, then dialog), Tab trap, Escape only for the top-most modal,
  focus restored on close.
- `styles.css` (dark theme tokens: `--bg --panel --panel-2 --border --text --muted --accent --pos --neg --warn
  --info --purple`, utility classes `.row .col .grid .gap-* .muted .pos .neg .mono .scroll .page .card`).

Visual language: dark TradingView-like, dense but readable, green `--pos` = long/up/TOMAR, red `--neg` =
short/down/NO TOMAR, amber `--warn` = ESPERAR/advertencias. Every page handles loading, empty and error states.

**Honesty rules for measured evidence** (§0.9; `components/brain/evidence.ts`, used by SignalPanel "Resultados de
las señales", backtest "Efecto del filtro", the Studio strategy panel and the Aprendizaje gate card):
- Always "x de n" (with the denominator: "26 con resultado conocido", "x de n verificadas" + "· muestra pequeña"
  below 30); differences between percentages in "pp".
- The filter's record is compared with the win rate of the trades that were taken, not with 50 %.
- A conclusion ("Por ahora indica…", plus "no es una garantía") only with n ≥ 30 on both sides and a 95 % Newcombe
  interval of the difference that excludes 0; otherwise "Aún sin evidencia suficiente (n = …)". Never "prueba" or
  "ahorrando dinero".
- Rejected signals "habrían ganado" = TP1 before SL; taken trades "ganadas" = closed with profit; an avoided
  decision is "no habría llegado a TP1" (a loss or a time stop), never "habría perdido".
- Prediction hit rates are shown against the **reference** (always the most frequent outcome of the same rows) and
  the Brier score against **climatology**, never against 50 % or 0,25 ('50 % = azar' and '0,25 = azar' appear nowhere;
  the SignalPanel Brier tooltip: 'Error cuadrático de las probabilidades: menor es mejor (0,25 = decir siempre 50 %;
  la referencia honesta es la climatología del periodo)'). A hit rate or Brier is coloured only with a conclusion
  (≥ 30 windows and a 95 % interval, `skill_ci` / `bss_ci`, that excludes the reference: `bssTone`,
  `learnFormat.brierTone(b, ref)`); headline figures are neutral, never coloured by another period's verdict. A daily
  reference line is drawn only for days the report covers entirely (`since`) whose verified count matches the daily
  aggregate within max(2 rows, 2 %) (`completeDays` / `sameRowsDay`: resolved scanner rows are pruned after 30 days,
  the daily series keeps 90). `opsUtils.rateTone`
  takes explicit thresholds.
- Walk-forward validation (§17): green only for 'Indicios de ventaja'; intervals hidden below 10 values ('sin
  intervalo (menos de 10)') and flagged 'muy incierto' from 10 to 29.

**Exit and entry semantics in the UI**:
- I7: "CERRAR AHORA" only when the action is EXIT_LONG / EXIT_SHORT. A negative `ev_hold_r` while NEXUS holds is an
  amber "Vigila la salida · EV …"; EARLY_EXIT warnings while holding are cautions, not the pulsing early-exit
  alert. Paper rows say "NEXUS: cerrar" only when the monitor's advice starts with "Cerrar" or "Salir".
- I8 (`GateChecklist.gateSignal` / `GateVerdictBadge`): TAKE plus BUY (long) or SELL (short) is TOMAR; any other
  TAKE is `FAVORABLE_LABEL` "Filtro favorable · sin señal NEXUS" (info tone, summary relabelled, a note naming the
  NEXUS action). The paper order ticket reads the NEXUS action through `/api/nexus` when its preview says TOMAR
  (30 s cache); toolbar gate chips follow the same rule.
- "¿Tomo esta operación?": comprar / vender go to market and the idea's entry never sets the price ('Órdenes
  simuladas: comprar y vender van siempre a mercado y tu entrada no fija el precio; para esperar a tu entrada, deja
  una orden límite o stop'). An idea with its own entry is re-checked at market (`GET /api/gate/check` with the same
  SL/TP and no entry) and shown idea vs market (entry, SL, objetivo, R:R, verdict) before any order; SL/TP on the
  wrong side of the market price means no order; the button for the opposite direction uses NEXUS' levels. 'Dejar
  <compra|venta> <límite|stop> a X (demo)' opens `PendingOrderModal`: what will happen (limit when touched, stop when
  crossed, the gate asked again at the fill), the validity, the gate's NO TOMAR at the order price (explicit 'Dejar
  de todos modos') and a portfolio-risk 409 (cannot be forced).

**Pages**:
- Chart (`components/chart/useMarketData.ts`): candles + NEXUS in parallel; on a WS `{type:'nexus'}` message the
  chart merges `current` and markers at once, then reads `/api/nexus` again unless the 1.5 s bar-close read is still
  pending (the server publishes after storing a semáforo veto of the closed bar, so a bar-close read that reached it
  earlier would keep the vetoed entry's marker and `bars.pos_*` lines). NEXUS bars and markers are clipped to the
  first candle on screen (the anchored window can start up to ~150 bars earlier). `ui.chart_indicators` is saved
  only after a successful read; failed reads retry with backoff 2→60 s, on WS reconnect, on browser 'online' and
  via 'Reintentar'. 'Variación del día' comes from a quote re-read every 120 s and right after 00:00 UTC; a snapshot
  from an earlier UTC day or older than 15 min falls back to the previous candle and is labelled as such.
  Semáforo toolbar: `components/chart/ChartMacro.tsx` (§15). Freshness badge `components/chart/QuoteFreshness.tsx`
  (pure rules in `components/markets/freshness.ts`, never claiming more than the source): 'Tiempo real · <fuente>',
  '<fuente> · hace N min' (a keyed price > 150 s old), 'Retraso N min · TradingView' (tooltip suggests a free Twelve
  Data key), 'Mercado cerrado · <fuente>', 'Desactualizado · hace X' (stale cache, a real-time price > 5 min old,
  stale candles), 'Sintético'. Inputs: the `source` of the WS candles (valid 90 s), `/api/quotes` every 60 s while
  visible, `/api/realtime` every 5 min for the tooltip. Watchlist rows add the short word after the exchange (Tiempo
  real / Retraso N min / Cerrado / Desactualizado / hace N min / Sintético, full sentence in the tooltip; nothing when
  the source does not say, there is no quote or the read is older than 3 min).
- Scanner (I8): the TOMAR tab, its count and the top of the verdict sort use `ScanRow.opportunity === true`; rows
  without it fall back to the gate verdict (null = stored before the field, absent = older server) and their
  verdict cell says that run did not check NEXUS' signal. A deep TAKE with opportunity=false shows "Filtro favorable
  · sin señal NEXUS" (info tone, gate score, tooltip naming NEXUS' action, e.g. 'dice ESPERAR') and sorts below
  TOMAR. The Acción column is the quick model's signal; the expanded row shows 'Acción NEXUS'. A background run
  never replaces a run the user selected (the newer run is offered in the banner); a failed run on screen is
  reloaded ('Este escaneo terminó con error: …'), never replaces another run and is never offered in the banner.
- Simulation: the backtest 'Comisión %' field is empty by default (= the asset-class fee; hint 'Vacío = la de su
  clase de activo'); `POST /api/backtest` carries `fee_pct` only when the user typed it (0 is valid). The sim UI
  localStorage payload (`nexus.sim.ui.v1`) has `v: 2`; a commission saved by v1 (always the prefilled global fee)
  is dropped. The result notes always state the commission used. Monte Carlo synthetic mode shows 'Símbolo' as a
  plain caption.
- Paper: `RiskPanel` (open risk against its cap, positions per group, the UTC day's realized P&L against the daily
  limit, whether new orders / pending fills are blocked and until when) and a pending-orders card
  (`PendingOrdersTable` + the latest closed orders with their reasons). `OrderCard`: 'A mercado' / 'Límite' / 'Stop'
  with price, live quote, validity picker (`ExpiryPicker`) and the gate preview at the order's own price;
  `RiskBlockModal` for a 409 `risk_limit` (no force). REST through `components/paper/ordersApi.ts`; the WS `paper`
  message carries `orders` / `risk`.
- Brain: `TimelineChart` takes `tf` (points through `ts(sec, tf)` / `timeScaleFor(tf)`): the learning timeline (one
  point per UTC day) uses '1d', a model's rolling stats use the model's tf; it and `HitRateSparkline` draw the daily
  reference and climatology lines (no 50 % / 0,25 lines). `SkillCard` ('Habilidad frente a la referencia', period 7 d /
  24 h / 'Todo lo guardado', `GET /api/learning/skill`, 404 → 'servidor sin informe' message): per source the
  period's 'x de n' with the reference and climatology of the same rows. `ValidationCard` (§17) has its own
  full-width row after the gate / scanner-weights row. Scanner page: `ScanAccuracyCard` shows the reference,
  climatology, Brier skill and verdict.
- Settings: kind `fee_classes` for `risk.fee_pct_by_class` (§4); the providers description says the keys are stored
  in the user's own database (SQLite locally, their Supabase Postgres in cloud mode). More field kinds: `text` (plain
  input with placeholder / inputMode), `password` (masked with show / hide like `secret`, but spaces allowed), `multi`
  (toggle chips over `options`, value in the options' order, a saved value outside them still shown); `FieldDef`
  has `placeholder` and `inputMode`. Feature groups are appended to `GROUPS` from `schema-<feature>.ts` files (§4);
  `AlertsCard` sits under 'Alertas' (§16). Sistema card service names include 'Semáforo de mercado', 'Alertas
  externas' and 'Validación walk-forward' (a test checks every id of `app.main.SERVICES` has one).

`api/types.ts` additions (all optional): `Quote.stale`, `CandlesResponse.stale`, `QuickSignal.stale` (I2);
`NexusStats.rejected_resolved`, `BacktestMetrics.rejected_resolved`, `LearningSummary.gate.avoided_judged /
taken_judged` (I4); `ScanRow.nexus_action / opportunity` (I8); `Health.role` and `Health.auth` 'required' (anonymous
cloud liveness). `gate_lift_pct` is in percentage points; `QuickSignal.time` is the wall-clock time of the read.
Improvement round: `Health.db.schema_version`; `Quote.source / realtime / delay_s / market_open` and the same
`source / realtime / delay_s` on the WS `candle` message (`CandlesResponse` has none: `GET /api/candles` sends none);
`MacroEvent.source` (forexfactory | federalreserve | bls | bea | ecb | boe | boj), `MacroCalendarSource`,
`MacroCalendarCoverage`, `MacroCalendarResponse {events, coverage?}`; WS `paper` `orders? / risk?`; `SkillVerdict`,
`OutcomeKind`, `SkillReference`, `SkillDirection`, `SkillDaily`, `SkillGroup` (`window?`), `SkillReport`
(`components/brain/skill.ts` re-exports them). Feature files: AlertsStatus /
AlertsTestResponse (`types-alerts.ts`), ValidationStatus / ValidationReport (`types-validation.ts`), PaperOrder /
PaperRisk (`types-paper-orders.ts`), RealtimeStatus (`types-realtime.ts`). The scanner replay has no TS type yet.

**Frontend tests** (no network, no browser, no new dependencies: the real sources are bundled with the esbuild that
Vite installs and run with `node --test`, React through react-dom/server or a fake browser env): from `frontend/`,
`npm test` = `node tests/run-all.mjs`, which runs every `tests/*.run.mjs` runner (each bundles its own `*.test.ts`
files: fe-chart, fe-shell, int-fe-b, imp-alerts, imp-calendar, imp-data-rt, imp-paper-db, imp-validation) and then
every `tests/*.test.mjs` with `node --test` (fe-trust, int-fe-a, imp-fe-perf, imp-scanner-model(-verify), int2-fe.*);
new suites are picked up by their file name (one file: `node tests/fe-shell.run.mjs session`).

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
  The RLS pass (`app/migrations/pg_rls.sql`, moved out of `schema_pg.sql`) runs after the migrations at every start
  and enables RLS only on tables where `pg_class.relrowsecurity` is still off: idempotent, it covers the tables
  created by migrations, and a redeploy next to an active instance takes no ACCESS EXCLUSIVE lock. `init_schema`
  (baseline + migrations, §5) is serialized with `pg_advisory_xact_lock` (key 'schema'). `db_pg.TABLE_PKS` includes
  `prediction_daily (day, source, model_key, bin)`, `schema_migrations (version)` and `paper_orders (id)`.
- **Auth** (`app.auth`): `NEXUS_AUTH=supabase` → `AuthMiddleware` (pure ASGI, HTTP + WS) requires a Supabase access
  token on every `/api` request (`Authorization: Bearer`; except `PUBLIC_PATHS` `/api/health`, `/api/auth/config`
  and CORS preflights) and on `/ws?token=`. `SupabaseVerifier` validates it against Supabase `GET /auth/v1/user`
  (positive cache 300 s, capped at the token's exp; rejections cached 15 s) and restricts it to
  `NEXUS_ALLOWED_EMAILS`, which is **mandatory** (`create_app` raises without it) unless
  `NEXUS_ALLOW_ANY_SUPABASE_USER=1`.
  - Supabase unreachable (network error, 5xx, 429, unexpected answer): HTTP 503 'No se pudo verificar tu sesión
    (Supabase no responde); reintenta en unos segundos.' + `Retry-After: 5`, WebSocket accepted then closed with
    1013; never cached.
  - Definitive rejection (400/401/403/404/422 from Supabase, or an account not in the allowlist): HTTP 401 'No
    autorizado: inicia sesión en NEXUS.', WebSocket accepted then closed with 4401 (closing before accepting would
    reach the browser as 1006).
  - The middleware puts the verified user and token in `scope['state']`; the WS watchdog (`app.ws`) re-checks that
    token and its exp, never a repeated `?token=` value. Open sockets close with 4401 'Sesión caducada' at the
    token's exp and are re-verified with Supabase every 120 s (`AUTH_RECHECK_S`, fresh, bypassing the 300 s cache;
    an outage keeps the socket). Refreshed tokens arrive as `{"type":"auth","token":…}` (§3 `app.ws`).
  - Startup, fail closed (`app.main.main`, exit 2): a public bind (`NEXUS_HOST` not loopback) needs
    `NEXUS_AUTH=supabase` (unless `NEXUS_ALLOW_INSECURE=1`) and `NEXUS_DATABASE_URL` (unless `NEXUS_ALLOW_LOCAL_DB=1`,
    only with a persistent volume on `/app/data`). In Supabase mode, running without an allowlist
    (`NEXUS_ALLOW_ANY_SUPABASE_USER=1`) or on SQLite logs a warning at start.
  - Host check: `NEXUS_ALLOWED_HOSTS` (locally the loopback names, DNS-rebinding protection) does not apply to
    `/api/health` on public binds (platform health checks use their own Host). CORS / WS origins:
    `NEXUS_CORS_ORIGINS` (+ localhost:5173); CORS exposes `Retry-After`. Middleware order (outermost first): CORS →
    origin check → Auth → host check → StandbyGate → app (preflights and the CORS headers of a 401 still work, and a
    cross-site change is refused before Supabase is asked about its token).
- **CSRF origin check** (`app.main.OriginCheckMiddleware`, pure ASGI, HTTP only, every mode): a POST / PUT / PATCH /
  DELETE that carries an `Origin` header gets 403 `{detail: 'Origen no permitido (<origin>): por seguridad (CSRF),
  NEXUS solo acepta cambios desde su propia interfaz o desde un origen de NEXUS_CORS_ORIGINS.'}` unless the Origin's
  host[:port] equals the request's `Host` (a page this server served; the scheme is not compared because proxies
  terminate TLS), or it is `http://localhost:5173` / `http://127.0.0.1:5173` (Vite dev), or it is in
  `NEXUS_CORS_ORIGINS` (`browser_origins()` = the list CORS uses too). 'null', non-http(s) and malformed origins are
  refused. Requests without Origin (the terminal client, curl, scripts, tests) and GET / HEAD / OPTIONS pass; app.ws
  checks the WebSocket's Origin itself. At most one warn event per 5 min: 'Petición POST /api/… rechazada: origen no
  permitido (…), protección CSRF. Si es tu interfaz, añade ese origen a NEXUS_CORS_ORIGINS.' A frontend on another
  domain (Vercel) must therefore be listed in `NEXUS_CORS_ORIGINS`, as it already had to be for CORS.
- **Built UI served by the backend** (`_mount_frontend`): `/assets/*` (`HashedAssets`) with 'Cache-Control: public,
  max-age=31536000, immutable', also on a 304 (Vite names every asset after a hash of its content); a missing asset
  is a plain 404. `index.html`, any `.html`, the SPA fallback and the 'interfaz no compilada' page get 'no-cache'.
- **Single active instance** (`app.main.Leadership`, §2): a PostgreSQL session advisory lock on a dedicated
  connection outside the pool (`app.db_pg.AdvisoryLock`, key = first 8 bytes of sha256('nexus-terminal:leader:
  <schema>'), `pg_try_advisory_lock`, held while the session lives). It requires a session connection: a direct one
  or Supabase's **Session pooler**, never the transaction pooler (port 6543). The lock session sets
  `idle_session_timeout` = 300 s (`LOCK_IDLE_TIMEOUT_S`, PostgreSQL 14+): after a crash or partition that leaves the
  connection open, take-over happens within about 5 min; after a normal stop or kill, in about 3 s (standby retry
  3 s, leader probe 5 s). While standby (`StandbyGate`), every `/api` path except `/api/health` and
  `/api/auth/config` answers 503 'Instancia en espera: otra instancia de NEXUS está activa con la misma base de
  datos (por ejemplo, durante un despliegue). Reintenta en unos segundos.' + `Retry-After: 5`, `/ws` is accepted then
  closed with 1013, and `/api/health` reports `"role": "standby"` (also in the anonymous view).
- **Frontend** (I5):
  - `lib/config.ts` `normalizeApiBase(VITE_API_BASE)` trims the value and strips trailing slashes, adds https://
    when the scheme is missing (http:// for localhost and 127.0.0.1), turns ws(s):// into http(s)://, keeps a
    path-only base on the same origin.
  - `auth/session.ts` (GoTrue REST: password login, refresh, logout, multi-tab sync; `auth/AuthGate.tsx` shows the
    login when `/api/auth/config` says `supabase`). `loadAuthConfig` in remote mode throws a Spanish error on 404, a
    non-AuthConfig body ('no parece un backend NEXUS… VITE_API_BASE') or a 5xx; AuthGate shows it with Reintentar
    and retries on 'online'. `refreshSession` resolves to the new session, or to null after a definitive rejection
    (400/401/403 with invalid_grant, refresh_token_not_found, refresh_token_already_used, session_not_found,
    session_expired, user_not_found or user_banned: signed out in every tab). Any other failure (no connection, 5xx,
    429, unexpected answer, or a Supabase token call unanswered for 15 s, aborted as status 0) rejects with
    `AuthRequestError{status, code}`: the session is kept and retried after 5 s, 15 s and 30 s, then every 60 s (at
    the latest ~15 s before the token expires), at once on 'online' and on visibilitychange; calls in the 4 s after
    a failure reuse it. A refresh token already rotated by another tab is adopted. `signIn` on a backend 503 shows
    the server's reason ('El servidor NEXUS no pudo verificar tu sesión… Reintenta', or the standby detail) and does
    not log in; only 401/403 give 'sin acceso'. Automatic sign-outs use `signOut({scope:'local'})`.
  - `api/client.ts`: bearer token; 401 → refresh and retry once; the fresh token still gets 401 → local sign-out; a
    transient refresh failure → `ApiError(401, Spanish message)`, no sign-out. Transient 503s ('no se pudo verificar
    tu/la sesión…' from app.auth, 'Instancia en espera…' from the StandbyGate) never sign out: GET is retried after
    1 s and 3 s, other methods never. The detail text is matched (written when CORS did not expose `Retry-After`;
    the server now exposes it, the client does not read it yet).
  - `api/ws.ts`: `?token=`; the socket is usable (status 'open', resubscribe, queue flush) only after the server's
    first message (hello) or 3 s after open. A token refresh is sent as `{type:'auth', token}`. 4401 → reconnect
    with the session's newer token if there is one, else refresh and wait for a new token; 3 consecutive 4401s →
    `signOut({scope:'local'})`. The count starts again only once a socket has stayed usable for 30 s
    (`AUTH_STABLE_MS`) or after a new login: the hello alone does not reset it (a server whose clock runs ahead of
    Supabase's says hello and closes with 4401 at once). 1013 (Supabase unreachable, or a standby instance) →
    reconnect with backoff from 2 s, no refresh; the backoff resets only when a socket is accepted.
- **Deploy**: `Dockerfile` (python:3.13-slim, `pip install --no-deps -r requirements.lock` + `pip check`, non-root
  user `nexus` uid 10001) + `railway.json` / `fly.toml` (backend, 1 always-on instance, EU region; Fly: `--ha=false`
  or `fly scale count 1`, and `fly.toml` sets `NEXUS_SCRIPT_MEMORY_MB=320` for its 1 GB VM), `frontend/vercel.json`
  (`/assets` immutable), `.env.example` (documents `NEXUS_ALLOW_ANY_SUPABASE_USER`, `NEXUS_ALLOW_LOCAL_DB`,
  `NEXUS_SCRIPT_MEMORY_MB`), step-by-step guide and measured database growth in `docs/DEPLOY.md`.
  - Image: the code and its bytecode are owned by root, read-only for the server and its workers (`compileall` at
    build time, because the workers start with `python -I -B`); only `/app/data` and `/app/backend/.tmp` belong to
    `nexus`. `CMD ["sh", "-c", "NEXUS_PORT=${PORT:-$NEXUS_PORT} exec python -m app.main"]`: `exec` makes the server
    PID 1, so the platform's SIGTERM reaches it. `.dockerignore` keeps secrets, databases, environments, caches,
    `frontend`, `tools`, `backend/tests` and `.git` out of the build context; the image has no test tools.
  - CI builds and checks the image on every push and pull request (§18). On Docker hosts with the default seccomp
    profile the worker's network namespace is refused (EPERM, logged, harmless: the Python layers still block the
    network); do not run privileged containers or add capabilities for it. A Firecracker VM (Fly.io) usually allows
    it. Outbound HTTPS also goes to www.bls.gov / www.bea.gov every 12 h (§15) and, when alerts are on, to
    api.telegram.org and SMTP 587 / 465 (§16).
- **Retention** (`app.maintenance`, every 6 h, first run 10 min after start; `run_once(now=None)` accepts a fixed
  clock for tests): candles per tf (1m 10 d, 3m 30 d, 5m 45 d, 15m 120 d, 30m 180 d, 1h 730 d, 2h 1095 d; higher
  tfs forever; `SIM:*` 7 d) and events (60 d) unchanged; predictions: resolved rows 365 d, resolved scanner rows
  30 d (only once the daily aggregates exist); `prediction_daily` 730 d; `gate_log` resolved rows 365 d;
  `scan_runs` 45 d, `scan_results` of the newest 100 runs only; `model_state` kind `nexus` not updated for 90 d is
  deleted (re-learned from history when the symbol is opened again). Added in the improvement round:
  - `model_state` kinds `nexus_veto` / `nexus_gate` older than 90 d (`NEXUS_MODEL_DAYS`) only when no `nexus` row
    with the same key is left (correlated NOT EXISTS), in the same pass right after the memory delete, so they go
    with their memory. While the memory exists they are kept: every later run replays them, and their `updated_at`
    only moves when a new veto or decision is recorded.
  - `paper_orders` no longer pending (filled, cancelled, expired) whose `updated_at` is older than 365 d
    (`PAPER_ORDER_DAYS`); pending orders never (positions do not join on this table).
  - Scanner feature vectors: `scanner.replay_days` (default 7) after resolution, dropped hourly by the evaluator
    (≈ 3 MB per day of resolved scanner rows with the default background scan; 0 = dropped at resolution).
  - Other kinds (`scanner`, `gate`, `macro`, `alerts`, `validation`) are never pruned.

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
  `ff_calendar_nextweek.json` (also .xml / .csv) answers HTTP 404 (checked 2026-10-31), so later weeks come from the
  official calendars below.
- FOMC (official federalreserve.gov schedule, `app/macro/fomc.py`, checked again 2026-10-31; decision 14:00
  America/New_York on day 2, press conference 14:30):
  2026: Jan 27-28, Mar 17-18*, Apr 28-29, Jun 16-17*, Jul 28-29, Sep 15-16*, Oct 27-28, Dec 8-9*;
  2027 (tentative): Jan 26-27, Mar 16-17*, Apr 27-28, Jun 8-9*, Jul 27-28, Sep 14-15*, Oct 26-27, Dec 7-8*
  (* = with Summary of Economic Projections: more volatile).
- Official live calendars (`app/macro/official.py`): the iCalendar (ICS) feeds the agencies publish for calendar
  apps, one plain `http_client()` request each at most every 12 h (1 h after a failure); both answered the honest
  client on 2026-10-31.
  - BLS `https://www.bls.gov/schedule/news_release/bls.ics`: CPI and Employment Situation high impact; PPI, JOLTS and
    ECI medium.
  - BEA `https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics`: advance GDP high, second and third
    estimates medium; Personal Income and Outlays (PCE) high.
  - The last good copy of each lives in `model_state(kind='macro', key='calendar_official')`; a live copy is the
    authority for its (source, kind) up to `until`, the last entry of its calendar. A source that refuses the client
    (403, a challenge page) is logged at most every 6 h ('Semáforo: calendario oficial del BLS no disponible (…)')
    and is **never worked around**. `NEXUS_OFFLINE=1` skips the live fetch.
- Curated official dates (`app/macro/schedules.py`, every date read on 2026-10-31 on the official page named in its
  comment; only dates after that day; never add one that was not checked on its source), used for every (source,
  kind) no live copy reaches (offline, a refusing source, after the end of its live calendar): BLS CPI and
  Employment Situation, BEA GDP + PCE (Nov 2026 – Dec 2027, 08:30 New York); ECB decision 14:15 and press conference
  14:45 Frankfurt (Dec 2026 – Dec 2027; the usual times, the meeting calendar gives dates only); BoE Bank Rate 12:00
  London (Nov 2026 – Dec 2027); BoJ 12:00 Tokyo as a reference only ('hora aproximada': BoJ has no fixed time). All
  lists, FOMC included, end in Dec 2027 and need yearly upkeep.
- Merge (`calendar.merge_extra(feed, fomc + official)`): an official event the weekly feed already lists (same
  currency, release family `FEED_MATCH`, ±2 h) is dropped and the feed copy wins (FOMC copies still become high impact
  with the SEP note); a live and a curated copy of one release are kept once, the live one. `MacroEvent.source` ∈
  forexfactory | federalreserve | bls | bea | ecb | boe | boj; official ids are `<source>-<kind>-<YYYYMMDD-HHMM UTC>`.
- Quotes via `rt.hub.tv_scan("global", columns, tickers=[...])`: TVC:VIX, CME_MINI:ES1!, CME_MINI:NQ1!, TVC:DXY,
  TVC:US10Y, TVC:US02Y, TVC:MOVE, NYMEX:CL1!, COMEX:GC1! (columns close, change, change_abs, high, low).
- Crypto: Fear & Greed `https://api.alternative.me/fng/?limit=1` (60 min cache), OKX funding
  `https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` (10 min cache).

Rules (settings `macro.*`): **red** = high-impact event of a relevant currency within
[−block_after_min, +block_before_min] (blackout), or VIX ≥ vix_danger, or VIX intraday ≥ +25 %, or S&P futures
|Δ| ≥ 3 %. **amber** = high-impact event within warn_before_min (or ≤ 2 h after one), VIX ≥ vix_caution or
VIX Δ ≥ +12 %, S&P futures |Δ| ≥ 1.5 %, DXY |Δ| ≥ 0.8 %, US10Y |Δ| ≥ 10 pb, MOVE ≥ 120; crypto scope also
Nasdaq futures |Δ| ≥ 2 %, Fear & Greed ≤ 15 / ≥ 85 and extreme BTC funding (|rate| ≥ 0.05 % per 8 h; normal
≈ 0.01 %). Daily-change rules are skipped while the US session is closed (Friday 17:00 → Sunday 18:00 New York; the
VIX until Monday 03:15), when the changes are Friday's. Scope by symbol: crypto → USD events + crypto metrics; forex
EURUSD → EUR + USD events; US stocks / indices / commodities → USD; others → own currency + USD high impact.
No data at all → amber "Sin datos macro: opera con cautela" with `stale: true`. With the weekly feed stale and the
quotes older than 15 min, the headline is the first calendar finding from the official dates (an official release
soon or just published: headline, advice = start of the window, gate_check amber with `passed` False) or a red
finding; an amber reading of stale quotes never leads (it stays 'Sin datos macro: opera con cautela.' and gate_check
`passed` None, as before). Reasons then say 'Calendario semanal sin actualizar: solo se evalúan las fechas oficiales'
or 'Sin calendario semanal ni cotizaciones recientes: solo fechas oficiales'. Official dates without a fixed release
time (BoJ) keep '(hora aproximada)' in headlines, reasons and the T−60 / T−15 / T−0 alerts
(`conditions_events.event_name`), and the blackout advice adds '; la hora del dato es aproximada'; the window is still
±block minutes around the reference time. Clock times in messages use the
setting `ui.timezone` (IANA zone the UI reports from the browser, so a cloud server in UTC still says "hasta las
9:00" in the user's time), falling back to the server's local zone.

Backend (`app/macro/*`, `app/api/macro.py`): `rt.macro` = `MacroService(rt)`; `await rt.macro.conditions(symbol=None)
-> MarketConditions`; `await rt.macro.calendar(days=7, impacts=("high","medium")) -> list[MacroEvent]`;
`await rt.macro.gate_check(symbol, direction) -> GateCheck | None` (id `macro`, label "Calendario y volatilidad",
critical when red) — merged into the Trade Gate by the NEXUS service at request time (§7 NexusService: the merge,
and the veto that re-runs the engine and is stored only for a real closed-bar decision). Alerts: level changes and
T−60 / T−15 / T−0 minutes before high-impact events → bus events (source `macro`, level `warn`) + WS topic `macro`
(`{"type":"macro","conditions","alert"}`). REST: `GET /api/macro?symbol=`, `GET /api/macro/calendar?days=1..60
&impact=high,medium|all&currency=` → `MacroCalendarResponse {events, coverage?}`, `coverage` = `{weekly_until,
weekly_stale, verified, sources: [{id, label, live, ok, updated_at, until, error}]}`. Also `rt.macro.coverage(now_s=
None)` and `refresh_official(force)`; `status()` adds `official_events` (official events in the next 60 days) and
`official_errors` only when a live source failed. The 12 h refresh is per process (each start fetches both ICS once).
Terminal: `macro [símbolo]` (alias `mercado`, `semaforo`), `calendario [días] [--all]` (alias `agenda`; a 'Fuente'
column FF / Fed / BLS / BEA / BCE / BoE / BoJ and coverage / failed-source notes).

Frontend (`components/macro/*`): `MarketPulse` (default export, header pill: colored dot + label + countdown to
the next high-impact event; click → compact panel with headline, advice, reasons, ≤ 6 metrics, ≤ 5 events) and
`MacroBadge` (`symbol` prop, scoped conditions for the chart toolbar; optional `state` = a lifted
`useMacro(symbol)`, which returns conditions only for its current scope). Toasts for WS `macro` alerts.
The chart toolbar uses `components/chart/ChartMacro.tsx` (`macroSync.ts`), which keeps the NEXUS verdict in step with
the semáforo: it re-reads NEXUS (debounced 1.5 s, the initial value of each symbol skipped) when the symbol's level
or 'no operar' window changes, or when the global level pushed on the WS `macro` topic changes (then also re-reading
the symbol's semáforo), and polls NEXUS at most every 60 s while the level is not green, because the backend merges
the semáforo at request time. The chart also re-reads NEXUS after the WS `nexus` message (§13 Pages), so an entry the
semáforo vetoed at the bar close leaves no marker or level lines behind. Calendar view: the panel button reads 'Ver
calendario: esta semana y la próxima'; `MacroWeek` shows 'Esta semana' and 'Próxima semana' (local weeks starting
Monday, requests of 8–14 days), source tags on official rows, '≈' for BoJ and coverage notes (failures of the live
official calendars, BLS / BEA; a failed ForexFactory refresh is the 'Calendario semanal (ForexFactory) sin
actualizar' note); tooltips add 'Fuente: …', and the ≤ 5 high-impact events show 'Fecha oficial · <tag>' when there is
no forecast. Pure view model: `components/macro/calendarView.ts`.

---------------------------------------------------------------------------------------------------------------------
## 16. Alerts outside the app — Telegram / e-mail (ALERTS, `app.alerts`)

Goal: a red semáforo, a NEXUS entry / exit of a watched symbol or a demo stop reaches the user when the tab is
closed or hidden. Everything is opt-in: nothing is sent until the user enables a channel and completes its data
(settings `alerts.*`, §4; UI group 'Alertas'). `NEXUS_OFFLINE=1` sends nothing; tests replace `app.util.http_client`
and `smtplib.SMTP` with fakes, and the live checker never calls `POST /api/alerts/test`.
- **Flow** (`AlertService`, `rt.alerts`): a cheap sync bus listener turns each event into an `Alert`
  (`app.alerts.classify`) and queues it (≤ 500); one worker collects the alerts that arrive within 1.5 s
  (`BATCH_WINDOW_S`, e.g. every watched market closing the same hour) and, after the user's filters, dedupe, quiet
  hours and the hourly limit, sends ONE short Spanish message to every active channel. It only reacts to events, so
  it also runs with `NEXUS_BACKGROUND=0`; only the active instance runs it. `stop()` detaches with
  `bus.remove_listener()`. State (counters received / sent / failed / filtered / deduped / quiet / rate_limited /
  no_channel / dropped, the last result per channel, the last test, ≤ 30 recent alerts, the policy memory) lives in
  `model_state(kind='alerts', key='state')`, so a deploy does not repeat messages.
- **Classification** (pure): semáforo (source `macro`, `data.alert`): level changes, T−60 / T−15 / T−0 of
  high-impact events and the end of a no-trade window; red → critical, amber → warn, green → info. NEXUS watcher
  (source `nexus`): BUY / SELL / EXIT_LONG / EXIT_SHORT (level `signal`) → warn, EARLY_EXIT → critical; only the
  watched pairs (the watchlist at `watch.tf`, what the watcher follows), never `SIM:*`. Demo account (source `paper`,
  level `trade`, account 1; the simulator's `sim` is not): opened / closed → warn, a stop (SL / trailing) → critical
  when the trade lost money, else warn, partial targets → info; the text always says 'simulado'. Any module can opt
  in with `data.notify = {category: macro|signals|paper, priority?, kind?, text?}` (or True). `collapse()` makes an
  exit plus an early exit of one market one (critical) alert, before the dedupe. Signal texts say 'no es asesoría
  financiera' and carry a delayed-data note from `hub.quotes` `delay_s` (4 s lookup).
- **Policy** (`app.alerts.policy`, pure, injected clock): category switches (`alerts.macro / signals / paper`),
  minimum priority (`alerts.min_level`), `alerts.signal_symbols` (narrows the watchlist; a bare ticker 'AAPL' matches
  any exchange; the status warns about entries the watcher does not follow, only while it runs); dedupe of the same
  alert key for 10 min, except that a new state of a group (semáforo kinds level / volatility / blackout_end; per
  market `nexus|SYMBOL|tf`) drops the previous key, so red → amber → red and BUY → EXIT → BUY are announced again
  while plain repeats, a restart re-announcing the same state and an exit + early-exit pair stay deduped; the hourly
  message limit `alerts.max_per_hour` (rolling hour), which critical alerts may exceed only up to max(2 × limit, 60);
  quiet hours in `ui.timezone` (else the server's zone), which also hold critical alerts unless
  `alerts.quiet_critical`. Alerts left out by the limit or the quiet hours are counted and announced in the next
  message. Policy memory = `{seen, sent, omitted, latest {group: [key, t]}}`.
- **Channels** (`app.alerts.channels`): Telegram = the user's own bot, plain-text `sendMessage` on
  `https://api.telegram.org` through `http_client()` (token and chat id validated; one 429 retry of ≤ 10 s; ≤ 4000
  characters). E-mail = `smtplib` in a worker thread, STARTTLS (587) or SSL/TLS (465) verified with
  `util.ssl_context()`, 'sin cifrado' only without a password; 20 s socket timeout, 45 s per channel and message. A
  batch holds ≤ 12 alerts, 280 characters per line. Failed deliveries are recorded, not queued for retry; a warn
  event (source `alerts`) at most every 15 min per channel. Secrets never appear in the status, health, events or
  errors.
- REST: §6.6. Terminal (group 'Alertas'): `alertas` (status) and `alertas test` (aliases `alerts`, `avisos`; `test`
  / `prueba` / `probar`). The test message reads 'puede recibir tus alertas' and 'Con tus ajustes se envían: …'.
- **Frontend**: settings group 'Alertas' (`schema-alerts.ts`: chat id, SMTP host / user and the addresses are kind
  `text` (trimmed), the bot token kind `secret`, the SMTP password kind `password`, sent exactly as typed, spaces
  included) with `AlertsCard` below it (state of each channel, last results, 'Enviar prueba'). Desktop
  notifications in the browser (`components/macro/sound.ts` / `alerts.ts`): localStorage
  'nexus.alerts.desktop', default off; the permission is asked only from the toggle's click; shown only while the
  tab is hidden or unfocused; 60 s dedupe by tag; bursts folded (3 per 15 s, then one 'N avisos más'); red macro
  alerts plus NEXUS entries, exits and early exits; a click opens the chart. They need an open NEXUS tab.
- Tests: `tests/test_imp_alerts_*.py` (classify, policy via the service, channels, API, emitters with the real
  PaperEngine, verify) and `frontend/tests/imp-alerts.*`.

---------------------------------------------------------------------------------------------------------------------
## 17. Walk-forward validation — "¿hay ventaja real?" (VALIDATION, `app.validation`)

Question: does NEXUS + Trade Gate win more than anyone entering at random with the same risk rules and costs? The
published answer is in **`docs/VALIDATION.md`** (Spanish), with its data in `docs/validation/*.json`. **Honest
conclusion of the current run** (31/10/2026, Binance BTC/ETH/SOL/XRP/BNB × 15m/1h/4h/1d, 5 000 bars, the final engine
of the improvement round, fingerprint `0afc602bbbbf`): **'Sin evidencia de ventaja'**. NEXUS + filtro −0,08R per
trade (437 trades; 95 % block interval −0,18R to +0,02R), 0,13R below random entries of the same period and side
(−0,22R to −0,03R); without the filter −0,23R (1.907 trades, −0,31R to −0,13R). The filter reduces the loss mainly
because it trades much less, not because it picks better. VALIDATION.md §4 says what that means for using NEXUS: a
research and simulation tool, a TOMAR is not an edge, not financial advice.
- **Method** (`walkforward.py`, pure and deterministic, CPU in worker threads ≈ 1–4 s per 5 000-bar series):
  `run_nexus` once per series, causally (no stored prior, the gate from its initial weights, never the live global
  state), so every decision is out of sample. After `validation.warmup_bars` (500) the decision bars are split into
  `validation.folds` (5) consecutive periods, fewer for short series (each ≥ 100 decision bars; e.g. 4 at the 1 000-bar
  minimum). Variants on identical risk rules (NEXUS levels and time stop, the asset-class commission inside R, 0.02 %
  slippage in R; account metrics from `simulate_account` at `risk.per_trade_pct`): `nexus` (the product), `ungated`
  (every candidate), `mechanical` / `ungated_mechanical` (the same entries with the mechanical exits of the engine's
  Position: SL, trail, TP3, time), `buy_hold`, `random` / `random_ungated` (`validation.random_runs` 1 000 Monte Carlo
  sequences matched per period and side, NEXUS default levels via `plan_levels`, mechanical exits; hence the 'mismas
  salidas' entries-vs-exits comparison). `ValidationSeries.gaps` = missing bars of 24/7 markets (crypto, `SIM:*`),
  null for markets with sessions.
- **Statistics** (`stats.py`, `aggregate.py`): 95 % percentile bootstrap at series level; time-block bootstrap for
  groups (block = span / 30, 1–30 days, ≥ 8 blocks); excess over the period / side random mean; correlation → N_eff.
  No interval with fewer than 10 values (`MIN_CI_N`: series, periods, every variant, the gate effect, pooled groups;
  `ci95` / `ci95_blocks` / `excess_ci95` … null); 10–29 → 'muy incierto' (`verdict.ci_for`; the card's `ciText` also
  hides intervals that older stored reports carry with n < 10). Drawdowns: the account and buy and hold are valued bar
  by bar; the comparison with random entries uses the closed-trade drawdown in time order
  (`overall.nexus.trade_max_dd_pct` vs `random.trade_max_dd_p50`).
- **Verdict** (`verdict.py`): 'Indicios de ventaja' only with ≥ 30 trades, the 95 % interval of the expectancy above
  0, NEXUS at or above the 95th percentile of the random sequences (groups: the block interval of the excess above 0)
  and a positive result in ≥ 60 % of the periods with trades (a series needs trades in ≥ 2 periods) or of the series;
  otherwise 'Sin evidencia de ventaja' with the concrete reason. Never 'prueba' or 'garantía'.
- **Service** (`rt.validation`, §2): one job at a time, in memory (a restart mid-job loses it); candles through
  `rt.hub`; the last report is stored in `model_state(kind='validation', key='latest')` and survives restarts. Events
  (source `validation`): info at start, learn per series and at the end, warn per failed series, error if the job
  fails, success only for an edge verdict. REST: §6.7. Terminal (group Aprendizaje): `validar [símbolos] [--tf=1h,4h]
  [--bars=3000]`, `validar estado`, `validar resultado` (aliases `validate`, `validacion`, `validación`).
- **CLI** (no server; public exchange APIs through the providers and `http_client()`): `python -m app.validation.cli
  --symbols … --tfs … --bars 5000 --out … --compact ../docs/validation/binance-AAAA-MM-DDTHH-MMZ.json --markdown …`
  (≈ 65 s for 20 series). VALIDATION.md §6 has the command and the recipe of the engine fingerprint (git blob ids
  of the 21 `app/engine` files it uses, `service*.py` excluded). `tests/test_imp_validation_verify.py` takes the newest
  JSON by `generated_at`, checks every figure the document quotes and requires every JSON in `docs/validation/` to be
  cited with its trade counts (the previous run, engine of commit `8191643`, is kept and cited in VALIDATION.md §7).
- **UI**: `components/brain/ValidationCard.tsx` on the Aprendizaje page (own full-width row): the last report per
  asset / timeframe, the baselines, the verdict, 'Validar ahora' / 'Opciones' (polls the status every 2 s while
  running, refreshes on `validation` events), 'x de n', green only with an edge verdict. Its tables fit a ~1024 px
  window; narrower windows scroll inside the table and the expanded series detail stays in view (sticky, container
  query units on `.val-card .table-wrap`). The 'Frente al azar' box compares the closed-trade drawdowns ('Caída máx.
  por operaciones cerradas: NEXUS x % · azar (mediana) y %'); the account box says both of its drawdowns are per bar.

---------------------------------------------------------------------------------------------------------------------
## 18. Verification checklist (what "done" means)

- `start.bat` (Windows; it runs `start.ps1` with `-ExecutionPolicy Bypass`) / `bash start.sh` from a clean clone:
  creates venv, installs deps (`requirements-dev.txt`), builds UI, starts server, opens browser; DB created and
  seeded; services running; `/api/health` ok (`role` "single").
- Chart page shows BINANCE:BTCUSDT 1h with NEXUS markers, SL/TP lines, gate verdicts and the freshness badge;
  indicators can be added; leaving a chart section logs no 'Object is disposed' error.
- Terminal: `help`, `status`, `signal btc`, `check eth long`, `scan crypto`, `backtest btc 1h`, `sim start`, `orders`,
  `risk`, `skill`, `realtime`, `calendario`, `alertas`, `validar estado`; `settings` never shows a credential.
- Backtest reports gated vs ungated metrics; no-lookahead tests pass; scripting sandbox blocks escapes.
- Backend suite, offline, from `backend/` (`PYTHONUTF8=1`): `.venv/Scripts/python.exe -m pytest -q -p
  no:cacheprovider` (SQLite; `tests/test_imp_engine_repaint_history.py`, the 300-close repaint regression, takes
  ~2.5 min). The same suite on PostgreSQL: `NEXUS_TEST_PG_URL=postgresql://... .venv/Scripts/python.exe -m pytest -q`
  (embedded server: `tools/pg_start.py`, see its header); any SQL or migration change runs on both. Platform-only
  tests use `pytest.mark.skipif(<os.name / sys.platform>, reason=...)`, and a refusal by the environment skips with
  its errno in the reason (EPERM…). Dependency drift: `tools/lock_requirements.py --check` or `NEXUS_CHECK_LOCK_ENV=1
  pytest tests/test_fix_cloud_deps.py`.
- Frontend, from `frontend/`: `npm run typecheck` (= `tsc --noEmit -p tsconfig.json`) and `npm test` (= `node
  tests/run-all.mjs`, every suite of §13). First-load weight: `node tests/imp-fe-perf.measure.mjs
  ../backend/.tmp/fe-perf/dist` (builds into that scratch folder, never into `frontend/dist`).
- Live API contract: `backend/.venv/Scripts/python tools/contract_check.py --base http://127.0.0.1:8420 [--symbol
  SIM:NEXUS]` (every endpoint against types.ts + types-*.ts, the terminal commands included; read-only: never
  `POST /api/alerts/test`, `POST /api/validation/run` or demo orders). On a fresh database a few warnings are expected
  (lists that are still empty, no stored validation report); failures are not.
- Linux portability (also worth running on Windows before a push): `python tools/ci/portability.py`. Docker image
  (with a Docker daemon): `docker build -t nexus-terminal:ci .`, then `python3 tools/ci/docker_smoke.py
  nexus-terminal:ci` and `python3 tools/ci/docker_tests.py nexus-terminal:ci`; `tools/ci/launcher_smoke.py` is POSIX
  only.
- After any change in `app/engine/` that changes what the engine computes: bump `nexus_resume.RESUME_VERSION` (§7)
  and re-run the walk-forward validation, refreshing `docs/VALIDATION.md` and a new `docs/validation/*.json` (§17).

### Continuous integration (`.github/workflows/ci.yml`, GitHub Actions)
Every push to main, every pull request and `workflow_dispatch`; `permissions: contents: read`, no secrets,
`persist-credentials: false`, a newer push cancels the run in progress (concurrency group); the app runs offline
(`NEXUS_OFFLINE=1`, `PYTHONUTF8=1`). First-party actions only (checkout@v5, setup-python@v6, setup-node@v5). Jobs:
- **Portabilidad Linux** (`tools/ci/portability.py`): LF in shell scripts, the Dockerfile and .dockerignore (also in
  the git index; `.gitattributes` pins `*.sh`, Dockerfile, .dockerignore to LF and `*.bat`/`*.ps1` to CRLF), start.sh
  mode 100755, the exact case of relative imports in frontend/src and frontend/tests, the Dockerfile's COPY / ADD
  sources, and the Linux optional native packages of `frontend/package-lock.json`.
- **Backend (SQLite)**: the whole pytest suite on Ubuntu, Python 3.13, `requirements-dev.txt`, `-ra --durations=15`.
  A step before pytest lifts `kernel.apparmor_restrict_unprivileged_userns` on the throw-away VM (sudo sysctl, a
  `::warning::` if it cannot) and probes `unshare --user --net true`, so the sandbox's network-namespace test can run
  there; pytest itself runs as the runner user.
- **Backend (PostgreSQL 16)**: the same suite against a `postgres:16` service (`NEXUS_TEST_PG_URL` on localhost: the
  client turns TLS off only for loopback hosts), one schema per test; no AppArmor step (the namespace test may skip
  with EPERM there).
- **Interfaz (Node 20)**: `npm ci`, then typecheck, `npm test` and `npm run build`, each still running when an earlier
  one failed.
- **Imagen Docker (Linux)** (job limit 55 min ≥ the sum of its step limits 15 + 10 + 25): build the production image;
  `tools/ci/docker_smoke.py` (fails closed without login / persistent database / e-mail allowlist; starts on
  loopback inside the container with `PORT` injected like Railway; `/api/health` in local mode; the server is PID 1,
  non-root, without capabilities, its code read-only, no test tools; no error events at start; a PyChart script runs
  and a 4 GB allocation is stopped by RLIMIT_AS; `contract_check.py --symbol SIM:NEXUS` against the container; `docker
  stop` without SIGKILL, slower than Fly's 5 s is a warning); then `tools/ci/docker_tests.py` runs the whole backend
  suite inside the image as uid 10001 with the exact lock (`NEXUS_CHECK_LOCK_ENV=1`; pytest added for the user in a
  throw-away container, the repository files the tests read mounted read-only, never over `backend/app`). A skip whose
  reason says POSIX- or Linux-only fails the run; other sandbox / hardening skips (e.g. the namespace under Docker's
  seccomp, EPERM) are warnings. It also runs after a failed smoke test. Both scripts stop themselves below their step
  limits, so a hung run still prints why it stopped and cleans up.
- **Lanzador Linux (start.sh)** (`tools/ci/launcher_smoke.py`): `bash start.sh --no-browser` on a clean checkout
  (venv, `requirements-dev.txt`, `npm ci`, the built UI), `/api/health`, index.html and one `/assets/` file, a clean
  SIGTERM stop.

The scripts in `tools/ci/` use only the standard library (Python 3.9+, `ci_common.py`: collapsible log groups,
annotations, the `$GITHUB_STEP_SUMMARY` tables 'Imagen Docker: …' with Estado | Comprobación | Detalle). CI covers
Linux amd64 only: the Windows launcher and macOS are not exercised there.

### Known limitations (residual risks after the improvement round, Oct 2026)

Engine and memory:
- **Exact resume needs history**: about `limit + 665` bars from the hub (1665 at the default). With a provider that
  serves fewer bars, or `nexus.default_limit` ≥ ~4335 (`MAX_LIMIT` 5000), runs fall back to the anchor's learner as a
  plain prior and history can change at anchor steps as before. Provider revisions of past bars are not re-read by
  the stored path (`POST /api/nexus/reset` recomputes it). A second chain (raising `nexus.default_limit`) switches to
  the main chain once; a legacy origin anchor off the grid can change the history once when the window steps onto it.
- **`RESUME_VERSION` is a manual contract**: a change to features, experts, learner, gate or decisions without a bump
  makes stored paths continue with different code (history stays stable but mixes old and new logic until the anchors
  age out). Its first run, a change of a path setting and the first run after an upgrade each recompute history
  once. Bit exactness relies on numpy per-window reductions and a sequential cumsum: different numpy builds sharing
  one database (several cloud instances) could differ in the last bits.
- **Memory size**: `nexus` rows are ~600 KB per symbol/timeframe at the default `nexus.horizon` (up to ~3.5 MB at
  horizon 200, ~150 ms of JSON per analysis) and are rewritten on every analysis, a forming-bar change of a live view
  included.
- **Bar-close records**: a REST poll between a bar close and the watcher's bar-close run shows that bar with the
  in-run gate decision once (measured 4 of 250 closes when the UI always polled first; the watcher's WS push is
  computed after the record). Records exist only for bars decided at their close (watcher, auto-trader, a live view
  right after it). The veto and gate-record caches are per process: several server instances on one database do not
  see each other's records until restart. Direct `run_nexus` callers (backtest, simulator, validation) build origin
  and resume blocks too: results can differ from before the round by about one trade for some seeds.

Market data:
- **Stamping of TradingView snapshots**: the 1m lag is ambiguous modulo 60 s, so after a fresh start with data
  running ahead the first 1m bars can be one minute off for ~5–15 min until 5m/15m rollovers agree; they are then
  moved in the database, but live events already emitted are not undone. Sparse trading biases the lag estimate late
  (the first trade of each bucket): about 2–4 % of 1m bars can still be misplaced for symbols trading every ~30 s,
  more when the real lag is also large. A one-snapshot spike of data ahead can move a run and move it back at the
  next rollover (rows rewritten twice, same final state). A TradingView correction that lowers a bar's high, or a new
  bar with the same open and a larger range, can be misread until the next rollover. A genuine new bar that opens
  exactly at the followed bar's open is placed up to 15 s later (30 s on 1m, 90 s on 5m/15m). The guarantees rest on
  TradingView's ≤ 15 min bars lining up with UTC quarter hours. Holidays are not in the session guesses.
- **Keyed series**: the first successful provider fetch of a keyed (symbol, tf) series removes its rows from other
  sources, including the locally recorded (`recorder`) history.
- **Delayed quotes are not stale**: TradingView quotes are never flagged `stale`; a delayed one carries `delay_s`
  (e.g. 900 s for US stocks), which the chart badge and the watchlist show ('Retraso 15 min · TradingView').
- **Real time with free keys is periodic**: on Twelve Data's free plan a US symbol gets a real-time price about every
  35–40 s in session (one symbol on screen; slower with more). 'Tiempo real' means each price was real time when
  fetched, not tick by tick; the label shows the age once the price is > 150 s old (`last_quote_at` has minute
  precision, so the age is conservative by up to 60 s). Live bars built from keyed quotes are sampled: wicks between
  polls appear only when the provider's bar is downloaded at the close, and per-bar volume comes from day-volume
  deltas. The plan is not known: real time is judged by the price's own timestamp, so an illiquid US stock without a
  trade for 10+ min is set aside for 1 h (TradingView for it); Polygon's plan is detected by its 403, and paid Polygon
  plans were tested with fakes only. Daily credits are spread by a session estimate that ignores holidays and early
  closes; the daily reset is assumed at 00:00 UTC; credits used by other apps with the same key (or by another
  instance: counters are per process) are invisible until a 429. Alpha Vantage premium entitlements are not
  supported.
- **Keyed real time stopping**: when it stops (credits exhausted, provider down) TradingView snapshots resume in
  buckets ~15 min behind the last keyed bar and are dropped as out of order, so the chart's live bar can pause up to
  ~15 min (the badge says 'Retraso 15 min · TradingView' meanwhile).

Sandbox, security and CI:
- **PyChart aliased constructors**: the buffer-constructor block is static, so an aliased constructor
  (`f = np.ndarray; f(shape, 'O', buf)`) escapes the validator (`np.ndarray` must stay the real type for
  `isinstance`). It is contained by the single-use isolated worker: the run ends with 'terminó sin resultado', with
  no escape and no audit-hook bypass.
- **Linux OS layer**: the Linux tests (`tests/test_imp_sandbox_os_linux.py`) run on GitHub Actions (backend job on the
  VM, docker job inside the image as uid 10001); in the round that wrote them they were only compile-checked and their
  harness run on Windows. The empty network namespace depends on the host: Docker's default seccomp profile refuses
  `unshare(2)` (EPERM), so it is inactive inside the image and probably on Railway / Render, and IP networking is then
  blocked only by the Python layers (the log says so); the backend CI job lifts Ubuntu 24.04's AppArmor restriction so
  the test can run there, while the PostgreSQL job and the image may report it skipped. rlimits, `oom_score_adj`,
  no_new_privs, descriptor closing and the non-dumpable server are exercised. With `NEXUS_SCRIPT_MEMORY_MB=320` on a
  1 GB VM the OOM-priority bump is the main memory guard.
- **What the OS layer does not cover**: the namespace isolates IP only (path-based Unix sockets and the filesystem are
  shared). The worker keeps the server's uid, so a worker that escaped every Python layer could read any file the
  server can (e.g. `data/nexus.db` with the provider keys in SQLite mode) and could signal the server. root or
  CAP_SYS_PTRACE bypass the non-dumpable flag; py-spy or gdb run as the same user no longer attach to the server (it
  needs root). When the namespace is refused, other processes of the same user (a wrapper that stays alive, a dev
  reloader) are not covered by the flag; the Docker CMD and start.sh exec the server. Inside its own user namespace
  an escaped worker has capabilities over that namespace (it could create nested ones); it could get the same with
  its own unshare on such a host, so this is not a regression (seccomp or max_user_namespaces=0 would close it).
- **Credentials in GET /api/settings**: the provider keys, the bot token and the SMTP password are stored in plain
  text in the `settings` table and returned in clear to the settings form (logged-in UI in cloud mode). Only the
  terminal output and the event log are masked (§3).
- **CSRF check relies on `Origin`**: browsers send it with every cross-site POST / PUT / PATCH / DELETE, which is what
  the check needs; requests without it (non-browser clients) are not affected, by design. A frontend on another
  origin that is missing from `NEXUS_CORS_ORIGINS` gets 403 on every change.
- **CI scope**: Linux amd64 only. Node 20 (CI and start scripts) is past end of life since April 2026 (`npm ci`
  prints EBADENGINE for an optional rollup build dependency; harmless). Actions are pinned to major tags, not commit
  SHAs, and `python:3.13-slim` is a floating tag. The in-image suite installs pytest from PyPI at run time (network on
  the runner), runs the whole suite (engine failures also turn the docker job red), and environment skips of security
  tests show up as warnings. A future feature that writes under `/app/backend` (outside `.tmp`) or `/app` (outside
  `data`) fails in the image (the code is root-owned); the smoke test and the in-image suite would show it.

Learning and measurement:
- **No measured edge**: the walk-forward validation finds no evidence that NEXUS + filtro beats random entries
  (§17, `docs/VALIDATION.md`), and the scanner model v2 has no demonstrated improvement either: the real data covered
  only 2 independent windows in the replay and 4 live (both v1 and v2 well below the 'siempre alcista' reference of
  that rally; the replayed v2's +27,1 pp over v1 cannot be claimed before ≥ 30 windows, about 5 days).
- **Validation results are tied to the engine fingerprint** (`0afc602bbbbf`, 21 `app/engine` files): nothing detects a
  stale document automatically (the docs test only checks that the document matches the newest JSON); re-run the CLI
  (~65 s, needs network) after any engine change. The job lives in memory (a restart mid-job loses it; the last
  finished report persists), one job per instance, CPU in worker threads (it can slow the event loop slightly, like a
  backtest). Series-level intervals use an iid bootstrap (trades within a series are sequential) and the group block
  length is a heuristic; there is no formal multiple-testing correction (the conservative rules and the majority of
  periods / series stand in for it). The random baseline cannot use NEXUS' model exits (hence 'mismas salidas') and
  random entries can overlap in time; shorts on spot pairs carry no funding cost; costs are fixed; `rejected_signals`
  counts the whole run, warm-up included. The gated product trades rarely, so most per-series verdicts are 'muestra
  insuficiente' and the pooled results carry the conclusion.
- **Skill intervals have low power** when an edge comes in regime blocks: the clustered + Newey–West intervals
  against the hindsight-best constant can keep a clear point estimate at no_edge for weeks (a simulated +15,9 pp over
  43 windows stayed no_edge). They assume short-range dependence; very persistent regimes can still make them
  somewhat optimistic (t with G − 1 degrees of freedom is conservative).
- **Scanner model v2 caveats**: after the upgrade the 'Acierto reciente x de n' line and the per-key 7-day group
  still include predictions of the old v1 model (rows carry no model-version tag; the texts say 'sin conclusión').
  The Platt calibrators are fit on logits recomputed at resolution time, after the model learned the overlapping
  windows (a slightly optimistic slope, never in served predictions). Hand-set constants: the neutral band
  0,25 %·√h, the volatility prior κ = 0,5 (measured on one day of crypto scans), the regime thresholds, the learning
  rates, the 12-lesson half-life and the 50-row cap; non-crypto markets use the 5 most liquid rows as 'majors' and
  were not tested on live data. The replay vectors add ~3 MB per day (~20 MB at the default 7 days; set
  `scanner.replay_days` 0 when space is tight: the live skill report does not need them).
- **Old scanner rows** (stored before I8, `opportunity` null) count differently: the terminal and the backend
  (`is_opportunity`, `rank_rows`, `results --take`) never count them as TOMAR, while the UI falls back to their gate
  verdict (with a tooltip saying that run did not check the NEXUS signal); their stored run message used the old
  rule.
- **Number formatting**: the backend's `app.sim.common.fmt_num` groups 4-digit integers ('1.238 de 2.515' in verdict
  and terminal texts) while the UI's `fmtInt` (Intl es-ES, CLDR minimum grouping 2, the RAE rule) prints '1238 de
  2515', so SkillCard can show both styles side by side.

Demo account (simulated, never a broker):
- **Optimistic fills, by design and said in the UI ('a ese precio o mejor')**: a limit fills when touched, and a limit
  gap fills at the bar's open, which is realistic for session gaps but slightly optimistic on continuous markets
  (crypto 1m), where a resting limit would fill at its own price. After a limit fill the trigger bar is judged by
  closes only, so a wick through the new stop that recovers within one feed update (or during the gate latency) does
  not stop the position out. The gate at the fill runs a few seconds after the trigger, so it can see that much
  post-trigger data.
- **Risk scope**: the daily limit counts realized P&L only, as a % of the day-start balance (floating losses are
  bounded by the open-risk cap and shown separately); the latch can be lifted by raising or disabling the limit. The
  group limit counts open positions only (pending orders are re-checked at the fill). The open risk at placement is the
  sizing target; the real check happens at the fill. Account 2 (simulator) has no risk rules.
- **Live prices only**: orders on symbols with only delayed data can never fill (the user learns it from the 'no llegó
  en vivo' / 'sin precio en vivo' notices, not at placement), and moves while NEXUS was stopped are unknown (judged
  from the restart by close only). A pending order's market price / distance can come from an old cached price
  (`PaperEngine.last_price` keeps `hub.last_price(allow_stale=True)`); fills are unaffected. The existing `buy` /
  `sell` commands still read their numeric flags with `ctx.flag_float`, so an unparsable value such as `--sl
  95.000,5` falls back to the NEXUS level (the printed SL shows the level used).
- **NEXUS automática (account 3)**: it decides only while the server runs; bars that closed while it was stopped are
  not replayed (after a start each candidate is decided once on its last closed bar). Its decision log and the
  per-symbol reads live in memory (a restart empties them; positions and history are in the database). Scanner
  candidates come from the scan's own timeframe, while entries are decided on `autopilot.tf`. Positions opened at
  another `autopilot.tf` are no longer followed by NEXUS's exits after a change of timeframe (their stop and targets
  still manage them). In a multi-instance deployment it trades on the active (leader) instance only, like every
  background service. No terminal command yet: it is driven from its view and `/api/paper/autopilot`.

Calendar and alerts:
- **Curated dates need yearly upkeep**: agencies can move a release (as in the 2025 shutdown). The live BLS / BEA
  copies override the curated dates within their range, but ECB, BoE and BoJ have only curated dates, so
  `app/macro/schedules.py` must be updated by hand with dates checked on the official pages; every list, FOMC
  included, ends in Dec 2027. ECB 14:15 / 14:45 and BoE 12:00 are their usual times, not confirmed per date. BoJ times
  are a reference: the official BoJ window (JPY scopes only, and only when the weekly feed does not list that
  meeting) is still ±block minutes around 12:00 Tokyo and can miss a late release (it is labelled 'hora aproximada').
- **Official calendar coverage**: beyond the weekly feed only official releases are known (no forecast or previous, no
  medium-impact data of other countries, no speeches). Coverage is per source, not per kind: a live copy hides the
  curated dates of every kind it lists up to the last entry of the whole calendar. Curated BLS fallbacks exist only
  for CPI and NFP (PPI, JOLTS and ECI need the live or stored copy), although the failure note says the stored or
  verified dates are used. Stored live copies never expire: if a source refuses the client for a long time, a release
  moved after the last good fetch is missed until a refresh succeeds. BLS often refuses non-browser clients; it is
  then reported, never worked around. The 12 h refresh is per process (each start fetches bls.ics and bea.ics once).
- **Alerts**: failed deliveries are not queued for retry (a Telegram or SMTP outage loses that message; it is
  recorded as failed and the omitted-alert note is kept for the next message). Batches go out one after another, so
  a slow SMTP server (20 s socket timeout, 45 s per channel) delays the alerts behind it, and a worker blocked on SMTP
  may delay shutdown by ~20 s. Signal alerts cover only the watchlist at `watch.tf`; `alerts.signal_symbols` can only
  narrow it. The delayed-data note needs the hub's TradingView quote (4 s): without it a signal carries no note even
  if its data is delayed. With `ui.timezone` empty (the UI never opened) quiet hours use the server's zone, usually
  UTC in the cloud. Paper alerts rely on the current `paper.py` event data shapes (`tests/test_imp_alerts_emitters.py`
  runs the real PaperEngine to catch changes). Desktop notifications need an open NEXUS tab (no service worker or
  push) and some browsers (Chrome on Android) do not show them.

Database and UI:
- **Migrations** run in one transaction, so no `CREATE INDEX CONCURRENTLY`: keep them small. A broken migrations
  folder aborts the start (fail fast). An older instance restarting after the upgrade writes `meta.schema_version` =
  '1' back (informational; `schema_migrations` is authoritative).
- **Stale tabs after a rebuild or redeploy**: a tab opened earlier fails when it opens a section it had not loaded yet
  ('No se pudo descargar esta sección.' with 'Recargar página'; no automatic reload, to avoid reload loops on broken
  deploys). Every `import()` rejection becomes a `PageLoadError`, including a page module that throws while it runs
  (a code bug), which then shows the same message although a reload will not help (the text hedges and shows the real
  error). If the network dies while the WebSocket still reports 'open' (before the ping notices), a hover prefetch can
  still break that section until a reload. `vendor-icons` puts every icon used anywhere in the first load (~8 kB
  gzip), and the total JS of the whole app is ~3 % larger than the single bundle (per-file overhead).
- **Transient 503 detection by text**: the server now exposes `Retry-After` through CORS, but the web client still
  recognizes the two transient 503s by their detail text and retries GETs after 1 s and 3 s; if that wording
  changes, GETs stop being retried (a 503 still never ends the session).
