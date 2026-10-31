-- 0002 · Pending entry orders of the demo account (limit / stop), filled from the live feed by app.sim.paper.
-- Time columns: *_time = unix seconds; *_at = unix milliseconds.
CREATE TABLE IF NOT EXISTS paper_orders (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   INTEGER NOT NULL,
  symbol       TEXT NOT NULL,
  tf           TEXT,                          -- timeframe of the idea (gate and NEXUS levels at the fill)
  side         TEXT NOT NULL,                 -- long | short
  kind         TEXT NOT NULL,                 -- limit (touched: at that price or better) | stop (crossed: at market)
  price        REAL NOT NULL,                 -- limit price / stop trigger
  qty          REAL,                          -- fixed size; NULL = sized by risk % of equity at the fill
  risk_pct     REAL,                          -- NULL = risk.per_trade_pct at the fill
  sl           REAL,                          -- user stop; NULL = NEXUS stop relative to the fill price
  tp           REAL,                          -- user target; NULL = NEXUS targets relative to the fill price
  expiry       TEXT NOT NULL DEFAULT 'gtc',   -- gtc | bars | date
  expire_bars  INTEGER,                       -- expiry 'bars': number of bars of tf
  expire_time  INTEGER,                       -- unix s; NULL = good till cancelled
  status       TEXT NOT NULL DEFAULT 'pending', -- pending | filled | cancelled | expired
  origin       TEXT NOT NULL DEFAULT 'manual',
  gate_verdict TEXT,                          -- Trade Gate at placement (TAKE | WAIT | AVOID)
  gate_score   REAL,
  position_id  INTEGER,                       -- paper_positions.id once filled
  fill_price   REAL,
  fill_time    INTEGER,
  reason       TEXT,                          -- Spanish: why it was cancelled or expired
  cancelled_by TEXT,                          -- user | gate | risk | levels | system
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL,
  meta         TEXT                           -- JSON
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON paper_orders (account_id, status);
