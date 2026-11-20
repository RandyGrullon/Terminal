// Where the backend lives. Local mode: same origin (Vite proxy in dev, FastAPI serves the UI in prod).
// Cloud mode (Vercel): VITE_API_BASE=https://<backend>.up.railway.app
const RAW_API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

/** Normalizes VITE_API_BASE: trims it, drops trailing slashes and adds the scheme when it is missing. Railway shows
 *  the domain without https://, and the browser would otherwise treat "x.up.railway.app" as a path of the Vercel
 *  site (every call answering 404). localhost / 127.0.0.1 default to http://, any other host to https://. A path
 *  ("/nexus", e.g. behind a same-origin proxy) is kept as is. */
export function normalizeApiBase(raw: string | null | undefined): string {
  let base = String(raw ?? '')
    .trim()
    .replace(/\/+$/, '')
  if (!base) return ''
  if (base.startsWith('//')) return `https:${base}`
  if (base.startsWith('/')) return base
  base = base.replace(/^ws(s?):\/\//i, 'http$1://') // a WebSocket URL given by mistake
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(base)) return base
  const local = /^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i.test(base)
  return `${local ? 'http' : 'https'}://${base}`
}

// Fixed link (Vercel) with a backend whose address changes (the phone's Cloudflare quick tunnel): the backend
// publishes its current address in Supabase (public.nexus_endpoint, written by tools/publish_endpoint.py) and the page
// reads it there at start and again when the backend stops answering. VITE_NEXUS_DISCOVERY_URL = the Supabase project
// URL, VITE_NEXUS_DISCOVERY_KEY = its publishable key (public by design: the backend already serves it at
// /api/auth/config). The last address found is remembered, so the page opens at once on the next visit.
const ENV = import.meta.env as Partial<ImportMetaEnv> | undefined // undefined in the test bundles
const DISCOVERY_URL = String(ENV?.VITE_NEXUS_DISCOVERY_URL ?? '').trim().replace(/\/+$/, '')
const DISCOVERY_KEY = String(ENV?.VITE_NEXUS_DISCOVERY_KEY ?? '').trim()
export const DISCOVERY_ENABLED = DISCOVERY_URL !== '' && DISCOVERY_KEY !== '' && RAW_API_BASE.trim() === ''
const CACHE_KEY = 'nexus.api_base'
const REDISCOVER_MIN_MS = 8_000

function cachedBase(): string {
  try {
    return normalizeApiBase(localStorage.getItem(CACHE_KEY))
  } catch {
    return ''
  }
}

export let API_BASE = DISCOVERY_ENABLED ? cachedBase() : normalizeApiBase(RAW_API_BASE)
export let IS_REMOTE_API = API_BASE !== '' || DISCOVERY_ENABLED

type BaseListener = (base: string) => void
const baseListeners = new Set<BaseListener>()
/** Called when discovery finds a new backend address (the WebSocket reconnects to it). */
export function onApiBaseChange(fn: BaseListener): () => void {
  baseListeners.add(fn)
  return () => baseListeners.delete(fn)
}

let inflight: Promise<boolean> | null = null
let lastTry = 0

/** Reads the backend's current address from Supabase. True when it changed. Never throws. */
export function discoverApiBase(force = false): Promise<boolean> {
  if (!DISCOVERY_ENABLED) return Promise.resolve(false)
  if (inflight) return inflight
  if (!force && Date.now() - lastTry < REDISCOVER_MIN_MS) return Promise.resolve(false)
  lastTry = Date.now()
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), 6_000)
  inflight = fetch(`${DISCOVERY_URL}/rest/v1/nexus_endpoint?id=eq.1&select=url`, {
    headers: { apikey: DISCOVERY_KEY },
    signal: ctrl.signal,
    cache: 'no-store',
  })
    .then((r) => (r.ok ? r.json() : null))
    .then((rows: unknown) => {
      const raw = Array.isArray(rows) && rows[0] && typeof rows[0].url === 'string' ? rows[0].url : ''
      const base = normalizeApiBase(raw)
      if (!/^https:\/\//i.test(base) || base === API_BASE) return false
      API_BASE = base
      IS_REMOTE_API = true
      try {
        localStorage.setItem(CACHE_KEY, base)
      } catch {
        /* private mode: discovery runs again next time */
      }
      baseListeners.forEach((fn) => fn(base))
      return true
    })
    .catch(() => false)
    .finally(() => {
      window.clearTimeout(timer)
      inflight = null
    })
  return inflight
}

export function apiUrl(path: string): string {
  return API_BASE ? `${API_BASE}${path}` : path
}

export function wsUrl(): string {
  if (/^https?:\/\//i.test(API_BASE)) return `${API_BASE.replace(/^http/i, 'ws')}/ws`
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}${API_BASE}/ws` // same origin (API_BASE empty or a path)
}
