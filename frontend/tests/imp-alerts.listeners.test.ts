// components/macro/alerts.ts with a fake socket and store: which WS messages become desktop notifications (red
// market alerts, NEXUS entries / exits / early exits of real markets) and that toasts keep working.
import { FakeNotification, doc } from './imp-alerts.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { NexusEvent } from '../src/api/types'
import { macroDesktopNotice, signalDesktopNotice, startAlertListeners } from '../src/components/macro/alerts'
import { enableDesktop } from '../src/components/macro/sound'
import { recorded, socket } from './imp-alerts.fakes'

function event(over: Partial<NexusEvent> & { data?: unknown }): NexusEvent {
  return { id: 41, ts: 1_790_000_000_000, level: 'signal', source: 'nexus', message: 'BINANCE:BTCUSDT 1h → COMPRAR · score +0,52', data: { symbol: 'BINANCE:BTCUSDT', tf: '1h', action: 'BUY' }, ...over }
}

test('NEXUS events that become a desktop notification', () => {
  const buy = signalDesktopNotice(event({}))
  assert.ok(buy)
  assert.equal(buy.title, 'NEXUS · Señal de compra')
  assert.equal(buy.body, 'BINANCE:BTCUSDT 1h → COMPRAR · score +0,52\nSeñal del modelo: no es una orden ni asesoría financiera.')
  assert.equal(buy.tag, 'nexus-event-41')
  assert.equal(signalDesktopNotice(event({ data: { symbol: 'X:Y', tf: '1h', action: 'SELL' } }))?.title, 'NEXUS · Señal de venta')
  assert.equal(signalDesktopNotice(event({ data: { symbol: 'X:Y', tf: '1h', action: 'EXIT_SHORT' } }))?.title, 'NEXUS · Señal de salida')
  const early = signalDesktopNotice(event({ level: 'warn', message: '⚠ BINANCE:ETHUSDT 1h: Cerrar ahora', data: { symbol: 'BINANCE:ETHUSDT', tf: '1h', code: 'EARLY_EXIT' } }))
  assert.equal(early?.title, 'NEXUS · Salida anticipada')
  // not notified: rejected signals, HOLD, other sources, synthetic markets, malformed events
  assert.equal(signalDesktopNotice(event({ level: 'info', data: { symbol: 'X:Y', tf: '1h', kind: 'REJECTED_LONG' } })), null)
  assert.equal(signalDesktopNotice(event({ data: { symbol: 'X:Y', tf: '1h', action: 'HOLD_LONG' } })), null)
  assert.equal(signalDesktopNotice(event({ source: 'paper', level: 'trade' })), null)
  assert.equal(signalDesktopNotice(event({ data: { symbol: 'SIM:NEXUS', tf: '1h', action: 'BUY' } })), null)
  assert.equal(signalDesktopNotice(event({ data: null })), null)
  assert.equal(signalDesktopNotice(event({ message: '' })), null)
  assert.equal(signalDesktopNotice(null), null)
})

test('market alerts: only red ones', () => {
  const red = macroDesktopNotice({ kind: 'event_soon', level: 'red', message: '🔴 IPC en 15 min: no abras posiciones nuevas.' })
  assert.equal(red?.title, 'NEXUS · Semáforo en rojo')
  assert.match(red?.tag ?? '', /^nexus-macro-/)
  const again = macroDesktopNotice({ kind: 'event_soon', level: 'red', message: '🔴 IPC en 15 min: no abras posiciones nuevas.' })
  assert.equal(again?.tag, red?.tag) // same alert -> same tag in every tab
  assert.equal(macroDesktopNotice({ kind: 'event_soon', level: 'amber', message: '🟡 IPC en 1 h' }), null)
  assert.equal(macroDesktopNotice({ kind: 'level', level: 'green', message: '🟢 Operar con normalidad' }), null)
})

test('listeners: toasts as before, desktop notifications only when enabled and in the background', async () => {
  const stop = startAlertListeners()
  assert.equal(socket.count('macro'), 1)
  assert.equal(socket.count('event'), 1)
  const red = { kind: 'event_now', level: 'red', message: '🔴 FOMC: se publica ahora.' }
  socket.emit('macro', { conditions: {}, alert: red })
  assert.deepEqual(recorded.toasts.at(-1), { message: '🔴 FOMC: se publica ahora.', level: 'error' })
  assert.equal(FakeNotification.all.length, 0) // not enabled yet
  FakeNotification.answer = 'granted'
  assert.equal(await enableDesktop(), 'granted')
  doc.hidden = false
  socket.emit('event', { event: event({ id: 1 }) })
  assert.equal(FakeNotification.all.length, 0) // tab visible: the shell's toast is enough
  doc.hidden = true
  socket.emit('event', { event: event({ id: 2 }) })
  socket.emit('event', { event: event({ id: 3, level: 'info', data: { symbol: 'X:Y', tf: '1h', kind: 'REJECTED_SHORT' } }) })
  assert.equal(FakeNotification.all.length, 1)
  const n = FakeNotification.all[0]
  assert.equal(n.title, 'NEXUS · Señal de compra')
  n.onclick?.()
  assert.deepEqual(recorded.charts.at(-1), { symbol: 'BINANCE:BTCUSDT', tf: '1h' })
  socket.emit('macro', { conditions: {}, alert: { kind: 'level', level: 'red', message: '🔴 No operar ahora: VIX 31' } })
  assert.equal(FakeNotification.all.length, 2)
  assert.equal(FakeNotification.all[1].title, 'NEXUS · Semáforo en rojo')
  socket.emit('macro', { conditions: {}, alert: { kind: 'level', level: 'amber', message: '🟡 Precaución' } })
  assert.equal(FakeNotification.all.length, 2)
  assert.deepEqual(recorded.toasts.at(-1), { message: '🟡 Precaución', level: 'warn' })
  stop()
  assert.equal(socket.count('macro'), 0)
  assert.equal(socket.count('event'), 0)
  doc.hidden = false
})
