// Fallback read for assets without enough history for full NEXUS: TradingView technicals through the scanner's
// online model (QuickSignal), plus the reason and a shortcut to the TradingView view.
import { memo } from 'react'
import { Radar, Tv } from 'lucide-react'
import type { NexusResponse } from '../../api/types'
import { fmtBarTime, fmtInt, fmtProb, fmtScore, signTone, toneColor } from '../../lib/format'
import { ActionBadge, Button, Card, Disclaimer, Empty, Meter } from '../ui'
import './nexus.css'

function QuickSignalCard(props: { nexus: NexusResponse; onOpenTradingView: () => void }) {
  const { nexus } = props
  const q = nexus.quick
  return (
    <Card
      title={
        <>
          <Radar size={14} /> Lectura rápida
        </>
      }
      sub="Técnicos de TradingView + modelo del escáner"
    >
      <div className="col gap-3">
        <div className="notice">
          <div className="col gap-1">
            <strong>NEXUS completo no disponible para este activo</strong>
            <span>{nexus.message || 'No hay suficiente historial de velas para el motor NEXUS.'}</span>
          </div>
        </div>
        {q ? (
          <>
            <div className="row between">
              <ActionBadge action={q.action} large />
              <div className="col gap-0 right">
                <span className="tiny muted">Confianza</span>
                <b className="mono">{fmtProb(q.confidence)}</b>
              </div>
            </div>
            <div className="col gap-1">
              <div className="row between small">
                <span className="muted">Probabilidad de subida</span>
                <b className="mono">{fmtProb(q.prob_up)}</b>
              </div>
              <Meter value={q.prob_up} tone={q.prob_up >= 0.5 ? 'pos' : 'neg'} />
            </div>
            <div className="col gap-1">
              <div className="row between small">
                <span className="muted">Puntuación</span>
                <b className="mono" style={{ color: toneColor(signTone(q.score)) }}>
                  {fmtScore(q.score)}
                </b>
              </div>
              <div className="chart-meter-c">
                <Meter value={q.score} centered tone={signTone(q.score)} />
              </div>
            </div>
            <div className="kv">
              <span>Recomendación TradingView</span>
              <span className="mono">{q.rating === null ? '—' : fmtScore(q.rating)}</span>
              <span>Actualizaciones del modelo</span>
              <span className="mono">{fmtInt(q.model_updates)}</span>
              <span>Calculado</span>
              <span className="mono">{fmtBarTime(q.time)}</span>
            </div>
            {Array.isArray(q.reasons) && q.reasons.length > 0 && (
              <ul className="chart-list">
                {q.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <Empty title="Sin lectura rápida">El escáner no pudo leer los técnicos de este activo.</Empty>
        )}
        <Button icon={<Tv size={14} />} onClick={props.onOpenTradingView}>
          Ver el gráfico completo en TradingView
        </Button>
        <Disclaimer />
      </div>
    </Card>
  )
}

export default memo(QuickSignalCard)
