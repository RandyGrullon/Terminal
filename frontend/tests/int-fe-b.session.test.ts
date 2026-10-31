// Regression (fe-shell verifier new issue, auth/session.ts tokenRequest): the call to Supabase Auth had no timeout. A
// refresh that never answered kept the deduplicated refresh pending, and ensureFreshSession (every API call near the
// token's expiry) and the 'online' / visible retries waited on it until the browser gave up.
// - after 15 s the call is aborted, request or answer body, and counts as no connection (AuthRequestError status 0):
//   the session stays and the usual retry backoff applies;
// - the login gives a Spanish message instead of an endless spinner;
// - a normal answer leaves no timeout timer behind.
import { AUTH_CONFIG, SUPABASE, clearAllTimers, flush, json, loginFromOtherTab, onFetch, pendingTimers, runTimers, session, stored, tokenAnswer } from './int-fe-b.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as auth from '../src/auth/session'

const BACKEND = 'https://nexus-production.up.railway.app'
const TIMEOUT_MS = 15_000
let tokenSeq = 0
let stall: 'none' | 'request' | 'body' = 'none'

onFetch((call) => {
  if (call.url === `${BACKEND}/api/auth/config`) return json(200, AUTH_CONFIG)
  if (call.url.startsWith(`${SUPABASE}/auth/v1/token`)) return tokenAnswer(++tokenSeq)
  if (call.url.startsWith(`${SUPABASE}/auth/v1/logout`)) return new Response(null, { status: 204 })
  if (call.url === `${BACKEND}/api/settings`) return json(200, { settings: {} })
  throw new Error(`fetch no esperado: ${call.method} ${call.url}`)
})

// The fake fetch of fe-shell.env ignores AbortSignal: a stalled Supabase call here ends only when it is aborted.
const abortError = () => new DOMException('The operation was aborted.', 'AbortError')
const envFetch = globalThis.fetch
let stalledCalls = 0
globalThis.fetch = (async (input: unknown, init: RequestInit = {}) => {
  const url = String(input)
  if (stall === 'none' || !url.startsWith(`${SUPABASE}/auth/v1/token`)) return envFetch(input as RequestInfo, init)
  stalledCalls += 1
  const signal = init.signal
  if (stall === 'request') {
    return new Promise<Response>((_, reject) => signal?.addEventListener('abort', () => reject(abortError()), { once: true }))
  }
  const body = new ReadableStream({
    start(c) {
      signal?.addEventListener('abort', () => c.error(abortError()), { once: true })
    },
  })
  return new Response(body, { status: 200, headers: { 'content-type': 'application/json' } })
}) as typeof fetch

const timeouts = () => pendingTimers().filter((t) => t.ms === TIMEOUT_MS)

test('setup: cloud mode', async () => {
  assert.equal((await auth.loadAuthConfig()).mode, 'supabase')
})

test('a refresh that never answers is aborted after 15 s: transient, the session stays, API calls are released', async () => {
  clearAllTimers()
  loginFromOtherTab(session('a', 20)) // expires in 20 s: API calls want a fresh token first
  stall = 'request'
  const refresh = auth.refreshSession()
  refresh.catch(() => undefined)
  let released = false
  const apiCall = auth.ensureFreshSession().then(() => {
    released = true
  })
  await flush()
  assert.equal(stalledCalls, 1, 'one call to Supabase, shared by both')
  assert.equal(released, false, 'the API call waits for the refresh in flight')
  assert.equal(timeouts().length, 1)
  runTimers((t) => t.ms === TIMEOUT_MS)
  await assert.rejects(refresh, (e: unknown) => e instanceof auth.AuthRequestError && e.status === 0 && /no respondió a tiempo/.test(e.message))
  await apiCall
  assert.equal(released, true)
  assert.equal(auth.isDefinitiveRejection(await refresh.catch((e: unknown) => e)), false)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
  assert.equal(stored()?.access_token, 'at-a', 'other tabs stay logged in')
  const retry = pendingTimers()
  assert.equal(retry.length, 1, 'only the retry is scheduled')
  assert.ok(retry[0].ms <= 5000, `retry in ${retry[0].ms} ms`)
})

test('an answer whose body stalls is aborted too', async () => {
  clearAllTimers()
  loginFromOtherTab(session('b', 20))
  stall = 'body'
  const refresh = auth.refreshSession()
  refresh.catch(() => undefined)
  await flush()
  assert.equal(timeouts().length, 1)
  runTimers((t) => t.ms === TIMEOUT_MS)
  await assert.rejects(refresh, (e: unknown) => e instanceof auth.AuthRequestError && e.status === 0)
  assert.equal(auth.currentSession()?.access_token, 'at-b')
})

test('login: Supabase never answers -> Spanish message, not an endless spinner', async () => {
  clearAllTimers()
  stall = 'request'
  const login = auth.signIn('owner@example.org', 'pw')
  login.catch(() => undefined)
  await flush()
  runTimers((t) => t.ms === TIMEOUT_MS)
  await assert.rejects(login, (e: unknown) => e instanceof auth.AuthRequestError && e.status === 0 && /^Supabase no respondió a tiempo\. Revisa tu conexión/.test(e.message))
})

test('a normal answer leaves no timeout timer behind', async () => {
  clearAllTimers()
  loginFromOtherTab(session('c', 20))
  stall = 'none'
  const s = await auth.refreshSession()
  assert.ok(s && s.access_token !== 'at-c', 'renewed')
  assert.equal(timeouts().length, 0)
})
