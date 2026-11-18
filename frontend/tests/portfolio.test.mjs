// «Cartera» of NEXUS automática (PortfolioCard): one row per asset of the buy-and-hold basket, held (quantity, purchase,
// price, value, gain) or waiting with its reason (a stock waits for its market), any holding outside the basket, the
// totals, the asset kinds, the «Vender» button and the honest note. Same harness as broker-fe.test.mjs.
// Run: cd frontend && node --test tests/portfolio.test.mjs
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
export { default as PortfolioCard, portfolioRows, assetKind, assetName } from './components/paper/PortfolioCard'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`
const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'portfolio-entry.tsx' },
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

const hold = (symbol, qty, entry, price, pnl, over = {}) => ({
  id: Math.floor(Math.random() * 1e6), account_id: 3, symbol, tf: '1d', side: 'long', qty, qty_open: qty, entry_price: entry,
  entry_time: 1, sl: entry / 2, tp1: null, tp2: null, tp3: null, trail: null, status: 'open', price, exit_price: null,
  exit_time: null, pnl, pnl_pct: 0, r_multiple: null, exit_reason: null, exit_label: null, origin: 'autopilot',
  gate_verdict: null, gate_score: null, advice: null, warnings: [], hold: true, ...over,
})
const basket = [{ symbol: 'AMEX:SPY', weight: 25 }, { symbol: 'NASDAQ:AAPL', weight: 10 }, { symbol: 'BINANCE:BTCUSDT', weight: 10 }]

test('rows and totals: held, waiting and a holding outside the basket', () => {
  const holdings = [hold('BINANCE:BTCUSDT', 0.002, 50000, 55000, 9.9), hold('NASDAQ:MSFT', 0.1, 400, 380, -2.1)]
  const { rows, cost, value, gain } = M.portfolioRows(basket, holdings)
  assert.deepEqual(rows.map((r) => [r.symbol, r.weight, !!r.position]), [
    ['AMEX:SPY', 25, false], ['NASDAQ:AAPL', 10, false], ['BINANCE:BTCUSDT', 10, true], ['NASDAQ:MSFT', null, true],
  ])
  assert.equal(cost, 0.002 * 50000 + 0.1 * 400)
  assert.equal(value, 0.002 * 55000 + 0.1 * 380)
  assert.ok(Math.abs(gain - 7.8) < 1e-9)
  assert.deepEqual(['AMEX:SPY', 'NASDAQ:AAPL', 'BINANCE:BTCUSDT'].map(M.assetKind), ['ETF', 'Acción', 'Cripto'])
  assert.equal(M.assetName('BINANCE:BTCUSDT'), 'BTC')
})

test('card: what it holds with its gain, why the rest waits, «Vender» and the honest note', () => {
  const noop = () => {}
  const html = M.renderToStaticMarkup(M.createElement(M.PortfolioCard, {
    basket, holdPct: 50, stopPct: 50, holdings: [hold('BINANCE:BTCUSDT', 0.002, 50000, 55000, 9.9)], closingId: null, onSell: noop, onShow: noop,
  }))
  assert.ok(html.includes('Cartera · comprar y mantener') && html.includes('50 %') && html.includes('cae un 50 %'), html)
  assert.ok(html.includes('BTC') && html.includes('Cripto') && html.includes('110,00 $') && html.includes('+9,90 $'), html)
  assert.ok(html.includes('Invertido') && html.includes('100,00 $') && html.includes('Vale ahora'), html)
  assert.ok(html.includes('cuando abra la bolsa de EE. UU.') && html.includes('Vender'), html)
  assert.ok(html.includes('no garantiza ganancias'), html)
  const off = M.renderToStaticMarkup(M.createElement(M.PortfolioCard, { basket, holdPct: 0, stopPct: 50, holdings: [], closingId: null, onSell: noop, onShow: noop }))
  assert.ok(off.includes('Apagada') && off.includes('Cartera apagada'), off)
})
