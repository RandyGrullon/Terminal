// Alert preferences of this browser and their side effects:
// - "Sonido en alertas rojas" (localStorage 'nexus.macro.sound', default on) and the short two-tone WebAudio beep
//   played for red market alerts;
// - desktop notifications (localStorage 'nexus.alerts.desktop', default OFF): macro red alerts and NEXUS signals
//   shown with the Notification API while the tab is in the background. The browser permission is requested only
//   from a user click (enableDesktop) and the choice is remembered per browser.
// Every browser API access is guarded: storage may be unavailable, audio may be blocked until the user has
// interacted with the page and some browsers have no Notification API (or only through a service worker).
import { useEffect, useState, useSyncExternalStore } from 'react'

const STORAGE_KEY = 'nexus.macro.sound'
export const DESKTOP_STORAGE_KEY = 'nexus.alerts.desktop'

function readFlag(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    if (v === null) return fallback
    if (v === '0' || v === 'false' || v === 'off') return false
    if (v === '1' || v === 'true' || v === 'on') return true
    return fallback
  } catch {
    return fallback
  }
}

function writeFlag(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? '1' : '0')
  } catch {
    /* storage unavailable: keep the in-memory value */
  }
}

let enabled = readFlag(STORAGE_KEY, true)
let desktopOn = readFlag(DESKTOP_STORAGE_KEY, false)
const listeners = new Set<() => void>()

function emit(): void {
  for (const fn of Array.from(listeners)) fn()
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

try {
  // Keep several open tabs in sync.
  window.addEventListener('storage', (e) => {
    if (e.key === STORAGE_KEY) {
      const next = readFlag(STORAGE_KEY, true)
      if (next !== enabled) {
        enabled = next
        emit()
      }
    } else if (e.key === DESKTOP_STORAGE_KEY) {
      const next = readFlag(DESKTOP_STORAGE_KEY, false)
      if (next !== desktopOn) {
        desktopOn = next
        emit()
      }
    }
  })
} catch {
  /* no window events (non-browser environment): ignore */
}

export function soundEnabled(): boolean {
  return enabled
}

export function setSoundEnabled(value: boolean): void {
  enabled = value
  writeFlag(STORAGE_KEY, value)
  emit()
}

/** React binding of the preference: [enabled, setEnabled]. */
export function useSoundPref(): [boolean, (value: boolean) => void] {
  const value = useSyncExternalStore(subscribe, soundEnabled, soundEnabled)
  return [value, setSoundEnabled]
}

type AudioCtor = typeof AudioContext
let ctx: AudioContext | null = null

function audioContext(): AudioContext | null {
  try {
    if (!ctx) {
      const Ctor: AudioCtor | undefined =
        window.AudioContext ?? (window as unknown as { webkitAudioContext?: AudioCtor }).webkitAudioContext
      if (!Ctor) return null
      ctx = new Ctor()
    }
    if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined)
    return ctx
  } catch {
    return null
  }
}

function tone(c: AudioContext, freq: number, start: number, duration: number, volume: number): void {
  const osc = c.createOscillator()
  const gain = c.createGain()
  osc.type = 'sine'
  osc.frequency.setValueAtTime(freq, start)
  // Short attack/release envelope: no clicks, gentle volume.
  gain.gain.setValueAtTime(0.0001, start)
  gain.gain.exponentialRampToValueAtTime(volume, start + 0.015)
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration)
  osc.connect(gain)
  gain.connect(c.destination)
  osc.onended = () => {
    osc.disconnect()
    gain.disconnect()
  }
  osc.start(start)
  osc.stop(start + duration + 0.02)
}

/** Short descending two-tone beep (≈ 0.4 s). Silently does nothing when audio is unavailable or blocked. */
export function playAlertBeep(): void {
  const c = audioContext()
  if (!c) return
  try {
    const t0 = c.currentTime + 0.03
    tone(c, 880, t0, 0.15, 0.12)
    tone(c, 587.33, t0 + 0.19, 0.22, 0.12)
  } catch {
    /* audio graph unavailable: ignore */
  }
}

/** Beep for a red alert, only when the viewer has the sound preference on. */
export function beepIfEnabled(): void {
  if (enabled) playAlertBeep()
}

// ----------------------------------------------------------------------------- desktop notifications

export type DesktopPermission = 'unsupported' | 'default' | 'granted' | 'denied'

export interface DesktopNotice {
  title: string
  body: string
  /** Same tag = same notification: several open tabs (or a repeated push) show it once. */
  tag: string
  onClick?: () => void
}

/** The same tag is not shown twice within this window. */
export const DESKTOP_DEDUP_MS = 60_000
/** At most `max` separate notifications per window; the rest update one summary notification (tag BURST_TAG), so
 *  several watched markets closing at the same time do not flood the desktop. */
export const DESKTOP_BURST = { max: 3, windowMs: 15_000 }
export const BURST_TAG = 'nexus-burst'
const shown = new Map<string, number>()
const recent: number[] = []
let burstExtra = 0

function notificationApi(): typeof Notification | null {
  try {
    const N = (globalThis as { Notification?: typeof Notification }).Notification
    return typeof N === 'function' ? N : null
  } catch {
    return null
  }
}

function asPermission(p: unknown): DesktopPermission {
  return p === 'granted' || p === 'denied' ? p : 'default'
}

export function desktopPermission(): DesktopPermission {
  const N = notificationApi()
  if (!N) return 'unsupported'
  try {
    return asPermission(N.permission)
  } catch {
    return 'unsupported'
  }
}

/** The viewer asked for desktop notifications (they still need the browser permission). */
export function desktopWanted(): boolean {
  return desktopOn
}

/** Notifications will be shown: wanted and allowed by the browser. */
export function desktopEnabled(): boolean {
  return desktopOn && desktopPermission() === 'granted'
}

function setDesktop(value: boolean): void {
  desktopOn = value
  writeFlag(DESKTOP_STORAGE_KEY, value)
  emit()
}

/** Turn desktop notifications on. Asks the browser's permission when it was never asked: call it from a click. */
export async function enableDesktop(): Promise<DesktopPermission> {
  const N = notificationApi()
  if (!N) return 'unsupported'
  let p = desktopPermission()
  if (p === 'default') {
    try {
      p = asPermission(await N.requestPermission())
    } catch {
      p = desktopPermission()
    }
  }
  setDesktop(p === 'granted')
  return p
}

export function disableDesktop(): void {
  setDesktop(false)
}

/** The tab is hidden or the window does not have the focus: the viewer would miss a toast. */
export function inBackground(): boolean {
  try {
    if (typeof document === 'undefined') return false
    if (document.visibilityState === 'hidden' || document.hidden) return true
    return typeof document.hasFocus === 'function' ? !document.hasFocus() : false
  } catch {
    return false
  }
}

/** Show `n` as a desktop notification when the viewer enabled them, the browser allows them and the tab is in the
 *  background (`force`: also in the foreground, for the "probar" button; the permission is still required).
 *  Returns true when one was shown. Never throws. */
export function notifyDesktop(n: DesktopNotice, opts: { force?: boolean; now?: number } = {}): boolean {
  if (desktopPermission() !== 'granted') return false
  if (!opts.force && (!desktopOn || !inBackground())) return false
  const N = notificationApi()
  if (!N) return false
  const now = opts.now ?? Date.now()
  for (const [tag, ts] of Array.from(shown)) {
    if (now - ts >= DESKTOP_DEDUP_MS) shown.delete(tag)
  }
  if (!opts.force && shown.has(n.tag)) return false
  let { title, body, tag } = n
  let burst = false
  if (!opts.force) {
    while (recent.length && now - recent[0] >= DESKTOP_BURST.windowMs) recent.shift()
    if (!recent.length) burstExtra = 0
    if (recent.length >= DESKTOP_BURST.max) {
      // a burst: one summary notification, replaced (same tag) by the next one of the burst
      burst = true
      title = `NEXUS · ${burstExtra + 1} ${burstExtra === 0 ? 'aviso más' : 'avisos más'}`
      body = `${n.title}: ${n.body.split('\n')[0]}`
      tag = BURST_TAG
    }
  }
  try {
    const notification = new N(title, { body, tag })
    shown.set(n.tag, now)
    if (burst) burstExtra += 1
    else if (!opts.force) {
      recent.push(now)
      burstExtra = 0 // shown on its own: the burst is over, the next summary counts from 1 again
    }
    notification.onclick = () => {
      try {
        window.focus()
      } catch {
        /* ignore */
      }
      try {
        n.onClick?.()
      } finally {
        notification.close()
      }
    }
    return true
  } catch {
    return false // e.g. Chrome on Android: notifications only through a service worker
  }
}

/** React binding: the viewer's choice and the browser permission (re-read when the window regains the focus,
 *  since the user may change it in the site settings). */
export function useDesktopPref(): { wanted: boolean; permission: DesktopPermission; enable: () => Promise<DesktopPermission>; disable: () => void } {
  const wanted = useSyncExternalStore(subscribe, desktopWanted, desktopWanted)
  const [permission, setPermission] = useState<DesktopPermission>(desktopPermission)
  useEffect(() => {
    const sync = () => setPermission(desktopPermission())
    sync()
    const off = subscribe(sync)
    try {
      window.addEventListener('focus', sync)
    } catch {
      /* ignore */
    }
    return () => {
      off()
      try {
        window.removeEventListener('focus', sync)
      } catch {
        /* ignore */
      }
    }
  }, [])
  return { wanted, permission, enable: enableDesktop, disable: disableDesktop }
}
