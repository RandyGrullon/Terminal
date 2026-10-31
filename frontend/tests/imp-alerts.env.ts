// Fake browser for the alerts tests: window events + focus, localStorage (can be made to throw), document visibility
// and focus, and a scriptable Notification API. Import it FIRST in a test file: sound.ts reads these globals when it
// is evaluated. No network, no real notifications.
/* eslint-disable @typescript-eslint/no-explicit-any */

type Listener = (ev: any) => void

const listeners = new Map<string, Set<Listener>>()
export const store = new Map<string, string>()
export const storage = { broken: false }
export const doc = { hidden: false, focused: true }
export const focusCalls: number[] = []

const fakeWindow: any = {
  addEventListener(type: string, fn: Listener) {
    if (!listeners.has(type)) listeners.set(type, new Set())
    listeners.get(type)!.add(fn)
  },
  removeEventListener(type: string, fn: Listener) {
    listeners.get(type)?.delete(fn)
  },
  focus() {
    focusCalls.push(Date.now())
  },
  setTimeout: (fn: () => void, ms?: number) => setTimeout(fn, ms) as unknown as number,
  clearTimeout: (id: number) => clearTimeout(id as unknown as NodeJS.Timeout),
  setInterval: (fn: () => void, ms?: number) => setInterval(fn, ms) as unknown as number,
  clearInterval: (id: number) => clearInterval(id as unknown as NodeJS.Timeout),
}

export function dispatchWindow(type: string, ev: Record<string, unknown> = {}): void {
  for (const fn of Array.from(listeners.get(type) ?? [])) fn({ type, ...ev })
}

const fakeStorage = {
  getItem(k: string) {
    if (storage.broken) throw new Error('SecurityError')
    return store.has(k) ? store.get(k)! : null
  },
  setItem(k: string, v: string) {
    if (storage.broken) throw new Error('QuotaExceededError')
    store.set(k, String(v))
  },
  removeItem(k: string) {
    store.delete(k)
  },
}

const fakeDocument = {
  get hidden() {
    return doc.hidden
  },
  get visibilityState() {
    return doc.hidden ? 'hidden' : 'visible'
  },
  hasFocus() {
    return doc.focused && !doc.hidden
  },
}

export class FakeNotification {
  static permission: 'default' | 'granted' | 'denied' = 'default'
  static answer: 'default' | 'granted' | 'denied' = 'granted'
  static requests = 0
  static all: FakeNotification[] = []
  static throwOnCreate = false
  title: string
  options: { body?: string; tag?: string }
  onclick: (() => void) | null = null
  closed = false
  constructor(title: string, options: { body?: string; tag?: string } = {}) {
    if (FakeNotification.throwOnCreate) throw new TypeError('Illegal constructor')
    this.title = title
    this.options = options
    FakeNotification.all.push(this)
  }
  static async requestPermission(): Promise<'default' | 'granted' | 'denied'> {
    FakeNotification.requests++
    FakeNotification.permission = FakeNotification.answer
    return FakeNotification.answer
  }
  close(): void {
    this.closed = true
  }
  static reset(): void {
    FakeNotification.all = []
    FakeNotification.requests = 0
    FakeNotification.throwOnCreate = false
  }
}

const g = globalThis as any
g.window = fakeWindow
g.localStorage = fakeStorage
g.document = fakeDocument
g.Notification = FakeNotification

/** Remove the Notification API (browsers without it). */
export function withoutNotificationApi<T>(fn: () => T): T {
  const saved = g.Notification
  delete g.Notification
  try {
    return fn()
  } finally {
    g.Notification = saved
  }
}
