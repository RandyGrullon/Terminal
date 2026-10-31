// Fake browser for the fe-shell tests: window / document events, localStorage, location + history, a scripted
// fetch, a controllable WebSocket and manual timers. Import it FIRST in a test file: the modules under test read these
// globals when they are evaluated. No network is ever used.
/* eslint-disable @typescript-eslint/no-explicit-any */

type Listener = (ev: any) => void

export class FakeTarget {
  private listeners = new Map<string, Set<Listener>>()
  addEventListener(type: string, fn: Listener): void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set())
    this.listeners.get(type)!.add(fn)
  }
  removeEventListener(type: string, fn: Listener): void {
    this.listeners.get(type)?.delete(fn)
  }
  dispatch(type: string, ev: Record<string, unknown> = {}): void {
    for (const fn of Array.from(this.listeners.get(type) ?? [])) fn({ type, ...ev })
  }
  count(type: string): number {
    return this.listeners.get(type)?.size ?? 0
  }
}

// ----------------------------------------------------------------------------- timers (manual)

export interface FakeTimer {
  id: number
  fn: () => void
  ms: number
  interval: boolean
  done: boolean
}

export const timers: FakeTimer[] = []
let timerSeq = 0
/** One-shot timers up to this delay run by themselves (on the next macrotask); longer ones wait for runTimers(). */
let autoRunMaxMs = -1

export function autoRunTimersUpTo(ms: number): void {
  autoRunMaxMs = ms
}

function addTimer(fn: () => void, ms: number | undefined, interval: boolean): number {
  const t: FakeTimer = { id: ++timerSeq, fn, ms: ms ?? 0, interval, done: false }
  timers.push(t)
  if (!interval && t.ms <= autoRunMaxMs) {
    setTimeout(() => {
      if (t.done) return
      t.done = true
      t.fn()
    }, 0)
  }
  return t.id
}

function clearTimer(id: number | null | undefined): void {
  const t = timers.find((x) => x.id === id)
  if (t) t.done = true
}

/** Pending one-shot timers (not cleared, not run yet). */
export function pendingTimers(): FakeTimer[] {
  return timers.filter((t) => !t.done && !t.interval)
}

/** Runs the pending one-shot timers that match `pred`; returns how many ran. */
export function runTimers(pred: (t: FakeTimer) => boolean = () => true): number {
  const due = pendingTimers().filter(pred)
  for (const t of due) {
    t.done = true
    t.fn()
  }
  return due.length
}

export function clearAllTimers(): void {
  for (const t of timers) t.done = true
}

/** Lets pending promises (fetch stubs, .then chains) settle. */
export async function flush(rounds = 5): Promise<void> {
  for (let i = 0; i < rounds; i++) await new Promise((r) => setTimeout(r, 0))
}

// ----------------------------------------------------------------------------- window, storage, document, location

export const win = new FakeTarget() as FakeTarget & Record<string, any>
win.setTimeout = (fn: () => void, ms?: number) => addTimer(fn, ms, false)
win.clearTimeout = clearTimer
win.setInterval = (fn: () => void, ms?: number) => addTimer(fn, ms, true)
win.clearInterval = clearTimer

const store = new Map<string, string>()
export const storage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
}

export const doc = new FakeTarget() as FakeTarget & Record<string, any>
doc.visibilityState = 'visible'
doc.activeElement = null
doc.body = null

export const loc = { hash: '', protocol: 'https:', host: 'nexus-terminal.vercel.app', origin: 'https://nexus-terminal.vercel.app' }

export const hist = {
  state: null as unknown,
  pushes: [] as string[],
  replaces: [] as string[],
  pushState(state: unknown, _title: string, url: string) {
    this.state = state
    this.pushes.push(url)
    if (url.startsWith('#')) loc.hash = url
  },
  replaceState(state: unknown, _title: string, url: string) {
    this.state = state
    this.replaces.push(url)
    if (url.startsWith('#')) loc.hash = url
  },
}

// ----------------------------------------------------------------------------- fetch

export interface FetchCall {
  url: string
  method: string
  headers: Record<string, string>
  body: string | undefined
}

export const fetchCalls: FetchCall[] = []
let handler: (call: FetchCall) => Response | Promise<Response> = (call) => {
  throw new Error(`fetch no esperado en el test: ${call.method} ${call.url}`)
}

/** Scripts the network: the handler gets every call (it may throw a TypeError to simulate "no connection"). */
export function onFetch(fn: (call: FetchCall) => Response | Promise<Response>): void {
  handler = fn
}

export const json = (status: number, body: unknown): Response =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

export const SUPABASE = 'https://ref.supabase.co'
export const AUTH_CONFIG = { mode: 'supabase', supabase_url: SUPABASE, supabase_anon_key: 'anon-key' }

/** Calls whose URL contains `part` (e.g. '/auth/v1/token'). */
export const callsTo = (part: string) => fetchCalls.filter((c) => c.url.includes(part))

// ----------------------------------------------------------------------------- WebSocket

export class FakeWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  static all: FakeWebSocket[] = []
  readonly url: string
  readyState = 0
  sent: string[] = []
  onopen: ((ev: any) => void) | null = null
  onmessage: ((ev: any) => void) | null = null
  onclose: ((ev: any) => void) | null = null
  onerror: ((ev: any) => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeWebSocket.all.push(this)
  }
  send(data: string): void {
    this.sent.push(data)
  }
  close(): void {
    this.readyState = 3
  }
  /** Server accepted the handshake. */
  serverOpen(): void {
    this.readyState = 1
    this.onopen?.({})
  }
  serverSend(msg: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }
  serverClose(code: number): void {
    this.readyState = 3
    this.onclose?.({ code })
  }
  static last(): FakeWebSocket {
    return FakeWebSocket.all[FakeWebSocket.all.length - 1]
  }
}

// ----------------------------------------------------------------------------- session helpers

export const STORAGE_KEY = 'nexus.auth.v1'

export interface StoredSession {
  access_token: string
  refresh_token: string
  expires_at: number
  email: string
}

export const nowS = () => Math.floor(Date.now() / 1000)

export function session(n: number | string, expiresInS = 3600): StoredSession {
  return { access_token: `at-${n}`, refresh_token: `rt-${n}`, expires_at: nowS() + expiresInS, email: 'owner@example.org' }
}

/** What another tab does on login / refresh: write the session and fire the storage event here. */
export function loginFromOtherTab(s: StoredSession | null): void {
  if (s) store.set(STORAGE_KEY, JSON.stringify(s))
  else store.delete(STORAGE_KEY)
  win.dispatch('storage', { key: STORAGE_KEY })
}

export const stored = (): StoredSession | null => {
  const raw = store.get(STORAGE_KEY)
  return raw ? (JSON.parse(raw) as StoredSession) : null
}

/** A GoTrue token answer (refresh_token grant or password grant). */
export const tokenAnswer = (n: number | string) =>
  json(200, { access_token: `at-${n}`, refresh_token: `rt-${n}`, expires_in: 3600, user: { email: 'owner@example.org' } })

// ----------------------------------------------------------------------------- install

const g = globalThis as any
g.window = win
g.document = doc
g.localStorage = storage
g.location = loc
g.history = hist
g.WebSocket = FakeWebSocket
g.fetch = async (input: unknown, init: { method?: string; headers?: Record<string, string>; body?: string } = {}) => {
  const call: FetchCall = { url: String(input), method: (init.method ?? 'GET').toUpperCase(), headers: init.headers ?? {}, body: init.body }
  fetchCalls.push(call)
  return handler(call)
}
