// Backtest equity curve vs buy & hold, with the drawdown histogram in a second pane.
import { useEffect, useMemo, useRef, useState } from 'react'
import { HistogramSeries, type ISeriesApi, type MouseEventParams, type Time } from 'lightweight-charts'
import type { BacktestResult } from '../../api/types'
import { COLORS, LineStyle, addLine, fromTs, ts, useChart } from '../../lib/chart'
import { fmtBarTime, fmtNum, fmtPct } from '../../lib/format'
import { Card } from '../ui'
import { ddValue } from './labels'
import { sortDedupeByTime } from './series'

interface Layers {
  equity: ISeriesApi<'Line'>
  bench: ISeriesApi<'Line'>
  dd: ISeriesApi<'Histogram'>
}

interface Point {
  time: number
  equity: number | null
  bench: number | null
  dd: number | null
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pctFormatter = (v: number) => `${fmtNum(v, 1)} %`

function valueOf(param: MouseEventParams<Time>, s: ISeriesApi<'Line'> | ISeriesApi<'Histogram'>): number | null {
  const d = param.seriesData.get(s) as { value?: number } | undefined
  return d && isNum(d.value) ? d.value : null
}

export function EquityChart(props: { result: BacktestResult }) {
  const { result } = props
  const { ref, chart } = useChart({ rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.08 } } })
  const layers = useRef<Layers | null>(null)
  const [hover, setHover] = useState<Point | null>(null)

  const data = useMemo(() => {
    const eq = sortDedupeByTime(result.equity.filter((p) => p && isNum(p.time) && isNum(p.equity)))
    const bench = sortDedupeByTime(result.benchmark.filter((p) => p && isNum(p.time) && isNum(p.equity)))
    return { eq, bench }
  }, [result])

  const last: Point | null = useMemo(() => {
    const e = data.eq[data.eq.length - 1]
    const b = data.bench[data.bench.length - 1]
    if (!e && !b) return null
    return { time: e?.time ?? b?.time ?? 0, equity: e?.equity ?? null, bench: b?.equity ?? null, dd: e ? ddValue(e.drawdown_pct) : null }
  }, [data])

  useEffect(() => {
    if (!chart) return
    const bench = addLine(chart, { color: COLORS.muted, width: 1, style: LineStyle.Dashed })
    const equity = addLine(chart, { color: COLORS.accent, width: 2, lastValue: true })
    const fmt = { type: 'price' as const, precision: 2, minMove: 0.01 }
    equity.applyOptions({ priceFormat: fmt })
    bench.applyOptions({ priceFormat: fmt })
    const dd = chart.addSeries(
      HistogramSeries,
      {
        color: 'rgba(234, 57, 67, 0.65)',
        priceFormat: { type: 'custom', formatter: pctFormatter, minMove: 0.01 },
        priceLineVisible: false,
        lastValueVisible: false,
      },
      1,
    )
    const panes = chart.panes()
    panes[0]?.setStretchFactor(3)
    panes[1]?.setStretchFactor(1)
    layers.current = { equity, bench, dd }
    const onMove = (param: MouseEventParams<Time>) => {
      const l = layers.current
      if (!l || param.time === undefined) {
        setHover(null)
        return
      }
      setHover({ time: fromTs(param.time), equity: valueOf(param, l.equity), bench: valueOf(param, l.bench), dd: valueOf(param, l.dd) })
    }
    chart.subscribeCrosshairMove(onMove)
    return () => {
      try {
        chart.unsubscribeCrosshairMove(onMove)
      } catch {
        /* chart already removed */
      }
      layers.current = null
    }
  }, [chart])

  useEffect(() => {
    const l = layers.current
    if (!chart || !l) return
    try {
      l.equity.setData(data.eq.map((p) => ({ time: ts(p.time), value: p.equity })))
      l.bench.setData(data.bench.map((p) => ({ time: ts(p.time), value: p.equity })))
      l.dd.setData(data.eq.map((p) => ({ time: ts(p.time), value: ddValue(p.drawdown_pct) ?? 0 })))
      chart.timeScale().fitContent()
    } catch (err) {
      console.error('No se pudo dibujar la curva de capital', err)
    }
  }, [chart, data])

  const shown = hover ?? last
  const initial = result.metrics.initial_capital
  const rel = (v: number | null) => (v !== null && isNum(initial) && initial > 0 ? fmtPct((v / initial - 1) * 100) : '—')

  return (
    <Card
      title="Curva de capital"
      sub={hover ? fmtBarTime(hover.time, result.tf) : 'Estrategia frente a comprar y mantener · caída (drawdown) abajo'}
      flush
    >
      <div className="chart-box sim-chart-frame sim-h-equity">
        <div ref={ref} className="sim-chart-canvas" />
        {data.eq.length > 0 && shown && (
          <div className="legend">
            <div className="legend-item">
              <span className="swatch" style={{ background: COLORS.accent }} /> Estrategia{' '}
              <b className="mono">{fmtNum(shown.equity, 2)}</b> <span className="muted mono">({rel(shown.equity)})</span>
            </div>
            <div className="legend-item">
              <span className="swatch" style={{ background: COLORS.muted }} /> Comprar y mantener{' '}
              <b className="mono">{fmtNum(shown.bench, 2)}</b> <span className="muted mono">({rel(shown.bench)})</span>
            </div>
            <div className="legend-item">
              <span className="swatch" style={{ background: COLORS.neg }} /> Drawdown <b className="mono neg">{fmtPct(shown.dd)}</b>
            </div>
          </div>
        )}
        {data.eq.length === 0 && (
          <div className="chart-overlay">
            <span className="muted small">El backtest no devolvió curva de capital.</span>
          </div>
        )}
      </div>
    </Card>
  )
}
