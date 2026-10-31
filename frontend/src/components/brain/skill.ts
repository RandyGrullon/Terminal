// Honest skill of the verified predictions versus the baseline (GET /api/learning/skill → app.sim.evaluator
// .skill_report). A prediction has three outcomes (up, down, neutral = the move stayed inside the flat band), so 50 %
// is NOT the chance level: the reference is always predicting the most frequent outcome of the same rows, "chance"
// is the hit rate of the same mix of calls if they were unrelated to the outcomes, and the Brier score is compared
// with climatology. Intervals are clustered by independent horizon windows (and count the correlation between
// neighbouring ones); nothing is concluded below 30 of them.
import { useEffect, useRef, useState } from 'react'
import { ApiError, request } from '../../api/client'
import type {
  CalibrationBin,
  SkillVerdict,
  OutcomeKind,
  SkillReference,
  SkillDirection,
  SkillDaily,
  SkillGroup,
  SkillReport,
} from '../../api/types'
import { fmtInt, fmtNum, fmtProb, type Tone } from '../../lib/format'

export type { SkillVerdict, OutcomeKind, SkillReference, SkillDirection, SkillDaily, SkillGroup, SkillReport }

export const SKILL_PATH = '/api/learning/skill'
export const MIN_WINDOWS = 30
export type SkillWindow = 'all' | '7' | '1'
export const SKILL_WINDOWS: { value: SkillWindow; label: string }[] = [
  { value: '7', label: 'Últimos 7 días' },
  { value: '1', label: 'Últimas 24 h' },
  { value: 'all', label: 'Todo lo guardado' },
]

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const num = (v: unknown): number | null => (isNum(v) ? v : null)
const arr = <T,>(v: T[] | null | undefined): T[] => (Array.isArray(v) ? v : [])
const pair = (v: unknown): [number, number] | null =>
  Array.isArray(v) && v.length === 2 && isNum(v[0]) && isNum(v[1]) ? [v[0], v[1]] : null

export function windowDays(w: SkillWindow): number | null {
  return w === 'all' ? null : Number(w)
}

export function periodLabel(days: number | null | undefined): string {
  if (!isNum(days) || days <= 0) return 'filas guardadas'
  if (days < 1.5) return 'últimas 24 h'
  return `últimos ${fmtInt(days)} días`
}

/** Defensive copy of a server report (missing arrays, wrong types). */
export function normalizeSkill(r: SkillReport | null | undefined): SkillReport | null {
  if (!r || typeof r !== 'object') return null
  const groups = arr(r.groups)
    .filter((g) => g && typeof g.id === 'string')
    .map((g) => ({
      ...g,
      n: isNum(g.n) ? g.n : 0,
      hits: isNum(g.hits) ? g.hits : 0,
      windows: isNum(g.windows) ? g.windows : 0,
      accuracy: num(g.accuracy),
      chance: num(g.chance),
      skill_pp: num(g.skill_pp),
      skill_ci: pair(g.skill_ci),
      brier: num(g.brier),
      brier_ref: num(g.brier_ref),
      bss: num(g.bss),
      bss_ci: pair(g.bss_ci),
      ece_pp: num(g.ece_pp),
      reference: g.reference && isNum(g.reference.accuracy) ? g.reference : null,
      by_direction: arr(g.by_direction),
      daily: arr(g.daily).filter((d) => d && isNum(d.time)),
      verdict: (['none', 'insufficient', 'no_edge', 'better', 'worse'] as const).includes(g.verdict) ? g.verdict : 'insufficient',
      verdict_text: typeof g.verdict_text === 'string' ? g.verdict_text : '',
      brier_text: typeof g.brier_text === 'string' ? g.brier_text : null,
    }))
  return {
    generated_at: isNum(r.generated_at) ? r.generated_at : Date.now(),
    days: num(r.days),
    since: num(r.since),
    min_windows: isNum(r.min_windows) ? r.min_windows : MIN_WINDOWS,
    groups,
    notes: arr(r.notes).filter((n): n is string => typeof n === 'string'),
  }
}

/** The report, or null when this server does not offer it (an older backend answers 404). */
export async function fetchSkill(q: { source?: string; days?: number | null; modelKey?: string } = {}): Promise<SkillReport | null> {
  try {
    const r = await request<SkillReport>('GET', SKILL_PATH, {
      query: { source: q.source, days: q.days ?? 0, model_key: q.modelKey },
    })
    return normalizeSkill(r)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

export interface SkillState {
  report: SkillReport | null // null = not offered by this server (or not loaded yet: see `loading`)
  unavailable: boolean
  loading: boolean
  error: unknown
}

/** Polls the report: on mount, when `tick` changes and when the period changes. */
export function useSkillReport(range: SkillWindow, tick: number, source?: string): SkillState {
  const [state, setState] = useState<SkillState>({ report: null, unavailable: false, loading: true, error: null })
  const seq = useRef(0)
  useEffect(() => {
    const my = ++seq.current
    setState((s) => ({ ...s, loading: true }))
    fetchSkill({ source, days: windowDays(range) })
      .then((report) => {
        if (my === seq.current) setState({ report, unavailable: report === null, loading: false, error: null })
      })
      .catch((error) => {
        if (my === seq.current) setState((s) => ({ ...s, loading: false, error }))
      })
  }, [range, tick, source])
  return state
}

// ----------------------------------------------------------------------------- picking groups

export function groupById(r: SkillReport | null | undefined, id: string): SkillGroup | null {
  return r?.groups.find((g) => g.id === id) ?? null
}

/** The group of one scanner model key (or the scanner as a whole). */
export function scannerGroup(r: SkillReport | null | undefined, key?: string | null): SkillGroup | null {
  if (!r) return null
  return (
    (key ? r.groups.find((g) => g.source === 'scanner' && g.model_key === key) : null) ??
    r.groups.find((g) => g.id === 'scanner') ??
    r.groups.find((g) => g.source === 'scanner') ??
    null
  )
}

/** Everything together ("all"), else the only group. */
export function overallGroup(r: SkillReport | null | undefined): SkillGroup | null {
  if (!r) return null
  return r.groups.find((g) => g.id === 'all') ?? (r.groups.length === 1 ? r.groups[0] : null)
}

export function sourceGroup(r: SkillReport | null | undefined, source: string): SkillGroup | null {
  return source === 'scanner' ? scannerGroup(r) : groupById(r, source)
}

/** The group's daily rows by UTC day (unix s), only for the days the report covers entirely: the first day of a 7-day
 *  or 24-h report starts before `since`, so its reference counts only part of that day's rows and must not be drawn
 *  next to the whole day's hit rate (daily aggregates). */
export function completeDays(r: SkillReport | null | undefined, g: SkillGroup | null | undefined): Map<number, SkillDaily> {
  const since = r && isNum(r.since) ? r.since : 0
  return new Map(arr(g?.daily).filter((d) => d && isNum(d.time) && d.time * 1000 >= since).map((d) => [d.time, d]))
}

/** The reference of one daily point only when it was computed on (almost) the same verified predictions: the daily
 *  aggregates keep 90 days of every source, while resolved scanner rows are pruned after 30 days and the report is
 *  cached a minute, so a day whose counts differ (a pruned or partial day) gets no reference at all. */
export function sameRowsDay(days: Map<number, SkillDaily>, time: number, n: number | null | undefined): SkillDaily | null {
  const d = days.get(time)
  if (!d) return null
  return isNum(n) && n > 0 && Math.abs(d.n - n) > Math.max(2, 0.02 * n) ? null : d
}

// ----------------------------------------------------------------------------- formatting

export const VERDICT_SHORT: Record<SkillVerdict, string> = {
  none: 'Sin datos',
  insufficient: 'Muestra insuficiente',
  no_edge: 'Sin ventaja demostrada',
  better: 'Supera la referencia',
  worse: 'Por debajo de la referencia',
}

/** Colour only a conclusion (≥ 30 windows and an interval that excludes the reference). */
export function skillTone(g: SkillGroup | null | undefined): Tone {
  if (!g) return 'muted'
  return g.verdict === 'better' ? 'pos' : g.verdict === 'worse' ? 'neg' : 'muted'
}

/** Brier tone of a group against its climatology: coloured only with enough windows and a Brier-skill interval that
 *  excludes 0 (the same rule as the hit rate); otherwise neutral, whatever the point value says. */
export function bssTone(g: SkillGroup | null | undefined, minWindows = MIN_WINDOWS): Tone {
  if (!g || !g.bss_ci || g.windows < minWindows) return 'muted'
  return g.bss_ci[0] > 0 ? 'pos' : g.bss_ci[1] < 0 ? 'neg' : 'muted'
}

export function verdictBadgeTone(v: SkillVerdict): Tone {
  return v === 'better' ? 'pos' : v === 'worse' ? 'neg' : v === 'no_edge' ? 'warn' : 'muted'
}

/** "15 de 20" */
export function fmtOfN(hits: number | null | undefined, n: number | null | undefined): string {
  return isNum(hits) && isNum(n) ? `${fmtInt(hits)} de ${fmtInt(n)}` : '—'
}

/** "+3,2 pp" / "−4,0 pp" */
export function fmtPp(v: number | null | undefined, digits = 1): string {
  if (!isNum(v)) return '—'
  const s = fmtNum(Math.abs(v), digits)
  return `${v > 0.00001 ? '+' : v < -0.00001 ? '−' : ''}${s} pp`
}

/** "−4,1 a +6,0 pp" */
export function fmtCi(ci: [number, number] | null | undefined, unit = 'pp'): string {
  if (!ci) return '—'
  const one = (v: number) => `${v > 0.00001 ? '+' : v < -0.00001 ? '−' : ''}${fmtNum(Math.abs(v), 1)}`
  return `${one(ci[0])} a ${one(ci[1])} ${unit}`
}

/** Brier skill score as "+12 %" (better than climatology) / "−30 %". */
export function fmtBss(v: number | null | undefined): string {
  if (!isNum(v)) return '—'
  const p = Math.round(v * 100)
  return `${p > 0 ? '+' : p < 0 ? '−' : ''}${fmtInt(Math.abs(p))} %`
}

/** "62 % (siempre «alcista»)" */
export function fmtReference(ref: SkillReference | null | undefined, digits = 1): string {
  return ref ? `${fmtProb(ref.accuracy, digits)} (${ref.label})` : '—'
}

// ----------------------------------------------------------------------------- climatology without the report

export interface Climatology {
  n: number
  upRate: number
  brierRef: number // u (1 − u): Brier of always saying the period's up rate
  signRef: number // max(u, 1 − u): always the more frequent direction (the neutral outcome is not counted)
  signKind: 'up' | 'down'
}

/** From calibration bins (the same rows as the hit rate and Brier they come with). Null without data. */
export function climatology(bins: CalibrationBin[] | null | undefined): Climatology | null {
  let n = 0
  let up = 0
  for (const b of arr(bins)) {
    if (!b || !isNum(b.count) || b.count <= 0 || !isNum(b.observed)) continue
    n += b.count
    up += b.observed * b.count
  }
  if (n <= 0) return null
  const u = Math.min(1, Math.max(0, up / n))
  return { n, upRate: u, brierRef: u * (1 - u), signRef: Math.max(u, 1 - u), signKind: u >= 0.5 ? 'up' : 'down' }
}

/** 1 − Brier / climatology (null when either is missing or climatology is 0). */
export function brierSkill(brier: number | null | undefined, ref: number | null | undefined): number | null {
  return isNum(brier) && isNum(ref) && ref > 0 ? 1 - brier / ref : null
}
