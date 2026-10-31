// data-rt: what the chart's price-freshness badge says. Honesty rules: "Tiempo real" only when the source says so
// for the price on screen, the announced / measured delay otherwise ("Retraso 15 min · TradingView"), the last price
// of a closed market is "Mercado cerrado", and an old price is "Desactualizado · hace X". Pure: no network, no DOM.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { CandlesResponse, RealtimeQuote, RealtimeStatus } from '../src/api/types'
import {
  KEYED_LIVE_MAX_S,
  LIVE_INFO_MS,
  QUOTE_INFO_MS,
  REALTIME_MAX_AGE_MS,
  agoText,
  describeFreshness,
  type FreshnessInput,
} from '../src/components/markets/freshness'

const NOW = Date.UTC(2026, 9, 29, 15, 0) // Thursday 15:00 UTC (US session open)

function quote(extra: Partial<RealtimeQuote>, symbol = 'NASDAQ:AAPL'): RealtimeQuote {
  return { symbol, price: 190.5, change_pct: 0.4, change: 0.76, volume: null, high: null, low: null, open: null, ts: NOW - 20_000, ...extra }
}

function input(extra: Partial<FreshnessInput>): FreshnessInput {
  return { symbol: 'NASDAQ:AAPL', quote: null, quoteAt: null, live: null, candles: null, tf: '1h', nowMs: NOW, status: null, ...extra }
}

const STATUS: RealtimeStatus = {
  enabled: true,
  offline: false,
  providers: [
    {
      id: 'twelvedata', label: 'Twelve Data', configured: true, live: true, state: 'activo', detail: '',
      plan: 'Plan Basic (gratis): tiempo real para acciones y ETF de EE. UU., forex y cripto.', doc_url: 'https://twelvedata.com/pricing',
      credits_min: 8, used_min: 2, credits_day: 800, used_day: 120,
    },
  ],
  symbols: [{ symbol: 'NASDAQ:AAPL', provider: 'twelvedata', interval_s: 36.4, last_at: NOW - 5000, ts: NOW - 20_000, realtime: true, delay_s: null, market_open: true, next_in_s: 30 }],
  polls: 12,
  errors: 0,
  last_error: null,
  last_poll_at: NOW - 5000,
}

test('keyed real-time quote: "Tiempo real · Twelve Data", plan and cadence in the tooltip', () => {
  const f = describeFreshness(input({ quote: quote({ source: 'twelvedata', realtime: true, market_open: true }), quoteAt: NOW - 1000, status: STATUS }))
  assert.equal(f?.label, 'Tiempo real · Twelve Data')
  assert.equal(f?.tone, 'pos')
  assert.match(f?.title ?? '', /Plan Basic/)
  assert.match(f?.title ?? '', /cada ~36 s/)
})

test('a keyed price polled a while ago shows its age instead of "Tiempo real"', () => {
  const f = describeFreshness(input({ quote: quote({ source: 'twelvedata', realtime: true, market_open: true, ts: NOW - (KEYED_LIVE_MAX_S + 30) * 1000 }), quoteAt: NOW - 1000, status: STATUS }))
  assert.equal(f?.label, 'Twelve Data · hace 3 min')
  assert.equal(f?.kind, 'recent')
  assert.match(f?.title ?? '', /cada ~36 s/)
})

test('delayed TradingView data is never shown as live, and a key is suggested', () => {
  const f = describeFreshness(input({ quote: quote({ source: 'tradingview', delay_s: 900 }), quoteAt: NOW - 1000, status: STATUS }))
  assert.equal(f?.label, 'Retraso 15 min · TradingView')
  assert.equal(f?.tone, 'warn')
  assert.match(f?.title ?? '', /anuncia un retraso de 15 min/)
  assert.match(f?.title ?? '', /Twelve Data/)
  // the live WS candles say the same (a recorder symbol fed by TradingView snapshots)
  const live = describeFreshness(input({ live: { source: 'tradingview', realtime: false, delay_s: 600, at: NOW - 3000 } }))
  assert.equal(live?.label, 'Retraso 10 min · TradingView')
})

test('the live feed source wins over an older quote (crypto on its exchange)', () => {
  const f = describeFreshness(
    input({
      symbol: 'BINANCE:BTCUSDT',
      quote: quote({ source: 'tradingview', realtime: true }, 'BINANCE:BTCUSDT'),
      quoteAt: NOW - 20_000,
      live: { source: 'binance', realtime: true, at: NOW - 2000 },
    }),
  )
  assert.equal(f?.label, 'Tiempo real · Binance')
  // a live update that stopped long ago no longer decides: the quote does
  const old = describeFreshness(input({ quote: quote({ source: 'tradingview', delay_s: 900 }), quoteAt: NOW - 1000, live: { source: 'twelvedata', realtime: true, at: NOW - LIVE_INFO_MS - 1 } }))
  assert.equal(old?.label, 'Retraso 15 min · TradingView')
})

test('closed market and outdated prices', () => {
  const closed = describeFreshness(input({ quote: quote({ source: 'twelvedata', market_open: false, ts: NOW - 3 * 3600_000 }), quoteAt: NOW - 1000 }))
  assert.equal(closed?.label, 'Mercado cerrado · Twelve Data')
  assert.equal(closed?.tone, 'muted')
  assert.match(closed?.title ?? '', /hace 3 h/)
  // TradingView's ts is when it answered: its closed-market title gives no age
  const tvClosed = describeFreshness(input({ quote: quote({ source: 'tradingview', delay_s: 900, market_open: false, ts: NOW - 2000 }), quoteAt: NOW - 1000 }))
  assert.equal(tvClosed?.label, 'Mercado cerrado · TradingView')
  assert.doesNotMatch(tvClosed?.title ?? '', /hace/)
  const stale = describeFreshness(input({ quote: quote({ source: 'cache', stale: true, ts: NOW - 2 * 3600_000 }), quoteAt: NOW - 1000 }))
  assert.equal(stale?.label, 'Desactualizado · hace 2 h')
  assert.equal(stale?.tone, 'neg')
  // a "real-time" quote whose price stopped moving long ago (paused tab, feed down) is not presented as live
  const frozen = describeFreshness(input({ quote: quote({ source: 'twelvedata', realtime: true, ts: NOW - REALTIME_MAX_AGE_MS - 60_000 }), quoteAt: NOW - 1000 }))
  assert.equal(frozen?.label, 'Desactualizado · hace 6 min')
  // a quote read too long ago is ignored; stale candles still say so
  const candles: CandlesResponse = {
    symbol: 'NASDAQ:AAPL', tf: '1h', provider: 'recorder', history: 'recorder', stale: true,
    note: 'Datos desactualizados: última vela hace 3 días', candles: [{ time: NOW / 1000 - 3 * 86400 - 3600, open: 1, high: 1, low: 1, close: 1, volume: 0 }],
  }
  const old = describeFreshness(input({ quote: quote({ source: 'tradingview', delay_s: 900 }), quoteAt: NOW - QUOTE_INFO_MS - 1, candles }))
  assert.equal(old?.label, 'Desactualizado · hace 3 días')
  assert.equal(describeFreshness(input({})), null)
})

test('synthetic market and sources that announce nothing', () => {
  assert.equal(describeFreshness(input({ symbol: 'SIM:NEXUS' }))?.label, 'Sintético')
  const unknown = describeFreshness(input({ quote: quote({ source: 'tradingview' }), quoteAt: NOW - 1000 }))
  assert.equal(unknown?.label, 'TradingView')
  assert.match(unknown?.title ?? '', /no indica si es en tiempo real/)
  // an older server without the freshness fields: nothing is claimed
  assert.equal(describeFreshness(input({ quote: quote({}), quoteAt: NOW - 1000 })), null)
  assert.equal(agoText(45), '45 s')
  assert.equal(agoText(12 * 60), '12 min')
  assert.equal(agoText(3 * 3600), '3 h')
  assert.equal(agoText(86400 * 1.2), '29 h')
  assert.equal(agoText(86400 * 3), '3 días')
})
