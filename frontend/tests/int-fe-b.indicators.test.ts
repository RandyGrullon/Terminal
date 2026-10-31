// Regression (fe-shell cross_area, a11y): the numeric parameters in the "Indicadores" dialog rendered a bare <input>
// inside <Field>, so the parameter's label pointed at nothing (screen readers read an unnamed number box and
// clicking the label did not focus it). NumberParam now takes the id of its Field.
// NumberParam is internal: tests/int-fe-b.run.mjs exports it to the tests only, as __NumberParam.
import './int-fe-b.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement as h, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { IndicatorParam } from '../src/api/types'
import * as dialog from '../src/components/chart/IndicatorsDialog'
import { Field } from '../src/components/ui'

type NumberParamProps = { param: IndicatorParam; value: number; onCommit: (v: number) => void }
const NumberParam = (dialog as unknown as { __NumberParam: ComponentType<NumberParamProps> }).__NumberParam
const noop = () => undefined

const LENGTH: IndicatorParam = { name: 'length', label: 'Periodo', type: 'int', default: 14, min: 1, max: 500 }
const MULT: IndicatorParam = { name: 'mult', label: 'Multiplicador', type: 'float', default: 2, min: 0.1, max: 10, step: 0.1 }

function attr(html: string, tag: RegExp, name: string): string | null {
  const m = html.match(tag)
  if (!m) return null
  const a = m[0].match(new RegExp(`\\s${name}="([^"]*)"`))
  return a ? a[1] : null
}

test('the Field label of a numeric indicator parameter points at its input', () => {
  assert.equal(typeof NumberParam, 'function')
  const html = renderToStaticMarkup(h(Field, { label: LENGTH.label }, h(NumberParam, { param: LENGTH, value: 14, onCommit: noop })))
  const forId = attr(html, /<label[^>]*>/, 'for')
  assert.ok(forId, html)
  assert.equal(attr(html, /<input[^>]*type="number"[^>]*>/, 'id'), forId, html)
})

test('two parameters get two different ids; outside a Field the input has no id', () => {
  const html = renderToStaticMarkup(
    h(
      'div',
      null,
      h(Field, { label: LENGTH.label }, h(NumberParam, { param: LENGTH, value: 14, onCommit: noop })),
      h(Field, { label: MULT.label }, h(NumberParam, { param: MULT, value: 2, onCommit: noop })),
    ),
  )
  const fors = [...html.matchAll(/<label[^>]*\sfor="([^"]+)"/g)].map((m) => m[1])
  const ids = [...html.matchAll(/<input[^>]*\sid="([^"]+)"/g)].map((m) => m[1])
  assert.equal(ids.length, 2)
  assert.notEqual(ids[0], ids[1])
  assert.deepEqual(ids, fors)
  const bare = renderToStaticMarkup(h(NumberParam, { param: LENGTH, value: 14, onCommit: noop }))
  assert.equal(attr(bare, /<input[^>]*>/, 'id'), null)
})
