// Regression (indicator-list-wiped): when the first read of "ui.chart_indicators" failed, the list was treated as
// empty and marked loaded, so adding one indicator saved [that one] over the user's whole saved list. Now a failed
// read is retried (backoff / reconnect / "Reintentar") and edits are only saved after a successful read, merged
// with the saved list.
import assert from 'node:assert/strict'
import { mock, test } from 'node:test'
import {
  listRetryDelay,
  mergeStoredList,
  startListLoader,
  type ActiveIndicator,
} from '../src/components/chart/indicatorList'

const flush = () => new Promise<void>((resolve) => setImmediate(resolve))

const SAVED = ['ema', 'sma', 'bb', 'vwap', 'macd', 'atr'].map((id, i) => ({ uid: `u${i}`, id, params: { length: 14 } }))

test('a failed read never becomes an empty list, and edits are only saved merged after a real read', async () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    // State kept by useIndicators: the debounced save only runs when `loaded && dirty`.
    let active: ActiveIndicator[] = []
    let loaded = false
    let dirty = false
    const saves: ActiveIndicator[][] = []
    const persist = () => {
      if (loaded && dirty) saves.push(active)
    }
    let serverUp = false
    let reads = 0
    const errors: number[] = []
    const loader = startListLoader({
      get: () => {
        reads++
        return serverUp ? Promise.resolve(SAVED) : Promise.reject(new Error('Failed to fetch'))
      },
      onLoaded: (stored) => {
        active = mergeStoredList(stored, active, dirty)
        loaded = true
        persist()
      },
      onError: (_err, wait) => errors.push(wait),
    })
    await flush()
    assert.equal(loaded, false)
    assert.deepEqual(errors, [2000])

    // The user, seeing no indicators, adds RSI: kept on screen, not saved.
    dirty = true
    active = [...active, { uid: 'new', id: 'rsi', params: { length: 14 } }]
    persist()
    assert.equal(saves.length, 0)

    mock.timers.tick(2000) // automatic retry, server still down
    await flush()
    assert.deepEqual(errors, [2000, 4000])
    assert.equal(saves.length, 0)

    serverUp = true
    mock.timers.tick(4000)
    await flush()
    assert.equal(loaded, true)
    assert.equal(reads, 3)
    assert.equal(saves.length, 1)
    assert.deepEqual(
      saves[0].map((a) => a.id),
      ['ema', 'sma', 'bb', 'vwap', 'macd', 'atr', 'rsi'],
    )
    loader.stop()
  } finally {
    mock.timers.reset()
  }
})

test('retryNow (reconnect / button) reads at once, restarts the backoff and supersedes a pending read', async () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const pending: { resolve: (v: unknown) => void; reject: (e: unknown) => void }[] = []
    const loadedLists: ActiveIndicator[][] = []
    const errors: number[] = []
    const loader = startListLoader({
      get: () => new Promise((resolve, reject) => pending.push({ resolve, reject })),
      onLoaded: (l) => loadedLists.push(l),
      onError: (_e, wait) => errors.push(wait),
    })
    pending[0].reject(new Error('503'))
    await flush()
    mock.timers.tick(2000)
    pending[1].reject(new Error('503'))
    await flush()
    assert.deepEqual(errors, [2000, 4000])

    loader.retryNow() // e.g. the socket is open again
    assert.equal(pending.length, 3)
    pending[2].reject(new Error('503'))
    await flush()
    assert.deepEqual(errors, [2000, 4000, 2000]) // backoff restarted

    loader.retryNow()
    loader.retryNow() // a newer attempt wins: the older answer is ignored
    pending[3].resolve([{ uid: 'old', id: 'ema', params: {} }])
    pending[4].resolve(SAVED)
    await flush()
    assert.equal(loadedLists.length, 1)
    assert.equal(loadedLists[0].length, SAVED.length)

    loader.retryNow() // no-op once loaded
    assert.equal(pending.length, 5)
  } finally {
    mock.timers.reset()
  }
})

test('stop() silences answers still in flight and pending retries', async () => {
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    let reads = 0
    const seen: string[] = []
    const loader = startListLoader({
      get: () => {
        reads++
        return Promise.reject(new Error('down'))
      },
      onLoaded: () => seen.push('loaded'),
      onError: () => seen.push('error'),
    })
    loader.stop()
    await flush()
    mock.timers.tick(120_000)
    await flush()
    assert.equal(reads, 1)
    assert.deepEqual(seen, [])
  } finally {
    mock.timers.reset()
  }
})

test('a user who never saved a list gets an empty list (that is a real answer)', async () => {
  const lists: ActiveIndicator[][] = []
  startListLoader({ get: () => Promise.resolve(undefined), onLoaded: (l) => lists.push(l), onError: () => undefined })
  await flush()
  assert.deepEqual(lists, [[]])
})

test('retry delays grow and are capped at 60 s', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 50].map(listRetryDelay), [2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000])
})

test('merge keeps the saved list first and the local edits, without duplicates', () => {
  const local = [{ uid: 'u1', id: 'sma', params: { length: 50 } }, { uid: 'x', id: 'rsi', params: {} }]
  assert.deepEqual(
    mergeStoredList(SAVED, local, true).map((a) => a.uid),
    ['u0', 'u2', 'u3', 'u4', 'u5', 'u1', 'x'],
  )
  assert.equal(mergeStoredList(SAVED, local, false), SAVED)
})
