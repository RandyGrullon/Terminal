// ValidationCard, integration round: no 95 % interval is shown for fewer than 10 trades (also when an older stored
// report still carries one, as the fixture does), 10 to 29 trades read "muy incierto", and the drawdown shown next to
// the random baseline is the same measure for both (closed trades), with the account's drawdown labelled per bar.
import './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MIN_CI_N, NO_CI_TITLE, ValidationReportView, ciText, fmtCi } from '../src/components/brain/ValidationCard'
import { fmtPct } from '../src/lib/format'
import fixture from './imp-validation.fixture.json'

/* eslint-disable @typescript-eslint/no-explicit-any */
const clone = (x: unknown): any => JSON.parse(JSON.stringify(x))
const text = (html: string) =>
  html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')

const series = (rep: any, symbol: string, tf: string) => rep.series.find((s: any) => s.symbol === symbol && s.tf === tf)

test('interval text by sample size', () => {
  assert.equal(MIN_CI_N, 10)
  assert.equal(ciText([-0.57, 0.85], 0), '—')
  assert.equal(ciText([-0.57, 0.85], 6), 'sin intervalo (menos de 10)') // an older report's interval is hidden
  assert.equal(ciText(null, 9), 'sin intervalo (menos de 10)')
  assert.equal(ciText([-0.57, 0.85], 10), `${fmtCi([-0.57, 0.85])} · muy incierto`)
  assert.equal(ciText([-0.57, 0.85], 29), `${fmtCi([-0.57, 0.85])} · muy incierto`)
  assert.equal(ciText([-0.57, 0.85], 30), fmtCi([-0.57, 0.85]))
  assert.equal(ciText(null, 40), 'sin intervalo')
})

test('series rows: a handful of trades shows no interval, 10 to 29 trades are flagged', () => {
  const alfa = series(fixture, 'SIM:ALFA', '1h')
  const beta = series(fixture, 'SIM:BETA', '1h')
  assert.ok(alfa.overall.nexus.trades < 10 && alfa.overall.nexus.ci95, 'the fixture is an older report with an interval')
  assert.ok(beta.overall.nexus.trades >= 10 && beta.overall.nexus.trades < 30)
  const html = renderToStaticMarkup(h(ValidationReportView, { report: fixture as any }))
  const t = text(html)
  assert.ok(!t.includes(fmtCi(alfa.overall.nexus.ci95)), 'no interval for 7 trades')
  assert.ok(t.includes('sin intervalo (menos de 10)'))
  assert.ok(html.includes(`title="${NO_CI_TITLE}"`), 'the reason is explained on hover')
  assert.ok(t.includes(`${fmtCi(beta.overall.nexus.ci95)} · muy incierto`))
  const all = fixture.groups[fixture.groups.length - 1]
  assert.ok(all.trades >= 30 && t.includes(fmtCi(all.ci95_blocks as any)))
  assert.ok(!t.includes(`${fmtCi(all.ci95_blocks as any)} · muy incierto`))
})

test('an expanded series hides the intervals of periods with fewer than 10 trades', () => {
  const s = series(fixture, 'SIM:ALFA', '1h')
  const t = text(renderToStaticMarkup(h(ValidationReportView, { report: fixture as any, open: `${s.symbol}|${s.tf}` })))
  const small = s.folds.find((f: any) => f.nexus.trades > 0 && f.nexus.trades < 10 && f.nexus.ci95)
  assert.ok(small, 'the fixture has a period with a few trades and an interval')
  assert.ok(!t.includes(fmtCi(small.nexus.ci95)))
  assert.ok(!t.includes(fmtCi(s.overall.mechanical.ci95)), 'nor for the same entries with mechanical exits')
})

test('the drawdown next to the random baseline is the closed-trade one for both', () => {
  const rep = clone(fixture)
  const s = series(rep, 'SIM:BETA', '1h')
  const o = s.overall.nexus
  const rnd = s.overall.random
  const t = text(renderToStaticMarkup(h(ValidationReportView, { report: rep, open: `${s.symbol}|${s.tf}` })))
  const nexusDd = fmtPct(o.trade_max_dd_pct, 1, false)
  const randomDd = fmtPct(rnd.trade_max_dd_p50, 1, false)
  assert.ok(t.includes(`Caída máx. por operaciones cerradas: NEXUS ${nexusDd} · azar (mediana) ${randomDd}`))
  assert.ok(t.includes('Las dos se valoran vela a vela'), 'the account and buy and hold drawdowns are labelled per bar')
  s.overall.nexus.trades = 0 // without trades there is no closed-trade drawdown to compare
  const none = text(renderToStaticMarkup(h(ValidationReportView, { report: rep, open: `${s.symbol}|${s.tf}` })))
  assert.ok(!none.includes('Caída máx. por operaciones cerradas'))
})
