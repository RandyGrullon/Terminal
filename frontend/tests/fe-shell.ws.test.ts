// Regression (ws-auth-4401-and-no-revalidation, transient-auth-failure-logout) for the WebSocket in cloud mode. The
// server now accepts the socket and then closes it with 4401 (token invalid / expired) or 1013 (cannot verify now):
// - nothing is sent before the server's hello (a socket about to be closed with 4401 would lose it);
// - 1013 is transient: keep the session and reconnect with a growing backoff (no tight loop, no refresh);
// - 4401 refreshes the session and reconnects with the new token; if Supabase is unreachable the session is kept and
//   the socket waits for the new token instead of signing out;
// - fresh tokens refused again and again (account no longer allowed) end in a local sign-out, not a refresh loop.
import { AUTH_CONFIG, FakeWebSocket, SUPABASE, callsTo, clearAllTimers, flush, json, loginFromOtherTab, onFetch, pendingTimers, runTimers, session, tokenAnswer, win } from './fe-shell.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { socket } from '../src/api/ws'
import * as auth from '../src/auth/session'

const BACKEND = 'https://nexus-production.up.railway.app'
let token: 'offline' | 'ok' = 'ok'
let tokenSeq = 0

onFetch((call) => {
  if (call.url === `${BACKEND}/api/auth/config`) return json(200, AUTH_CONFIG)
  if (call.url.startsWith(`${SUPABASE}/auth/v1/token`)) {
    if (token === 'offline') throw new TypeError('Failed to fetch')
    return tokenAnswer(++tokenSeq)
  }
  if (call.url.startsWith(`${SUPABASE}/auth/v1/logout`)) return new Response(null, { status: 204 })
  throw new Error(`fetch no esperado: ${call.method} ${call.url}`)
})

const tokenOf = (ws: FakeWebSocket) => new URL(ws.url).searchParams.get('token')
const hello = (ws: FakeWebSocket) => ws.serverSend({ type: 'hello', version: 'test', server_time: Date.now(), topics: [] })
const reconnectDelays = () => pendingTimers().map((t) => t.ms)

test('setup: cloud mode, logged in, socket connects with the token', async () => {
  await auth.loadAuthConfig()
  loginFromOtherTab(session('a'))
  socket.connect()
  const ws = FakeWebSocket.last()
  assert.equal(ws.url, `wss://nexus-production.up.railway.app/ws?token=at-a`)
})

test('messages wait for the server hello (the session is not accepted on open)', () => {
  const ws = FakeWebSocket.last()
  socket.send({ type: 'command', id: 'c1', text: 'help' })
  ws.serverOpen()
  assert.deepEqual(ws.sent, [], 'nothing sent on open: the server may still close it with 4401')
  assert.equal(socket.status, 'connecting')
  hello(ws)
  assert.equal(socket.status, 'open')
  assert.deepEqual(ws.sent.map((m) => JSON.parse(m).type), ['command'])
})

test('1013 (cannot verify now) is transient: growing backoff from 2 s, no refresh, session kept', () => {
  clearAllTimers()
  const tokenCalls = callsTo('/auth/v1/token').length
  const delays: number[] = []
  for (let i = 0; i < 5; i++) {
    const ws = FakeWebSocket.last()
    if (i > 0) ws.serverOpen() // accepted, then closed before any hello
    ws.serverClose(1013)
    const d = reconnectDelays()
    assert.equal(d.length, 1)
    delays.push(d[0])
    runTimers((t) => t.ms <= 10_000)
  }
  assert.deepEqual(delays, [2000, 2000, 2000, 4000, 8000])
  assert.equal(callsTo('/auth/v1/token').length, tokenCalls)
  assert.equal(auth.currentSession()?.access_token, 'at-a')
  const ws = FakeWebSocket.last()
  ws.serverOpen()
  hello(ws)
  assert.equal(socket.status, 'open')
})

test('4401 while Supabase is unreachable: keep the session, wait for a new token, then reconnect', async () => {
  clearAllTimers()
  token = 'offline'
  const sockets = FakeWebSocket.all.length
  FakeWebSocket.last().serverClose(4401)
  await flush()
  assert.equal(callsTo('/auth/v1/token').length > 0, true)
  assert.equal(auth.currentSession()?.access_token, 'at-a', 'not signed out')
  assert.equal(callsTo('/auth/v1/logout').length, 0)
  socket.send({ type: 'ping' }) // would reconnect with the refused token
  runTimers((t) => t.ms <= 10_000 && t.ms !== 5000) // reconnect timers, if any (5000 = the session retry)
  assert.equal(FakeWebSocket.all.length, sockets, 'no reconnection with the refused token')
  token = 'ok'
  win.dispatch('online') // the session module retries now and gets a new token
  await flush()
  const ws = FakeWebSocket.last()
  assert.equal(FakeWebSocket.all.length, sockets + 1)
  assert.equal(tokenOf(ws), auth.currentSession()?.access_token)
  assert.notEqual(tokenOf(ws), 'at-a')
  ws.serverOpen()
  hello(ws)
  assert.equal(socket.status, 'open')
  assert.deepEqual(ws.sent.map((m) => JSON.parse(m).type), ['ping'], 'the queued message is delivered once accepted')
})

test('a refreshed token is handed to the open socket; a 4401 at the old expiry reconnects with it, no new refresh', () => {
  runTimers((t) => t.ms === 30_000) // the open socket lasted (accepted for good): the 4401 count starts again
  clearAllTimers()
  const tokenCalls = callsTo('/auth/v1/token').length
  const sockets = FakeWebSocket.all.length
  const ws = FakeWebSocket.last()
  loginFromOtherTab(session('b')) // e.g. the refresh timer or another tab renewed the session
  assert.deepEqual(JSON.parse(ws.sent[ws.sent.length - 1]), { type: 'auth', token: 'at-b' }, 'no reconnect needed')
  assert.equal(FakeWebSocket.all.length, sockets)
  ws.serverClose(4401) // a server that ignores {type: 'auth'} closes when the handshake token expires
  assert.equal(runTimers((t) => t.ms <= 10_000), 1, 'one reconnect timer (the session refresh timer is not due)')
  assert.equal(tokenOf(FakeWebSocket.last()), 'at-b')
  assert.equal(callsTo('/auth/v1/token').length, tokenCalls)
  FakeWebSocket.last().serverOpen()
  hello(FakeWebSocket.last())
  assert.ok(!FakeWebSocket.last().sent.some((m) => JSON.parse(m).type === 'auth'), 'the handshake token is current')
})

test('fresh tokens refused again and again: local sign-out instead of a refresh loop', async () => {
  runTimers((t) => t.ms === 30_000) // the open socket lasted (accepted for good): the 4401 count starts again
  clearAllTimers()
  token = 'ok'
  const tokenCalls = callsTo('/auth/v1/token').length
  for (let i = 0; i < 3; i++) {
    const ws = FakeWebSocket.last()
    if (i > 0) ws.serverOpen() // accepted, then refused: the account is not allowed (anymore)
    ws.serverClose(4401)
    await flush()
  }
  assert.equal(callsTo('/auth/v1/token').length - tokenCalls, 2, 'two refreshes, then give up')
  assert.equal(auth.currentSession(), null)
  const logout = callsTo('/auth/v1/logout')
  assert.equal(logout.length, 1)
  assert.match(logout[0].url, /\?scope=local$/)
  const sockets = FakeWebSocket.all.length
  socket.connect()
  assert.equal(FakeWebSocket.all.length, sockets, 'no socket without a session')
  assert.equal(socket.status, 'closed')
})
