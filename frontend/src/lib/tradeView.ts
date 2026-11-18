// "Ver operación": what the chart draws for one position of a demo account (the user's or NEXUS automática's): where
// it opened, its stop («hasta aquí»), its targets, the path it has followed and two dashed lines from the current price
// to the next target (green) and to the stop (red). Where a trade ends is not a prediction: the target or the stop,
// whichever comes first (or an earlier exit of NEXUS / the user). Pure helpers: no chart library here.
import type { Position } from '../api/types'

export interface TradeView {
  id: number
  accountId: number
  /** The chart's symbol (an option is not drawn: its levels are premiums, not prices of the stock). */
  symbol: string
  tf: string
  side: 'long' | 'short'
  entryPrice: number
  entryTime: number
  sl: number
  tps: (number | null)[]
  tpHits: number
  trail: number | null
  open: boolean
  exitPrice: number | null
  exitTime: number | null
  exitLabel: string | null
  rMultiple: number | null
}

export interface TradeLevel {
  price: number
  label: string
}

/** Bars drawn to the right of the last candle by the projection lines. */
export const PROJECTION_BARS = 12

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** The view of a position, or null when it cannot be drawn on its market's chart (an option, missing prices). */
export function tradeOf(p: Position | null | undefined): TradeView | null {
  if (!p || p.instrument?.kind === 'option') return null
  if (!fin(p.entry_price) || !fin(p.entry_time) || !fin(p.sl) || p.entry_price <= 0) return null
  const open = p.status !== 'closed'
  return {
    id: p.id,
    accountId: p.account_id,
    symbol: String(p.symbol || '').toUpperCase(),
    tf: p.tf || '1h',
    side: p.side === 'short' ? 'short' : 'long',
    entryPrice: p.entry_price,
    entryTime: p.entry_time,
    sl: p.sl,
    tps: [p.tp1, p.tp2, p.tp3].map((v) => (fin(v) ? v : null)),
    tpHits: fin(p.tp_hits) ? Math.max(0, Math.min(3, Math.round(p.tp_hits))) : 0,
    trail: fin(p.trail) ? p.trail : null,
    open,
    exitPrice: !open && fin(p.exit_price) ? p.exit_price : null,
    exitTime: !open && fin(p.exit_time) ? p.exit_time : null,
    exitLabel: !open ? (p.exit_label ?? p.exit_reason ?? null) : null,
    rMultiple: fin(p.r_multiple) ? p.r_multiple : null,
  }
}

/** The stop that closes the trade now: the dynamic stop when it is tighter than the initial one. */
export function activeStop(t: TradeView): TradeLevel {
  if (t.trail === null) return { price: t.sl, label: 'Stop' }
  const tighter = t.side === 'long' ? t.trail > t.sl : t.trail < t.sl
  return tighter ? { price: t.trail, label: 'Stop dinámico' } : { price: t.sl, label: 'Stop' }
}

/** The next objective: the first target not reached yet (none: the trade is closed or has no targets left). */
export function nextTarget(t: TradeView): TradeLevel | null {
  if (!t.open) return null
  for (let i = t.tpHits; i < 3; i++) {
    const price = t.tps[i]
    if (price !== null) return { price, label: `Objetivo TP${i + 1}` }
  }
  return null
}

export interface TradeProgress {
  price: number
  /** Result so far, % of the entry price (positive = winning), before costs. */
  pnlPct: number
  target: TradeLevel | null
  /** What the price still has to move in the trade's favour to reach the target, % of the current price. */
  toTargetPct: number | null
  stop: TradeLevel
  /** What the price can still move against the trade before the stop, % of the current price (≤ 0 = at or past it). */
  toStopPct: number
}

export function tradeProgress(t: TradeView, price: number): TradeProgress | null {
  if (!fin(price) || price <= 0) return null
  const s = t.side === 'long' ? 1 : -1
  const target = nextTarget(t)
  const stop = activeStop(t)
  return {
    price,
    pnlPct: (s * (price - t.entryPrice) / t.entryPrice) * 100,
    target,
    toTargetPct: target ? (s * (target.price - price) / price) * 100 : null,
    stop,
    toStopPct: (s * (price - stop.price) / price) * 100,
  }
}

export interface ProjectionPoint {
  time: number
  value: number
}

/** Dashed lines from the last candle to the next target and to the stop, PROJECTION_BARS bars ahead (open trades). */
export function projection(
  t: TradeView,
  last: { time: number; price: number } | null,
  barSeconds: number,
): { target: ProjectionPoint[] | null; stop: ProjectionPoint[] } | null {
  if (!t.open || !last || !fin(last.time) || !fin(last.price) || !fin(barSeconds) || barSeconds <= 0) return null
  const end = last.time + PROJECTION_BARS * barSeconds
  const from = { time: last.time, value: last.price }
  const target = nextTarget(t)
  return {
    target: target ? [from, { time: end, value: target.price }] : null,
    stop: [from, { time: end, value: activeStop(t).price }],
  }
}

/** The bar (open time) that contains `t` in ascending `times`, or null when `t` is before the first one. */
export function barAt(times: readonly number[], t: number): number | null {
  let lo = 0
  let hi = times.length - 1
  let ans: number | null = null
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= t) {
      ans = times[mid]
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return ans
}

/** Seconds per bar from the last two candles (the chart's own spacing), or null with fewer than two. */
export function barSpacing(times: readonly number[]): number | null {
  if (times.length < 2) return null
  const d = times[times.length - 1] - times[times.length - 2]
  return d > 0 ? d : null
}

/** Where a trade ends, said honestly: never a prediction. */
export const TRADE_END_NOTE =
  'Termina en el objetivo o en el stop, lo que llegue antes; NEXUS (o tú) puede cerrarla antes. Las líneas punteadas muestran hacia dónde iría en cada caso, no una predicción.'
