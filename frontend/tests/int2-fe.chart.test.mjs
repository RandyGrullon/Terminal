// Chart lifetime (lib/chart.ts): after chart.remove() nothing of that chart may be touched again. lightweight-charts
// does not throw at the call: detaching the markers plugin, removing a price line or setting data on a removed chart
// schedules a redraw, and that animation frame throws "Uncaught Error: Object is disposed" (fancy-canvas
// resizeCanvasElement), which is what leaving the chart or PyChart section logged. useChart's cleanup runs before the
// cleanups of the effects declared after it, so removeChart marks the chart and its series first and the layers do
// nothing afterwards. Fake chart / series objects record every call (no DOM needed).
// Run: cd frontend && node --test tests/int2-fe.chart.test.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')

// Browser globals touched at import time by lightweight-charts (nothing is mounted).
globalThis.window ??= globalThis
globalThis.addEventListener ??= () => {}
globalThis.removeEventListener ??= () => {}
globalThis.navigator ??= { userAgent: 'node', language: 'es-ES' }

const bundle = await build({
  stdin: { contents: "export * from './lib/chart'", resolveDir: SRC, loader: 'ts', sourcefile: 'int2-fe-chart-entry.ts' },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  define: { 'process.env.NODE_ENV': '"production"' },
  logLevel: 'silent',
})
const M = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

/** A series that records the calls a layer makes on it. */
function fakeSeries(calls, name = 's') {
  return {
    name,
    attachPrimitive: () => calls.push(`${name}.attachPrimitive`),
    detachPrimitive: () => calls.push(`${name}.detachPrimitive`),
    createPriceLine: (o) => {
      calls.push(`${name}.createPriceLine`)
      return { o }
    },
    removePriceLine: () => calls.push(`${name}.removePriceLine`),
    setData: () => calls.push(`${name}.setData`),
    applyOptions: () => calls.push(`${name}.applyOptions`),
  }
}

/** A chart whose series are listed by panes()[i].getSeries(), like lightweight-charts v5. */
function fakeChart(calls) {
  const series = []
  return {
    series,
    panes: () => [{ getSeries: () => series.slice() }],
    addSeries: () => {
      const s = fakeSeries(calls, `line${series.length}`)
      series.push(s)
      return s
    },
    removeSeries: () => calls.push('chart.removeSeries'),
    removePane: () => calls.push('chart.removePane'),
    remove: () => calls.push('chart.remove'),
  }
}

const LEVEL = { price: 100, color: '#ea3943', title: 'SL · HASTA AQUÍ' }
const MARKER = { time: 1700000000, kind: 'BUY', position: 'belowBar', shape: 'arrowUp', color: '#16c784', text: 'COMPRA' }
const BARS = { time: [1700000000], pos_entry: [1], pos_sl: [0.9], pos_tp1: [1.1], pos_tp2: [1.2], pos_tp3: [1.3] }

test('removeChart marks the chart and every series of its panes, and removes the chart only once', () => {
  const calls = []
  const chart = fakeChart(calls)
  const candles = chart.addSeries()
  assert.equal(M.isChartRemoved(chart), false)
  assert.equal(M.isSeriesRemoved(candles), false)
  M.removeChart(chart)
  M.removeChart(chart)
  assert.deepEqual(calls, ['chart.remove'])
  assert.equal(M.isChartRemoved(chart), true)
  assert.equal(M.isSeriesRemoved(candles), true)
  // a missing chart / series counts as removed: nothing to touch
  assert.equal(M.isChartRemoved(null), true)
  assert.equal(M.isSeriesRemoved(undefined), true)
})

test('after removal the layers never touch the chart (late cleanups and callbacks are no-ops)', () => {
  const calls = []
  const chart = fakeChart(calls)
  const candles = chart.addSeries()
  const markers = new M.MarkerLayer(candles)
  const lines = new M.LevelLines(candles)
  lines.set([LEVEL])
  const trade = new M.TradeLevelsLayer(chart)
  const cone = new M.ConeLayer(chart)
  M.removeChart(chart)
  calls.length = 0
  // what NexusChart / PreviewChart cleanups and late callbacks used to do on the removed chart
  markers.detach()
  markers.set([MARKER], '1h')
  markers.clear()
  lines.clear()
  lines.set([LEVEL])
  trade.set(BARS, '1h')
  trade.clear()
  trade.remove()
  cone.set([{ time: 1700003600, p10: 1, p25: 2, p50: 3, p75: 4, p90: 5 }], { time: 1700000000, price: 3 }, '1h')
  cone.clear()
  cone.remove()
  M.trimPanes(chart, 1)
  assert.deepEqual(calls, [])
})

test('on a live chart the layers still do their work', () => {
  const calls = []
  const chart = fakeChart(calls)
  const candles = chart.addSeries()
  const markers = new M.MarkerLayer(candles)
  const lines = new M.LevelLines(candles)
  lines.set([LEVEL])
  lines.clear()
  markers.detach()
  markers.detach() // detached once only
  markers.set([MARKER], '1h') // a detached plugin is not used again
  const trade = new M.TradeLevelsLayer(chart)
  trade.clear()
  trade.remove()
  assert.ok(calls.includes('line0.attachPrimitive'), calls.join(' '))
  assert.equal(calls.filter((c) => c === 'line0.createPriceLine').length, 1)
  assert.equal(calls.filter((c) => c === 'line0.removePriceLine').length, 1)
  assert.equal(calls.filter((c) => c === 'line0.detachPrimitive').length, 1)
  assert.equal(calls.filter((c) => /^line[1-5]\.setData$/.test(c)).length, 5) // TradeLevelsLayer.clear: its 5 lines
  assert.equal(calls.filter((c) => c === 'chart.removeSeries').length, 5)
})

test('removeChart survives a chart whose panes cannot be listed or whose remove() throws', () => {
  const warn = console.warn
  const warned = []
  console.warn = (...a) => warned.push(a.join(' '))
  try {
    const broken = {
      panes: () => {
        throw new Error('roto')
      },
      remove: () => {
        throw new Error('Object is disposed')
      },
    }
    assert.doesNotThrow(() => M.removeChart(broken))
    assert.equal(M.isChartRemoved(broken), true)
    assert.equal(warned.length, 1)
    assert.match(warned[0], /no se pudo liberar el gráfico/)
  } finally {
    console.warn = warn
  }
})
