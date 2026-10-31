// Market conditions ("semáforo de mercado") for the header pill (global scope: REST + live WS pushes) and the
// chart badge (symbol scope: REST only). Polls every 60 s as a fallback, ticks a 1 s clock for countdowns and
// never throws: errors keep the last good snapshot (or leave `conditions` null when there never was one).
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { MarketConditions } from '../../api/types'
import { socket } from '../../api/ws'
import { normalizeConditions } from './macroModel'

const POLL_MS = 60_000
/** /api/macro not deployed (404): poll it less often. */
const MISSING_BACKOFF_MS = 5 * 60_000
/** Without a fresh answer for this long the snapshot is shown as outdated. */
const CLIENT_STALE_MS = 3 * 60_000

export interface MacroState {
  /** Latest conditions of the current scope, normalized (`blackout.until` and `updated_at` in unix ms); null until
   *  the first answer for that scope. */
  conditions: MarketConditions | null
  /** The first request is still running. */
  loading: boolean
  /** The last request failed (the previous snapshot, if any, is kept). */
  failed: boolean
  /** The server flags the data as stale, or nothing fresh arrived for 3 minutes. */
  stale: boolean
  /** Unix ms of the last good update (REST or WS). */
  lastOkAt: number | null
  /** Clock (unix ms) refreshed every second while there are conditions: use it for countdowns. */
  now: number
  /** Fetch again now (also bypasses the 404 back-off). */
  refresh: () => void
}

export function useMacro(symbol?: string): MacroState {
  const scope = (symbol ?? '').trim().toUpperCase()
  const isGlobal = scope === ''
  // Conditions are kept with the scope they belong to: right after a symbol change the previous symbol's
  // semáforo is never returned for the new one (callers react to level changes).
  const [held, setHeld] = useState<{ scope: string; conditions: MarketConditions } | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [lastOkAt, setLastOkAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const seq = useRef(0)
  const lastOk = useRef(0)
  const retryAfter = useRef(0)

  const accept = useCallback((c: MarketConditions, forScope: string) => {
    const t = Date.now()
    lastOk.current = t
    retryAfter.current = 0
    setHeld({ scope: forScope, conditions: c })
    setFailed(false)
    setLoading(false)
    setLastOkAt(t)
    setNow(t)
  }, [])

  const load = useCallback(() => {
    const id = ++seq.current
    api.macro(scope || undefined).then(
      (raw) => {
        if (id !== seq.current) return
        const c = normalizeConditions(raw)
        if (c) {
          accept(c, scope)
          return
        }
        setFailed(true)
        setLoading(false)
      },
      (err: unknown) => {
        if (id !== seq.current) return
        if (err instanceof ApiError && err.status === 404) retryAfter.current = Date.now() + MISSING_BACKOFF_MS
        setFailed(true)
        setLoading(false)
      },
    )
  }, [scope, accept])

  // (Re)load whenever the scope changes, then poll as a fallback for missed pushes.
  useEffect(() => {
    setHeld(null)
    setLoading(true)
    setFailed(false)
    setLastOkAt(null)
    lastOk.current = 0
    retryAfter.current = 0
    load()
    const due = () => {
      const t = Date.now()
      return t >= retryAfter.current && t - lastOk.current >= POLL_MS - 5000
    }
    const poll = window.setInterval(() => {
      if (document.visibilityState !== 'hidden' && due()) load()
    }, POLL_MS)
    const onVisibility = () => {
      if (document.visibilityState === 'visible' && due()) load()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      seq.current++ // ignore answers still in flight
      window.clearInterval(poll)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [load])

  // Global scope: live pushes on the default-subscribed WS topic 'macro', and a resync after reconnecting.
  useEffect(() => {
    if (!isGlobal) return
    const offMsg = socket.on('macro', (msg) => {
      const c = normalizeConditions(msg.conditions)
      if (!c || c.symbol) return
      seq.current++ // a REST answer still in flight would be older than this push
      accept(c, '')
    })
    let prev: string | null = null
    const offStatus = socket.onStatus((s) => {
      if (s === 'open' && prev !== null && prev !== 'open') load()
      prev = s
    })
    return () => {
      offMsg()
      offStatus()
    }
  }, [isGlobal, load, accept])

  const conditions = held && held.scope === scope ? held.conditions : null

  // 1 s clock for countdowns (countdown_s in the payload is only a snapshot).
  const ticking = conditions !== null
  useEffect(() => {
    if (!ticking) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [ticking])

  const stale = conditions !== null && (conditions.stale || (lastOkAt !== null && now - lastOkAt > CLIENT_STALE_MS))
  return { conditions, loading, failed, stale, lastOkAt, now, refresh: load }
}
