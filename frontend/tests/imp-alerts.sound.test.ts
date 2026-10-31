// Desktop notifications preference (per browser) and display rules of components/macro/sound.ts: off by default,
// the permission is requested only through enableDesktop (a click), shown only with the tab in the background,
// deduplicated by tag, never throwing; the existing sound preference keeps working.
import { FakeNotification, dispatchWindow, doc, focusCalls, storage, store, withoutNotificationApi } from './imp-alerts.env'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  BURST_TAG,
  DESKTOP_BURST,
  DESKTOP_DEDUP_MS,
  DESKTOP_STORAGE_KEY,
  desktopEnabled,
  desktopPermission,
  desktopWanted,
  disableDesktop,
  enableDesktop,
  inBackground,
  notifyDesktop,
  setSoundEnabled,
  soundEnabled,
} from '../src/components/macro/sound'

const notice = (tag: string) => ({ title: 'NEXUS · Semáforo en rojo', body: '🔴 FOMC: se publica ahora.', tag })

test('off by default; nothing is shown and no permission is asked on load', () => {
  assert.equal(desktopWanted(), false)
  assert.equal(desktopEnabled(), false)
  assert.equal(desktopPermission(), 'default')
  doc.hidden = true
  assert.equal(notifyDesktop(notice('a')), false)
  assert.equal(FakeNotification.requests, 0)
  assert.equal(FakeNotification.all.length, 0)
  doc.hidden = false
})

test('enabling asks the browser once and remembers the choice per browser', async () => {
  FakeNotification.answer = 'denied'
  assert.equal(await enableDesktop(), 'denied')
  assert.equal(desktopWanted(), false)
  assert.equal(store.get(DESKTOP_STORAGE_KEY), '0')
  assert.equal(FakeNotification.requests, 1)
  assert.equal(await enableDesktop(), 'denied') // denied: the browser is not asked again
  assert.equal(FakeNotification.requests, 1)
  FakeNotification.permission = 'default'
  FakeNotification.answer = 'granted'
  assert.equal(await enableDesktop(), 'granted')
  assert.equal(desktopEnabled(), true)
  assert.equal(store.get(DESKTOP_STORAGE_KEY), '1')
  assert.equal(await enableDesktop(), 'granted')
  assert.equal(FakeNotification.requests, 2) // already granted: no new prompt
})

test('shown only in the background, once per tag, and a click focuses the tab', () => {
  FakeNotification.reset()
  doc.hidden = false
  doc.focused = true
  assert.equal(inBackground(), false)
  assert.equal(notifyDesktop(notice('fomc'), { now: 1_000 }), false) // the viewer is looking: the toast is enough
  doc.focused = false
  assert.equal(inBackground(), true)
  let clicked = 0
  assert.equal(notifyDesktop({ ...notice('fomc'), onClick: () => clicked++ }, { now: 2_000 }), true)
  assert.equal(notifyDesktop(notice('fomc'), { now: 3_000 }), false) // same tag within the dedupe window
  assert.equal(notifyDesktop(notice('fomc'), { now: 2_000 + DESKTOP_DEDUP_MS }), true)
  assert.equal(FakeNotification.all.length, 2)
  const first = FakeNotification.all[0]
  assert.equal(first.title, 'NEXUS · Semáforo en rojo')
  assert.deepEqual(first.options, { body: '🔴 FOMC: se publica ahora.', tag: 'fomc' })
  first.onclick?.()
  assert.equal(clicked, 1)
  assert.equal(first.closed, true)
  assert.ok(focusCalls.length >= 1)
  doc.focused = true
})

test('force shows it in the foreground but still needs the permission; errors never throw', () => {
  FakeNotification.reset()
  assert.equal(notifyDesktop(notice('test-1'), { force: true }), true)
  FakeNotification.throwOnCreate = true // Chrome on Android: only through a service worker
  doc.hidden = true
  assert.equal(notifyDesktop(notice('test-2')), false)
  FakeNotification.throwOnCreate = false
  FakeNotification.permission = 'denied' // revoked in the site settings
  assert.equal(desktopEnabled(), false)
  assert.equal(notifyDesktop(notice('test-3'), { force: true }), false)
  FakeNotification.permission = 'granted'
  assert.equal(withoutNotificationApi(() => desktopPermission()), 'unsupported')
  assert.equal(withoutNotificationApi(() => notifyDesktop(notice('test-4'), { force: true })), false)
  doc.hidden = false
})

test('unsupported browsers and broken storage degrade quietly', async () => {
  assert.equal(await withoutNotificationApi(() => enableDesktop()), 'unsupported')
  storage.broken = true
  disableDesktop() // storage throws: the in-memory value still changes
  assert.equal(desktopWanted(), false)
  assert.equal(await enableDesktop(), 'granted')
  assert.equal(desktopWanted(), true)
  storage.broken = false
})

test('another tab turning them off is followed here; the sound preference is untouched', () => {
  assert.equal(soundEnabled(), true)
  store.set(DESKTOP_STORAGE_KEY, '0')
  dispatchWindow('storage', { key: DESKTOP_STORAGE_KEY })
  assert.equal(desktopWanted(), false)
  assert.equal(soundEnabled(), true)
  setSoundEnabled(false)
  assert.equal(store.get('nexus.macro.sound'), '0')
  assert.equal(soundEnabled(), false)
  store.set('nexus.macro.sound', '1')
  dispatchWindow('storage', { key: 'nexus.macro.sound' })
  assert.equal(soundEnabled(), true)
})

test('a burst is folded into one summary notification instead of flooding the desktop', async () => {
  FakeNotification.reset()
  assert.equal(await enableDesktop(), 'granted')
  doc.hidden = true
  const t0 = 10_000_000
  for (let i = 0; i < DESKTOP_BURST.max; i++) assert.equal(notifyDesktop(notice(`burst-${i}`), { now: t0 + i }), true)
  assert.equal(notifyDesktop({ ...notice('burst-x'), title: 'NEXUS · Señal de compra', body: 'ETH 1h → COMPRAR\nNo es asesoría' }, { now: t0 + 10 }), true)
  assert.equal(notifyDesktop(notice('burst-y'), { now: t0 + 20 }), true)
  const all = FakeNotification.all
  assert.equal(all.length, DESKTOP_BURST.max + 2)
  assert.deepEqual(all.slice(0, DESKTOP_BURST.max).map((n) => n.options.tag), ['burst-0', 'burst-1', 'burst-2'])
  assert.equal(all[DESKTOP_BURST.max].title, 'NEXUS · 1 aviso más')
  assert.equal(all[DESKTOP_BURST.max].options.body, 'NEXUS · Señal de compra: ETH 1h → COMPRAR')
  assert.equal(all[DESKTOP_BURST.max + 1].title, 'NEXUS · 2 avisos más') // same tag: the browser replaces it
  assert.equal(all[DESKTOP_BURST.max + 1].options.tag, BURST_TAG)
  assert.equal(notifyDesktop(notice('burst-y'), { now: t0 + 30 }), false) // still deduplicated by its own tag
  assert.equal(notifyDesktop(notice('after'), { now: t0 + DESKTOP_BURST.windowMs + 50 }), true) // window over
  assert.equal(FakeNotification.all.at(-1)?.options.tag, 'after')
  doc.hidden = false
})

test('the summary of a new burst counts from 1 again (no count carried over from an earlier burst)', async () => {
  FakeNotification.reset()
  assert.equal(await enableDesktop(), 'granted')
  doc.hidden = true
  const t0 = 50_000_000
  for (let i = 0; i < DESKTOP_BURST.max; i++) notifyDesktop(notice(`first-${i}`), { now: t0 + i })
  notifyDesktop(notice('first-x'), { now: t0 + 10 })
  notifyDesktop(notice('first-y'), { now: t0 + 20 })
  assert.equal(FakeNotification.all.at(-1)?.title, 'NEXUS · 2 avisos más')
  // it calms down (the oldest leave the window, one is shown on its own), then a new burst starts
  const t1 = t0 + DESKTOP_BURST.windowMs + 1
  for (let i = 0; i < DESKTOP_BURST.max; i++) {
    assert.equal(notifyDesktop(notice(`second-${i}`), { now: t1 + i }), true)
    assert.equal(FakeNotification.all.at(-1)?.options.tag, `second-${i}`) // shown on its own
  }
  assert.equal(notifyDesktop(notice('second-x'), { now: t1 + 10 }), true)
  assert.equal(FakeNotification.all.at(-1)?.title, 'NEXUS · 1 aviso más')
  assert.equal(FakeNotification.all.at(-1)?.options.tag, BURST_TAG)
  doc.hidden = false
})
