// Side panel of the live simulator: NEXUS action + advice, Trade Gate verdicts ("¿Tomo la operación?"),
// probabilities, regime, warnings, NEXUS' open position levels and the simulation paper account.
import { memo, type ReactNode } from 'react'
import { CircleCheck, CircleX, Crosshair, Gauge, Info, ShieldCheck, TriangleAlert, Wallet } from 'lucide-react'
import type { Direction, GateCheck, GateResult, NexusCurrent, NexusWarning, PositionState, SimState } from '../../api/types'
import {
  DIRECTION_LABEL,
  fmtBarTime,
  fmtInt,
  fmtNum,
  fmtPct,
  fmtPrice,
  fmtProb,
  fmtR,
  fmtScore,
  regimeTone,
  signTone,
  verdictTone,
  type Tone,
} from '../../lib/format'
import { ActionBadge, Badge, Meter, VerdictBadge } from '../ui'
import { fmtRate, simRegimeLabel, warningTone } from './labels'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function focusDirection(c: NexusCurrent): Direction {
  switch (c.action) {
    case 'BUY':
    case 'HOLD_LONG':
    case 'EXIT_LONG':
      return 'long'
    case 'SELL':
    case 'HOLD_SHORT':
    case 'EXIT_SHORT':
      return 'short'
    default:
      if (c.position) return c.position.side
      return (c.score ?? 0) >= 0 ? 'long' : 'short'
  }
}

/** One-line answer to "should I trade now?" built from both gate verdicts. */
function gateHeadline(gate: NexusCurrent['gate'] | null | undefined): { text: string; tone: Tone } | null {
  if (!gate || !gate.long || !gate.short) return null
  const { long, short } = gate
  if (long.verdict === 'TAKE' && short.verdict !== 'TAKE') return { text: `TOMAR en largo · filtro ${Math.round(long.score)}/100`, tone: 'pos' }
  if (short.verdict === 'TAKE' && long.verdict !== 'TAKE') return { text: `TOMAR en corto · filtro ${Math.round(short.score)}/100`, tone: 'pos' }
  if (long.verdict === 'TAKE' && short.verdict === 'TAKE') {
    const best = long.score >= short.score ? 'largo' : 'corto'
    return { text: `Ambas direcciones pasan el filtro; la mejor es ${best}`, tone: 'pos' }
  }
  if (long.verdict === 'WAIT' || short.verdict === 'WAIT') {
    const dir = long.verdict === 'WAIT' && (short.verdict !== 'WAIT' || long.score >= short.score) ? 'largo' : 'corto'
    return { text: `ESPERAR: el ${dir} aún no tiene confirmación suficiente`, tone: 'warn' }
  }
  return { text: 'NO TOMAR: ninguna dirección pasa el filtro ahora mismo', tone: 'neg' }
}

function CheckDots(props: { checks: GateCheck[] | null | undefined }) {
  const checks = Array.isArray(props.checks) ? props.checks : []
  if (!checks.length) return null
  return (
    <div className="sim-checks" aria-label="Comprobaciones del filtro">
      {checks.map((c) => {
        const state = c.passed === null ? 'neutral' : c.passed ? 'pass' : 'fail'
        const title = `${c.label}${c.critical ? ' (crítica)' : ''}: ${
          c.passed === null ? 'no aplica' : c.passed ? 'cumple' : 'no cumple'
        }${c.detail ? ` · ${c.detail}` : ''}`
        return <span key={c.id} className={`sim-check ${state} ${c.critical ? 'critical' : ''}`} title={title} />
      })}
    </div>
  )
}

function GateRow(props: { dir: Direction; gate: GateResult | null | undefined; focus: boolean }) {
  const g = props.gate
  if (!g) return null
  const against = Array.isArray(g.reasons_against) ? g.reasons_against : []
  const pro = Array.isArray(g.reasons_for) ? g.reasons_for : []
  return (
    <div className={`sim-gate ${props.focus ? 'focus' : ''} ${verdictTone(g.verdict)}`}>
      <div className="row between">
        <span className="sim-gate-dir">{props.dir === 'long' ? 'Largo ▲' : 'Corto ▼'}</span>
        <VerdictBadge verdict={g.verdict} score={g.score} label={g.verdict_label || undefined} />
      </div>
      <div className="sim-gate-nums">
        <span title="Probabilidad aprendida de alcanzar TP1 antes que el SL">
          P(TP1) <b>{fmtProb(g.win_prob)}</b>
        </span>
        <span title="Relación beneficio / riesgo hasta TP2">
          R:R <b>{fmtNum(g.rr, 2)}</b>
        </span>
        <span title="Valor esperado en R, después de comisiones">
          EV <b className={signTone(g.ev_r)}>{fmtR(g.ev_r)}</b>
        </span>
      </div>
      <CheckDots checks={g.checks} />
      {g.summary && <div className="sim-gate-summary">{g.summary}</div>}
      {(against.length > 0 || pro.length > 0) && (
        <details className="sim-details">
          <summary>
            Motivos · {pro.length} a favor · {against.length} en contra
          </summary>
          <ul className="sim-reasons">
            {against.map((r, i) => (
              <li key={`a${i}`}>
                <CircleX size={12} className="neg" />
                <span>{r}</span>
              </li>
            ))}
            {pro.map((r, i) => (
              <li key={`f${i}`}>
                <CircleCheck size={12} className="pos" />
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function MeterRow(props: { label: string; value: string; meter: ReactNode; title?: string }) {
  return (
    <div className="sim-meter-row" title={props.title}>
      <div className="row between small">
        <span className="muted">{props.label}</span>
        <span className="mono bold">{props.value}</span>
      </div>
      {props.meter}
    </div>
  )
}

function WarningList(props: { warnings: NexusWarning[] }) {
  return (
    <ul className="sim-warnings">
      {props.warnings.map((w, i) => {
        const tone = warningTone(w)
        const Icon = w.severity === 'info' ? Info : TriangleAlert
        return (
          <li key={`${w.code}-${i}`} className={tone}>
            <Icon size={13} />
            <span>{w.message}</span>
          </li>
        )
      })}
    </ul>
  )
}

function PositionBlock(props: { pos: PositionState }) {
  const p = props.pos
  const hit = (n: number) => (p.tp_hits >= n ? ' ✓' : '')
  return (
    <>
      <div className="row between">
        <Badge tone={p.side === 'long' ? 'pos' : 'neg'}>{DIRECTION_LABEL[p.side] ?? p.side}</Badge>
        <span className={`mono bold ${signTone(p.unrealized_pct)}`}>
          {fmtPct(p.unrealized_pct)} · {fmtR(p.r_multiple)}
        </span>
      </div>
      <div className="kv sim-kv">
        <span>Entrada</span>
        <span className="mono">{fmtPrice(p.entry_price)}</span>
        <span className="neg">SL · hasta aquí</span>
        <span className="mono neg">{fmtPrice(p.sl)}</span>
        <span>TP1{hit(1)}</span>
        <span className="mono pos">{fmtPrice(p.tp1)}</span>
        <span>TP2{hit(2)}</span>
        <span className="mono pos">{fmtPrice(p.tp2)}</span>
        <span>TP3{hit(3)}</span>
        <span className="mono pos">{fmtPrice(p.tp3)}</span>
        {isNum(p.trail) && (
          <>
            <span>Stop dinámico</span>
            <span className="mono warn">{fmtPrice(p.trail)}</span>
          </>
        )}
        <span>Barras en la operación</span>
        <span className="mono">{fmtInt(p.bars_in_trade)}</span>
        <span>Tamaño abierto</span>
        <span className="mono">{fmtProb(p.size_left)}</span>
        <span title="Valor esperado de seguir dentro, en R. Negativo = mejor cerrar antes del SL">EV de mantener</span>
        <span className={`mono ${signTone(p.ev_hold_r)}`}>{fmtR(p.ev_hold_r)}</span>
      </div>
      {isNum(p.ev_hold_r) && p.ev_hold_r < 0 && (
        <div className="sim-alert neg">
          <TriangleAlert size={13} />
          <span>El valor esperado de mantener es negativo: NEXUS sugiere cerrar antes de que toque el SL.</span>
        </div>
      )}
    </>
  )
}

function AccountBlock(props: { state: SimState | null }) {
  const acc = props.state?.account ?? null
  const stats = props.state?.stats ?? null
  if (!acc && !stats) return <div className="small muted">Sin datos de la cuenta todavía.</div>
  const ret = acc ? acc.return_pct : stats?.pnl_pct
  const trades = acc ? acc.trades : stats?.trades
  const winRate = acc ? acc.win_rate : stats?.win_rate
  return (
    <div className="kv sim-kv">
      {acc && (
        <>
          <span>Equity</span>
          <span className="mono">{fmtNum(acc.equity, 2)}</span>
        </>
      )}
      <span>Rendimiento</span>
      <span className={`mono ${signTone(ret)}`}>{fmtPct(ret)}</span>
      <span>Operaciones</span>
      <span className="mono">
        {fmtInt(trades)}
        {acc && isNum(acc.wins) && isNum(acc.losses) && <span className="muted"> ({acc.wins} G · {acc.losses} P)</span>}
      </span>
      <span>% acierto</span>
      <span className="mono">{fmtRate(winRate)}</span>
      {acc && (
        <>
          <span>P&amp;L realizado</span>
          <span className={`mono ${signTone(acc.realized_pnl)}`}>{fmtNum(acc.realized_pnl, 2)}</span>
          <span>P&amp;L abierto</span>
          <span className={`mono ${signTone(acc.unrealized_pnl)}`}>{fmtNum(acc.unrealized_pnl, 2)}</span>
          <span>Posiciones abiertas</span>
          <span className="mono">{fmtInt(acc.open_positions)}</span>
        </>
      )}
    </div>
  )
}

export const SimPanel = memo(function SimPanel(props: { current: NexusCurrent | null; state: SimState | null; tf: string | null }) {
  const c = props.current
  const gate = c?.gate ?? null
  const focus = c ? focusDirection(c) : null
  const headline = gateHeadline(gate)
  const warnings = Array.isArray(c?.warnings) ? c.warnings : []
  const reasons = Array.isArray(c?.reasons) ? c.reasons.slice(0, 3) : []
  const probTone: Tone = !c ? 'muted' : c.prob_up >= 0.55 ? 'pos' : c.prob_up <= 0.45 ? 'neg' : 'warn'

  return (
    <div className="sim-panel">
      <section className="sim-sec">
        <div className="sim-sec-title">
          <Crosshair size={12} /> Señal NEXUS
          {c && <span className="mono sim-sec-aside">{fmtBarTime(c.time, props.tf ?? undefined)}</span>}
        </div>
        {c ? (
          <>
            <div className="row between">
              <ActionBadge action={c.action} label={c.action_label || undefined} />
              <span className="mono bold">{fmtPrice(c.price)}</span>
            </div>
            {c.advice && <div className="sim-advice">{c.advice}</div>}
            {reasons.length > 0 && (
              <ul className="sim-bullets">
                {reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <div className="small muted">
            {props.state?.running
              ? 'Esperando suficientes velas para que NEXUS emita su primera lectura…'
              : 'Inicia una simulación para ver las señales en directo.'}
          </div>
        )}
      </section>

      <section className="sim-sec">
        <div className="sim-sec-title">
          <ShieldCheck size={12} /> ¿Tomo la operación?
        </div>
        {headline && <div className={`sim-headline ${headline.tone}`}>{headline.text}</div>}
        {gate ? (
          <>
            <GateRow dir="long" gate={gate.long} focus={focus === 'long'} />
            <GateRow dir="short" gate={gate.short} focus={focus === 'short'} />
          </>
        ) : (
          <div className="small muted">El filtro evalúa cada posible entrada en largo y en corto con cada vela nueva.</div>
        )}
      </section>

      {c && (
        <section className="sim-sec">
          <div className="sim-sec-title">
            <Gauge size={12} /> Probabilidad y régimen
          </div>
          <MeterRow
            label="P↑ (probabilidad de subida)"
            value={fmtProb(c.prob_up)}
            meter={<Meter value={c.prob_up} tone={probTone} />}
            title="Probabilidad calibrada de que el precio suba en el horizonte de NEXUS"
          />
          <MeterRow
            label="Puntuación"
            value={fmtScore(c.score)}
            meter={<Meter value={c.score} centered tone={signTone(c.score)} />}
            title="-1 = muy bajista · +1 = muy alcista"
          />
          <MeterRow label="Confianza" value={fmtProb(c.confidence)} meter={<Meter value={c.confidence} tone="accent" />} />
          <div className="row between small">
            <span className="muted">Régimen detectado</span>
            <Badge tone={regimeTone(c.regime)}>{c.regime_label || simRegimeLabel(c.regime)}</Badge>
          </div>
        </section>
      )}

      {warnings.length > 0 && (
        <section className="sim-sec">
          <div className="sim-sec-title">
            <TriangleAlert size={12} /> Advertencias
          </div>
          <WarningList warnings={warnings} />
        </section>
      )}

      {c && (
        <section className="sim-sec">
          <div className="sim-sec-title">Posición NEXUS</div>
          {c.position ? (
            <PositionBlock pos={c.position} />
          ) : (
            <div className="small muted">Sin posición abierta. NEXUS solo entra cuando el filtro dice TOMAR.</div>
          )}
        </section>
      )}

      <section className="sim-sec">
        <div className="sim-sec-title">
          <Wallet size={12} /> Cuenta «Simulación» (papel)
          {props.state && (
            <span className="sim-sec-aside">
              <Badge tone={props.state.auto_trade ? 'accent' : 'muted'}>{props.state.auto_trade ? 'Auto-trading' : 'Manual'}</Badge>
            </span>
          )}
        </div>
        <AccountBlock state={props.state} />
      </section>
    </div>
  )
})
