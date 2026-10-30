// Small, defensive helpers for feeding lightweight-charts (used by the Studio preview, the strategy equity chart
// and the learning charts). lightweight-charts throws when data is not strictly ascending by time, so every
// series built from backend arrays goes through `ensureAscending` before `setData`.
import type { Time } from 'lightweight-charts'

const asNum = (t: Time): number => (typeof t === 'number' ? t : Number.NaN)

/** Sorts (only when needed) and removes duplicate / invalid times. For duplicates the last point wins. */
export function ensureAscending<T extends { time: Time }>(points: readonly T[]): T[] {
  let sorted = true
  for (let i = 1; i < points.length; i++) {
    if (asNum(points[i].time) < asNum(points[i - 1].time)) {
      sorted = false
      break
    }
  }
  const src = sorted ? points : [...points].sort((a, b) => asNum(a.time) - asNum(b.time))
  const out: T[] = []
  let last = Number.NEGATIVE_INFINITY
  for (const p of src) {
    const t = asNum(p.time)
    if (!Number.isFinite(t)) continue
    if (t > last) {
      out.push(p)
      last = t
    } else if (t === last) {
      out[out.length - 1] = p
    }
  }
  return out
}

/** Accepts unix seconds or milliseconds and returns unix seconds. */
export function toSec(t: number): number {
  return t > 1e11 ? Math.floor(t / 1000) : t
}

/** Accepts unix seconds or milliseconds and returns unix milliseconds. */
export function toMs(t: number | null | undefined): number | null {
  if (typeof t !== 'number' || !Number.isFinite(t)) return null
  return t < 1e11 ? t * 1000 : t
}

export const isFiniteNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

// ----------------------------------------------------------------------------- colors

interface Rgba {
  r: number
  g: number
  b: number
  a: number
}

const NAMED: Record<string, string> = {
  red: '#f23645',
  green: '#089981',
  blue: '#2962ff',
  orange: '#ff9800',
  yellow: '#ffeb3b',
  purple: '#9c27b0',
  fuchsia: '#e040fb',
  aqua: '#00bcd4',
  teal: '#00897b',
  lime: '#00e676',
  navy: '#311b92',
  maroon: '#880e4f',
  olive: '#808000',
  silver: '#b2b5be',
  gray: '#787b86',
  grey: '#787b86',
  white: '#ffffff',
  black: '#363a45',
}

function parseColor(input: string): Rgba | null {
  const c = (NAMED[input.trim().toLowerCase()] ?? input).trim()
  let m = /^#([0-9a-f]{3,8})$/i.exec(c)
  if (m) {
    let hex = m[1]
    if (hex.length === 3 || hex.length === 4) hex = hex.split('').map((ch) => ch + ch).join('')
    if (hex.length !== 6 && hex.length !== 8) return null
    const n = (i: number) => parseInt(hex.slice(i, i + 2), 16)
    return { r: n(0), g: n(2), b: n(4), a: hex.length === 8 ? n(6) / 255 : 1 }
  }
  m = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(c)
  if (m) {
    return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]), a: m[4] === undefined ? 1 : Number(m[4]) }
  }
  return null
}

/** Resolves named colors (Pine-like `red`, `green`…) to hex; unknown strings are returned unchanged. */
export function resolveColor(color: string | null | undefined, fallback: string): string {
  if (!color || typeof color !== 'string') return fallback
  const named = NAMED[color.trim().toLowerCase()]
  if (named) return named
  return parseColor(color) ? color : fallback
}

/** Returns the color with the given alpha (multiplied with any alpha it already has). */
export function withAlpha(color: string, alpha: number, fallback = 'rgba(47, 107, 255, 0.15)'): string {
  const c = parseColor(color)
  if (!c) return fallback
  const a = Math.max(0, Math.min(1, c.a * alpha))
  return `rgba(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)}, ${a.toFixed(3)})`
}

/** Background tint: keeps translucent colors as they are, softens opaque ones so candles stay readable. */
export function softBackground(color: string): string {
  const c = parseColor(color)
  if (!c) return 'rgba(47, 107, 255, 0.12)'
  return c.a < 0.6 ? withAlpha(color, 1) : withAlpha(color, 0.16)
}

/** Picks a readable step-size for numeric inputs when the backend gives none. */
export function defaultStep(type: 'int' | 'float', value: unknown): number {
  if (type === 'int') return 1
  const v = typeof value === 'number' ? Math.abs(value) : 1
  if (v === 0) return 0.1
  if (v < 0.01) return 0.0001
  if (v < 1) return 0.01
  if (v < 100) return 0.1
  return 1
}
