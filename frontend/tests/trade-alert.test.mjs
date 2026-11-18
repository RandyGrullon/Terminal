// Side cards of NEXUS automática's trades (lib/tradeAlert + TradeAlerts): the first snapshot only learns what is open, a
// new id is a purchase (long) or a sale (short) with its amount, collateral, money at the next target and at the stop
// (before costs, never an "expected" profit), an id that moves to the history is a close with its real result; the card
// and the desktop notification text. Same harness as broker-fe.test.mjs.
// Run: cd frontend && node --test tests/trade-alert.test.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')

const memory = new Map()
globalThis.window ??= globalThis
globalThis.addEventListener ??= () => {}
globalThis.removeEventListener ??= () => {}
globalThis.navigator ??= { userAgent: 'node', language: 'es-ES' }
globalThis.location ??= { hash: '', origin: 'http://localhost', href: 'http://localhost/' }
globalThis.history ??= { state: null, replaceState() {}, pushState() {} }
globalThis.localStorage ??= {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null),
  setItem: (k, v) => void memory.set(k, String(v)),
  removeItem: (k) => void memory.delete(k),
}

const ENTRY = `
export * from './lib/tradeAlert'
export { TradeAlertCard, alertText, ALERT_NOTE } from './components/paper/TradeAlerts'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`
const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'trade-alert-entry.tsx' },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  jsx: 'automatic',
  loader: { '.css': 'empty' },
  define: { 'import.meta.env.VITE_API_BASE': 'undefined', 'process.env.NODE_ENV': '"production"' },
  logLevel: 'silent',
})
const M = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

const perp = (over = {}) => ({
  id: 7, account_id: 3, symbol: 'BINANCE:BTCUSDT.P', tf: '5m', side: 'short', qty: 0.012, qty_open: 0.012,
  entry_price: 67000, entry_time: 1_794_700_000, sl: 67800, tp1: 66000, tp2: 65000, tp3: 64000, trail: null, tp_hits: 0,
  status: 'open', price: 67000, exit_price: null, exit_time: null, pnl: 0, pnl_pct: 0, r_multiple: null, exit_reason: null,
  exit_label: null, origin: 'autopilot', gate_verdict: 'TAKE', gate_score: 72, advice: null, warnings: [],
  instrument: { kind: 'future', root: 'BTCUSDT.P', name: 'Perpetuo BTC/USDT', multiplier: 0.001, contracts: 12, margin: 6.7,
    perpetual: true, leverage: 10, liquidation: 73700 },
  ...over,
})
const spot = (over = {}) => perp({ id: 8, symbol: 'BINANCE:XRPUSDT', side: 'long', qty: 500, qty_open: 500, entry_price: 1,
  sl: 0.98, tp1: 1.03, tp2: null, tp3: null, tf: '1h', instrument: null, ...over })

test('diff: the first snapshot only learns; a new id opens, an id in the history closes', () => {
  let r = M.diffSnapshot(null, [perp()], [])
  assert.deepEqual([...r.known], [7])
  assert.deepEqual(r.alerts, [])
  r = M.diffSnapshot(r.known, [perp(), spot()], [])
  assert.deepEqual(r.alerts.map((a) => [a.kind, a.action, a.ticker]), [['open', 'COMPRA', 'XRPUSDT']])
  const closed = perp({ status: 'closed', qty_open: 0, exit_price: 66000, exit_time: 1_794_707_200, pnl: 11.4, r_multiple: 1.19,
    exit_label: 'Objetivo TP1' })
  r = M.diffSnapshot(r.known, [spot()], [closed])
  assert.deepEqual(r.alerts.map((a) => [a.kind, a.action, a.pnl, a.r, a.durationS]), [['close', 'COMPRA', 11.4, 1.19, 7200]])
  assert.deepEqual([...r.known], [8])
})

test('open: a short perpetual is a sale with its amount, collateral and money at target and stop', () => {
  const a = M.openAlert(perp())
  assert.equal(a.action, 'VENTA')
  assert.equal(a.product, 'Perpetuo 10x')
  assert.equal(a.speed, '5m · rápida')
  assert.ok(Math.abs(a.notional - 804) < 1e-9 && Math.abs(a.margin - 80.4) < 1e-9)
  assert.equal(a.target.label, 'TP1')
  assert.ok(Math.abs(a.target.money - 12) < 1e-9, 'a short gains when the price falls to TP1')
  assert.ok(Math.abs(a.stop.money + 9.6) < 1e-9, 'and loses at its stop above')
  // a reached target moves on, and a tighter dynamic stop is the one that closes it
  const later = M.openAlert(perp({ tp_hits: 1, trail: 67300 }))
  assert.equal(later.target.label, 'TP2')
  assert.equal(later.stop.price, 67300)
  assert.equal(M.openAlert(spot()).product, 'Cripto al contado')
  assert.equal(M.productOf(spot({ symbol: 'NASDAQ:AAPL' })), 'Acción o ETF')
  assert.equal(M.productOf(spot({ symbol: 'CAPITALCOM:TSLA' })), 'CFD')
  assert.equal(M.productOf(spot({ instrument: { kind: 'option', name: 'AAPL 230 P', multiplier: 100, contracts: 1,
    underlying: 'NASDAQ:AAPL', right: 'put' } })), 'Opción put sobre AAPL')
})

test('card and notification: what was bought, the two outcomes and the honest note; a close with its result', () => {
  const noop = () => {}
  const html = M.renderToStaticMarkup(M.createElement(M.TradeAlertCard, { alert: M.openAlert(spot()), onClose: noop, onShow: noop }))
  assert.ok(html.includes('COMPRA') && html.includes('XRPUSDT') && html.includes('Cripto al contado') && html.includes('1h · medida'), html)
  assert.ok(html.includes('Monto') && html.includes('500,00 $') && html.includes('Si llega a TP1') && html.includes('+15,00 $'), html)
  assert.ok(html.includes('Si toca el stop') && html.includes('-10,00 $') && html.includes('No es una predicción'), html)
  assert.ok(html.includes('Ver operación'), html)
  const closed = M.closeAlert(perp({ status: 'closed', qty_open: 0, exit_price: 66000, exit_time: 1_794_707_200, pnl: 11.4,
    r_multiple: 1.19, exit_label: 'Objetivo TP1' }))
  const done = M.renderToStaticMarkup(M.createElement(M.TradeAlertCard, { alert: closed, onClose: noop, onShow: noop }))
  assert.ok(done.includes('COMPRA') && done.includes('cierra el corto') && done.includes('+11,40 $') && done.includes('+1,19R'), done)
  assert.ok(done.includes('Objetivo TP1') && done.includes('duró'), done)
  const n = M.alertText(M.openAlert(perp()))
  assert.equal(n.title, 'NEXUS · VENTA BTCUSDT.P')
  assert.ok(n.body.includes('garantía 80,40 $') && n.body.includes('objetivo +12,00 $') && n.body.includes('stop -9,60 $'), n.body)
  assert.ok(!/esperad/i.test(n.body + M.ALERT_NOTE), 'never an "expected" profit')
})
