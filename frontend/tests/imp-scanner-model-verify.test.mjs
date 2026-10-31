// Improvement 5 (scanner-model), adversarial verification of the UI: a reference is only drawn next to a figure of the
// same rows (the first day of a 7-day / 24-h report starts mid-day: its partial reference is left out), and nothing is
// coloured as better or worse than its benchmark without a conclusion (30 windows and an interval that excludes it).
// Run: cd frontend && node --test tests/imp-scanner-model-verify.test.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')

globalThis.window = globalThis
globalThis.addEventListener ??= () => {}
globalThis.removeEventListener ??= () => {}
globalThis.navigator ??= { userAgent: 'node', language: 'es-ES' }
globalThis.location ??= { hash: '', origin: 'http://localhost', href: 'http://localhost/' }
globalThis.history ??= { state: null, replaceState() {}, pushState() {} }
const memory = new Map()
globalThis.localStorage ??= {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null),
  setItem: (k, v) => void memory.set(k, String(v)),
  removeItem: (k) => void memory.delete(k),
}

const ENTRY = `
export * from './components/brain/skill'
export { default as HitRateSparkline } from './components/scanner/HitRateSparkline'
export { default as SkillCard } from './components/brain/SkillCard'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'imp-scanner-model-verify-entry.tsx' },
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

const DAY = 86400
const D0 = 1793232000 // a UTC midnight (unix s)

function group(overrides = {}) {
  return {
    id: 'scanner:crypto|1h|14400',
    label: 'Escáner crypto 1h · horizonte 4 h',
    source: 'scanner',
    model_key: 'crypto|1h|14400',
    n: 400,
    hits: 280,
    accuracy: 0.7,
    windows: 40,
    horizon: '4 h',
    window: '4 h',
    outcomes: { up: 240, down: 160, flat: 90 },
    reference: { kind: 'up', label: 'siempre «alcista»', hits: 240, accuracy: 0.6 },
    chance: 0.52,
    skill_pp: 10,
    skill_ci: [3, 17],
    brier: 0.2,
    brier_ref: 0.24,
    bss: 0.17,
    bss_ci: [0.05, 0.3],
    ece_pp: 4,
    reliability: 0.01,
    resolution: 0.03,
    by_direction: [],
    daily: [0, 1, 2].map((k) => ({ time: D0 + k * DAY, n: 100, hit_rate: 0.7, reference: 0.6, reference_kind: 'up', brier: 0.2, brier_ref: 0.24 })),
    verdict: 'better',
    verdict_text: 'Supera a la referencia',
    brier_text: null,
    ...overrides,
  }
}

test('completeDays keeps only the days the report covers entirely', () => {
  const g = group()
  // a 7-day report generated mid-day: `since` falls inside the first day, whose reference counts only part of its rows
  const partial = M.completeDays({ since: (D0 + 15 * 3600) * 1000, groups: [g] }, g)
  assert.deepEqual([...partial.keys()], [D0 + DAY, D0 + 2 * DAY])
  // a report that starts exactly at midnight, or covers every stored row, keeps every day
  assert.deepEqual([...M.completeDays({ since: D0 * 1000, groups: [g] }, g).keys()], [D0, D0 + DAY, D0 + 2 * DAY])
  assert.equal(M.completeDays({ since: null, groups: [g] }, g).size, 3)
  assert.equal(M.completeDays(null, null).size, 0)
  assert.equal(M.completeDays({ since: 0, groups: [] }, group({ daily: null })).size, 0)
})

test('sameRowsDay: a day whose counts differ from the daily aggregate gets no reference', () => {
  const days = M.completeDays({ since: null, groups: [] }, group())
  assert.equal(M.sameRowsDay(days, D0, 100).reference, 0.6) // same rows
  assert.equal(M.sameRowsDay(days, D0, 101).reference, 0.6) // a resolution arrived after the report was cached
  // 40 days ago with "todo lo guardado": the aggregate counts 16 000 scanner + NEXUS rows, the stored rows only NEXUS
  assert.equal(M.sameRowsDay(days, D0, 16100), null)
  assert.equal(M.sameRowsDay(days, D0 + 5 * DAY, 100), null) // no reference for that day
  assert.equal(M.sameRowsDay(days, D0, null).reference, 0.6) // a point without its count keeps the day's reference
})

test('bssTone colours the Brier only with a conclusion', () => {
  assert.equal(M.bssTone(group()), 'pos') // 40 windows, Brier-skill interval above 0
  assert.equal(M.bssTone(group({ bss_ci: [-0.4, -0.1] })), 'neg')
  assert.equal(M.bssTone(group({ bss_ci: [-0.1, 0.3] })), 'muted') // the interval includes climatology
  // a point value far better than climatology, but in 2 windows: no colour (the backend says "insufficient")
  assert.equal(M.bssTone(group({ windows: 2, brier: 0.1, bss: 0.6, bss_ci: [0.2, 0.9] })), 'muted')
  assert.equal(M.bssTone(group({ bss_ci: null })), 'muted')
  assert.equal(M.bssTone(null), 'muted')
})

test('SkillCard names the window length of a group that mixes horizons', () => {
  const all = group({ id: 'all', label: 'Todas las predicciones', source: null, model_key: null, horizon: '4 h', window: '5 d', windows: 3 })
  const state = { report: { generated_at: 0, days: 7, since: null, min_windows: 30, groups: [group(), all], notes: [] }, unavailable: false, loading: false, error: null }
  const t = M.renderToStaticMarkup(M.createElement(M.SkillCard, { state, range: '7', onRange: () => {} })).replace(/<[^>]+>/g, ' ')
  assert.ok(t.includes('horizonte 4 h · ventanas de 5 d'))
  assert.equal(t.split('ventanas de').length - 1, 1) // only where the window differs from the horizon
})

test('the sparkline never colours one day against its reference', () => {
  const pts = [
    { time: D0, hit_rate: 0.41, n: 1500, reference: 0.62 },
    { time: D0 + DAY, hit_rate: 0.9, n: 1600, reference: 0.51 }, // far above its reference, but one day only
  ]
  const html = M.renderToStaticMarkup(M.createElement(M.HitRateSparkline, { points: pts }))
  assert.ok(!html.includes('var(--pos)') && !html.includes('var(--neg)'))
  const below = M.renderToStaticMarkup(M.createElement(M.HitRateSparkline, { points: [pts[1], { ...pts[0], time: D0 + 2 * DAY }] }))
  assert.ok(!below.includes('var(--neg)') && !below.includes('var(--pos)'))
})
