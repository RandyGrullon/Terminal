// NEXUS automática's speeds (lib/tradeStyle): «Rápidas» up to 5m, «Medidas» up to 2h, «Largo plazo» above (same
// thresholds as app.sim.paper.trade_style), the summary of each tab («x de n» and the realized P&L), the timeframes in
// its status (fastest first, [tf] when the server sends none) and what the controls send: `tfs` for 2..3 timeframes,
// `tf` for one, never more than three. Same harness as broker-fe.test.mjs.
// Run: cd frontend && node --test tests/trade-style.test.mjs
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
globalThis.matchMedia ??= () => ({ matches: false, addEventListener() {}, removeEventListener() {} })

const ENTRY = `
export * from './lib/tradeStyle'
export { draftChanges, draftFrom, normalizeStatus, validateDraft } from './components/paper/AutopilotView'
`
const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'trade-style-entry.tsx' },
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

const pos = (tf, pnl, over = {}) => ({ id: Math.random(), tf, pnl, status: 'closed', ...over })

test('speeds: the same thresholds as the backend, and the timeframes sorted fastest first', () => {
  assert.deepEqual(['1m', '5m', '15m', '1h', '2h', '4h', '1d', '1w', '??'].map(M.styleOf),
    ['rapida', 'rapida', 'medida', 'medida', 'medida', 'largo', 'largo', 'largo', 'medida'])
  assert.deepEqual(M.sortTfs(['4h', '5m', '1h']), ['5m', '1h', '4h'])
  assert.deepEqual(M.STYLE_ORDER.map((s) => M.STYLE_LABEL[s]), ['Rápidas', 'Medidas', 'Largo plazo'])
  assert.deepEqual(M.STYLE_DEFAULT_TF, { rapida: '5m', medida: '1h', largo: '4h' })
})

test('tab summary: open, «x de n» winners and the realized P&L of one speed or of all', () => {
  const open = [pos('5m', 0, { status: 'open' }), pos('1h', 0, { status: 'open' }), pos('15m', 0, { status: 'open' })]
  const closed = [pos('5m', 4), pos('5m', -2.5), pos('1h', 9), pos('4h', -1)]
  assert.deepEqual(M.styleSummary(open, closed, 'rapida'), { open: 1, closed: 2, wins: 1, pnl: 1.5 })
  assert.deepEqual(M.styleSummary(open, closed, 'medida'), { open: 2, closed: 1, wins: 1, pnl: 9 })
  assert.deepEqual(M.styleSummary(open, closed, 'largo'), { open: 0, closed: 1, wins: 0, pnl: -1 })
  assert.deepEqual(M.styleSummary(open, closed, null), { open: 3, closed: 4, wins: 2, pnl: 9.5 })
})

test('status and controls: tfs fastest first, `tfs` for several, `tf` for one, at most three', () => {
  const raw = { enabled: true, tf: '5m', tfs: ['4h', '5m', '1h', '1h', 'raro'], source: 'list', symbols: [] }
  const st = M.normalizeStatus(raw)
  assert.deepEqual(st.tfs, ['5m', '1h', '4h'])
  assert.deepEqual(M.normalizeStatus({ enabled: true, tf: '15m' }).tfs, ['15m'])
  const saved = M.draftFrom(st)
  assert.deepEqual(saved.tfs, ['5m', '1h', '4h'])
  assert.deepEqual(M.draftChanges({ ...saved, tfs: ['5m', '4h'], tf: '5m' }, saved), { tfs: ['5m', '4h'] })
  assert.deepEqual(M.draftChanges({ ...saved, tfs: ['1h'], tf: '1h' }, saved), { tf: '1h' })
  assert.deepEqual(M.draftChanges(saved, saved), {})
  assert.equal(M.validateDraft({ ...saved, tfs: ['1m', '5m', '1h', '4h'] }), 'Elige de 1 a 3 temporalidades.')
  assert.equal(M.validateDraft({ ...saved, tfs: [] }), 'Elige de 1 a 3 temporalidades.')
})
