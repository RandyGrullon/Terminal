// paper-db: pending limit / stop orders and portfolio risk of the demo account (pure helpers of the ticket,
// "¿Tomo esta operación?", the pending-orders table and the risk panel) + the settings group against the backend
// defaults (backend/app/settings_ext/paper_risk.py).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import type { PaperOrder, PaperRisk } from '../src/api/types'
import {
  KIND_HELP,
  dailyUse,
  distanceText,
  expiryRequest,
  kindLabel,
  localInputToUnix,
  normalizeOrders,
  normalizeRisk,
  orderKindFor,
  orderStatusTone,
  riskBlockDetail,
  unixToLocalInput,
  usageTone,
  usedFraction,
  validatePendingOrder,
} from '../src/components/paper/ordersUtils'
import { GROUPS_EXT } from '../src/components/settings/schema-paper-risk'

const NOW = 1_800_000_000

function order(over: Partial<PaperOrder> = {}): PaperOrder {
  return {
    id: 1, account_id: 1, symbol: 'BINANCE:BTCUSDT', tf: '1h', side: 'long', kind: 'limit', label: 'Compra límite', price: 95,
    qty: null, risk_pct: null, sl: null, tp: null, expiry: 'gtc', expire_bars: null, expire_time: null,
    expiry_text: 'válida hasta cancelarla', status: 'pending', status_label: 'Pendiente', origin: 'manual', gate_verdict: 'TAKE',
    gate_score: 78, forced: false, market_price: 100, distance_pct: -5, executing: false, position_id: null, fill_price: null,
    fill_time: null, reason: null, cancelled_by: null, created_at: NOW * 1000, updated_at: NOW * 1000, ...over,
  }
}

function risk(over: Partial<PaperRisk> = {}): PaperRisk {
  return {
    account_id: 1, equity: 10000, open_risk: 200, open_risk_pct: 2, max_open_risk_pct: 6, open_positions: 2, groups: [],
    max_positions_per_group: 3, crypto_one_group: true, day_start: NOW - (NOW % 86400), day_start_balance: 10000, daily_pnl: -150,
    daily_pnl_pct: -1.5, daily_low: -150, daily_loss_limit_pct: 3, daily_loss_limit: 300, unrealized_pnl: 12, blocked: false,
    block_reason: null, blocked_until: null, pending_orders: 1, note: 'Cuenta demo', ...over,
  }
}

test('the order type follows the side of the market: buy below / sell above = limit, the other way = stop', () => {
  assert.equal(orderKindFor('long', 95, 100), 'limit')
  assert.equal(orderKindFor('long', 105, 100), 'stop')
  assert.equal(orderKindFor('short', 105, 100), 'limit')
  assert.equal(orderKindFor('short', 95, 100), 'stop')
  assert.equal(orderKindFor('long', 100, 100), null)
  assert.equal(orderKindFor('long', 95, null), null)
  assert.equal(orderKindFor('long', null, 100), null)
  assert.equal(kindLabel('short', 'stop'), 'Venta stop')
})

test('pending order validation mirrors the server and suggests the right type', () => {
  assert.equal(validatePendingOrder('long', 'limit', 95, 100, 93, 99), null)
  assert.match(validatePendingOrder('long', 'limit', 101, 100, null, null) ?? '', /compra stop/)
  assert.match(validatePendingOrder('long', 'stop', 99, 100, null, null) ?? '', /compra límite/)
  assert.match(validatePendingOrder('short', 'limit', 99, 100, null, null) ?? '', /venta stop/)
  assert.match(validatePendingOrder('short', 'stop', 101, 100, null, null) ?? '', /venta límite/)
  assert.match(validatePendingOrder('long', 'limit', 95, 100, 96, null) ?? '', /stop loss/)
  assert.match(validatePendingOrder('short', 'limit', 105, 100, null, 106) ?? '', /objetivo/)
  assert.match(validatePendingOrder('long', 'limit', null, 100, null, null) ?? '', /precio de la orden/)
  assert.match(validatePendingOrder('long', 'limit', Number.NaN, 100, null, null) ?? '', /precio de la orden/)
  // without a live price the side cannot be checked here (the server checks it); the levels still are
  assert.equal(validatePendingOrder('long', 'limit', 101, null, null, null), null)
  assert.match(validatePendingOrder('long', 'limit', 101, null, 102, null) ?? '', /stop loss/)
})

test('expiry: until cancelled, N bars (integer 1..1000) or a future date within 365 days', () => {
  assert.deepEqual(expiryRequest({ mode: 'gtc', bars: null, date: '' }, NOW), { ok: true, fields: { expiry: 'gtc' } })
  assert.deepEqual(expiryRequest({ mode: 'bars', bars: 4, date: '' }, NOW), { ok: true, fields: { expiry: 'bars', expire_bars: 4 } })
  for (const bad of [0, 2.5, 1001, null]) assert.equal(expiryRequest({ mode: 'bars', bars: bad, date: '' }, NOW).ok, false)
  const tomorrow = unixToLocalInput(NOW + 86400)
  const res = expiryRequest({ mode: 'date', bars: null, date: tomorrow }, NOW)
  assert.ok(res.ok && res.fields.expiry === 'date' && Math.abs((res.fields.expire_time ?? 0) - (NOW + 86400)) < 60)
  assert.equal(expiryRequest({ mode: 'date', bars: null, date: unixToLocalInput(NOW - 3600) }, NOW).ok, false)
  assert.equal(expiryRequest({ mode: 'date', bars: null, date: unixToLocalInput(NOW + 400 * 86400) }, NOW).ok, false)
  assert.equal(expiryRequest({ mode: 'date', bars: null, date: 'mañana' }, NOW).ok, false)
  assert.equal(localInputToUnix('2026-11-02T14:30'), Math.floor(new Date('2026-11-02T14:30').getTime() / 1000))
  assert.equal(localInputToUnix(''), null)
})

test('server data is parsed defensively', () => {
  assert.deepEqual(normalizeOrders([order(), { id: 2 }, null, 'x', order({ id: 3, kind: 'market' as never })]).map((o) => o.id), [1])
  assert.deepEqual(normalizeOrders(undefined), [])
  assert.equal(normalizeRisk(null), null)
  assert.equal(normalizeRisk({ account_id: 1 }), null)
  const r = normalizeRisk({ ...risk(), groups: [{ group: 'crypto', label: 'Cripto', positions: 2 }, { bad: true }] })
  assert.equal(r?.groups.length, 1)
})

test('a portfolio-risk 409 is told apart from the gate NO TOMAR 409 (only the gate one can be forced)', () => {
  const block = riskBlockDetail({ detail: { message: 'Riesgo abierto máximo superado', code: 'risk_limit', rule: 'open_risk', risk: risk() } })
  assert.equal(block?.rule, 'open_risk')
  assert.equal(block?.risk?.equity, 10000)
  assert.equal(riskBlockDetail({ detail: { message: 'Operación bloqueada por el filtro', gate: { verdict: 'AVOID' } } }), null)
  assert.equal(riskBlockDetail({ detail: 'texto' }), null)
  assert.equal(riskBlockDetail(null), null)
})

test('risk meters: share of each limit used, warn from 80 %, reached = red; gains never consume the daily limit', () => {
  assert.equal(usedFraction(2, 6), 2 / 6)
  assert.equal(usedFraction(8, 6), 1)
  assert.equal(usedFraction(2, null), null)
  assert.equal(usageTone(0.5), 'pos')
  assert.equal(usageTone(0.85), 'warn')
  assert.equal(usageTone(1), 'neg')
  assert.equal(usageTone(null), 'muted')
  assert.equal(dailyUse(risk()), 0.5)
  assert.equal(dailyUse(risk({ daily_pnl: 120, daily_low: 0 })), 0)
  // latched on the worst moment of the day, even after a later recovery
  assert.equal(dailyUse(risk({ daily_pnl: -50, daily_low: -300 })), 1)
  assert.equal(dailyUse(risk({ daily_loss_limit: null, daily_loss_limit_pct: null })), null)
})

test('order display helpers', () => {
  assert.equal(distanceText(order()), '5 % por debajo')
  assert.equal(distanceText(order({ distance_pct: 0.4321 })), '0,43 % por encima')
  assert.equal(distanceText(order({ distance_pct: null })), null)
  assert.equal(orderStatusTone(order({ status: 'filled' })), 'pos')
  assert.equal(orderStatusTone(order({ status: 'cancelled', cancelled_by: 'gate' })), 'warn')
  assert.equal(orderStatusTone(order({ status: 'cancelled', cancelled_by: 'user' })), 'muted')
  assert.equal(orderStatusTone(order({ executing: true })), 'warn')
  // honesty: a demo limit fill on touch is optimistic in a real market, and stops pay slippage
  assert.match(KIND_HELP.limit, /no garantiza la ejecución/)
  assert.match(KIND_HELP.stop, /deslizamiento/)
})

test('the settings group lists exactly the backend defaults of the paper-risk feature', () => {
  const repo = process.env.PAPER_DB_REPO ?? join(process.cwd(), '..')
  const py = readFileSync(join(repo, 'backend', 'app', 'settings_ext', 'paper_risk.py'), 'utf-8')
  const backendKeys = [...py.matchAll(/^\s+"(paper\.[a-z_]+)":/gm)].map((m) => m[1]).sort()
  const fields = GROUPS_EXT.flatMap((g) => g.fields)
  assert.deepEqual(fields.map((f) => f.key).sort(), backendKeys)
  assert.equal(backendKeys.length, 4)
  for (const f of fields) assert.ok(f.label && f.help.length > 40, f.key)
  const byKey = Object.fromEntries(fields.map((f) => [f.key, f]))
  assert.equal(byKey['paper.crypto_one_group'].kind, 'bool')
  assert.equal(byKey['paper.max_positions_per_group'].kind, 'int')
  assert.equal(byKey['paper.daily_loss_limit_pct'].min, 0) // 0 switches the rule off
})
