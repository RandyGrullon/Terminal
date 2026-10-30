// Out-of-sample accuracy over time: hit rate (%) on top with a 50 % "coin flip" reference and the Brier score
// below with its 0.25 reference. Used for the global timeline and for a single NEXUS model's rolling stats.
import { memo, useEffect, useRef } from 'react'
import type { ISeriesApi } from 'lightweight-charts'
import { COLORS, LevelLines, LineStyle, addLine, ts, useChart } from '../../lib/chart'
import { Empty } from '../ui'
import { ensureAscending, toSec } from '../studio/chartUtils'
import { ratePct } from './learnFormat'

export interface TimelinePoint {
  time: number
  hit_rate: number | null
  brier: number | null
  n?: number
}

interface Series {
  hit: ISeriesApi<'Line'>
  brier: ISeriesApi<'Line'>
  hitRef: LevelLines
  brierRef: LevelLines
}

function TimelineChart({ points, height = 260 }: { points: TimelinePoint[]; height?: number }) {
  const { ref, chart } = useChart({ timeScale: { timeVisible: true, rightOffset: 2 } })
  const series = useRef<Series | null>(null)

  useEffect(() => {
    if (!chart) return
    const hit = addLine(chart, { color: COLORS.accent, width: 2, title: 'Acierto', lastValue: true, pane: 0 })
    hit.applyOptions({ priceFormat: { type: 'custom', minMove: 0.1, formatter: (v: number) => `${v.toFixed(1)} %` } })
    const brier = addLine(chart, { color: COLORS.purple, width: 2, title: 'Brier', lastValue: true, pane: 1 })
    brier.applyOptions({ priceFormat: { type: 'custom', minMove: 0.001, formatter: (v: number) => v.toFixed(3) } })
    const hitRef = new LevelLines(hit)
    const brierRef = new LevelLines(brier)
    hitRef.set([{ price: 50, color: COLORS.muted, title: 'azar 50 %', style: LineStyle.Dashed }])
    brierRef.set([{ price: 0.25, color: COLORS.muted, title: 'azar 0,25', style: LineStyle.Dashed }])
    const panes = chart.panes()
    if (panes.length > 1) {
      panes[0].setStretchFactor(3)
      panes[1].setStretchFactor(2)
    }
    series.current = { hit, brier, hitRef, brierRef }
    return () => {
      series.current = null
    }
  }, [chart])

  useEffect(() => {
    const s = series.current
    if (!chart || !s) return
    const clean = points.filter((p) => typeof p.time === 'number' && Number.isFinite(p.time))
    s.hit.setData(
      ensureAscending(
        clean.flatMap((p) => {
          const v = ratePct(p.hit_rate)
          return v === null ? [] : [{ time: ts(toSec(p.time)), value: v }]
        }),
      ),
    )
    s.brier.setData(
      ensureAscending(
        clean.flatMap((p) =>
          typeof p.brier === 'number' && Number.isFinite(p.brier) ? [{ time: ts(toSec(p.time)), value: p.brier }] : [],
        ),
      ),
    )
    chart.timeScale().fitContent()
  }, [chart, points])

  return (
    <div className="brain-timeline" style={{ height }}>
      <div ref={ref} className="brain-chart-canvas" />
      <div className="legend">
        <span className="legend-item">
          <span className="swatch" style={{ background: COLORS.accent }} /> Acierto (%)
        </span>
        <span className="legend-item">
          <span className="swatch" style={{ background: COLORS.purple }} /> Brier (panel inferior · menor es mejor)
        </span>
      </div>
      {!points.length && (
        <div className="chart-overlay brain-chart-empty">
          <Empty title="Aún no hay predicciones resueltas">La curva aparece cuando las primeras predicciones lleguen a su horizonte.</Empty>
        </div>
      )}
    </div>
  )
}

export default memo(TimelineChart)
