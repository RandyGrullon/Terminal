// Regression (auth-refresh-signout, transient-auth-failure-logout, vite-api-base-misconfig-silent):
// - a token refresh that fails because the network or Supabase is down (no connection, 5xx, 429) keeps the session in
//   every tab and retries with backoff (also when the browser is back online or the tab visible again); only a
//   definitive rejection of the refresh token signs out;
// - login errors are Spanish and a backend that cannot verify the session (503) is not "sin acceso";
// - automatic sign-outs revoke only this browser's session (scope=local);
// - VITE_API_BASE copied without https:// still reaches the backend, and a URL that is not a NEXUS backend (404)
//   gives a clear Spanish error instead of skipping the login screen.
import { AUTH_CONFIG, SUPABASE, callsTo, clearAllTimers, doc, flush, json, loginFromOtherTab, nowS, onFetch, pendingTimers, runTimers, session, stored, tokenAnswer, win } from './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as auth from '../src/auth/session'
import * as config from '../src/lib/config'

const BACKEND = 'https://nexus-production.up.railway.app'

type Mode = {
  config: number | 'html' | 'ok'
  token: 'offline' | number | 'ok'
  tokenBody?: Record<string, unknown>
  settings: number
  settingsBody?: unknown // body of a non-200 /api/settings answer (default {detail: 'x'})
}
const mode: Mode = { config: 'ok', token: 'ok', settings: 200 }
let tokenSeq = 0

onFetch((call) => {
  if (call.url === `${BACKEND}/api/auth/config`) {
    if (mode.config === 'ok') return json(200, AUTH_CONFIG)
    if (mode.config === 'html') return new Response('<!doctype html><title>Vercel</title>', { status: 200 })
    return json(mode.config, { error: 'not found' })
  }
  if (call.url.startsWith(`${SUPABASE}/auth/v1/token`)) {
    if (mode.token === 'offline') throw new TypeError('Failed to fetch')
    if (mode.token === 'ok') return tokenAnswer(++tokenSeq)
    return json(mode.token, mode.tokenBody ?? { msg: 'upstream error' })
  }
  if (call.url.startsWith(`${SUPABASE}/auth/v1/logout`)) return new Response(null, { status: 204 })
  if (call.url === `${BACKEND}/api/settings`) {
    if (mode.settings === 200) return json(200, { settings: {} })
    return mode.settingsBody === null ? new Response('Service Unavailable', { status: mode.settings }) : json(mode.settings, mode.settingsBody ?? { detail: 'x' })
  }
  throw new Error(`fetch no esperado: ${call.method} ${call.url}`)
})

const seen: (string | null)[] = []
auth.onAuthChange((s) => seen.push(s ? s.access_token : null))

/** Fresh state for a scenario: no pending timers, a session written by "another tab", empty listener log. */
function start(s = session('a', 20)): void {
  clearAllTimers()
  loginFromOtherTab(s)
  seen.length = 0
}

test('VITE_API_BASE without https:// (as Railway shows the domain) is normalized', () => {
  assert.equal(config.API_BASE, BACKEND)
  assert.equal(config.apiUrl('/api/health'), `${BACKEND}/api/health`)
  assert.equal(config.wsUrl(), 'wss://nexus-production.up.railway.app/ws')
  assert.equal(config.normalizeApiBase('  https://x.up.railway.app///  '), 'https://x.up.railway.app')
  assert.equal(config.normalizeApiBase('x.up.railway.app'), 'https://x.up.railway.app')
  assert.equal(config.normalizeApiBase('localhost:8420'), 'http://localhost:8420')
  assert.equal(config.normalizeApiBase('127.0.0.1:8420/'), 'http://127.0.0.1:8420')
  assert.equal(config.normalizeApiBase('//x.example'), 'https://x.example')
  assert.equal(config.normalizeApiBase('wss://x.example'), 'https://x.example')
  assert.equal(config.normalizeApiBase('/nexus/'), '/nexus')
  assert.equal(config.normalizeApiBase(''), '')
  assert.equal(config.normalizeApiBase(undefined), '')
})

test('a URL that is not a NEXUS backend gives a clear Spanish error instead of skipping the login', async () => {
  mode.config = 404
  await assert.rejects(auth.loadAuthConfig(), (e: Error) => /VITE_API_BASE/.test(e.message) && /no parece un backend NEXUS/.test(e.message))
  mode.config = 'html' // e.g. a SPA fallback answering index.html with 200
  await assert.rejects(auth.loadAuthConfig(), (e: Error) => /no parece un backend NEXUS/.test(e.message))
  mode.config = 502
  await assert.rejects(auth.loadAuthConfig(), /HTTP 502/)
  assert.equal(auth.authEnabled(), false)
  mode.config = 'ok'
  const cfg = await auth.loadAuthConfig()
  assert.equal(cfg.mode, 'supabase')
  assert.equal(auth.authEnabled(), true)
})

test('no connection while refreshing: the session stays (here and in other tabs) and a retry is scheduled', async () => {
  start()
  mode.token = 'offline'
  await assert.rejects(auth.refreshSession(), (e: unknown) => e instanceof auth.AuthRequestError && e.status === 0)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
  assert.equal(stored()?.access_token, 'at-a', 'localStorage keeps the session, so other tabs stay logged in')
  assert.ok(!seen.includes(null), 'nobody was told the session ended')
  const retry = pendingTimers()
  assert.equal(retry.length, 1)
  assert.ok(retry[0].ms <= 5000, `retry in ${retry[0].ms} ms`)
  // API calls right after the failure do not hammer Supabase and never throw
  const before = callsTo('/auth/v1/token').length
  await auth.ensureFreshSession()
  assert.equal(callsTo('/auth/v1/token').length, before)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
})

test('polling during an outage never bypasses the retry backoff (no Supabase call per request)', async () => {
  start({ ...session('poll'), expires_at: nowS() - 5 }) // expired: every request would want to refresh it
  mode.token = 503
  await auth.refreshSession().catch(() => undefined) // schedules the retry (5 s)
  const calls = callsTo('/auth/v1/token').length
  const realNow = Date.now
  let t = realNow()
  Date.now = () => t
  try {
    for (let i = 0; i < 20; i++) {
      t += 1000 // one polling request per second for 20 s, well past the 4 s failure cooldown
      await auth.ensureFreshSession()
    }
  } finally {
    Date.now = realNow
  }
  assert.equal(callsTo('/auth/v1/token').length, calls, 'only the scheduled retries (timers) contact Supabase')
  assert.equal(pendingTimers().length, 1, 'the retry is still scheduled')
  assert.equal(auth.currentSession()?.access_token, 'at-poll', 'session kept')
  mode.token = 'ok'
})

test('Supabase 503 / 429 / 500 while refreshing are transient too', async () => {
  for (const status of [503, 429, 500]) {
    start()
    mode.token = status
    await assert.rejects(auth.refreshSession(), (e: unknown) => e instanceof auth.AuthRequestError && e.status === status)
    assert.equal(auth.currentSession()?.access_token, 'at-a', `HTTP ${status}`)
    assert.equal(stored()?.access_token, 'at-a')
    assert.ok(!seen.includes(null))
  }
})

test('the scheduled retry renews the session once Supabase answers again', async () => {
  start()
  mode.token = 'offline'
  await auth.refreshSession().catch(() => undefined)
  mode.token = 'ok'
  assert.equal(runTimers(), 1)
  await flush()
  const s = auth.currentSession()
  assert.ok(s && s.access_token !== 'at-a', 'new access token')
  assert.equal(stored()?.access_token, s?.access_token)
  assert.equal(seen[seen.length - 1], s?.access_token)
  const next = pendingTimers()
  assert.equal(next.length, 1)
  assert.ok(next[0].ms > 60_000, 'back to the normal refresh before expiry')
})

test('back online: a failed refresh is retried at once (even inside the failure cooldown)', async () => {
  start()
  mode.token = 'offline'
  await auth.refreshSession().catch(() => undefined)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
  const calls = callsTo('/auth/v1/token').length
  mode.token = 'ok'
  win.dispatch('online')
  await flush()
  assert.equal(callsTo('/auth/v1/token').length, calls + 1)
  const s = auth.currentSession()
  assert.ok(s, 'still signed in')
  assert.notEqual(s.access_token, 'at-a')
})

test('tab visible again after sleep: an expired token is refreshed right away', async () => {
  start({ ...session('slept'), expires_at: nowS() - 120 })
  mode.token = 'ok'
  doc.visibilityState = 'visible'
  doc.dispatch('visibilitychange')
  await flush()
  const s = auth.currentSession()
  assert.ok(s, 'still signed in')
  assert.notEqual(s.access_token, 'at-slept')
})

test('a definitive rejection of the refresh token signs out (new and legacy GoTrue codes)', async () => {
  const bodies = [
    { code: 400, error_code: 'refresh_token_not_found', msg: 'Invalid Refresh Token: Refresh Token Not Found' },
    { code: 400, error_code: 'session_not_found', msg: 'Session not found' },
    { error: 'invalid_grant', error_description: 'Invalid Refresh Token: Already Used' },
  ]
  for (const body of bodies) {
    start()
    mode.token = 400
    mode.tokenBody = body
    assert.equal(await auth.refreshSession(), null)
    assert.equal(auth.currentSession(), null)
    assert.equal(stored(), null)
    assert.equal(seen[seen.length - 1], null)
  }
  mode.tokenBody = undefined
})

test('another tab already rotated the refresh token: use its session instead of signing out', async () => {
  start(session('mine'))
  mode.token = 400
  mode.tokenBody = { error_code: 'refresh_token_already_used', msg: 'Invalid Refresh Token: Already Used' }
  const other = session('other-tab')
  onFetchOnce('/auth/v1/token', () => {
    localStorage.setItem('nexus.auth.v1', JSON.stringify(other)) // its storage event has not arrived yet
  })
  const s = await auth.refreshSession()
  assert.equal(s?.refresh_token, 'rt-other-tab')
  assert.equal(auth.currentSession()?.refresh_token, 'rt-other-tab')
  assert.equal(stored()?.refresh_token, 'rt-other-tab')
  mode.tokenBody = undefined
})

test('login: Spanish errors; a backend that cannot verify the session (503) is not "sin acceso"', async () => {
  await auth.signOut()
  mode.token = 'ok'
  mode.settings = 503
  mode.settingsBody = { detail: 'No se pudo verificar tu sesión (Supabase no responde); reintenta en unos segundos.' } // app.auth
  await assert.rejects(auth.signIn('owner@example.org', 'pw'), /No se pudo verificar tu sesión \(Supabase no responde\)/)
  assert.equal(auth.currentSession(), null)
  assert.match(callsTo('/auth/v1/logout').at(-1)!.url, /\?scope=local$/, 'the unused session is revoked locally')
  // a deploy in progress: this instance is in standby (app.main StandbyGate); the cause shown is the server's, not Supabase
  mode.settingsBody = { detail: 'Instancia en espera: otra instancia de NEXUS está activa con la misma base de datos. Reintenta en unos segundos.' }
  await assert.rejects(auth.signIn('owner@example.org', 'pw'), (e: Error) => /^Instancia en espera/.test(e.message) && !/Supabase/.test(e.message))
  mode.settingsBody = null // a proxy 503 without JSON
  await assert.rejects(auth.signIn('owner@example.org', 'pw'), /no pudo verificar tu sesión ahora mismo/)
  assert.equal(auth.currentSession(), null)
  mode.settingsBody = undefined
  mode.settings = 401
  await assert.rejects(auth.signIn('owner@example.org', 'pw'), /Tu cuenta no tiene acceso a esta instancia de NEXUS/)
  mode.token = 'offline'
  await assert.rejects(auth.signIn('owner@example.org', 'pw'), (e: Error) => /No se pudo contactar con Supabase/.test(e.message) && !/Failed to fetch/.test(e.message))
  mode.token = 503
  await assert.rejects(auth.signIn('owner@example.org', 'pw'), /Supabase\) no responde \(HTTP 503\)/)
  mode.token = 'ok'
  mode.settings = 200
  const s = await auth.signIn('owner@example.org', 'pw')
  assert.equal(auth.currentSession()?.access_token, s.access_token)
})

test('sign-out scope: automatic sign-outs only end this browser session', async () => {
  start(session('x'))
  await auth.signOut({ scope: 'local' })
  assert.match(callsTo('/auth/v1/logout').at(-1)!.url, /\/auth\/v1\/logout\?scope=local$/)
  start(session('y'))
  await auth.signOut()
  assert.match(callsTo('/auth/v1/logout').at(-1)!.url, /\/auth\/v1\/logout$/)
  assert.equal(auth.currentSession(), null)
})

/** Runs `effect` the next time a request to `part` is made (before the scripted answer). */
function onFetchOnce(part: string, effect: () => void): void {
  const original = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes(part)) {
      globalThis.fetch = original
      effect()
    }
    return original(input, init)
  }) as typeof fetch
}
