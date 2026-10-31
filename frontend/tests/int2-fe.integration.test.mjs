// Integration round 2 (UI): typed REST helpers, the new settings field kinds and groups, Spanish service names, quote
// freshness in the watchlist and the honest wording of the Brier score. The real sources are bundled in memory with
// the esbuild that Vite already installs; components render with react-dom/server; fetch is faked (no network).
// Run: cd frontend && node --test tests/int2-fe.integration.test.mjs
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(HERE, '../src')
const BACKEND = path.resolve(HERE, '../../backend')

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
export { FIELD_BY_KEY, GROUPS } from './components/settings/schema'
export { GROUPS_EXT as VALIDATION_GROUPS, VALIDATION_TF_OPTIONS } from './components/settings/schema-validation'
export { default as SettingField } from './components/settings/SettingField'
export { SERVICE_LABEL } from './components/settings/SystemCard'
export { rowFreshness } from './components/nexus/Watchlist'
export { rateTone } from './components/markets/opsUtils'
export * as ops from './components/markets/opsUtils'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'int2-fe-entry.tsx' },
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

const noop = () => {}
const render = (def, value) =>
  M.renderToStaticMarkup(M.createElement(M.SettingField, { def, value, dirty: false, error: null, markets: [], onChange: noop, onRevert: noop }))
const attr = (html, re, name) => html.match(re)?.[0].match(new RegExp(`\\s${name}="([^"]*)"`))?.[1] ?? null

// ----------------------------------------------------------------------------- REST helpers (client.ts)

test('typed helpers call the routes of paper orders, risk, realtime, the calendar and the skill report', async () => {
  const realFetch = globalThis.fetch
  const seen = []
  globalThis.fetch = async (url, init = {}) => {
    seen.push({ url: String(url), method: init.method ?? 'GET', body: init.body ? JSON.parse(init.body) : undefined })
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await M.api.paper.orders()
    await M.api.paper.orders('all', 10)
    await M.api.paper.placeOrder({ symbol: 'BINANCE:BTCUSDT', side: 'long', kind: 'limit', price: 60000, tf: '1h' })
    await M.api.paper.cancelOrders({ all: true })
    await M.api.paper.risk()
    await M.api.realtime()
    await M.api.macroCalendar({ days: 14, impact: 'high', currency: 'USD,EUR' })
    await M.api.learningSkill({ source: 'scanner', days: 0 })
    await M.api.learningSkill()
  } finally {
    globalThis.fetch = realFetch
  }
  const at = (i) => {
    const u = new URL(seen[i].url, 'http://localhost')
    return [seen[i].method, u.pathname, Object.fromEntries(u.searchParams), seen[i].body]
  }
  assert.deepEqual(at(0), ['GET', '/api/paper/orders', { status: 'pending', limit: '50' }, undefined])
  assert.deepEqual(at(1), ['GET', '/api/paper/orders', { status: 'all', limit: '10' }, undefined])
  assert.deepEqual(at(2), ['POST', '/api/paper/orders', {}, { symbol: 'BINANCE:BTCUSDT', side: 'long', kind: 'limit', price: 60000, tf: '1h' }])
  assert.deepEqual(at(3), ['POST', '/api/paper/orders/cancel', {}, { all: true }])
  assert.deepEqual(at(4), ['GET', '/api/paper/risk', {}, undefined])
  assert.deepEqual(at(5), ['GET', '/api/realtime', {}, undefined])
  assert.deepEqual(at(6), ['GET', '/api/macro/calendar', { days: '14', impact: 'high', currency: 'USD,EUR' }, undefined])
  assert.deepEqual(at(7), ['GET', '/api/learning/skill', { source: 'scanner', days: '0' }, undefined]) // 0 = every stored row
  assert.deepEqual(at(8), ['GET', '/api/learning/skill', {}, undefined]) // server default: 7 days
})

// ----------------------------------------------------------------------------- canonical types (types.ts)

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

test('types.ts holds the skill report types; a local copy in brain/skill.ts may not drift from them', () => {
  const canonical = interfaces(readFileSync(path.join(SRC, 'api', 'types.ts'), 'utf8'))
  const skillSrc = readFileSync(path.join(SRC, 'components', 'brain', 'skill.ts'), 'utf8')
  const local = interfaces(skillSrc)
  for (const name of ['SkillReference', 'SkillDirection', 'SkillDaily', 'SkillGroup', 'SkillReport']) {
    assert.ok(canonical[name], `${name} en types.ts`)
    if (local[name]) assert.deepEqual(local[name], canonical[name], `${name}: skill.ts y types.ts difieren`)
  }
  assert.ok(canonical.SkillGroup.includes('window?') && canonical.SkillGroup.includes('bss_ci'))
  for (const alias of ['SkillVerdict', 'OutcomeKind']) {
    const def = (src) => src.match(new RegExp(`export type ${alias} = ([^\\n]+)`))?.[1]
    const types = readFileSync(path.join(SRC, 'api', 'types.ts'), 'utf8')
    assert.ok(def(types), alias)
    if (def(skillSrc)) assert.equal(def(skillSrc), def(types), alias)
  }
  // optional fields of the other features, also canonical now (project rule 6)
  assert.ok(['source?', 'realtime?', 'delay_s?', 'market_open?'].every((f) => canonical.Quote.includes(f)))
  assert.deepEqual(canonical.MacroCalendarSource, ['id', 'label', 'live', 'ok', 'updated_at', 'until', 'error'])
  assert.deepEqual(canonical.MacroCalendarResponse, ['events', 'coverage?'])
})

// ----------------------------------------------------------------------------- settings

test('"Validación" group: the three keys of the backend defaults, with the run limits', () => {
  const g = M.GROUPS.find((x) => x.id === 'validation')
  assert.ok(g, 'grupo Validación')
  assert.equal(M.GROUPS[M.GROUPS.length - 1], g)
  assert.equal(g.title, 'Validación')
  assert.match(g.description, /no una garantía/)
  const backend = readFileSync(path.join(BACKEND, 'app', 'settings_ext', 'validation.py'), 'utf8')
  const backendKeys = new Set(Array.from(backend.matchAll(/"(validation\.[a-z_]+)":/g), (m) => m[1]))
  assert.deepEqual(g.fields.map((f) => f.key), ['validation.symbols', 'validation.tfs', 'validation.bars'])
  for (const f of g.fields) assert.ok(backendKeys.has(f.key), `${f.key} existe en el backend`)
  const by = Object.fromEntries(g.fields.map((f) => [f.key, f]))
  assert.equal(by['validation.symbols'].kind, 'symbols')
  assert.match(by['validation.symbols'].help, /12 símbolos y 24 series/)
  assert.equal(by['validation.tfs'].kind, 'multi')
  assert.deepEqual(by['validation.tfs'].options.map((o) => o.value), ['15m', '1h', '4h', '1d'])
  assert.deepEqual([by['validation.bars'].kind, by['validation.bars'].min, by['validation.bars'].max], ['int', 1000, 5000])
  // the method parameters stay out of the form on purpose
  assert.ok(!M.FIELD_BY_KEY['validation.folds'] && !M.FIELD_BY_KEY['validation.random_runs'])
})

test('scanner.replay_days is in the scanner group, 0..30 days, and the backend knows the key', () => {
  const def = M.FIELD_BY_KEY['scanner.replay_days']
  assert.ok(def)
  assert.ok(M.GROUPS.find((x) => x.id === 'scanner').fields.includes(def))
  assert.deepEqual([def.kind, def.min, def.max, def.unit], ['int', 0, 30, 'días'])
  assert.match(def.help, /skill replay/)
  assert.match(def.help, /0 = no guardarlas/)
  // DEFAULT_SETTINGS = app/config.py plus the feature modules of app/settings_ext (a key it lacks cannot be saved)
  const ext = readdirSync(path.join(BACKEND, 'app', 'settings_ext')).filter((f) => f.endsWith('.py'))
  const all = [path.join(BACKEND, 'app', 'config.py'), ...ext.map((f) => path.join(BACKEND, 'app', 'settings_ext', f))]
    .map((p) => readFileSync(p, 'utf8'))
    .join('\n')
  assert.match(all, /"scanner\.replay_days":/)
})

test('every settings key appears once across the groups', () => {
  const keys = M.GROUPS.flatMap((g) => g.fields.map((f) => f.key))
  assert.equal(new Set(keys).size, keys.length)
  assert.equal(Object.keys(M.FIELD_BY_KEY).length, keys.length)
})

test('text kind: a plain labelled input with its placeholder and keyboard, nothing masked', () => {
  const def = { key: 'x.host', label: 'Servidor', help: 'Nombre del servidor.', kind: 'text', placeholder: 'p. ej. smtp.gmail.com', inputMode: 'url' }
  const html = render(def, 'smtp.example.org')
  const forId = attr(html, /<label[^>]*class="set-label"[^>]*>/, 'for')
  const input = html.match(/<input[^>]*>/)?.[0] ?? ''
  assert.ok(forId && attr(input, /<input[^>]*>/, 'id') === forId, html)
  assert.equal(attr(input, /<input[^>]*>/, 'type'), 'text')
  assert.equal(attr(input, /<input[^>]*>/, 'placeholder'), 'p. ej. smtp.gmail.com')
  assert.equal(attr(input, /<input[^>]*>/, 'inputMode') ?? attr(input, /<input[^>]*>/, 'inputmode'), 'url')
  assert.equal(attr(input, /<input[^>]*>/, 'value'), 'smtp.example.org')
  assert.equal(attr(input, /<input[^>]*>/, 'aria-describedby'), attr(html, /<span[^>]*class="set-help"[^>]*>/, 'id'))
  assert.ok(!html.includes('Mostrar'), 'no show / hide button')
})

test('password kind: masked like a key, with password wording', () => {
  const def = { key: 'x.pass', label: 'Contraseña', help: 'Puede tener espacios.', kind: 'password' }
  const html = render(def, 'abcd efgh ijkl mnop')
  assert.equal(attr(html, /<input[^>]*>/, 'type'), 'password')
  assert.match(html, /title="Mostrar contraseña"/)
  assert.match(html, /title="Borrar contraseña"/)
  const key = render({ key: 'x.key', label: 'Clave', help: '', kind: 'secret' }, 'abc')
  assert.match(key, /title="Mostrar clave"/)
})

test('multi kind: a labelled group of toggle chips; a saved value outside the options is still shown', () => {
  const def = M.FIELD_BY_KEY['validation.tfs']
  const html = render(def, ['1h', '4h', '2h'])
  const labelId = attr(html, /<span[^>]*class="set-label"[^>]*>/, 'id')
  assert.ok(labelId)
  assert.equal(attr(html, /<div[^>]*role="group"[^>]*>/, 'aria-labelledby'), labelId)
  const chips = Array.from(html.matchAll(/<button[^>]*aria-pressed="(true|false)"[^>]*>([^<]*)<\/button>/g), (m) => `${m[2]}=${m[1]}`)
  assert.deepEqual(chips, ['15m=false', '1h=true', '4h=true', '1d=false', '2h=true'])
})

// ----------------------------------------------------------------------------- system card

test('every backend service has a Spanish name in the system card', () => {
  const main = readFileSync(path.join(BACKEND, 'app', 'main.py'), 'utf8')
  const block = main.match(/^SERVICES:[^=]*=\s*\[([\s\S]*?)\n\]/m)?.[1] ?? ''
  const ids = Array.from(block.matchAll(/\("(\w+)", "app\./g), (m) => m[1])
  assert.ok(ids.length >= 11, ids.join(','))
  for (const id of [...ids, 'ws']) assert.ok(M.SERVICE_LABEL[id], `sin nombre: ${id}`)
  assert.equal(M.SERVICE_LABEL.alerts, 'Alertas externas')
  assert.equal(M.SERVICE_LABEL.validation, 'Validación walk-forward')
  assert.equal(M.SERVICE_LABEL.macro, 'Semáforo de mercado')
})

// ----------------------------------------------------------------------------- watchlist freshness

const NOW = Date.UTC(2026, 9, 30, 15, 0)
const item = (symbol, quote) => ({ symbol, ticker: symbol.split(':')[1], exchange: symbol.split(':')[0], quote })
const quote = (extra) => ({ symbol: 'X', price: 10, change_pct: 0.1, change: 0.01, volume: 1, high: 11, low: 9, open: 10, ts: NOW - 5_000, ...extra })

test('watchlist rows say how fresh the price is only when the source tells', () => {
  const f = (it, readAt = NOW - 2_000) => M.rowFreshness(it, readAt, NOW)
  assert.deepEqual(
    { ...f(item('BINANCE:BTCUSDT', quote({ source: 'binance', realtime: true }))), title: undefined },
    { text: 'Tiempo real', tone: 'pos', title: undefined },
  )
  const delayed = f(item('NASDAQ:AAPL', quote({ source: 'tradingview', realtime: false, delay_s: 900 })))
  assert.deepEqual([delayed.text, delayed.tone], ['Retraso 15 min', 'warn'])
  assert.match(delayed.title, /retraso de 15 min/)
  assert.equal(f(item('NASDAQ:AAPL', quote({ source: 'twelvedata', realtime: true, market_open: false }))).text, 'Cerrado')
  assert.equal(f(item('NASDAQ:AAPL', quote({ stale: true, ts: NOW - 7_200_000 }))).text, 'Desactualizado')
  assert.equal(f(item('NASDAQ:AAPL', quote({ source: 'twelvedata', realtime: true, ts: NOW - 240_000 }))).text, 'hace 4 min')
  assert.equal(f(item('SIM:NEXUS', quote({}))).text, 'Sintético')
  // nothing is claimed: a source that does not say, no quote, or a read older than 3 min
  assert.equal(f(item('NASDAQ:AAPL', quote({ source: 'tradingview' }))), null)
  assert.equal(f(item('NASDAQ:AAPL', null)), null)
  assert.equal(f(item('BINANCE:BTCUSDT', quote({ source: 'binance', realtime: true })), NOW - 600_000), null)
})

// ----------------------------------------------------------------------------- honest wording

test('no screen calls 0,25 or 50 % the chance level any more', () => {
  const files = []
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = path.join(dir, name)
      if (statSync(p).isDirectory()) walk(p)
      else if (/\.(ts|tsx)$/.test(name)) files.push(p)
    }
  }
  walk(SRC)
  const bad = files.filter((p) => /0,25 = azar|50 % = azar|50 % \(azar\)/.test(readFileSync(p, 'utf8')))
  assert.deepEqual(bad.map((p) => path.relative(SRC, p)), [])
  const panel = readFileSync(path.join(SRC, 'components', 'nexus', 'SignalPanel.tsx'), 'utf8')
  assert.match(panel, /0,25 = decir siempre 50 %; la referencia honesta es la climatología del periodo/)
})

test('rateTone has no hidden "coin flip" default: the caller states its thresholds', () => {
  assert.equal(M.rateTone(0.6, 0.5, 0.35), 'pos')
  assert.equal(M.rateTone(40, 0.5, 0.35), 'warn') // percentages are accepted too
  assert.equal(M.rateTone(0.2, 0.5, 0.35), 'neg')
  assert.equal(M.rateTone(null, 0.5, 0.35), 'muted')
  assert.equal(M.ops.brierTone, undefined) // the unused "0.25 = coin flip" helper is gone
})
