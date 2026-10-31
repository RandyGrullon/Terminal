// Trade Gate detail ("¿Tomo esta operación?"): key numbers, levels, the checklist (✓ / ✗ / –) with the learned
// weight of each check, and the concrete reasons for and against. Also how a verdict is presented next to NEXUS'
// own signal: a TOMAR without the matching entry signal is "Filtro favorable · sin señal NEXUS".
import { memo } from 'react'
import { CircleCheck, CircleMinus, CircleX, ShieldCheck, ThumbsDown, ThumbsUp } from 'lucide-react'
import type { Direction, GateCheck, GateResult, NexusAction } from '../../api/types'
import { fmtNum, fmtPct, fmtPrice, fmtProb, fmtR, signTone, toneColor, verdictTone } from '../../lib/format'
import { Meter, VerdictBadge } from '../ui'
import './nexus.css'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

// ----------------------------------------------------------------------------- verdict vs NEXUS signal

/** NEXUS action that makes a gate TOMAR a real opportunity in each direction. */
const ENTRY_ACTION: Record<Direction, NexusAction> = { long: 'BUY', short: 'SELL' }

export const FAVORABLE_LABEL = 'Filtro favorable · sin señal NEXUS'

/**
 * take = TOMAR confirmed by NEXUS (BUY for a long, SELL for a short); favorable = TOMAR while NEXUS gives no entry
 * signal in that direction (shown as "Filtro favorable · sin señal NEXUS", never as TOMAR); unconfirmed = TOMAR with
 * no NEXUS reading at hand (`action` undefined).
 */
export type GateSignal = 'take' | 'favorable' | 'unconfirmed' | 'wait' | 'avoid' | 'none'

export function gateSignal(
  gate: Pick<GateResult, 'verdict' | 'direction'> | null | undefined,
  action?: NexusAction | string | null,
): GateSignal {
  if (!gate) return 'none'
  if (gate.verdict === 'TAKE') {
    if (action === undefined) return 'unconfirmed'
    return action === ENTRY_ACTION[gate.direction] ? 'take' : 'favorable'
  }
  return gate.verdict === 'WAIT' ? 'wait' : gate.verdict === 'AVOID' ? 'avoid' : 'none'
}

/** The gate's one-line summary, re-labelled when its TOMAR is not backed by a NEXUS signal. */
export function gateSummary(gate: GateResult, action?: NexusAction | string | null): string {
  const s = gate.summary || ''
  if (gateSignal(gate, action) !== 'favorable') return s
  return s.replace(/^\s*TOMAR\s*:\s*/i, 'Filtro favorable: ')
}

/** Why a favorable gate is not an opportunity: NEXUS gives no entry signal in that direction right now. */
export function noSignalNote(direction: Direction, actionLabel?: string | null): string {
  const what = direction === 'long' ? 'compra' : 'venta'
  const side = direction === 'long' ? 'el largo' : 'el corto'
  const now = actionLabel ? ` (dice ${actionLabel})` : ''
  return `NEXUS no da señal de ${what} en esta vela${now}: el filtro aprueba ${side}, pero no es una oportunidad TOMAR.`
}

/** Short verdict text for tables and summaries ("TOMAR 72/100"), with the same rule as GateVerdictBadge. */
export function gateVerdictText(gate: GateResult, action?: NexusAction | string | null): string {
  const label = gateSignal(gate, action) === 'favorable' ? FAVORABLE_LABEL : gate.verdict_label || gate.verdict
  return isNum(gate.score) ? `${label} ${Math.round(gate.score)}/100` : label
}

/** Verdict chip that respects NEXUS' signal (see gateSignal). */
export function GateVerdictBadge(props: { gate: GateResult; action?: NexusAction | string | null; large?: boolean; hideScore?: boolean }) {
  const { gate } = props
  const score = props.hideScore ? undefined : gate.score
  if (gateSignal(gate, props.action) === 'favorable') {
    return (
      <span className={`verdict info ${props.large ? 'lg' : ''}`} style={{ color: 'var(--info)' }}>
        <ShieldCheck size={props.large ? 20 : 15} />
        {FAVORABLE_LABEL}
        {isNum(score) && <span className="mono" style={{ opacity: 0.85 }}>{Math.round(score)}/100</span>}
      </span>
    )
  }
  return <VerdictBadge verdict={gate.verdict} score={score} label={gate.verdict_label || undefined} large={props.large} />
}

// ----------------------------------------------------------------------------- detail

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
