// "Efecto del filtro": gated vs ungated backtest, so the user SEES what saying NO to doubtful trades changed in this
// period. Differences of percentages are percentage points (pp); the rejected signals are compared with the taken
// trades and never sold as proof on a small sample.
import { Info, ShieldCheck } from 'lucide-react'
import type { BacktestMetrics } from '../../api/types'
import { fmtInt, fmtNum, fmtPct, type Tone } from '../../lib/format'
import { Card } from '../ui'
import { evidenceText, evidenceTone, filterEvidence, fmtTally } from '../brain/evidence'
import { asFraction, ddValue } from './labels'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

interface Row {
  label: string
  title?: string
  gated: number | null
  ungated: number | null
  fmt: (v: number | null) => string
  diff: (d: number) => string
  /** Which direction is an improvement. */
  better: 'higher' | 'lower' | 'none'
}

const signed = (v: number, digits: number, suffix = '') =>
  `${v > 0 ? '+' : ''}${v.toLocaleString('es-ES', { minimumFractionDigits: digits, maximumFractionDigits: digits })}${suffix}`

const num = (v: number | null | undefined): number | null => (isNum(v) ? v : null)

function diffTone(row: Row, d: number): Tone {
  if (row.better === 'none' || Math.abs(d) < 1e-9) return 'muted'
  const improved = row.better === 'higher' ? d > 0 : d < 0
  return improved ? 'pos' : 'neg'
}

function buildRows(g: BacktestMetrics, u: BacktestMetrics): Row[] {
  const win = (m: BacktestMetrics) => {
    const f = asFraction(m.win_rate)
    return f === null ? null : f * 100
  }
  return [
    {
      label: 'Rendimiento',
      gated: num(g.total_return_pct),
      ungated: num(u.total_return_pct),
      fmt: (v) => fmtPct(v),
      diff: (d) => signed(d, 2, ' pp'),
      better: 'higher',
    },
    {
      label: 'Profit factor',
      gated: num(g.profit_factor),
      ungated: num(u.profit_factor),
      fmt: (v) => fmtNum(v, 2),
      diff: (d) => signed(d, 2),
      better: 'higher',
    },
    {
      label: '% acierto',
      gated: win(g),
      ungated: win(u),
      fmt: (v) => fmtPct(v, 1, false),
      diff: (d) => signed(d, 1, ' pp'),
      better: 'higher',
    },
    {
      label: 'Max DD',
      title: 'Peor caída desde un máximo (menos negativo = mejor)',
      gated: ddValue(g.max_dd_pct),
      ungated: ddValue(u.max_dd_pct),
      fmt: (v) => fmtPct(v),
      diff: (d) => signed(d, 2, ' pp'),
      better: 'higher',
    },
    {
      label: 'Operaciones',
      gated: num(g.trades),
      ungated: num(u.trades),
      fmt: (v) => fmtInt(v),
      diff: (d) => signed(d, 0),
      better: 'none',
    },
    {
      label: 'Expectativa / operación',
      gated: num(g.expectancy_pct),
      ungated: num(u.expectancy_pct),
      fmt: (v) => fmtPct(v),
      diff: (d) => signed(d, 2, ' pp'),
      better: 'higher',
    },
    {
      label: 'Sharpe',
      gated: num(g.sharpe),
      ungated: num(u.sharpe),
      fmt: (v) => fmtNum(v, 2),
      diff: (d) => signed(d, 2),
      better: 'higher',
    },
  ]
}

function RejectedLine(props: { metrics: BacktestMetrics }) {
  const m = props.metrics
  const rejected = num(m.rejected_signals) ?? 0
  // Rejected signals vs the trades this run did take ("x de n"; no conclusion below 30 cases or inside the 95 % margin).
  const e = filterEvidence({
    total: rejected,
    resolved: m.rejected_resolved,
    winRate: m.rejected_would_win_rate,
    takenN: m.trades,
    takenWinRate: m.win_rate,
  })
  const note = rejected === 0 ? 'El filtro no rechazó ninguna señal en este periodo.' : evidenceText(e, 'signals')
  return (
    <div className="sim-rejected">
      <div>
        Señales rechazadas: <b className="mono">{fmtInt(rejected)}</b>
        {e.rejected && <span className="muted"> ({fmtInt(e.rejected.n)} con resultado conocido)</span>}
        {e.verdict !== 'none' && (
          <>
            {' · '}habrían ganado <b className={`mono ${evidenceTone(e)}`}>{fmtTally(e.rejected, e.rate)}</b>
            {e.takenRate !== null && (
              <>
                {' · '}tomadas que ganaron <b className="mono">{fmtTally(e.taken, e.takenRate)}</b>
              </>
            )}
          </>
        )}
      </div>
      <div className="tiny muted">{note}</div>
    </div>
  )
}

export function GateEffect(props: { metrics: BacktestMetrics; ungated: BacktestMetrics | null; usedGate: boolean }) {
  const g = props.metrics
  const u = props.ungated

  if (!props.usedGate) {
    return (
      <Card title={<><ShieldCheck size={14} /> Efecto del filtro</>} sub="¿Qué cambia al decir NO a las operaciones dudosas?">
        <div className="notice">
          <Info size={15} style={{ flex: 'none', marginTop: 1 }} />
          <div>
            Este backtest se ejecutó <b>sin filtro</b>: se tomaron todas las señales de NEXUS. Activa «Usar filtro (gate)» y vuelve a
            ejecutarlo para ver cuánto cambia el resultado cuando solo se toman las operaciones con veredicto TOMAR.
          </div>
        </div>
      </Card>
    )
  }

  const lift = num(g.gate_lift_pct) ?? (u && isNum(g.total_return_pct) && isNum(u.total_return_pct) ? g.total_return_pct - u.total_return_pct : null)
  const liftTone: Tone = lift === null || Math.abs(lift) < 1e-9 ? 'muted' : lift > 0 ? 'pos' : 'neg'
  const liftText =
    lift === null
      ? 'Sin variante sin filtro para comparar.'
      : lift > 0
        ? 'Decir NO a las señales dudosas mejoró el resultado en este periodo.'
        : lift < 0
          ? 'En este periodo el filtro fue demasiado estricto: sin él se habría ganado más.'
          : 'El filtro no cambió el resultado en este periodo.'

  return (
    <Card title={<><ShieldCheck size={14} /> Efecto del filtro</>} sub="Con filtro (solo TOMAR) frente a sin filtro (todas las señales)">
      <div className="col gap-3">
        <div className={`sim-lift ${liftTone}`}>
          <div className="col gap-0">
            <span className="tiny muted nowrap" title="Rendimiento con filtro − rendimiento sin filtro, en puntos porcentuales (pp)">
              Diferencia por el filtro
            </span>
            <span className={`sim-lift-value ${liftTone}`}>{lift === null ? '—' : signed(lift, 2, ' pp')}</span>
          </div>
          <div className="small">{liftText}</div>
        </div>

        {u ? (
          <div className="table-wrap">
            <table className="table sim-compare">
              <thead>
                <tr>
                  <th>Métrica</th>
                  <th className="num">Con filtro</th>
                  <th className="num">Sin filtro</th>
                  <th className="num" title="Con filtro − sin filtro (pp = puntos porcentuales)">
                    Diferencia
                  </th>
                </tr>
              </thead>
              <tbody>
                {buildRows(g, u).map((row) => {
                  const d = row.gated !== null && row.ungated !== null ? row.gated - row.ungated : null
                  return (
                    <tr key={row.label}>
                      <td title={row.title}>{row.label}</td>
                      <td className="num mono bold">{row.fmt(row.gated)}</td>
                      <td className="num mono">{row.fmt(row.ungated)}</td>
                      <td className={`num mono ${d === null ? 'muted' : diffTone(row, d)}`}>{d === null ? '—' : row.diff(d)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="notice">
            <Info size={15} style={{ flex: 'none', marginTop: 1 }} />
            <div>El servidor no devolvió la variante sin filtro; solo se muestran los datos con filtro.</div>
          </div>
        )}

        <RejectedLine metrics={g} />
      </div>
    </Card>
  )
}
