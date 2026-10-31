// Supabase Auth session for cloud mode (email + password) using the official GoTrue REST API.
// In local mode the backend reports auth mode "none" and nothing here is used.
//
// Only a definitive rejection of the refresh token ends the session. No connection (or no answer within 15 s), 5xx
// and 429 keep it and retry with backoff (and at once when the browser is back online or the tab becomes visible again), so a laptop waking
// up before its Wi-Fi, or a short Supabase outage, never signs the user out of every tab.
import type { AuthConfig } from '../api/types'
import { apiUrl, IS_REMOTE_API } from '../lib/config'

export interface Session {
  access_token: string
  refresh_token: string
  expires_at: number // unix seconds
  email: string
}

/** A failed call to Supabase Auth. status 0 = Supabase could not be reached or did not answer in time; code = GoTrue
 *  error code. */
export class AuthRequestError extends Error {
  readonly status: number
  readonly code: string
  constructor(message: string, status: number, code = '') {
    super(message)
    this.name = 'AuthRequestError'
    this.status = status
    this.code = code
  }
}

/** GoTrue error codes meaning that the refresh token, or its session, will never work again. */
const DEFINITIVE_CODES = new Set([
  'invalid_grant',
  'refresh_token_not_found',
  'refresh_token_already_used',
  'session_not_found',
  'session_expired',
  'user_not_found',
  'user_banned',
])

/** True when Supabase rejected the refresh token for good (the user has to sign in again). Anything else (no
 *  connection, 5xx, 429, unexpected answers) is transient: the session is kept and the refresh retried. */
export function isDefinitiveRejection(err: unknown): boolean {
  return err instanceof AuthRequestError && [400, 401, 403].includes(err.status) && DEFINITIVE_CODES.has(err.code)
}

type Listener = (s: Session | null) => void

const STORAGE_KEY = 'nexus.auth.v1'
const REFRESH_MARGIN_S = 90 // refresh this long before the access token expires
const RETRY_DELAYS_MS = [5000, 15000, 30000, 60000] // after transient refresh failures (the last one repeats)
const FAILURE_COOLDOWN_MS = 4000 // API calls right after a failed refresh reuse its error instead of retrying at once
const TOKEN_TIMEOUT_MS = 15000 // a Supabase Auth call unanswered this long is aborted: "no connection" (status 0)
const NO_AUTH: AuthConfig = { mode: 'none', supabase_url: null, supabase_anon_key: null }

const listeners = new Set<Listener>()
let config: AuthConfig | null = null
let session: Session | null = readStored()
let refreshTimer: number | null = null
let refreshing: Promise<Session | null> | null = null
let failures = 0 // consecutive transient refresh failures (> 0 = a retry is scheduled)
let lastFailure: { at: number; error: AuthRequestError } | null = null

function readStored(): Session | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const s = raw ? (JSON.parse(raw) as Partial<Session> | null) : null
    if (!s || typeof s.access_token !== 'string' || typeof s.refresh_token !== 'string' || !Number.isFinite(s.expires_at)) return null
    return { access_token: s.access_token, refresh_token: s.refresh_token, expires_at: Number(s.expires_at), email: String(s.email ?? '') }
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

function notify(): void {
  for (const fn of Array.from(listeners)) fn(session)
}

function setSession(s: Session | null): void {
  session = s
  store(s)
  failures = 0
  lastFailure = null
  scheduleRefresh()
  notify()
}

/** A session written by another tab (login, refresh or logout there). */
function applyExternal(s: Session | null): void {
  session = s
  failures = 0
  lastFailure = null
  scheduleRefresh()
  notify()
}

function setTimer(ms: number | null): void {
  if (refreshTimer !== null) window.clearTimeout(refreshTimer)
  refreshTimer = null
  if (ms === null) return
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null
    refreshNow()
  }, ms)
}

function scheduleRefresh(): void {
  setTimer(session ? Math.max(5000, (session.expires_at - REFRESH_MARGIN_S) * 1000 - Date.now()) : null)
}

/** After a transient failure: back off, but try once more shortly before the current access token expires. */
function scheduleRetry(): void {
  if (!session) return
  const backoff = RETRY_DELAYS_MS[Math.min(failures, RETRY_DELAYS_MS.length) - 1]
  const untilExpiry = (session.expires_at - 15) * 1000 - Date.now()
  setTimer(untilExpiry > 1000 ? Math.min(backoff, untilExpiry) : backoff)
}

/** A refresh attempt that skips the failure cooldown (retry timer, back online, tab visible again). */
function refreshNow(): void {
  lastFailure = null
  refreshSession().catch(() => undefined) // a transient failure schedules its own retry
}

/** Retries a failed refresh, or refreshes an expiring token, right away: after sleep the timers may have fired late
 *  or before the network was back. */
function refreshIfDue(): void {
  if (!session || !authEnabled()) return
  if (failures > 0 || session.expires_at - REFRESH_MARGIN_S <= Date.now() / 1000) refreshNow()
}

function isAuthConfig(v: unknown): v is AuthConfig {
  const c = v as Partial<AuthConfig> | null
  return !!c && typeof c === 'object' && (c.mode === 'none' || c.mode === 'supabase')
}

/** Fetches the backend auth configuration (public endpoint). */
export async function loadAuthConfig(): Promise<AuthConfig> {
  if (config) return config
  let res: Response
  try {
    res = await fetch(apiUrl('/api/auth/config'))
  } catch {
    if (IS_REMOTE_API) throw new Error('No se pudo contactar con el servidor NEXUS. Revisa tu conexión o que el backend esté en marcha.')
    // Local mode: the backend may still be starting; behave as "no auth" and let pages show their errors.
    config = NO_AUTH
    return config
  }
  const data: unknown = res.ok ? await res.json().catch(() => null) : null
  if (isAuthConfig(data)) config = data
  else if (!IS_REMOTE_API) config = NO_AUTH // local backend without the auth module or still starting
  else if (res.ok || res.status === 404) {
    // VITE_API_BASE points to something that is not a NEXUS backend (typically the Vercel site itself).
    throw new Error(
      `La dirección configurada en VITE_API_BASE no parece un backend NEXUS: /api/auth/config respondió HTTP ${res.status}` +
        `${res.ok ? ' sin la configuración esperada' : ''}. Revisa la variable en Vercel (https://<tu-backend>.up.railway.app) y vuelve a desplegar.`,
    )
  } else throw new Error(`El servidor NEXUS respondió con un error (HTTP ${res.status}). Reintenta en unos segundos.`)
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

function errorCode(data: Record<string, unknown>): string {
  const code = data.error_code ?? data.error
  return typeof code === 'string' ? code : ''
}

function authError(grant: 'password' | 'refresh_token', data: Record<string, unknown>, status: number): string {
  const code = errorCode(data)
  const msg = String(data.msg ?? data.error_description ?? data.message ?? '')
  if (grant === 'refresh_token') {
    if (status === 429) return 'No se pudo renovar tu sesión: Supabase limita las peticiones ahora mismo. Reintenta en unos segundos.'
    if (status >= 500) return `No se pudo renovar tu sesión: Supabase no responde (HTTP ${status}). Reintenta en unos segundos.`
    return msg ? `No se pudo renovar tu sesión: ${msg}` : `No se pudo renovar tu sesión (HTTP ${status}).`
  }
  if (code === 'invalid_credentials' || /invalid login credentials/i.test(msg)) return 'Correo o contraseña incorrectos.'
  if (code === 'email_not_confirmed' || /not confirmed/i.test(msg)) return 'Tu correo aún no está confirmado en Supabase.'
  if (status === 429 || code === 'over_request_rate_limit') return 'Demasiados intentos. Espera un momento.'
  if (status >= 500) return `El servicio de acceso (Supabase) no responde (HTTP ${status}). Reintenta en unos segundos.`
  return msg ? `No se pudo iniciar sesión: ${msg}` : `No se pudo iniciar sesión (HTTP ${status}).`
}

async function tokenRequest(grant: 'password' | 'refresh_token', body: Record<string, string>): Promise<Session> {
  if (!config?.supabase_url || !config.supabase_anon_key) throw new AuthRequestError('El login no está configurado en el servidor.', 0)
  // A stalled call must not keep the deduplicated refresh pending (ensureFreshSession and the 'online' / visible
  // retries wait on it) until the browser gives up: abort it, answer included, and treat it as no connection.
  const ctrl = new AbortController()
  let timedOut = false
  const timer = window.setTimeout(() => {
    timedOut = true
    ctrl.abort()
  }, TOKEN_TIMEOUT_MS)
  let res: Response
  let data: Record<string, any>
  try {
    res = await fetch(`${config.supabase_url}/auth/v1/token?grant_type=${grant}`, {
      method: 'POST',
      headers: { apikey: config.supabase_anon_key, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
    data = ((await res.json().catch(() => null)) ?? {}) as Record<string, any>
    if (timedOut) throw new Error('timeout') // the answer stalled while it was being read
  } catch {
    const reason = timedOut ? 'Supabase no respondió a tiempo' : 'sin conexión con Supabase'
    if (grant === 'password') {
      const what = timedOut ? 'Supabase no respondió a tiempo.' : 'No se pudo contactar con Supabase.'
      throw new AuthRequestError(`${what} Revisa tu conexión y vuelve a intentarlo.`, 0)
    }
    throw new AuthRequestError(`No se pudo renovar tu sesión: ${reason}. Se reintentará automáticamente.`, 0)
  } finally {
    window.clearTimeout(timer)
  }
  if (!res.ok) throw new AuthRequestError(authError(grant, data, res.status), res.status, errorCode(data))
  if (typeof data.access_token !== 'string' || !data.access_token || typeof data.refresh_token !== 'string' || !data.refresh_token) {
    throw new AuthRequestError('Supabase devolvió una respuesta inesperada. Reintenta en unos segundos.', res.status)
  }
  const expiresAt = Number(data.expires_at) || Math.floor(Date.now() / 1000) + (Number(data.expires_in) || 3600)
  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: expiresAt,
    email: String(data.user?.email ?? session?.email ?? ''),
  }
}

/** Best-effort revocation at Supabase. scope 'local' only ends this browser's session; no scope = GoTrue's default
 *  (every session of the account). */
function logoutRemote(accessToken: string, scope?: 'local' | 'global'): Promise<void> {
  if (!config?.supabase_url || !config.supabase_anon_key) return Promise.resolve()
  return fetch(`${config.supabase_url}/auth/v1/logout${scope ? `?scope=${scope}` : ''}`, {
    method: 'POST',
    headers: { apikey: config.supabase_anon_key, Authorization: `Bearer ${accessToken}` },
  }).then(
    () => undefined,
    () => undefined,
  )
}

/** Signs in and checks that this account may use this NEXUS instance (NEXUS_ALLOWED_EMAILS). */
export async function signIn(email: string, password: string): Promise<Session> {
  const s = await tokenRequest('password', { email: email.trim(), password })
  const res = await fetch(apiUrl('/api/settings'), { headers: { Authorization: `Bearer ${s.access_token}` } }).catch(() => null)
  if (res && (res.status === 401 || res.status === 403 || res.status === 503)) {
    void logoutRemote(s.access_token, 'local') // do not leave an unused session behind
    if (res.status === 503) {
      // Transient. The server's own message says why: Supabase unreachable from the backend, or this instance is in
      // standby while another one is active (deploys).
      const detail = await res.json().then((d: { detail?: unknown } | null) => d?.detail, () => undefined)
      throw new Error(typeof detail === 'string' && detail.trim() ? detail : 'El servidor NEXUS no pudo verificar tu sesión ahora mismo. Reintenta en unos segundos.')
    }
    throw new Error('Tu cuenta no tiene acceso a esta instancia de NEXUS.')
  }
  setSession(s)
  return s
}

/** Refreshes the access token (deduplicated). Resolves to the new session, or to null when Supabase rejected the
 *  refresh token for good (the user is signed out). Rejects with an AuthRequestError when the token could not be
 *  refreshed right now (no connection, 5xx, 429…): the session is kept and a retry with backoff is scheduled. */
export function refreshSession(): Promise<Session | null> {
  if (!session) return Promise.resolve(null)
  if (!authEnabled()) return Promise.resolve(session)
  if (refreshing) return refreshing
  if (lastFailure && Date.now() - lastFailure.at < FAILURE_COOLDOWN_MS) return Promise.reject(lastFailure.error)
  const refreshToken = session.refresh_token
  const attempt = tokenRequest('refresh_token', { refresh_token: refreshToken }).then(
    (s): Session | null => {
      if (!session) return null // signed out meanwhile (maybe in another tab): never resurrect the session
      if (session.refresh_token !== refreshToken) return session // another tab refreshed first: keep its session
      setSession(s)
      return s
    },
    (err: unknown): Session | null => {
      if (!session) return null
      if (session.refresh_token !== refreshToken) return session
      // Another tab may have rotated the refresh token already (its storage event can arrive later): use its session.
      const stored = readStored()
      if (stored && stored.refresh_token !== refreshToken) {
        applyExternal(stored)
        return stored
      }
      if (isDefinitiveRejection(err)) {
        setSession(null)
        return null
      }
      const error = err instanceof AuthRequestError ? err : new AuthRequestError('No se pudo renovar tu sesión.', 0)
      failures += 1
      lastFailure = { at: Date.now(), error }
      scheduleRetry()
      throw error
    },
  )
  refreshing = attempt.finally(() => {
    refreshing = null
  })
  return refreshing
}

/** Refreshes shortly before expiry (call before API requests). A transient failure keeps the current token. While a
 *  retry is scheduled, requests go out with the current token (a 401 still asks for a refresh): polling must not turn
 *  the backoff into one Supabase call every few seconds per tab. */
export async function ensureFreshSession(): Promise<void> {
  if (!authEnabled() || !session || session.expires_at - 30 >= Date.now() / 1000) return
  if (failures > 0 && !refreshing) return
  await refreshSession().catch(() => undefined)
}

/** Signs out here and in every open tab. The button keeps GoTrue's default scope; automatic sign-outs pass
 *  scope 'local' so they never end the sessions of the account on other devices. */
export async function signOut(opts: { scope?: 'local' | 'global' } = {}): Promise<void> {
  const s = session
  setSession(null)
  if (s) await logoutRemote(s.access_token, opts.scope)
}

// Keep tabs in sync (login, refresh and logout in one tab apply to all).
window.addEventListener('storage', (e) => {
  if (e.key === STORAGE_KEY) applyExternal(readStored())
})
// Retry a failed or overdue refresh as soon as the connection is back or the tab is visible again.
window.addEventListener('online', refreshIfDue)
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refreshIfDue()
  })
}
