// "Estrategia" tab: backtest metrics, equity curve (vs. buy & hold, with drawdown) and the list of trades
// produced by strategy.entry / exit / close in a PyChart script.
import { memo, useEffect, useMemo, useRef } from 'react'
import { BaselineSeries, type ISeriesApi } from 'lightweight-charts'
import { FlaskConical } from 'lucide-react'
import type { BacktestMetrics, BacktestResult } from '../../api/types'
import { COLORS, LineStyle, addLine, timeScaleFor, ts, useChart } from '../../lib/chart'
import {
  DIRECTION_LABEL,
  EXIT_REASON_LABEL,
  fmtBarTime,
  fmtInt,
  fmtNum,
  fmtPct,
  fmtPrice,
  fmtR,
  signTone,
  type Tone,
} from '../../lib/format'
import { Badge, Disclaimer, Empty, Stat } from '../ui'
import { evidenceText, evidenceTone, filterEvidence, fmtTally } from '../brain/evidence'
import { ensureAscending, isFiniteNum } from './chartUtils'

function EquityChart({ result }: { result: BacktestResult }) {
  const { ref, chart } = useChart({ rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.08 } } })
  const series = useRef<{ equity: ISeriesApi<'Line'>; bench: ISeriesApi<'Line'>; dd: ISeriesApi<'Baseline'> } | null>(null)

  useEffect(() => {
    if (!chart) return
    const equity = addLine(chart, { color: COLORS.accent, width: 2, title: 'Capital', lastValue: true })
    const bench = addLine(chart, { color: COLORS.muted, style: LineStyle.Dashed, title: 'Comprar y mantener' })
    const dd = chart.addSeries(
      BaselineSeries,
      {
        baseValue: { type: 'price', price: 0 },
        topLineColor: 'rgba(0,0,0,0)',
        topFillColor1: 'rgba(0,0,0,0)',
        topFillColor2: 'rgba(0,0,0,0)',
        bottomLineColor: COLORS.neg,
        bottomFillColor1: 'rgba(234, 57, 67, 0.08)',
        bottomFillColor2: 'rgba(234, 57, 67, 0.4)',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: true,
        title: 'Drawdown %',
      },
      1,
    )
    const panes = chart.panes()
    if (panes.length > 1) {
      panes[0].setStretchFactor(3)
      panes[1].setStretchFactor(1)
    }
    series.current = { equity, bench, dd }
    return () => {
      series.current = null
    }
  }, [chart])

  useEffect(() => {
    const s = series.current
    if (!chart || !s) return
    // Bar times: 1d / 1w runs keep their UTC session dates (see lib/chart.ts `ts`).
    const tf = result.tf
    chart.applyOptions({ timeScale: timeScaleFor(tf) })
    const eq = (result.equity ?? []).filter((p) => isFiniteNum(p.time) && isFiniteNum(p.equity))
    s.equity.setData(ensureAscending(eq.map((p) => ({ time: ts(p.time, tf), value: p.equity }))))
    s.dd.setData(
      ensureAscending(
        eq.filter((p) => isFiniteNum(p.drawdown_pct)).map((p) => ({ time: ts(p.time, tf), value: -Math.abs(p.drawdown_pct) })),
      ),
    )
    const bench = (result.benchmark ?? []).filter((p) => isFiniteNum(p.time) && isFiniteNum(p.equity))
    s.bench.setData(ensureAscending(bench.map((p) => ({ time: ts(p.time, tf), value: p.equity }))))
    chart.timeScale().fitContent()
  }, [chart, result])

  return (
    <div className="studio-equity">
      <div ref={ref} className="studio-chart-canvas" />
      <div className="legend">
        <span className="legend-item">
          <span className="swatch" style={{ background: COLORS.accent }} /> Capital
        </span>
        <span className="legend-item">
          <span className="swatch" style={{ background: COLORS.muted }} /> Comprar y mantener
        </span>
        <span className="legend-item">
          <span className="swatch" style={{ background: COLORS.neg }} /> Drawdown (panel inferior)
        </span>
      </div>
    </div>
  )
}

interface MetricDef {
  label: string
  value: (m: BacktestMetrics) => string
  tone?: (m: BacktestMetrics) => Tone
  hint?: string
}

const pctOrRate = (v: number | null | undefined) => {
  if (!isFiniteNum(v)) return '—'
  return Math.abs(v) <= 1 ? fmtPct(v * 100, 1, false) : fmtPct(v, 1, false)
}

/** Difference of two percentages, in percentage points: "+3,21 pp". */
const fmtPp = (v: number) => `${v > 0 ? '+' : ''}${fmtNum(v, 2)} pp`

/** Rejected signals vs the trades that were taken: "x de n" and no conclusion below 30 cases (brain/evidence.ts). */
function RejectedSummary({ m }: { m: BacktestMetrics }) {
  const rejected = isFiniteNum(m.rejected_signals) ? m.rejected_signals : 0
  const e = filterEvidence({
    total: rejected,
    resolved: m.rejected_resolved,
    winRate: m.rejected_would_win_rate,
    takenN: m.trades,
    takenWinRate: m.win_rate,
  })
  return (
    <>
      <span className="tiny muted">
        Señales rechazadas: {fmtInt(rejected)}
        {e.rejected && <> ({fmtInt(e.rejected.n)} con resultado conocido)</>}
        {e.verdict !== 'none' && (
          <>
            {' · '}habrían ganado <span className={`mono ${evidenceTone(e)}`}>{fmtTally(e.rejected, e.rate)}</span>
          </>
        )}
        {isFiniteNum(m.gate_lift_pct) && (
          <>
            {' · '}
            <span title="Rendimiento con filtro − rendimiento sin filtro, en puntos porcentuales (pp)">
              diferencia por el filtro <span className={`mono ${signTone(m.gate_lift_pct)}`}>{fmtPp(m.gate_lift_pct)}</span>
            </span>
          </>
        )}
      </span>
      {rejected > 0 && <span className="tiny muted">{evidenceText(e, 'signals')}</span>}
    </>
  )
}

const METRICS: MetricDef[] = [
  { label: 'Retorno total', value: (m) => fmtPct(m.total_return_pct), tone: (m) => signTone(m.total_return_pct) },
  { label: 'Comprar y mantener', value: (m) => fmtPct(m.buy_hold_return_pct), tone: (m) => signTone(m.buy_hold_return_pct) },
  { label: 'CAGR', value: (m) => fmtPct(m.cagr_pct), tone: (m) => signTone(m.cagr_pct) },
  { label: 'Máx. drawdown', value: (m) => fmtPct(-Math.abs(m.max_dd_pct)), tone: () => 'neg' },
  { label: 'Operaciones', value: (m) => fmtInt(m.trades) },
  { label: '% acierto', value: (m) => pctOrRate(m.win_rate) },
  { label: 'Profit factor', value: (m) => fmtNum(m.profit_factor), tone: (m) => (isFiniteNum(m.profit_factor) ? (m.profit_factor >= 1 ? 'pos' : 'neg') : 'muted') },
  { label: 'Expectativa', value: (m) => fmtPct(m.expectancy_pct), tone: (m) => signTone(m.expectancy_pct) },
  { label: 'R medio', value: (m) => fmtR(m.avg_r), tone: (m) => signTone(m.avg_r) },
  { label: 'Sharpe', value: (m) => fmtNum(m.sharpe) },
  { label: 'Sortino', value: (m) => fmtNum(m.sortino) },
  { label: 'Calmar', value: (m) => fmtNum(m.calmar) },
  { label: 'Ganancia media', value: (m) => fmtPct(m.avg_win_pct), tone: () => 'pos' },
  { label: 'Pérdida media', value: (m) => fmtPct(m.avg_loss_pct), tone: () => 'neg' },
  { label: 'Mejor operación', value: (m) => fmtPct(m.best_trade_pct), tone: (m) => signTone(m.best_trade_pct) },
  { label: 'Peor operación', value: (m) => fmtPct(m.worst_trade_pct), tone: (m) => signTone(m.worst_trade_pct) },
  { label: 'Exposición', value: (m) => fmtPct(m.exposure_pct, 1, false), hint: 'tiempo con posición abierta' },
  { label: 'Velas por operación', value: (m) => fmtNum(m.avg_bars_in_trade, 1) },
  { label: 'Pérdidas seguidas (máx.)', value: (m) => fmtInt(m.max_consecutive_losses) },
  { label: 'Comisiones pagadas', value: (m) => fmtNum(m.fees_paid) },
]

function StrategyPanel({ result }: { result: BacktestResult | null }) {
  const trades = useMemo(() => (result ? [...(result.trades ?? [])].sort((a, b) => b.entry_time - a.entry_time) : []), [result])

  if (!result) {
    return (
      <Empty icon={<FlaskConical size={28} />} title="Sin backtest todavía">
        Ejecuta el script (Ctrl+Enter). Las estrategias declaradas con <span className="mono">strategy(...)</span> muestran aquí sus
        métricas, la curva de capital y cada operación.
      </Empty>
    )
  }
  const m = result.metrics
  const ungated = result.ungated
  return (
    <div className="studio-strategy scroll">
      <div className="studio-strategy-head">
        <span className="small text-2">
          {result.symbol} · {result.tf} · {fmtInt(result.bars)} velas · {fmtBarTime(result.start_time, result.tf)} → {fmtBarTime(result.end_time, result.tf)}
        </span>
        <span className="small text-2">
          Capital inicial <span className="mono">{fmtNum(m.initial_capital)}</span> → final{' '}
          <span className={`mono ${signTone(m.final_equity - m.initial_capital)}`}>{fmtNum(m.final_equity)}</span>
        </span>
      </div>
      <div className="studio-metrics">
        {METRICS.map((d) => (
          <Stat key={d.label} label={d.label} value={d.value(m)} tone={d.tone?.(m)} hint={d.hint} />
        ))}
      </div>
      {ungated && (
        <div className="studio-compare">
          <strong className="small">Con filtro “¿Tomo la operación?” vs. sin filtro</strong>
          <table className="table">
            <thead>
              <tr>
                <th />
                <th className="num">Retorno</th>
                <th className="num">Operaciones</th>
                <th className="num">% acierto</th>
                <th className="num">Máx. DD</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Con filtro</td>
                <td className={`num ${signTone(m.total_return_pct)}`}>{fmtPct(m.total_return_pct)}</td>
                <td className="num">{fmtInt(m.trades)}</td>
                <td className="num">{pctOrRate(m.win_rate)}</td>
                <td className="num neg">{fmtPct(-Math.abs(m.max_dd_pct))}</td>
              </tr>
              <tr>
                <td>Sin filtro</td>
                <td className={`num ${signTone(ungated.total_return_pct)}`}>{fmtPct(ungated.total_return_pct)}</td>
                <td className="num">{fmtInt(ungated.trades)}</td>
                <td className="num">{pctOrRate(ungated.win_rate)}</td>
                <td className="num neg">{fmtPct(-Math.abs(ungated.max_dd_pct))}</td>
              </tr>
            </tbody>
          </table>
          <RejectedSummary m={m} />
        </div>
      )}
      <div className="studio-equity-wrap">
        <EquityChart result={result} />
      </div>
      <div className="studio-trades">
        <div className="row between" style={{ padding: '8px 10px' }}>
          <strong className="small">Operaciones ({fmtInt(trades.length)})</strong>
          <span className="tiny muted">más recientes primero</span>
        </div>
        {trades.length ? (
          <div className="table-wrap" style={{ maxHeight: 320 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Lado</th>
                  <th>Entrada</th>
                  <th>Salida</th>
                  <th>Motivo</th>
                  <th className="num">P&amp;L</th>
                  <th className="num">R</th>
                  <th className="num">Velas</th>
                </tr>
              </thead>
              <tbody>
                {trades.slice(0, 500).map((t, i) => (
                  <tr key={`${t.id}-${t.entry_time}-${i}`}>
                    <td className="muted">{t.id}</td>
                    <td>
                      <Badge tone={t.side === 'long' ? 'pos' : 'neg'}>{DIRECTION_LABEL[t.side] ?? t.side}</Badge>
                    </td>
                    <td className="studio-cell2">
                      <span className="mono">{fmtBarTime(t.entry_time, result.tf)}</span>
                      <span className="mono muted">{fmtPrice(t.entry_price)}</span>
                    </td>
                    <td className="studio-cell2">
                      <span className="mono">{t.exit_time ? fmtBarTime(t.exit_time, result.tf) : '—'}</span>
                      <span className="mono muted">{fmtPrice(t.exit_price)}</span>
                    </td>
                    <td className="studio-wrap" style={{ minWidth: 90 }}>
                      {t.exit_label ?? (t.exit_reason ? EXIT_REASON_LABEL[t.exit_reason] ?? t.exit_reason : 'Abierta')}
                    </td>
                    <td className={`num ${signTone(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td>
                    <td className={`num ${signTone(t.r_multiple)}`}>{fmtR(t.r_multiple)}</td>
                    <td className="num">{fmtInt(t.bars)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="studio-problem muted">La estrategia no generó operaciones en este periodo.</div>
        )}
      </div>
      <Disclaimer />
    </div>
  )
}

export default memo(StrategyPanel)
