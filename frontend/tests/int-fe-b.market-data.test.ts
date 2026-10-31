// Regression (engine cross_area, chart race of the semáforo veto): on a bar close the chart re-reads NEXUS after
// 1.5 s, but the server decides the closed bar, and stores any semáforo veto of its entry, only after the engine run
// and the macro checks; then it publishes the WebSocket 'nexus' message (the live view, veto applied). A re-read that
// reached the server before the veto kept the vetoed entry on the chart: its BUY marker survived the merge of the
// message (markers merge by time|kind|position) and its level lines (bars.pos_*) are not in the message.
// - the chart reads NEXUS again when the 'nexus' message arrives;
// - unless the bar-close re-read is still pending: it runs after the message anyway, so it is not doubled.
// Runs useMarketData on tests/int-fe-b.react-shim.ts (see tests/int-fe-b.run.mjs) against the fake browser.
import { FakeWebSocket, fetchCalls, flush, json, onFetch, runTimers } from './int-fe-b.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { renderHook } from './int-fe-b.react-shim'
import type { ChartMarker } from '../src/api/types'
import { socket } from '../src/api/ws'
import { useMarketData } from '../src/components/chart/useMarketData'

const SYMBOL = 'BINANCE:BTCUSDT'
const TF = '1h'
const N = 60
const T0 = 1_790_002_800 // a whole hour (unix s)
const times = Array.from({ length: N }, (_, i) => T0 + i * 3600)
const TK = times[N - 1] // the bar that closes in these tests
const candles = times.map((t, i) => ({ time: t, open: 100 + i, high: 101 + i, low: 99 + i, close: 100.5 + i, volume: 10 }))

const BUY: ChartMarker = { time: TK, kind: 'BUY', position: 'belowBar', shape: 'arrowUp', color: '#26a69a', text: 'COMPRAR' }
const REJECTED: ChartMarker = { time: TK, kind: 'REJECTED_LONG', position: 'belowBar', shape: 'circle', color: '#888888', text: 'Rechazada' }

/** /api/nexus answer: `markers`, and level lines on the last bar when the entry opened a position. */
function nexusAnswer(markers: ChartMarker[], inPosition: boolean) {
  const line = (v: number) => times.map((_, i) => (inPosition && i === N - 1 ? v : null))
  return {
    symbol: SYMBOL,
    tf: TF,
    provider: 'test',
    history: 'full',
    note: null,
    ok: true,
    message: null,
    params: {},
    bars: { time: times, pos_entry: line(158), pos_sl: line(150), pos_tp1: line(165), pos_tp2: line(170), pos_tp3: line(175) },
    markers,
    trades: [],
    current: { time: TK, action: inPosition ? 'HOLD' : 'WAIT' },
    prediction: null,
    learning: {},
    stats: {},
    quick: null,
  }
}

const nexusMsg = (markers: ChartMarker[]) => ({ type: 'nexus', symbol: SYMBOL, tf: TF, current: { time: TK, action: 'WAIT' }, markers })
const closedCandle = (close = candles[N - 1].close) => ({ type: 'candle', symbol: SYMBOL, tf: TF, candle: { ...candles[N - 1], close }, closed: true })

let answer = nexusAnswer([], false)
onFetch((call) => {
  if (call.url.includes('/api/candles?')) return json(200, { symbol: SYMBOL, tf: TF, provider: 'test', history: 'full', note: null, candles })
  if (call.url.includes('/api/nexus?')) return json(200, answer)
  throw new Error(`fetch no esperado: ${call.method} ${call.url}`)
})

const nexusCalls = () => fetchCalls.filter((c) => c.url.includes('/api/nexus?')).length
const hook = renderHook(() => useMarketData(SYMBOL, TF, () => undefined))
const md = () => hook.result.current
const kindsAt = (t: number) =>
  (md().nexus?.markers ?? [])
    .filter((m) => m.time === t)
    .map((m) => m.kind)
    .sort()

test('setup: candles and NEXUS load, the socket is live', async () => {
  await flush()
  hook.rerender()
  assert.equal(md().candles?.candles.length, N)
  assert.ok(md().nexus?.ok)
  assert.equal(nexusCalls(), 1)
  socket.connect()
  const ws = FakeWebSocket.last()
  ws.serverOpen()
  ws.serverSend({ type: 'hello', version: 'test', server_time: Date.now(), topics: [] })
  assert.equal(socket.status, 'open')
})

test('a bar-close read that reached the server before the veto is corrected when the nexus message arrives', async () => {
  const ws = FakeWebSocket.last()
  const before = nexusCalls()
  answer = nexusAnswer([BUY], true) // the server has not stored the veto of the closed bar yet
  ws.serverSend(closedCandle())
  assert.equal(runTimers((t) => t.ms === 1500), 1, 'the debounced bar-close read')
  await flush()
  hook.rerender()
  assert.deepEqual(kindsAt(TK), ['BUY'])
  assert.equal(md().nexus?.bars.pos_entry[N - 1], 158)

  answer = nexusAnswer([REJECTED], false) // veto stored; the live view is published next
  ws.serverSend(nexusMsg([REJECTED]))
  hook.rerender()
  assert.deepEqual(kindsAt(TK), ['BUY', 'REJECTED_LONG'], 'the message is merged at once')
  await flush()
  hook.rerender()
  assert.equal(nexusCalls() - before, 2, 'NEXUS is read again after the message')
  assert.deepEqual(kindsAt(TK), ['REJECTED_LONG'], 'the vetoed entry is gone')
  assert.equal(md().nexus?.bars.pos_entry[N - 1], null, 'and so are its level lines')
  assert.equal(md().nexus?.current?.action, 'WAIT')
})

test('a nexus message before the bar-close read adds no request: that read runs after it', async () => {
  const ws = FakeWebSocket.last()
  const before = nexusCalls()
  ws.serverSend(closedCandle(160.5)) // fast server: its message arrives within the 1.5 s debounce
  ws.serverSend(nexusMsg([REJECTED]))
  await flush()
  assert.equal(nexusCalls(), before, 'nothing yet: the pending bar-close read is enough')
  assert.equal(runTimers((t) => t.ms === 1500), 1)
  await flush()
  assert.equal(nexusCalls(), before + 1)
  await flush()
  assert.equal(nexusCalls(), before + 1, 'no further reads (no loop)')
})
