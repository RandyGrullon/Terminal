// ValidationCard (Aprendizaje): the honest reading of a walk-forward validation report. Rendered with
// react-dom/server from a real report produced by the backend (tests/imp-validation.fixture.json: synthetic SIM:*
// series, small samples, one series that could not be validated).
import './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ValidationCard, {
  ValidationProgress,
  ValidationReportView,
  VerdictBanner,
  expectancyTone,
  fmtCi,
  progressText,
  verdictDetail,
  verdictTone,
  winsOf,
} from '../src/components/brain/ValidationCard'
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

test('helpers: verdict tones, intervals, x de n and progress', () => {
  assert.equal(verdictTone('edge'), 'pos')
  assert.equal(verdictTone('negative'), 'neg')
  assert.equal(verdictTone('insufficient'), 'muted')
  assert.equal(verdictTone('no_edge'), 'warn')
  assert.equal(verdictTone(undefined), 'warn')
  assert.equal(fmtCi([-0.12, 0.08]), '-0,12R a +0,08R')
  assert.equal(fmtCi(null), '—')
  assert.equal(expectancyTone(80, [0.05, 0.3]), 'pos')
  assert.equal(expectancyTone(80, [-0.3, -0.05]), 'neg')
  assert.equal(expectancyTone(80, [-0.1, 0.2]), 'muted') // the interval includes 0: no colour
  assert.equal(expectancyTone(20, [0.05, 0.3]), 'muted') // below 30 trades: no colour
  assert.equal(winsOf(12, 40), '12 de 40 ganadas')
  assert.equal(winsOf(1, 1), '1 de 1 ganada · muestra pequeña')
  assert.equal(winsOf(5, 20), '5 de 20 ganadas · muestra pequeña')
  assert.equal(winsOf(0, 0), 'sin operaciones')
  const v = { code: 'no_edge' as const, label: 'Sin evidencia de ventaja', text: 'Sin evidencia de ventaja: el intervalo incluye el 0.' }
  assert.equal(verdictDetail(v), 'El intervalo incluye el 0.')
  assert.equal(progressText({ done: 3, total: 10, message: 'Validando X' } as any), '3 de 10 series · Validando X')
  assert.equal(progressText(null), '')
})

test('report view: honest headline, x de n, groups, unavailable series, method and disclaimer', () => {
  const html = renderToStaticMarkup(h(ValidationReportView, { report: fixture as any }))
  const t = text(html)
  assert.ok(t.includes('Sin evidencia de ventaja'))
  assert.ok(html.includes('val-verdict val-warn'), 'a no-edge headline is amber, never green')
  for (const s of fixture.series) assert.ok(t.includes(s.symbol), s.symbol)
  assert.ok(t.includes('muestra pequeña'))
  assert.ok(t.includes('Temporalidad 1h') && t.includes('Todas las series'))
  assert.ok(t.includes('1 serie sin validar') && t.includes('SIM:BETA 4h'))
  assert.ok(t.includes('Cómo se mide') && t.includes('fuera de muestra'))
  assert.ok(t.includes('no garantiza resultados futuros'))
  assert.ok(!html.includes('badge-pos') && !/class="num pos"/.test(html), 'nothing is green without evidence')
  assert.ok(!/\bprueba\b/i.test(t))
  const top = fixture.groups[fixture.groups.length - 1]
  assert.equal(typeof top.mechanical_excess_r, 'number')
  assert.ok(t.includes(`${top.label}: entradas frente a salidas`) && t.includes('efecto de las salidas del modelo'))
})

test('the entries-vs-exits split is hidden when an older server does not send it', () => {
  const rep = clone(fixture)
  for (const g of rep.groups) {
    delete g.mechanical_excess_r
    if (g.ungated) delete g.ungated.mechanical_excess_r
  }
  const t = text(renderToStaticMarkup(h(ValidationReportView, { report: rep })))
  assert.ok(!t.includes('entradas frente a salidas'))
})

test('an edge headline is green and says it is not a guarantee; a loss after costs is red', () => {
  const rep = clone(fixture)
  rep.summary = {
    scope: 'all',
    code: 'edge',
    label: 'Indicios de ventaja',
    text: 'Indicios de ventaja: expectativa conjunta +0,20R por operación. No es una garantía: es una medición sobre datos pasados y el mercado cambia.',
  }
  const s0 = rep.series[0]
  s0.verdict = { code: 'negative', label: 'Sin evidencia de ventaja', text: 'Sin evidencia de ventaja: pierde después de comisiones.' }
  s0.overall.nexus.trades = 80
  s0.overall.nexus.ci95 = [-0.3, -0.05]
  const html = renderToStaticMarkup(h(ValidationReportView, { report: rep }))
  assert.ok(html.includes('val-verdict val-pos'))
  assert.ok(text(html).includes('No es una garantía'))
  assert.ok(html.includes('badge-neg') && html.includes('class="num neg"'))
  const banner = renderToStaticMarkup(h(VerdictBanner, { verdict: rep.summary }))
  assert.ok(text(banner).includes('Indicios de ventaja') && text(banner).includes('Expectativa conjunta'))
})

test('an expanded series shows its periods, the same-exits comparison, the gate effect and the data', () => {
  const s = fixture.series[0]
  const html = renderToStaticMarkup(h(ValidationReportView, { report: fixture as any, open: `${s.symbol}|${s.tf}` }))
  const t = text(html)
  assert.ok(html.includes('aria-expanded="true"'))
  assert.ok(t.includes('P1') && t.includes(`P${s.folds.length}`))
  assert.ok(t.includes('Mismas salidas') && t.includes('percentil de NEXUS'))
  assert.ok(t.includes(s.gate_effect.text))
  assert.ok(t.includes('Comprar y mantener arriesga todo el capital'))
  assert.ok(t.includes('al final (excluidas)'))
  assert.ok(t.includes('NEXUS sin filtro frente a su propio azar') && t.includes(s.verdict_ungated.label))
  if (typeof s.overall.random_ungated?.percentile === 'number') assert.ok(t.includes('· percentil '))
  const closed = renderToStaticMarkup(h(ValidationReportView, { report: fixture as any }))
  assert.ok(!closed.includes('aria-expanded="true"') && closed.includes('aria-expanded="false"'))
})

test('progress line and the first render of the card', () => {
  const job = {
    id: 1,
    state: 'running',
    request: { symbols: ['BINANCE:BTCUSDT'], tfs: ['1h', '4h'], bars: 3000 },
    total: 2,
    done: 1,
    current: 'BINANCE:BTCUSDT 4h',
    message: 'Validando BINANCE:BTCUSDT 4h (3000 velas)…',
    started_at: 0,
    finished_at: null,
    errors: [],
    error: null,
    summary: null,
  }
  const t = text(renderToStaticMarkup(h(ValidationProgress, { job: job as any })))
  assert.ok(t.includes('1 de 2 series') && t.includes('Validando BINANCE:BTCUSDT 4h'))
  assert.ok(t.includes('1 símbolo ×') && !t.includes('1 símbolos'))
  const card = renderToStaticMarkup(h(ValidationCard))
  assert.ok(card.includes('val-card'))
  assert.ok(text(card).includes('Validación walk-forward') && text(card).includes('Cargando la última validación'))
  assert.ok(text(card).includes('Validar ahora'))
})

test('a partial report (older or newer server) does not break the view', () => {
  const rep = clone(fixture)
  rep.groups = undefined
  rep.errors = null
  rep.method = undefined
  rep.series = [...rep.series, { symbol: 'X:SIN-DATOS' }]
  const html = renderToStaticMarkup(h(ValidationReportView, { report: rep }))
  assert.ok(text(html).includes(fixture.series[0].symbol))
  assert.ok(!text(html).includes('X:SIN-DATOS'))
})
