// Defensive data helpers shared by the simulator, backtest and Monte Carlo charts.
// lightweight-charts throws on unsorted / duplicated times, so every series goes through these first.
import type { ISeriesApi, SeriesType } from 'lightweight-charts'
import type { Candle, ChartMarker, NexusBars, Trade } from '../../api/types'
import { priceDecimals } from '../../lib/format'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Validates one candle (finite OHLC and time); a missing/invalid volume becomes 0. */
export function sanitizeCandle(c: Candle | null | undefined): Candle | null {
  if (!c || !isNum(c.time) || !isNum(c.open) || !isNum(c.high) || !isNum(c.low) || !isNum(c.close)) return null
  return {
    time: Math.floor(c.time),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
    volume: isNum(c.volume) ? c.volume : 0,
  }
}

/** Ascending, unique-time copy (the last item wins on duplicated times). */
export function sortDedupeByTime<T extends { time: number }>(xs: readonly T[]): T[] {
  let sorted = true
  for (let i = 1; i < xs.length; i++) {
    if (!(xs[i].time > xs[i - 1].time)) {
      sorted = false
      break
    }
  }
  if (sorted) return xs.slice()
  const indexed = xs.map((x, i) => ({ x, i }))
  indexed.sort((a, b) => a.x.time - b.x.time || a.i - b.i)
  const out: T[] = []
  for (const { x } of indexed) {
    if (out.length && out[out.length - 1].time === x.time) out[out.length - 1] = x
    else out.push(x)
  }
  return out
}

export function sanitizeCandles(cs: readonly Candle[] | null | undefined): Candle[] {
  if (!Array.isArray(cs)) return []
  const valid: Candle[] = []
  for (const c of cs) {
    const s = sanitizeCandle(c)
    if (s) valid.push(s)
  }
  return sortDedupeByTime(valid)
}

export function isValidMarker(m: ChartMarker | null | undefined): m is ChartMarker {
  return !!m && isNum(m.time) && typeof m.kind === 'string'
}

export function markerKey(m: ChartMarker): string {
  return `${m.time}|${m.kind}|${m.position}|${m.text ?? ''}`
}

/** Groups markers by bar time (for hover tooltips). */
export function indexMarkers(markers: readonly ChartMarker[] | null | undefined): Map<number, ChartMarker[]> {
  const map = new Map<number, ChartMarker[]>()
  if (!Array.isArray(markers)) return map
  for (const m of markers) {
    if (!isValidMarker(m)) continue
    const list = map.get(m.time)
    if (list) list.push(m)
    else map.set(m.time, [m])
  }
  return map
}

/** Index of the first element with time >= t (binary search over an ascending array). */
export function lowerBound(times: readonly number[], t: number): number {
  let lo = 0
  let hi = times.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (times[mid] < t) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** Typical bar spacing in seconds (median of the last diffs); 3600 when unknown. */
export function barSeconds(candles: readonly Candle[]): number {
  const diffs: number[] = []
  for (let i = Math.max(1, candles.length - 200); i < candles.length; i++) {
    const d = candles[i].time - candles[i - 1].time
    if (d > 0) diffs.push(d)
  }
  if (!diffs.length) return 3600
  diffs.sort((a, b) => a - b)
  return diffs[Math.floor(diffs.length / 2)]
}

/** Sets the price format of a price series to the precision that the instrument needs. */
export function applyPricePrecision(series: ISeriesApi<SeriesType>, price: number | null | undefined): void {
  if (!isNum(price) || price === 0) return
  const d = Math.min(10, priceDecimals(price))
  try {
    series.applyOptions({ priceFormat: { type: 'price', precision: d, minMove: Number((10 ** -d).toFixed(d)) } })
  } catch {
    /* series removed */
  }
}

export interface EmaFit {
  alpha: number
  period: number | null
}

/**
 * Infers the smoothing factor of an EMA series from its values and the closes (least squares through the origin of
 * Δema on (close − ema_prev)). Returns null when the series does not behave like a close-based EMA.
 * Used to keep the snapshot's EMA lines growing bar by bar while the simulator streams new candles.
 */
export function inferEmaAlpha(closes: readonly number[], ema: readonly (number | null)[], window = 400): EmaFit | null {
  const n = Math.min(closes.length, ema.length)
  let sxy = 0
  let sxx = 0
  let syy = 0
  let count = 0
  for (let i = Math.max(1, n - window); i < n; i++) {
    const prev = ema[i - 1]
    const cur = ema[i]
    const c = closes[i]
    if (!isNum(prev) || !isNum(cur) || !isNum(c)) continue
    const x = c - prev
    const y = cur - prev
    sxy += x * y
    sxx += x * x
    syy += y * y
    count++
  }
  if (count < 10 || sxx <= 0 || syy <= 0) return null
  const k = sxy / sxx
  if (!(k > 0 && k < 1)) return null
  const r2 = 1 - (syy - 2 * k * sxy + k * k * sxx) / syy
  if (!(r2 >= 0.95)) return null
  const p = 2 / k - 1
  const pr = Math.round(p)
  if (pr >= 1 && Math.abs(p - pr) <= Math.max(0.5, pr * 0.03)) return { alpha: 2 / (pr + 1), period: pr }
  return { alpha: k, period: null }
}

/**
 * Per-bar entry / initial SL / TP arrays built from backtest trades, in the NexusBars shape expected by
 * TradeLevelsLayer (only `time` and `pos_*` are used by that layer).
 */
export function tradeLevelBars(candles: readonly Candle[], trades: readonly Trade[]): NexusBars {
  const time = candles.map((c) => c.time)
  const n = time.length
  const mk = () => new Array<number | null>(n).fill(null)
  const pos_entry = mk()
  const pos_sl = mk()
  const pos_tp1 = mk()
  const pos_tp2 = mk()
  const pos_tp3 = mk()
  if (n > 0) {
    for (const tr of trades) {
      if (!isNum(tr.entry_time) || !isNum(tr.entry_price)) continue
      const i0 = lowerBound(time, tr.entry_time)
      const exitT = isNum(tr.exit_time) ? tr.exit_time : time[n - 1]
      const i1 = lowerBound(time, exitT + 1) - 1
      for (let i = i0; i <= i1 && i < n; i++) {
        pos_entry[i] = tr.entry_price
        pos_sl[i] = isNum(tr.sl_initial) ? tr.sl_initial : null
        pos_tp1[i] = isNum(tr.tp1) ? tr.tp1 : null
        pos_tp2[i] = isNum(tr.tp2) ? tr.tp2 : null
        pos_tp3[i] = isNum(tr.tp3) ? tr.tp3 : null
      }
    }
  }
  return {
    time,
    score: [],
    prob_up: [],
    action: [],
    regime: [],
    ema_fast: [],
    ema_slow: [],
    ema_trend: [],
    pos_entry,
    pos_sl,
    pos_tp1,
    pos_tp2,
    pos_tp3,
  }
}
