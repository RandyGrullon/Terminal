// Pure helpers for the market-conditions widgets ("semáforo de mercado"): defensive normalization of backend
// payloads (the widgets live in the app header and must never crash on a partial or malformed response),
// level metadata and event selection. No React here.
import type { MacroAlert, MacroEvent, MacroLevel, MacroMetric, MarketConditions } from '../../api/types'
import { fmtNum, type Tone } from '../../lib/format'

export const LEVELS: readonly MacroLevel[] = ['green', 'amber', 'red']

export interface LevelMeta {
  /** Pill text: "Operar" | "Precaución" | "No operar". */
  short: string
  /** Long label used when the backend does not send one. */
  label: string
  tone: Tone
}

export const LEVEL_META: Record<MacroLevel, LevelMeta> = {
  green: { short: 'Operar', label: 'Operar con normalidad', tone: 'pos' },
  amber: { short: 'Precaución', label: 'Precaución', tone: 'warn' },
  red: { short: 'No operar', label: 'No operar ahora', tone: 'neg' },
}

export const IMPACT_LABEL: Record<MacroEvent['impact'], string> = {
  high: 'Impacto alto',
  medium: 'Impacto medio',
  low: 'Impacto bajo',
  holiday: 'Festivo',
}

export const METRIC_TONE: Record<MacroMetric['level'], Tone> = { ok: 'pos', warn: 'warn', danger: 'neg' }

const IMPACTS: readonly MacroEvent['impact'][] = ['high', 'medium', 'low', 'holiday']
const METRIC_LEVELS: readonly MacroMetric['level'][] = ['ok', 'warn', 'danger']
const ALERT_KINDS: readonly MacroAlert['kind'][] = ['level', 'event_soon', 'event_now', 'blackout_end', 'volatility']

type Obj = Record<string, unknown>

const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v.trim() || fallback : fallback)
const optStr = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v.trim() : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const isPresent = <T>(v: T | null): v is T => v !== null

function oneOf<T extends string>(v: unknown, options: readonly T[]): T | null {
  if (typeof v !== 'string') return null
  const s = v.trim().toLowerCase()
  return (options as readonly string[]).includes(s) ? (s as T) : null
}

function idOf(v: unknown): string {
  if (typeof v === 'string') return v.trim()
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  return ''
}

/** Unix time in milliseconds from a value that may be expressed in seconds or in milliseconds. */
export function toMs(v: number): number {
  return v > 1e11 ? v : v * 1000
}

/** Unix time in seconds from a value that may be expressed in seconds or in milliseconds. */
export function toSec(v: number): number {
  return v > 1e11 ? v / 1000 : v
}

export function normalizeEvent(raw: unknown): MacroEvent | null {
  if (!isObj(raw)) return null
  const time = num(raw.time)
  if (time === null) return null
  const title = str(raw.title)
  const titleEs = str(raw.title_es, title)
  if (!titleEs) return null
  const t = toSec(time)
  const currency = str(raw.currency).toUpperCase()
  return {
    id: idOf(raw.id) || `${currency}-${t}-${titleEs}`,
    title: title || titleEs,
    title_es: titleEs,
    currency,
    time: t,
    impact: oneOf(raw.impact, IMPACTS) ?? 'low',
    forecast: optStr(raw.forecast),
    previous: optStr(raw.previous),
    source: str(raw.source),
    countdown_s: num(raw.countdown_s) ?? 0,
  }
}

export function normalizeMetric(raw: unknown): MacroMetric | null {
  if (!isObj(raw)) return null
  const label = str(raw.label)
  if (!label) return null
  const value = num(raw.value)
  return {
    id: idOf(raw.id) || label,
    label,
    value,
    display: str(raw.display, value === null ? '—' : fmtNum(value)),
    change_pct: num(raw.change_pct),
    change_display: str(raw.change_display),
    level: oneOf(raw.level, METRIC_LEVELS) ?? 'ok',
    note: str(raw.note),
  }
}

/**
 * Validate and normalize a `MarketConditions` payload. Returns null when it is unusable (no valid level).
 * Normalized units: `blackout.until` and `updated_at` are unix **milliseconds**, event `time` unix seconds.
 */
export function normalizeConditions(raw: unknown): MarketConditions | null {
  if (!isObj(raw)) return null
  const level = oneOf(raw.level, LEVELS)
  if (!level) return null
  const label = str(raw.label, LEVEL_META[level].label)
  const b = isObj(raw.blackout) ? raw.blackout : {}
  const until = num(b.until)
  const updated = num(raw.updated_at)
  const list = <T>(v: unknown, fn: (x: unknown) => T | null): T[] => (Array.isArray(v) ? v.map(fn).filter(isPresent) : [])
  return {
    level,
    label,
    headline: str(raw.headline, label),
    advice: str(raw.advice),
    reasons: list(raw.reasons, optStr).slice(0, 3),
    scope: str(raw.scope, 'global'),
    symbol: optStr(raw.symbol),
    blackout: { active: b.active === true, until: until === null ? null : toMs(until), event: optStr(b.event) },
    next_event: normalizeEvent(raw.next_event),
    events: list(raw.events, normalizeEvent).sort((a, c) => a.time - c.time),
    metrics: list(raw.metrics, normalizeMetric).slice(0, 6),
    updated_at: updated === null ? Date.now() : toMs(updated),
    stale: raw.stale === true,
  }
}

export function normalizeAlert(raw: unknown): MacroAlert | null {
  if (!isObj(raw)) return null
  const level = oneOf(raw.level, LEVELS)
  const message = str(raw.message)
  if (!level || !message) return null
  return { kind: oneOf(raw.kind, ALERT_KINDS) ?? 'level', level, message }
}

export function normalizeEvents(raw: unknown): MacroEvent[] {
  const arr = isObj(raw) && Array.isArray(raw.events) ? raw.events : Array.isArray(raw) ? raw : []
  return arr
    .map(normalizeEvent)
    .filter(isPresent)
    .sort((a, b) => a.time - b.time)
}

/** True while the "no operar" window is running (client-side check against `until`, in ms). */
export function blackoutActive(c: MarketConditions, nowMs: number): boolean {
  return c.blackout.active && (c.blackout.until === null || c.blackout.until > nowMs)
}

/** The high-impact event to count down to: the snapshot's `next_event` until it is more than a minute old,
 *  then the next future high-impact event of the list (countdowns are recomputed from `time`). */
export function pickNextEvent(c: MarketConditions, nowMs: number): MacroEvent | null {
  const nowS = nowMs / 1000
  if (c.next_event && c.next_event.time > nowS - 60) return c.next_event
  return c.events.find((e) => e.impact === 'high' && e.time > nowS) ?? null
}

/** High-impact events for the panel: upcoming ones plus those of the last hour (they explain a lingering
 *  caution level), sorted by time, at most `max`. */
export function highImpactEvents(c: MarketConditions, nowMs: number, max = 5): MacroEvent[] {
  const minS = nowMs / 1000 - 3600
  const all = [...c.events]
  const next = c.next_event
  if (next && !all.some((e) => e.id === next.id)) all.push(next)
  return all
    .filter((e) => e.impact === 'high' && e.time >= minS)
    .sort((a, b) => a.time - b.time)
    .slice(0, max)
}
