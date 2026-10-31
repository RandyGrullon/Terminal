// ValidationCard, second verifier: the report header never claims more walk-forward periods than a short series got,
// and missing bars are only shown for 24/7 markets (a stock's nights and weekends are not "huecos").
import './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ValidationReportView, foldsText } from '../src/components/brain/ValidationCard'
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

test('periods in the header: "hasta N" when a short series was split into fewer', () => {
  const full = [{ folds: [1, 2, 3, 4, 5] }, { folds: [1, 2, 3, 4, 5] }] as any
  assert.equal(foldsText(5, full), '5 periodos')
  assert.equal(foldsText(5, [...full, { folds: [1, 2, 3, 4] }] as any), 'hasta 5 periodos')
  assert.equal(foldsText(5, []), '5 periodos')
  assert.equal(foldsText(undefined, full), '— periodos')
  const rep = clone(fixture)
  const n = rep.config.folds
  assert.ok(text(renderToStaticMarkup(h(ValidationReportView, { report: rep }))).includes(` ${n} periodos`))
  rep.series[0].folds = rep.series[0].folds.slice(0, n - 1)
  assert.ok(text(renderToStaticMarkup(h(ValidationReportView, { report: rep }))).includes(`hasta ${n} periodos`))
})

test('missing bars are shown for 24/7 markets only', () => {
  const rep = clone(fixture)
  const s = rep.series[0]
  const key = `${s.symbol}|${s.tf}`
  s.gaps = 3
  assert.ok(text(renderToStaticMarkup(h(ValidationReportView, { report: rep, open: key }))).includes('3 huecos'))
  s.gaps = null // a market with sessions: nights and weekends are not missing data
  const t = text(renderToStaticMarkup(h(ValidationReportView, { report: rep, open: key })))
  assert.ok(!t.includes('hueco') && t.includes('al final (excluidas)'))
})
