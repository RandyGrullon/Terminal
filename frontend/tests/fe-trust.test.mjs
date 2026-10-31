// Regression tests of the fe-trust fixes (honest filter statistics, exit advice, demo orders at market, Spanish
// plurals / percentage points, gate TOMAR vs NEXUS signal). No extra dependencies: the real sources are bundled in
// memory with the esbuild that Vite already installs, and components are rendered with react-dom/server.
// Run: cd frontend && node --test tests/fe-trust.test.mjs
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

const ENTRY = `
export { isOne, plural, pluralWord } from './lib/plural'
export { MIN_EVIDENCE_N, evidenceText, evidenceTone, filterEvidence, fmtOf, fmtTally, wilson } from './components/brain/evidence'
export { EARLY_EXIT_EV, exitAdvice, isExitAction, shownWarnings } from './components/sim/labels'
export { FAVORABLE_LABEL, gateSignal, gateSummary, gateVerdictText } from './components/nexus/GateChecklist'
export { default as TradeCheck, needsMarketCheck, validate } from './components/nexus/TradeCheck'
export { default as SignalPanel } from './components/nexus/SignalPanel'
export { SimPanel } from './components/sim/SimPanel'
export { GateEffect } from './components/sim/GateEffect'
export { BacktestMetrics } from './components/sim/BacktestMetrics'
export { default as GateLearningCard } from './components/brain/GateLearningCard'
export { default as ResetModal } from './components/paper/ResetModal'
export { default as GatePreview } from './components/paper/GatePreview'
export { adviceLevel } from './components/paper/paperUtils'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'fe-trust-entry.tsx' },
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

// ----------------------------------------------------------------------------- fixtures

const LABEL = { TAKE: 'TOMAR', WAIT: 'ESPERAR', AVOID: 'NO TOMAR' }

function gate(direction, verdict, score = 72) {
  const s = direction === 'long' ? 1 : -1
  return {
    direction,
    verdict,
    verdict_label: LABEL[verdict],
    score,
    win_prob: 0.58,
    rr: 2.1,
    ev_r: 0.3,
    entry: 100,
    sl: 100 - 2 * s,
    tp1: 100 + 2 * s,
    tp2: 100 + 4 * s,
    tp3: 100 + 6 * s,
    checks: [{ id: 'trend_alignment', label: 'Tendencia mayor a favor', passed: true, critical: false, weight: 0.7, detail: 'EMA 4h ascendente' }],
    reasons_for: ['EMA 4h ascendente'],
    reasons_against: [],
    summary: `${LABEL[verdict]}: tendencia mayor a favor · R:R 2,1 · P(TP1) 58 %`,
    time: 1_700_000_000,
  }
}

function position(over = {}) {
  return {
    side: 'long',
    entry_price: 100,
    entry_time: 1_699_990_000,
    sl: 98,
    tp1: 102,
    tp2: 104,
    tp3: 106,
    trail: null,
    tp_hits: 0,
    size_left: 1,
    unrealized_pct: -0.4,
    r_multiple: -0.2,
    bars_in_trade: 1,
    ev_hold_r: -0.05,
    ...over,
  }
}

function current(over = {}) {
  return {
    time: 1_700_000_000,
    price: 100,
    action: 'WAIT',
    action_label: 'ESPERAR',
    score: 0.05,
    prob_up: 0.52,
    confidence: 0.4,
    regime: 'range',
    regime_label: 'Lateral',
    position: null,
    gate: { long: gate('long', 'TAKE'), short: gate('short', 'AVOID', 20) },
    advice: 'Esperar: sin señal clara.',
    warnings: [],
    contributions: [],
    levels: { support: [], resistance: [] },
    reasons: [],
    ...over,
  }
}

// Live BTC 1h figures from the review: 27 rejected, 12 of 26 resolved would have won; NEXUS took 25 trades.
const BTC_STATS = {
  trades: 25,
  win_rate: 0.52,
  profit_factor: 1.1,
  total_return_pct: 3,
  max_dd_pct: 5,
  sharpe: null,
  avg_r: 0.1,
  expectancy_pct: 0.1,
  rejected_signals: 27,
  rejected_would_win_rate: 0.4615,
  rejected_resolved: 26,
}

function nexus(cur, stats = BTC_STATS) {
  return { symbol: 'BINANCE:BTCUSDT', tf: '1h', provider: 'binance', history: 'full', note: null, ok: true, message: null, params: {}, bars: null, markers: [], trades: [], current: cur, prediction: null, learning: null, stats, quick: null }
}

const signalPanel = (cur, stats) =>
  render(M.SignalPanel, { nexus: nexus(cur, stats), loading: false, error: null, refreshing: false, updatedAt: null, tf: '1h', onRetry: noop, onRefresh: noop, onOpenTradingView: noop })

const TAKE_BADGE = '</svg>TOMAR<' // VerdictBadge of a TOMAR ("NO TOMAR" does not match)

// ----------------------------------------------------------------------------- spanish-copy-errors

test('plural helper: singular only for exactly one', () => {
  assert.equal(M.plural(1, 'vela', 'velas'), '1 vela')
  assert.equal(M.plural(0, 'vela', 'velas'), '0 velas')
  assert.equal(M.plural(2, 'vela', 'velas'), '2 velas')
  assert.equal(M.plural(null, 'vela', 'velas'), '— velas')
  assert.equal(M.pluralWord(-1, 'posición', 'posiciones'), 'posición')
  assert.equal(M.isOne(1.2), true)
  assert.equal(M.isOne(1.6), false)
})

test('reset modal with one open position is grammatical', () => {
  const html = render(M.ResetModal, { open: true, currentInitial: 10000, openPositions: 1, onClose: noop, onDone: noop })
  assert.ok(html.includes('Se cerrará la posición abierta'), html)
  assert.ok(!html.includes('las 1 posiciones'))
  const many = render(M.ResetModal, { open: true, currentInitial: 10000, openPositions: 3, onClose: noop, onDone: noop })
  assert.ok(many.includes('Se cerrarán las 3 posiciones abiertas'))
})

test('signal panel: one bar in the trade is "1 vela"', () => {
  const html = signalPanel(current({ action: 'HOLD_LONG', action_label: 'MANTENER LARGO', position: position({ ev_hold_r: 0.2 }) }))
  assert.ok(html.includes('>1 vela<'), 'expected "1 vela"')
  assert.ok(!html.includes('1 velas'))
})

test('gate lift and NEXUS vs buy & hold are percentage points', () => {
  const metrics = { total_return_pct: 10, buy_hold_return_pct: 6.8, gate_lift_pct: 3.21, rejected_signals: 0, rejected_would_win_rate: null, trades: 20, win_rate: 0.5 }
  const ungated = { ...metrics, total_return_pct: 6.79, gate_lift_pct: null }
  const fx = render(M.GateEffect, { metrics, ungated, usedGate: true })
  assert.ok(fx.includes('+3,21 pp'), 'lift must read +3,21 pp')
  assert.ok(!fx.includes('+3,21 %'))
  const bm = render(M.BacktestMetrics, { metrics })
  assert.ok(bm.includes('NEXUS lo supera por 3,2 pp'), 'difference with buy & hold must be pp')
})

// ----------------------------------------------------------------------------- overclaimed-filter-proof

test('evidence: 12 of 26 is not proof (small n)', () => {
  const e = M.filterEvidence({ total: 27, resolved: 26, winRate: 0.4615, takenN: 25, takenWinRate: 0.52 })
  assert.equal(e.verdict, 'insufficient')
  assert.equal(e.gap, 'small_n')
  assert.deepEqual(e.rejected, { n: 26, wins: 12 })
  const text = M.evidenceText(e, 'signals')
  assert.ok(text.includes('12 de 26'), text)
  assert.ok(text.includes('Aún sin evidencia suficiente (n = 26)'), text)
  assert.ok(!/prueba|protege|ahorrando/i.test(text), text)
})

test('evidence: compares with the taken trades, not with 50 %', () => {
  // Review case BTC 4h: the rejected signals would have won 56 % vs 22 % of the taken trades. Old UI: "la prueba de que
  // el filtro te protege" because 44 % would have lost; the filter actually discards BETTER signals here.
  const better = M.filterEvidence({ total: 60, resolved: 50, winRate: 0.56, takenN: 40, takenWinRate: 0.22 })
  assert.equal(better.verdict, 'discards_better')
  assert.ok(M.evidenceText(better, 'signals').startsWith('De las señales rechazadas'))
  assert.ok(M.evidenceText(better, 'signals').includes('Ojo'))
  const worse = M.filterEvidence({ total: 220, resolved: 200, winRate: 0.3, takenN: 100, takenWinRate: 0.55 })
  assert.equal(worse.verdict, 'discards_worse')
  assert.ok(M.evidenceText(worse, 'signals').includes('Por ahora indica'))
  // Enough cases but the 95 % interval of the difference includes zero.
  const overlap = M.filterEvidence({ total: 60, resolved: 60, winRate: 0.45, takenN: 50, takenWinRate: 0.5 })
  assert.equal(overlap.gap, 'overlap')
  // A large rejected sample against a handful of taken trades is still not evidence.
  assert.equal(M.filterEvidence({ total: 90, resolved: 80, winRate: 0.2, takenN: 6, takenWinRate: 0.9 }).gap, 'small_benchmark')
  assert.equal(M.filterEvidence({ total: 90, resolved: 80, winRate: 0.2, takenN: 0, takenWinRate: null }).gap, 'no_benchmark')
})

test('evidence: unknown denominator and no outcomes', () => {
  const e = M.filterEvidence({ total: 27, resolved: undefined, winRate: 0.4615, takenN: 25, takenWinRate: 0.52 })
  assert.equal(e.gap, 'unknown_n')
  assert.ok(M.evidenceText(e, 'signals').includes('n ≤ 27'))
  assert.equal(M.filterEvidence({ total: 5, resolved: 0, winRate: null }).verdict, 'none')
  const w = M.wilson(14, 26)
  assert.ok(Math.abs(w[0] - 0.355) < 0.01 && Math.abs(w[1] - 0.716) < 0.01, String(w))
})

test('signal panel statistics: "x de n" and no proof claim', () => {
  const html = signalPanel(current())
  assert.ok(html.includes('12 de 26'), 'rejected tally')
  assert.ok(html.includes('13 de 25'), 'taken tally')
  assert.ok(html.includes('Aún sin evidencia suficiente (n = 26)'))
  assert.ok(!/prueba de que|te protege/.test(html))
})

test('learning page gate card: one verified loss is not "ahorrando dinero"', () => {
  const g = { decisions: 30, taken: 4, avoided: 23, avoided_would_lose_rate: 1, taken_win_rate: 0.5, avoided_judged: 1, taken_judged: 4 }
  const html = render(M.GateLearningCard, { gate: g })
  assert.ok(!html.includes('ahorrando'), 'no money-saving claim')
  assert.ok(html.includes('1 de 1 verificada)'), html)
  assert.ok(html.includes('2 de 4 verificadas'), html)
  assert.ok(html.includes('Aún sin evidencia suficiente (n = 1'), html)
})

test('backtest rejected line: no "El filtro acertó" on a small sample', () => {
  const metrics = { total_return_pct: 5, buy_hold_return_pct: 2, gate_lift_pct: 1, trades: 25, win_rate: 0.52, rejected_signals: 27, rejected_would_win_rate: 0.4615, rejected_resolved: 26 }
  const html = render(M.GateEffect, { metrics, ungated: null, usedGate: true })
  assert.ok(!html.includes('acertó'))
  assert.ok(html.includes('12 de 26'))
  assert.ok(html.includes('Aún sin evidencia suficiente'))
})

// ----------------------------------------------------------------------------- ev-negative-close-now-vs-hold (I7)

test('exit advice: CERRAR only on an EXIT action', () => {
  assert.equal(M.exitAdvice('HOLD_LONG', -0.05), 'watch')
  assert.equal(M.exitAdvice('HOLD_SHORT', -0.4), 'watch')
  assert.equal(M.exitAdvice('EXIT_LONG', -0.2), 'exit')
  assert.equal(M.exitAdvice('HOLD_LONG', 0.1), 'hold')
  const ws = [{ code: 'EARLY_EXIT', severity: 'critical', message: 'El valor esperado de mantener es negativo (-0,12R): vigila la salida' }]
  assert.equal(M.shownWarnings(ws, 'HOLD_LONG')[0].severity, 'warn')
  assert.equal(M.shownWarnings(ws, 'HOLD_LONG', true).length, 0)
  assert.equal(M.shownWarnings(ws, 'EXIT_LONG')[0].severity, 'critical')
})

test('signal panel: negative EV while holding is "Vigila la salida", not CERRAR AHORA', () => {
  const warn = { code: 'EARLY_EXIT', severity: 'warn', message: 'El valor esperado de mantener es negativo (-0,05R): vigila la salida' }
  const html = signalPanel(current({ action: 'HOLD_LONG', action_label: 'MANTENER LARGO', position: position(), warnings: [warn] }))
  assert.ok(!html.includes('CERRAR AHORA'))
  assert.ok(!html.includes('Salida anticipada'))
  assert.ok(html.includes('Vigila la salida'))
  const exiting = signalPanel(current({ action: 'EXIT_LONG', action_label: 'CERRAR LARGO', position: position({ ev_hold_r: -0.15 }) }))
  assert.ok(exiting.includes('CERRAR AHORA'))
})

test('simulator panel: no close order while NEXUS holds', () => {
  const hold = render(M.SimPanel, { current: current({ action: 'HOLD_LONG', action_label: 'MANTENER LARGO', position: position() }), state: null, tf: '1h' })
  assert.ok(!hold.includes('NEXUS sugiere cerrar'))
  assert.ok(!hold.includes('CERRAR AHORA'))
  assert.ok(hold.includes('Vigila la salida'))
  const exit = render(M.SimPanel, { current: current({ action: 'EXIT_LONG', action_label: 'CERRAR LARGO', position: position() }), state: null, tf: '1h' })
  assert.ok(exit.includes('CERRAR AHORA'))
})

test('paper positions: an engine warning is not "NEXUS: cerrar"', () => {
  const base = { id: 1, symbol: 'BINANCE:BTCUSDT', side: 'long', status: 'open' }
  const ev = { code: 'EARLY_EXIT', severity: 'warn', message: 'El valor esperado de mantener es negativo (-0,05R): vigila la salida' }
  assert.equal(M.adviceLevel({ ...base, advice: 'Mantener: EV -0,05R · probabilidad a favor 48 %', warnings: [ev] }), 'warn')
  const spike = { code: 'VOL_SPIKE', severity: 'critical', message: 'Vela de 3,1 ATR en contra de tu largo' }
  assert.equal(M.adviceLevel({ ...base, advice: spike.message, warnings: [spike] }), 'warn')
  assert.equal(M.adviceLevel({ ...base, advice: 'Cerrar ya: NEXUS recomienda cerrar el largo (EV -0,20R, CERRAR LARGO)', warnings: [] }), 'close')
})

// ----------------------------------------------------------------------------- tradecheck-entry-ignored

test('trade check: an idea with its own entry is re-checked at market before ordering', () => {
  const idea = { direction: 'long', entry: 75000, sl: 74000, tp: 79000 }
  assert.equal(M.needsMarketCheck('long', idea), true)
  assert.equal(M.needsMarketCheck('short', idea), false) // the other button uses NEXUS' own levels
  assert.equal(M.needsMarketCheck('long', { ...idea, entry: null }), false) // verified at market already
  // Breakout idea: stop above the current market price -> no valid market order.
  assert.match(M.validate('long', 77093, 77873, 80000), /por debajo de la entrada/)
  assert.equal(M.validate('long', 77093, 76000, 80000), null)
})

test('trade check: order buttons say the demo order is at market', () => {
  const html = render(M.TradeCheck, { symbol: 'BINANCE:BTCUSDT', tf: '1h', current: current(), onPreview: noop })
  assert.ok(html.includes('Comprar (demo, a mercado)'))
  assert.ok(html.includes('Vender (demo, a mercado)'))
  assert.ok(html.includes('siempre a mercado'))
})

// ----------------------------------------------------------------------------- gate-take-without-signal (I8)

test('gate signal: TOMAR is an opportunity only with the matching NEXUS action', () => {
  assert.equal(M.gateSignal(gate('long', 'TAKE'), 'BUY'), 'take')
  assert.equal(M.gateSignal(gate('long', 'TAKE'), 'WAIT'), 'favorable')
  assert.equal(M.gateSignal(gate('long', 'TAKE'), 'HOLD_LONG'), 'favorable')
  assert.equal(M.gateSignal(gate('short', 'TAKE'), 'SELL'), 'take')
  assert.equal(M.gateSignal(gate('short', 'TAKE'), 'BUY'), 'favorable')
  assert.equal(M.gateSignal(gate('long', 'TAKE'), null), 'favorable')
  assert.equal(M.gateSignal(gate('long', 'TAKE'), undefined), 'unconfirmed')
  assert.equal(M.gateSignal(gate('long', 'WAIT'), 'BUY'), 'wait')
  assert.ok(M.gateSummary(gate('long', 'TAKE'), 'WAIT').startsWith('Filtro favorable:'))
})

test('signal panel: "¿Comprar ahora?" is not TOMAR while NEXUS says ESPERAR', () => {
  const html = signalPanel(current())
  assert.ok(html.includes(M.FAVORABLE_LABEL))
  assert.ok(!html.includes(TAKE_BADGE), 'no TOMAR badge without a NEXUS signal')
  assert.ok(html.includes('Filtro favorable: tendencia mayor a favor'), 'gate summary kept, relabelled')
  assert.ok(html.includes('NEXUS no da señal de compra en esta vela (dice ESPERAR)'))
  const buy = signalPanel(current({ action: 'BUY', action_label: 'COMPRAR' }))
  assert.ok(buy.includes(TAKE_BADGE))
  assert.ok(!buy.includes(M.FAVORABLE_LABEL))
})

test('simulator headline: favorable filter without signal is not "TOMAR en largo"', () => {
  const html = render(M.SimPanel, { current: current(), state: null, tf: '1h' })
  assert.ok(!html.includes('TOMAR en largo'))
  assert.ok(html.includes('Filtro favorable en largo · sin señal NEXUS'))
  const buy = render(M.SimPanel, { current: current({ action: 'BUY', action_label: 'COMPRAR' }), state: null, tf: '1h' })
  assert.ok(buy.includes('TOMAR en largo'))
})

test('paper gate preview follows the NEXUS signal', () => {
  const props = { gate: gate('long', 'TAKE'), loading: false, error: null, onRetry: noop, tf: '1h' }
  const favorable = render(M.GatePreview, { ...props, action: 'WAIT', actionLabel: 'ESPERAR' })
  assert.ok(favorable.includes(M.FAVORABLE_LABEL))
  assert.ok(!favorable.includes(TAKE_BADGE))
  assert.ok(favorable.includes('Tendencia mayor a favor'), 'per-check detail kept')
  assert.ok(render(M.GatePreview, { ...props, action: 'BUY' }).includes(TAKE_BADGE))
  const unknown = render(M.GatePreview, props)
  assert.ok(unknown.includes(TAKE_BADGE))
  assert.ok(unknown.includes('solo es una oportunidad si NEXUS'))
})

// ----------------------------------------------------------------------------- verifier follow-ups

const count = (s, sub) => s.split(sub).length - 1
// Current engine: a negative EV while holding is a WATCH_EXIT warning; EARLY_EXIT only on an actual exit.
const WATCH = { code: 'WATCH_EXIT', severity: 'warn', message: 'Vigila la salida: el valor esperado de mantener es negativo (−0,05R)' }

test('exit advice: the entry bar is not "Vigila la salida"; WATCH_EXIT is not listed twice', () => {
  assert.equal(M.exitAdvice('BUY', -0.02), 'hold')
  assert.equal(M.exitAdvice('SELL', -0.3), 'hold')
  assert.equal(M.shownWarnings([WATCH], 'HOLD_LONG', true).length, 0)
  assert.equal(M.shownWarnings([WATCH], 'HOLD_LONG', false).length, 1)
  const entry = signalPanel(current({ action: 'BUY', action_label: 'COMPRAR', position: position({ bars_in_trade: 0, ev_hold_r: -0.02 }) }))
  assert.ok(!entry.includes('Vigila la salida'), 'no exit warning next to the entry NEXUS has just decided')
})

test('signal and simulator panels: one "Vigila la salida" and no exclusive exit rule', () => {
  const hold = current({ action: 'HOLD_LONG', action_label: 'MANTENER LARGO', position: position(), warnings: [WATCH] })
  for (const html of [signalPanel(hold), render(M.SimPanel, { current: hold, state: null, tf: '1h' })]) {
    assert.equal(count(html, 'Vigila la salida'), 1, html)
    assert.ok(html.includes('un EV negativo es un aviso'))
    // NEXUS also exits before the stop on SIGNAL / TIME / risk flags: never "solo … cuando el EV baja a −0,10R".
    assert.ok(!/solo (da la orden de cerrar|cierra)/.test(html), html)
    assert.ok(!html.includes('CERRAR AHORA'))
  }
  // Without the position block the engine's caution is kept.
  const noPos = signalPanel(current({ action: 'HOLD_LONG', action_label: 'MANTENER LARGO', warnings: [WATCH] }))
  assert.equal(count(noPos, 'Vigila la salida'), 1)
})

test('signal panel: an early-exit message is not prefixed twice', () => {
  const early = { code: 'EARLY_EXIT', severity: 'critical', message: 'Salida anticipada: valor esperado de mantener negativo (−0,15R)' }
  const html = signalPanel(current({ action: 'EXIT_LONG', action_label: 'CERRAR LARGO', warnings: [early] }))
  assert.ok(html.includes('<b>Salida anticipada: </b>valor esperado'), html)
  assert.ok(!html.includes('Salida anticipada: </b>Salida anticipada'))
  // Only a whole leading label is dropped from the message.
  const divs = [
    { code: 'DIVERGENCE', severity: 'warn', message: 'Divergencia bajista del RSI en contra de tu largo' },
    { code: 'DIVERGENCE', severity: 'info', message: 'Divergencias repetidas en el RSI' },
  ]
  const hold = signalPanel(current({ action: 'HOLD_LONG', action_label: 'MANTENER LARGO', position: position({ ev_hold_r: 0.2 }), warnings: divs }))
  assert.ok(hold.includes('<b>Divergencia: </b>bajista del RSI'), hold)
  assert.ok(hold.includes('<b>Divergencia: </b>Divergencias repetidas'), hold)
})

test('gate verdict text follows the NEXUS signal (market re-check table)', () => {
  assert.equal(M.gateVerdictText(gate('long', 'TAKE'), 'WAIT'), `${M.FAVORABLE_LABEL} 72/100`)
  assert.equal(M.gateVerdictText(gate('long', 'TAKE'), 'BUY'), 'TOMAR 72/100')
  assert.equal(M.gateVerdictText(gate('short', 'AVOID', 20), 'SELL'), 'NO TOMAR 20/100')
})
