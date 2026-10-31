// Regression (transient-auth-failure-logout, auth-refresh-signout) for the REST client in cloud mode:
// - HTTP 503 "no se pudo verificar tu sesión" (the backend could not reach Supabase) never signs out; GET requests are
//   retried with backoff, other methods report the Spanish message;
// - a 401 whose token refresh fails only because Supabase is unreachable keeps the session (Spanish error, no logout);
// - a 401 that persists with a freshly refreshed token signs out with scope=local (never every device's session).
import { AUTH_CONFIG, SUPABASE, autoRunTimersUpTo, callsTo, clearAllTimers, json, loginFromOtherTab, onFetch, session, tokenAnswer } from './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ApiError, request } from '../src/api/client'
import * as auth from '../src/auth/session'

const BACKEND = 'https://nexus-production.up.railway.app'
const UNVERIFIED = { detail: 'No se pudo verificar tu sesión con Supabase; reintenta en unos segundos.' }

let backend: (n: number) => Response = () => json(200, { ok: true })
let backendCalls = 0
let token: 'offline' | 'ok' | Response = 'ok'
let tokenSeq = 0

onFetch((call) => {
  if (call.url === `${BACKEND}/api/auth/config`) return json(200, AUTH_CONFIG)
  if (call.url.startsWith(`${SUPABASE}/auth/v1/token`)) {
    if (token === 'offline') throw new TypeError('Failed to fetch')
    return token === 'ok' ? tokenAnswer(++tokenSeq) : token.clone()
  }
  if (call.url.startsWith(`${SUPABASE}/auth/v1/logout`)) return new Response(null, { status: 204 })
  if (call.url.startsWith(`${BACKEND}/api/`)) return backend(++backendCalls)
  throw new Error(`fetch no esperado: ${call.method} ${call.url}`)
})

autoRunTimersUpTo(3000) // the client's 1 s / 3 s retry waits run by themselves; session refresh timers do not

function start(): void {
  clearAllTimers()
  loginFromOtherTab(session('a'))
  backendCalls = 0
  token = 'ok'
}

test('setup: cloud mode', async () => {
  assert.equal((await auth.loadAuthConfig()).mode, 'supabase')
})

test('GET: 503 "no se pudo verificar tu sesión" is retried and the session is kept', async () => {
  start()
  backend = (n) => (n <= 2 ? json(503, UNVERIFIED) : json(200, { settings: { a: 1 } }))
  const data = await request<{ settings: Record<string, number> }>('GET', '/api/settings')
  assert.deepEqual(data, { settings: { a: 1 } })
  assert.equal(backendCalls, 3)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
  assert.equal(callsTo('/auth/v1/logout').length, 0)
})

test('GET: a 503 that lasts is reported with the server message, without signing out', async () => {
  start()
  backend = () => json(503, UNVERIFIED)
  await assert.rejects(request('GET', '/api/settings'), (e: unknown) => e instanceof ApiError && e.status === 503 && /verificar tu sesión/.test(e.message))
  assert.equal(backendCalls, 3)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
})

test('POST: the auth 503 is not retried (never repeat an order) and the session is kept', async () => {
  start()
  backend = () => json(503, UNVERIFIED)
  await assert.rejects(request('POST', '/api/paper/order', { body: { symbol: 'X' } }), (e: unknown) => e instanceof ApiError && e.status === 503)
  assert.equal(backendCalls, 1)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
})

test('a 503 because a module is not available is not retried', async () => {
  start()
  backend = () => json(503, { detail: 'El motor NEXUS no está disponible' })
  await assert.rejects(request('GET', '/api/nexus'), /El motor NEXUS no está disponible/)
  assert.equal(backendCalls, 1)
})

test('401 while Supabase is unreachable: no sign-out, Spanish error, the session stays', async () => {
  start()
  backend = () => json(401, { detail: 'No autorizado: inicia sesión en NEXUS.' })
  token = 'offline'
  await assert.rejects(request('GET', '/api/settings'), (e: unknown) => e instanceof ApiError && /renovar tu sesión/.test(e.message))
  assert.equal(auth.currentSession()?.access_token, 'at-a')
  assert.equal(callsTo('/auth/v1/logout').length, 0)
})

test('401 again with a freshly refreshed token: sign out of this browser only (scope=local)', async () => {
  start()
  backend = () => json(401, { detail: 'No autorizado: inicia sesión en NEXUS.' })
  token = 'ok'
  await assert.rejects(request('GET', '/api/settings'), (e: unknown) => e instanceof ApiError && e.status === 401)
  assert.equal(backendCalls, 2, 'retried once with the new token')
  assert.equal(auth.currentSession(), null)
  const logout = callsTo('/auth/v1/logout')
  assert.equal(logout.length, 1)
  assert.match(logout[0].url, /\?scope=local$/)
})

test('401 and the refresh token is rejected for good: back to the login screen', async () => {
  start()
  const before = callsTo('/auth/v1/logout').length
  backend = () => json(401, { detail: 'No autorizado: inicia sesión en NEXUS.' })
  token = json(400, { code: 400, error_code: 'session_not_found', msg: 'Session not found' })
  await assert.rejects(request('GET', '/api/settings'), (e: unknown) => e instanceof ApiError && e.status === 401)
  assert.equal(backendCalls, 1)
  assert.equal(auth.currentSession(), null)
  assert.equal(callsTo('/auth/v1/logout').length, before)
})
