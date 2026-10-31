// Live preview of a PyChart run: candles + volume, script plots (overlay on the price pane, the rest in their own
// panes), plotshape / strategy markers, hlines and bgcolor bands, with a crosshair legend.
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import {
  AreaSeries,
  HistogramSeries,
  type AutoscaleInfo,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type SeriesType,
  type Time,
} from 'lightweight-charts'
import { ChartCandlestick, LoaderCircle, TriangleAlert } from 'lucide-react'
import type { ScriptPlot, ScriptRunResult } from '../../api/types'
import {
  COLORS,
  LevelLines,
  LineStyle,
  MarkerLayer,
  addCandles,
  addLine,
  addVolume,
  timeScaleFor,
  toCandleData,
  toHistogramData,
  toLineData,
  toVolumeData,
  trimPanes,
  ts,
  useChart,
} from '../../lib/chart'
import { fmtPrice } from '../../lib/format'
import { Empty, ErrorBox } from '../ui'
import { ensureAscending, resolveColor, softBackground, withAlpha } from './chartUtils'

interface Base {
  candles: ISeriesApi<'Candlestick'>
  volume: ISeriesApi<'Histogram'>
  markers: MarkerLayer
}

interface Dynamic {
  series: ISeriesApi<SeriesType>[]
  levels: LevelLines[]
  plots: { plot: ScriptPlot; series: ISeriesApi<SeriesType> }[]
}

const lineWidth = (w: number | null | undefined): 1 | 2 | 3 | 4 => {
  const n = Math.round(typeof w === 'number' && Number.isFinite(w) ? w : 1)
  return (n <= 1 ? 1 : n >= 4 ? 4 : n) as 1 | 2 | 3 | 4
}

function createPlotSeries(chart: IChartApi, plot: ScriptPlot, pane: number, color: string): ISeriesApi<SeriesType> {
  switch (plot.style) {
    case 'histogram':
    case 'columns':
      return chart.addSeries(
        HistogramSeries,
        { color, priceLineVisible: false, lastValueVisible: false, title: plot.title, base: 0 },
        pane,
      ) as ISeriesApi<SeriesType>
    case 'area':
      return chart.addSeries(
        AreaSeries,
        {
          lineColor: color,
          topColor: withAlpha(color, 0.35, color),
          bottomColor: withAlpha(color, 0.03, 'rgba(0,0,0,0)'),
          lineWidth: lineWidth(plot.linewidth),
          priceLineVisible: false,
          lastValueVisible: false,
          title: plot.title,
        },
        pane,
      ) as ISeriesApi<SeriesType>
    case 'circles':
      return addLine(chart, { color, dots: true, pane, title: plot.title }) as ISeriesApi<SeriesType>
    case 'step':
      return addLine(chart, { color, step: true, pane, title: plot.title, width: lineWidth(plot.linewidth) }) as ISeriesApi<SeriesType>
    default:
      return addLine(chart, { color, pane, title: plot.title, width: lineWidth(plot.linewidth) }) as ISeriesApi<SeriesType>
  }
}

function plotData(plot: ScriptPlot, time: number[], color: string, tf: string) {
  const n = Math.min(time.length, Array.isArray(plot.data) ? plot.data.length : 0)
  const t = n === time.length ? time : time.slice(0, n)
  const v = n === plot.data.length ? plot.data : plot.data.slice(0, n)
  if (plot.style === 'histogram' || plot.style === 'columns') return ensureAscending(toHistogramData(t, v, tf, color, color))
  return ensureAscending(toLineData(t, v, tf))
}

const LINE_STYLE = { solid: LineStyle.Solid, dashed: LineStyle.Dashed, dotted: LineStyle.Dotted } as const

interface Props {
  result: ScriptRunResult | null
  viewKey: string
  running: boolean
  error: string | null
  symbol: string
  /** Timeframe of `result` (1d / 1w bars are drawn with their UTC session dates). */
  tf: string
  onRetry: () => void
}

function PreviewChart({ result, viewKey, running, error, symbol, tf, onRetry }: Props) {
  const { ref, chart } = useChart({ rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.22 } } })
  const base = useRef<Base | null>(null)
  const dyn = useRef<Dynamic>({ series: [], levels: [], plots: [] })
  const shownKey = useRef('')
  const [hover, setHover] = useState<Record<string, number | null> | null>(null)
  const [drawError, setDrawError] = useState<string | null>(null)

  useEffect(() => {
    if (!chart) return
    const candles = addCandles(chart, 0)
    const volume = addVolume(chart, 0)
    const markers = new MarkerLayer(candles)
    base.current = { candles, volume, markers }
    const onMove = (param: MouseEventParams<Time>) => {
      if (param.time === undefined || !param.seriesData.size) {
        setHover(null)
        return
      }
      const vals: Record<string, number | null> = {}
      for (const { plot, series } of dyn.current.plots) {
        const d = param.seriesData.get(series) as { value?: number } | undefined
        vals[plot.id] = d && typeof d.value === 'number' && Number.isFinite(d.value) ? d.value : null
      }
      setHover(vals)
    }
    chart.subscribeCrosshairMove(onMove)
    return () => {
      base.current = null
      try {
        chart.unsubscribeCrosshairMove(onMove)
        markers.detach()
      } catch {
        /* chart already removed */
      }
    }
  }, [chart])

  useEffect(() => {
    const b = base.current
    if (!chart || !b) return
    for (const s of dyn.current.series) {
      try {
        chart.removeSeries(s)
      } catch {
        /* already gone */
      }
    }
    for (const l of dyn.current.levels) l.clear()
    dyn.current = { series: [], levels: [], plots: [] }
    trimPanes(chart, 1)
    setHover(null)
    setDrawError(null)

    if (!result || !result.candles.length) {
      b.candles.setData([])
      b.volume.setData([])
      b.markers.clear()
      shownKey.current = ''
      return
    }

    try {
      chart.applyOptions({ timeScale: timeScaleFor(tf) })
      const candleData = ensureAscending(toCandleData(result.candles, tf))
      b.candles.setData(candleData)
      b.volume.setData(ensureAscending(toVolumeData(result.candles, tf)))

      // Plots: overlay -> pane 0, the rest grouped by their declared pane (renumbered 1..n).
      const time = result.time.length ? result.time : result.candles.map((c) => c.time)
      const subPanes = Array.from(new Set(result.plots.filter((p) => !p.overlay).map((p) => Math.max(1, Math.round(p.pane || 1))))).sort(
        (a, c) => a - c,
      )
      const paneOf = (p: ScriptPlot) => (p.overlay ? 0 : subPanes.indexOf(Math.max(1, Math.round(p.pane || 1))) + 1)
      const ordered = result.plots.map((p, i) => ({ p, i, pane: paneOf(p) })).sort((a, c) => a.pane - c.pane || a.i - c.i)
      for (const { p, pane } of ordered) {
        const color = resolveColor(p.color, COLORS.info)
        const series = createPlotSeries(chart, p, pane, color)
        series.setData(plotData(p, time, color, tf))
        dyn.current.series.push(series)
        dyn.current.plots.push({ plot: p, series })
      }
      const panes = chart.panes()
      if (panes.length > 1) {
        panes[0].setStretchFactor(3)
        for (let i = 1; i < panes.length; i++) panes[i].setStretchFactor(1)
      }

      // The script's "own" pane: price pane for overlays, first sub-pane otherwise.
      const ownPane = !result.meta.overlay && subPanes.length ? 1 : 0
      const ownHost = ownPane === 0 ? (b.candles as ISeriesApi<SeriesType>) : dyn.current.plots.find((x) => paneOf(x.plot) === 1)?.series

      if (result.bgcolors.length) {
        const bg = chart.addSeries(
          HistogramSeries,
          {
            priceScaleId: 'studio-bg',
            priceLineVisible: false,
            lastValueVisible: false,
            base: 0,
            autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }),
          },
          ownPane,
        )
        bg.priceScale().applyOptions({ visible: false, scaleMargins: { top: 0, bottom: 0 } })
        bg.setData(ensureAscending(result.bgcolors.map((x) => ({ time: ts(x.time, tf), value: 1, color: softBackground(x.color) }))))
        try {
          bg.setSeriesOrder(0)
        } catch {
          /* older engines: drawn above, still translucent */
        }
        dyn.current.series.push(bg as ISeriesApi<SeriesType>)
      }

      const hl = result.hlines.filter((h) => typeof h.price === 'number' && Number.isFinite(h.price))
      if (hl.length && ownHost) {
        const levels = new LevelLines(ownHost)
        levels.set(
          hl.map((h) => ({
            price: h.price,
            color: resolveColor(h.color, COLORS.muted),
            title: h.title ?? '',
            style: LINE_STYLE[h.style] ?? LineStyle.Dashed,
          })),
        )
        dyn.current.levels.push(levels)
        if (ownPane !== 0) {
          const lo = Math.min(...hl.map((h) => h.price))
          const hi = Math.max(...hl.map((h) => h.price))
          ;(ownHost as ISeriesApi<'Line'>).applyOptions({
            autoscaleInfoProvider: (original: () => AutoscaleInfo | null): AutoscaleInfo | null => {
              const r = original()
              if (!r || !r.priceRange) return r
              return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, lo), maxValue: Math.max(r.priceRange.maxValue, hi) } }
            },
          })
        }
      }

      b.markers.set(
        [...result.markers, ...(result.strategy?.markers ?? [])].filter((m) => typeof m.time === 'number' && Number.isFinite(m.time)),
        tf,
      )

      if (shownKey.current !== viewKey) {
        const n = candleData.length
        chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 180), to: n + 4 })
        shownKey.current = viewKey
      }
    } catch (err) {
      setDrawError(err instanceof Error ? err.message : String(err))
    }
  }, [chart, result, viewKey, tf])

  const legend = useMemo(() => {
    if (!result) return []
    return result.plots.map((p) => {
      let last: number | null = null
      for (let i = p.data.length - 1; i >= 0; i--) {
        const v = p.data[i]
        if (typeof v === 'number' && Number.isFinite(v)) {
          last = v
          break
        }
      }
      return { id: p.id, title: p.title || p.id, color: resolveColor(p.color, COLORS.info), last, pane: p.overlay ? 0 : Math.max(1, p.pane || 1) }
    })
  }, [result])

  const hasCandles = !!result && result.candles.length > 0
  const title = result?.meta?.title

  return (
    <div className="studio-chart">
      <div ref={ref} className="studio-chart-canvas" />
      {hasCandles && (
        <div className="legend">
          <span className="legend-item">
            <strong>{title || 'Script'}</strong>
            <span className="muted mono">
              {symbol} · {tf}
            </span>
          </span>
          {legend.slice(0, 12).map((l) => {
            const v = hover ? hover[l.id] : l.last
            return (
              <span key={l.id} className="legend-item">
                <span className="swatch" style={{ background: l.color }} />
                <span className="text-2">{l.title}</span>
                <span className="mono" style={{ color: l.color }}>
                  {fmtPrice(v ?? null)}
                </span>
                {l.pane > 0 && <span className="tiny muted">panel {l.pane}</span>}
              </span>
            )
          })}
          {legend.length > 12 && <span className="legend-item tiny muted">+{legend.length - 12} series</span>}
        </div>
      )}
      {running && hasCandles && (
        <div className="studio-chart-badge">
          <LoaderCircle size={13} className="studio-spin" /> Ejecutando…
        </div>
      )}
      {!hasCandles && (
        <div className="chart-overlay studio-chart-overlay">
          {running ? (
            <Empty icon={<LoaderCircle size={26} className="studio-spin" />} title="Ejecutando script…">
              Descargando velas de {symbol} ({tf}) y calculando.
            </Empty>
          ) : error ? (
            <div style={{ maxWidth: 420, width: '100%', padding: 16 }}>
              <ErrorBox error={`No se pudo ejecutar: ${error}`} onRetry={onRetry} />
            </div>
          ) : result ? (
            <Empty icon={<TriangleAlert size={26} />} title={result.ok ? `Sin velas para ${symbol} (${tf})` : 'El script terminó con errores'}>
              {result.ok ? 'Prueba otro símbolo o temporalidad.' : 'Revisa la pestaña “Problemas” del editor.'}
            </Empty>
          ) : (
            <Empty icon={<ChartCandlestick size={28} />} title="Vista previa">
              Ejecuta el script con <span className="mono">Ctrl+Enter</span> para dibujarlo sobre {symbol}.
            </Empty>
          )}
        </div>
      )}
      {hasCandles && (error || drawError || (result && !result.ok)) && (
        <div className="studio-chart-banner">
          <TriangleAlert size={13} />
          <span className="grow">
            {error
              ? `Última ejecución fallida: ${error}`
              : drawError
                ? `No se pudo dibujar todo el resultado: ${drawError}`
                : 'El script terminó con errores; se muestra lo que se pudo calcular.'}
          </span>
        </div>
      )}
    </div>
  )
}

export default memo(PreviewChart)
