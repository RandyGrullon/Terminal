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

export const API_BASE = normalizeApiBase(RAW_API_BASE)
export const IS_REMOTE_API = API_BASE !== ''

export function apiUrl(path: string): string {
  return API_BASE ? `${API_BASE}${path}` : path
}

export function wsUrl(): string {
  if (/^https?:\/\//i.test(API_BASE)) return `${API_BASE.replace(/^http/i, 'ws')}/ws`
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}${API_BASE}/ws` // same origin (API_BASE empty or a path)
}
