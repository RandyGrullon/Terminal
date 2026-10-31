// Pure view model of the economic calendar popover (MacroWeek): this week and next week in local time (weeks start
// on Monday), events grouped by day, the source of every event (official calendars get a tag) and honest notes on
// what each source covers. No React here.
import type { MacroEvent } from '../../api/types'
import {
  fmtDayHeader,
  fmtHM,
  fmtIsoDay,
  fmtShortDay,
  isApproxTime,
  isOfficialSource,
  sourceLabel,
  sourceTag,
} from './macroFormat'
import type { CalendarCoverage } from './macroModel'

const DAY_MS = 86_400_000
export const MAX_CALENDAR_ROWS = 160
const OFFICIAL_NAMES = 'Fed, BLS, BEA, BCE, BoE y BoJ'

export interface CalendarRow {
  id: string
  /** "08:30", or "≈05:00" when the time is only a reference (Bank of Japan). */
  time: string
  currency: string
  title: string
  impact: MacroEvent['impact']
  past: boolean
  /** Short tag of an official source ("BLS", "BCE"...); null for the weekly feed. */
  tag: string | null
  tagTitle: string
  event: MacroEvent
}

export interface CalendarDay {
  key: string
  label: string
  rows: CalendarRow[]
}

export interface CalendarSection {
  key: 'this' | 'next'
  title: string
  days: CalendarDay[]
  empty: string
}

export interface CalendarView {
  sections: CalendarSection[]
  notes: string[]
  /** Events left out by the row limit. */
  truncated: number
}

/** Local midnight of `ms` moved by `days` calendar days (DST-safe). */
export function addDays(ms: number, days: number): number {
  const d = new Date(ms)
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + days).getTime()
}

/** Local Monday 00:00 of the week that contains `ms`. */
export function weekStart(ms: number): number {
  const day = new Date(ms).getDay() // 0 = Sunday
  return addDays(ms, -((day + 6) % 7))
}

/** Days to request so the calendar reaches the end of next week (8 on a Sunday, 14 on a Monday). */
export function calendarDays(nowMs: number): number {
  const end = addDays(weekStart(nowMs), 14)
  return Math.min(14, Math.max(1, Math.ceil((end - nowMs) / DAY_MS)))
}

function row(ev: MacroEvent, nowS: number): CalendarRow {
  const official = isOfficialSource(ev.source)
  const hm = fmtHM(ev.time * 1000)
  return {
    id: ev.id,
    time: isApproxTime(ev) ? `≈${hm}` : hm,
    currency: ev.currency,
    title: ev.title_es,
    impact: ev.impact,
    past: ev.time < nowS - 60,
    tag: official ? sourceTag(ev.source) : null,
    tagTitle: official ? `${sourceLabel(ev.source)}. Fecha oficial: puede cambiar si el organismo la mueve.` : '',
    event: ev,
  }
}

function groupDays(events: MacroEvent[], nowMs: number): CalendarDay[] {
  const days: CalendarDay[] = []
  for (const ev of events) {
    const key = new Date(ev.time * 1000).toDateString()
    const last = days[days.length - 1]
    if (last && last.key === key) last.rows.push(row(ev, nowMs / 1000))
    else days.push({ key, label: fmtDayHeader(ev.time, nowMs), rows: [row(ev, nowMs / 1000)] })
  }
  return days
}

function coverageNotes(c: CalendarCoverage | null, nextEnd: number, hasOfficial: boolean): string[] {
  const notes: string[] = []
  if (c?.weekly_stale) {
    notes.push(
      `Calendario semanal (ForexFactory) sin actualizar: solo se muestran las fechas oficiales (${OFFICIAL_NAMES}), sin previsiones.`,
    )
  } else if (c?.weekly_until && c.weekly_until * 1000 < nextEnd) {
    notes.push(
      `Datos semanales (ForexFactory, con previsiones) hasta el ${fmtShortDay(c.weekly_until * 1000 - 60_000)}; ` +
        `después, solo las fechas de los calendarios oficiales (${OFFICIAL_NAMES}), sin previsiones.`,
    )
  }
  const verified = c?.verified ? fmtIsoDay(c.verified) : null
  for (const s of c?.sources ?? []) {
    // only the official calendars: a failed refresh of the weekly feed is not an official date source (its stale
    // copy is already explained above)
    if (s.live && s.error && isOfficialSource(s.id)) {
      notes.push(
        `${s.label}: el calendario oficial no respondió (${s.error}); se usan las fechas guardadas` +
          `${verified ? ` o las verificadas el ${verified}` : ''}.`,
      )
    }
  }
  if (hasOfficial) notes.push('Las etiquetas marcan fechas de calendarios oficiales; pueden cambiar si el organismo las mueve.')
  return notes
}

/** Two-week view: events (sorted, unix s) of this week and next week; later ones are left out. */
export function buildCalendarView(
  events: MacroEvent[],
  coverage: CalendarCoverage | null,
  nowMs: number,
  maxRows = MAX_CALENDAR_ROWS,
): CalendarView {
  const nextStart = addDays(weekStart(nowMs), 7)
  const nextEnd = addDays(nextStart, 7)
  const inRange = [...events].sort((a, b) => a.time - b.time).filter((e) => e.time * 1000 < nextEnd)
  const shown = inRange.slice(0, maxRows)
  const thisWeek = shown.filter((e) => e.time * 1000 < nextStart)
  const nextWeek = shown.filter((e) => e.time * 1000 >= nextStart)
  const sections: CalendarSection[] = [
    {
      key: 'this',
      title: 'Esta semana',
      days: groupDays(thisWeek, nowMs),
      empty: 'Sin más eventos de impacto alto o medio esta semana.',
    },
    {
      key: 'next',
      title: `Próxima semana · ${fmtShortDay(nextStart)} – ${fmtShortDay(addDays(nextEnd, -1))}`,
      days: groupDays(nextWeek, nowMs),
      empty: 'Sin eventos de impacto alto o medio la próxima semana.',
    },
  ]
  const hasOfficial = shown.some((e) => isOfficialSource(e.source))
  return { sections, notes: coverageNotes(coverage, nextEnd, hasOfficial), truncated: inRange.length - shown.length }
}
