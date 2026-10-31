// Regression tests of the int-fe-a integration (fix round, frontend part): scanner I8 presentation, backtest
// commission per asset class, honest I4 wording ("no habrían llegado a TP1", "x de n con resultado conocido", pp),
// sim.css variants instead of inline styles and the Monte Carlo synthetic "Símbolo" caption.
// Same harness as fe-trust.test.mjs: the real sources are bundled in memory with Vite's esbuild and rendered with
// react-dom/server; nothing is written to disk, no network.
// Run: cd frontend && node --test tests/int-fe-a.test.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')

// Browser globals touched at import time by store.ts / auth/session.ts / lightweight-charts (nothing is mounted).
const memory = new Map()
globalThis.window = globalThis
globalThis.addEventListener ??= () => {}
globalThis.removeEventListener ??= () => {}
globalThis.navigator ??= { userAgent: 'node', language: 'es-ES' }
globalThis.location ??= { hash: '' }
globalThis.history ??= { state: null, replaceState() {}, pushState() {} }
globalThis.localStorage ??= {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null),
  setItem: (k, v) => void memory.set(k, String(v)),
  removeItem: (k) => void memory.delete(k),
}

// Forms saved by the previous version: the backtest one always carried the global risk.fee_pct the form prefilled;
// the Monte Carlo one is in synthetic mode (server rendering reads the store's initial state, so it is set here).
const SIM_KEY = 'nexus.sim.ui.v1'
memory.set(
  SIM_KEY,
  JSON.stringify({
    tab: 'backtest',
    bt: { limit: 1500, initialCapital: 5000, riskPct: 1, feePct: 0.1, slippagePct: 0.02, useGate: true },
    mc: { mode: 'synthetic', paths: 30, bars: 500, seed: 1, initialCapital: 10000 },
  }),
)

const ENTRY = `
export * as scan from './components/scanner/scanUtils'
export { default as ScanTable } from './components/scanner/ScanTable'
export { BacktestForm, buildBacktestBody } from './components/sim/BacktestForm'
export { useSimStore, withFeeNote } from './components/sim/simStore'
export { MonteCarloView } from './components/sim/MonteCarloView'
export { SimPanel } from './components/sim/SimPanel'
export { GateEffect } from './components/sim/GateEffect'
export { default as GateLearningCard } from './components/brain/GateLearningCard'
export { default as StrategyPanel } from './components/studio/StrategyPanel'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'int-fe-a-entry.tsx' },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  jsx: 'automatic',
  loader: { '.css': 'empty' },
  define: { 'import.meta.env.VITE_API_BASE': 'undefined', 'process.env.NODE_ENV': '"production"' },
  logLevel: 'silent',
})
const BUNDLE_URL = `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
let M
try {
  M = await import(BUNDLE_URL)
} catch (err) {
  throw new Error(`could not load the test bundle: ${String(err?.stack ?? err).replaceAll(BUNDLE_URL, '<bundle>')}`)
}

const render = (Component, props) => M.renderToStaticMarkup(M.createElement(Component, props))
const noop = () => {}
const TAKE_BADGE = '</svg>TOMAR<' // VerdictBadge of a TOMAR ("NO TOMAR" does not match)

// ----------------------------------------------------------------------------- scanner (I8)

test('scanner: a gate TOMAR without the NEXUS signal is "Filtro favorable"; old runs keep the gate verdict', () => {
  const base = { score: 0.4, prob_up: 0.6, confidence: 0.5, deep: true, direction: 'long', gate_score: 72, reasons: [], action: 'BUY' }
  const rows = M.scan.normalizeRows([
    { ...base, rank: 1, symbol: 'A:REAL', verdict: 'TAKE', nexus_action: 'BUY', opportunity: true },
    { ...base, rank: 2, symbol: 'B:FAV', verdict: 'TAKE', nexus_action: 'WAIT', opportunity: false, gate_score: 80 },
    { ...base, rank: 3, symbol: 'C:OLD', verdict: 'TAKE', nexus_action: null, opportunity: null }, // stored before the field
    { ...base, rank: 4, symbol: 'D:ODD', verdict: 'WAIT', nexus_action: 'NOPE', opportunity: false },
  ])
  assert.deepEqual(rows.map((r) => r.nexus_action), ['BUY', 'WAIT', null, null], 'unknown actions are dropped')
  assert.deepEqual(rows.map((r) => M.scan.isOpportunity(r)), [true, false, true, false])
  assert.deepEqual(rows.map((r) => M.scan.isLegacyTake(r)), [false, false, true, false])
  assert.equal(M.scan.countByFilter(rows).take, 2, 'TOMAR tab: opportunity === true, or the old rule on old rows')

  const html = render(M.ScanTable, { rows, sort: { key: 'rank', dir: 'asc' }, onSort: noop, tf: '1h' })
  const cells = html.split('<tr').slice(2) // skip <thead> and its row
  assert.ok(cells[0].includes(TAKE_BADGE), 'a real opportunity is TOMAR')
  assert.match(cells[1], /Filtro favorable · sin señal NEXUS/)
  assert.match(cells[1], />80\/100</, 'with the gate score')
  assert.match(cells[1], /dice ESPERAR/, 'the tooltip names what NEXUS says')
  assert.ok(!cells[1].includes(TAKE_BADGE), 'no TOMAR badge on a favorable row')
  assert.ok(cells[2].includes(TAKE_BADGE), 'old run: shown as that run showed it')
  assert.match(cells[2], /Escaneo anterior a la comprobación de la señal NEXUS/)
  assert.doesNotMatch(cells[0], /Escaneo anterior/)
})

// ----------------------------------------------------------------------------- backtest commission per asset class

test('backtest: a stored v1 form drops the prefilled global commission; a typed one is kept (v2)', () => {
  const f = M.useSimStore.getState().btForm
  assert.equal(f.limit, 1500, 'the stored form was read')
  assert.equal(f.feePct, null, 'empty = the asset class commission')
  M.useSimStore.getState().patchBt({ feePct: 0.02 })
  const saved = JSON.parse(memory.get(SIM_KEY))
  assert.equal(saved.v, 2)
  assert.equal(saved.bt.feePct, 0.02)
  M.useSimStore.getState().patchBt({ feePct: null })
})

test('backtest: fee_pct is sent only when the user typed it', () => {
  const form = { ...M.useSimStore.getState().btForm, feePct: null }
  const built = M.buildBacktestBody(form, 'OANDA:EURUSD', '1h')
  assert.ok('body' in built, JSON.stringify(built))
  assert.ok(!('fee_pct' in built.body), 'the server resolves the asset class fee')
  assert.equal(M.buildBacktestBody({ ...form, feePct: 0.05 }, 'x', '1h').body.fee_pct, 0.05)
  assert.equal(M.buildBacktestBody({ ...form, feePct: 0 }, 'x', '1h').body.fee_pct, 0, 'zero is a typed value')
  assert.ok('error' in M.buildBacktestBody({ ...form, feePct: 20 }, 'x', '1h'))
  const html = render(M.BacktestForm, { form, appSymbol: 'BTC', appTf: '1h', defaults: null, loading: false, error: null, onChange: noop, onSubmit: noop })
  assert.ok(html.includes('placeholder="Según la clase de activo (Ajustes)"'), html)
})

test('backtest result notes always say which commission was used', () => {
  const own = { fee_pct: 0.005, notes: ['Comisión 0,005 % por lado: la de las divisas en Ajustes.'] }
  assert.equal(M.withFeeNote(own), own, 'the server note is kept as is')
  assert.deepEqual(M.withFeeNote({ fee_pct: 0.1, notes: ['Tamaño por riesgo'] }).notes, ['Tamaño por riesgo', 'Comisión 0,1 % por lado.'])
  assert.deepEqual(M.withFeeNote({ notes: [] }), { notes: [] }, 'no fee reported: nothing is invented')
})

// ----------------------------------------------------------------------------- I4 wording

test('learning gate card: avoided trades "no habrían llegado a TP1", not "habrían perdido"', () => {
  const g = { decisions: 30, taken: 4, avoided: 23, avoided_would_lose_rate: 1, taken_win_rate: 0.5, avoided_judged: 1, taken_judged: 4 }
  const html = render(M.GateLearningCard, { gate: g })
  assert.ok(html.includes('no habrían llegado a TP1 (1 de 1 verificada)'), html)
  assert.ok(!html.includes('habrían perdido'))
  assert.ok(html.includes('2 de 4 verificadas'))
  assert.ok(html.includes('Aún sin evidencia suficiente (n = 1'))
})

const METRICS = {
  initial_capital: 10000, final_equity: 10500, total_return_pct: 5, cagr_pct: null, buy_hold_return_pct: 2, trades: 25,
  win_rate: 0.52, profit_factor: 1.2, avg_win_pct: 1, avg_loss_pct: -1, expectancy_pct: 0.2, avg_r: 0.1, max_dd_pct: 4,
  sharpe: 1, sortino: 1, calmar: 1, exposure_pct: 30, avg_bars_in_trade: 5, best_trade_pct: 3, worst_trade_pct: -2,
  max_consecutive_losses: 3, fees_paid: 12, rejected_signals: 27, rejected_would_win_rate: 0.4615, rejected_resolved: 26,
  gate_lift_pct: 3.21,
}

test('backtest filter effect: the rejected denominator is labelled "con resultado conocido"', () => {
  const html = render(M.GateEffect, { metrics: METRICS, ungated: null, usedGate: true })
  assert.ok(html.includes('(26 con resultado conocido)'), html)
  assert.ok(html.includes('12 de 26'))
})

test('studio strategy: the filter difference is pp and the rejected rate is "x de n" with no conclusion below 30', () => {
  const result = {
    symbol: 'BINANCE:BTCUSDT', tf: '1h', bars: 500, start_time: 1_700_000_000, end_time: 1_701_800_000, params: {},
    metrics: METRICS, ungated: { ...METRICS, total_return_pct: 1.79, gate_lift_pct: null }, equity: [], benchmark: [],
    trades: [], markers: [], candles: [],
  }
  const html = render(M.StrategyPanel, { result })
  assert.ok(html.includes('diferencia por el filtro'), html)
  assert.ok(html.includes('+3,21 pp'))
  assert.ok(!html.includes('mejora del filtro') && !html.includes('+3,21 %'))
  assert.ok(html.includes('12 de 26') && html.includes('(26 con resultado conocido)'))
  assert.ok(html.includes('Aún sin evidencia suficiente (n = 26)'))
})

// ----------------------------------------------------------------------------- sim.css variants / Monte Carlo

function gate(direction, verdict, score = 72) {
  const s = direction === 'long' ? 1 : -1
  const label = { TAKE: 'TOMAR', WAIT: 'ESPERAR', AVOID: 'NO TOMAR' }[verdict]
  return {
    direction, verdict, verdict_label: label, score, win_prob: 0.58, rr: 2.1, ev_r: 0.3, entry: 100, sl: 100 - 2 * s,
    tp1: 100 + 2 * s, tp2: 100 + 4 * s, tp3: 100 + 6 * s, checks: [], reasons_for: [], reasons_against: [],
    summary: `${label}: tendencia mayor a favor`, time: 1_700_000_000,
  }
}

test('simulator panel: favorable headline and "Vigila la salida" use sim.css classes, no inline styles', () => {
  const position = {
    side: 'long', entry_price: 100, entry_time: 1_699_990_000, sl: 98, tp1: 102, tp2: 104, tp3: 106, trail: null, tp_hits: 0,
    size_left: 1, unrealized_pct: -0.4, r_multiple: -0.2, bars_in_trade: 3, ev_hold_r: -0.05,
  }
  const current = {
    time: 1_700_000_000, price: 100, action: 'HOLD_LONG', action_label: 'MANTENER LARGO', score: 0.05, prob_up: 0.52,
    confidence: 0.4, regime: 'range', regime_label: 'Lateral', position, gate: { long: gate('long', 'TAKE'), short: gate('short', 'AVOID', 20) },
    advice: 'Mantener.', warnings: [], contributions: [], levels: { support: [], resistance: [] }, reasons: [],
  }
  const html = render(M.SimPanel, { current, state: null, tf: '1h' })
  assert.match(html, /class="sim-headline info">Filtro favorable en largo · sin señal NEXUS/)
  assert.match(html, /class="sim-gate-summary info"/)
  assert.match(html, /class="sim-alert warn"/)
  assert.ok(!html.includes('var(--info-bg)') && !html.includes('#ffd9a0'), 'no inline info / amber styles')
})

test('monte carlo: in synthetic mode "Símbolo" is a caption, not a label without a control', () => {
  assert.equal(M.useSimStore.getState().mcForm.mode, 'synthetic')
  const html = render(M.MonteCarloView, {})
  assert.ok(html.includes('<span class="label">Símbolo</span>'), html)
  assert.ok(html.includes('SIM:NEXUS (mercados generados)'))
  assert.ok(!/<label[^>]*>Símbolo<\/label>/.test(html))
  const labels = [...html.matchAll(/<label for="([^"]+)">/g)]
  assert.ok(labels.length >= 5, 'the other fields keep their labels')
  for (const m of labels) assert.ok(html.includes(`id="${m[1]}"`), `${m[0]} names a control`)
})
