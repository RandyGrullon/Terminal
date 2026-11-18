// "Reto" and "Horario de mercados" of NEXUS automática in the UI (components/paper/AutopilotGoal.tsx): the defensive parse of the running
// challenge and of the last result, the deadline from "HH:MM" (today or tomorrow), the time left, the card while it
// runs (target, progress, what is left, the aggressive mode and the honesty note shown verbatim), the form and the last
// result, and the typed REST helpers. Same harness as autopilot-fe.test.mjs (bundled with esbuild, no network).
// Run: cd frontend && node --test tests/autopilot-goal-fe.test.mjs
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
export { api } from './api/client'
export { GoalView, deadlineFrom, fmtLeft, normalizeGoal, normalizeGoalResult } from './components/paper/AutopilotGoal'
export { normalizeStatus } from './components/paper/AutopilotView'
export { SessionView, fmtWhen, marketWhen, normalizeMarkets, normalizeSession } from './components/paper/AutopilotSession'
export { ChallengesView, STATUS_LABEL, normalizeChallenges, planText, sizeText } from './components/paper/AutopilotChallenges'
export { PlanLine, TradeTile } from './components/paper/DemoKit'
export { TradeDetailBody, detailProps, isRowClick } from './components/paper/TradeDetailModal'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'autopilot-goal-fe-entry.tsx' },
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
const M = await import(BUNDLE_URL)

const render = (Component, props = {}) => M.renderToStaticMarkup(M.createElement(Component, props))
const noop = () => {}

const MODE = 'Modo agresivo: velas de 5m, 15m y 1h · 4 % de riesgo por operación en lotes de MT5.'
const NOTE = 'Un reto no crea ventaja: la validación no la encontró.'
const GOAL = {
  target: 12000, until: 1_800_003_600, started_at: 1_800_000_000, start_equity: 10000, aggressive: true, then: 'normal',
  equity: 10500, pnl: 500, remaining: 1500, progress_pct: 25, seconds_left: 7500, mode_text: MODE, note: NOTE,
}
const LAST = {
  outcome: 'time', target: 1200, until: 1_799_000_000, started_at: 1_798_990_000, ended_at: 1_799_000_000,
  start_equity: 1000, end_equity: 1045.5, pnl: 45.5, pnl_pct: 4.55, trades: 3, wins: 2, losses: 1, closed: 1,
  aggressive: true, late: false, text: 'Se acabó el tiempo: meta 1.200,00, de 1.000,00 a 1.045,50 · 2 de 3 operaciones ganadoras',
}
const props = (over = {}) => ({ goal: null, last: null, enabled: true, draft: null, busy: false, error: null,
  onDraft: noop, onStart: noop, onStop: noop, ...over })

test('the running challenge and the last result are parsed defensively', () => {
  assert.deepEqual(M.normalizeGoal(GOAL), GOAL)
  assert.equal(M.normalizeGoal({ target: 'x' }), null)
  assert.equal(M.normalizeGoal(null), null)
  const odd = M.normalizeGoal({ ...GOAL, progress_pct: 140, remaining: -5, then: 'sell', until: null, seconds_left: null })
  assert.deepEqual([odd.progress_pct, odd.remaining, odd.then, odd.until, odd.seconds_left], [100, 0, 'normal', null, null])
  assert.deepEqual(M.normalizeGoalResult(LAST), LAST)
  assert.equal(M.normalizeGoalResult({ ...LAST, outcome: 'won' }), null)
  // the status carries both (null when the server sends none)
  const st = M.normalizeStatus({ enabled: true, goal: GOAL, goal_last: LAST })
  assert.deepEqual([st.goal?.target, st.goal_last?.outcome], [12000, 'time'])
  const none = M.normalizeStatus({ enabled: false })
  assert.deepEqual([none.goal, none.goal_last], [null, null])
})

test('the deadline is the next HH:MM and the time left reads in hours and minutes', () => {
  const now = new Date(2026, 10, 17, 14, 30, 0)
  assert.equal(M.deadlineFrom('17:00', now), Math.floor(new Date(2026, 10, 17, 17, 0, 0).getTime() / 1000))
  assert.equal(M.deadlineFrom('09:15', now), Math.floor(new Date(2026, 10, 18, 9, 15, 0).getTime() / 1000)) // tomorrow
  for (const bad of ['', '25:00', '7', 'xx:yy', '10:75']) assert.equal(M.deadlineFrom(bad, now), null)
  assert.deepEqual([M.fmtLeft(30), M.fmtLeft(720), M.fmtLeft(7500)], ['menos de 1 min', '12 min', '2 h 05 min'])
})

test('while it runs the card shows the target, the progress, what is left and the honest notes verbatim', () => {
  const html = render(M.GoalView, props({ goal: GOAL, last: LAST }))
  assert.match(html, /Meta <b class="mono">12\.000,00 \$<\/b>/)
  assert.match(html, /Modo agresivo/)
  assert.match(html, /Faltan <b class="mono">1500,00 \$<\/b>/) // es-ES groups thousands from 5 digits
  assert.match(html, /Quedan <b>2 h 05 min<\/b>/)
  assert.match(html, /\+500,00 \$/)
  assert.ok(html.includes(MODE) && html.includes(NOTE))
  assert.match(html, /cierra sus operaciones \(la cartera se queda\) y vuelve a sus ajustes normales/)
  assert.match(html, /Terminar y cerrar/)
  assert.ok(!html.includes('Empezar un reto'))
  assert.ok(html.includes(LAST.text)) // the last result stays below
  const paused = render(M.GoalView, props({ goal: { ...GOAL, then: 'pause' }, enabled: false }))
  assert.match(paused, /y se pausa/)
  assert.match(paused, /La cuenta está pausada: actívala/)
})

test('without a challenge: the start button, the form and the last result', () => {
  const idle = render(M.GoalView, props())
  assert.match(idle, /Empezar un reto/)
  assert.match(idle, /NEXUS cierra sola sus operaciones/)
  const form = render(M.GoalView, props({ draft: { target: null, time: '17:00', aggressive: true, then: 'normal' }, error: 'La meta debe ser mayor' }))
  assert.match(form, /Meta \(valor de la cuenta, \$\)/)
  assert.match(form, /type="time"[^>]*value="17:00"/)
  assert.match(form, /Un reto no crea ventaja/)
  assert.match(form, /role="alert">La meta debe ser mayor/)
  assert.match(form, /<button[^>]*disabled=""[^>]*>[^<]*(<[^>]+>)*Empezar/) // no target yet
  const done = render(M.GoalView, props({ last: { ...LAST, outcome: 'reached' } }))
  assert.match(done, /Meta cumplida/)
})

test('the REST helpers post the challenge and its end', async () => {
  const calls = []
  globalThis.fetch = async (url, init) => {
    calls.push([String(url), init?.method, init?.body ? JSON.parse(init.body) : null])
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  await M.api.paper.autopilot.startGoal({ target_equity: 12000, until: 1_800_003_600, aggressive: true, then: 'normal' })
  await M.api.paper.autopilot.stopGoal(true)
  assert.deepEqual(calls.map((c) => [c[0].replace(/^https?:\/\/[^/]+/, ''), c[1], c[2]]), [
    ['/api/paper/autopilot/goal', 'POST', { target_equity: 12000, until: 1_800_003_600, aggressive: true, then: 'normal' }],
    ['/api/paper/autopilot/goal/stop', 'POST', { close: true }],
  ])
})

// ----------------------------------------------------------------------------- "Horario de mercados" (AutopilotSession.tsx)

const NOW_MS = new Date(2026, 10, 17, 19, 0, 0).getTime() // a Tuesday evening in the browser's zone
const sec = (d) => Math.floor(d.getTime() / 1000)
const OPENS = sec(new Date(2026, 10, 18, 10, 30)) // tomorrow
const CLOSES = sec(new Date(2026, 10, 18, 17, 0))
const MARKETS = [
  { key: 'crypto', label: 'Cripto y oro (perpetuos)', open: true, opens_at: null, closes_at: null, note: '24 horas, 7 días' },
  { key: 'ny', label: 'Bolsa de Nueva York (acciones y fondos)', open: false, opens_at: OPENS, closes_at: null, note: '9:30–16:00 de Nueva York' },
  { key: 'cme', label: 'Futuros de índices (CME)', open: true, opens_at: null, closes_at: sec(new Date(2026, 10, 18, 18, 0)), note: 'sin festivos' },
]
const SESSION = { enabled: true, active: false, open: false, opens_at: OPENS, closes_at: CLOSES, early_close: false, known: true, mode_text: MODE }

test('market hours: the defensive parse and the times in words', () => {
  assert.deepEqual(M.normalizeMarkets([...MARKETS, { key: 'forex', label: 'x', open: true }, null]), MARKETS)
  assert.deepEqual(M.normalizeSession(SESSION), SESSION)
  const old = M.normalizeSession(undefined, true) // an older server: no session object
  assert.deepEqual([old.enabled, old.active, old.open], [true, false, false])
  assert.equal(M.fmtWhen(OPENS, NOW_MS), 'mañana 10:30')
  assert.equal(M.fmtWhen(sec(new Date(2026, 10, 17, 21, 5)), NOW_MS), 'hoy 21:05')
  assert.match(M.fmtWhen(sec(new Date(2026, 10, 23, 10, 30)), NOW_MS), /^lun 23\/11 10:30$/)
  assert.equal(M.marketWhen(MARKETS[0], NOW_MS), 'no cierra')
  assert.equal(M.marketWhen(MARKETS[1], NOW_MS), 'abre mañana 10:30 (en 15 h 30 min)')
  assert.equal(M.marketWhen(MARKETS[2], NOW_MS), 'cierra mañana 18:00 (en 23 h 00 min)')
})

test('market hours card: the New York session mode, each market and the honest notes', () => {
  const html = render(M.SessionView, { session: SESSION, markets: MARKETS, enabled: true, saving: false, nowMs: NOW_MS, onToggle: noop })
  assert.match(html, /Modo agresivo en la sesión de Nueva York/)
  assert.match(html, /Sesión cerrada: ahora opera con sus ajustes normales; el modo agresivo empieza mañana 10:30\./)
  assert.ok(html.includes(MODE))
  assert.match(html, /Abierto<\/span><span class="ap-market-name">Cripto y oro/)
  assert.match(html, /Cerrado<\/span><span class="ap-market-name">Bolsa de Nueva York/)
  assert.match(html, /NEXUS aún no los opera/)
  assert.match(html, /Nada indica que operar en ese horario dé ventaja/)
  assert.ok(!html.includes('Activo ahora'))
  const active = render(M.SessionView, { session: { ...SESSION, active: true, open: true }, markets: MARKETS, enabled: false, saving: false, nowMs: NOW_MS, onToggle: noop })
  assert.match(active, /Activo ahora/)
  assert.match(active, /Sesión abierta: modo agresivo hasta mañana 17:00/)
  assert.match(active, /La cuenta está pausada/)
  const off = render(M.SessionView, { session: { ...SESSION, enabled: false }, markets: [], enabled: true, saving: false, nowMs: NOW_MS, onToggle: noop })
  assert.match(off, /Apagado: opera con sus ajustes normales a cualquier hora/)
  assert.ok(!off.includes(MODE))
})

// ----------------------------------------------------------------------------- "Cuentas de reto" (AutopilotChallenges.tsx)

const ACCOUNT = { id: 101, name: 'Reto 10 $ → 100 $', balance: 11.2, equity: 12.5, initial_balance: 10, realized_pnl: 1.2,
  unrealized_pnl: 1.3, return_pct: 25, trades: 3, wins: 2, losses: 1, win_rate: 2 / 3, open_positions: 1, auto_trade: true,
  auto_symbols: [], auto_tf: '5m' }
const OPEN = { id: 7, account_id: 101, symbol: 'BINANCE:DOGEUSDT.P', tf: '5m', side: 'long', qty: 25, qty_open: 25, entry_price: 0.06,
  entry_time: 1_800_000_000, sl: 0.059, tp1: 0.061, tp2: 0.062, tp3: 0.063, trail: null, tp_hits: 0, status: 'open', price: 0.0612,
  exit_price: null, exit_time: null, pnl: 1.3, pnl_pct: 2, r_multiple: 0.5, exit_reason: null, exit_label: null, origin: 'autopilot',
  gate_verdict: null, gate_score: null, advice: null, warnings: [], lot: { lots: 25, size: 1, unit: 'coin' }, instrument: null }
const CHALLENGE = {
  id: 101, name: 'Reto 10 $ → 100 $', status: 'running', initial: 10, target: 100, until: null, brake_pct: 50, aggressive: true,
  started_at: 1_800_000_000, ended_at: null, account: ACCOUNT, positions: [OPEN], history: [], brake: { limit_pct: 50, drawdown_pct: 0, active: false },
  log: [{ ts: 1_800_000_000_000, symbol: 'BINANCE:DOGEUSDT.P', kind: 'open', text: 'Abre largo en 5m (rápida) a 0,06' }],
  progress_pct: 2.78, remaining: 87.5, seconds_left: null, result: null,
}
const log = (raw) => (Array.isArray(raw) ? raw : [])
const brake = (raw) => (raw && typeof raw === 'object' ? raw : null)

test('challenge accounts: the defensive parse, the size of a trade and the statuses', () => {
  const [c] = M.normalizeChallenges([CHALLENGE, { id: 'x' }, { ...CHALLENGE, status: 'won' }], log, brake)
  assert.equal(M.normalizeChallenges([CHALLENGE, { id: 'x' }, { ...CHALLENGE, status: 'won' }], log, brake).length, 1)
  assert.deepEqual([c.id, c.name, c.status, c.initial, c.target, c.positions.length, c.log.length], [101, 'Reto 10 $ → 100 $', 'running', 10, 100, 1, 1])
  assert.equal(M.sizeText(c.positions[0]), '25 lotes')
  assert.deepEqual(Object.keys(M.STATUS_LABEL), ['running', 'reached', 'time', 'brake', 'cancelled'])
  assert.deepEqual(M.normalizeChallenges(null, log, brake), [])
})

test('challenge accounts card: each account with its progress, its trades and decisions, and the form', () => {
  const props = { challenges: [CHALLENGE, { ...CHALLENGE, id: 102, name: 'Reto 100 $ → 200 $', status: 'reached', positions: [],
    result: { ...LAST, outcome: 'reached', text: 'Meta cumplida: meta 200,00' } }], enabled: true, draft: null, busy: null, error: null,
    onDraft: noop, onCreate: noop, onStop: noop, onDelete: noop }
  const html = render(M.ChallengesView, props)
  assert.match(html, /Cuentas de reto/)
  assert.match(html, /1 en marcha/)
  assert.match(html, /Reto 10 \$ → 100 \$/)
  assert.match(html, /En marcha/)
  assert.match(html, /Empezó con <b class="mono">10,00 \$<\/b>/)
  assert.match(html, /Faltan <b class="mono">87,50 \$<\/b>/)
  assert.match(html, /2 de 3 cerradas ganadoras/)
  assert.match(html, /DOGEUSDT/)
  assert.match(html, /25 lotes/)
  assert.match(html, /Abre largo en 5m/)
  assert.match(html, /Meta cumplida: meta 200,00/)
  assert.match(html, /Un reto no crea ventaja/)
  assert.equal(html.split('>Terminar<').length - 1, 1) // only the running one
  const form = render(M.ChallengesView, { ...props, challenges: [], draft: { initial: 10, target: 100, brake: 50, until: '', name: '' } })
  assert.match(form, /Saldo inicial/)
  assert.match(form, /type="datetime-local"/)
  const paused = render(M.ChallengesView, { ...props, enabled: false })
  assert.match(paused, /NEXUS automática está pausada/)
})

test('challenge accounts: the REST helpers', async () => {
  const calls = []
  globalThis.fetch = async (url, init) => {
    calls.push([String(url).replace(/^https?:\/\/[^/]+/, ''), init?.method, init?.body ? JSON.parse(init.body) : null])
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  await M.api.paper.autopilot.createChallenge({ initial_balance: 10, target_equity: 100, brake_pct: 50, until: null, name: null })
  await M.api.paper.autopilot.stopChallenge(101)
  await M.api.paper.autopilot.deleteChallenge(101)
  assert.deepEqual(calls, [
    ['/api/paper/autopilot/challenges', 'POST', { initial_balance: 10, target_equity: 100, brake_pct: 50, until: null, name: null }],
    ['/api/paper/autopilot/challenges/101/stop', 'POST', { close: true }],
    ['/api/paper/autopilot/challenges/101', 'DELETE', null],
  ])
})

// ----------------------------------------------------------------------------- a trade's plan (Position.plan)

test('every trade shows its target, or why it has none, and why it opened', () => {
  const withTarget = { why: 'NEXUS dijo VENDER al cierre de la vela de 1h y el filtro dijo TOMAR 78/100', target: 3683.37, target_r: 1.6, no_target: null }
  const noTarget = { why: 'Señal de entrada «Corto» de la estrategia «Supertrend» al cierre de la vela de 15m', target: null, target_r: null,
    no_target: 'La estrategia «Supertrend» no fija objetivo: la cierra su propia regla de salida' }
  const a = render(M.PlanLine, { plan: withTarget })
  assert.match(a, /<b>Objetivo<\/b> <span class="mono">3683,37<\/span> \(1,6R\)/)
  assert.match(a, /<b>Por qué:<\/b> NEXUS dijo VENDER/)
  const b = render(M.PlanLine, { plan: noTarget })
  assert.match(b, /<b>Sin objetivo:<\/b> La estrategia «Supertrend» no fija objetivo/)
  assert.match(b, /Por qué:<\/b> Señal de entrada «Corto»/)
  assert.equal(M.planText({ ...OPEN, plan: withTarget }), 'objetivo 3683,37 (1,6R)')
  assert.match(M.planText({ ...OPEN, plan: noTarget }), /^sin objetivo: La estrategia «Supertrend»/)
  assert.equal(M.planText({ ...OPEN, plan: null }), '—')
  const card = render(M.ChallengesView, { challenges: [{ ...CHALLENGE, positions: [{ ...OPEN, plan: noTarget }] }], enabled: true, draft: null,
    busy: null, error: null, onDraft: noop, onCreate: noop, onStop: noop, onDelete: noop })
  assert.match(card, /sin objetivo: La estrategia «Supertrend»/)
  assert.match(card, /title="Por qué: Señal de entrada «Corto»/)
})

// ----------------------------------------------------------------------------- the detail of a trade (click)

test('a trade opens its detail: why it opened, its target, the parameters it took and how it goes', () => {
  const plan = {
    why: 'Señal de entrada «Corto» de la estrategia «Supertrend» al cierre de la vela de 15m; filtro no exigido (ESPERAR 55/100); NEXUS decía ESPERAR',
    target: null, target_r: null, no_target: 'La estrategia «Supertrend» no fija objetivo: la cierra su propia regla de salida',
    params: [{ label: 'Fuente de la señal', value: 'Estrategia «Supertrend» · entrada «Corto»' }, { label: 'Riesgo por operación', value: '4,0 % del capital' },
      { label: 'Apalancamiento', value: '20x · margen ~187,60 $ · liquidación ~3.925,00' }],
  }
  const html = render(M.TradeDetailBody, { p: { ...OPEN, plan } })
  assert.match(html, /<h4>Por qué abrió<\/h4><p>Señal de entrada «Corto» de la estrategia «Supertrend»/)
  assert.match(html, /<b>Sin objetivo:<\/b> La estrategia «Supertrend» no fija objetivo/)
  assert.match(html, /<h4>Parámetros que tomó al entrar<\/h4>/)
  assert.match(html, /<th scope="row">Riesgo por operación<\/th><td>4,0 % del capital<\/td>/)
  assert.match(html, /<th scope="row">Apalancamiento<\/th><td>20x · margen ~187,60 \$/)
  assert.match(html, /<h4>Cómo va<\/h4>/)
  assert.match(html, /No es asesoría financiera/)
  const target = render(M.TradeDetailBody, { p: { ...OPEN, plan: { ...plan, target: 3683.37, target_r: 0.95, no_target: null, params: [] } } })
  assert.match(target, /<b class="mono">3683,37<\/b> · a 0,95R de la entrada/)
  assert.ok(!target.includes('Parámetros que tomó'))
  const mine = render(M.TradeDetailBody, { p: { ...OPEN, plan: null, tp1: 0.061, tp2: 0.062, tp3: null } })
  assert.match(mine, /Operación sin explicación de NEXUS/)
  const closed = render(M.TradeDetailBody, { p: { ...OPEN, status: 'closed', exit_price: 0.0615, exit_time: 1_800_003_600, exit_label: 'Objetivo 1', plan } })
  assert.match(closed, /<h4>Cómo terminó<\/h4>/)
  assert.match(closed, /· Objetivo 1/)
})

test('a click on a trade (not on its buttons) or Enter opens its detail', () => {
  const opened = []
  const props = M.detailProps(OPEN, (p) => opened.push(p.id))
  assert.equal(props.role, 'button')
  assert.equal(props.tabIndex, 0)
  const on = (tag) => ({ target: { closest: (sel) => (sel.includes(tag) ? {} : null) } })
  props.onClick({ target: { closest: () => null } })
  props.onClick(on('button'))  // its own button keeps its action
  const el = {}
  props.onKeyDown({ key: 'Enter', target: el, currentTarget: el, preventDefault() {} })
  assert.deepEqual(opened, [7, 7])
  assert.deepEqual(M.detailProps(OPEN, undefined), {})
  const tile = render(M.TradeTile, { p: { ...OPEN, plan: { why: 'x', target: null, target_r: null, no_target: 'y', params: [] } }, closingId: null,
    onClose: noop, onShow: noop, onDetail: noop })
  assert.match(tile, /class="dk-tile dk-clickable" role="button" tabindex="0"/)
})
