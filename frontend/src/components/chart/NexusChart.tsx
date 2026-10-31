// Main NEXUS chart (lightweight-charts v5).
// Pane 0: candles, volume overlay, NEXUS EMAs, markers (signals / rejected), historical "hasta aquí" SL/TP step
// lines, open-position (or proposed entry) price lines, the prediction cone and overlay indicators.
// Optional pane: NEXUS score + probability. Every non-overlay indicator gets its own pane.
// Series live in refs; data updates never recreate the chart. Live candles arrive through the imperative handle.
import { forwardRef, memo, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type ReactNode } from 'react'
import { HistogramSeries, type IChartApi, type ISeriesApi, type MouseEventParams, type SeriesType, type Time } from 'lightweight-charts'
import { X } from 'lucide-react'
import type { Candle, ChartMarker, GateResult, IndicatorOutput, IndicatorSpec, NexusCurrent, NexusResponse } from '../../api/types'
import {
  COLORS,
  ConeLayer,
  LevelLines,
  LineStyle,
  MarkerLayer,
  TradeLevelsLayer,
  addCandles,
  addLine,
  addSeriesForOutput,
  addVolume,
  fromTs,
  isChartRemoved,
  outputData,
  positionLevels,
  timeScaleFor,
  toCandleData,
  toHistogramData,
  toLineData,
  toVolumeData,
  trimPanes,
  ts,
  useChart,
  type LevelLine,
} from '../../lib/chart'
import { priceDecimals } from '../../lib/format'
import ChartLegend from './ChartLegend'
import MarkerTooltip, { type TooltipState } from './MarkerTooltip'
import {
  ascendingIndices,
  candleIndexAt,
  cleanBars,
  cleanCandles,
  clipBarsFrom,
  createEmitter,
  groupMarkersByTime,
  isFiniteNumber,
  isRejectedMarker,
  pick,
  precisionFor,
  safeColor,
  withAlpha,
} from './chartUtils'
import type { ResolvedIndicator } from './indicatorsModel'

export interface ChartToggles {
  signals: boolean
  rejected: boolean
  levels: boolean
  cone: boolean
  volume: boolean
  ema: boolean
  scorePane: boolean
}

export interface NexusChartHandle {
  /** Apply a live candle (same bar -> update, newer bar -> append). Older candles are ignored. */
  updateCandle: (c: Candle) => void
  /** Show the most recent bars (plus the prediction cone). */
  resetView: () => void
  /** Fit the whole history. */
  fitAll: () => void
}

interface Props {
  /** `${symbol}|${tf}`: the view (zoom) is reset only when this changes. */
  viewKey: string
  /** Timeframe of the data: 1d / 1w bars are drawn with their UTC session date, intraday bars in local time. */
  tf: string
  candles: Candle[] | null
  nexus: NexusResponse | null
  indicators: ResolvedIndicator[]
  toggles: ChartToggles
  /** Levels of the idea checked in "¿Tomo esta operación?" (drawn dotted). */
  preview: GateResult | null
  entryThreshold: number
  onEditIndicator: (uid: string) => void
  onRemoveIndicator: (uid: string) => void
  onRetryIndicator: (uid: string) => void
  /** Overlays (loading / errors / notices) rendered inside the chart box. */
  children?: ReactNode
}

interface BaseSeries {
  candles: ISeriesApi<'Candlestick'>
  volume: ISeriesApi<'Histogram'>
  emaFast: ISeriesApi<'Line'>
  emaSlow: ISeriesApi<'Line'>
  emaTrend: ISeriesApi<'Line'>
  markers: MarkerLayer
  trade: TradeLevelsLayer
  posLines: LevelLines
  previewLines: LevelLines
  cone: ConeLayer
}

interface IndicatorSeries {
  spec: IndicatorSpec
  outputs: { output: IndicatorOutput; series: ISeriesApi<SeriesType> }[]
  levels: LevelLines | null
}

interface Structure {
  chart: IChartApi
  score: { hist: ISeriesApi<'Histogram'>; prob: ISeriesApi<'Line'>; guides: LevelLines } | null
  inds: Map<string, IndicatorSeries>
}

const OUTPUT_TYPES = new Set<IndicatorOutput['type']>(['line', 'histogram', 'area', 'dots', 'step'])
const PALETTE = ['#2f6bff', '#f5a524', '#a97bff', '#22d3ee', '#f472b6', '#facc15', '#16c784', '#60a5fa']
const RECENT_BARS = 240

function priceFormat(precision: number) {
  return { type: 'price' as const, precision, minMove: Math.pow(10, -precision) }
}

function proposalLevels(g: GateResult): LevelLine[] {
  const side = g.direction === 'long' ? 'LARGO' : 'CORTO'
  return [
    { price: g.entry, color: COLORS.muted, title: `ENTRADA ${side}`, style: LineStyle.Solid },
    { price: g.sl, color: COLORS.neg, title: 'SL · HASTA AQUÍ', style: LineStyle.Dashed, width: 2 },
    { price: g.tp1, color: COLORS.tp1, title: 'TP1' },
    { price: g.tp2, color: COLORS.tp2, title: 'TP2' },
    { price: g.tp3, color: COLORS.tp3, title: 'TP3' },
  ]
}

function previewLevels(g: GateResult): LevelLine[] {
  return [
    { price: g.entry, color: COLORS.info, title: 'IDEA · ENTRADA', style: LineStyle.Dotted },
    { price: g.sl, color: COLORS.neg, title: 'IDEA · SL', style: LineStyle.Dotted, width: 2 },
    { price: g.tp1, color: COLORS.tp1, title: 'IDEA · TP1', style: LineStyle.Dotted },
    { price: g.tp2, color: COLORS.tp2, title: 'IDEA · TP2', style: LineStyle.Dotted },
    { price: g.tp3, color: COLORS.tp3, title: 'IDEA · TP3', style: LineStyle.Dotted },
  ]
}

function anchorOf(current: NexusCurrent | null, candles: Candle[]): { time: number; price: number } | null {
  if (current && isFiniteNumber(current.time) && isFiniteNumber(current.price)) return { time: current.time, price: current.price }
  const last = candles.length ? candles[candles.length - 1] : null
  return last ? { time: last.time, price: last.close } : null
}

function removeStructure(chart: IChartApi, s: Structure): void {
  const all: ISeriesApi<SeriesType>[] = []
  if (s.score) {
    s.score.guides.clear()
    all.push(s.score.hist, s.score.prob)
  }
  for (const ind of s.inds.values()) {
    ind.levels?.clear()
    for (const o of ind.outputs) all.push(o.series)
  }
  for (const series of all) {
    try {
      chart.removeSeries(series)
    } catch {
      /* already removed */
    }
  }
  // Every structure series is gone, so panes above 0 are empty: drop any left behind.
  trimPanes(chart, 1)
}

const NexusChart = forwardRef<NexusChartHandle, Props>(function NexusChart(props, ref) {
  const { viewKey, tf, candles, nexus, indicators, toggles, preview, entryThreshold } = props
  const { ref: boxRef, chart } = useChart({ timeScale: timeScaleFor(tf) })
  const baseRef = useRef<BaseSeries | null>(null)
  const structRef = useRef<Structure | null>(null)
  const candleArrRef = useRef<Candle[]>([])
  /** Timeframe of the candles on screen (live updates and the crosshair convert times with it). */
  const drawnTfRef = useRef<string>(tf)
  const viewKeyRef = useRef<string | null>(null)
  const coneViewKeyRef = useRef<string | null>(null)
  const userMovedRef = useRef(false)
  const coneLenRef = useRef(0)
  const hoveringRef = useRef(false)
  const markersByTimeRef = useRef<Map<number, ChartMarker[]>>(new Map())
  const pricePrecisionRef = useRef(2)
  const hoverBus = useMemo(() => createEmitter<number | null>(), [])
  const tipBus = useMemo(() => createEmitter<TooltipState | null>(), [])
  const [drawError, setDrawError] = useState<string | null>(null)

  const guard = useCallback((label: string, fn: () => void) => {
    try {
      fn()
    } catch (err) {
      console.error(`[NexusChart] ${label}`, err)
      setDrawError(`${label}: ${err instanceof Error ? err.message : String(err)}`)
    }
  }, [])

  const ok = !!nexus?.ok
  // The NEXUS window may start before the first loaded candle (anchored learning window): draw it from there on.
  const firstCandle = candles && candles.length ? candles[0].time : null
  const bars = useMemo(
    () => (ok && nexus ? clipBarsFrom(cleanBars(nexus.bars, tf), firstCandle) : null),
    [ok, nexus?.bars, tf, firstCandle], // eslint-disable-line react-hooks/exhaustive-deps
  )
  const current = ok ? (nexus?.current ?? null) : null
  const markers = useMemo(() => {
    const list = ok ? (nexus?.markers ?? null) : null
    return list && firstCandle !== null ? list.filter((m) => m.time >= firstCandle) : list
  }, [ok, nexus?.markers, firstCandle]) // eslint-disable-line react-hooks/exhaustive-deps
  const prediction = ok ? (nexus?.prediction ?? null) : null

  const resetView = useCallback(() => {
    if (!chart || isChartRemoved(chart)) return
    const arr = candleArrRef.current
    if (arr.length === 0) return
    try {
      const scale = chart.timeScale()
      if (arr.length <= RECENT_BARS + 20) {
        scale.fitContent()
        return
      }
      const lastIdx = scale.timeToIndex(ts(arr[arr.length - 1].time, drawnTfRef.current), true) ?? arr.length - 1
      scale.setVisibleLogicalRange({ from: lastIdx - RECENT_BARS, to: lastIdx + coneLenRef.current + 4 })
    } catch (err) {
      console.warn('[NexusChart] no se pudo ajustar la vista', err)
    }
  }, [chart])

  const fitAll = useCallback(() => {
    if (!chart || isChartRemoved(chart)) return
    try {
      chart.timeScale().fitContent()
    } catch {
      /* chart not ready */
    }
  }, [chart])

  // ------------------------------------------------------------------ base series (once per chart instance)
  useEffect(() => {
    if (!chart) return
    let base: BaseSeries
    try {
      const volume = addVolume(chart, 0)
      const candleSeries = addCandles(chart, 0)
      const emaTrend = addLine(chart, { color: COLORS.muted, width: 1 })
      const emaSlow = addLine(chart, { color: COLORS.purple, width: 1 })
      const emaFast = addLine(chart, { color: COLORS.accent, width: 1 })
      const trade = new TradeLevelsLayer(chart, 0)
      const cone = new ConeLayer(chart, 0)
      base = {
        candles: candleSeries,
        volume,
        emaFast,
        emaSlow,
        emaTrend,
        markers: new MarkerLayer(candleSeries),
        trade,
        posLines: new LevelLines(candleSeries),
        previewLines: new LevelLines(candleSeries),
        cone,
      }
    } catch (err) {
      console.error('[NexusChart] no se pudo crear el gráfico', err)
      setDrawError(`No se pudo crear el gráfico: ${err instanceof Error ? err.message : String(err)}`)
      return
    }
    baseRef.current = base

    const onMove = (param: MouseEventParams<Time>) => {
      const point = param.point
      if (param.time === undefined || !point || point.x < 0 || point.y < 0) {
        if (hoveringRef.current) {
          hoveringRef.current = false
          hoverBus.emit(null)
        }
        tipBus.emit(null)
        return
      }
      hoveringRef.current = true
      const t = fromTs(param.time, drawnTfRef.current)
      hoverBus.emit(t)
      let list: ChartMarker[] | undefined
      if (param.paneIndex === undefined || param.paneIndex === 0) {
        const byTime = markersByTimeRef.current
        list = byTime.get(t)
        if (!list) {
          // ±1 bar tolerance: markers are tiny when zoomed out.
          const arr = candleArrRef.current
          const i = candleIndexAt(arr, t)
          list = (i > 0 ? byTime.get(arr[i - 1].time) : undefined) ?? (i >= 0 && i + 1 < arr.length ? byTime.get(arr[i + 1].time) : undefined)
        }
      }
      tipBus.emit(list && list.length ? { x: point.x, y: point.y, markers: list } : null)
    }
    chart.subscribeCrosshairMove(onMove)

    const el = boxRef.current
    const markMoved = () => {
      userMovedRef.current = true
    }
    // Belt and braces: the legend returns to the last bar whenever the pointer leaves the chart.
    const onLeave = () => {
      hoveringRef.current = false
      hoverBus.emit(null)
      tipBus.emit(null)
    }
    el?.addEventListener('wheel', markMoved, { passive: true })
    el?.addEventListener('mousedown', markMoved)
    el?.addEventListener('touchstart', markMoved, { passive: true })
    el?.addEventListener('mouseleave', onLeave)

    return () => {
      // On unmount useChart's cleanup has already removed the chart (it runs first): its series and plugins must not
      // be touched now. Detaching the markers plugin of a removed chart scheduled a redraw of the destroyed widgets,
      // the "Uncaught Error: Object is disposed" seen when leaving the chart (MarkerLayer is a no-op by now anyway).
      if (!isChartRemoved(chart)) {
        try {
          chart.unsubscribeCrosshairMove(onMove)
          base.markers.detach()
        } catch {
          /* chart in a broken state */
        }
      }
      el?.removeEventListener('wheel', markMoved)
      el?.removeEventListener('mousedown', markMoved)
      el?.removeEventListener('touchstart', markMoved)
      el?.removeEventListener('mouseleave', onLeave)
      baseRef.current = null
      structRef.current = null
      viewKeyRef.current = null
      coneViewKeyRef.current = null
    }
  }, [chart, boxRef, hoverBus, tipBus])

  // ------------------------------------------------------------------ time axis (1d / 1w: dates without a clock)
  useEffect(() => {
    if (!chart) return
    guard('eje de tiempo', () => chart.applyOptions({ timeScale: timeScaleFor(tf) }))
  }, [chart, tf, guard])

  // ------------------------------------------------------------------ candles + volume
  useEffect(() => {
    const b = baseRef.current
    if (!chart || !b) return
    guard('velas', () => {
      const arr = cleanCandles(candles, tf)
      candleArrRef.current = arr
      drawnTfRef.current = tf
      b.candles.setData(toCandleData(arr, tf))
      b.volume.setData(toVolumeData(arr, tf))
      if (arr.length) {
        const d = priceDecimals(arr[arr.length - 1].close)
        pricePrecisionRef.current = d
        b.candles.applyOptions({ priceFormat: priceFormat(d) })
        if (viewKeyRef.current !== viewKey) {
          viewKeyRef.current = viewKey
          coneViewKeyRef.current = null
          userMovedRef.current = false
          resetView()
        }
      }
      if (!hoveringRef.current) hoverBus.emit(null)
    })
  }, [chart, candles, tf, viewKey, guard, resetView, hoverBus])

  // ------------------------------------------------------------------ NEXUS EMAs
  useEffect(() => {
    const b = baseRef.current
    if (!chart || !b) return
    guard('medias NEXUS', () => {
      if (!bars || !toggles.ema) {
        b.emaFast.setData([])
        b.emaSlow.setData([])
        b.emaTrend.setData([])
        return
      }
      b.emaFast.setData(toLineData(bars.time, bars.ema_fast, tf))
      b.emaSlow.setData(toLineData(bars.time, bars.ema_slow, tf))
      b.emaTrend.setData(toLineData(bars.time, bars.ema_trend, tf))
    })
  }, [chart, bars, tf, toggles.ema, guard])

  // ------------------------------------------------------------------ markers
  const visibleMarkers = useMemo(
    () => (markers ?? []).filter((m) => (isRejectedMarker(m) ? toggles.rejected : toggles.signals)),
    [markers, toggles.rejected, toggles.signals],
  )
  useEffect(() => {
    const b = baseRef.current
    if (!chart || !b) return
    markersByTimeRef.current = groupMarkersByTime(visibleMarkers)
    guard('marcadores', () => b.markers.set(visibleMarkers, tf))
  }, [chart, visibleMarkers, tf, guard])

  // ------------------------------------------------------------------ historical SL/TP step lines
  useEffect(() => {
    const b = baseRef.current
    if (!chart || !b) return
    guard('niveles históricos', () => {
      if (bars && toggles.levels) b.trade.set(bars, tf)
      else b.trade.clear()
    })
  }, [chart, bars, tf, toggles.levels, guard])

  // ------------------------------------------------------------------ open position / proposed entry lines
  const position = current?.position ?? null
  const proposal = useMemo<GateResult | null>(() => {
    if (!current || current.position) return null
    const g = current.action === 'BUY' ? current.gate?.long : current.action === 'SELL' ? current.gate?.short : null
    return g && g.verdict === 'TAKE' ? g : null
  }, [current])
  useEffect(() => {
    const b = baseRef.current
    if (!chart || !b) return
    guard('niveles de la posición', () => {
      if (!toggles.levels) b.posLines.clear()
      else if (position) b.posLines.set(positionLevels(position))
      else if (proposal) b.posLines.set(proposalLevels(proposal))
      else b.posLines.clear()
    })
  }, [chart, position, proposal, toggles.levels, guard])

  // ------------------------------------------------------------------ idea checked by the user
  useEffect(() => {
    const b = baseRef.current
    if (!chart || !b) return
    guard('niveles de la idea', () => {
      if (preview) b.previewLines.set(previewLevels(preview))
      else b.previewLines.clear()
    })
  }, [chart, preview, guard])

  // ------------------------------------------------------------------ prediction cone
  const anchorTime = current?.time
  const anchorPrice = current?.price
  useEffect(() => {
    const b = baseRef.current
    if (!chart || !b) return
    guard('cono de predicción', () => {
      const cone = prediction?.cone
      if (!toggles.cone || !Array.isArray(cone) || cone.length === 0) {
        b.cone.clear()
        coneLenRef.current = 0
        return
      }
      const anchor = anchorOf(current, candleArrRef.current)
      const pts = cone
        .filter(
          (p) =>
            p &&
            isFiniteNumber(p.time) &&
            (!anchor || p.time > anchor.time) &&
            isFiniteNumber(p.p10) &&
            isFiniteNumber(p.p25) &&
            isFiniteNumber(p.p50) &&
            isFiniteNumber(p.p75) &&
            isFiniteNumber(p.p90),
        )
        .sort((a, c) => a.time - c.time)
      const clean = pick(pts, ascendingIndices(pts.map((p) => p.time), tf))
      if (clean.length === 0 || (anchor && ts(clean[0].time, tf) <= ts(anchor.time, tf))) {
        b.cone.clear()
        coneLenRef.current = 0
        return
      }
      b.cone.set(clean, anchor ?? undefined, tf)
      coneLenRef.current = clean.length
      if (coneViewKeyRef.current !== viewKey && viewKeyRef.current === viewKey && !userMovedRef.current) {
        coneViewKeyRef.current = viewKey
        resetView()
      }
    })
    // current is represented by its time/price (the cone only needs the anchor)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, prediction, toggles.cone, anchorTime, anchorPrice, viewKey, tf, guard, resetView])

  // ------------------------------------------------------------------ volume visibility
  useEffect(() => {
    const b = baseRef.current
    if (!chart || !b) return
    guard('volumen', () => b.volume.applyOptions({ visible: toggles.volume }))
  }, [chart, toggles.volume, guard])

  // ------------------------------------------------------------------ panes: NEXUS score + indicators
  const structure = useMemo(() => {
    const showScore = toggles.scorePane && !!bars
    // Failed indicators get no series/pane (their error and a retry button stay in the legend).
    const inds = indicators.filter((i): i is ResolvedIndicator & { spec: IndicatorSpec } => !!i.spec && i.status !== 'error')
    const key = [
      showScore ? 'S' : '-',
      ...inds.map(
        (i) => `${i.uid}:${i.spec.id}:${i.spec.overlay ? 'o' : 'p'}:${i.spec.outputs.map((o) => `${o.key}/${o.type}/${o.color}`).join(',')}`,
      ),
    ].join('|')
    return { key, showScore, inds }
  }, [toggles.scorePane, bars, indicators])

  useEffect(() => {
    if (!chart || !baseRef.current) return
    const prev = structRef.current
    if (prev && prev.chart === chart) removeStructure(chart, prev)
    const next: Structure = { chart, score: null, inds: new Map() }
    structRef.current = next
    guard('paneles', () => {
      let paneCount = 1
      if (structure.showScore) {
        const pane = paneCount++
        const fixed = () => ({ priceRange: { minValue: -1, maxValue: 1 } })
        const hist = chart.addSeries(
          HistogramSeries,
          { priceLineVisible: false, lastValueVisible: false, priceFormat: priceFormat(2), autoscaleInfoProvider: fixed },
          pane,
        )
        const prob = addLine(chart, { color: COLORS.cyan, width: 1, pane })
        prob.applyOptions({ autoscaleInfoProvider: fixed, priceFormat: priceFormat(2) })
        next.score = { hist, prob, guides: new LevelLines(hist) }
      }
      for (const ind of structure.inds) {
        const spec = ind.spec
        const pane = spec.overlay ? 0 : paneCount++
        const outputs = spec.outputs.map((o, i) => {
          const output: IndicatorOutput = {
            ...o,
            title: typeof o.title === 'string' ? o.title : o.key,
            type: OUTPUT_TYPES.has(o.type) ? o.type : 'line',
            color: safeColor(o.color, PALETTE[i % PALETTE.length]),
          }
          const series = addSeriesForOutput(chart, output, pane)
          // Names live in the legend; panel indicators show their last value on the axis (TradingView style).
          series.applyOptions({ title: '', lastValueVisible: !spec.overlay, priceLineVisible: false })
          return { output, series }
        })
        const range = spec.range
        if (!spec.overlay && Array.isArray(range) && range.length === 2 && isFiniteNumber(range[0]) && isFiniteNumber(range[1]) && range[1] > range[0]) {
          const fixed = () => ({ priceRange: { minValue: range[0], maxValue: range[1] } })
          for (const o of outputs) o.series.applyOptions({ autoscaleInfoProvider: fixed })
        }
        const levels = !spec.overlay && Array.isArray(spec.levels) && spec.levels.length > 0 && outputs[0] ? new LevelLines(outputs[0].series) : null
        next.inds.set(ind.uid, { spec, outputs, levels })
      }
      // Price pane keeps >= 60 % of the height; extra panes share the rest (max 20 % each).
      const panes = chart.panes()
      const k = panes.length - 1
      if (k > 0) {
        const each = Math.min(0.2, 0.4 / k)
        panes[0].setStretchFactor(1 - each * k)
        for (let i = 1; i < panes.length; i++) panes[i].setStretchFactor(each)
      } else if (panes[0]) {
        panes[0].setStretchFactor(1)
      }
    })
    // `structure` is represented by its key: the panes are rebuilt only when the layout changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, structure.key, guard])

  // score pane data
  useEffect(() => {
    const st = structRef.current
    if (!chart || !st || st.chart !== chart || !st.score) return
    const score = st.score
    guard('panel NEXUS', () => {
      if (!bars) {
        score.hist.setData([])
        score.prob.setData([])
        score.guides.clear()
        return
      }
      score.hist.setData(toHistogramData(bars.time, bars.score, tf, withAlpha(COLORS.pos, 0.6), withAlpha(COLORS.neg, 0.6)))
      score.prob.setData(toLineData(bars.time, bars.prob_up.map((p) => (isFiniteNumber(p) ? p * 2 - 1 : null)), tf))
      score.guides.set([
        { price: entryThreshold, color: COLORS.pos, title: 'UMBRAL COMPRA', style: LineStyle.Dashed },
        { price: -entryThreshold, color: COLORS.neg, title: 'UMBRAL VENTA', style: LineStyle.Dashed },
        { price: 0, color: COLORS.muted, title: '', style: LineStyle.Dotted, axisLabel: false },
      ])
    })
  }, [chart, structure.key, bars, tf, entryThreshold, guard])

  // indicator data
  useEffect(() => {
    const st = structRef.current
    if (!chart || !st || st.chart !== chart) return
    guard('indicadores', () => {
      for (const ind of indicators) {
        const s = st.inds.get(ind.uid)
        if (!s) continue
        const r = ind.result
        if (!r || r.error || r.time.length === 0) {
          for (const o of s.outputs) o.series.setData([])
          s.levels?.clear()
          continue
        }
        const idx = ascendingIndices(r.time, tf)
        const time = pick(r.time, idx)
        const first = s.outputs[0] ? r.outputs[s.outputs[0].output.key] : undefined
        const fmt = priceFormat(s.spec.overlay ? pricePrecisionRef.current : precisionFor(first))
        for (const o of s.outputs) {
          const values = r.outputs[o.output.key]
          if (!Array.isArray(values)) {
            o.series.setData([])
            continue
          }
          o.series.setData(outputData(o.output, time, pick(values, idx), tf))
          o.series.applyOptions({ priceFormat: fmt })
        }
        s.levels?.set(
          (s.spec.levels ?? [])
            .filter(isFiniteNumber)
            .map((lv) => ({ price: lv, color: 'rgba(122, 131, 150, 0.7)', title: '', style: LineStyle.Dashed, axisLabel: false })),
        )
      }
    })
  }, [chart, structure.key, indicators, tf, guard])

  // ------------------------------------------------------------------ imperative API
  useImperativeHandle(
    ref,
    () => ({
      updateCandle: (c: Candle) => {
        const b = baseRef.current
        if (!b) return
        const arr = candleArrRef.current
        const last = arr.length ? arr[arr.length - 1] : null
        if (!last || c.time < last.time) return
        const volume = isFiniteNumber(c.volume) ? c.volume : 0
        const t = ts(c.time, drawnTfRef.current)
        try {
          b.candles.update({ time: t, open: c.open, high: c.high, low: c.low, close: c.close })
          b.volume.update({ time: t, value: volume, color: c.close >= c.open ? COLORS.upVol : COLORS.downVol })
        } catch (err) {
          console.warn('[NexusChart] vela en vivo descartada', err)
          return
        }
        const candle: Candle = { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume }
        if (last.time === c.time) arr[arr.length - 1] = candle
        else arr.push(candle)
        if (!hoveringRef.current) hoverBus.emit(null)
      },
      resetView,
      fitAll,
    }),
    [resetView, fitAll, hoverBus],
  )

  const getCandles = useCallback(() => candleArrRef.current, [])
  const getPrecision = useCallback(() => pricePrecisionRef.current, [])
  const getSize = useCallback(() => {
    const el = boxRef.current
    return { width: el?.clientWidth ?? 800, height: el?.clientHeight ?? 500 }
  }, [boxRef])

  return (
    <div className="chart-box chart-nx">
      <div ref={boxRef} className="chart-canvas" />
      <ChartLegend
        hover={hoverBus}
        getCandles={getCandles}
        getPrecision={getPrecision}
        bars={bars}
        showEma={toggles.ema}
        showScore={structure.showScore}
        indicators={indicators}
        onEdit={props.onEditIndicator}
        onRemove={props.onRemoveIndicator}
        onRetry={props.onRetryIndicator}
      />
      <MarkerTooltip bus={tipBus} getSize={getSize} />
      {drawError && (
        <div className="chart-draw-error" role="status">
          <span className="ellipsis" title={drawError}>
            Problema al dibujar ({drawError})
          </span>
          <button type="button" className="chart-legend-btn" title="Ocultar" onClick={() => setDrawError(null)}>
            <X size={12} />
          </button>
        </div>
      )}
      {props.children}
    </div>
  )
})

export default memo(NexusChart)
