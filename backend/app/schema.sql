-- NEXUS Terminal local database (SQLite). Created/migrated automatically on start.
-- Time columns: `time`/`*_time` = unix seconds; `ts`/`*_at` = unix milliseconds.
-- BASELINE (schema version 1, frozen): later changes are numbered migrations in app/migrations/ (one .sqlite.sql and
-- one .pg.sql file each), applied in order by init_schema and recorded in schema_migrations(version, name, applied_at).
-- An existing database with every table and index below is marked as baseline without re-running this script.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,            -- JSON
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
  symbol      TEXT PRIMARY KEY,        -- EXCHANGE:TICKER
  ticker      TEXT,
  exchange    TEXT,
  description TEXT,
  type        TEXT,
  market      TEXT,
  currency    TEXT,
  pricescale  INTEGER,
  logoid      TEXT,
  provider    TEXT,
  history     TEXT,
  updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS candles (
  symbol TEXT    NOT NULL,
  tf     TEXT    NOT NULL,
  time   INTEGER NOT NULL,             -- bar open, unix s UTC
  open   REAL    NOT NULL,
  high   REAL    NOT NULL,
  low    REAL    NOT NULL,
  close  REAL    NOT NULL,
  volume REAL    NOT NULL DEFAULT 0,
  source TEXT,                         -- provider id | recorder | synthetic
  PRIMARY KEY (symbol, tf, time)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS watchlist (
  symbol   TEXT PRIMARY KEY,
  position INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS model_state (
  kind          TEXT NOT NULL,         -- nexus | scanner | gate | nexus_veto
  key           TEXT NOT NULL,         -- "SYMBOL|tf" | "market|tf|horizon" | "global"
  state         TEXT NOT NULL,         -- JSON
  updates       INTEGER NOT NULL DEFAULT 0,
  trained_until INTEGER,               -- last bar time learned (unix s)
  updated_at    INTEGER NOT NULL,
  PRIMARY KEY (kind, key)
);
-- kind='nexus_veto' (key "SYMBOL|tf"): entries the market-conditions semáforo vetoed, so the engine rejects them on
-- later runs instead of opening a position. state = {version, entries:[{time, direction, level, detail, at}]}
-- (time = bar open unix s; direction = long|short; level = red|amber; at = unix ms when recorded; newest 200 kept).

CREATE TABLE IF NOT EXISTS signals (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol     TEXT NOT NULL,
  tf         TEXT NOT NULL,
  time       INTEGER NOT NULL,
  action     TEXT NOT NULL,
  score      REAL,
  prob_up    REAL,
  confidence REAL,
  price      REAL,
  sl         REAL,
  tp1        REAL,
  tp2        REAL,
  tp3        REAL,
  regime     TEXT,
  verdict    TEXT,
  gate_score REAL,
  reason     TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE (symbol, tf, time, action)
);
CREATE INDEX IF NOT EXISTS idx_signals_sym ON signals (symbol, tf, time);

CREATE TABLE IF NOT EXISTS predictions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  source             TEXT NOT NULL,    -- nexus | scanner
  symbol             TEXT NOT NULL,
  tf                 TEXT NOT NULL,
  model_key          TEXT,
  created_at         INTEGER NOT NULL,
  target_at          INTEGER NOT NULL,
  price              REAL NOT NULL,
  direction          INTEGER NOT NULL DEFAULT 0,
  prob_up            REAL NOT NULL,
  score              REAL NOT NULL DEFAULT 0,
  features           TEXT,             -- JSON (for learner updates)
  resolved           INTEGER NOT NULL DEFAULT 0,
  outcome_price      REAL,
  outcome_return_pct REAL,
  correct            INTEGER,
  resolved_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pred_due ON predictions (resolved, target_at);
CREATE INDEX IF NOT EXISTS idx_pred_sym ON predictions (symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_pred_source ON predictions (source, resolved, target_at);
CREATE INDEX IF NOT EXISTS idx_pred_model ON predictions (model_key, resolved, target_at);

-- Daily aggregates of resolved predictions, written in the same transaction that resolves each row. Totals, hit
-- rates, Brier, calibration and the daily charts read them, so they survive the pruning of old rows.
CREATE TABLE IF NOT EXISTS prediction_daily (
  day       INTEGER NOT NULL,          -- UTC day of target_at, unix s
  source    TEXT    NOT NULL,          -- nexus | scanner
  model_key TEXT    NOT NULL,          -- scanner "market|tf|horizon_s" | NEXUS "SYMBOL|tf"
  bin       INTEGER NOT NULL,          -- calibration bin of prob_up (0..9)
  n         INTEGER NOT NULL DEFAULT 0, -- resolved rows (with or without verdict)
  scored    INTEGER NOT NULL DEFAULT 0, -- rows with a verdict (correct 0/1)
  correct   INTEGER NOT NULL DEFAULT 0,
  up        INTEGER NOT NULL DEFAULT 0, -- scored rows whose return was > 0
  prob_sum  REAL    NOT NULL DEFAULT 0, -- sum of prob_up over scored rows
  brier_sum REAL    NOT NULL DEFAULT 0, -- sum of (prob_up - up)^2 over scored rows
  PRIMARY KEY (day, source, model_key, bin)
);

CREATE TABLE IF NOT EXISTS gate_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol      TEXT NOT NULL,
  tf          TEXT NOT NULL,
  time        INTEGER NOT NULL,        -- bar time of the decision
  direction   TEXT NOT NULL,           -- long | short
  verdict     TEXT NOT NULL,           -- TAKE | WAIT | AVOID
  score       REAL NOT NULL,
  win_prob    REAL,
  entry       REAL,
  sl          REAL,
  tp1         REAL,
  tp2         REAL,
  checks      TEXT,                    -- JSON {check_id: 1 | 0 | null}
  taken       INTEGER NOT NULL DEFAULT 0,
  source      TEXT,                    -- watch | scanner | paper | manual | sim
  created_at  INTEGER NOT NULL,
  target_at   INTEGER,
  resolved    INTEGER NOT NULL DEFAULT 0,
  outcome     TEXT,                    -- TP1 | SL | NONE
  outcome_r   REAL,
  resolved_at INTEGER,
  UNIQUE (symbol, tf, time, direction, source)
);
CREATE INDEX IF NOT EXISTS idx_gate_due ON gate_log (resolved, target_at);

CREATE TABLE IF NOT EXISTS scan_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  market      TEXT NOT NULL,
  tf          TEXT NOT NULL,
  started_at  INTEGER NOT NULL,
  finished_at INTEGER,
  n_symbols   INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'running',
  deep        INTEGER NOT NULL DEFAULT 0,
  message     TEXT,
  params      TEXT
);

CREATE TABLE IF NOT EXISTS scan_results (
  run_id     INTEGER NOT NULL,
  symbol     TEXT NOT NULL,
  rank       INTEGER NOT NULL,
  score      REAL,
  prob_up    REAL,
  action     TEXT,
  confidence REAL,
  price      REAL,
  change_pct REAL,
  volume     REAL,
  verdict    TEXT,
  gate_score REAL,
  data       TEXT,                     -- JSON (full ScanRow)
  PRIMARY KEY (run_id, symbol)
);

CREATE TABLE IF NOT EXISTS paper_accounts (
  id              INTEGER PRIMARY KEY,  -- 1 = Principal, 2 = Simulación
  name            TEXT NOT NULL,
  balance         REAL NOT NULL,
  initial_balance REAL NOT NULL,
  currency        TEXT NOT NULL DEFAULT 'USD',
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER
);

CREATE TABLE IF NOT EXISTS paper_positions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   INTEGER NOT NULL,
  symbol       TEXT NOT NULL,
  tf           TEXT,
  side         TEXT NOT NULL,          -- long | short
  qty          REAL NOT NULL,
  qty_open     REAL NOT NULL,
  entry_price  REAL NOT NULL,
  entry_time   INTEGER NOT NULL,
  sl           REAL,
  tp1          REAL,
  tp2          REAL,
  tp3          REAL,
  trail        REAL,
  tp_hits      INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'open',
  exit_price   REAL,
  exit_time    INTEGER,
  pnl          REAL NOT NULL DEFAULT 0,
  pnl_pct      REAL NOT NULL DEFAULT 0,
  r_multiple   REAL,
  exit_reason  TEXT,
  origin       TEXT NOT NULL DEFAULT 'manual',
  gate_verdict TEXT,
  gate_score   REAL,
  meta         TEXT                     -- JSON
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON paper_positions (account_id, status);

CREATE TABLE IF NOT EXISTS paper_fills (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  account_id  INTEGER NOT NULL,
  time        INTEGER NOT NULL,
  side        TEXT NOT NULL,            -- buy | sell
  qty         REAL NOT NULL,
  price       REAL NOT NULL,
  fee         REAL NOT NULL DEFAULT 0,
  reason      TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY,
  ts      INTEGER NOT NULL,
  level   TEXT NOT NULL,
  source  TEXT NOT NULL,
  message TEXT NOT NULL,
  data    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS scripts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  code        TEXT NOT NULL,
  is_example  INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS backtests (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol     TEXT NOT NULL,
  tf         TEXT NOT NULL,
  params     TEXT,
  metrics    TEXT,
  created_at INTEGER NOT NULL
);
