// Trade Gate detail ("¿Tomo esta operación?"): key numbers, levels, the checklist (✓ / ✗ / –) with the learned
// weight of each check, and the concrete reasons for and against.
import { memo } from 'react'
import { CircleCheck, CircleMinus, CircleX, ThumbsDown, ThumbsUp } from 'lucide-react'
import type { GateCheck, GateResult } from '../../api/types'
import { fmtNum, fmtPct, fmtPrice, fmtProb, fmtR, signTone, toneColor, verdictTone } from '../../lib/format'
import { Meter } from '../ui'
import './nexus.css'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function checkRank(c: GateCheck): number {
  if (c.passed === false) return c.critical ? 0 : 1
  if (c.passed === true) return 2
  return 3
}

/** Signed distance (in %) from the reference price to a level. */
function distPct(level: number, ref: number | null | undefined): string {
  if (!isNum(level) || !isNum(ref) || ref === 0) return ''
  return fmtPct(((level - ref) / ref) * 100, 2)
}

export function GateNumbers({ gate }: { gate: GateResult }) {
  const tone = verdictTone(gate.verdict)
  return (
    <div className="chart-mini-stats">
      <div title="Puntuación del filtro (0-100) ponderada con los pesos aprendidos">
        <span>Puntos</span>
        <b style={{ color: toneColor(tone) }}>{isNum(gate.score) ? `${Math.round(gate.score)}/100` : '—'}</b>
      </div>
      <div title="Probabilidad aprendida de alcanzar TP1 antes que el stop loss">
        <span>P. éxito</span>
        <b style={{ color: toneColor(isNum(gate.win_prob) && gate.win_prob >= 0.5 ? 'pos' : 'neg') }}>{fmtProb(gate.win_prob)}</b>
      </div>
      <div title="Relación beneficio / riesgo hasta TP2">
        <span>R:R</span>
        <b style={{ color: toneColor(isNum(gate.rr) && gate.rr >= 1.5 ? 'pos' : 'warn') }}>{isNum(gate.rr) ? fmtNum(gate.rr, 2) : '—'}</b>
      </div>
      <div title="Valor esperado de la operación en múltiplos de R, después de comisiones">
        <span>EV</span>
        <b style={{ color: toneColor(signTone(gate.ev_r)) }}>{fmtR(gate.ev_r)}</b>
      </div>
    </div>
  )
}

export function GateLevels({ gate }: { gate: GateResult }) {
  const ref = gate.entry
  return (
    <div className="kv chart-levels">
      <span>Entrada</span>
      <span className="mono">{fmtPrice(gate.entry)}</span>
      <span className="neg">SL · hasta aquí</span>
      <span className="mono neg">
        {fmtPrice(gate.sl)} <span className="tiny">{distPct(gate.sl, ref)}</span>
      </span>
      <span>TP1</span>
      <span className="mono pos">
        {fmtPrice(gate.tp1)} <span className="tiny">{distPct(gate.tp1, ref)}</span>
      </span>
      <span>TP2</span>
      <span className="mono pos">
        {fmtPrice(gate.tp2)} <span className="tiny">{distPct(gate.tp2, ref)}</span>
      </span>
      <span>TP3</span>
      <span className="mono pos">
        {fmtPrice(gate.tp3)} <span className="tiny">{distPct(gate.tp3, ref)}</span>
      </span>
    </div>
  )
}

export function CheckList({ checks }: { checks: GateCheck[] }) {
  const list = [...(Array.isArray(checks) ? checks : [])].sort((a, b) => checkRank(a) - checkRank(b))
  if (list.length === 0) return <div className="tiny muted">El filtro no devolvió comprobaciones.</div>
  return (
    <ul className="chart-checks">
      {list.map((c) => {
        const state = c.passed === true ? 'ok' : c.passed === false ? 'fail' : 'neutral'
        const Icon = state === 'ok' ? CircleCheck : state === 'fail' ? CircleX : CircleMinus
        const weight = isNum(c.weight) ? Math.max(0, Math.min(1, c.weight)) : 0
        return (
          <li key={c.id} className={`chart-check ${state} ${c.critical && state === 'fail' ? 'critical' : ''}`}>
            <Icon size={15} className="chart-check-icon" />
            <div className="grow">
              <div className="row gap-1 wrap">
                <span className="chart-check-label">{c.label || c.id}</span>
                {c.critical && (
                  <span className="chart-check-crit" title="Si falla, el filtro dice NO TOMAR sin importar la puntuación">
                    crítico
                  </span>
                )}
              </div>
              {c.detail && <div className="chart-check-detail">{c.detail}</div>}
            </div>
            <div className="chart-check-weight" title={`Peso aprendido: ${fmtNum(weight, 2)} (cuánto influye esta comprobación)`}>
              <Meter value={weight} tone={state === 'ok' ? 'pos' : state === 'fail' ? 'neg' : 'muted'} height={4} />
            </div>
          </li>
        )
      })}
    </ul>
  )
}

export function Reasons({ gate }: { gate: GateResult }) {
  const pros = Array.isArray(gate.reasons_for) ? gate.reasons_for : []
  const cons = Array.isArray(gate.reasons_against) ? gate.reasons_against : []
  if (pros.length === 0 && cons.length === 0) return null
  return (
    <div className="chart-reasons">
      {pros.length > 0 && (
        <div className="col gap-1">
          <div className="chart-reasons-title pos">
            <ThumbsUp size={12} /> A favor
          </div>
          <ul>
            {pros.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}
      {cons.length > 0 && (
        <div className="col gap-1">
          <div className="chart-reasons-title neg">
            <ThumbsDown size={12} /> En contra
          </div>
          <ul>
            {cons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/** Full detail of one gate evaluation. */
function GateChecklist(props: { gate: GateResult; showNumbers?: boolean; showLevels?: boolean }) {
  const { gate, showNumbers = true, showLevels = true } = props
  return (
    <div className="chart-gate-detail">
      {showNumbers && <GateNumbers gate={gate} />}
      {showLevels && <GateLevels gate={gate} />}
      <CheckList checks={gate.checks} />
      <Reasons gate={gate} />
    </div>
  )
}

export default memo(GateChecklist)
