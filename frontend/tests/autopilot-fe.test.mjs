// "NEXUS automática" (NEXUS's own demo account, id 3) in the UI: the contract types, the typed REST helpers, the honest
// explanation and the backend note, the winners as "x de n", what it watches and its decision log, the controls (the
// scanner-only field, disabled while saving, the Spanish error), the account switch remembered per browser, and that the
// user's {type: 'paper'} messages never feed this view. Same harness as fe-trust.test.mjs: the real sources are bundled
// in memory with Vite's esbuild and rendered with react-dom/server; fetch and the WebSocket are faked (no network).
// Run: cd frontend && node --test tests/autopilot-fe.test.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')

// Browser globals touched at import time by store.ts / auth/session.ts (nothing is mounted).
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
export { api, ApiError } from './api/client'
export { socket } from './api/ws'
export {
  default as AutopilotView,
  AUTOPILOT_ACCOUNT_ID,
  AutopilotControls,
  AutopilotResetModal,
  BrakeNotice,
  DecisionLog,
  MoneyCard,
  KIND_INFO,
  WatchCard,
  autopilotFromMessage,
  draftChanges,
  draftDirty,
  draftFrom,
  emptyWatchText,
  CompareCard,
  compareInterval,
  normalizeAutopilot,
  normalizeComparison,
  normalizeLast,
  subscribeAutopilot,
  validateDraft,
} from './components/paper/AutopilotView'
export { default as PaperStats } from './components/paper/PaperStats'
export { default as PaperPage } from './pages/PaperPage'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'autopilot-fe-entry.tsx' },
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

const render = (Component, props = {}) => M.renderToStaticMarkup(M.createElement(Component, props))
const noop = () => {}
const count = (s, sub) => s.split(sub).length - 1

// ----------------------------------------------------------------------------- fixtures

// The note the backend sends (app/sim/paper.py AUTOPILOT_NOTE): the view must show it verbatim.
const NOTE =
  'Cuenta 100 % simulada que opera sola con las señales de NEXUS y el filtro; nunca se envía nada a un bróker. La ' +
  'validación publicada (docs/VALIDATION.md) no encontró ventaja: lo esperable son resultados parecidos a entrar al ' +
  'azar, menos las comisiones.'
const NOW = Date.now()

function account(over = {}) {
  return {
    id: 3, name: 'NEXUS automática', balance: 10120.5, equity: 10180.25, initial_balance: 10000, realized_pnl: 120.5,
    unrealized_pnl: 59.75, return_pct: 1.8025, trades: 7, wins: 3, losses: 4, win_rate: 3 / 7, open_positions: 1,
    auto_trade: false, auto_symbols: [], auto_tf: '1h', ...over,
  }
}

function position(over = {}) {
  return {
    id: 41, account_id: 3, symbol: 'BINANCE:ETHUSDT', tf: '1h', side: 'long', qty: 2, qty_open: 2, entry_price: 3000,
    entry_time: Math.floor(NOW / 1000) - 7200, sl: 2950, tp1: 3050, tp2: 3100, tp3: 3150, trail: null, tp_hits: 0,
    status: 'open', price: 3030, exit_price: null, exit_time: null, pnl: 60, pnl_pct: 1, r_multiple: 0.6, exit_reason: null,
    exit_label: null, origin: 'autopilot', gate_verdict: 'TAKE', gate_score: 74, advice: null, warnings: [], ...over,
  }
}

const CLOSED = position({
  id: 37, symbol: 'BINANCE:ADAUSDT', status: 'closed', price: null, exit_price: 0.41, exit_time: Math.floor(NOW / 1000) - 600,
  entry_price: 0.4, sl: 0.39, tp1: 0.41, tp2: 0.42, tp3: 0.43, qty: 1000, qty_open: 0, pnl: -12.4, pnl_pct: -0.6,
  r_multiple: -0.4, exit_reason: 'EARLY', exit_label: 'Salida anticipada',
})

function status(over = {}) {
  return {
    enabled: true, account_id: 3, source: 'scanner', tf: '1h', symbols: [], max_positions: 5, risk_pct: 1, scanner_top: 10,
    alerts: false, alert_channels: 0, initial_balance: 1000, max_exposure_pct: 100, max_drawdown_pct: 20, futures_margin_pct: 50,
    brake: { limit_pct: 20, drawdown_pct: 1.2, active: false },
    watching: [
      { symbol: 'BINANCE:SOLUSDT', why: 'escáner', direction: 'short' },
      { symbol: 'BINANCE:BTCUSDT', why: 'escáner', direction: 'long' },
      { symbol: 'BINANCE:ETHUSDT', why: 'posición abierta', direction: 'long' },
    ],
    scan: { id: 812, finished_at: NOW - 5 * 60_000, opportunities: 4 },
    log: [
      { ts: NOW - 60_000, symbol: 'BINANCE:ETHUSDT', kind: 'open', text: 'Abre largo: señal COMPRAR y filtro TOMAR (74/100)' },
      { ts: NOW - 120_000, symbol: 'BINANCE:XRPUSDT', kind: 'skip', text: 'Corto descartado: el filtro dice ESPERAR' },
      { ts: NOW - 180_000, symbol: 'BINANCE:ADAUSDT', kind: 'close', text: 'Cierra largo: salida anticipada de NEXUS (−0,4R)' },
      { ts: NOW - 240_000, symbol: '—', kind: 'info', text: 'Activada' },
    ],
    note: NOTE,
    ...over,
  }
}

function snapshot(statusOver = {}, over = {}) {
  return { account: account(), positions: [position()], history: [CLOSED], risk: null, autopilot: status(statusOver), ...over }
}

/** interface name -> "field" / "field?" list, like tools/contract_check.py parse_interfaces (one level). */
function interfaces(src) {
  const out = {}
  for (const m of src.matchAll(/export interface (\w+)(?:\s+extends\s+[\w\s,<>]+)?\s*\{/g)) {
    let depth = 1
    let i = m.index + m[0].length
    const start = i
    while (depth && i < src.length) depth += { '{': 1, '}': -1 }[src[i++]] ?? 0
    const fields = []
    let level = 0
    for (const line of src.slice(start, i - 1).split('\n')) {
      const s = line.trim()
      const f = level === 0 ? s.match(/^(\w+)(\?)?\s*:/) : null
      if (f) fields.push(f[1] + (f[2] ?? ''))
      level = Math.max(0, level + (s.match(/\{/g) ?? []).length - (s.match(/\}/g) ?? []).length)
    }
    out[m[1]] = fields
  }
  return out
}

// ----------------------------------------------------------------------------- contract (types-autopilot.ts)

test('contract: types-autopilot.ts has one field per line (contract_check), re-exported by types.ts', () => {
  const src = readFileSync(path.join(SRC, 'api', 'types-autopilot.ts'), 'utf8')
  const t = interfaces(src)
  assert.deepEqual(t.AutopilotStatus, [
    'enabled', 'account_id', 'source', 'tf', 'symbols', 'max_positions', 'risk_pct', 'scanner_top', 'watching', 'scan', 'log',
    'alerts', 'alert_channels', 'initial_balance', 'max_exposure_pct', 'max_drawdown_pct', 'futures_margin_pct', 'brake',
    'note',
  ])
  assert.deepEqual(t.AutopilotSnapshot, ['account', 'positions', 'history', 'risk', 'autopilot'])
  assert.deepEqual(t.AutopilotWatch, ['symbol', 'why', 'direction?', 'last?'])
  assert.deepEqual(t.AutopilotLast, ['bar_time', 'tf', 'action', 'label'])
  assert.deepEqual(t.AutopilotComparison, [
    'generated_at', 'window_bars', 'trades', 'compared', 'missing', 'days', 'nexus', 'random', 'diff_r', 'ci95', 'verdict',
    'verdict_label', 'text', 'note',
  ])
  assert.deepEqual(t.AutopilotCompareNexus, ['n', 'wins', 'mean_r'])
  assert.deepEqual(t.AutopilotCompareRandom, ['entries', 'wins', 'mean_r'])
  assert.match(src, /export type AutopilotVerdict = 'none' \| 'insufficient' \| 'no_edge' \| 'better' \| 'worse'/)
  assert.deepEqual(t.AutopilotLogEntry, ['ts', 'symbol', 'kind', 'text'])
  assert.deepEqual(t.AutopilotScan, ['id', 'finished_at', 'opportunities'])
  assert.deepEqual(t.AutopilotUpdate, [
    'enabled?', 'source?', 'symbols?', 'tf?', 'max_positions?', 'risk_pct?', 'scanner_top?', 'alerts?', 'max_exposure_pct?',
    'max_drawdown_pct?', 'futures_margin_pct?',
  ])
  assert.deepEqual(t.AutopilotBrake, ['limit_pct', 'drawdown_pct', 'active'])
  assert.match(src, /export type AutopilotSource = 'scanner' \| 'list' \| 'both'/)
  assert.match(src, /export type AutopilotWhy = 'escáner' \| 'lista' \| 'posición abierta'/)
  const types = readFileSync(path.join(SRC, 'api', 'types.ts'), 'utf8')
  assert.ok(types.includes("export * from './types-autopilot'"))
  assert.match(types, /type: 'paper_autopilot' \}.*AutopilotSnapshot/, 'ServerMessage member')
})

// ----------------------------------------------------------------------------- REST helpers (client.ts)

test('client: api.paper.autopilot calls its four routes with only the given fields', async () => {
  const realFetch = globalThis.fetch
  const seen = []
  globalThis.fetch = async (url, init = {}) => {
    seen.push({ url: String(url), method: init.method ?? 'GET', body: init.body ? JSON.parse(init.body) : undefined })
    return new Response(JSON.stringify(snapshot()), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await M.api.paper.autopilot.get()
    await M.api.paper.autopilot.update({ enabled: true })
    await M.api.paper.autopilot.update({ source: 'list', symbols: ['BINANCE:BTCUSDT'] })
    await M.api.paper.autopilot.reset(5000)
    await M.api.paper.autopilot.reset()
    await M.api.paper.autopilot.close({ position_id: 41 })
    await M.api.paper.autopilot.close({ all: true })
    await M.api.paper.autopilot.compare()
    await M.api.paper.autopilot.compare(true)
  } finally {
    globalThis.fetch = realFetch
  }
  const at = (i) => [seen[i].method, new URL(seen[i].url, 'http://localhost').pathname, seen[i].body]
  assert.deepEqual(at(0), ['GET', '/api/paper/autopilot', undefined])
  assert.deepEqual(at(1), ['POST', '/api/paper/autopilot', { enabled: true }])
  assert.deepEqual(at(2), ['POST', '/api/paper/autopilot', { source: 'list', symbols: ['BINANCE:BTCUSDT'] }])
  assert.deepEqual(at(3), ['POST', '/api/paper/autopilot/reset', { initial_balance: 5000 }])
  assert.deepEqual(at(4), ['POST', '/api/paper/autopilot/reset', {}], 'no balance: the server keeps its own')
  assert.deepEqual(at(5), ['POST', '/api/paper/autopilot/close', { position_id: 41 }])
  assert.deepEqual(at(6), ['POST', '/api/paper/autopilot/close', { all: true }])
  assert.deepEqual(at(7), ['GET', '/api/paper/autopilot/compare', undefined])
  assert.equal(new URL(seen[7].url, 'http://localhost').search, '')
  assert.deepEqual(at(8), ['GET', '/api/paper/autopilot/compare', undefined])
  assert.equal(new URL(seen[8].url, 'http://localhost').searchParams.get('fresh'), 'true')
})

function comparison(over = {}) {
  return {
    generated_at: NOW, window_bars: 48, trades: 12, compared: 11, missing: 1, days: 6,
    nexus: { n: 11, wins: 5, mean_r: 0.08 }, random: { entries: 812, wins: 331, mean_r: -0.06 },
    diff_r: 0.14, ci95: [-0.31, 0.52], verdict: 'insufficient', verdict_label: 'Aún sin evidencia',
    text: 'Aún sin evidencia: con 11 operaciones comparadas no se puede concluir nada (hacen falta 30).',
    note: 'Referencia de cada operación: entradas al azar… la validación publicada (docs/VALIDATION.md) no encontró ventaja.',
    ...over,
  }
}

test('both sources: the list and the scanner candidates are edited and sent together', () => {
  const saved = M.draftFrom(M.normalizeAutopilot(snapshot({ source: 'both', symbols: ['NASDAQ:AAPL'] })).autopilot)
  assert.equal(saved.source, 'both')
  const changed = { ...saved, symbols: ['NASDAQ:AAPL', 'NASDAQ:MSFT'], scanner_top: 4, max_exposure_pct: 80, max_drawdown_pct: 15 }
  assert.deepEqual(M.draftChanges(changed, saved), {
    scanner_top: 4, max_exposure_pct: 80, max_drawdown_pct: 15, symbols: ['NASDAQ:AAPL', 'NASDAQ:MSFT'],
  })
  assert.match(M.validateDraft({ ...saved, max_drawdown_pct: 95 }), /El freno de pérdidas \(%\) debe ser un número entre 0,0 y 90,0/)
  assert.match(M.validateDraft({ ...saved, max_exposure_pct: 600 }), /La exposición máxima \(%\) debe ser un número entre 0,0 y 500,0/)
  const html = render(M.AutopilotControls, { status: M.normalizeAutopilot(snapshot({ source: 'both', symbols: ['NASDAQ:AAPL'] })).autopilot, saving: false, error: null, onSave: async () => null })
  assert.ok(html.includes('Ambos') && html.includes('AAPL') && html.includes('Candidatos del escáner'), html)
  assert.ok(html.includes('Exposición máxima (%)') && html.includes('Freno de pérdidas (%)'))
  assert.equal(M.normalizeAutopilot(snapshot({ source: 'weird' })).autopilot.source, 'scanner')
})

test('money management: its own rules and numbers, never a promise; the brake notice says what to do', () => {
  const st = M.normalizeAutopilot(snapshot({ risk_pct: 1.5, max_positions: 4, max_exposure_pct: 100, max_drawdown_pct: 20 })).autopilot
  const html = render(M.MoneyCard, { status: st, dailyLossPct: 3 })
  assert.ok(html.includes('Cómo gestiona el dinero') && html.includes('Cuenta simulada que empezó con'), html)
  assert.ok(html.includes('Arriesga el 1,5 % de su capital'), html)
  assert.ok(html.includes('Como mucho 4 posiciones a la vez'), html)
  assert.ok(html.includes('no tiene invertido más del 100 % de su capital'), html)
  assert.ok(html.includes('Se frena si pierde un 20 % desde su saldo inicial o un 3 % en el día'), html)
  assert.ok(html.includes('no crean ventaja ni garantizan ganancias'), html)
  assert.ok(html.includes('Futuros de índices (micro o mini): contratos enteros') && html.includes('en el 50 % del capital'), html)
  const off = render(M.MoneyCard, { status: { ...st, max_drawdown_pct: 0, max_exposure_pct: 0, futures_margin_pct: 0 }, dailyLossPct: null })
  assert.ok(off.includes('Sin freno de pérdidas acumuladas') && off.includes('Sin tope de exposición propio'), off)
  assert.ok(off.includes('No opera futuros'), off)
  assert.equal(render(M.BrakeNotice, { brake: { limit_pct: 20, drawdown_pct: 5, active: false } }), '')
  const on = render(M.BrakeNotice, { brake: { limit_pct: 20, drawdown_pct: 24.5, active: true } })
  assert.ok(on.includes('Freno de pérdidas activo') && on.includes('24,5 %') && on.includes('Reiníciala o cambia el freno'), on)
})

test('vs random entries: the backend verdict and note verbatim, "x de n", the interval only when it means something', () => {
  const card = (over, extra = {}) =>
    render(M.CompareCard, { cmp: M.normalizeComparison(comparison(over)), loading: false, error: null, onRefresh: noop, ...extra })
  const early = card()
  assert.ok(early.includes('Frente a entrar al azar') && early.includes('>Aún sin evidencia<'), early)
  assert.ok(early.includes('Aún sin evidencia: con 11 operaciones comparadas no se puede concluir nada (hacen falta 30).'))
  assert.ok(early.includes('5 de 11') && early.includes('331 de 812 entradas'), early)
  assert.ok(early.includes('· muy incierto'), 'between 10 and 29 trades the interval is flagged')
  assert.ok(early.includes('11 de 12 operaciones con referencia, en 6 días'), early)
  assert.ok(early.includes('no encontró ventaja'), 'the method note is shown')
  assert.ok(!/% acierto|% de acierto/.test(early), 'never a bare hit rate')
  assert.equal(M.compareInterval(M.normalizeComparison(comparison({ compared: 7 }))), 'sin intervalo: menos de 10 operaciones')
  const settled = M.normalizeComparison(comparison({ compared: 42, trades: 42, missing: 0, verdict: 'no_edge', verdict_label: 'Sin evidencia de ventaja' }))
  assert.ok(!M.compareInterval(settled).includes('muy incierto'))
  const none = card({ compared: 0, trades: 0, verdict: 'none', verdict_label: 'Sin datos', text: 'Aún no hay operaciones cerradas de NEXUS automática que comparar.' })
  assert.ok(none.includes('Aún no hay operaciones cerradas') && !none.includes('<table'), none)
  const waiting = render(M.CompareCard, { cmp: null, loading: true, error: null, onRefresh: noop })
  assert.ok(waiting.includes('Comparando con entradas al azar'))
  const failed = render(M.CompareCard, { cmp: null, loading: false, error: 'Sin conexión', onRefresh: noop })
  assert.ok(failed.includes('Sin conexión') && !failed.includes('Comparando'))
  assert.equal(M.normalizeComparison({ verdict: 'great', text: 'x', verdict_label: 'y' }), null)
  assert.equal(M.normalizeComparison(comparison({ ci95: [1, 'x'] })).ci95, null)
})

test('client: a refused change (400) rejects with the Spanish detail', async () => {
  const realFetch = globalThis.fetch
  const detail = 'El máximo de posiciones debe ser un número entero entre 1 y 20'
  globalThis.fetch = async () => new Response(JSON.stringify({ detail }), { status: 400, headers: { 'Content-Type': 'application/json' } })
  try {
    await assert.rejects(M.api.paper.autopilot.update({ max_positions: 50 }), (e) => e instanceof M.ApiError && e.status === 400 && e.message === detail)
  } finally {
    globalThis.fetch = realFetch
  }
})

// ----------------------------------------------------------------------------- parsing

test('parsing: invalid rows are dropped, the log is newest first, the account and status are required', () => {
  const raw = snapshot({
    tf: '7m',
    source: 'nope',
    watching: [{ symbol: 'BINANCE:BTCUSDT', why: 'lista', direction: 'up' }, { why: 'lista' }, null, 'x'],
    log: [
      { ts: NOW - 5000, symbol: 'A:B', kind: 'skip', text: 'antes' },
      { ts: NOW, symbol: 'A:B', kind: 'weird', text: 'después' },
      { ts: 'ayer', symbol: 'A:B', kind: 'open', text: 'sin hora' },
      ...Array.from({ length: 40 }, (_, i) => ({ ts: NOW - 10_000 - i, symbol: 'A:B', kind: 'info', text: `n${i}` })),
    ],
    scan: { id: 'x' },
  })
  const s = M.normalizeAutopilot(raw)
  assert.ok(s)
  assert.equal(s.autopilot.tf, '1h', 'an unknown timeframe falls back to 1h')
  assert.equal(s.autopilot.source, 'scanner', 'an unknown source falls back to the scanner (as the server does)')
  assert.deepEqual(s.autopilot.watching, [{ symbol: 'BINANCE:BTCUSDT', why: 'lista', direction: null, last: null }])
  assert.equal(s.autopilot.log.length, 30, 'at most 30')
  assert.deepEqual(s.autopilot.log.slice(0, 2).map((e) => [e.text, e.kind]), [['después', 'info'], ['antes', 'skip']])
  assert.equal(s.autopilot.scan, null)
  assert.equal(s.autopilot.note, NOTE)
  assert.equal(M.normalizeAutopilot({ ...snapshot(), account: null }), null)
  assert.equal(M.normalizeAutopilot({ ...snapshot(), autopilot: { enabled: 'yes' } }), null)
  assert.equal(M.normalizeAutopilot(null), null)
})

// ----------------------------------------------------------------------------- live updates

test('live: only {type: "paper_autopilot"} of account 3 feeds the view; "paper" messages never do', () => {
  class FakeWebSocket {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSING = 2
    static CLOSED = 3
    static last = null
    constructor(url) {
      this.url = url
      this.readyState = FakeWebSocket.CONNECTING
      FakeWebSocket.last = this
    }
    send() {}
    close() {
      this.readyState = FakeWebSocket.CLOSED
    }
  }
  const realWs = globalThis.WebSocket
  globalThis.WebSocket = FakeWebSocket
  const got = []
  const off = M.subscribeAutopilot((s) => got.push(s))
  try {
    M.socket.connect()
    const deliver = (msg) => FakeWebSocket.last.onmessage({ data: JSON.stringify(msg) })
    const mine = { ...account(), id: 1, name: 'Principal' }
    deliver({ type: 'paper', account: mine, positions: [position({ account_id: 1, origin: 'manual' })], orders: [], risk: null })
    deliver({ type: 'paper', ...snapshot() }) // even a 'paper' message shaped like NEXUS's snapshot
    deliver({ type: 'paper_autopilot', ...snapshot({}, { account: mine }) }) // never the user's account
    deliver({ type: 'paper_autopilot', account: account() }) // no status: not a snapshot
    assert.equal(got.length, 0)
    deliver({ type: 'paper_autopilot', ...snapshot() })
    assert.equal(got.length, 1)
    assert.equal(got[0].account.id, M.AUTOPILOT_ACCOUNT_ID)
    assert.equal(got[0].autopilot.note, NOTE)
    off()
    deliver({ type: 'paper_autopilot', ...snapshot() })
    assert.equal(got.length, 1, 'unsubscribed')
  } finally {
    off()
    globalThis.WebSocket = realWs
  }
  assert.equal(M.autopilotFromMessage({ type: 'paper', account: account(), positions: [] }), null)
  assert.equal(M.autopilotFromMessage(null), null)
  assert.equal(M.autopilotFromMessage({ type: 'paper_autopilot', ...snapshot() }).account.id, 3)
  // the view listens through subscribeAutopilot only (effects do not run in server rendering: checked in the source)
  const view = readFileSync(path.join(SRC, 'components', 'paper', 'AutopilotView.tsx'), 'utf8')
  assert.deepEqual([...view.matchAll(/socket\.on\(\s*'([^']+)'/g)].map((m) => m[1]), ['paper_autopilot'])
  assert.match(view, /useEffect\(\s*\(\)\s*=>\s*subscribeAutopilot\(/)
})

// ----------------------------------------------------------------------------- the view

test('view: honest explanation, the backend note verbatim and the state badge', () => {
  const html = render(M.AutopilotView, { initial: snapshot() })
  assert.ok(html.includes(NOTE), 'the note is shown verbatim')
  assert.match(html, /class="notice ap-note" role="note"/)
  for (const part of ['100 % simulada', 'opera sola', 'COMPRAR o', 'VENDER y el filtro dice TOMAR', 'reglas de salida', 'límites de', 'servidor de NEXUS está en marcha', 'nunca envía nada a un bróker']) {
    assert.ok(html.includes(part), `explanation: ${part}`)
  }
  assert.match(html, /class="badge badge-pos ?">Activa</)
  assert.ok(!/ventaja (probada|demostrada)|rentable/i.test(html), 'no claim of an edge')
  const paused = render(M.AutopilotView, { initial: snapshot({ enabled: false }) })
  assert.match(paused, /class="badge badge-muted ?">Pausada</)
  assert.ok(!/class="badge badge-pos ?">Activa</.test(paused))
  assert.ok(paused.includes('Pausada: no abre posiciones nuevas; las abiertas siguen con su stop y sus objetivos.'))
  assert.ok(render(M.AutopilotView).includes('Cargando la cuenta de NEXUS…'), 'loading state before the first answer')
})

test('stats: the winners are "x de n" (no bare percentage) and nothing is concluded below 30', () => {
  const small = render(M.PaperStats, { account: account(), winTally: true })
  assert.ok(small.includes('>3 de 7<'), small)
  assert.ok(small.includes('Aún sin evidencia suficiente (n = 7)'))
  assert.ok(!small.includes('% acierto') && !small.includes('42,9'), 'no bare win rate')
  const big = render(M.PaperStats, { account: account({ trades: 40, wins: 22, losses: 18, win_rate: 0.55 }), winTally: true })
  assert.ok(big.includes('>22 de 40<') && big.includes('55,0 % de las cerradas'), big)
  assert.ok(render(M.PaperStats, { account: account({ trades: 0, wins: 0, losses: 0, win_rate: null }), winTally: true }).includes('>0 de 0<'))
  // the user's account keeps its stats exactly as before
  const mine = render(M.PaperStats, { account: account({ id: 1 }) })
  assert.ok(mine.includes('% acierto') && !mine.includes(' de 7<'))
  assert.ok(render(M.AutopilotView, { initial: snapshot() }).includes('>3 de 7<'), 'the view uses the tally')
})

test('watching: symbol, why and direction (Largo / Corto) plus the latest scan', () => {
  const html = render(M.AutopilotView, { initial: snapshot() })
  const card = html.slice(html.indexOf('Qué está vigilando'), html.indexOf('Decisiones recientes'))
  assert.ok(card.includes('Escaneo #812') && card.includes('4 oportunidades TOMAR'), card)
  assert.ok(card.includes('Hasta 10 oportunidades TOMAR del último escaneo · decide en velas de 1h'))
  const rows = card.split('<tr').slice(2)
  assert.equal(rows.length, 3)
  assert.ok(rows[0].includes('SOLUSDT') && rows[0].includes('>Escáner<') && rows[0].includes('>Corto<'), rows[0])
  assert.ok(rows[1].includes('BTCUSDT') && rows[1].includes('>Largo<'))
  assert.ok(rows[2].includes('ETHUSDT') && rows[2].includes('>Posición abierta<') && rows[2].includes('>Largo<'))
  const list = render(M.WatchCard, { status: status({ source: 'list', symbols: ['BINANCE:BTCUSDT'], watching: [{ symbol: 'BINANCE:BTCUSDT', why: 'lista', direction: null }] }), onOpenChart: noop })
  assert.ok(list.includes('>Lista<') && list.includes('según la señal') && !list.includes('Escaneo #'), list)
})

test('watching: "Última vela" shows what NEXUS said on the last decided bar (no entry is the usual answer)', () => {
  const bar = NOW - 60 * 60_000
  const read = (action, label) => ({ bar_time: bar, tf: '1h', action, label })
  const watching = [
    { symbol: 'BINANCE:BTCUSDT', why: 'lista', direction: null, last: read('WAIT', 'ESPERAR') },
    { symbol: 'BINANCE:ETHUSDT', why: 'lista', direction: null, last: read('BUY', 'COMPRAR') },
    { symbol: 'BINANCE:SOLUSDT', why: 'lista', direction: null, last: read(null, 'Sin análisis (faltan datos)') },
    { symbol: 'BINANCE:XRPUSDT', why: 'lista', direction: null, last: null },
  ]
  const snap = M.normalizeAutopilot(snapshot({ source: 'list', symbols: watching.map((w) => w.symbol), watching }))
  assert.deepEqual(snap.autopilot.watching.map((w) => w.last?.label ?? null), ['ESPERAR', 'COMPRAR', 'Sin análisis (faltan datos)', null])
  const html = render(M.WatchCard, { status: snap.autopilot, onOpenChart: noop })
  assert.ok(html.includes('Última vela'), html)
  const rows = html.split('<tr').slice(2)
  assert.equal(rows.length, 4)
  assert.ok(rows[0].includes('>ESPERAR<') && rows[1].includes('>COMPRAR<'), rows[0])
  assert.ok(rows[2].includes('Sin análisis (faltan datos)') && rows[3].includes('>—<'), rows[2])
  // malformed reads are dropped (never shown as a decision)
  assert.equal(M.normalizeLast({ bar_time: 'x', tf: '1h', action: 'BUY', label: 'COMPRAR' }), null)
  assert.equal(M.normalizeLast({ bar_time: bar, tf: '7x', action: 'BUY', label: 'COMPRAR' }), null)
  assert.deepEqual(M.normalizeLast({ bar_time: bar, tf: '4h', action: '', label: 'Error al analizar' }),
    { bar_time: bar, tf: '4h', action: null, label: 'Error al analizar' })
})

test('watching: Spanish empty states (no opportunity in the last scan, paused, empty list, no scan)', () => {
  const card = (over) => render(M.WatchCard, { status: status({ watching: [], ...over }), onOpenChart: noop })
  const none = card({ scan: { id: 900, finished_at: NOW - 60_000, opportunities: 0 } })
  assert.ok(none.includes('0 oportunidades TOMAR'))
  assert.ok(none.includes('El último escaneo no encontró ninguna oportunidad TOMAR'), none)
  assert.ok(card({ enabled: false }).includes('Pausada: no vigila ningún símbolo'))
  assert.ok(card({ source: 'list', symbols: [] }).includes('La lista está vacía'))
  const noScan = card({ scan: null })
  assert.ok(noScan.includes('Sin escaneos terminados todavía.') && noScan.includes('Cuando termine un escaneo'))
  assert.ok(card({ scan: { id: 5, finished_at: null, opportunities: 0 } }).includes('aún en curso'))
  assert.ok(card({ scan: { id: 5, finished_at: NOW, opportunities: 1 } }).includes('1 oportunidad TOMAR<'), 'singular')
})

test('decision log: newest first, time + symbol + text, a tone per kind', () => {
  const html = render(M.DecisionLog, { log: status().log, onOpenChart: noop })
  const rows = html.split('<li').slice(1)
  assert.equal(rows.length, 4)
  assert.match(rows[0], /class="ap-log-row ap-tone-accent"/)
  assert.ok(rows[0].includes('>Abre<') && rows[0].includes('>ETHUSDT<') && rows[0].includes('señal COMPRAR y filtro TOMAR'))
  // "14:03" today ("30/10, 14:03" when the entry is from another day, e.g. just after midnight)
  assert.match(rows[0], /<time class="ap-log-time mono" date[tT]ime="[^"]+"[^>]*>[\d/, ]*\d{2}:\d{2}<\/time>/)
  assert.ok(rows[1].includes('ap-tone-muted') && rows[1].includes('>Descarta<') && rows[1].includes('XRPUSDT'))
  assert.ok(rows[2].includes('ap-tone-purple') && rows[2].includes('>Cierra<'))
  assert.ok(rows[3].includes('ap-tone-info') && rows[3].includes('>Nota<') && rows[3].includes('Activada'))
  assert.ok(!rows[3].includes('<button'), 'an entry without a symbol has no chart link')
  assert.deepEqual(Object.keys(M.KIND_INFO).sort(), ['close', 'info', 'open', 'skip'])
  assert.ok(render(M.DecisionLog, { log: [], onOpenChart: noop }).includes('Sin decisiones todavía'))
  const view = render(M.AutopilotView, { initial: snapshot() })
  assert.ok(view.indexOf('señal COMPRAR y filtro TOMAR') < view.indexOf('Corto descartado'), 'the view keeps the order')
})

test('futures show whole contracts (open of total), never the units of the index', () => {
  const fut = position({ id: 51, symbol: 'CME_MINI:M2K1!', qty: 15, qty_open: 10, instrument: {
    kind: 'future', name: 'Micro E-mini Russell 2000', root: 'M2K', multiplier: 5, contracts: 3, margin: 700 } })
  const html = render(M.AutopilotView, { initial: snapshot({}, { positions: [fut], history: [{ ...fut, id: 52, status: 'closed', qty_open: 0 }] }) })
  assert.ok(html.includes('2 de 3 contratos M2K'), 'open positions: contracts left of the total')
  assert.ok(html.includes('3 contratos M2K'), 'history: the contracts traded')
  const plain = render(M.AutopilotView, { initial: snapshot() })
  assert.ok(plain.includes('2 de 2') && !/contratos? M2K|\d contratos?</.test(plain), 'shares and crypto keep their units')
})

test('positions and history reuse the paper tables; NEXUS is the origin, never "autopilot"', () => {
  const html = render(M.AutopilotView, { initial: snapshot() })
  const open = html.slice(html.indexOf('Posiciones abiertas'), html.indexOf('Cómo opera'))
  assert.ok(open.includes('ETHUSDT') && open.includes('>Cerrar<'), 'closable by hand')
  assert.ok(open.includes('el resultado cuenta en esta cuenta'))
  const hist = html.slice(html.indexOf('>Historial'))
  assert.ok(hist.includes('ADAUSDT') && hist.includes('Salida anticipada'), hist)
  assert.equal(count(html, '>autopilot<'), 0)
  assert.ok(count(open, '>NEXUS<') >= 1 && count(hist, '>NEXUS<') >= 1)
  const empty = render(M.AutopilotView, { initial: snapshot({}, { positions: [], history: [] }) })
  assert.ok(empty.includes('NEXUS abrirá una cuando su señal al cierre de una vela sea COMPRAR o VENDER y el filtro diga TOMAR.'))
})

// ----------------------------------------------------------------------------- controls

const controls = (over = {}, props = {}) =>
  render(M.AutopilotControls, { status: status(over), saving: false, error: null, onSave: async () => null, ...props })

test('controls: the scanner candidates are shown only in scanner mode; list mode edits the symbols', () => {
  const scanner = controls()
  assert.ok(scanner.includes('Candidatos del escáner'), scanner)
  assert.match(scanner, /aria-pressed="true"[^>]*>Escáner</)
  assert.ok(scanner.includes('las mejores oportunidades TOMAR del último escaneo') || scanner.includes('Las mejores oportunidades TOMAR del último escaneo'))
  assert.ok(!scanner.includes('Quitar '), 'no symbol editor in scanner mode')
  const list = controls({ source: 'list', symbols: ['BINANCE:BTCUSDT', 'OANDA:EURUSD'] })
  assert.ok(!list.includes('Candidatos del escáner'), 'scanner-only field hidden in list mode')
  assert.match(list, /aria-pressed="true"[^>]*>Lista fija</)
  assert.ok(list.includes('Símbolos (2)') && list.includes('aria-label="Quitar BINANCE:BTCUSDT"') && list.includes('EURUSD'))
  assert.ok(controls({ source: 'list', symbols: [] }).includes('Con la lista vacía NEXUS no vigila nada'))
  for (const label of ['Temporalidad', 'Máximo de posiciones', 'Riesgo por operación (%)']) assert.ok(list.includes(label), label)
  // every <label for> names a control (Field + NumberInput / Select / SymbolSearch)
  const labels = [...scanner.matchAll(/<label for="([^"]+)">/g), ...list.matchAll(/<label for="([^"]+)">/g)]
  assert.ok(labels.length >= 6)
  for (const m of labels) assert.ok(scanner.includes(`id="${m[1]}"`) || list.includes(`id="${m[1]}"`), `${m[0]} names a control`)
})

test('controls: the alerts switch, and a warning when no alert channel would deliver them', () => {
  const html = (over) =>
    render(M.AutopilotControls, { status: M.normalizeAutopilot(snapshot(over)).autopilot, saving: false, error: null, onSave: async () => null })
  const off = html({ alerts: false, alert_channels: 1 })
  assert.ok(off.includes('Avisarme por Telegram o correo cuando opere') && off.includes('sus operaciones no se avisan fuera de la app'), off)
  const none = html({ alerts: true, alert_channels: 0 })
  assert.ok(none.includes('Ningún canal de avisos está activo: configura Telegram o correo en Ajustes › Alertas.'), none)
  const on = html({ alerts: true, alert_channels: 2 })
  assert.ok(on.includes('Avisa de sus aperturas, cierres y stops (operaciones simuladas)'), on)
  const parsed = M.normalizeAutopilot(snapshot({ alerts: 'yes', alert_channels: -3 })).autopilot
  assert.equal(parsed.alerts, false) // only a real true switches it on
  assert.equal(parsed.alert_channels, 0)
})

test('controls: on / off at once, everything disabled while saving, the Spanish error shown', () => {
  const on = controls()
  assert.match(on, /<input type="checkbox" checked=""/)
  assert.ok(on.includes('Activa: NEXUS opera sola'))
  assert.match(on, /<fieldset class="ap-fieldset">/, 'enabled while idle')
  assert.ok(controls({ enabled: false }).includes('>Pausada<'))
  const busy = controls({}, { saving: true })
  assert.match(busy, /<fieldset class="ap-fieldset" disabled="">/, 'every control disabled while saving')
  const detail = 'El riesgo por operación (%) debe ser un número entre 0,1 y 5,0'
  const failed = controls({}, { error: detail })
  assert.ok(failed.includes(`role="alert">${detail}<`), failed)
  assert.match(on, /<button type="button" class="btn btn-primary btn-sm" disabled="">Guardar cambios<\/button>/, 'nothing to save yet')
})

test('draft: only the changed visible fields are sent, with the server limits', () => {
  const saved = M.draftFrom(status())
  assert.deepEqual(M.draftChanges({ ...saved }, saved), {})
  assert.equal(M.draftDirty({ ...saved }, saved), false)
  assert.deepEqual(M.draftChanges({ ...saved, max_positions: 8, tf: '4h' }, saved), { tf: '4h', max_positions: 8 })
  assert.deepEqual(M.draftChanges({ ...saved, scanner_top: 12 }, saved), { scanner_top: 12 })
  // list mode: the symbols go, the hidden scanner candidates do not
  const list = { ...saved, source: 'list', symbols: ['BINANCE:BTCUSDT'], scanner_top: 25 }
  assert.deepEqual(M.draftChanges(list, saved), { source: 'list', symbols: ['BINANCE:BTCUSDT'] })
  assert.equal(M.validateDraft({ ...list, scanner_top: 99 }), null, 'a hidden field is not validated')
  // scanner mode: an edited (hidden) symbol list is not sent
  assert.deepEqual(M.draftChanges({ ...saved, symbols: ['X:Y'] }, saved), {})
  // an emptied number is an unsaved edit that cannot be saved
  const emptied = { ...saved, max_positions: null }
  assert.equal(M.draftDirty(emptied, saved), true)
  assert.deepEqual(M.draftChanges(emptied, saved), {})
  assert.equal(M.validateDraft(emptied), 'El máximo de posiciones debe ser un número entero entre 1 y 20.')
  assert.equal(M.validateDraft({ ...saved, risk_pct: 7 }), 'El riesgo por operación (%) debe ser un número entre 0,1 y 5,0.')
  assert.equal(M.validateDraft({ ...saved, risk_pct: 0.05 }), 'El riesgo por operación (%) debe ser un número entre 0,1 y 5,0.')
  assert.equal(M.validateDraft({ ...saved, scanner_top: 2.5 }), 'El número de candidatos del escáner debe ser un número entero entre 1 y 30.')
  assert.equal(M.validateDraft({ ...saved, max_positions: 20, risk_pct: 0.1, scanner_top: 30 }), null, 'limits included')
  const many = { ...list, symbols: Array.from({ length: 31 }, (_, i) => `BINANCE:S${i}USDT`) }
  assert.equal(M.validateDraft(many), 'Como máximo 30 símbolos en la lista.')
})

test('reset modal: grammatical, says what is lost and that the user account does not change', () => {
  const props = { open: true, currentInitial: 10000, onClose: noop, onDone: noop }
  const one = render(M.AutopilotResetModal, { ...props, openPositions: 1 })
  assert.ok(one.includes('Se cerrará su posición abierta') && one.includes('Tu cuenta no cambia'), one)
  assert.ok(one.includes('Reiniciar la cuenta de NEXUS') && one.includes('Balance inicial ($)'))
  assert.ok(render(M.AutopilotResetModal, { ...props, openPositions: 3 }).includes('Se cerrarán sus 3 posiciones abiertas'))
  assert.equal(render(M.AutopilotResetModal, { ...props, open: false, openPositions: 0 }), '')
})

// ----------------------------------------------------------------------------- account switch (PaperPage)

test('demo page: the account switch is remembered per browser; "Mi cuenta" stays the default', () => {
  const KEY = 'nexus.paper.account.v1'
  const selected = (html) => html.match(/<button type="button" role="tab" aria-selected="true"[^>]*>(.*?)<\/button>/)?.[1] ?? ''
  memory.delete(KEY)
  const first = render(M.PaperPage)
  assert.ok(first.includes('Mi cuenta') && first.includes('NEXUS automática'), 'both accounts in the switch')
  assert.ok(selected(first).includes('Mi cuenta') && first.includes('Cargando cuenta demo…'), 'default: the user account')
  assert.ok(!first.includes('Cargando la cuenta de NEXUS'))
  memory.set(KEY, JSON.stringify('nexus'))
  const nexus = render(M.PaperPage)
  assert.ok(selected(nexus).includes('NEXUS automática') && nexus.includes('Cargando la cuenta de NEXUS…'), nexus)
  assert.ok(!nexus.includes('Cargando cuenta demo…'), 'only the selected account is mounted')
  memory.set(KEY, '{roto')
  assert.ok(render(M.PaperPage).includes('Cargando cuenta demo…'), 'a corrupt value falls back to the user account')
  const realGet = globalThis.localStorage.getItem
  globalThis.localStorage.getItem = () => {
    throw new Error('SecurityError')
  }
  try {
    assert.ok(render(M.PaperPage).includes('Cargando cuenta demo…'), 'storage unavailable: no crash')
  } finally {
    globalThis.localStorage.getItem = realGet
    memory.delete(KEY)
  }
})
