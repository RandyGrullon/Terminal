// Live simulator data model + imperative chart view (no React here: per-bar updates go straight to the chart).
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import type { Candle, ChartMarker, NexusBars, NexusResponse, PositionState } from '../../api/types'
import {
  COLORS,
  LevelLines,
  MarkerLayer,
  addCandles,
  addLine,
  addVolume,
  positionLevels,
  timeScaleFor,
  toCandleData,
  toLineData,
  toVolumeData,
  ts,
} from '../../lib/chart'
import { applyPricePrecision, inferEmaAlpha, isValidMarker, markerKey, sanitizeCandle, sanitizeCandles } from './series'

export const MAX_MARKERS = 400
const MAX_BARS = 5000
const KEEP_BARS = 3500

type EmaKey = 'ema_fast' | 'ema_slow' | 'ema_trend'

const EMA_SPECS: { key: EmaKey; label: string; color: string }[] = [
  { key: 'ema_fast', label: 'EMA rápida', color: COLORS.warn },
  { key: 'ema_slow', label: 'EMA lenta', color: COLORS.info },
  { key: 'ema_trend', label: 'EMA tendencia', color: COLORS.purple },
]

export interface EmaTrack {
  key: EmaKey
  label: string
  color: string
  alpha: number | null
  period: number | null
  values: (number | null)[]
}

export interface EmaLegend {
  key: string
  label: string
  color: string
}

export type PushResult = 'append' | 'update' | 'ignored'

function buildEmaTracks(candles: Candle[], bars: NexusBars | null | undefined): EmaTrack[] {
  if (!bars || !Array.isArray(bars.time) || bars.time.length === 0 || candles.length === 0) return []
  const index = new Map<number, number>()
  bars.time.forEach((t, i) => index.set(t, i))
  const closes = candles.map((c) => c.close)
  const tracks: EmaTrack[] = []
  for (const spec of EMA_SPECS) {
    const src = bars[spec.key]
    if (!Array.isArray(src)) continue
    const values = new Array<number | null>(candles.length)
    let lastValid = -1
    for (let i = 0; i < candles.length; i++) {
      const j = index.get(candles[i].time)
      const v = j === undefined ? null : src[j]
      values[i] = typeof v === 'number' && Number.isFinite(v) ? v : null
      if (values[i] !== null) lastValid = i
    }
    if (lastValid < 0) continue
    const fit = inferEmaAlpha(closes, values)
    if (fit) {
      // Extend a trailing gap (candles newer than the NEXUS window) with the inferred smoothing.
      for (let i = lastValid + 1; i < values.length; i++) {
        const prev = values[i - 1] as number
        values[i] = prev + fit.alpha * (closes[i] - prev)
      }
    }
    tracks.push({ key: spec.key, label: spec.label, color: spec.color, alpha: fit?.alpha ?? null, period: fit?.period ?? null, values })
  }
  return tracks
}

/** Candles, NEXUS markers and EMA tracks of the running simulation (single source of truth for the chart). */
export class SimFeedModel {
  candles: Candle[] = []
  markers: ChartMarker[] = []
  emas: EmaTrack[] = []
  /** Timeframe of the session: the chart converts bar times with it (1d / 1w keep their UTC dates). */
  tf: string | null = null
  /** Bumped whenever the whole data set is replaced or trimmed. */
  version = 0
  private markerKeys = new Set<string>()
  private byTime = new Map<number, ChartMarker[]>()

  load(candles: Candle[], nexus: NexusResponse | null, tf: string | null = null): void {
    this.tf = tf
    this.candles = sanitizeCandles(candles)
    this.emas = buildEmaTracks(this.candles, nexus?.bars)
    this.markers = []
    this.markerKeys.clear()
    this.byTime.clear()
    this.addMarkers(Array.isArray(nexus?.markers) ? nexus.markers : [])
    this.trimIfNeeded()
    this.version++
  }

  clear(): void {
    this.load([], null)
  }

  /** Appends a new bar or updates the last one (same time). Older bars are ignored. */
  pushBar(bar: Candle): PushResult {
    const c = sanitizeCandle(bar)
    if (!c) return 'ignored'
    const n = this.candles.length
    const last = n ? this.candles[n - 1] : null
    if (!last || c.time > last.time) {
      this.candles.push(c)
      for (const t of this.emas) {
        const prev = t.values.length ? t.values[t.values.length - 1] : null
        t.values.push(prev !== null && t.alpha !== null ? prev + t.alpha * (c.close - prev) : null)
      }
      return 'append'
    }
    if (c.time === last.time) {
      this.candles[n - 1] = c
      for (const t of this.emas) {
        const prev = n >= 2 ? t.values[n - 2] : null
        if (t.alpha !== null && prev !== null && prev !== undefined) t.values[n - 1] = prev + t.alpha * (c.close - prev)
      }
      return 'update'
    }
    return 'ignored'
  }

  /** Adds new markers (deduplicated, newest MAX_MARKERS kept). Returns true when something changed. */
  addMarkers(list: readonly ChartMarker[]): boolean {
    let changed = false
    for (const m of list) {
      if (!isValidMarker(m)) continue
      const key = markerKey(m)
      if (this.markerKeys.has(key)) continue
      this.markerKeys.add(key)
      this.markers.push(m)
      const at = this.byTime.get(m.time)
      if (at) at.push(m)
      else this.byTime.set(m.time, [m])
      changed = true
    }
    if (this.markers.length > MAX_MARKERS) {
      this.markers.sort((a, b) => a.time - b.time)
      this.rebuildMarkers(this.markers.slice(-MAX_MARKERS))
      changed = true
    }
    return changed
  }

  /** Keeps memory bounded on very long sessions. Returns true when the data set was trimmed. */
  trimIfNeeded(): boolean {
    if (this.candles.length <= MAX_BARS) return false
    const drop = this.candles.length - KEEP_BARS
    this.candles = this.candles.slice(drop)
    for (const t of this.emas) t.values = t.values.slice(drop)
    const first = this.candles[0].time
    this.rebuildMarkers(this.markers.filter((m) => m.time >= first))
    this.version++
    return true
  }

  markersAt(time: number): ChartMarker[] {
    return this.byTime.get(time) ?? []
  }

  candleAt(time: number): Candle | null {
    const times = this.candles
    let lo = 0
    let hi = times.length - 1
    while (lo <= hi) {
      const mid = (lo + hi) >>> 1
      const t = times[mid].time
      if (t === time) return times[mid]
      if (t < time) lo = mid + 1
      else hi = mid - 1
    }
    return null
  }

  times(): number[] {
    return this.candles.map((c) => c.time)
  }

  lastCandle(): Candle | null {
    return this.candles.length ? this.candles[this.candles.length - 1] : null
  }

  emaLegend(): EmaLegend[] {
    return this.emas.map((t) => ({ key: t.key, color: t.color, label: t.period ? `EMA ${t.period}` : t.label }))
  }

  private rebuildMarkers(list: ChartMarker[]): void {
    this.markers = list
    this.markerKeys = new Set(list.map(markerKey))
    this.byTime.clear()
    for (const m of list) {
      const at = this.byTime.get(m.time)
      if (at) at.push(m)
      else this.byTime.set(m.time, [m])
    }
  }
}

/** Series of the live chart: candles + volume + EMA lines + NEXUS markers + "hasta aquí" price lines. */
export class LiveChartView {
  readonly candles: ISeriesApi<'Candlestick'>
  private volume: ISeriesApi<'Histogram'>
  private emaSeries = new Map<EmaKey, ISeriesApi<'Line'>>()
  private markerLayer: MarkerLayer
  private levels: LevelLines
  private levelKey = ''
  private renderedLen = 0
  private precisionFor = NaN

  constructor(private readonly chart: IChartApi) {
    this.candles = addCandles(chart)
    this.volume = addVolume(chart)
    for (const spec of EMA_SPECS) this.emaSeries.set(spec.key, addLine(chart, { color: spec.color, width: 1 }))
    this.markerLayer = new MarkerLayer(this.candles)
    this.levels = new LevelLines(this.candles)
  }

  /** Full redraw from the model. `fit` = reset the visible range to the most recent bars. */
  render(model: SimFeedModel, fit: boolean): void {
    const timeScale = this.chart.timeScale()
    const prev = fit ? null : timeScale.getVisibleLogicalRange()
    const followEdge = !prev || prev.to >= this.renderedLen - 3
    const tf = model.tf
    try {
      this.chart.applyOptions({ timeScale: timeScaleFor(tf) })
      this.candles.setData(toCandleData(model.candles, tf))
      this.volume.setData(toVolumeData(model.candles, tf))
      const times = model.times()
      for (const [key, series] of this.emaSeries) {
        const track = model.emas.find((t) => t.key === key)
        series.setData(track ? toLineData(times, track.values, tf) : [])
      }
      this.markerLayer.set(model.markers, tf)
    } catch (err) {
      console.error('No se pudo dibujar el gráfico del simulador', err)
    }
    this.updatePrecision(model)
    this.renderedLen = model.candles.length
    if (fit || !prev) this.fitRecent(model.candles.length)
    else if (followEdge) timeScale.scrollToRealTime()
  }

  /** Incremental update after SimFeedModel.pushBar (falls back to a full redraw on any chart error). */
  pushed(model: SimFeedModel, kind: Exclude<PushResult, 'ignored'>): void {
    const c = model.lastCandle()
    if (!c) return
    const t = ts(c.time, model.tf)
    try {
      this.candles.update({ time: t, open: c.open, high: c.high, low: c.low, close: c.close })
      this.volume.update({ time: t, value: c.volume, color: c.close >= c.open ? COLORS.upVol : COLORS.downVol })
      for (const track of model.emas) {
        const series = this.emaSeries.get(track.key)
        if (!series) continue
        const v = track.values[track.values.length - 1]
        series.update(v === null || v === undefined ? { time: t } : { time: t, value: v })
      }
      if (kind === 'append') this.renderedLen = model.candles.length
      this.updatePrecision(model)
    } catch (err) {
      console.error('Actualización incremental fallida; redibujando', err)
      this.render(model, false)
    }
  }

  renderMarkers(model: SimFeedModel): void {
    try {
      this.markerLayer.set(model.markers, model.tf)
    } catch (err) {
      console.error('No se pudieron dibujar las marcas NEXUS', err)
    }
  }

  /** Entry / SL "HASTA AQUÍ" / TP lines of NEXUS' open position (only redrawn when they change). */
  renderLevels(pos: PositionState | null): void {
    const levels = positionLevels(pos)
    const key = levels.map((l) => `${l.title}:${l.price}`).join('|')
    if (key === this.levelKey) return
    this.levelKey = key
    this.levels.set(levels)
  }

  private fitRecent(n: number): void {
    const timeScale = this.chart.timeScale()
    if (n === 0) return
    if (n > 180) timeScale.setVisibleLogicalRange({ from: n - 160, to: n + 8 })
    else timeScale.fitContent()
  }

  private updatePrecision(model: SimFeedModel): void {
    const last = model.lastCandle()
    if (!last) return
    const scale = Math.abs(last.close)
    if (Number.isFinite(this.precisionFor) && scale > this.precisionFor * 0.2 && scale < this.precisionFor * 5) return
    this.precisionFor = scale
    applyPricePrecision(this.candles, last.close)
  }
}
