// «Ver operación»: a demo position drawn on its chart. The pure helpers (lib/tradeView: next target, active stop,
// progress, projection, bar alignment), the strip above the chart (TradeFocusCard: open, closed, another market, an
// option) with its honest note, the chart's price lines (tradeViewLevels) and the store action that opens the chart on
// the trade. Same harness as broker-fe.test.mjs: real sources bundled in memory with esbuild, react-dom/server.
// Run: cd frontend && node --test tests/trade-view.test.mjs
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
export * from './lib/tradeView'
export { tradeViewLevels } from './lib/chart'
export { default as TradeFocusCard } from './components/chart/TradeFocusCard'
export { useApp } from './store'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'trade-view-entry.tsx' },
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

const pos = (over = {}) => ({
  id: 41, account_id: 3, symbol: 'BINANCE:ADAUSDT.P', tf: '15m', side: 'short', qty: 3758, qty_open: 3758,
  entry_price: 0.15727, entry_time: 1_794_672_000, sl: 0.15995, tp1: 0.15485, tp2: 0.1523, tp3: 0.14976, trail: null,
  tp_hits: 0, status: 'open', price: 0.1565, exit_price: null, exit_time: null, pnl: 2.5, pnl_pct: 0.4, r_multiple: null,
  exit_reason: null, exit_label: null, origin: 'autopilot', instrument: null, gate_verdict: 'TAKE', gate_score: 75,
  advice: null, warnings: [], ...over,
})

const render = (props) =>
  M.renderToStaticMarkup(M.createElement(M.TradeFocusCard, { chartSymbol: 'BINANCE:ADAUSDT.P', price: 0.1565, onShowChart() {}, onClose() {}, ...props }))

test('helpers: next target after the hits, the tighter dynamic stop, progress for a short and a long', () => {
  const t = M.tradeOf(pos())
  assert.equal(t.side, 'short')
  assert.deepEqual(M.nextTarget(t), { price: 0.15485, label: 'Objetivo TP1' })
  assert.deepEqual(M.nextTarget(M.tradeOf(pos({ tp_hits: 1 }))), { price: 0.1523, label: 'Objetivo TP2' })
  assert.equal(M.nextTarget(M.tradeOf(pos({ tp_hits: 3 }))), null)
  assert.deepEqual(M.activeStop(t), { price: 0.15995, label: 'Stop' })
  // a short's dynamic stop below its initial stop is tighter; above it, the initial stop still rules
  assert.deepEqual(M.activeStop(M.tradeOf(pos({ trail: 0.157 }))), { price: 0.157, label: 'Stop dinámico' })
  assert.equal(M.activeStop(M.tradeOf(pos({ trail: 0.161 }))).price, 0.15995)
  const p = M.tradeProgress(t, 0.1565)
  assert.ok(p.pnlPct > 0.48 && p.pnlPct < 0.5, 'a short wins when the price falls')
  assert.ok(p.toTargetPct > 1.05 && p.toTargetPct < 1.06)
  assert.ok(p.toStopPct > 2.2 && p.toStopPct < 2.21)
  const long = M.tradeProgress(M.tradeOf(pos({ side: 'long', sl: 90, tp1: 110, tp2: null, tp3: null, entry_price: 100 })), 105)
  assert.equal(long.pnlPct, 5)
  assert.ok(Math.abs(long.toTargetPct - (5 / 105) * 100) < 1e-9 && Math.abs(long.toStopPct - (15 / 105) * 100) < 1e-9)
  // an option is not drawn (its levels are premiums), nor a position without prices
  assert.equal(M.tradeOf(pos({ instrument: { kind: 'option', name: 'X', multiplier: 100, contracts: 1, underlying: 'NASDAQ:AAPL' } })), null)
  assert.equal(M.tradeOf(pos({ entry_price: null })), null)
})

test('projection: two dashed lines from the last candle, 12 bars ahead, only while it is open', () => {
  const t = M.tradeOf(pos())
  const proj = M.projection(t, { time: 1000, price: 0.1565 }, 900)
  assert.deepEqual(proj.target, [{ time: 1000, value: 0.1565 }, { time: 1000 + 12 * 900, value: 0.15485 }])
  assert.deepEqual(proj.stop, [{ time: 1000, value: 0.1565 }, { time: 1000 + 12 * 900, value: 0.15995 }])
  assert.equal(M.PROJECTION_BARS, 12)
  assert.equal(M.projection(M.tradeOf(pos({ status: 'closed', exit_price: 0.156, exit_time: 2000 })), { time: 1000, price: 1 }, 900), null)
  assert.equal(M.projection(t, null, 900), null)
  // bar alignment: a time inside a bar is drawn on that bar; before the first one there is none
  assert.equal(M.barAt([100, 200, 300], 250), 200)
  assert.equal(M.barAt([100, 200, 300], 300), 300)
  assert.equal(M.barAt([100, 200, 300], 50), null)
  assert.equal(M.barSpacing([100, 200, 400]), 200)
  assert.equal(M.barSpacing([100]), null)
})

test('chart levels: entry, «hasta aquí» stop, targets with their hits; a closed trade shows its exit', () => {
  const open = M.tradeViewLevels(M.tradeOf(pos({ tp_hits: 1, trail: 0.157 })))
  assert.deepEqual(open.map((l) => l.title), ['ENTRADA CORTO', 'SL · HASTA AQUÍ', 'TP1 ✓', 'TP2', 'TP3', 'STOP DINÁMICO'])
  const closed = M.tradeViewLevels(M.tradeOf(pos({ status: 'closed', exit_price: 0.1561, exit_time: 1_794_673_000 })))
  assert.deepEqual(closed.map((l) => [l.title, l.price]), [['ENTRADA CORTO', 0.15727], ['SALIDA', 0.1561]])
})

test('strip: an open trade with where it is, its target and its stop, and the honest note', () => {
  const html = render({ position: pos() })
  assert.ok(html.includes('>Corto<') && html.includes('ADAUSDT.P') && html.includes('NEXUS automática') && html.includes('abierta'), html)
  assert.ok(html.includes('Entrada') && html.includes('Ahora') && html.includes('Objetivo TP1') && html.includes('faltan'), html)
  assert.ok(html.includes('Stop') && html.includes('a 2,2'), html)
  assert.ok(html.includes('lo que llegue antes') && html.includes('no una predicción'), 'never a promise of where it ends')
})

test('strip: a closed trade, another market on the chart, an option', () => {
  const closed = render({ position: pos({ status: 'closed', exit_price: 0.1561, exit_time: 1_794_673_000, r_multiple: 0.44,
    exit_label: 'Cierre manual', account_id: 1 }) })
  assert.ok(closed.includes('cerrada') && closed.includes('Salida') && closed.includes('+0,44R') && closed.includes('Cierre manual'), closed)
  assert.ok(closed.includes('Mi cuenta demo') && !closed.includes('lo que llegue antes'), closed)
  const other = render({ position: pos(), chartSymbol: 'BINANCE:BTCUSDT' })
  assert.ok(other.includes('estás viendo otro mercado') && other.includes('Volver a su gráfico'), other)
  const option = render({ position: pos({ instrument: { kind: 'option', name: 'AAPL 230 P', multiplier: 100, contracts: 1, underlying: 'NASDAQ:AAPL' } }) })
  assert.ok(option.includes('opción teórica'), option)
})

test('store: «Ver operación» opens the chart on the trade (an option on its stock) and can hide it', () => {
  M.useApp.getState().showTrade(pos())
  let st = M.useApp.getState()
  assert.equal(st.page, 'chart')
  assert.equal(st.symbol, 'BINANCE:ADAUSDT.P')
  assert.equal(st.tf, '15m')
  assert.equal(st.trade.id, 41)
  M.useApp.getState().showTrade(pos({ id: 42, symbol: 'OPT:NASDAQ:AAPL:20261218:P:230', tf: '1h',
    instrument: { kind: 'option', name: 'AAPL 230 P', multiplier: 100, contracts: 1, underlying: 'NASDAQ:AAPL' } }))
  st = M.useApp.getState()
  assert.equal(st.symbol, 'NASDAQ:AAPL')
  M.useApp.getState().setTrade(null)
  assert.equal(M.useApp.getState().trade, null)
})
