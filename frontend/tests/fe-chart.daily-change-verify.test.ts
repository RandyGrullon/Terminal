// Regression (stale-daily-change, verifier round):
// - a quote fetched just now can still be a cached / stale price of an earlier UTC day (its `ts` is the real time of
//   the price, I2): its change is against that day's previous close, so it must not be shown as "Variación del día";
// - the candle fallback must compare with the bar right before the live one, not with the last candle loaded when
//   the page opened (after several live bars that one is hours old, while the label says "vela anterior").
import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { Candle, Quote } from '../src/api/types'
import { CANDLE_BASIS, DAY_BASIS, priceChange, quoteOfToday } from '../src/components/chart/dailyChange'

const D = Date.UTC(2026, 9, 30) // 30/10 00:00 UTC
const H = 3_600_000

function quote(price: number, change: number, ts: number): Quote {
  return { symbol: 'BINANCE:BTCUSDT', price, change, change_pct: null, volume: null, high: null, low: null, open: null, ts }
}

const candles: Candle[] = [
  { time: (D + 9 * H) / 1000, open: 0, high: 0, low: 0, close: 100, volume: 0 },
  { time: (D + 10 * H) / 1000, open: 0, high: 0, low: 0, close: 104, volume: 0 },
]

test('a quote fetched now whose price is from an earlier UTC day is not "the day"', () => {
  const now = D + 14 * H
  // DB-cache quote: price of 29/10 22:00 UTC (stale), change against the close of 28/10
  const stale = { ...quote(103, 3, D - 2 * H), stale: true }
  const r = priceChange({ price: 106, quote: stale, quoteAt: now - 1000, nowMs: now, candles, live: null })
  assert.equal(r?.basis, CANDLE_BASIS) // before: "Variación del día" +6 % against a close two sessions back
  assert.ok(Math.abs((r?.pct ?? 0) - (106 / 100 - 1) * 100) < 1e-9)

  // the same quote with a price of today is trusted
  const fresh = priceChange({ price: 106, quote: quote(103, 3, now - 5000), quoteAt: now - 1000, nowMs: now, candles, live: null })
  assert.equal(fresh?.basis, DAY_BASIS)
})

test('quote time in seconds or missing', () => {
  const now = D + 14 * H
  assert.equal(quoteOfToday({ ts: (now - 60_000) / 1000 }, now), true)
  assert.equal(quoteOfToday({ ts: (D - H) / 1000 }, now), false)
  assert.equal(quoteOfToday({ ts: 0 }, now), true) // judged by the fetch time only
  assert.equal(quoteOfToday({ ts: Number.NaN }, now), true)
})

test('candle fallback after several live bars: the bar before the live one', () => {
  const now = D + 14 * H
  // page loaded at 10:xx (last loaded bar 10:00, close 104); live bar is now 13:00, the 12:00 bar closed at 110
  const live = { time: (D + 13 * H) / 1000, prevClose: 110 }
  const r = priceChange({ price: 111, quote: null, quoteAt: null, nowMs: now, candles, live })
  assert.equal(r?.basis, CANDLE_BASIS)
  assert.ok(Math.abs((r?.pct ?? 0) - (111 / 110 - 1) * 100) < 1e-9) // before: against 104 (the 10:00 bar)

  // the first live bar after the load: the last loaded candle is the previous one
  const next = priceChange({ price: 105, quote: null, quoteAt: null, nowMs: now, candles, live: { time: (D + 11 * H) / 1000, prevClose: null } })
  assert.ok(Math.abs((next?.pct ?? 0) - (105 / 104 - 1) * 100) < 1e-9)

  // live tick on the last loaded bar: the one before it
  const same = priceChange({ price: 105, quote: null, quoteAt: null, nowMs: now, candles, live: { time: candles[1].time, prevClose: null } })
  assert.ok(Math.abs((same?.pct ?? 0) - (105 / 100 - 1) * 100) < 1e-9)
})
