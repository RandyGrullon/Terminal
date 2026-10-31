// Regression (a11y-labels-modal, verifier): a dialog whose content takes the focus by itself (autoFocus, e.g. the
// search box of the indicators dialog) must still give the focus back to the element that opened it. React runs
// autoFocus during the commit, before the Modal's effect, so the focused element at that point is inside the dialog;
// Modal records where the focus was while rendering and hands it to the focus trap (`openedFrom`).
// Checked in a real browser too (Chromium, React 18 StrictMode): open with autoFocus -> Escape / footer button ->
// focus back on the opening button.
import { doc } from './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as ui from '../src/components/ui'

/** Minimal element: parent links, focus, removal (isConnected) and the queries the focus trap uses. */
class El {
  children: El[] = []
  parent: El | null = null
  tabIndex: number
  removed = false
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
    for (let c: El | null = this; c; c = c.parent) if (c.removed) return false
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

type Trap = (d: El, onEscape: () => void, openedFrom?: El | null) => () => void
const trap = (ui as any).__trapDialog as Trap

function dialogWithAutoFocus(): { dialog: El; search: El } {
  const search = new El('search', '', true)
  const dialog = new El('dialog')
  dialog.add(new El('header', 'modal-header').add(new El('close', '', true)))
  dialog.add(new El('body-part', 'modal-body').add(search, new El('add', '', true)))
  dialog.add(new El('footer', 'modal-footer').add(new El('done', '', true)))
  body.add(dialog)
  return { dialog, search }
}

test('a dialog that focused its own search box (autoFocus) gives the focus back to its opener on close', () => {
  const opener = new El('indicators-button', '', true)
  body.add(opener)
  opener.focus()
  const { dialog, search } = dialogWithAutoFocus()
  search.focus() // React's autoFocus, before the Modal effect runs
  const release = trap(dialog, () => undefined, opener) // Modal passes where the focus was when it opened
  assert.equal(doc.activeElement, search, 'the focus stays in the search box')
  dialog.removed = true // closed: React removed the dialog
  doc.activeElement = body
  release()
  assert.equal(doc.activeElement, opener, 'focus back on the button that opened the dialog')
})

test('without a known opener, a control inside the dialog is never taken for it', () => {
  const { dialog, search } = dialogWithAutoFocus()
  search.focus()
  const release = trap(dialog, () => undefined)
  dialog.removed = true
  doc.activeElement = body
  release()
  assert.equal(doc.activeElement, body, 'nothing to restore (the search box is gone), and no stale target')
})

test('the focus outside the dialog is still used when no opener is passed', () => {
  const opener = new El('reset-button', '', true)
  body.add(opener)
  opener.focus()
  const { dialog, search } = dialogWithAutoFocus()
  const release = trap(dialog, () => undefined)
  assert.equal(doc.activeElement, search, 'first control of the content')
  dialog.removed = true
  doc.activeElement = body
  release()
  assert.equal(doc.activeElement, opener)
})
