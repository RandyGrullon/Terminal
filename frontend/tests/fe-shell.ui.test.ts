// Regression (a11y-labels-modal):
// - <Field> labels its NumberInput / Select / TextInput / Toggle (label for = control id, hint = aria-describedby), and
//   SettingField does the same (a group label for TP1-TP3, markets and fees by asset class);
// - Modal is named by its title, moves focus inside, keeps Tab / Shift+Tab inside, closes on Escape only the
//   top-most dialog and gives focus back to the element that opened it.
// Also covers the settings editor for risk.fee_pct_by_class (validation and change detection).
import { doc } from './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import * as ui from '../src/components/ui'
import SettingField from '../src/components/settings/SettingField'
import { FIELD_BY_KEY } from '../src/components/settings/schema'
import { sameValue, validateField } from '../src/components/settings/validate'

const { Field, Modal, NumberInput, Select, TextInput, Toggle } = ui
const noop = () => undefined

/** Attribute value of the first tag matching `tag` in an HTML string. */
function attr(html: string, tag: RegExp, name: string): string | null {
  const m = html.match(tag)
  if (!m) return null
  const a = m[0].match(new RegExp(`\\s${name}="([^"]*)"`))
  return a ? a[1] : null
}

test('Field: the label points at its control (number, select, text, toggle) and the hint describes it', () => {
  const cases = [
    { control: h(NumberInput, { value: 1, onChange: noop }), tag: /<input[^>]*type="number"[^>]*>/ },
    { control: h(Select, { value: 'a', onChange: noop, options: ['a', 'b'] }), tag: /<select[^>]*>/ },
    { control: h(TextInput, { value: '', onChange: noop }), tag: /<input[^>]*type="text"[^>]*>/ },
    { control: h(Toggle, { checked: true, onChange: noop, label: 'Profundo' }), tag: /<input[^>]*type="checkbox"[^>]*>/ },
  ]
  for (const c of cases) {
    const html = renderToStaticMarkup(h(Field, { label: 'Riesgo %', hint: 'Entre 0,05 y 5' }, c.control))
    const forId = attr(html, /<label[^>]*>/, 'for')
    assert.ok(forId, `label without for: ${html}`)
    assert.equal(attr(html, c.tag, 'id'), forId, html)
    const hintId = attr(html, /<span[^>]*class="tiny muted"[^>]*>/, 'id')
    assert.ok(hintId)
    assert.equal(attr(html, c.tag, 'aria-describedby'), hintId)
  }
})

test('Field: two fields get different ids; an explicit control id / htmlFor wins', () => {
  const html = renderToStaticMarkup(
    h('div', null, h(Field, { label: 'SL' }, h(NumberInput, { value: 1, onChange: noop })), h(Field, { label: 'TP' }, h(NumberInput, { value: 2, onChange: noop }))),
  )
  const ids = [...html.matchAll(/<input[^>]*\sid="([^"]+)"/g)].map((m) => m[1])
  assert.equal(ids.length, 2)
  assert.notEqual(ids[0], ids[1])
  const own = renderToStaticMarkup(h(Field, { label: 'Símbolo', htmlFor: 'sym' }, h('input', { id: 'sym' })))
  assert.equal(attr(own, /<label[^>]*>/, 'for'), 'sym')
  const named = renderToStaticMarkup(h(NumberInput, { value: 1, onChange: noop, 'aria-label': 'Riesgo' }))
  assert.equal(attr(named, /<input[^>]*>/, 'aria-label'), 'Riesgo')
})

test('Modal is named by its title and focusable as a whole', () => {
  const html = renderToStaticMarkup(h(Modal, { open: true, onClose: noop, title: 'Recargar ajustes', footer: h('button', null, 'Cancelar') }, 'Texto'))
  const labelledBy = attr(html, /<div[^>]*role="dialog"[^>]*>/, 'aria-labelledby')
  assert.ok(labelledBy, html)
  assert.equal(attr(html, /<h3[^>]*>/, 'id'), labelledBy)
  assert.equal(attr(html, /<div[^>]*role="dialog"[^>]*>/, 'tabindex'), '-1')
})

// ----------------------------------------------------------------------------- dialog focus (fake DOM)

class El {
  children: El[] = []
  parent: El | null = null
  tabIndex: number
  constructor(
    public name: string,
    public cls = '',
    public focusable = false,
  ) {
    this.tabIndex = focusable ? 0 : -1
  }
  add(...kids: El[]): El {
    for (const k of kids) {
      k.parent = this
      this.children.push(k)
    }
    return this
  }
  get isConnected(): boolean {
    return true
  }
  contains(n: unknown): boolean {
    for (let c = n as El | null; c; c = c.parent) if (c === this) return true
    return false
  }
  descendants(): El[] {
    return this.children.flatMap((c) => [c, ...c.descendants()])
  }
  querySelector(sel: string): El | null {
    return this.descendants().find((d) => sel === `.${d.cls}`) ?? null
  }
  querySelectorAll(): El[] {
    return this.descendants().filter((d) => d.focusable)
  }
  getClientRects(): unknown[] {
    return [{}]
  }
  focus(): void {
    doc.activeElement = this
  }
}
;(globalThis as any).HTMLElement = El
const body = new El('body')
doc.body = body

function dialog(name: string, bodyItems: El[], footerItems: El[] = []): El {
  const d = new El(name)
  d.add(new El(`${name}-header`, 'modal-header').add(new El(`${name}-close`, '', true)))
  d.add(new El(`${name}-body`, 'modal-body').add(...bodyItems))
  if (footerItems.length) d.add(new El(`${name}-footer`, 'modal-footer').add(...footerItems))
  body.add(d)
  return d
}

const key = (k: string, shift = false) => {
  const ev = { key: k, shiftKey: shift, defaultPrevented: false, preventDefault() {
    ev.defaultPrevented = true
  } }
  doc.dispatch('keydown', ev)
  return ev
}

test('dialog focus: inside on open, Tab kept inside, Escape only for the top-most, focus back on close', () => {
  const trap = (ui as any).__trapDialog as (d: El, onEscape: () => void) => () => void
  assert.equal(typeof trap, 'function')
  const opener = new El('order-button', '', true)
  body.add(opener)
  opener.focus()

  const risk = new El('risk', '', true)
  const sl = new El('sl', '', true)
  const cancel = new El('cancel', '', true)
  const a = dialog('a', [risk, sl], [cancel])
  let closedA = 0
  const releaseA = trap(a, () => closedA++)
  assert.equal(doc.activeElement, risk, 'first control of the content')

  cancel.focus()
  const tab = key('Tab')
  assert.ok(tab.defaultPrevented)
  assert.equal(doc.activeElement.name, 'a-close', 'Tab on the last element wraps to the first (close button)')
  key('Tab', true)
  assert.equal(doc.activeElement, cancel, 'Shift+Tab on the first element wraps to the last')
  opener.focus() // focus escaped somehow
  key('Tab')
  assert.equal(doc.activeElement.name, 'a-close', 'Tab brings it back inside')

  // a confirmation opened on top: no field in its content, so its first footer button (Cancelar) gets the focus
  const keep = new El('keep', '', true)
  const b = dialog('b', [], [keep, new El('discard', '', true)])
  let closedB = 0
  const releaseB = trap(b, () => closedB++)
  assert.equal(doc.activeElement, keep)
  key('Escape')
  assert.deepEqual([closedA, closedB], [0, 1], 'Escape closes only the top-most dialog')
  releaseB()
  assert.equal(doc.activeElement.name, 'a-close', 'focus back where it was in the first dialog')
  key('Escape')
  assert.deepEqual([closedA, closedB], [1, 1])

  const handled = { key: 'Escape', shiftKey: false, defaultPrevented: true, preventDefault: noop }
  doc.dispatch('keydown', handled) // e.g. a search box inside already closed its list
  assert.equal(closedA, 1)

  doc.activeElement = body // the dialog DOM is gone when React runs the cleanup
  releaseA()
  assert.equal(doc.activeElement, opener, 'focus returns to the element that opened the dialog')
  assert.equal(doc.count('keydown'), 0, 'listeners removed')

  const empty = dialog('c', [])
  const releaseC = trap(empty, noop)
  assert.equal(doc.activeElement, empty, 'nothing focusable inside: the dialog itself')
  releaseC()
})

// ----------------------------------------------------------------------------- settings fields

test('SettingField: label for its control, help as description; groups are labelled', () => {
  const props = { dirty: false, error: null, markets: [], onChange: noop, onRevert: noop }
  for (const key of ['risk.fee_pct', 'gate.enabled', 'watch.tf', 'providers.crypto_default', 'providers.polygon_key', 'paper.auto_symbols']) {
    const html = renderToStaticMarkup(h(SettingField, { ...props, def: FIELD_BY_KEY[key], value: undefined }))
    const forId = attr(html, /<label[^>]*class="set-label"[^>]*>/, 'for')
    assert.ok(forId, `${key}: ${html}`)
    const control = html.match(new RegExp(`<(input|select)[^>]*\\sid="${forId.replace(/[:]/g, '\\$&')}"[^>]*>`))
    assert.ok(control, `${key}: no control with id ${forId}`)
    const help = attr(html, /<span[^>]*class="set-help"[^>]*>/, 'id')
    assert.equal(attr(control[0], /<(input|select)[^>]*>/, 'aria-describedby'), help, key)
  }
  for (const key of ['nexus.tp_r', 'scanner.markets', 'risk.fee_pct_by_class']) {
    const value = key === 'nexus.tp_r' ? [1, 2, 3] : key === 'scanner.markets' ? ['crypto'] : { forex: 0.005, stock: 0.02 }
    const html = renderToStaticMarkup(h(SettingField, { ...props, def: FIELD_BY_KEY[key], value }))
    const labelId = attr(html, /<span[^>]*class="set-label"[^>]*>/, 'id')
    assert.ok(labelId, `${key}: ${html}`)
    assert.equal(attr(html, /<div[^>]*role="group"[^>]*>/, 'aria-labelledby'), labelId, key)
  }
})

test('fees by asset class: validation and change detection', () => {
  const def = FIELD_BY_KEY['risk.fee_pct_by_class']
  assert.equal(def.kind, 'fee_classes')
  assert.equal(validateField(def, { forex: 0.005, stock: 0.02 }), null)
  assert.match(validateField(def, { forex: null }) ?? '', /^Forex: obligatorio/)
  assert.match(validateField(def, { stock: 5 }) ?? '', /^Acciones: debe estar entre 0 y 2/)
  assert.match(validateField(def, { index: Number.NaN }) ?? '', /^Índices: número no válido/)
  assert.equal(validateField(def, [1]), 'Valor no válido')
  assert.ok(sameValue({ forex: 0.005, stock: 0.02 }, { stock: 0.02, forex: 0.005 }), 'key order does not matter')
  assert.ok(!sameValue({ forex: 0.005 }, { forex: 0.01 }))
  const html = renderToStaticMarkup(
    h(SettingField, { def, value: { forex: 0.005, stock: 0.02 }, dirty: false, error: null, markets: [], onChange: noop, onRevert: noop }),
  )
  assert.match(html, /Forex/)
  assert.match(html, /Acciones/)
})
