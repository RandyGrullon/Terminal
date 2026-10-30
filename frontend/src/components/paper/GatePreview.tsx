// Trade Gate preview shown BEFORE opening a demo order: verdict, one-line summary, key numbers, proposed levels
// ("hasta aquí" stop and targets) and the top reasons for and against; the full checklist is one click away.
import { memo } from 'react'
import { ThumbsDown, ThumbsUp } from 'lucide-react'
import type { GateResult, Timeframe } from '../../api/types'
import { DIRECTION_LABEL, fmtBarTime, fmtNum, fmtPct, fmtPrice, fmtProb, fmtR, signTone, toneColor, verdictTone } from '../../lib/format'
import { ErrorBox, Loading, VerdictBadge } from '../ui'
import GateChecklist from '../nexus/GateChecklist'
import { isNum } from '../markets/opsUtils'
import { distPct } from './paperUtils'

const TOP_REASONS = 3

interface Props {
  gate: GateResult | null
  loading: boolean
  error: string | null
  onRetry: () => void
  tf: Timeframe
}

function Level({ label, value, entry, tone }: { label: string; value: number | null | undefined; entry: number | null; tone?: 'pos' | 'neg' }) {
  const d = distPct(value, entry)
  return (
    <>
      <span className={tone === 'neg' ? 'neg' : undefined}>{label}</span>
      <span className={`mono ${tone ?? ''}`}>
        {fmtPrice(value)} {d !== null && <span className="tiny muted">{fmtPct(d)}</span>}
      </span>
    </>
  )
}

function ReasonList({ items, kind }: { items: string[]; kind: 'for' | 'against' }) {
  if (!items.length) return null
  const extra = items.length - TOP_REASONS
  return (
    <div>
      <span className={`pp-reasons-title ${kind === 'for' ? 'pos' : 'neg'}`}>
        {kind === 'for' ? <ThumbsUp size={11} /> : <ThumbsDown size={11} />}
        {kind === 'for' ? 'A favor' : 'En contra'}
      </span>
      <ul>
        {items.slice(0, TOP_REASONS).map((r, i) => (
          <li key={i}>{r}</li>
        ))}
        {extra > 0 && <li className="muted">y {extra} más en el detalle…</li>}
      </ul>
    </div>
  )
}

function GatePreview({ gate, loading, error, onRetry, tf }: Props) {
  if (!gate) {
    if (error) return <ErrorBox error={error} onRetry={onRetry} />
    if (loading) return <Loading label="Consultando el filtro…" />
    return <div className="tiny muted">Elige un símbolo para ver qué dice el filtro antes de abrir.</div>
  }
  const tone = verdictTone(gate.verdict)
  const entry = isNum(gate.entry) ? gate.entry : null
  const pros = Array.isArray(gate.reasons_for) ? gate.reasons_for : []
  const cons = Array.isArray(gate.reasons_against) ? gate.reasons_against : []
  return (
    <div className={`pp-gate ${tone}`} style={loading ? { opacity: 0.6 } : undefined} aria-busy={loading}>
      <div className="row between wrap gap-2">
        <VerdictBadge verdict={gate.verdict} score={gate.score} label={gate.verdict_label || undefined} large />
        <span className="tiny muted">
          {DIRECTION_LABEL[gate.direction] ?? gate.direction} · {fmtBarTime(gate.time, tf)}
        </span>
      </div>
      {gate.summary && <p className="pp-gate-summary">{gate.summary}</p>}

      <div className="pp-gnums">
        <div title="Probabilidad aprendida de alcanzar TP1 antes que el stop loss">
          <span>P. éxito</span>
          <b style={{ color: toneColor(isNum(gate.win_prob) && gate.win_prob >= 0.5 ? 'pos' : 'neg') }}>{fmtProb(gate.win_prob)}</b>
        </div>
        <div title="Relación beneficio / riesgo hasta TP2">
          <span>R:R</span>
          <b style={{ color: toneColor(isNum(gate.rr) && gate.rr >= 1.5 ? 'pos' : 'warn') }}>{isNum(gate.rr) ? fmtNum(gate.rr, 2) : '—'}</b>
        </div>
        <div title="Valor esperado en múltiplos de R, después de comisiones">
          <span>EV</span>
          <b style={{ color: toneColor(signTone(gate.ev_r)) }}>{fmtR(gate.ev_r)}</b>
        </div>
        <div title="Puntuación del filtro (0-100)">
          <span>Puntos</span>
          <b style={{ color: toneColor(tone) }}>{isNum(gate.score) ? Math.round(gate.score) : '—'}</b>
        </div>
      </div>

      <div className="kv">
        <Level label="Entrada" value={gate.entry} entry={null} />
        <Level label="SL · hasta aquí" value={gate.sl} entry={entry} tone="neg" />
        <Level label="TP1" value={gate.tp1} entry={entry} tone="pos" />
        <Level label="TP2" value={gate.tp2} entry={entry} tone="pos" />
        <Level label="TP3" value={gate.tp3} entry={entry} tone="pos" />
      </div>

      {(pros.length > 0 || cons.length > 0) && (
        <div className="pp-reasons">
          <ReasonList items={cons} kind="against" />
          <ReasonList items={pros} kind="for" />
        </div>
      )}

      <details className="pp-gate-more">
        <summary>Ver todas las comprobaciones ({Array.isArray(gate.checks) ? gate.checks.length : 0})</summary>
        <GateChecklist gate={gate} showNumbers={false} showLevels={false} />
      </details>
      {error && <div className="tiny neg">{error}</div>}
    </div>
  )
}

export default memo(GatePreview)
