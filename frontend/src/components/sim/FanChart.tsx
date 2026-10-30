// Monte Carlo fan chart: equity percentiles (p5..p95, % of initial capital) + a few sample paths.
// lightweight-charts needs a time axis, so each step is mapped to a synthetic day (T0 + step * 1 day, through ts())
// and the formatters print the step number instead of a date.
import { useEffect, useMemo, useRef, useState } from 'react'
import { LineSeries, type ISeriesApi, type MouseEventParams, type Time } from 'lightweight-charts'
import type { MonteCarloResult } from '../../api/types'
import { COLORS, LevelLines, LineStyle, addLine, fromTs, ts, useChart } from '../../lib/chart'
import { fmtNum } from '../../lib/format'
import { Card } from '../ui'
import { finiteValues, quantileSorted } from './stats'

const T0 = 1_700_000_000
const DAY = 86_400
const MAX_SAMPLE_PATHS = 24

const stepTime = (step: number) => ts(T0 + step * DAY)
const stepOf = (t: Time) => Math.round((fromTs(t) - T0) / DAY)
const pctLabel = (v: number) => `${fmtNum(v, 1)} %`

type PKey = 'p5' | 'p25' | 'p50' | 'p75' | 'p95'

const BANDS: { key: PKey; label: string; color: string; width: 1 | 2; style: LineStyle }[] = [
  { key: 'p95', label: 'p95 (optimista)', color: 'rgba(22, 199, 132, 0.85)', width: 1, style: LineStyle.Dashed },
  { key: 'p75', label: 'p75', color: 'rgba(22, 199, 132, 0.55)', width: 1, style: LineStyle.Solid },
  { key: 'p50', label: 'Mediana (p50)', color: COLORS.accent, width: 2, style: LineStyle.Solid },
  { key: 'p25', label: 'p25', color: 'rgba(234, 57, 67, 0.55)', width: 1, style: LineStyle.Solid },
  { key: 'p5', label: 'p5 (pesimista)', color: 'rgba(234, 57, 67, 0.85)', width: 1, style: LineStyle.Dashed },
]

/** Factor that converts a series whose first value is `first` into "% of initial" (100 = flat). */
function unitFactor(first: number | null, capital: number): number {
  if (first === null || !(first > 0)) return 1
  const candidates = [100, capital > 0 ? capital : 100, 1]
  let best = 100
  let bestErr = Infinity
  for (const c of candidates) {
    const err = Math.abs(Math.log(first / c))
    if (err < bestErr) {
      bestErr = err
      best = c
    }
  }
  return 100 / best
}

/** Step of each point of a sample path (aligned with the percentiles when lengths match). */
function pathSteps(len: number, pctSteps: number[], totalSteps: number): number[] {
  if (pctSteps.length === len) return pctSteps
  if (len <= 1) return [0]
  const total = totalSteps > 0 ? totalSteps : len - 1
  return Array.from({ length: len }, (_, i) => Math.round((i * total) / (len - 1)))
}

interface Prepared {
  bands: Record<PKey, { time: Time; value: number }[]>
  paths: { time: Time; value: number }[][]
  lastStep: number
  final: Record<PKey, number | null>
}

function prepare(result: MonteCarloResult, capital: number): Prepared {
  const pct = result.percentiles
  const pFactor = unitFactor(pct.length ? pct[0].p50 : null, capital)
  const bands = { p5: [], p25: [], p50: [], p75: [], p95: [] } as Prepared['bands']
  let prevStep = -Infinity
  for (const p of pct) {
    if (!(p.step > prevStep)) continue
    prevStep = p.step
    const t = stepTime(p.step)
    for (const b of BANDS) {
      const v = p[b.key]
      if (typeof v === 'number' && Number.isFinite(v)) bands[b.key].push({ time: t, value: v * pFactor })
    }
  }
  const pctSteps = pct.map((p) => p.step)
  const samples = result.sample_paths.slice(0, MAX_SAMPLE_PATHS)
  const starts = finiteValues(samples.map((s) => (Array.isArray(s) ? s[0] : null))).sort((a, b) => a - b)
  const sFactor = unitFactor(quantileSorted(starts, 0.5), capital)
  const paths = samples.map((path) => {
    const steps = pathSteps(path.length, pctSteps, result.steps)
    const out: { time: Time; value: number }[] = []
    let last = -Infinity
    for (let i = 0; i < path.length; i++) {
      const v = path[i]
      const s = steps[i]
      if (!(s > last) || typeof v !== 'number' || !Number.isFinite(v)) continue
      last = s
      out.push({ time: stepTime(s), value: v * sFactor })
    }
    return out
  })
  const lastP = pct.length ? pct[pct.length - 1] : null
  const final = {
    p5: lastP ? lastP.p5 * pFactor : null,
    p25: lastP ? lastP.p25 * pFactor : null,
    p50: lastP ? lastP.p50 * pFactor : null,
    p75: lastP ? lastP.p75 * pFactor : null,
    p95: lastP ? lastP.p95 * pFactor : null,
  }
  return { bands, paths, lastStep: lastP ? lastP.step : 0, final }
}

export function FanChart(props: { result: MonteCarloResult; capital: number }) {
  const { result, capital } = props
  const { ref, chart } = useChart({
    localization: { timeFormatter: (t: Time) => `Paso ${stepOf(t)}` },
    timeScale: { tickMarkFormatter: (t: Time) => String(stepOf(t)), timeVisible: false, secondsVisible: false, rightOffset: 2 },
    rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.08 } },
  })
  const bandSeries = useRef<Map<PKey, ISeriesApi<'Line'>> | null>(null)
  const pathSeries = useRef<ISeriesApi<'Line'>[]>([])
  const baseline = useRef<LevelLines | null>(null)
  const [hover, setHover] = useState<{ step: number; values: Record<PKey, number | null> } | null>(null)
  const data = useMemo(() => prepare(result, capital), [result, capital])

  useEffect(() => {
    if (!chart) return
    const map = new Map<PKey, ISeriesApi<'Line'>>()
    for (const b of BANDS) {
      const s = addLine(chart, { color: b.color, width: b.width, style: b.style, lastValue: b.key === 'p50' })
      s.applyOptions({ priceFormat: { type: 'custom', formatter: pctLabel, minMove: 0.01 } })
      map.set(b.key, s)
    }
    bandSeries.current = map
    const p50 = map.get('p50')
    baseline.current = p50 ? new LevelLines(p50) : null
    const onMove = (param: MouseEventParams<Time>) => {
      const m = bandSeries.current
      if (!m || param.time === undefined) {
        setHover(null)
        return
      }
      const values = {} as Record<PKey, number | null>
      for (const b of BANDS) {
        const s = m.get(b.key)
        const d = s ? (param.seriesData.get(s) as { value?: number } | undefined) : undefined
        values[b.key] = d && typeof d.value === 'number' && Number.isFinite(d.value) ? d.value : null
      }
      setHover({ step: stepOf(param.time), values })
    }
    chart.subscribeCrosshairMove(onMove)
    return () => {
      try {
        chart.unsubscribeCrosshairMove(onMove)
      } catch {
        /* chart already removed */
      }
      bandSeries.current = null
      baseline.current = null
      pathSeries.current = []
    }
  }, [chart])

  useEffect(() => {
    const map = bandSeries.current
    if (!chart || !map) return
    try {
      for (const s of pathSeries.current) {
        try {
          chart.removeSeries(s)
        } catch {
          /* already removed */
        }
      }
      // Sample paths are created after the percentile lines, so move each one to the bottom of the pane's z-order.
      pathSeries.current = data.paths.map((points) => {
        const s = chart.addSeries(
          LineSeries,
          {
            color: 'rgba(122, 131, 150, 0.28)',
            lineWidth: 1,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
            priceFormat: { type: 'custom', formatter: pctLabel, minMove: 0.01 },
          },
          0,
        )
        s.setSeriesOrder(0)
        s.setData(points)
        return s
      })
      for (const b of BANDS) map.get(b.key)?.setData(data.bands[b.key])
      baseline.current?.set([{ price: 100, color: COLORS.muted, title: 'Capital inicial', style: LineStyle.Dotted }])
      chart.timeScale().fitContent()
    } catch (err) {
      console.error('No se pudo dibujar el abanico de Monte Carlo', err)
    }
  }, [chart, data])

  const shown = hover ? hover.values : data.final
  const stepLabel = hover ? `Paso ${hover.step}` : `Paso final (${data.lastStep})`

  return (
    <Card
      title="Abanico de trayectorias"
      sub={`Capital en % del inicial (100 = sin cambios) · ${result.paths} trayectorias · ${data.paths.length} de muestra en gris`}
      flush
    >
      <div className="chart-box sim-chart-frame sim-h-fan">
        <div ref={ref} className="sim-chart-canvas" />
        {result.percentiles.length > 0 ? (
          <div className="legend">
            <div className="legend-item muted">{stepLabel}</div>
            {BANDS.map((b) => (
              <div key={b.key} className="legend-item">
                <span className="swatch" style={{ background: b.color }} /> {b.label}{' '}
                <b className="mono">{shown[b.key] === null ? '—' : pctLabel(shown[b.key] as number)}</b>
              </div>
            ))}
          </div>
        ) : (
          <div className="chart-overlay">
            <span className="muted small">El servidor no devolvió percentiles para dibujar.</span>
          </div>
        )}
      </div>
    </Card>
  )
}
