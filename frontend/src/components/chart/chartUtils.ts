// Chart-area utilities: color normalization, DST-safe time filtering, fast lookups, value formatting and a
// tiny event emitter used to push high-frequency updates (crosshair, live ticks) to small components
// without re-rendering the whole chart tree.
import type { Candle, ChartMarker, NexusBars } from '../../api/types'
import { isSessionTf, ts, type ChartTf } from '../../lib/chart'
import { fmtCompact } from '../../lib/format'

export type ParamValue = number | boolean | string
export type ParamMap = Record<string, ParamValue>

// ----------------------------------------------------------------------------- colors

const NAMED_COLORS: Record<string, string> = {
  red: '#ea3943',
  green: '#16c784',
  lime: '#84cc16',
  blue: '#2f6bff',
  navy: '#1e3a8a',
  orange: '#f5a524',
  yellow: '#facc15',
  gold: '#eab308',
  purple: '#a97bff',
  violet: '#8b5cf6',
  fuchsia: '#e879f9',
  pink: '#f472b6',
  aqua: '#22d3ee',
  cyan: '#22d3ee',
  teal: '#14b8a6',
  white: '#e5e7eb',
  silver: '#aab1c0',
  gray: '#7a8396',
  grey: '#7a8396',
  black: '#111827',
  maroon: '#9f1239',
  olive: '#a3a33a',
  brown: '#a16207',
}

/** Normalizes any CSS-like color to `#rrggbb` (alpha dropped) so alpha suffixes can be appended safely. */
export function safeColor(color: unknown, fallback = '#2f6bff'): string {
  if (typeof color !== 'string') return fallback
  const c = color.trim().toLowerCase()
  if (/^#[0-9a-f]{6}$/.test(c)) return c
  if (/^#[0-9a-f]{8}$/.test(c)) return c.slice(0, 7)
  if (/^#[0-9a-f]{3,4}$/.test(c)) {
    return `#${c
      .slice(1, 4)
      .split('')
      .map((h) => h + h)
      .join('')}`
  }
  const m = c.match(/^rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})/)
  if (m) {
    return `#${[m[1], m[2], m[3]]
      .map((v) => Math.min(255, Number(v)).toString(16).padStart(2, '0'))
      .join('')}`
  }
  return NAMED_COLORS[c] ?? fallback
}

/** `#rrggbb` + alpha (0..1) -> `#rrggbbaa`. */
export function withAlpha(color: string, alpha: number): string {
  const a = Math.round(Math.max(0, Math.min(1, alpha)) * 255)
    .toString(16)
    .padStart(2, '0')
  return `${safeColor(color)}${a}`
}

// ----------------------------------------------------------------------------- time ordering

// One cache per time mode: the same array maps to different chart times for intraday and 1d / 1w data.
const ascCache = new WeakMap<readonly number[], number[] | null>()
const ascCacheSession = new WeakMap<readonly number[], number[] | null>()

/**
 * Indices whose chart time (`ts(t, tf)`: shifted to the local clock unless 1d / 1w) strictly increases.
 * lightweight-charts throws on duplicated / unordered times, which can happen when the local offset changes
 * (DST) or when a provider sends a bad row. Returns `null` when every index is fine (fast path, no allocation).
 */
export function ascendingIndices(time: readonly number[], tf?: ChartTf): number[] | null {
  const cache = isSessionTf(tf) ? ascCacheSession : ascCache
  const cached = cache.get(time)
  if (cached !== undefined) return cached
  let prev = -Infinity
  let keep: number[] | null = null
  for (let i = 0; i < time.length; i++) {
    const t = time[i]
    const ct = typeof t === 'number' && Number.isFinite(t) ? (ts(t, tf) as number) : Number.NaN
    if (ct > prev) {
      prev = ct
      if (keep) keep.push(i)
    } else if (!keep) {
      keep = []
      for (let j = 0; j < i; j++) keep.push(j)
    }
  }
  cache.set(time, keep)
  return keep
}

export function pick<T>(arr: readonly T[], idx: number[] | null): T[] {
  return idx ? idx.map((i) => arr[i]) : (arr as T[])
}

function isCandle(c: unknown): c is Candle {
  if (!c || typeof c !== 'object') return false
  const k = c as Record<string, unknown>
  return ['time', 'open', 'high', 'low', 'close'].every((f) => typeof k[f] === 'number' && Number.isFinite(k[f] as number))
}

/** Valid candles in strictly increasing chart-time order (copy). */
export function cleanCandles(candles: readonly Candle[] | null | undefined, tf?: ChartTf): Candle[] {
  if (!Array.isArray(candles) || candles.length === 0) return []
  const valid = candles.filter(isCandle).map((c) => (Number.isFinite(c.volume) ? c : { ...c, volume: 0 }))
  return pick(valid, ascendingIndices(valid.map((c) => c.time), tf)).slice()
}

const BAR_KEYS = [
  'score',
  'prob_up',
  'ema_fast',
  'ema_slow',
  'ema_trend',
  'pos_entry',
  'pos_sl',
  'pos_tp1',
  'pos_tp2',
  'pos_tp3',
] as const

/** NexusBars with every numeric array present (null-filled when missing) and strictly ordered times. */
export function cleanBars(bars: NexusBars | null | undefined, tf?: ChartTf): NexusBars | null {
  if (!bars || !Array.isArray(bars.time) || bars.time.length === 0) return null
  const n = bars.time.length
  const idx = ascendingIndices(bars.time, tf)
  const complete =
    BAR_KEYS.every((k) => Array.isArray(bars[k]) && bars[k].length === n) &&
    Array.isArray(bars.action) &&
    Array.isArray(bars.regime)
  if (!idx && complete) return bars
  const num = (a: (number | null)[] | undefined): (number | null)[] => {
    const src = Array.isArray(a) ? a : []
    const full = src.length === n ? src : Array.from({ length: n }, (_, i) => (i < src.length ? src[i] : null))
    return pick(full, idx)
  }
  const str = (a: string[] | undefined): string[] => {
    const src = Array.isArray(a) ? a : []
    return pick(
      Array.from({ length: n }, (_, i) => (typeof src[i] === 'string' ? src[i] : '')),
      idx,
    )
  }
  return {
    time: pick(bars.time, idx),
    score: num(bars.score),
    prob_up: num(bars.prob_up),
    action: str(bars.action),
    regime: str(bars.regime),
    ema_fast: num(bars.ema_fast),
    ema_slow: num(bars.ema_slow),
    ema_trend: num(bars.ema_trend),
    pos_entry: num(bars.pos_entry),
    pos_sl: num(bars.pos_sl),
    pos_tp1: num(bars.pos_tp1),
    pos_tp2: num(bars.pos_tp2),
    pos_tp3: num(bars.pos_tp3),
  }
}

/** NEXUS bars from `t0` (the first candle on screen) on: the NEXUS window is anchored to its learning snapshots, so
 * it can start up to ~150 bars before the candles the chart loaded. Clean (ascending) bars expected. */
export function clipBarsFrom(bars: NexusBars | null, t0: number | null | undefined): NexusBars | null {
  if (!bars || !isFiniteNumber(t0)) return bars
  const start = bars.time.findIndex((t) => t >= t0)
  if (start <= 0) return bars // nothing before the candles (0), or no overlap at all (-1): leave it to the caller
  const out = { ...bars } as Record<string, unknown>
  for (const [key, value] of Object.entries(bars)) if (Array.isArray(value)) out[key] = value.slice(start)
  return out as unknown as NexusBars
}

// ----------------------------------------------------------------------------- lookups

/** Index of the last element `<= t` in an ascending array (-1 when none). */
export function indexAtOrBefore(times: readonly number[], t: number): number {
  let lo = 0
  let hi = times.length - 1
  let ans = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= t) {
      ans = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return ans
}

/** Index of the candle at (or right before) bar time `t`; null time = last candle; -1 when none. */
export function candleIndexAt(candles: readonly Candle[], t: number | null): number {
  if (candles.length === 0) return -1
  if (t === null) return candles.length - 1
  let lo = 0
  let hi = candles.length - 1
  let ans = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (candles[mid].time <= t) {
      ans = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return ans
}

/** Value of an aligned series at bar time `t` (null time = last bar). */
export function valueAt(times: readonly number[], values: readonly (number | null)[] | undefined, t: number | null): number | null {
  if (!values || times.length === 0) return null
  const i = t === null ? times.length - 1 : indexAtOrBefore(times, t)
  if (i < 0) return null
  const v = values[i]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

export function isRejectedMarker(m: ChartMarker): boolean {
  return typeof m.kind === 'string' && m.kind.startsWith('REJECTED')
}

const POSITIONS = new Set(['aboveBar', 'belowBar', 'inBar'])
const SHAPES = new Set(['arrowUp', 'arrowDown', 'circle', 'square'])

/** Drops malformed markers (they would break the markers plugin while it renders). */
export function validMarkers(markers: readonly ChartMarker[] | null | undefined): ChartMarker[] {
  if (!Array.isArray(markers)) return []
  return markers.filter(
    (m) =>
      m &&
      typeof m.time === 'number' &&
      Number.isFinite(m.time) &&
      POSITIONS.has(m.position) &&
      SHAPES.has(m.shape),
  )
}

/** Merges marker lists by (time, kind, position); later lists win. Result sorted by time. */
export function mergeMarkers(base: readonly ChartMarker[], extra: readonly ChartMarker[]): ChartMarker[] {
  const map = new Map<string, ChartMarker>()
  for (const m of [...base, ...extra]) map.set(`${m.time}|${m.kind}|${m.position}`, m)
  return Array.from(map.values()).sort((a, b) => a.time - b.time)
}

export function groupMarkersByTime(markers: readonly ChartMarker[]): Map<number, ChartMarker[]> {
  const out = new Map<number, ChartMarker[]>()
  for (const m of markers) {
    const list = out.get(m.time)
    if (list) list.push(m)
    else out.set(m.time, [m])
  }
  return out
}

// ----------------------------------------------------------------------------- numbers

/** Decimal places that keep an indicator readable given its magnitude. */
export function precisionFor(values: readonly (number | null)[] | undefined): number {
  if (!values) return 2
  let maxAbs = 0
  let count = 0
  for (let i = values.length - 1; i >= 0 && count < 400; i--) {
    const v = values[i]
    if (typeof v === 'number' && Number.isFinite(v)) {
      const a = Math.abs(v)
      if (a > maxAbs) maxAbs = a
      count++
    }
  }
  if (maxAbs === 0 || maxAbs >= 10) return 2
  if (maxAbs >= 1) return 3
  if (maxAbs >= 0.1) return 4
  if (maxAbs >= 0.01) return 5
  if (maxAbs >= 0.001) return 6
  return 8
}

/** Compact, magnitude-aware number formatting for indicator values. */
export function fmtAuto(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e6) return fmtCompact(v)
  const d = a >= 100 ? 2 : a >= 1 ? 3 : a === 0 ? 2 : a >= 0.01 ? 4 : 6
  return v.toLocaleString('es-ES', { minimumFractionDigits: d, maximumFractionDigits: d })
}

/** Accepts both conventions (0..1 or 0..100) for "rate" fields and returns a fraction. */
export function asFraction(v: number | null | undefined): number | null {
  if (typeof v !== 'number' || !Number.isFinite(v)) return null
  return Math.abs(v) > 1.0001 ? v / 100 : v
}

export function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** Lower-case text without accents, for forgiving searches ("estadistica" finds "Estadística"). */
export function foldText(s: string): string {
  return s
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
}

// ----------------------------------------------------------------------------- emitter

export interface Emitter<T> {
  emit: (value: T) => void
  on: (fn: (value: T) => void) => () => void
}

export function createEmitter<T>(): Emitter<T> {
  const listeners = new Set<(value: T) => void>()
  return {
    emit: (value) => {
      for (const fn of Array.from(listeners)) fn(value)
    },
    on: (fn) => {
      listeners.add(fn)
      return () => {
        listeners.delete(fn)
      }
    },
  }
}

export function isAbortError(err: unknown): boolean {
  return !!err && typeof err === 'object' && (err as { name?: unknown }).name === 'AbortError'
}
