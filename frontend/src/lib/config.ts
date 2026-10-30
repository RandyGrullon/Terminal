// Where the backend lives. Local mode: same origin (Vite proxy in dev, FastAPI serves the UI in prod).
// Cloud mode (Vercel): VITE_API_BASE=https://<backend>.up.railway.app
const RAW_API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

export const API_BASE = RAW_API_BASE.trim().replace(/\/+$/, '')
export const IS_REMOTE_API = API_BASE !== ''

export function apiUrl(path: string): string {
  return API_BASE ? `${API_BASE}${path}` : path
}

export function wsUrl(): string {
  if (API_BASE) return `${API_BASE.replace(/^http/i, 'ws')}/ws`
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/ws`
}
