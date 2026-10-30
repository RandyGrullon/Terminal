// Hero card of the chart workspace: what NEXUS says right now and — above all — whether to take a trade.
// Action + advice, "¿Comprar ahora?" / "¿Vender ahora?" gate verdicts (click for the full checklist),
// probability / score / confidence meters, regime, warnings (early exit highlighted), the open position with its
// "hasta aquí" stop and the EV of holding, expert contributions, prediction, learning and honest statistics.
import { memo, useMemo, useState, type ReactNode } from 'react'
import {
  Activity,
  BookOpen,
  Brain,
  ChevronDown,
  ChevronUp,
  CircleCheck,
  Info,
  OctagonX,
  RefreshCw,
  ShieldCheck,
  Siren,
  Target,
  TriangleAlert,
} from 'lucide-react'
import type { Contribution, GateResult, NexusCurrent, NexusResponse, NexusWarning, PositionState, Prediction, Timeframe } from '../../api/types'
import {
  REGIME_LABEL,
  fmtAgo,
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
  toneColor,
  verdictTone,
} from '../../lib/format'
import { ActionBadge, Badge, Card, Disclaimer, Empty, ErrorBox, IconButton, Loading, Meter, Spinner, VerdictBadge } from '../ui'
import { asFraction, isFiniteNumber } from '../chart/chartUtils'
import GateChecklist from './GateChecklist'
import QuickSignalCard from './QuickSignalCard'
import './nexus.css'

const WARNING_LABEL: Record<string, string> = {
  EARLY_EXIT: 'Salida anticipada',
  NEAR_SL: 'Cerca del stop',
  DIVERGENCE: 'Divergencia',
  VOL_SPIKE: 'Pico de volatilidad',
  TIME_STOP: 'Tiempo agotado',
  COUNTER_TREND: 'Contra tendencia',
  REGIME_CHANGE: 'Cambio de régimen',
  LOW_DATA: 'Pocos datos',
}

const SEVERITY_RANK: Record<string, number> = { critical: 0, warn: 1, info: 2 }

function Section(props: { title: ReactNode; icon?: ReactNode; open?: boolean; children: ReactNode }) {
  return (
    <details className="chart-sec" open={props.open}>
      <summary>
        {props.icon}
        <span className="grow">{props.title}</span>
        <ChevronDown size={13} className="chart-sec-chevron" />
      </summary>
      <div className="chart-sec-body">{props.children}</div>
    </details>
  )
}

function MeterRow(props: { label: string; value: string; tone: string; title?: string; children: ReactNode }) {
  return (
    <div className="col gap-1" title={props.title}>
      <div className="row between small">
        <span className="muted">{props.label}</span>
        <b className="mono" style={{ color: props.tone }}>
          {props.value}
        </b>
      </div>
      {props.children}
    </div>
  )
}

// ----------------------------------------------------------------------------- gate rows

function GateRow(props: { question: string; gate: GateResult | undefined; open: boolean; onToggle: () => void }) {
  const { gate } = props
  if (!gate) {
    return (
      <div className="chart-gate-row muted">
        <div className="row between">
          <span className="chart-gate-q">{props.question}</span>
          <VerdictBadge verdict={null} />
        </div>
      </div>
    )
  }
  const tone = verdictTone(gate.verdict)
  return (
    <div className={`chart-gate-wrap ${props.open ? 'open' : ''}`}>
      <button type="button" className={`chart-gate-row ${tone}`} onClick={props.onToggle} aria-expanded={props.open}>
        <div className="row between gap-2">
          <span className="chart-gate-q">{props.question}</span>
          <span className="row gap-1">
            <span title={`Filtro de operaciones: ${gate.verdict_label}. Puntuación ${Math.round(gate.score)}/100`}>
              <VerdictBadge verdict={gate.verdict} score={gate.score} label={gate.verdict_label || undefined} />
            </span>
            {props.open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </span>
        </div>
        {gate.summary && <div className="chart-gate-summary">{gate.summary}</div>}
      </button>
      {props.open && <GateChecklist gate={gate} />}
    </div>
  )
}

// ----------------------------------------------------------------------------- warnings

function Warnings({ warnings }: { warnings: NexusWarning[] }) {
  const list = [...warnings].sort((a, b) => {
    const ea = a.code === 'EARLY_EXIT' ? -1 : 0
    const eb = b.code === 'EARLY_EXIT' ? -1 : 0
    return ea - eb || (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3)
  })
  return (
    <div className="col gap-1">
      {list.map((w, i) => {
        const early = w.code === 'EARLY_EXIT'
        const Icon = early ? Siren : w.severity === 'info' ? Info : TriangleAlert
        return (
          <div key={`${w.code}-${i}`} className={`chart-warn ${w.severity} ${early ? 'early' : ''}`}>
            <Icon size={early ? 16 : 14} style={{ flex: 'none', marginTop: 1 }} />
            <div className="grow">
              {WARNING_LABEL[w.code] && <b>{WARNING_LABEL[w.code]}: </b>}
              {w.message}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ----------------------------------------------------------------------------- open position

function distLabel(level: number | null | undefined, price: number): string {
  if (!isFiniteNumber(level) || !isFiniteNumber(price) || price === 0) return ''
  return fmtPct(((level - price) / price) * 100, 2)
}

function PositionBlock({ pos, price }: { pos: PositionState; price: number }) {
  const long = pos.side === 'long'
  const ev = pos.ev_hold_r
  const closeNow = isFiniteNumber(ev) && ev < 0
  const hit = (n: number) => pos.tp_hits >= n
  return (
    <div className={`chart-pos ${long ? 'long' : 'short'}`}>
      <div className="row between">
        <span className="row gap-2">
          <Badge tone={long ? 'pos' : 'neg'}>{long ? 'LARGO' : 'CORTO'}</Badge>
          <b className="small">Posición NEXUS abierta</b>
        </span>
        <span className="tiny muted">{fmtInt(pos.bars_in_trade)} velas</span>
      </div>

      {closeNow ? (
        <div className="chart-close-now" role="alert">
          <OctagonX size={20} style={{ flex: 'none' }} />
          <div>
            <div className="chart-close-now-title">CERRAR AHORA</div>
            <div className="small">
              El valor esperado de mantener es <b>{fmtR(ev)}</b>: la operación se volvió mala y seguir dentro resta.
            </div>
          </div>
        </div>
      ) : (
        <div className="chart-hold-ok" title="Valor esperado de mantener la posición, en múltiplos de R">
          <CircleCheck size={14} /> Mantener · EV de mantener <b className="mono">{fmtR(ev)}</b>
        </div>
      )}

      <div className="kv chart-levels">
        <span>Entrada</span>
        <span className="mono">{fmtPrice(pos.entry_price)}</span>
        <span className="neg bold">SL · hasta aquí</span>
        <span className="mono neg bold">
          {fmtPrice(pos.sl)} <span className="tiny">{distLabel(pos.sl, price)}</span>
        </span>
        {[1, 2, 3].map((n) => {
          const level = n === 1 ? pos.tp1 : n === 2 ? pos.tp2 : pos.tp3
          return [
            <span key={`l${n}`}>
              TP{n} {hit(n) && <CircleCheck size={11} className="pos" style={{ verticalAlign: -1 }} />}
            </span>,
            <span key={`v${n}`} className={`mono ${hit(n) ? 'pos' : ''}`}>
              {fmtPrice(level)} <span className="tiny">{hit(n) ? 'alcanzado' : distLabel(level, price)}</span>
            </span>,
          ]
        })}
        {pos.trail !== null && isFiniteNumber(pos.trail) && (
          <>
            <span className="warn">Stop dinámico</span>
            <span className="mono warn">{fmtPrice(pos.trail)}</span>
          </>
        )}
        <span>Resultado</span>
        <span className="mono" style={{ color: toneColor(signTone(pos.unrealized_pct)) }}>
          {fmtPct(pos.unrealized_pct)} · {fmtR(pos.r_multiple)}
        </span>
        <span>Tamaño abierto</span>
        <span className="mono">{fmtProb(pos.size_left)}</span>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------------------- contributions & prediction

function Contributions({ items }: { items: Contribution[] }) {
  const sorted = useMemo(
    () => [...items].filter((c) => isFiniteNumber(c.contribution)).sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution)),
    [items],
  )
  const maxAbs = Math.max(1e-9, ...sorted.map((c) => Math.abs(c.contribution)))
  if (sorted.length === 0) return <div className="tiny muted">Sin datos de los expertos.</div>
  return (
    <div className="col gap-1">
      {sorted.map((c) => (
        <div
          key={c.expert}
          className="chart-contrib"
          title={`Señal ${fmtScore(c.signal)} × peso ${fmtNum(c.weight, 2)} = ${fmtScore(c.contribution)}`}
        >
          <span className="chart-contrib-label ellipsis">{c.label || c.expert}</span>
          <div className="chart-meter-c">
            <Meter value={c.contribution / maxAbs} centered tone={signTone(c.contribution)} height={5} />
          </div>
          <span className="mono tiny right" style={{ color: toneColor(signTone(c.contribution)) }}>
            {fmtScore(c.contribution)}
          </span>
        </div>
      ))}
      <div className="tiny muted">Barra a la derecha = empuja a comprar; a la izquierda = empuja a vender.</div>
    </div>
  )
}

function PredictionBlock({ pred, price }: { pred: Prediction; price: number }) {
  const last = Array.isArray(pred.cone) && pred.cone.length ? pred.cone[pred.cone.length - 1] : null
  return (
    <div className="kv">
      <span>Horizonte</span>
      <span className="mono">{fmtInt(pred.horizon_bars)} velas</span>
      <span>Probabilidad de subida</span>
      <span className="mono" style={{ color: toneColor(pred.prob_up >= 0.5 ? 'pos' : 'neg') }}>
        {fmtProb(pred.prob_up)}
      </span>
      <span>Movimiento esperado</span>
      <span className="mono" style={{ color: toneColor(signTone(pred.expected_return_pct)) }}>
        {fmtPct(pred.expected_return_pct)}
      </span>
      {last && (
        <>
          <span>Rango p10 – p90</span>
          <span className="mono">
            {fmtPrice(last.p10)} – {fmtPrice(last.p90)}
          </span>
          <span />
          <span className="mono tiny muted">
            {distLabel(last.p10, price)} / {distLabel(last.p90, price)}
          </span>
        </>
      )}
      <span>Método</span>
      <span className="tiny">{pred.method || '—'}</span>
    </div>
  )
}

// ----------------------------------------------------------------------------- panel

interface Props {
  nexus: NexusResponse | null
  loading: boolean
  error: unknown
  refreshing: boolean
  updatedAt: number | null
  tf: Timeframe
  onRetry: () => void
  onRefresh: () => void
  onOpenTradingView: () => void
}

function SignalPanel(props: Props) {
  const { nexus, tf } = props
  const [open, setOpen] = useState<'long' | 'short' | null>(null)

  if (!nexus) {
    return (
      <Card
        title={
          <>
            <Activity size={14} /> Señal NEXUS
          </>
        }
      >
        {props.error ? (
          <ErrorBox error={props.error} onRetry={props.onRetry} />
        ) : (
          <Loading label="Calculando la señal NEXUS…" />
        )}
      </Card>
    )
  }

  if (!nexus.ok) return <QuickSignalCard nexus={nexus} onOpenTradingView={props.onOpenTradingView} />

  const cur: NexusCurrent | null = nexus.current
  const refresh = (
    <IconButton
      size="sm"
      icon={props.refreshing ? <Spinner /> : <RefreshCw size={13} />}
      title="Recalcular NEXUS ahora"
      onClick={props.onRefresh}
      disabled={props.refreshing}
    />
  )

  if (!cur) {
    return (
      <Card title={<><Activity size={14} /> Señal NEXUS</>} actions={refresh}>
        <Empty title="Sin señal">NEXUS no devolvió una lectura para la última vela.</Empty>
      </Card>
    )
  }

  const stats = nexus.stats
  const learning = nexus.learning
  const rejectedWin = asFraction(stats?.rejected_would_win_rate)
  const rejectedLose = rejectedWin === null ? null : 1 - rejectedWin
  const winRate = asFraction(stats?.win_rate)
  const hitRate = asFraction(learning?.hit_rate)
  const regimeLabel = cur.regime_label || REGIME_LABEL[cur.regime] || cur.regime
  const toggle = (side: 'long' | 'short') => setOpen((o) => (o === side ? null : side))

  return (
    <Card
      className="chart-signal"
      title={
        <>
          <Activity size={14} /> Señal NEXUS
        </>
      }
      sub={`Vela ${fmtBarTime(cur.time, tf)}${props.updatedAt ? ` · ${fmtAgo(props.updatedAt)}` : ''}`}
      actions={refresh}
    >
      <div className="col gap-3">
        {/* hero */}
        <div className="chart-hero">
          <ActionBadge action={cur.action} label={cur.action_label || undefined} large />
          <div className="col gap-1 right">
            <Badge tone={regimeTone(cur.regime)} title="Régimen de mercado detectado por NEXUS">
              {regimeLabel}
            </Badge>
            <span className="tiny muted">
              Precio <b className="mono text-2">{fmtPrice(cur.price)}</b>
            </span>
          </div>
        </div>
        {cur.advice && <p className="chart-advice">{cur.advice}</p>}

        {/* the gate: when to take it and when NOT */}
        <div className="col gap-2">
          <div className="chart-gate-head">
            <ShieldCheck size={13} /> Filtro de operaciones · ¿la tomo?
          </div>
          <GateRow question="¿Comprar ahora?" gate={cur.gate?.long} open={open === 'long'} onToggle={() => toggle('long')} />
          <GateRow question="¿Vender ahora?" gate={cur.gate?.short} open={open === 'short'} onToggle={() => toggle('short')} />
        </div>

        {Array.isArray(cur.warnings) && cur.warnings.length > 0 && <Warnings warnings={cur.warnings} />}

        {cur.position && <PositionBlock pos={cur.position} price={cur.price} />}

        {/* meters */}
        <div className="col gap-2">
          <MeterRow
            label="Probabilidad de subida"
            value={fmtProb(cur.prob_up)}
            tone={toneColor(cur.prob_up >= 0.5 ? 'pos' : 'neg')}
            title="Probabilidad calibrada (aprendida) de que el precio suba en el horizonte de predicción"
          >
            <Meter value={cur.prob_up} tone={cur.prob_up >= 0.5 ? 'pos' : 'neg'} />
          </MeterRow>
          <MeterRow
            label="Puntuación NEXUS"
            value={fmtScore(cur.score)}
            tone={toneColor(signTone(cur.score))}
            title="De −1 (venta fuerte) a +1 (compra fuerte): mezcla del voto de los expertos y la probabilidad"
          >
            <div className="chart-meter-c">
              <Meter value={cur.score} centered tone={signTone(cur.score)} />
            </div>
          </MeterRow>
          <MeterRow
            label="Confianza"
            value={fmtProb(cur.confidence)}
            tone="var(--info)"
            title="Acuerdo entre expertos y calidad de calibración del modelo"
          >
            <Meter value={cur.confidence} tone="info" />
          </MeterRow>
        </div>

        {Array.isArray(cur.reasons) && cur.reasons.length > 0 && (
          <Section title="Por qué" icon={<Info size={13} />} open>
            <ul className="chart-list">
              {cur.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </Section>
        )}

        {Array.isArray(cur.contributions) && cur.contributions.length > 0 && (
          <Section title="Contribución de los expertos" icon={<Activity size={13} />}>
            <Contributions items={cur.contributions} />
          </Section>
        )}

        {nexus.prediction && (
          <Section title="Predicción" icon={<Target size={13} />} open>
            <PredictionBlock pred={nexus.prediction} price={cur.price} />
          </Section>
        )}

        {learning && (
          <Section title="Aprendizaje" icon={<Brain size={13} />}>
            <div className="kv">
              <span>Actualizaciones</span>
              <span className="mono">{fmtInt(learning.updates)}</span>
              <span title="Acierto de sus propias predicciones pasadas, fuera de muestra">Acierto (fuera de muestra)</span>
              <span className="mono">{fmtProb(hitRate, 1)}</span>
              <span title="Error cuadrático de las probabilidades: menor es mejor (0,25 = azar)">Brier</span>
              <span className="mono">{isFiniteNumber(learning.brier) ? fmtNum(learning.brier, 3) : '—'}</span>
              <span>Memoria</span>
              <span className="mono">{fmtInt(learning.memory_bars)} velas</span>
              {isFiniteNumber(learning.trained_until) && (
                <>
                  <span>Entrenado hasta</span>
                  <span className="mono">{fmtBarTime(learning.trained_until, tf)}</span>
                </>
              )}
            </div>
          </Section>
        )}

        {stats && (
          <Section title="Resultados de las señales" icon={<BookOpen size={13} />} open>
            <div className="kv">
              <span>Operaciones</span>
              <span className="mono">{fmtInt(stats.trades)}</span>
              <span>% ganadoras</span>
              <span className="mono">{fmtProb(winRate, 1)}</span>
              <span>Factor de beneficio</span>
              <span className="mono">{isFiniteNumber(stats.profit_factor) ? fmtNum(stats.profit_factor, 2) : '—'}</span>
              <span>Retorno total</span>
              <span className="mono" style={{ color: toneColor(signTone(stats.total_return_pct)) }}>
                {fmtPct(stats.total_return_pct)}
              </span>
              <span>Máx. caída</span>
              <span className="mono neg">{fmtPct(-Math.abs(stats.max_dd_pct ?? 0))}</span>
              <span>Señales rechazadas</span>
              <span className="mono">{fmtInt(stats.rejected_signals)}</span>
              <span className="bold">% de rechazadas que habrían perdido</span>
              <span className="mono bold" style={{ color: toneColor(rejectedLose !== null && rejectedLose >= 0.5 ? 'pos' : 'warn') }}>
                {fmtProb(rejectedLose, 1)}
              </span>
            </div>
            <p className="tiny muted chart-proof">
              {rejectedLose === null
                ? 'Aún no hay suficientes señales rechazadas para medir el filtro.'
                : `De las señales que el filtro NO dejó tomar, el ${fmtProb(rejectedLose, 0)} habría terminado en pérdida: esa es la prueba de que el filtro te protege${rejectedLose < 0.5 ? ' (por ahora no lo está demostrando en este activo)' : ''}.`}
            </p>
          </Section>
        )}

        <Disclaimer />
      </div>
    </Card>
  )
}

export default memo(SignalPanel)
