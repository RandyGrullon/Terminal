// "Sonido en alertas rojas" preference (per viewer, localStorage 'nexus.macro.sound', default on) and the short
// two-tone WebAudio beep played for red market alerts. Every browser API access is guarded: storage may be
// unavailable and audio may be blocked until the user has interacted with the page.
import { useSyncExternalStore } from 'react'

const STORAGE_KEY = 'nexus.macro.sound'

function readPref(): boolean {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return !(v === '0' || v === 'false' || v === 'off')
  } catch {
    return true
  }
}

let enabled = readPref()
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
    if (e.key !== STORAGE_KEY) return
    const next = readPref()
    if (next !== enabled) {
      enabled = next
      emit()
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
  try {
    localStorage.setItem(STORAGE_KEY, value ? '1' : '0')
  } catch {
    /* storage unavailable: keep the in-memory value */
  }
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
