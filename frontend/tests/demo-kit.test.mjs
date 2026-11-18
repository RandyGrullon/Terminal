// Friendly building blocks of the demo page (components/paper/DemoKit): asset names and kinds (perpetuals, futures,
// options, CFDs), the money at stake, a trade's path from its stop to its next target (long and short, a dynamic stop,
// no target), the curve of the closed trades, the activity feed in plain words and the hero (winners as «x de n»).
// Same harness as portfolio.test.mjs. Run: cd frontend && node --test tests/demo-kit.test.mjs
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
export * from './components/paper/DemoKit'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`
const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'demo-kit-entry.tsx' },
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
const render = (C, props) => M.renderToStaticMarkup(M.createElement(C, props))

const NOW_S = Math.floor(Date.now() / 1000)
const pos = (over = {}) => ({
  id: 1, account_id: 3, symbol: 'BINANCE:ETHUSDT', tf: '1h', side: 'long', qty: 2, qty_open: 2, entry_price: 100,
  entry_time: NOW_S - 3600, sl: 90, tp1: 120, tp2: 130, tp3: 140, trail: null, tp_hits: 0, status: 'open', price: 110,
  exit_price: null, exit_time: null, pnl: 20, pnl_pct: 10, r_multiple: 1, exit_reason: null, exit_label: null,
  origin: 'autopilot', gate_verdict: null, gate_score: null, advice: null, warnings: [], ...over,
})

test('assets: names, kinds and avatars of every kind of position', () => {
  assert.deepEqual(['BINANCE:BTCUSDT', 'BINANCE:BTCUSDT.P', 'NASDAQ:AAPL', 'AMEX:SPY'].map(M.assetName), ['BTC', 'BTC', 'AAPL', 'SPY'])
  assert.deepEqual(['BINANCE:SOLUSDT.P', 'NASDAQ:AAPL', 'AMEX:QQQ'].map(M.assetKind), ['Cripto', 'Acción', 'ETF'])
  const perp = M.assetOf(pos({ symbol: 'BINANCE:SOLUSDT.P', instrument: { kind: 'future', perpetual: true, leverage: 10, name: 'Perpetuo SOL/USDT', multiplier: 1, contracts: 2 } }))
  assert.deepEqual(perp, { name: 'SOL', kind: 'Perpetuo 10×', avatar: 'SOL' })
  const fut = M.assetOf(pos({ symbol: 'CME_MINI:MES1!', instrument: { kind: 'future', root: 'MES', name: 'Micro E-mini S&P 500', multiplier: 5, contracts: 1, margin: 1500 } }))
  assert.equal(fut.name, 'MES')
  assert.equal(fut.kind, 'Futuro')
  const opt = M.assetOf(pos({ symbol: 'OPT:NYSE:F:20261218:P:12', instrument: { kind: 'option', name: 'F 12 P 18/12/2026', underlying: 'NYSE:F', right: 'put', multiplier: 100, contracts: 1 } }))
  assert.deepEqual(opt, { name: 'F 12 P 18/12/2026', kind: 'Opción', avatar: 'F' })
  assert.equal(M.assetOf(pos({ symbol: 'CAPITALCOM:TSLA' })).kind, 'CFD')
  const gold = M.assetOf(pos({ symbol: 'BINANCE:PAXGUSDT.P', instrument: { kind: 'future', perpetual: true, leverage: 10, name: 'Perpetuo PAXG/USDT', multiplier: 0.001, contracts: 3 } }))
  assert.deepEqual(gold, { name: 'PAXG', kind: 'Oro · perpetuo 10×', avatar: 'PAXG' })
  assert.equal(M.assetOf(pos({ symbol: 'BINANCE:PAXGUSDT' })).kind, 'Oro')
  assert.equal(M.assetColor('PAXG'), '#e6b422')
  assert.equal(M.assetColor('BTC'), '#f7931a')
  assert.equal(M.assetColor('ZZZ'), M.assetColor('zzz'), 'the same colour for the same name')
})

test('money at stake: the amount, a perpetual its guarantee, an option its premium', () => {
  assert.deepEqual(M.moneyOf(pos()).value, 200)
  const perp = M.moneyOf(pos({ instrument: { kind: 'future', perpetual: true, leverage: 10, multiplier: 1, contracts: 2, name: 'x' } }))
  assert.equal(perp.label, 'Garantía')
  assert.equal(perp.value, 20)
  assert.equal(M.moneyOf(pos({ instrument: { kind: 'option', name: 'x', multiplier: 100, contracts: 1 } })).label, 'Prima pagada')
  assert.equal(M.directionOf(pos({ side: 'short' })).label, 'Venta')
  assert.equal(M.directionOf(pos({ instrument: { kind: 'option', right: 'call', name: 'x', multiplier: 100, contracts: 1 } })).hint, 'Gana si la acción sube')
})

test('path: long and short between the stop and the next target, a dynamic stop, no target', () => {
  const long = M.tradePath(pos())
  assert.equal(long.target, 120)
  assert.ok(Math.abs(long.at - 20 / 30) < 1e-9 && Math.abs(long.entryAt - 10 / 30) < 1e-9)
  assert.ok(Math.abs(long.toTarget - (10 / 110) * 100) < 1e-9 && Math.abs(long.toStop - (20 / 110) * 100) < 1e-9)
  // TP1 reached: the next target is TP2; a tighter trailing stop replaces the stop
  const after = M.tradePath(pos({ tp_hits: 1, trail: 105, price: 125 }))
  assert.equal(after.target, 130)
  assert.equal(after.stop, 105)
  assert.equal(after.stopLabel, 'Stop dinámico')
  // short: the stop is above, the target below
  const short = M.tradePath(pos({ side: 'short', sl: 110, tp1: 80, tp2: null, tp3: null, price: 95 }))
  assert.equal(short.target, 80)
  assert.ok(Math.abs(short.at - 0.5) < 1e-9 && Math.abs(short.entryAt - 1 / 3) < 1e-9)
  // a holding (no targets): no bar, only its stop
  const hold = M.tradePath(pos({ tp1: null, tp2: null, tp3: null }))
  assert.equal(hold.target, null)
  assert.ok(render(M.PathBar, { path: hold }).includes('Sin objetivo fijo'))
  assert.equal(M.tradePath(pos({ price: null })), null, 'no price yet: nothing to draw')
  const bar = render(M.PathBar, { path: long })
  assert.ok(bar.includes('Le falta 9,1 % para el objetivo') && bar.includes('el stop está a 18,2 %'), bar)
})

test('curve and feed: closed trades oldest first, plain words newest first', () => {
  const closed = [
    pos({ id: 7, status: 'closed', exit_time: NOW_S - 100, pnl: -5, exit_reason: 'SL', exit_label: 'Stop' }),
    pos({ id: 6, status: 'closed', exit_time: NOW_S - 900, pnl: 12, symbol: 'BINANCE:BTCUSDT', exit_reason: 'TP1', exit_label: 'Objetivo 1' }),
  ]
  assert.deepEqual(M.pnlCurve(closed), [0, 12, 7])
  const items = M.activityOf([pos({ id: 9, entry_time: NOW_S - 50, hold: true, symbol: 'AMEX:SPY' })], closed, 10)
  assert.deepEqual(items.map((i) => i.title), ['Compró SPY para la cartera', 'Cerró ETH', 'Cerró BTC', 'Compró ETH', 'Compró BTC'])
  assert.equal(items[1].detail, 'Stop')
  assert.equal(items[1].tone, 'neg')
  assert.equal(M.activityOf([], closed, 2).length, 2, 'limited')
  const html = render(M.ActivityFeed, { items, onShow: () => {}, empty: 'nada' })
  assert.ok(html.includes('Cerró BTC') && html.includes('+12,00 $') && html.includes('-5,00 $'), html)
  assert.ok(render(M.ActivityFeed, { items: [], onShow: () => {}, empty: 'nada' }).includes('nada'))
})

test('MT5 lots: a trade shows its lots and what they hold; a holding keeps its units', () => {
  const gold = pos({ symbol: 'BINANCE:PAXGUSDT.P', qty: 5, qty_open: 5, lot: { lots: 0.05, size: 100, unit: 'oz' },
    instrument: { kind: 'future', perpetual: true, leverage: 10, name: 'Perpetuo PAXG/USDT', multiplier: 0.001, contracts: 5000 } })
  assert.equal(M.sizeOf(gold, 5), '0,05 lotes (5 oz)')
  assert.equal(M.sizeOf(pos({ qty: 1, qty_open: 1, lot: { lots: 1, size: 1, unit: 'ETH' } }), 1), '1,00 lote (1 ETH)')
  assert.equal(M.sizeOf(pos({ lot: { lots: 2, size: 1, unit: 'acción' } }), 2), '2,00 lotes (2 acciones)')
  assert.match(M.sizeOf(pos({ qty: 0.0164, lot: { lots: 0.0164, size: 1, unit: 'acción' } }), 0.0164), /^0,0164 lotes \(0,0164 acciones\)$/)
  assert.equal(M.sizeOf(pos({ hold: true, lot: { lots: 2, size: 1, unit: 'acción' } }), 2), '2', 'a holding: its units')
  assert.equal(M.sizeOf(pos(), 2), '2', 'an older server: no lot, its units')
  const tile = render(M.TradeTile, { p: gold, closingId: null, onClose: () => {}, onShow: () => {} })
  assert.ok(tile.includes('0,05 lotes (5 oz)') && tile.includes('1 lote = 100 oz'), tile)
  const [item] = M.activityOf([gold], [], 5)
  assert.ok(item.detail.startsWith('0,05 lotes (5 oz) a '), item.detail)
})

test('hero: the value in big, the change since the start, the winners as «x de n», never a conclusion below 30', () => {
  const account = { id: 3, name: 'NEXUS automática', balance: 98, equity: 101.5, initial_balance: 100, realized_pnl: -2,
    unrealized_pnl: 3.5, return_pct: 1.5, trades: 4, wins: 1, losses: 3, win_rate: 0.25, open_positions: 1, auto_trade: false,
    auto_symbols: [], auto_tf: '1h' }
  const html = render(M.AccountHero, { account, history: [], label: 'Valor de la cuenta' })
  assert.ok(html.includes('101,50 $') && html.includes('+1,50 $ (+1,50 %)') && html.includes('desde que empezó con 100,00 $'), html)
  assert.ok(html.includes('>1 de 4<') && html.includes('muy pocas para concluir') && html.includes('1 abierta<'), html)
  assert.ok(!/25(,0)? %/.test(html), 'no bare win rate')
  assert.ok(html.includes('La curva aparece cuando cierre al menos dos operaciones.'))
})
