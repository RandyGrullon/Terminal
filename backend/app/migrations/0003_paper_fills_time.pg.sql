-- 0003 · The demo account's daily loss limit (portfolio risk) sums the fills of the current UTC day.
CREATE INDEX IF NOT EXISTS idx_fills_time ON paper_fills (account_id, time);
