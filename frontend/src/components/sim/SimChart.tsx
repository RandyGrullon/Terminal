// Live simulator chart: candles + volume + EMA lines + NEXUS markers + SL "hasta aquí" / TP price lines.
import { memo, useEffect, useRef, useState } from 'react'
import type { MouseEventParams, Time } from 'lightweight-charts'
import { FlaskConical, MousePointer2 } from 'lucide-react'
import type { Candle } from '../../api/types'
import { fromTs, useChart } from '../../lib/chart'
import { fmtCompact, fmtPrice, splitSymbol } from '../../lib/format'
import { Spinner } from '../ui'
import { LiveChartView, type EmaLegend } from './liveModel'
import { MarkerTooltip, type MarkerHover } from './MarkerTooltip'
import type { SimFeed } from './simFeed'

interface Props {
  feed: SimFeed
  emas: EmaLegend[]
  lastBar: Candle | null
  bars: number
  loading: boolean
  symbol: string | null
  tf: string | null
}

export const SimChart = memo(function SimChart(props: Props) {
  const { feed } = props
  const { ref, chart } = useChart()
  const [hover, setHover] = useState<MarkerHover | null>(null)
  const [hoverBar, setHoverBar] = useState<Candle | null>(null)
  const hoverTime = useRef<number | null>(null)

  useEffect(() => {
    if (!chart) return
    const view = new LiveChartView(chart)
    feed.attachView(view)
    const onMove = (param: MouseEventParams<Time>) => {
      const el = ref.current
      if (param.time === undefined || !param.point || !el) {
        if (hoverTime.current !== null) {
          hoverTime.current = null
          setHover(null)
          setHoverBar(null)
        }
        return
      }
      const t = fromTs(param.time)
      const markers = feed.model.markersAt(t)
      hoverTime.current = t
      setHoverBar(feed.model.candleAt(t))
      setHover(
        markers.length
          ? { x: param.point.x, y: param.point.y, width: el.clientWidth, height: el.clientHeight, time: t, markers }
          : null,
      )
    }
    chart.subscribeCrosshairMove(onMove)
    return () => {
      // useChart's cleanup has already removed the chart at this point (it runs first): only detach the view from the
      // feed. Touching series now (price lines, marker plugin) would schedule a redraw on a disposed chart.
      try {
        chart.unsubscribeCrosshairMove(onMove)
      } catch {
        /* chart already removed */
      }
      feed.attachView(null)
    }
  }, [chart, feed, ref])

  const bar = hoverBar ?? props.lastBar
  const ticker = props.symbol ? splitSymbol(props.symbol).ticker || props.symbol : null
  const up = bar ? bar.close >= bar.open : true

  return (
    <div className="chart-box sim-chart">
      <div ref={ref} className="sim-chart-canvas" />
      {props.bars > 0 && (
        <div className="legend">
          <div className="legend-item">
            {ticker && <b>{ticker}</b>}
            {props.tf && <span className="muted">{props.tf}</span>}
            {bar && (
              <span className={`mono ${up ? 'pos' : 'neg'}`}>
                A {fmtPrice(bar.open)} · M {fmtPrice(bar.high)} · m {fmtPrice(bar.low)} · C {fmtPrice(bar.close)}
                <span className="muted"> · Vol {fmtCompact(bar.volume)}</span>
              </span>
            )}
          </div>
          {props.emas.map((e) => (
            <div key={e.key} className="legend-item">
              <span className="swatch" style={{ background: e.color }} />
              {e.label}
            </div>
          ))}
          <div className="legend-item sim-chart-hint">
            <MousePointer2 size={11} /> Cursor sobre una marca: por qué NEXUS entró, salió o rechazó la operación
          </div>
        </div>
      )}
      <MarkerTooltip hover={hover} tf={props.tf} />
      {props.bars === 0 && (
        <div className="chart-overlay">
          {props.loading ? (
            <div className="empty">
              <Spinner large />
              <span>Cargando el estado del simulador…</span>
            </div>
          ) : (
            <div className="empty sim-empty">
              <FlaskConical size={30} />
              <strong style={{ color: 'var(--text-2)' }}>No hay ninguna simulación en curso</strong>
              <div className="small">
                Elige <b>Sintético</b> (mercado generado con el régimen que quieras) o <b>Replay histórico</b> (velas reales reveladas
                una a una) y pulsa <b>Iniciar</b>. Verás en directo cuándo el filtro dice TOMAR y cuándo NO TOMAR.
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
})
