// Regression (stale-daily-change): "Variación del día" divided the live price by the previous close of a quote read
// once per symbol, so after the session rolled over (00:00 UTC for crypto) it showed a two-session change, even with
// the wrong sign. The page now re-reads the quote (every 2 min and right after 00:00 UTC) and only trusts a snapshot
// of the current UTC day; otherwise it falls back to the previous candle and says so.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { Candle, Quote } from '../src/api/types'
import {
  CANDLE_BASIS,
  DAY_BASIS,
  QUOTE_MAX_AGE_MS,
  msToNextUtcDay,
  priceChange,
  quoteIsCurrent,
} from '../src/components/chart/dailyChange'

const D = Date.UTC(2026, 9, 30) // 30/10 00:00 UTC

function quote(price: number, change: number): Quote {
  return { symbol: 'BINANCE:BTCUSDT', price, change, change_pct: null, volume: null, high: null, low: null, open: null, ts: 0 }
}

const candles: Candle[] = [
  { time: D / 1000 + 22 * 3600, open: 0, high: 0, low: 0, close: 104, volume: 0 },
  { time: D / 1000 + 23 * 3600, open: 0, high: 0, low: 0, close: 105, volume: 0 },
]

test('a fresh quote of the current UTC day gives the change against the previous session close', () => {
  const now = D + 14 * 3_600_000
  const r = priceChange({ price: 103, quote: quote(102, 2), quoteAt: now - 60_000, nowMs: now, candles, live: null })
  assert.equal(r?.basis, DAY_BASIS)
  assert.ok(Math.abs((r?.pct ?? 0) - 3) < 1e-9) // previous close 100
})

test('after 00:00 UTC a snapshot of the previous day is not used as "the day"', () => {
  // read at 23:59 UTC (previous close = D-1's close = 100), shown at 00:01 UTC of the next day
  const fetched = D + 86_400_000 - 60_000
  const now = D + 86_400_000 + 60_000
  const r = priceChange({ price: 106, quote: quote(103, 3), quoteAt: fetched, nowMs: now, candles, live: null })
  // before the fix: "Variación del día" +6 % (against D-1, two sessions back)
  assert.equal(r?.basis, CANDLE_BASIS)
  assert.ok(Math.abs((r?.pct ?? 0) - (106 / 104 - 1) * 100) < 1e-9)
})

test('a snapshot that could not be refreshed for a long time stops defining the day', () => {
  const now = D + 20 * 3_600_000
  assert.equal(quoteIsCurrent(now - QUOTE_MAX_AGE_MS, now), true)
  assert.equal(quoteIsCurrent(now - QUOTE_MAX_AGE_MS - 1, now), false)
  assert.equal(quoteIsCurrent(null, now), false)
  const r = priceChange({ price: 106, quote: quote(103, 3), quoteAt: now - QUOTE_MAX_AGE_MS - 1, nowMs: now, candles, live: null })
  assert.equal(r?.basis, CANDLE_BASIS)
})

test('live bar newer than the loaded candles: the reference is the last loaded close', () => {
  const now = D + 14 * 3_600_000
  const live = { time: candles[1].time + 3600 }
  const r = priceChange({ price: 110, quote: null, quoteAt: null, nowMs: now, candles, live })
  assert.equal(r?.basis, CANDLE_BASIS)
  assert.ok(Math.abs((r?.pct ?? 0) - (110 / 105 - 1) * 100) < 1e-9)
  assert.equal(priceChange({ price: null, quote: null, quoteAt: null, nowMs: now, candles, live: null }), null)
})

test('the rollover re-read is scheduled for the next 00:00 UTC', () => {
  assert.equal(msToNextUtcDay(D), 86_400_000)
  assert.equal(msToNextUtcDay(D + 86_400_000 - 1000), 1000)
  assert.equal(msToNextUtcDay(D + 20 * 3_600_000), 4 * 3_600_000)
})
