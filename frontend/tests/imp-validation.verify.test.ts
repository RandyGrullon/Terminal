// ValidationCard, verifier checks: the data source / data notes are shown in Spanish (a synthetic or equivalent-pair
// series is never presented as plain exchange data), and a quick job that ended before the card saw it running
// still refreshes the report shown.
import './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ValidationReportView, dataSource, finishedUnseen } from '../src/components/brain/ValidationCard'
import fixture from './imp-validation.fixture.json'

/* eslint-disable @typescript-eslint/no-explicit-any */
const clone = (x: unknown): any => JSON.parse(JSON.stringify(x))
const text = (html: string) =>
  html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')

const SIM_NOTE = 'Mercado sintético: datos simulados, no son precios reales'

test('data source: provider plus the kind of history, in Spanish', () => {
  assert.equal(dataSource({ provider: 'binance', history: 'full' }), 'binance')
  assert.equal(dataSource({ provider: 'synthetic', history: 'synthetic' }), 'mercado sintético')
  assert.equal(dataSource({ provider: 'recorder', history: 'recorder' }), 'grabado localmente')
  assert.equal(dataSource({ provider: 'twelvedata', history: 'keyed' }), 'twelvedata · proveedor con clave')
  assert.equal(dataSource({ provider: null, history: null }), '')
})

test('a series with a data note shows it (row tag and details), never the raw English provider id', () => {
  const rep = clone(fixture)
  const s = rep.series[0]
  s.note = SIM_NOTE
  const closed = text(renderToStaticMarkup(h(ValidationReportView, { report: rep })))
  assert.ok(closed.includes('datos simulados'), 'the row says the data are simulated')
  const open = text(renderToStaticMarkup(h(ValidationReportView, { report: rep, open: `${s.symbol}|${s.tf}` })))
  assert.ok(open.includes(SIM_NOTE) && open.includes('mercado sintético · comisión'))
  assert.ok(!/\bsynthetic\b/.test(open), 'no English provider id in the visible text')
  const other = clone(fixture)
  other.series[0].history = 'full'
  other.series[0].provider = 'kraken'
  other.series[0].note = 'Datos de BINANCE:BTCUSDT vía Binance (par equivalente)'
  const t = text(renderToStaticMarkup(h(ValidationReportView, { report: other, open: `${s.symbol}|${s.tf}` })))
  assert.ok(t.includes('aviso sobre los datos') && t.includes('(par equivalente)') && t.includes('kraken · comisión'))
  const plain = text(renderToStaticMarkup(h(ValidationReportView, { report: fixture as any })))
  assert.ok(!plain.includes('aviso sobre los datos') && !plain.includes('datos simulados'))
})

test('a job that ended unseen refreshes the report once; a seen or older one does not', () => {
  const shown = { started_at: 1_000 } as any
  const done = (startedAt: number, extra: Record<string, unknown> = {}) =>
    ({ running: false, state: 'done', job: { id: 2, state: 'done', started_at: startedAt }, defaults: {}, ...extra }) as any
  assert.equal(finishedUnseen(done(5_000), shown, false), true) // newer job than the report shown
  assert.equal(finishedUnseen(done(5_000), null, false), true) // nothing shown yet
  assert.equal(finishedUnseen(done(1_000), shown, false), false) // the report shown is this job's
  assert.equal(finishedUnseen(done(5_000), shown, true), false) // the polling saw it running and reloads it itself
  assert.equal(finishedUnseen({ ...done(5_000), running: true }, shown, false), false)
  const failed = { running: false, state: 'error', job: { id: 2, state: 'error', started_at: 5_000 }, defaults: {} } as any
  assert.equal(finishedUnseen(failed, shown, false), false) // no new report was stored
  assert.equal(finishedUnseen({ running: false, state: 'idle', job: null, defaults: {} } as any, shown, false), false)
  assert.equal(finishedUnseen(null, shown, false), false)
})
