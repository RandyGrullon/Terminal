// NEXUS automática's speeds: its trades grouped by the timeframe that opened them, as its tabs show them. Same thresholds
// as the backend (app.sim.paper.trade_style): «Rápidas» up to 5m, «Medidas» up to 2h, «Largo plazo» above.
import type { Position, Timeframe } from '../api/types'

export type TradeStyle = 'rapida' | 'medida' | 'largo'

export const STYLE_ORDER: TradeStyle[] = ['rapida', 'medida', 'largo']

export const STYLE_LABEL: Record<TradeStyle, string> = { rapida: 'Rápidas', medida: 'Medidas', largo: 'Largo plazo' }

/** One word per speed, for a timeframe's tag ("5m · rápida"). */
export const STYLE_WORD: Record<TradeStyle, string> = { rapida: 'rápida', medida: 'medida', largo: 'largo plazo' }

/** The timeframes it suggests for each speed (the backend accepts any three). */
export const STYLE_DEFAULT_TF: Record<TradeStyle, Timeframe> = { rapida: '5m', medida: '1h', largo: '4h' }

const TF_SECONDS: Record<string, number> = {
  '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600,
  '8h': 28800, '12h': 43200, '1d': 86400, '3d': 259200, '1w': 604800,
}

/** Seconds of a timeframe ('30m' → 1800); null for an unknown one. */
export function tfSeconds(tf: string | null | undefined): number | null {
  return TF_SECONDS[String(tf ?? '')] ?? null
}

export function styleOf(tf: string | null | undefined): TradeStyle {
  const s = TF_SECONDS[String(tf ?? '')]
  if (!s) return 'medida'
  return s <= 300 ? 'rapida' : s <= 7200 ? 'medida' : 'largo'
}

/** Timeframes sorted fastest first (unknown ones last). */
export function sortTfs<T extends string>(tfs: readonly T[]): T[] {
  return [...tfs].sort((a, b) => (TF_SECONDS[a] ?? 1e12) - (TF_SECONDS[b] ?? 1e12))
}

export interface StyleSummary {
  open: number
  closed: number
  wins: number
  /** Realized P&L of its closed trades ($). */
  pnl: number
}

/** Open and closed trades of one speed («x de n» winners and the realized P&L), or of all with `style` null. */
export function styleSummary(open: readonly Position[], closed: readonly Position[], style: TradeStyle | null): StyleSummary {
  const mine = (p: Position) => style === null || styleOf(p.tf) === style
  const done = closed.filter(mine)
  return {
    open: open.filter(mine).length,
    closed: done.length,
    wins: done.filter((p) => (p.pnl ?? 0) > 0).length,
    pnl: done.reduce((sum, p) => sum + (Number.isFinite(p.pnl) ? p.pnl : 0), 0),
  }
}
