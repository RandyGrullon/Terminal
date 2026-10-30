// Supabase Auth session for cloud mode (email + password) using the official GoTrue REST API.
// In local mode the backend reports auth mode "none" and nothing here is used.
import type { AuthConfig } from '../api/types'
import { apiUrl, IS_REMOTE_API } from '../lib/config'

export interface Session {
  access_token: string
  refresh_token: string
  expires_at: number // unix seconds
  email: string
}

type Listener = (s: Session | null) => void

const STORAGE_KEY = 'nexus.auth.v1'
const listeners = new Set<Listener>()
let config: AuthConfig | null = null
let session: Session | null = readStored()
let refreshTimer: number | null = null
let refreshing: Promise<Session | null> | null = null

function readStored(): Session | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as Session) : null
  } catch {
    return null
  }
}

function store(s: Session | null): void {
  try {
    if (s) localStorage.setItem(STORAGE_KEY, JSON.stringify(s))
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* storage unavailable: session lives in memory only */
  }
}

function setSession(s: Session | null): void {
  session = s
  store(s)
  scheduleRefresh()
  for (const fn of Array.from(listeners)) fn(session)
}

function scheduleRefresh(): void {
  if (refreshTimer) window.clearTimeout(refreshTimer)
  refreshTimer = null
  if (!session) return
  const ms = Math.max(5000, (session.expires_at - 90) * 1000 - Date.now())
  refreshTimer = window.setTimeout(() => void refreshSession(), ms)
}

/** Fetches the backend auth configuration (public endpoint). */
export async function loadAuthConfig(): Promise<AuthConfig> {
  if (config) return config
  try {
    const res = await fetch(apiUrl('/api/auth/config'))
    if (res.status === 404) config = { mode: 'none', supabase_url: null, supabase_anon_key: null }
    else if (!res.ok) throw new Error(`HTTP ${res.status}`)
    else config = (await res.json()) as AuthConfig
  } catch (err) {
    if (IS_REMOTE_API) throw new Error('No se pudo contactar con el servidor NEXUS')
    // Local mode: the backend may still be starting; behave as "no auth" and let pages show their errors.
    config = { mode: 'none', supabase_url: null, supabase_anon_key: null }
  }
  if (config.mode === 'supabase') scheduleRefresh()
  return config
}

export const authEnabled = (): boolean => config?.mode === 'supabase'
export const currentSession = (): Session | null => session

export function getAccessToken(): string | null {
  return authEnabled() ? session?.access_token ?? null : null
}

export function onAuthChange(fn: Listener): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

function authError(data: Record<string, unknown>, status: number): string {
  const code = String(data.error_code ?? data.error ?? '')
  const msg = String(data.msg ?? data.error_description ?? data.message ?? '')
  if (code === 'invalid_credentials' || /invalid login credentials/i.test(msg)) return 'Correo o contraseña incorrectos.'
  if (code === 'email_not_confirmed' || /not confirmed/i.test(msg)) return 'Tu correo aún no está confirmado en Supabase.'
  if (status === 429 || code === 'over_request_rate_limit') return 'Demasiados intentos. Espera un momento.'
  return msg ? `No se pudo iniciar sesión: ${msg}` : `No se pudo iniciar sesión (HTTP ${status}).`
}

async function tokenRequest(grant: 'password' | 'refresh_token', body: Record<string, string>): Promise<Session> {
  if (!config?.supabase_url || !config.supabase_anon_key) throw new Error('El login no está configurado en el servidor.')
  const res = await fetch(`${config.supabase_url}/auth/v1/token?grant_type=${grant}`, {
    method: 'POST',
    headers: { apikey: config.supabase_anon_key, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = (await res.json().catch(() => ({}))) as Record<string, any>
  if (!res.ok) throw new Error(authError(data, res.status))
  const expiresAt = Number(data.expires_at) || Math.floor(Date.now() / 1000) + (Number(data.expires_in) || 3600)
  return {
    access_token: String(data.access_token),
    refresh_token: String(data.refresh_token),
    expires_at: expiresAt,
    email: String(data.user?.email ?? session?.email ?? ''),
  }
}

/** Signs in and checks that this account may use this NEXUS instance (NEXUS_ALLOWED_EMAILS). */
export async function signIn(email: string, password: string): Promise<Session> {
  const s = await tokenRequest('password', { email: email.trim(), password })
  const res = await fetch(apiUrl('/api/settings'), { headers: { Authorization: `Bearer ${s.access_token}` } }).catch(() => null)
  if (res && res.status === 401) throw new Error('Tu cuenta no tiene acceso a esta instancia de NEXUS.')
  setSession(s)
  return s
}

/** Refreshes the access token (deduplicated). Signs out when the refresh token is no longer valid. */
export function refreshSession(): Promise<Session | null> {
  if (!session) return Promise.resolve(null)
  if (!refreshing) {
    const refreshToken = session.refresh_token
    refreshing = tokenRequest('refresh_token', { refresh_token: refreshToken })
      .then((s) => {
        setSession(s)
        return s
      })
      .catch(() => {
        setSession(null)
        return null
      })
      .finally(() => {
        refreshing = null
      })
  }
  return refreshing
}

/** Refresh shortly before expiry (call before API requests). */
export async function ensureFreshSession(): Promise<void> {
  if (authEnabled() && session && session.expires_at - 30 < Date.now() / 1000) await refreshSession()
}

export async function signOut(): Promise<void> {
  const s = session
  setSession(null)
  if (s && config?.supabase_url && config.supabase_anon_key) {
    await fetch(`${config.supabase_url}/auth/v1/logout`, {
      method: 'POST',
      headers: { apikey: config.supabase_anon_key, Authorization: `Bearer ${s.access_token}` },
    }).catch(() => undefined)
  }
}

// Keep tabs in sync (login/logout in one tab applies to all).
window.addEventListener('storage', (e) => {
  if (e.key !== STORAGE_KEY) return
  session = readStored()
  scheduleRefresh()
  for (const fn of Array.from(listeners)) fn(session)
})
