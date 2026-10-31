// Change % next to the price in the chart toolbar. The quote's `change` is relative to the previous session close,
// a reference that moves when the session rolls over (00:00 UTC for 24/7 crypto, the exchange session for the
// rest). The page re-reads the quote periodically and right after 00:00 UTC; a snapshot is only trusted as "the
// day" while it belongs to the current UTC day and is recent. Otherwise the change is shown against the previous
// candle and labeled as such. No React here.
import type { Candle, Quote } from '../../api/types'

/** The chart page re-reads the symbol snapshot (quote) this often while visible. */
export const DETAIL_REFRESH_MS = 120_000
/** Wait after 00:00 UTC before re-reading, so the quote provider has rolled its daily change. */
export const ROLLOVER_DELAY_MS = 5_000
/** A snapshot older than this (re-reads failing) no longer defines today's change. */
export const QUOTE_MAX_AGE_MS = 15 * 60_000

export const DAY_BASIS = 'Variación del día (frente al cierre de la sesión anterior)'
export const CANDLE_BASIS = 'Variación respecto al cierre de la vela anterior'

const DAY_MS = 86_400_000

export interface PriceChange {
  pct: number
  basis: string
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Milliseconds from `nowMs` to the next 00:00 UTC. */
export function msToNextUtcDay(nowMs: number): number {
  return (Math.floor(nowMs / DAY_MS) + 1) * DAY_MS - nowMs
}

/** The snapshot fetched at `fetchedAt` (client ms) can still define today's change. */
export function quoteIsCurrent(fetchedAt: number | null, nowMs: number): boolean {
  if (fetchedAt === null || !isNum(fetchedAt) || fetchedAt > nowMs + 60_000) return false
  return Math.floor(fetchedAt / DAY_MS) === Math.floor(nowMs / DAY_MS) && nowMs - fetchedAt <= QUOTE_MAX_AGE_MS
}

/** The quote's own time (`ts`, the real time of its price) is in the current UTC day: a cached or stale price of an
 *  earlier day carries that day's reference close even when it was fetched just now. A quote without a usable `ts`
 *  is judged by its fetch time only. */
export function quoteOfToday(quote: Pick<Quote, 'ts'>, nowMs: number): boolean {
  if (!isNum(quote.ts) || quote.ts <= 0) return true
  const ms = quote.ts > 1e11 ? quote.ts : quote.ts * 1000
  return Math.floor(ms / DAY_MS) === Math.floor(nowMs / DAY_MS)
}

export function priceChange(input: {
  /** Price on screen (live tick, last candle or quote). */
  price: number | null
  quote: Quote | null
  /** Client time (ms) when `quote` was fetched. */
  quoteAt: number | null
  nowMs: number
  /** Candles loaded for the chart (fallback reference). */
  candles: readonly Candle[] | null
  /** Bar time of the latest live tick, if any, and the close of the bar before it when that bar was seen live
   *  (null / missing = the loaded candles define it). */
  live: { time: number; prevClose?: number | null } | null
}): PriceChange | null {
  const { price, quote, candles, live } = input
  if (!isNum(price)) return null
  if (quote && quoteIsCurrent(input.quoteAt, input.nowMs) && quoteOfToday(quote, input.nowMs)) {
    if (isNum(quote.price) && isNum(quote.change) && quote.price - quote.change > 0) {
      const prevClose = quote.price - quote.change
      return { pct: (price / prevClose - 1) * 100, basis: DAY_BASIS }
    }
    if (isNum(quote.change_pct) && quote.price === price) return { pct: quote.change_pct, basis: DAY_BASIS }
  }
  if (candles && candles.length >= 2) {
    const last = candles[candles.length - 1]
    // Once bars went by live, the previous candle is the one before the live bar, not the last one loaded.
    const prev =
      live && live.time > last.time ? (isNum(live.prevClose) ? live.prevClose : last.close) : candles[candles.length - 2].close
    if (prev > 0) return { pct: (price / prev - 1) * 100, basis: CANDLE_BASIS }
  }
  return null
}
