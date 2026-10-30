// "Ver semana completa": compact economic calendar for the next 7 days (high + medium impact), grouped by
// local day. Loaded on demand when the user expands it.
import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { MacroEvent } from '../../api/types'
import { Spinner } from '../ui'
import { fmtDayHeader, fmtHM } from './macroFormat'
import { normalizeEvents } from './macroModel'
import { eventTooltip } from './MacroSections'

const MAX_ROWS = 120

interface DayGroup {
  key: string
  events: MacroEvent[]
}

function groupByDay(events: MacroEvent[]): DayGroup[] {
  const groups: DayGroup[] = []
  for (const ev of events.slice(0, MAX_ROWS)) {
    const key = new Date(ev.time * 1000).toDateString()
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.events.push(ev)
    else groups.push({ key, events: [ev] })
  }
  return groups
}

export default function MacroWeek(props: { now: number }) {
  const [events, setEvents] = useState<MacroEvent[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setFailed(false)
    api.macroCalendar({ days: 7, impact: 'high,medium' }).then(
      (raw) => {
        if (!alive) return
        setEvents(normalizeEvents(raw))
        setLoading(false)
      },
      () => {
        if (!alive) return
        setFailed(true)
        setLoading(false)
      },
    )
    return () => {
      alive = false
    }
  }, [attempt])

  const groups = useMemo(() => groupByDay(events ?? []), [events])

  if (!events) {
    if (loading) {
      return (
        <div className="mp-week-state">
          <Spinner /> Cargando calendario…
        </div>
      )
    }
    return (
      <div className="mp-week-state">
        No se pudo cargar el calendario.
        {failed && (
          <button type="button" className="mp-link" onClick={() => setAttempt((a) => a + 1)}>
            Reintentar
          </button>
        )}
      </div>
    )
  }
  if (!groups.length) return <div className="mp-week-state">Sin eventos de impacto alto o medio en los próximos 7 días.</div>

  const nowS = props.now / 1000
  return (
    <div className="mp-week" aria-label="Calendario de la semana">
      {groups.map((g) => (
        <div key={g.key} className="mp-week-day">
          <div className="mp-week-head">{fmtDayHeader(g.events[0].time, props.now)}</div>
          {g.events.map((ev) => (
            <div key={ev.id} className={`mp-week-row imp-${ev.impact}${ev.time < nowS - 60 ? ' past' : ''}`} title={eventTooltip(ev)}>
              <span className="mono">{fmtHM(ev.time * 1000)}</span>
              <span className="mp-cur">{ev.currency || '—'}</span>
              <span className="ellipsis">{ev.title_es}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
