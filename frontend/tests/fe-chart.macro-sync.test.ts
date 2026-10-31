// Regression (stale-gate-vs-semaforo): the backend merges the semáforo into the NEXUS verdict when /api/nexus is
// read, but the chart only re-read NEXUS on bar closes, so during a red "no operar" window it kept showing
// COMPRAR / TOMAR (and NO TOMAR after the window ended) until the next close. The chart now re-reads NEXUS
// (debounced) when the symbol's level or window changes, and polls at most every 60 s while not green.
import assert from 'node:assert/strict'
import { mock, test } from 'node:test'
import type { MarketConditions } from '../src/api/types'
import {
  ChangeDetector,
  MACRO_POLL_MS,
  MACRO_REFRESH_DEBOUNCE_MS,
  createDebouncer,
  macroKey,
  macroPollDue,
  needsMacroPoll,
} from '../src/components/chart/macroSync'

const NOW = Date.UTC(2026, 11, 9, 18, 30) // FOMC blackout starts (decision at 19:00 UTC)
const NO_WINDOW = { active: false, until: null, event: null }

function cond(level: MarketConditions['level'], blackout: MarketConditions['blackout'] = NO_WINDOW): MarketConditions {
  return {
    level,
    label: '',
    headline: '',
    advice: '',
    reasons: [],
    scope: 'crypto',
    symbol: 'BINANCE:BTCUSDT',
    blackout,
    next_event: null,
    events: [],
    metrics: [],
    updated_at: NOW,
    stale: false,
  }
}

const RED = cond('red', { active: true, until: NOW + 3_600_000, event: 'FOMC' })

test('a level change after the first answer triggers exactly one debounced NEXUS refresh', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    let refreshes = 0
    const debouncer = createDebouncer(() => refreshes++, MACRO_REFRESH_DEBOUNCE_MS)
    const changes = new ChangeDetector()
    const feed = (c: MarketConditions | null) => {
      if (changes.next(macroKey(c, NOW))) debouncer.trigger()
    }
    feed(null) // still loading
    feed(cond('green')) // first answer: the verdict on screen was computed with it
    mock.timers.tick(10_000)
    assert.equal(refreshes, 0)

    feed(RED) // 18:30 UTC: the blackout starts
    feed(RED) // the next poll returns the same state
    mock.timers.tick(MACRO_REFRESH_DEBOUNCE_MS - 1)
    assert.equal(refreshes, 0)
    mock.timers.tick(1)
    assert.equal(refreshes, 1)

    feed(cond('amber')) // window over: the verdict must change back
    mock.timers.tick(MACRO_REFRESH_DEBOUNCE_MS)
    assert.equal(refreshes, 2)
  } finally {
    mock.timers.reset()
  }
})

test('pushes that arrive together collapse into one refresh; cancel drops a pending one', () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    let refreshes = 0
    const debouncer = createDebouncer(() => refreshes++, MACRO_REFRESH_DEBOUNCE_MS)
    debouncer.trigger()
    mock.timers.tick(1000)
    debouncer.trigger()
    mock.timers.tick(MACRO_REFRESH_DEBOUNCE_MS)
    assert.equal(refreshes, 1)
    debouncer.trigger()
    assert.ok(debouncer.pending())
    debouncer.cancel()
    mock.timers.tick(10_000)
    assert.equal(refreshes, 1)
  } finally {
    mock.timers.reset()
  }
})

test('the end of the "no operar" window changes the key even before the level does', () => {
  const before = macroKey(RED, NOW)
  const after = macroKey(RED, NOW + 3_600_001)
  assert.notEqual(before, null)
  assert.notEqual(before, after)
  assert.equal(macroKey(null, NOW), null)
  assert.equal(macroKey(cond('green'), NOW), macroKey(cond('green'), NOW + 60_000))
  // a window extended by another event changes the verdict text; seconds of jitter in `until` do not
  const red = (until: number) => cond('red', { active: true, until, event: 'FOMC' })
  assert.notEqual(macroKey(red(NOW + 3_600_000), NOW), macroKey(red(NOW + 5_400_000), NOW))
  assert.equal(macroKey(red(NOW + 3_600_000), NOW), macroKey(red(NOW + 3_610_000), NOW))
})

test('change detector: unknown values are ignored and reset starts a new symbol', () => {
  const d = new ChangeDetector()
  assert.equal(d.next('green'), false)
  assert.equal(d.next(null), false)
  assert.equal(d.next('green'), false)
  assert.equal(d.next('red'), true)
  d.reset()
  assert.equal(d.next('green'), false) // first value of another symbol only seeds
})

test('polling only while not green, at most every 60 s, and only with a verdict on screen', () => {
  assert.equal(needsMacroPoll(cond('green'), NOW), false)
  assert.equal(needsMacroPoll(cond('amber'), NOW), true)
  assert.equal(needsMacroPoll(RED, NOW), true)
  assert.equal(needsMacroPoll(cond('green', { active: true, until: NOW + 60_000, event: 'CPI' }), NOW), true)
  assert.equal(needsMacroPoll(null, NOW), false)

  assert.equal(macroPollDue(NOW, null, null), false)
  assert.equal(macroPollDue(NOW, NOW - MACRO_POLL_MS + 1, null), false)
  assert.equal(macroPollDue(NOW, NOW - MACRO_POLL_MS, null), true)
  // a failed refresh does not make the next poll come sooner
  assert.equal(macroPollDue(NOW, NOW - 5 * MACRO_POLL_MS, NOW - 1000), false)
  assert.equal(macroPollDue(NOW, NOW - 5 * MACRO_POLL_MS, NOW - MACRO_POLL_MS), true)
})
