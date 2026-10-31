// The user's indicator list as saved in the settings key "ui.chart_indicators": parsing, and reading it safely.
// The list is only overwritten after it was read successfully: a failed read is retried (backoff, socket reconnect,
// browser back online, "Reintentar") and never treated as an empty list, because saving an edit made on top of an
// empty list would replace every saved indicator on the server. No React here (indicatorsModel.ts uses it).
import type { ParamMap } from './chartUtils'

/** Generous cap: every catalog indicator can be added (several times), but the chart must stay usable. */
export const MAX_INDICATORS = 40

export interface ActiveIndicator {
  uid: string
  id: string
  params: ParamMap
}

export function newUid(): string {
  return `i${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`
}

/** Stored value -> valid list (unique uids, scalar params only, at most MAX_INDICATORS). */
export function parseStored(value: unknown): ActiveIndicator[] {
  if (!Array.isArray(value)) return []
  const out: ActiveIndicator[] = []
  const seen = new Set<string>()
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') continue
    const o = raw as Record<string, unknown>
    if (typeof o.id !== 'string' || !o.id) continue
    let uid = typeof o.uid === 'string' && o.uid ? o.uid : newUid()
    if (seen.has(uid)) uid = newUid()
    seen.add(uid)
    const params: ParamMap = {}
    if (o.params && typeof o.params === 'object') {
      for (const [k, v] of Object.entries(o.params as Record<string, unknown>)) {
        if (typeof v === 'number' || typeof v === 'boolean' || typeof v === 'string') params[k] = v
      }
    }
    out.push({ uid, id: o.id, params })
    if (out.length >= MAX_INDICATORS) break
  }
  return out
}

/** Wait before retrying a failed read of the saved list: 2 s, 4 s, 8 s ... capped at 60 s. */
export function listRetryDelay(attempt: number): number {
  return Math.min(60_000, 2000 * 2 ** Math.min(Math.max(0, attempt), 10))
}

/** Saved list + what the user did before it could be read (`dirty`): saved items first, local ones kept. */
export function mergeStoredList(stored: ActiveIndicator[], current: ActiveIndicator[], dirty: boolean): ActiveIndicator[] {
  if (!dirty) return stored
  const have = new Set(current.map((c) => c.uid))
  return [...stored.filter((s) => !have.has(s.uid)), ...current].slice(0, MAX_INDICATORS)
}

export interface ListLoader {
  /** Retry now (connection back, user click) and restart the backoff; no-op once loaded or stopped. */
  retryNow: () => void
  stop: () => void
}

/**
 * Reads the saved list until it succeeds. `get` resolves with the stored value (undefined when the user never saved
 * one) and rejects on any failure: failures go to `onError` and are retried with backoff; `onLoaded` runs once, only
 * with a real answer. A newer attempt supersedes an older one still in flight.
 */
export function startListLoader(opts: {
  get: () => Promise<unknown>
  onLoaded: (stored: ActiveIndicator[]) => void
  onError: (err: unknown, retryInMs: number) => void
}): ListLoader {
  let stopped = false
  let done = false
  let attempt = 0
  let run = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  const clearTimer = () => {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }
  const load = () => {
    if (stopped || done) return
    clearTimer()
    const id = ++run
    let request: Promise<unknown>
    try {
      request = opts.get()
    } catch (err) {
      request = Promise.reject(err)
    }
    request.then(
      (value) => {
        if (stopped || done || id !== run) return
        done = true
        opts.onLoaded(parseStored(value))
      },
      (err: unknown) => {
        if (stopped || done || id !== run) return
        const wait = listRetryDelay(attempt++)
        timer = setTimeout(load, wait)
        opts.onError(err, wait)
      },
    )
  }
  load()
  return {
    retryNow: () => {
      if (stopped || done) return
      attempt = 0
      load()
    },
    stop: () => {
      stopped = true
      clearTimer()
    },
  }
}
