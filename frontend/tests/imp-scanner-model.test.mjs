// Improvement 5 (scanner-model): honest baselines in the UI. Predictions have three outcomes (up, down, neutral inside
// the flat band), so no screen may present 50 % as the chance level: hit rates are compared with their reference
// (always the most frequent outcome), the Brier score with climatology, and nothing is concluded below 30 independent
// windows. The real sources are bundled in memory with the esbuild that Vite already installs; components render with
// react-dom/server. Run: cd frontend && node --test tests/imp-scanner-model.test.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')

const memory = new Map()
globalThis.window = globalThis
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
export * from './components/brain/skill'
export { default as SkillCard } from './components/brain/SkillCard'
export { default as LoopExplainer } from './components/brain/LoopExplainer'
export { default as HitRateSparkline } from './components/scanner/HitRateSparkline'
export { ScanHelpCard } from './components/scanner/ScanAccuracyCard'
export { brierTone, rateTone } from './components/brain/learnFormat'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'imp-scanner-model-entry.tsx' },
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
const text = (html) => html.replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ')

// ----------------------------------------------------------------------------- fixtures

function group(overrides = {}) {
  return {
    id: 'scanner:crypto|1h|14400',
    label: 'Escáner crypto 1h · horizonte 4 h',
    source: 'scanner',
    model_key: 'crypto|1h|14400',
    n: 2515,
    hits: 1238,
    accuracy: 1238 / 2515,
    windows: 2,
    horizon: '4 h',
    outcomes: { up: 1976, down: 539, flat: 694 },
    reference: { kind: 'up', label: 'siempre «alcista»', hits: 1976, accuracy: 1976 / 2515 },
    chance: 0.469,
    skill_pp: -29.3,
    skill_ci: null,
    brier: 0.255,
    brier_ref: 0.168,
    bss: -0.516,
    bss_ci: null,
    ece_pp: 27.2,
    reliability: 0.08,
    resolution: 0.01,
    by_direction: [
      { direction: 1, label: 'alcista', n: 1066, hits: 818, accuracy: 818 / 1066, base_rate: 0.786 },
      { direction: -1, label: 'bajista', n: 940, hits: 119, accuracy: 119 / 940, base_rate: 0.214 },
      { direction: 0, label: 'neutral', n: 509, hits: 301, accuracy: 301 / 509, base_rate: 0.276 },
    ],
    daily: [{ time: 1793318400, n: 2515, hit_rate: 0.492, reference: 0.786, reference_kind: 'up', brier: 0.255, brier_ref: 0.168 }],
    verdict: 'insufficient',
    verdict_text:
      'Muestra insuficiente: aciertos 1.238 de 2.515 (49,2 %) frente a 1.976 de 2.515 (78,6 %, siempre «alcista») en solo 2 ventanas independientes de 4 h; se necesitan al menos 30 para sacar conclusiones.',
    brier_text: 'Brier 0,255 frente a 0,168 de la climatología (habilidad −52 %)',
    ...overrides,
  }
}

function report(groups) {
  return { generated_at: Date.now(), days: null, since: null, min_windows: 30, groups, notes: ['Referencia: acertar siempre el resultado más frecuente.'] }
}

// ----------------------------------------------------------------------------- helpers

test('number formatting of the skill helpers is Spanish and signed', () => {
  assert.equal(M.fmtOfN(15, 20), '15 de 20')
  // es-ES groups thousands from five digits on (fmtInt = Intl es-ES, like the rest of the interface): CLDR's minimum
  // grouping digits for Spanish is 2, as the RAE rule (no separator in four-digit numbers). The backend texts
  // (verdict_text) group four digits too ("1.238"): see the fixture below.
  assert.equal(M.fmtOfN(1238, 2515), '1238 de 2515')
  assert.equal(M.fmtOfN(12380, 25150), '12.380 de 25.150')
  assert.equal(M.fmtPp(3.24), '+3,2 pp')
  assert.equal(M.fmtPp(-29.31), '−29,3 pp')
  assert.equal(M.fmtPp(null), '—')
  assert.equal(M.fmtCi([-4.12, 6.0]), '−4,1 a +6,0 pp')
  assert.equal(M.fmtBss(0.124), '+12 %')
  assert.equal(M.fmtBss(-0.516), '−52 %')
  assert.equal(M.fmtReference({ kind: 'up', label: 'siempre «alcista»', hits: 7, accuracy: 0.7 }), '70,0 % (siempre «alcista»)')
  assert.equal(M.periodLabel(null), 'filas guardadas')
  assert.equal(M.periodLabel(7), 'últimos 7 días')
  assert.equal(M.periodLabel(1), 'últimas 24 h')
  assert.equal(M.windowDays('all'), null)
  assert.equal(M.windowDays('7'), 7)
})

test('only a conclusion is coloured: insufficient or no edge stays neutral', () => {
  assert.equal(M.skillTone(group()), 'muted')
  assert.equal(M.skillTone(group({ verdict: 'no_edge' })), 'muted')
  assert.equal(M.skillTone(group({ verdict: 'better' })), 'pos')
  assert.equal(M.skillTone(group({ verdict: 'worse' })), 'neg')
  assert.equal(M.skillTone(null), 'muted')
  // Brier is judged against its climatology; without one it is not judged at all (0,25 is not "chance")
  assert.equal(M.brierTone(0.24), 'muted')
  assert.equal(M.brierTone(0.2, 0.24), 'pos')
  assert.equal(M.brierTone(0.3, 0.24), 'neg')
})

test('climatology and Brier skill from the calibration bins', () => {
  const clim = M.climatology([
    { bin: 3, predicted: 0.35, observed: 0.2, count: 50 },
    { bin: 6, predicted: 0.65, observed: 0.8, count: 150 },
  ])
  assert.equal(clim.n, 200)
  assert.ok(Math.abs(clim.upRate - 0.65) < 1e-9)
  assert.ok(Math.abs(clim.brierRef - 0.65 * 0.35) < 1e-9)
  assert.ok(Math.abs(clim.signRef - 0.65) < 1e-9)
  assert.equal(clim.signKind, 'up')
  assert.equal(M.climatology([]), null)
  assert.ok(Math.abs(M.brierSkill(0.2, 0.25) - 0.2) < 1e-9)
  assert.equal(M.brierSkill(0.2, 0), null)
})

test('normalizeSkill and the group pickers are defensive', () => {
  const r = M.normalizeSkill({
    groups: [group(), { id: 'nexus', label: 'NEXUS', source: 'nexus', n: 'x', by_direction: null, daily: null, verdict: 'raro' }, null],
  })
  assert.equal(r.groups.length, 2)
  assert.equal(r.groups[1].n, 0)
  assert.deepEqual(r.groups[1].by_direction, [])
  assert.equal(r.groups[1].verdict, 'insufficient')
  assert.equal(r.min_windows, 30)
  assert.equal(M.normalizeSkill(null), null)
  assert.equal(M.scannerGroup(r, 'crypto|1h|14400').id, 'scanner:crypto|1h|14400')
  assert.equal(M.sourceGroup(r, 'nexus').id, 'nexus')
  assert.equal(M.overallGroup(report([group()])).id, 'scanner:crypto|1h|14400') // the only group
  assert.equal(M.overallGroup(report([group(), group({ id: 'all', label: 'Todas' })])).id, 'all')
})

test('fetchSkill: an older server without the route answers null, not an error', async () => {
  const realFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404 })
    assert.equal(await M.fetchSkill({ days: 7 }), null)
    let asked = ''
    globalThis.fetch = async (url) => {
      asked = String(url)
      return new Response(JSON.stringify(report([group()])), { status: 200 })
    }
    const r = await M.fetchSkill({ source: 'scanner', days: null })
    assert.equal(r.groups[0].hits, 1238)
    assert.ok(asked.includes('/api/learning/skill') && asked.includes('source=scanner') && asked.includes('days=0'), asked)
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Error interno' }), { status: 500 })
    await assert.rejects(() => M.fetchSkill({}))
  } finally {
    globalThis.fetch = realFetch
  }
})

// ----------------------------------------------------------------------------- components

const noop = () => {}

test('SkillCard: x de n, the reference, chance, climatology and the honest verdict', () => {
  const better = group({
    id: 'nexus',
    label: 'NEXUS (señales)',
    source: 'nexus',
    model_key: null,
    n: 400,
    hits: 280,
    accuracy: 0.7,
    windows: 40,
    reference: { kind: 'up', label: 'siempre «alcista»', hits: 240, accuracy: 0.6 },
    skill_pp: 10,
    skill_ci: [3.1, 16.9],
    bss: 0.125,
    verdict: 'better',
    verdict_text: 'Supera a la referencia: aciertos 280 de 400 (70,0 %) frente a 240 de 400 (60,0 %, siempre «alcista»), diferencia +10,0 pp. Es una medición del pasado, no una garantía.',
  })
  const html = render(M.SkillCard, {
    state: { report: report([group(), better]), unavailable: false, loading: false, error: null },
    range: 'all',
    onRange: noop,
  })
  const t = text(html)
  assert.ok(t.includes('Habilidad frente a la referencia'))
  assert.ok(t.includes('el 50 % no es el azar'))
  assert.ok(t.includes('1.238 de 2.515') && t.includes('siempre «alcista»'))
  assert.ok(t.includes('Muestra insuficiente') && t.includes('2 ventanas independientes'))
  assert.ok(t.includes('Supera la referencia') && t.includes('+3,1 a +16,9 pp'))
  assert.ok(t.includes('habilidad −52 %') && t.includes('habilidad +13 %'))
  assert.ok(t.includes('Predicción neutral: 301 de 509') && t.includes('ese resultado ocurrió en el 27,6 % de todas'))
  assert.ok(!/50 % = azar|0,25 = azar|azar 50/.test(t))
  assert.ok(html.includes('pos')) // the "better" row is coloured, the insufficient one is not
})

test('SkillCard: unavailable, empty and loading states never show a fake baseline', () => {
  const off = text(render(M.SkillCard, { state: { report: null, unavailable: true, loading: false, error: null }, range: '7', onRange: noop }))
  assert.ok(off.includes('todavía no ofrece el informe de habilidad') && off.includes('no con un 50 %'))
  const empty = text(
    render(M.SkillCard, { state: { report: report([group({ n: 0, hits: 0 })]), unavailable: false, loading: false, error: null }, range: 'all', onRange: noop }),
  )
  assert.ok(empty.includes('Aún no hay predicciones verificadas en este periodo'))
  const loading = text(render(M.SkillCard, { state: { report: null, unavailable: false, loading: true, error: null }, range: 'all', onRange: noop }))
  assert.ok(loading.includes('Calculando la habilidad'))
})

test('the explainer and the scanner help no longer call 50 % a coin flip', () => {
  const loop = text(render(M.LoopExplainer, {}))
  assert.ok(loop.includes('el 50 % no es el azar') && loop.includes('al menos 30 ventanas independientes'))
  assert.ok(!loop.includes('Un 50 % equivale a lanzar una moneda'))
  const help = text(render(M.ScanHelpCard, {}))
  assert.ok(help.includes('neutral') && help.includes('no con un 50 %') && help.includes('150 lecciones'))
})

test('HitRateSparkline draws the daily reference instead of a 50 % line', () => {
  const pts = [
    { time: 1793232000, hit_rate: 0.41, n: 1500, reference: 0.62 },
    { time: 1793318400, hit_rate: 0.55, n: 1600, reference: 0.51 },
  ]
  const html = render(M.HitRateSparkline, { points: pts })
  const t = text(html)
  assert.ok(t.includes('discontinua = referencia del día'))
  assert.ok(!t.includes('50 % (azar)'))
  assert.ok(html.includes('stroke-dasharray'))
  assert.ok(t.includes('referencia 62,0 %'))
  const plain = text(render(M.HitRateSparkline, { points: pts.map(({ reference, ...p }) => p) }))
  assert.ok(plain.includes('sin referencia diaria'))
})
