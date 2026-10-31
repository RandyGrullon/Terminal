// Out-of-sample accuracy over time: hit rate (%) on top and the Brier score below. Predictions have three outcomes
// (up, down, neutral inside the flat band), so there is no fixed "coin flip" line: when the points carry their
// reference (the hit rate of always predicting that day's most frequent outcome, and the climatology Brier of that
// day) it is drawn dashed next to each series. Used for the global timeline (one point per UTC day: tf '1d') and for
// a single NEXUS model's rolling stats (bar times of the model's timeframe). Times go through lib/chart `ts(sec, tf)`,
// so daily points keep their UTC date instead of showing as the previous day at 20:00 in UTC-4.
import { memo, useEffect, useRef } from 'react'
import type { ISeriesApi } from 'lightweight-charts'
import { COLORS, LineStyle, addLine, timeScaleFor, ts, useChart } from '../../lib/chart'
import { Empty } from '../ui'
import { ensureAscending, toSec } from '../studio/chartUtils'
import { ratePct } from './learnFormat'

export interface TimelinePoint {
  time: number
  hit_rate: number | null
  brier: number | null
  n?: number
  /** Hit rate of always predicting the most frequent outcome of the same rows (0..1). */
  reference?: number | null
  /** Brier score of climatology (always the period's up rate) over the same rows. */
  brier_ref?: number | null
}

interface Series {
  hit: ISeriesApi<'Line'>
  brier: ISeriesApi<'Line'>
  hitRef: ISeriesApi<'Line'>
  brierRef: ISeriesApi<'Line'>
}

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function TimelineChart({ points, height = 260, tf }: { points: TimelinePoint[]; height?: number; tf?: string }) {
  const { ref, chart } = useChart({ timeScale: { ...timeScaleFor(tf), rightOffset: 2 } })
  const series = useRef<Series | null>(null)

  useEffect(() => {
    if (!chart) return
    const hit = addLine(chart, { color: COLORS.accent, width: 2, title: 'Acierto', lastValue: true, pane: 0 })
    hit.applyOptions({ priceFormat: { type: 'custom', minMove: 0.1, formatter: (v: number) => `${v.toFixed(1)} %` } })
    const hitRef = addLine(chart, { color: COLORS.muted, width: 1, title: 'Referencia', style: LineStyle.Dashed, pane: 0 })
    hitRef.applyOptions({ priceFormat: { type: 'custom', minMove: 0.1, formatter: (v: number) => `${v.toFixed(1)} %` } })
    const brier = addLine(chart, { color: COLORS.purple, width: 2, title: 'Brier', lastValue: true, pane: 1 })
    brier.applyOptions({ priceFormat: { type: 'custom', minMove: 0.001, formatter: (v: number) => v.toFixed(3) } })
    const brierRef = addLine(chart, { color: COLORS.muted, width: 1, title: 'Climatología', style: LineStyle.Dashed, pane: 1 })
    brierRef.applyOptions({ priceFormat: { type: 'custom', minMove: 0.001, formatter: (v: number) => v.toFixed(3) } })
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
    // 1d / 1w: dates without a clock on the axis and crosshair (see lib/chart.ts timeScaleFor).
    chart.applyOptions({ timeScale: { ...timeScaleFor(tf), rightOffset: 2 } })
    const clean = points.filter((p) => finite(p.time))
    const line = (pick: (p: TimelinePoint) => number | null) =>
      ensureAscending(
        clean.flatMap((p) => {
          const v = pick(p)
          return v === null ? [] : [{ time: ts(toSec(p.time), tf), value: v }]
        }),
      )
    s.hit.setData(line((p) => ratePct(p.hit_rate)))
    s.hitRef.setData(line((p) => ratePct(p.reference ?? null)))
    s.brier.setData(line((p) => (finite(p.brier) ? p.brier : null)))
    s.brierRef.setData(line((p) => (finite(p.brier_ref) ? p.brier_ref : null)))
    chart.timeScale().fitContent()
  }, [chart, points, tf])

  const hasRef = points.some((p) => finite(p.reference) || finite(p.brier_ref))
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
        {hasRef && (
          <span className="legend-item" title="Discontinua: acierto de decir siempre el resultado más frecuente del día y Brier de la climatología del día">
            <span className="swatch" style={{ background: COLORS.muted }} /> Referencia y climatología (discontinuas)
          </span>
        )}
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
