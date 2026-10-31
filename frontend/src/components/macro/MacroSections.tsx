// Compact rows of the semáforo panel: the key metrics (≤ 6) and the high-impact events (≤ 5).
import type { MacroEvent, MacroMetric } from '../../api/types'
import { toneColor } from '../../lib/format'
import { countdownTone, fmtCountdown, fmtEventWhen, isApproxTime, officialNote, sourceLabel } from './macroFormat'
import { IMPACT_LABEL, METRIC_TONE } from './macroModel'

export function MacroMetrics(props: { metrics: MacroMetric[] }) {
  return (
    <div className="mp-metrics">
      {props.metrics.map((mt) => {
        const tone = METRIC_TONE[mt.level]
        const color = toneColor(tone)
        return (
          <div key={mt.id} className="mp-metric" title={mt.note || mt.label}>
            <span className="mp-tdot" style={{ background: color }} aria-hidden="true" />
            <span className="mp-metric-label ellipsis">{mt.label}</span>
            <span className="mp-metric-value mono">{mt.display}</span>
            <span className="mp-metric-change mono" style={mt.level === 'ok' ? undefined : { color }}>
              {mt.change_display || '—'}
            </span>
            {mt.note && <span className="mp-metric-note">{mt.note}</span>}
          </div>
        )
      })}
    </div>
  )
}

/** Forecast / previous values of an event ("Previsión 0,3 % · Anterior 0,2 %"), empty when absent. */
export function eventValues(ev: MacroEvent): string {
  const parts: string[] = []
  if (ev.forecast) parts.push(`Previsión ${ev.forecast}`)
  if (ev.previous) parts.push(`Anterior ${ev.previous}`)
  return parts.join(' · ')
}

export function eventTooltip(ev: MacroEvent): string {
  const values = eventValues(ev)
  const original = ev.title && ev.title !== ev.title_es ? `\n${ev.title}` : ''
  const source = ev.source ? `\nFuente: ${sourceLabel(ev.source)}` : ''
  const approx = isApproxTime(ev) ? '\nHora aproximada: se publica al terminar la reunión' : ''
  return `${ev.title_es}${original}\n${IMPACT_LABEL[ev.impact]} · ${ev.currency || '—'}${values ? `\n${values}` : ''}${source}${approx}`
}

export function MacroEvents(props: { events: MacroEvent[]; now: number }) {
  return (
    <div className="mp-events">
      {props.events.map((ev) => {
        const secs = ev.time - props.now / 1000
        const tone = countdownTone(secs)
        const values = eventValues(ev) || officialNote(ev)
        return (
          <div key={ev.id} className={`mp-event imp-${ev.impact}${secs < -60 ? ' past' : ''}`} title={eventTooltip(ev)}>
            <span className="mp-event-when mono">{fmtEventWhen(ev.time, props.now)}</span>
            <span className="mp-event-title ellipsis">{ev.title_es}</span>
            {ev.currency ? <span className="mp-cur">{ev.currency}</span> : <span />}
            <span className="mp-event-count" style={tone === 'muted' ? undefined : { color: toneColor(tone) }}>
              {fmtCountdown(secs)}
            </span>
            {values && <span className="mp-event-values ellipsis">{values}</span>}
          </div>
        )
      })}
    </div>
  )
}
