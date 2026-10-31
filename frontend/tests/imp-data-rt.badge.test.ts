// data-rt: the freshness badge markup of the chart toolbar and the "Tiempo real" settings group (every realtime.*
// setting of backend/app/settings_ext/realtime.py has a Spanish field with limits). The fake browser of the fe-shell
// tests is loaded first: the API modules imported by the badge read window / storage when evaluated.
import './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QuoteFreshnessBadge } from '../src/components/chart/QuoteFreshness'
import { FIELD_BY_KEY, GROUPS } from '../src/components/settings/schema'
import { validateField } from '../src/components/settings/validate'
import { describeFreshness } from '../src/components/markets/freshness'

const NOW = Date.UTC(2026, 9, 29, 15, 0)

test('badge markup: label, tone class, tooltip and accessible name', () => {
  const f = describeFreshness({
    symbol: 'NASDAQ:AAPL',
    quote: { symbol: 'NASDAQ:AAPL', price: 190.5, change_pct: null, change: null, volume: null, high: null, low: null, open: null, ts: NOW, source: 'tradingview', delay_s: 900 },
    quoteAt: NOW,
    live: null,
    candles: null,
    tf: '1h',
    nowMs: NOW,
  })
  const html = renderToStaticMarkup(h(QuoteFreshnessBadge, { freshness: f }))
  assert.match(html, /class="badge badge-warn chart-fresh"/)
  assert.match(html, /Retraso 15 min · TradingView/)
  assert.match(html, /aria-label="Origen del precio: Retraso 15 min · TradingView"/)
  assert.match(html, /title="TradingView anuncia un retraso de 15 min/)
  assert.equal(renderToStaticMarkup(h(QuoteFreshnessBadge, { freshness: null })), '')
})

test('the realtime settings group covers every backend default with limits', () => {
  const keys = [
    'realtime.enabled',
    'realtime.twelvedata_credits_min',
    'realtime.twelvedata_credits_day',
    'realtime.polygon_calls_min',
    'realtime.reserve_pct',
    'realtime.max_symbols',
    'realtime.min_interval_s',
  ]
  const group = GROUPS.find((g) => g.id === 'realtime')
  assert.ok(group, 'grupo Tiempo real')
  assert.deepEqual(group.fields.map((f) => f.key).sort(), [...keys].sort())
  assert.match(group.description, /Twelve Data Basic \(gratis\): tiempo real/)
  assert.match(group.description, /retraso que anuncia/)
  for (const key of keys) {
    const def = FIELD_BY_KEY[key]
    assert.ok(def && def.label && def.help, key)
    if (def.kind !== 'bool') assert.ok(typeof def.min === 'number' && typeof def.max === 'number', `${key} limits`)
  }
  assert.equal(validateField(FIELD_BY_KEY['realtime.twelvedata_credits_min'], 0), 'Debe estar entre 1 y 100.000')
  assert.equal(validateField(FIELD_BY_KEY['realtime.twelvedata_credits_day'], 0), null) // 0 = no daily limit
  assert.equal(validateField(FIELD_BY_KEY['realtime.reserve_pct'], 95), 'Debe estar entre 0 y 90')
})
