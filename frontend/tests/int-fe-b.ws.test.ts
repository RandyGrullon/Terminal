// Regression (fe-shell verifier new issue, api/ws.ts 3-strike guard): the count of 4401 closes that ends in a local
// sign-out was reset by every hello. A server whose clock runs ahead of Supabase's by more than the token lifetime says
// hello and closes with 4401 at once, every time ('Sesión caducada'), so the client refreshed the session and
// reconnected forever (until Supabase rate-limits the refreshes).
// - the count now starts again only once a socket has stayed usable for 30 s, or after a new login;
// - a socket closed before that leaves no timer behind that could reset it later;
// - the fe-shell scenario (refused fresh tokens after a long-lived socket) still signs out after two refreshes.
import { AUTH_CONFIG, FakeWebSocket, SUPABASE, callsTo, clearAllTimers, flush, json, loginFromOtherTab, onFetch, pendingTimers, runTimers, session, tokenAnswer } from './int-fe-b.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { socket } from '../src/api/ws'
import * as auth from '../src/auth/session'

const BACKEND = 'https://nexus-production.up.railway.app'
const STABLE_MS = 30_000
let tokenSeq = 0

onFetch((call) => {
  if (call.url === `${BACKEND}/api/auth/config`) return json(200, AUTH_CONFIG)
  if (call.url.startsWith(`${SUPABASE}/auth/v1/token`)) return tokenAnswer(++tokenSeq)
  if (call.url.startsWith(`${SUPABASE}/auth/v1/logout`)) return new Response(null, { status: 204 })
  throw new Error(`fetch no esperado: ${call.method} ${call.url}`)
})

const tokenOf = (ws: FakeWebSocket) => new URL(ws.url).searchParams.get('token')
const hello = (ws: FakeWebSocket) => ws.serverSend({ type: 'hello', version: 'test', server_time: Date.now(), topics: [] })
const refreshes = () => callsTo('/auth/v1/token').length
const stableTimers = () => pendingTimers().filter((t) => t.ms === STABLE_MS)

/** The newest socket is accepted (hello) and then refused at once, as a server with a skewed clock does. */
async function helloThen4401(): Promise<void> {
  const ws = FakeWebSocket.last()
  ws.serverOpen()
  hello(ws)
  assert.equal(socket.status, 'open')
  ws.serverClose(4401)
  await flush()
}

test('setup: cloud mode, logged in, socket connects with the token', async () => {
  await auth.loadAuthConfig()
  loginFromOtherTab(session('a'))
  socket.connect()
  assert.equal(tokenOf(FakeWebSocket.last()), 'at-a')
})

test('hello and then 4401 at once, every time: local sign-out after 3 closes instead of a refresh loop', async () => {
  clearAllTimers()
  const before = refreshes()
  for (let i = 0; i < 3; i++) await helloThen4401()
  assert.equal(refreshes() - before, 2, 'two refreshes, then give up')
  assert.equal(auth.currentSession(), null)
  const logout = callsTo('/auth/v1/logout')
  assert.equal(logout.length, 1)
  assert.match(logout[0].url, /\?scope=local$/)
  const sockets = FakeWebSocket.all.length
  socket.connect()
  assert.equal(FakeWebSocket.all.length, sockets, 'no socket without a session')
})

test('after that sign-out, a new login connects and the count starts from zero', async () => {
  clearAllTimers()
  loginFromOtherTab(session('b'))
  assert.equal(tokenOf(FakeWebSocket.last()), 'at-b')
  const before = refreshes()
  for (let i = 0; i < 2; i++) await helloThen4401()
  assert.equal(refreshes() - before, 2)
  assert.equal(auth.currentSession()?.email, 'owner@example.org', 'still signed in after two refused sockets')
})

test('a socket that stays usable for 30 s starts the count again; one closed earlier leaves no timer', async () => {
  // two strikes are pending from the previous test: a third quick refusal would sign out
  let ws = FakeWebSocket.last()
  ws.serverOpen()
  hello(ws)
  assert.equal(stableTimers().length, 1)
  assert.equal(runTimers((t) => t.ms === STABLE_MS), 1, 'the socket was accepted for good')
  const before = refreshes()
  ws.serverClose(4401) // e.g. revoked later: strike 1 of a new count
  await flush()
  assert.equal(refreshes() - before, 1, 'refreshed, not signed out')
  assert.ok(auth.currentSession())
  ws = FakeWebSocket.last()
  ws.serverOpen()
  hello(ws)
  ws.serverClose(4401) // strike 2
  await flush()
  assert.equal(stableTimers().length, 0, 'the closed socket cancelled its 30 s timer')
  assert.ok(auth.currentSession(), 'two strikes: still signed in')
  await helloThen4401() // strike 3
  assert.equal(auth.currentSession(), null)
})

test('fe-shell scenario: fresh tokens refused again and again after a long-lived socket: two refreshes, then sign-out', async () => {
  clearAllTimers()
  loginFromOtherTab(session('c'))
  let ws = FakeWebSocket.last()
  ws.serverOpen()
  hello(ws)
  runTimers((t) => t.ms === STABLE_MS) // it lived well past 30 s
  const before = refreshes()
  for (let i = 0; i < 3; i++) {
    ws = FakeWebSocket.last()
    if (i > 0) ws.serverOpen() // accepted, then refused before any hello: the account is no longer allowed
    ws.serverClose(4401)
    await flush()
  }
  assert.equal(refreshes() - before, 2)
  assert.equal(auth.currentSession(), null)
})
