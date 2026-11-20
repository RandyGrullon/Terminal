// "Espejo en Alpaca (práctica)" in NEXUS automática: the contract types, the REST helpers, the card while it is off,
// connected or failing, its price differences (positive = worse than the simulation) and the reasons for what was not
// mirrored, and its settings group (secrets for the keys). Same harness as copilot-fe.test.mjs: the real sources are
// bundled in memory with Vite's esbuild and rendered with react-dom/server; fetch is faked (no network).
// Run: cd frontend && node --test tests/broker-fe.test.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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
export { BrokerView, fmtBps, normalizeBroker } from './components/paper/BrokerCard'
export { GROUPS, FIELD_BY_KEY } from './components/settings/schema'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'broker-fe-entry.tsx' },
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

const noop = () => {}
const render = (props) =>
  M.renderToStaticMarkup(M.createElement(M.BrokerView, { status: null, error: null, syncing: false, onSync: noop, onSettings: noop, ...props }))

const entry = (over = {}) => ({
  pos_id: 7, symbol: 'NASDAQ:AAPL', alpaca_symbol: 'AAPL', side: 'long', status: 'open', status_label: 'Abierta en Alpaca',
  reason: null, sim_qty: 2.5, held: 2.5, sim_entry: 100, fill_entry: 100.05, sim_exit: null, fill_exit: null,
  slip_entry_bps: 5, slip_exit_bps: null, sim_result_pct: null, mirror_result_pct: null, opened_at: 1, closed_at: null, ...over,
})

const status = (over = {}) => ({
  enabled: true, provider: 'alpaca', paper: true, configured: true, connected: true,
  account: { equity: 100000, cash: 99000.5, buying_power: 200000, status: 'ACTIVE', currency: 'USD' },
  error: null, last_sync_ms: Date.now() - 20_000, mirror_stocks: true, mirror_crypto: true,
  entries: [
    entry(),
    entry({ pos_id: 8, symbol: 'BINANCE:BTCUSDT', alpaca_symbol: 'BTC/USD', fill_entry: 49990, sim_entry: 50000, slip_entry_bps: -2,
      status: 'closed', status_label: 'Cerrada', sim_exit: 51000, fill_exit: 50950, slip_exit_bps: 9.8, sim_result_pct: 1.8, mirror_result_pct: 1.92 }),
    entry({ pos_id: 9, symbol: 'BINANCE:ETHUSDT', alpaca_symbol: 'ETH/USD', side: 'short', status: 'skipped', status_label: 'No replicada',
      reason: 'Alpaca no admite cortos en cripto', fill_entry: null, held: 0, slip_entry_bps: null }),
  ],
  stats: { entries: 3, mirrored: 2, skipped: 1, rejected: 0, closed: 1, slip_entry_bps: 1.5, slip_entry_n: 2, slip_exit_bps: 9.8,
    slip_exit_n: 1, result_diff_pct: 0.12, result_n: 1,
    text: '2 de 3 posiciones replicadas · entrada: 1,5 pb peor que la simulación de media (2) · muestra pequeña (1 cerrada): sin conclusiones' },
  note: 'Cuenta de práctica de Alpaca (paper trading): órdenes con cotizaciones reales y sin dinero real.',
  ...over,
})

test('contract: types-broker.ts has one field per line and is re-exported by types.ts', () => {
  const src = readFileSync(path.join(SRC, 'api', 'types-broker.ts'), 'utf8')
  const fields = (name) => {
    const body = src.split(`export interface ${name} {`)[1].split('\n}')[0]
    return body.split('\n').map((l) => l.trim().match(/^(\w+)(\?)?\s*:/)).filter(Boolean).map((m) => m[1] + (m[2] ?? ''))
  }
  assert.deepEqual(fields('BrokerStatus'), ['enabled', 'provider', 'paper', 'configured', 'connected', 'account', 'error',
    'last_sync_ms', 'mirror_stocks', 'mirror_crypto', 'mirror_perps', 'entries', 'stats', 'note',
    'max_lots?', 'allow_real?', 'blocked?', 'source_account?']) // the last four: MetaTrader 5 only
  assert.deepEqual(fields('BrokerAccount'), ['equity', 'cash', 'buying_power', 'status', 'currency',
    'login?', 'server?', 'company?', 'kind?', 'leverage?']) // the optional ones: MetaTrader 5 only
  assert.equal(fields('BrokerMirrorEntry').length, 19)
  assert.ok(fields('BrokerStats').includes('slip_entry_bps') && fields('BrokerStats').includes('text'))
  assert.ok(readFileSync(path.join(SRC, 'api', 'types.ts'), 'utf8').includes("export * from './types-broker'"))
})

test('client: api.broker.status (with refresh) and api.broker.sync call their routes', async () => {
  const realFetch = globalThis.fetch
  const seen = []
  globalThis.fetch = async (url, init = {}) => {
    seen.push({ url: new URL(String(url), 'http://localhost'), method: init.method ?? 'GET' })
    return new Response(JSON.stringify(status()), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await M.api.broker.status()
    await M.api.broker.status(true)
    await M.api.broker.sync()
  } finally {
    globalThis.fetch = realFetch
  }
  assert.deepEqual(seen.map((s) => [s.method, s.url.pathname, s.url.search]), [
    ['GET', '/api/broker/status', ''],
    ['GET', '/api/broker/status', '?refresh=true'],
    ['POST', '/api/broker/sync', ''],
  ])
})

test('off: what it does, how to set it up, no sync button, never real money', () => {
  const html = render({ status: M.normalizeBroker(status({ enabled: false, connected: false, account: null, entries: [] })) })
  assert.ok(html.includes('Espejo en Alpaca (práctica)') && html.includes('>Desactivado<') && html.includes('Configurarlo en Ajustes'), html)
  assert.ok(html.includes('Nunca usa dinero real') && html.includes('sin dinero real') && !html.includes('Sincronizar'), html)
})

test('connected: the practice account, the stats and each position with its price difference or its reason', () => {
  const html = render({ status: M.normalizeBroker(status()) })
  assert.ok(html.includes('>Conectado<') && html.includes('100.000,00 $') && html.includes('Sincronizar'), html)
  assert.ok(html.includes('2 de 3 posiciones replicadas') && html.includes('sin conclusiones'), html)
  assert.ok(html.includes('Entrada NEXUS 100,00 · Alpaca 100,05 (+5,0 pb)'), html)
  assert.ok(html.includes('(−2,0 pb)') && html.includes('(+9,8 pb)') && html.includes('resultado +1,80 % simulado, +1,92 % en Alpaca'), html)
  assert.ok(html.includes('Alpaca no admite cortos en cripto') && html.includes('No replicada') && html.includes('Corto'), html)
  assert.ok(html.includes('sin dinero real'), 'its honest note')
  assert.equal(M.fmtBps(null), '—')
  assert.equal(M.fmtBps(0), '0,0 pb')
})

test('errors: the backend error and the request error are shown; the badge says it is not connected', () => {
  const html = render({ status: M.normalizeBroker(status({ connected: false, account: null,
    error: 'Alpaca rechazó las claves: usa las de tu cuenta de práctica (paper trading).' })), error: 'No se pudo cargar' })
  assert.ok(html.includes('>Sin conexión<') && html.includes('rechazó las claves') && html.includes('No se pudo cargar'), html)
  assert.equal(M.normalizeBroker({ enabled: 'si' }), null)
  const parsed = M.normalizeBroker({ enabled: true, entries: [entry(), { pos_id: 'x' }, null], stats: null })
  assert.equal(parsed.entries.length, 1)
  assert.equal(parsed.stats.text, '')
  assert.equal(parsed.paper, true)
})

test('settings: the practice-broker group, its keys are secrets, and «Validación» stays last', () => {
  const group = M.GROUPS.find((g) => g.id === 'broker')
  assert.ok(group && group.title === 'Bróker de práctica (Alpaca)' && group.description.includes('nunca dinero real'))
  assert.deepEqual(group.fields.map((f) => f.key), ['broker.alpaca_enabled', 'broker.alpaca_key_id', 'broker.alpaca_secret_key',
    'broker.mirror_stocks', 'broker.mirror_crypto'])
  assert.equal(M.FIELD_BY_KEY['broker.alpaca_key_id'].kind, 'secret')
  assert.equal(M.FIELD_BY_KEY['broker.alpaca_secret_key'].kind, 'secret')
  assert.ok(M.FIELD_BY_KEY['broker.alpaca_key_id'].link.href.startsWith('https://app.alpaca.markets/paper'))
  assert.equal(M.GROUPS[M.GROUPS.length - 1].title, 'Validación')
})

test('binance testnet: its routes, its own texts, «Binance» on the fills and never real money', async () => {
  const realFetch = globalThis.fetch
  const seen = []
  globalThis.fetch = async (url, init = {}) => {
    seen.push({ url: new URL(String(url), 'http://localhost'), method: init.method ?? 'GET' })
    return new Response(JSON.stringify(status({ provider: 'binance_testnet' })), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await M.api.brokerBinance.status()
    await M.api.brokerBinance.status(true)
    await M.api.brokerBinance.sync()
  } finally {
    globalThis.fetch = realFetch
  }
  assert.deepEqual(seen.map((s) => [s.method, s.url.pathname, s.url.search]), [
    ['GET', '/api/broker/binance/status', ''],
    ['GET', '/api/broker/binance/status', '?refresh=true'],
    ['POST', '/api/broker/binance/sync', ''],
  ])
  const note = 'Testnet de Binance (práctica): órdenes reales en un exchange de pruebas con fondos de prueba, nunca dinero real.'
  const off = render({ venue: 'binance', status: M.normalizeBroker(status({ provider: 'binance_testnet', enabled: false, connected: false, note,
    account: null, entries: [] })) })
  assert.ok(off.includes('Espejo en el testnet de Binance (práctica)') && off.includes('Demo Trading de futuros') && off.includes('Nunca usa dinero real'), off)
  assert.ok(!off.includes('Alpaca'), off)
  const perp = entry({ pos_id: 11, symbol: 'BINANCE:BTCUSDT.P', alpaca_symbol: 'BTCUSDT', side: 'short', status_label: 'Abierta en Binance Testnet',
    sim_entry: 50000, fill_entry: 49990, slip_entry_bps: 2 })
  const on = render({ venue: 'binance', status: M.normalizeBroker(status({ provider: 'binance_testnet', mirror_perps: true, entries: [perp],
    note: 'Testnet de Binance (práctica): fondos de prueba, nunca dinero real.',
    account: { equity: 15000.5, cash: 9000, buying_power: 14000.25, status: 'ACTIVE', currency: 'USDT' } })) })
  assert.ok(on.includes('Entrada NEXUS 50.000,0 · Binance 49.990,0 (+2,0 pb)') && on.includes('15.000,50 USDT') && !on.includes('$ USDT') && on.includes('Abierta en Binance Testnet'), on)
  assert.equal(M.normalizeBroker(status({ mirror_perps: true })).mirror_perps, true)
  assert.equal(M.normalizeBroker(status()).mirror_perps, false)
})

test('settings: the Binance testnet group, four secret keys with their testnet links', () => {
  const group = M.GROUPS.find((g) => g.id === 'broker_binance')
  assert.ok(group && group.title === 'Testnet de Binance (práctica)' && group.description.includes('nunca dinero real'))
  assert.deepEqual(group.fields.map((f) => f.key), ['broker.binance_enabled', 'broker.binance_spot_key', 'broker.binance_spot_secret',
    'broker.binance_futures_key', 'broker.binance_futures_secret', 'broker.binance_mirror_spot', 'broker.binance_mirror_perps'])
  for (const key of ['broker.binance_spot_key', 'broker.binance_spot_secret', 'broker.binance_futures_key', 'broker.binance_futures_secret']) {
    assert.equal(M.FIELD_BY_KEY[key].kind, 'secret', key)
  }
  assert.equal(M.FIELD_BY_KEY['broker.binance_spot_key'].link.href, 'https://testnet.binance.vision/')
  assert.equal(M.FIELD_BY_KEY['broker.binance_futures_key'].link.href, 'https://demo.binance.com/en/my/settings/api-management')
})
