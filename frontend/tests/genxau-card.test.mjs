// «GenXAU — oro M30» card on the Aprendizaje page (components/brain/GenXauCard.tsx): the funnel of the external GenXAU
// research shown faithfully (the same figures as genxau-mt5/README.md) and in the honest tone of docs/VALIDATION.md:
// «Sin evidencia de ventaja», amber, never green, never «prueba» or «garantía». Bundled with esbuild, no network.
// Run: cd frontend && node --test tests/genxau-card.test.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')
globalThis.window ??= globalThis
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
export { GENXAU, GenXauView } from './components/brain/GenXauCard'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`
const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'genxau-card-entry.tsx' },
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
const html = M.renderToStaticMarkup(M.createElement(M.GenXauView))

test('the funnel and the holdout are those of the research', () => {
  for (const figure of ['44.592', '11.409', '2.418', '516', '5.242', '−0,02', '+0,13', '16 % de las veces', '54 % de las veces',
    '0,28', '0,31', '0,17', '2019-01-01 → 2024-06-30', '2024-07-01 → 2025-11-16', '2025-11-17 → 2026-11-18']) {
    assert.ok(html.includes(figure), figure)
  }
  assert.deepEqual(M.GENXAU.finalists.map((f) => [f.id, f.r, f.pf]), [['#1', -11.5, 0.46], ['#5', -6.4, 0.81], ['#3', 1.9, 1.06], ['#4', 1.1, 1.04], ['#6', 0.3, 1.01]])
  assert.match(html, /−11,5R<\/td><td class="mono">0,46/)
  assert.match(html, /\+1,9R<\/td><td class="mono">1,06/)
  assert.match(html, /los 5 fallan/)
  assert.match(html, /16\.651 estrategias probadas, 0 que aguanten datos nuevos/)
  assert.match(html, /el swap pesa entre el 0 y el 7 %/)
  assert.match(html, /Los costos no explican los fallos/)
})

test('the tone is the honest one of docs/VALIDATION.md', () => {
  assert.match(html, /<span class="badge badge-warn ">Sin evidencia de ventaja<\/span>/)
  assert.match(html, /class="val-verdict val-warn"/)
  assert.ok(!html.includes('badge-pos') && !html.includes('val-pos'), 'never green')
  assert.ok(!/garant|\bprueba\b|indicios de ventaja|rentable/i.test(html), 'no claim of proof, edge or profit')
  assert.match(html, /validarse con datos futuros/)
  assert.match(html, /no es asesoría financiera/)
})
