// Regression (back-button-exits-app, scanner-view-hijack, contract I8 in the scanner):
// - every section change (menu, openChart from the scanner / paper / terminal, backend "navigate" actions) is a
//   history entry, so the browser's Back / Forward move between sections instead of leaving NEXUS; the first entry
//   is normalized without adding one;
// - a background (automatic) scan finishing never replaces the run the user picked: it is only offered (banner);
// - a gate TOMAR without the NEXUS entry signal is "Filtro favorable · sin señal NEXUS", not a TOMAR opportunity.
import { hist, loc, win } from './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ScanTable from '../src/components/scanner/ScanTable'
import * as scanUtils from '../src/components/scanner/scanUtils'
import { runUiAction, useApp } from '../src/store'

test('the first history entry gets #/chart without adding an entry', () => {
  assert.deepEqual(hist.replaces, ['#/chart'])
  assert.deepEqual(hist.pushes, [])
  assert.equal(useApp.getState().page, 'chart')
})

test('section changes push history entries (menu, openChart, backend navigate)', () => {
  const s = useApp.getState()
  s.setPage('scanner')
  assert.deepEqual(hist.pushes, ['#/scanner'])
  s.setPage('scanner') // same section: no duplicate entry
  assert.deepEqual(hist.pushes, ['#/scanner'])
  useApp.getState().openChart('binance:ethusdt', '4h') // clicking a symbol in the scanner
  assert.deepEqual(hist.pushes, ['#/scanner', '#/chart'])
  assert.equal(useApp.getState().symbol, 'BINANCE:ETHUSDT')
  assert.equal(useApp.getState().tf, '4h')
  runUiAction({ action: 'navigate', page: 'settings' })
  assert.deepEqual(hist.pushes, ['#/scanner', '#/chart', '#/settings'])
  assert.equal(useApp.getState().page, 'settings')
})

test('Back / Forward switch the section shown', () => {
  loc.hash = '#/chart' // Back
  win.dispatch('popstate')
  assert.equal(useApp.getState().page, 'chart')
  loc.hash = '#/scanner' // Back again (this browser fires hashchange)
  win.dispatch('hashchange')
  assert.equal(useApp.getState().page, 'scanner')
  loc.hash = '#/nope' // unknown hash: stay
  win.dispatch('hashchange')
  assert.equal(useApp.getState().page, 'scanner')
  loc.hash = '' // an entry without hash shows the default section
  win.dispatch('popstate')
  assert.equal(useApp.getState().page, 'chart')
  assert.equal(hist.pushes.length, 3, 'history navigation adds no entries')
})

test('a background scan finishing never replaces the run the user picked', () => {
  assert.equal(scanUtils.onRunFinished({ startedByUser: false }, 12), 'offer', 'user is studying run #12: only the banner')
  assert.equal(scanUtils.onRunFinished({ startedByUser: false }, null), 'show', 'the table follows the newest run')
  assert.equal(scanUtils.onRunFinished({ startedByUser: true }, 12), 'show', 'the user launched this scan: show its results')
  assert.equal(scanUtils.onRunFinished({ startedByUser: true }, null), 'show')
})

test('the run on screen, picked while it was running, gets its final results when it finishes', () => {
  // "Este escaneo sigue en curso; los resultados aparecerán al terminar": the followed background run #20 was picked
  // in the history while it ran (the "más reciente" banner is hidden for the run on screen, so only a reload helps).
  assert.equal(scanUtils.onRunFinished({ id: 20, startedByUser: false }, 20, 20), 'refresh')
  assert.equal(scanUtils.onRunFinished({ id: 20, startedByUser: false }, 12, 12), 'offer', 'another run picked: banner only')
  assert.equal(scanUtils.onRunFinished({ id: 20, startedByUser: false }, null, 20), 'refresh', 'following the newest run')
  // a run the page was not following (queued behind another scan) that is on screen
  const other = { id: 21, startedByUser: false, followed: false }
  assert.equal(scanUtils.onRunFinished(other, 21, 21), 'refresh')
  assert.equal(scanUtils.onRunFinished(other, null, 21), 'refresh')
  assert.equal(scanUtils.onRunFinished(other, null, 19), 'offer', 'not on screen: never switches the table')
  assert.equal(scanUtils.onRunFinished(other, 12, 12), 'offer')
})

test('scanner: a gate TOMAR without the NEXUS entry signal is not counted as TOMAR (contract I8)', () => {
  const base = { score: 0.4, prob_up: 0.6, confidence: 0.5, deep: true, direction: 'long', gate_score: 72, reasons: [] }
  const rows = scanUtils.normalizeRows([
    { ...base, rank: 1, symbol: 'A:REAL', action: 'BUY', verdict: 'TAKE', nexus_action: 'BUY', opportunity: true },
    { ...base, rank: 2, symbol: 'B:FAV', action: 'BUY', verdict: 'TAKE', nexus_action: 'WAIT', opportunity: false, gate_score: 80 },
    { ...base, rank: 3, symbol: 'C:WAIT', action: 'BUY', verdict: 'WAIT', nexus_action: 'WAIT', opportunity: false },
    { ...base, rank: 4, symbol: 'D:OLD', action: 'BUY', verdict: 'TAKE' }, // server without the field: the gate decides
  ])
  assert.deepEqual(rows.map((r) => scanUtils.isOpportunity(r)), [true, false, false, true])
  assert.equal(scanUtils.countByFilter(rows).take, 2)
  assert.deepEqual(scanUtils.filterRows(rows, 'take', '').map((r) => r.symbol), ['A:REAL', 'D:OLD'])
  const byVerdict = scanUtils.sortRows(rows, { key: 'verdict', dir: 'desc' }).map((r) => r.symbol)
  assert.deepEqual(byVerdict, ['A:REAL', 'D:OLD', 'B:FAV', 'C:WAIT'], 'favorable sorts below TOMAR even with a higher score')
  const html = renderToStaticMarkup(h(ScanTable, { rows, sort: { key: 'rank', dir: 'asc' }, onSort: () => undefined, tf: '1h' }))
  const cells = html.split('<tr').slice(2) // skip <thead> and its row
  assert.match(cells[0], /TOMAR/)
  assert.doesNotMatch(cells[0], /Filtro favorable/)
  assert.match(cells[1], /Filtro favorable · sin señal NEXUS/)
  assert.doesNotMatch(cells[1].replace(/oportunidad TOMAR/g, ''), /TOMAR/)
})
