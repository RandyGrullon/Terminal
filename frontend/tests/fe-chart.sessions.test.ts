// Session bands of the main chart (lib/sessions): the London-New York overlap (12:00–16:00 UTC, Monday to Friday), the
// broker's rollover (23:00–01:00 of XM's server, Monday before 02:00) and the Friday close (from 21:00 of the server).
// XM's server is GMT+2, GMT+3 with the US daylight saving time. Bars above 1h get no bands.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { SESSION_COLORS, SESSION_LABELS, SESSION_NOTE, sessionBandOf, sessionBands, usDst, xmServerOffsetHours } from '../src/lib/sessions'
import { tfSeconds } from '../src/lib/tradeStyle'

const utc = (y: number, m: number, d: number, h = 0, min = 0) => Date.UTC(y, m - 1, d, h, min) / 1000

test('US daylight saving time and the server offset of XM', () => {
  // 2026: DST from Sunday 8 March 07:00 UTC to Sunday 1 November 06:00 UTC
  assert.equal(usDst(utc(2026, 3, 8, 6, 59)), false)
  assert.equal(usDst(utc(2026, 3, 8, 7, 0)), true)
  assert.equal(usDst(utc(2026, 11, 1, 5, 59)), true)
  assert.equal(usDst(utc(2026, 11, 1, 6, 0)), false)
  assert.equal(usDst(utc(2027, 3, 14, 7, 0)), true) // 2027: second Sunday of March is the 14th
  assert.equal(usDst(utc(2027, 3, 13, 12, 0)), false)
  assert.equal(xmServerOffsetHours(utc(2026, 7, 15, 12)), 3)
  assert.equal(xmServerOffsetHours(utc(2026, 11, 18, 12)), 2)
})

test('each window, in winter and in summer', () => {
  // Wednesday 18 November 2026 (winter: server = UTC+2)
  assert.equal(sessionBandOf(utc(2026, 11, 18, 11, 30)), null)
  assert.equal(sessionBandOf(utc(2026, 11, 18, 12, 0)), 'overlap')
  assert.equal(sessionBandOf(utc(2026, 11, 18, 15, 30)), 'overlap')
  assert.equal(sessionBandOf(utc(2026, 11, 18, 16, 0)), null)
  assert.equal(sessionBandOf(utc(2026, 11, 18, 20, 30)), null) // 22:30 server
  assert.equal(sessionBandOf(utc(2026, 11, 18, 21, 0)), 'rollover') // 23:00 server
  assert.equal(sessionBandOf(utc(2026, 11, 18, 22, 30)), 'rollover') // 00:30 server (Thursday)
  assert.equal(sessionBandOf(utc(2026, 11, 18, 23, 0)), null) // 01:00 server
  // Friday 20 November: from 21:00 of the server (19:00 UTC)
  assert.equal(sessionBandOf(utc(2026, 11, 20, 18, 30)), null)
  assert.equal(sessionBandOf(utc(2026, 11, 20, 19, 0)), 'friday')
  assert.equal(sessionBandOf(utc(2026, 11, 20, 21, 30)), 'friday') // 23:30 server: still Friday
  // the weekend has no bands; Monday before 02:00 of the server is the weekly open
  assert.equal(sessionBandOf(utc(2026, 11, 21, 13, 0)), null)
  assert.equal(sessionBandOf(utc(2026, 11, 22, 13, 0)), null)
  assert.equal(sessionBandOf(utc(2026, 11, 22, 22, 30)), 'rollover') // Monday 00:30 server
  assert.equal(sessionBandOf(utc(2026, 11, 22, 23, 30)), 'rollover') // Monday 01:30 server
  assert.equal(sessionBandOf(utc(2026, 11, 23, 0, 0)), null) // Monday 02:00 server
  // summer (server = UTC+3): the rollover is an hour earlier in UTC, the overlap stays at 12–16 UTC
  assert.equal(sessionBandOf(utc(2026, 7, 15, 20, 0)), 'rollover') // 23:00 server
  assert.equal(sessionBandOf(utc(2026, 7, 15, 19, 30)), null)
  assert.equal(sessionBandOf(utc(2026, 7, 15, 12, 0)), 'overlap')
  assert.equal(sessionBandOf(utc(2026, 7, 17, 18, 0)), 'friday') // Friday 21:00 server
  assert.equal(sessionBandOf(Number.NaN), null)
})

test('bands of a day of 30m bars, and none above 1h', () => {
  const day = utc(2026, 11, 18)
  const times = Array.from({ length: 48 }, (_, i) => day + i * 1800)
  const rows = sessionBands(times, tfSeconds('30m') ?? 0)
  assert.equal(rows.filter((r) => r.band === 'overlap').length, 8) // 12:00 … 15:30 UTC
  assert.equal(rows.filter((r) => r.band === 'rollover').length, 4) // 21:00 … 22:30 UTC (23:00–01:00 of the server)
  assert.equal(rows.filter((r) => r.band === 'friday').length, 0)
  assert.deepEqual(sessionBands(times, tfSeconds('4h') ?? 0), [])
  assert.deepEqual(sessionBands(times, 0), [])
  assert.equal(sessionBands(times, 3600).length, rows.length) // 1h still has them
  assert.equal(tfSeconds('30m'), 1800)
  assert.equal(tfSeconds('zz'), null)
})

test('the legend texts are honest: context of a research that found no edge', () => {
  assert.deepEqual(Object.keys(SESSION_COLORS), ['overlap', 'rollover', 'friday'])
  assert.match(SESSION_LABELS.overlap.hint, /12:00–16:00 UTC/)
  assert.match(SESSION_LABELS.rollover.hint, /servidor de XM/)
  assert.match(SESSION_NOTE, /ninguna estrategia aguantó datos nuevos/)
  assert.match(SESSION_NOTE, /No es una señal/)
})
