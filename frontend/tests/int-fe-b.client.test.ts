// Regression (cloud cross_area, standby instance): during a deploy the instance in standby answers HTTP 503 +
// Retry-After with "Instancia en espera: …" (app.main StandbyGate). It is transient like the auth 503 (I5): the
// session is kept and GET requests are retried with backoff; POST is never repeated; the login shows the server's
// message instead of "sin acceso". (Its WebSocket close 1013 is covered by tests/fe-shell.ws.test.ts.)
import { AUTH_CONFIG, SUPABASE, autoRunTimersUpTo, callsTo, clearAllTimers, json, loginFromOtherTab, onFetch, session, tokenAnswer } from './int-fe-b.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ApiError, request } from '../src/api/client'
import * as auth from '../src/auth/session'

const BACKEND = 'https://nexus-production.up.railway.app'
// app.main STANDBY_DETAIL
const STANDBY = {
  detail:
    'Instancia en espera: otra instancia de NEXUS está activa con la misma base de datos (por ejemplo, durante un despliegue). Reintenta en unos segundos.',
}

let backend: (n: number) => Response = () => json(200, { ok: true })
let backendCalls = 0
let tokenSeq = 0

onFetch((call) => {
  if (call.url === `${BACKEND}/api/auth/config`) return json(200, AUTH_CONFIG)
  if (call.url.startsWith(`${SUPABASE}/auth/v1/token`)) return tokenAnswer(++tokenSeq)
  if (call.url.startsWith(`${SUPABASE}/auth/v1/logout`)) return new Response(null, { status: 204 })
  if (call.url.startsWith(`${BACKEND}/api/`)) return backend(++backendCalls)
  throw new Error(`fetch no esperado: ${call.method} ${call.url}`)
})

autoRunTimersUpTo(3000) // the client's 1 s / 3 s retry waits run by themselves; session refresh timers do not

function start(): void {
  clearAllTimers()
  loginFromOtherTab(session('a'))
  backendCalls = 0
}

test('setup: cloud mode', async () => {
  assert.equal((await auth.loadAuthConfig()).mode, 'supabase')
})

test('GET: the standby 503 is retried and the session is kept', async () => {
  start()
  backend = (n) => (n <= 2 ? json(503, STANDBY) : json(200, { settings: { a: 1 } }))
  const data = await request<{ settings: Record<string, number> }>('GET', '/api/settings')
  assert.deepEqual(data, { settings: { a: 1 } })
  assert.equal(backendCalls, 3)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
  assert.equal(callsTo('/auth/v1/logout').length, 0)
})

test('GET: a standby that lasts is reported with its message, without signing out', async () => {
  start()
  backend = () => json(503, STANDBY)
  await assert.rejects(request('GET', '/api/paper/account'), (e: unknown) => e instanceof ApiError && e.status === 503 && /^Instancia en espera/.test(e.message))
  assert.equal(backendCalls, 3)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
  assert.equal(callsTo('/auth/v1/logout').length, 0)
})

test('POST: the standby 503 is not retried (an order is never sent twice) and the session is kept', async () => {
  start()
  backend = () => json(503, STANDBY)
  await assert.rejects(request('POST', '/api/paper/order', { body: { symbol: 'X' } }), (e: unknown) => e instanceof ApiError && e.status === 503)
  assert.equal(backendCalls, 1)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
})

test('login while the instance is in standby: the server message, never "sin acceso"', async () => {
  start()
  await auth.signOut({ scope: 'local' })
  backend = () => json(503, STANDBY)
  const logoutsBefore = callsTo('/auth/v1/logout').length
  await assert.rejects(auth.signIn('owner@example.org', 'pw'), (e: unknown) => e instanceof Error && /^Instancia en espera/.test(e.message) && !/no tiene acceso/.test(e.message))
  assert.equal(auth.currentSession(), null, 'not logged in yet: the access check did not run')
  assert.equal(callsTo('/auth/v1/logout').length - logoutsBefore, 1, 'the unused session is revoked (scope=local)')
})
