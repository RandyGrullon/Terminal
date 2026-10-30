// Detail popover of the header semáforo. The decision comes first (level, headline, advice, "no operar" window),
// then only the few data points that justify it: ≤ 3 reasons, ≤ 6 metrics, ≤ 5 high-impact events; the weekly
// calendar loads on demand. Footer: data age and the "sound on red alerts" preference.
import { useEffect, useRef, useState } from 'react'
import { Ban, ChevronDown, ChevronUp, RefreshCw, TriangleAlert, X } from 'lucide-react'
import type { MarketConditions } from '../../api/types'
import { fmtAgo } from '../../lib/format'
import { Button, IconButton, Spinner, Toggle } from '../ui'
import { MacroEvents, MacroMetrics } from './MacroSections'
import MacroWeek from './MacroWeek'
import { blackoutText } from './macroFormat'
import { highImpactEvents } from './macroModel'
import { playAlertBeep, useSoundPref } from './sound'
import type { MacroState } from './useMacro'

export default function MarketPulsePanel(props: { state: MacroState; onClose: () => void }) {
  const { conditions, loading, stale, now, refresh } = props.state
  const [sound, setSound] = useSoundPref()
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    ref.current?.focus({ preventScroll: true })
  }, [])

  const onSound = (on: boolean) => {
    setSound(on)
    if (on) playAlertBeep() // audible confirmation (and unlocks audio with this user gesture)
  }

  return (
    <div
      ref={ref}
      className={`mp-panel lv-${conditions?.level ?? 'none'}`}
      role="dialog"
      aria-label="Semáforo de mercado"
      tabIndex={-1}
    >
      <div className="mp-panel-head">
        <span className="mp-panel-title">Semáforo de mercado</span>
        <IconButton size="sm" icon={<X size={14} />} title="Cerrar (Esc)" onClick={props.onClose} />
      </div>

      {conditions ? (
        <PanelBody c={conditions} now={now} />
      ) : loading ? (
        <div className="mp-empty">
          <Spinner /> Cargando condiciones de mercado…
        </div>
      ) : (
        <div className="mp-empty">
          <TriangleAlert size={18} className="warn" />
          <div className="grow">
            <div className="mp-empty-title">Sin datos macro</div>
            <div className="muted small">
              No se pudo obtener el calendario económico ni la volatilidad. Opera con cautela y reduce el tamaño.
            </div>
          </div>
          <Button size="sm" icon={<RefreshCw size={13} />} onClick={refresh}>
            Reintentar
          </Button>
        </div>
      )}

      <footer className="mp-foot">
        {conditions && (
          <span className={`mp-updated${stale ? ' stale' : ''}`} title={new Date(conditions.updated_at).toLocaleString('es-ES')}>
            {stale && <TriangleAlert size={12} />}
            Actualizado {fmtAgo(conditions.updated_at)}
            {stale ? ' · datos desactualizados' : ''}
          </span>
        )}
        {conditions && <IconButton size="sm" icon={<RefreshCw size={13} />} title="Actualizar ahora" onClick={refresh} />}
        <span className="grow" />
        <Toggle checked={sound} onChange={onSound} label="Sonido en alertas rojas" />
      </footer>
    </div>
  )
}

function PanelBody(props: { c: MarketConditions; now: number }) {
  const { c, now } = props
  const [week, setWeek] = useState(false)
  const blackout = blackoutText(c, now)
  const events = highImpactEvents(c, now)

  return (
    <>
      <div className={`mp-hero lv-${c.level}`}>
        <div className="mp-hero-level">
          <span className="mp-dot" aria-hidden="true" />
          {c.label}
        </div>
        <div className="mp-headline">{c.headline}</div>
        {c.advice && <div className="mp-advice">{c.advice}</div>}
      </div>

      {blackout && (
        <div className="mp-blackout" role="status">
          <Ban size={15} aria-hidden="true" />
          <span>{blackout}</span>
        </div>
      )}

      {c.reasons.length > 0 && (
        <ul className="mp-reasons">
          {c.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}

      {c.metrics.length > 0 && (
        <section className="mp-section">
          <div className="mp-section-title">Lo que importa ahora</div>
          <MacroMetrics metrics={c.metrics} />
        </section>
      )}

      <section className="mp-section">
        <div className="mp-section-title">Próximos eventos de alto impacto</div>
        {events.length > 0 ? (
          <MacroEvents events={events} now={now} />
        ) : (
          <div className="muted small">Sin eventos de alto impacto a la vista.</div>
        )}
        <button type="button" className="mp-link" aria-expanded={week} onClick={() => setWeek((w) => !w)}>
          {week ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          {week ? 'Ocultar semana' : 'Ver semana completa'}
        </button>
        {week && <MacroWeek now={now} />}
      </section>
    </>
  )
}
