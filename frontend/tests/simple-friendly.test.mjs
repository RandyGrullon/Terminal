// Plain-language helpers of the simple mode (components/simple/friendly.ts): why a trade opened in everyday words,
// its speed, where its price stands between the stop and the target, today's results and what NEXUS does now.
// Bundled with esbuild, no network. Run: cd frontend && node --test tests/simple-friendly.test.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')
const bundle = await build({
  entryPoints: [path.join(SRC, 'components/simple/friendly.ts')],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'neutral',
  logLevel: 'silent',
})
const F = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

const pos = (o = {}) => ({
  id: 1, symbol: 'BINANCE:XAUUSDT.P', tf: '15m', side: 'short', status: 'open', entry_price: 3717, entry_time: 1, sl: 3740,
  tp1: null, price: 3710, pnl: 7, exit_time: null, hold: false,
  plan: {
    why: 'Señal de entrada «Corto» de la estrategia «Supertrend + filtro ADX (estrategia)» al cierre de la vela de 15m; filtro no exigido (NO TOMAR 57/100); NEXUS decía ESPERAR',
    target: null, target_r: null, no_target: 'Sale con su propia regla',
    params: [{ label: 'Fuente de la señal', value: 'Estrategia «Supertrend + filtro ADX (estrategia)»' }],
  },
  ...o,
})

test('where the signal came from and why, in plain words', () => {
  assert.deepEqual(F.sourceOf(pos()), { kind: 'strategy', short: 'Supertrend', strategy: 'Supertrend + filtro ADX' })
  assert.match(F.friendlyReason(pos()), /tendencia giró.*bajar/)
  const don = pos({ side: 'long', plan: { ...pos().plan, why: 'Señal de entrada «Largo» de la estrategia «Breakout de Donchian con stop ATR (estrategia)»', params: [] } })
  assert.equal(F.sourceOf(don).short, 'Ruptura')
  assert.match(F.friendlyReason(don), /rompió el máximo/)
  const nexus = pos({ side: 'long', plan: { why: 'NEXUS dijo COMPRAR al cierre de la vela de 1h y el filtro dijo TOMAR 74/100', params: [{ label: 'Fuente de la señal', value: 'Señal de NEXUS' }] } })
  assert.equal(F.sourceOf(nexus).kind, 'nexus')
  assert.equal(F.friendlyReason(nexus), 'El motor NEXUS vio señal para subir y su filtro la aprobó.')
  assert.equal(F.sourceOf(pos({ hold: true })).short, 'Cartera')
  assert.match(F.friendlyReason(pos({ hold: true })), /largo plazo/)
})

test('speed and side in words', () => {
  assert.deepEqual(F.speedOf('15m'), { label: 'Rápida', detail: 'velas de 15 min' })
  assert.equal(F.speedOf('1h').label, 'Media')
  assert.equal(F.speedOf('4h').label, 'Lenta')
  assert.equal(F.sideWords(pos()).hope, 'gana si baja')
})

test('the price bar goes from the stop (0) to the target (1) for both sides', () => {
  // short without target: stop 3740, entry 3717 → right end 3694; price 3710 is on the winning side
  const b = F.priceBarOf(pos())
  assert.equal(b.hasTarget, false)
  assert.equal(b.end, 3694)
  assert.ok(Math.abs(b.entry - 0.5) < 1e-9)
  assert.ok(b.now > b.entry)
  // long with a target
  const l = F.priceBarOf(pos({ side: 'long', sl: 90, entry_price: 100, price: 95, plan: { why: '', target: 120 } }))
  assert.equal(l.hasTarget, true)
  assert.ok(Math.abs(l.now - 5 / 30) < 1e-9 && l.now < l.entry)
  assert.equal(F.priceBarOf(pos({ status: 'closed' })), null)
  assert.equal(F.priceBarOf(pos({ hold: true })), null)
})

test('today counts the trades closed since local midnight', () => {
  const now = new Date(2026, 10, 19, 15, 0).getTime()
  const today = new Date(2026, 10, 19, 9, 0).getTime() / 1000
  const yesterday = new Date(2026, 10, 18, 22, 0).getTime() / 1000
  const t = F.todayOf([pos({ exit_time: today, pnl: 10 }), pos({ exit_time: today, pnl: -4 }), pos({ exit_time: yesterday, pnl: 50 })], now)
  assert.deepEqual(t, { closed: 2, wins: 1, pnl: 6 })
})

test('what NEXUS is doing now', () => {
  const snap = { account: { id: 3 }, autopilot: { enabled: true, session: { active: false } }, risk: { blocked: false } }
  assert.equal(F.nexusNow(snap, 3, [pos()]).title, 'Vigilando 1 operación')
  assert.match(F.nexusNow(snap, 3, [pos()]).detail, /En oro/)
  assert.equal(F.nexusNow(snap, 3, []).title, 'Esperando una señal')
  assert.equal(F.nexusNow({ ...snap, risk: { blocked: true, blocked_until: null } }, 3, []).title, 'Frenada por hoy')
  assert.equal(F.nexusNow({ ...snap, risk: { blocked: true } }, 104, []).title, 'Esperando una señal') // the block is account 3's
  assert.equal(F.nexusNow({ ...snap, autopilot: { enabled: false } }, 3, []).title, 'En pausa')
})

test('the glossary is honest and plain', () => {
  const all = F.GLOSSARY.map((g) => g.text).join(' ')
  assert.ok(!/garant|segur[oa] de ganar|rentable/i.test(all))
  assert.match(all, /menos de 30/i)
})
