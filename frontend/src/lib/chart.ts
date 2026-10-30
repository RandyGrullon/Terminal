// lightweight-charts v5 helpers shared by every chart in the app (main chart, simulator, backtest, studio).
// IMPORTANT: always convert bar times with `ts()` (it shifts UTC -> local clock so the axis shows local time),
// and convert back with `fromTs()` when reading times from the chart.
import { useEffect, useRef, useState } from 'react'
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  LineType,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type ChartOptions,
  type DeepPartial,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type SeriesMarker,
  type SeriesType,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'
import type { Candle, ChartMarker, ConePoint, IndicatorOutput, NexusBars, PositionState } from '../api/types'

export { LineStyle, LineType }

export const COLORS = {
  bg: '#0b0e14',
  panel: '#121722',
  grid: 'rgba(42, 49, 66, 0.45)',
  border: '#262d3d',
  text: '#aab1c0',
  muted: '#7a8396',
  pos: '#16c784',
  neg: '#ea3943',
  warn: '#f5a524',
  info: '#3ea6ff',
  accent: '#2f6bff',
  purple: '#a97bff',
  cyan: '#22d3ee',
  upVol: 'rgba(22, 199, 132, 0.35)',
  downVol: 'rgba(234, 57, 67, 0.35)',
  tp1: '#4ade80',
  tp2: '#22c55e',
  tp3: '#15a34a',
}

// ----------------------------------------------------------------------------- time

const offsetCache = new Map<number, number>()
function localOffsetSec(sec: number): number {
  const hourKey = Math.floor(sec / 3600)
  let off = offsetCache.get(hourKey)
  if (off === undefined) {
    off = -new Date(sec * 1000).getTimezoneOffset() * 60
    if (offsetCache.size > 50000) offsetCache.clear()
    offsetCache.set(hourKey, off)
  }
  return off
}

/** Bar time (unix s, UTC) -> chart time (shifted to the local clock). */
export const ts = (sec: number): UTCTimestamp => (sec + localOffsetSec(sec)) as UTCTimestamp

/** Chart time -> unix seconds UTC. */
export const fromTs = (t: Time): number => {
  const n = typeof t === 'number' ? t : 0
  return n - localOffsetSec(n)
}

// ----------------------------------------------------------------------------- chart creation

type Plain = Record<string, unknown>
function deepMerge<T>(base: T, extra: unknown): T {
  if (!extra || typeof extra !== 'object') return base
  const out: Plain = { ...(base as Plain) }
  for (const [k, v] of Object.entries(extra as Plain)) {
    const b = out[k]
    out[k] = v && typeof v === 'object' && !Array.isArray(v) && b && typeof b === 'object' ? deepMerge(b, v) : v
  }
  return out as T
}

export function createBaseChart(el: HTMLElement, opts: DeepPartial<ChartOptions> = {}): IChartApi {
  const base: DeepPartial<ChartOptions> = {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: COLORS.bg },
      textColor: COLORS.text,
      fontSize: 11,
      fontFamily: "Inter, 'Segoe UI', system-ui, sans-serif",
      panes: { separatorColor: COLORS.border, separatorHoverColor: 'rgba(47, 107, 255, 0.35)', enableResize: true },
    },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    crosshair: { mode: CrosshairMode.Normal },
    rightPriceScale: { borderColor: COLORS.border },
    timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false, rightOffset: 6 },
    localization: { locale: 'es-ES' },
  }
  return createChart(el, deepMerge(base, opts))
}

/**
 * React hook: creates a chart in the returned ref'd div and removes it on unmount.
 *   const { ref, chart } = useChart()
 *   return <div ref={ref} style={{ height: 400 }} />
 */
export function useChart(opts: DeepPartial<ChartOptions> = {}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [chart, setChart] = useState<IChartApi | null>(null)
  useEffect(() => {
    if (!ref.current) return
    const c = createBaseChart(ref.current, opts)
    setChart(c)
    return () => {
      setChart(null)
      c.remove()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return { ref, chart }
}

// ----------------------------------------------------------------------------- data mappers

export function toCandleData(candles: Candle[]): CandlestickData<Time>[] {
  return candles.map((c) => ({ time: ts(c.time), open: c.open, high: c.high, low: c.low, close: c.close }))
}

export function toVolumeData(candles: Candle[]): HistogramData<Time>[] {
  return candles.map((c) => ({ time: ts(c.time), value: c.volume, color: c.close >= c.open ? COLORS.upVol : COLORS.downVol }))
}

/** Aligned arrays -> line data with whitespace (gaps) where the value is null. */
export function toLineData(time: number[], values: (number | null)[]): (LineData<Time> | WhitespaceData<Time>)[] {
  const out: (LineData<Time> | WhitespaceData<Time>)[] = new Array(time.length)
  for (let i = 0; i < time.length; i++) {
    const v = values[i]
    out[i] = v === null || v === undefined || !Number.isFinite(v) ? { time: ts(time[i]) } : { time: ts(time[i]), value: v }
  }
  return out
}

export function toHistogramData(
  time: number[],
  values: (number | null)[],
  posColor: string = COLORS.upVol,
  negColor: string = COLORS.downVol,
): (HistogramData<Time> | WhitespaceData<Time>)[] {
  return time.map((t, i) => {
    const v = values[i]
    if (v === null || v === undefined || !Number.isFinite(v)) return { time: ts(t) }
    return { time: ts(t), value: v, color: v >= 0 ? posColor : negColor }
  })
}

export function toMarkers(markers: ChartMarker[]): SeriesMarker<Time>[] {
  return [...markers]
    .sort((a, b) => a.time - b.time)
    .map((m) => ({
      time: ts(m.time),
      position: m.position,
      shape: m.shape,
      color: m.color,
      text: m.text,
      size: m.kind?.startsWith('REJECTED') ? 0.6 : 1,
    }))
}

// ----------------------------------------------------------------------------- series factories

export function addCandles(chart: IChartApi, pane = 0): ISeriesApi<'Candlestick'> {
  return chart.addSeries(
    CandlestickSeries,
    {
      upColor: COLORS.pos,
      downColor: COLORS.neg,
      borderVisible: false,
      wickUpColor: COLORS.pos,
      wickDownColor: COLORS.neg,
    },
    pane,
  )
}

/** Volume histogram overlaid at the bottom 18 % of a pane. */
export function addVolume(chart: IChartApi, pane = 0): ISeriesApi<'Histogram'> {
  const s = chart.addSeries(
    HistogramSeries,
    { priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false },
    pane,
  )
  s.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
  return s
}

export interface LineOpts {
  color: string
  width?: 1 | 2 | 3 | 4
  style?: LineStyle
  title?: string
  pane?: number
  priceScaleId?: string
  lastValue?: boolean
  step?: boolean
  dots?: boolean
  autoscale?: boolean
}

export function addLine(chart: IChartApi, o: LineOpts): ISeriesApi<'Line'> {
  return chart.addSeries(
    LineSeries,
    {
      color: o.color,
      lineWidth: o.width ?? 1,
      lineStyle: o.style ?? LineStyle.Solid,
      title: o.title ?? '',
      priceScaleId: o.priceScaleId,
      lastValueVisible: o.lastValue ?? false,
      priceLineVisible: false,
      crosshairMarkerVisible: !o.dots,
      lineType: o.step ? LineType.WithSteps : LineType.Simple,
      lineVisible: !o.dots,
      pointMarkersVisible: !!o.dots,
      pointMarkersRadius: o.dots ? 2 : undefined,
      ...(o.autoscale === false ? { autoscaleInfoProvider: () => null } : {}),
    },
    o.pane ?? 0,
  )
}

/** Creates the right series type for an indicator output (line | histogram | area | dots | step). */
export function addSeriesForOutput(chart: IChartApi, output: IndicatorOutput, pane: number): ISeriesApi<SeriesType> {
  switch (output.type) {
    case 'histogram':
      return chart.addSeries(
        HistogramSeries,
        { color: output.color, priceLineVisible: false, lastValueVisible: false, title: output.title },
        pane,
      )
    case 'area':
      return chart.addSeries(
        AreaSeries,
        {
          lineColor: output.color,
          topColor: `${output.color}55`,
          bottomColor: `${output.color}05`,
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          title: output.title,
        },
        pane,
      )
    case 'dots':
      return addLine(chart, { color: output.color, dots: true, pane, title: output.title })
    case 'step':
      return addLine(chart, { color: output.color, step: true, pane, title: output.title })
    default:
      return addLine(chart, { color: output.color, pane, title: output.title, width: 1 })
  }
}

/** Data for an indicator output, matching addSeriesForOutput's series type. */
export function outputData(output: IndicatorOutput, time: number[], values: (number | null)[]) {
  if (output.type === 'histogram') {
    return time.map((t, i) => {
      const v = values[i]
      if (v === null || v === undefined || !Number.isFinite(v)) return { time: ts(t) }
      return { time: ts(t), value: v, color: v >= 0 ? `${output.color}cc` : `${COLORS.neg}cc` }
    })
  }
  return toLineData(time, values)
}

// ----------------------------------------------------------------------------- overlays

export class MarkerLayer {
  private plugin: ISeriesMarkersPluginApi<Time>
  constructor(series: ISeriesApi<SeriesType>) {
    this.plugin = createSeriesMarkers(series, [])
  }
  set(markers: ChartMarker[]): void {
    this.plugin.setMarkers(toMarkers(markers))
  }
  clear(): void {
    this.plugin.setMarkers([])
  }
  detach(): void {
    this.plugin.detach()
  }
}

export interface LevelLine {
  price: number | null | undefined
  color: string
  title: string
  style?: LineStyle
  width?: 1 | 2 | 3 | 4
  axisLabel?: boolean
}

/** Horizontal price lines on a series ("hasta aquí" lines, hlines). */
export class LevelLines {
  private lines: IPriceLine[] = []
  constructor(private series: ISeriesApi<SeriesType>) {}
  set(levels: LevelLine[]): void {
    this.clear()
    for (const l of levels) {
      if (l.price === null || l.price === undefined || !Number.isFinite(l.price)) continue
      this.lines.push(
        this.series.createPriceLine({
          price: l.price,
          color: l.color,
          lineWidth: l.width ?? 1,
          lineStyle: l.style ?? LineStyle.Dashed,
          axisLabelVisible: l.axisLabel ?? true,
          title: l.title,
        }),
      )
    }
  }
  clear(): void {
    for (const line of this.lines) {
      try {
        this.series.removePriceLine(line)
      } catch {
        /* series already removed */
      }
    }
    this.lines = []
  }
}

/** Price lines for an open position: entry, SL ("HASTA AQUÍ"), TP1..3 and trailing stop. */
export function positionLevels(pos: PositionState | null | undefined): LevelLine[] {
  if (!pos) return []
  const hit = (n: number) => (pos.tp_hits >= n ? ' ✓' : '')
  return [
    { price: pos.entry_price, color: COLORS.muted, title: 'ENTRADA', style: LineStyle.Solid },
    { price: pos.sl, color: COLORS.neg, title: 'SL · HASTA AQUÍ', style: LineStyle.Solid, width: 2 },
    { price: pos.tp1, color: COLORS.tp1, title: `TP1${hit(1)}` },
    { price: pos.tp2, color: COLORS.tp2, title: `TP2${hit(2)}` },
    { price: pos.tp3, color: COLORS.tp3, title: `TP3${hit(3)}` },
    ...(pos.trail !== null ? [{ price: pos.trail, color: COLORS.warn, title: 'STOP DINÁMICO', style: LineStyle.Dotted }] : []),
  ]
}

/** Per-bar SL/TP/entry step lines drawn only while a trade is open (from NexusBars.pos_*). */
export class TradeLevelsLayer {
  private entry: ISeriesApi<'Line'>
  private sl: ISeriesApi<'Line'>
  private tp1: ISeriesApi<'Line'>
  private tp2: ISeriesApi<'Line'>
  private tp3: ISeriesApi<'Line'>
  constructor(private chart: IChartApi, pane = 0) {
    const mk = (color: string, style: LineStyle, width: 1 | 2) =>
      addLine(chart, { color, style, width, pane, step: true, autoscale: false })
    this.entry = mk(COLORS.muted, LineStyle.Dotted, 1)
    this.sl = mk(COLORS.neg, LineStyle.Solid, 2)
    this.tp1 = mk(COLORS.tp1, LineStyle.Dashed, 1)
    this.tp2 = mk(COLORS.tp2, LineStyle.Dashed, 1)
    this.tp3 = mk(COLORS.tp3, LineStyle.Dashed, 1)
  }
  set(bars: NexusBars): void {
    this.entry.setData(toLineData(bars.time, bars.pos_entry))
    this.sl.setData(toLineData(bars.time, bars.pos_sl))
    this.tp1.setData(toLineData(bars.time, bars.pos_tp1))
    this.tp2.setData(toLineData(bars.time, bars.pos_tp2))
    this.tp3.setData(toLineData(bars.time, bars.pos_tp3))
  }
  clear(): void {
    for (const s of [this.entry, this.sl, this.tp1, this.tp2, this.tp3]) s.setData([])
  }
  remove(): void {
    for (const s of [this.entry, this.sl, this.tp1, this.tp2, this.tp3]) {
      try {
        this.chart.removeSeries(s)
      } catch {
        /* chart already removed */
      }
    }
  }
}

/** Prediction cone (p10/p50/p90 + inner p25/p75) projected after the last bar. */
export class ConeLayer {
  private series: ISeriesApi<'Line'>[]
  constructor(private chart: IChartApi, pane = 0) {
    const outer = 'rgba(47, 107, 255, 0.45)'
    const inner = 'rgba(47, 107, 255, 0.75)'
    this.series = [
      addLine(chart, { color: outer, style: LineStyle.Dashed, pane, autoscale: false }),
      addLine(chart, { color: inner, style: LineStyle.Dotted, pane, autoscale: false }),
      addLine(chart, { color: COLORS.accent, style: LineStyle.Dashed, width: 2, pane, autoscale: false }),
      addLine(chart, { color: inner, style: LineStyle.Dotted, pane, autoscale: false }),
      addLine(chart, { color: outer, style: LineStyle.Dashed, pane, autoscale: false }),
    ]
  }
  set(cone: ConePoint[], anchor?: { time: number; price: number }): void {
    const keys: (keyof ConePoint)[] = ['p90', 'p75', 'p50', 'p25', 'p10']
    keys.forEach((k, i) => {
      const pts = cone.map((p) => ({ time: ts(p.time), value: p[k] as number }))
      if (anchor) pts.unshift({ time: ts(anchor.time), value: anchor.price })
      this.series[i].setData(pts)
    })
  }
  clear(): void {
    for (const s of this.series) s.setData([])
  }
  remove(): void {
    for (const s of this.series) {
      try {
        this.chart.removeSeries(s)
      } catch {
        /* chart already removed */
      }
    }
  }
}

/** Remove every pane above `keep` (panes are 0-based; pane 0 holds the price). */
export function trimPanes(chart: IChartApi, keep = 1): void {
  for (let i = chart.panes().length - 1; i >= keep; i--) {
    try {
      chart.removePane(i)
    } catch {
      /* ignore */
    }
  }
}
