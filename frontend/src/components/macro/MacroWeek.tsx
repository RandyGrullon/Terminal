// Economic calendar of the semáforo panel: this week and next week (high + medium impact), grouped by local day.
// The current week comes from the weekly feed (with forecasts); the next one only from the official calendars (Fed,
// BLS, BEA, BCE, BoE, BoJ), whose rows carry a source tag. Loaded on demand when the user expands it.
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api/client'
import type { MacroEvent } from '../../api/types'
import { Spinner } from '../ui'
import { buildCalendarView, calendarDays } from './calendarView'
import { normalizeCalendar, type CalendarCoverage } from './macroModel'
import { eventTooltip } from './MacroSections'

interface Loaded {
  events: MacroEvent[]
  coverage: CalendarCoverage | null
}

export default function MacroWeek(props: { now: number }) {
  const [data, setData] = useState<Loaded | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const nowRef = useRef(props.now)
  nowRef.current = props.now

  useEffect(() => {
    let alive = true
    setLoading(true)
    setFailed(false)
    api.macroCalendar({ days: calendarDays(nowRef.current), impact: 'high,medium' }).then(
      (raw: unknown) => {
        if (!alive) return
        setData(normalizeCalendar(raw))
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

  const minute = Math.floor(props.now / 60_000) // the "past" marks only change once a minute
  const view = useMemo(
    () => (data ? buildCalendarView(data.events, data.coverage, minute * 60_000) : null),
    [data, minute],
  )

  if (!view) {
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

  return (
    <>
      <div className="mp-week" aria-label="Calendario económico: esta semana y la próxima">
        {view.sections.map((s) => (
          <section key={s.key} className="mp-week-sec" aria-label={s.title}>
            <div className="mp-week-sec-title">{s.title}</div>
            {s.days.length === 0 && <div className="mp-week-state">{s.empty}</div>}
            {s.days.map((d) => (
              <div key={d.key} className="mp-week-day">
                <div className="mp-week-head">{d.label}</div>
                {d.rows.map((r) => (
                  <div
                    key={r.id}
                    className={`mp-week-row imp-${r.impact}${r.past ? ' past' : ''}`}
                    title={eventTooltip(r.event)}
                  >
                    <span className="mono">{r.time}</span>
                    <span className="mp-cur">{r.currency || '—'}</span>
                    <span className="ellipsis">{r.title}</span>
                    {r.tag ? (
                      <span className="mp-src" title={r.tagTitle}>
                        {r.tag}
                      </span>
                    ) : (
                      <span />
                    )}
                  </div>
                ))}
              </div>
            ))}
          </section>
        ))}
        {view.truncated > 0 && <div className="mp-week-state">… {view.truncated} eventos más</div>}
      </div>
      {view.notes.length > 0 && (
        <ul className="mp-week-notes">
          {view.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </>
  )
}
