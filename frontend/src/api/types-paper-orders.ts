// Types of the paper-orders feature (re-exported by types.ts). Same contract rules as types.ts.
// Pending limit / stop entry orders and the portfolio risk of the demo account (account 1, simulated: never a broker).
import type { Direction, GateResult, Timeframe, Verdict } from './types'

export type PaperOrderKind = 'limit' | 'stop'
export type PaperOrderStatus = 'pending' | 'filled' | 'cancelled' | 'expired'
export type PaperOrderExpiry = 'gtc' | 'bars' | 'date'
/** Who cancelled it: the user, the gate at the fill (NO TOMAR), a risk rule, the levels (e.g. a gap through the
 *  stop) or the system. */
export type PaperOrderCancelledBy = 'user' | 'gate' | 'risk' | 'levels' | 'system'

/** GET /api/paper/orders · POST /api/paper/orders · WS 'paper' `orders`. A limit fills when the price touches it (at
 *  its price or better); a stop when the price crosses it (at market, slippage included). At the fill the gate is
 *  asked again at the fill price and a NO TOMAR cancels the order. */
export interface PaperOrder {
  id: number
  account_id: number
  symbol: string
  tf: string // timeframe of the idea: gate and NEXUS levels at the fill
  side: Direction
  kind: PaperOrderKind
  label: string // "Compra límite" | "Compra stop" | "Venta límite" | "Venta stop"
  price: number // limit price / stop trigger
  qty: number | null // fixed size; null = sized by risk % of equity at the fill
  risk_pct: number | null // null = risk.per_trade_pct at the fill
  sl: number | null // the user's stop; null = NEXUS stop relative to the fill price
  tp: number | null // the user's target; null = NEXUS targets relative to the fill price
  expiry: PaperOrderExpiry
  expire_bars: number | null
  expire_time: number | null // unix s; null = good till cancelled
  expiry_text: string // "válida hasta cancelarla" · "válida 3 velas de 1h (hasta hoy 14:00)" · "válida hasta mañana 9:30"
  status: PaperOrderStatus
  status_label: string // Pendiente | Ejecutada | Cancelada | Caducada
  origin: 'manual' | 'auto' | 'script'
  gate_verdict: Verdict | null // at placement (asked at the order's own price)
  gate_score: number | null
  forced: boolean // left against a NO TOMAR at placement (it is still re-checked at the fill)
  market_price: number | null // last live price (pending orders only)
  distance_pct: number | null // (price / market_price - 1) * 100
  executing: boolean // triggered: the gate is being asked at the fill price
  position_id: number | null // once filled
  fill_price: number | null
  fill_time: number | null // unix s
  reason: string | null // Spanish: why it was cancelled or expired
  cancelled_by: PaperOrderCancelledBy | null
  created_at: number // ms
  updated_at: number | null // ms
}

export interface PaperOrderRequest {
  symbol: string
  side: Direction
  kind: PaperOrderKind
  price: number
  tf?: Timeframe
  qty?: number
  risk_pct?: number
  sl?: number
  tp?: number
  expiry?: PaperOrderExpiry
  expire_bars?: number // expiry 'bars': 1..1000 bars of tf
  expire_time?: number // expiry 'date': unix s (future, at most 365 days)
  force?: boolean // leave it although the gate says NO TOMAR now (the fill re-checks it anyway)
}

export interface PaperOrderResponse {
  order: PaperOrder
  gate: GateResult | null
}

export interface PaperRiskGroup {
  group: string // asset class (crypto | stock | forex | ...), 'crypto:BTC' when crypto is split by coin, 'sim'
  label: string
  positions: number
  symbols: string[]
  risk: number // money from entry to the current stop of the group's positions
  max: number | null
  full: boolean
}

/** GET /api/paper/risk · WS 'paper' `risk`. Limits are null when switched off (setting 0). Open risk = entry to the
 *  current stop of every open position (no fees); the daily P&L counts only what was realized in the UTC day. */
export interface PaperRisk {
  account_id: number
  equity: number
  open_risk: number
  open_risk_pct: number | null
  max_open_risk_pct: number | null
  open_positions: number
  groups: PaperRiskGroup[]
  max_positions_per_group: number | null
  crypto_one_group: boolean
  day_start: number // unix s, 00:00 UTC
  day_start_balance: number
  daily_pnl: number // realized today (closes and fees)
  daily_pnl_pct: number | null
  daily_low: number // worst realized P&L of the day: the block latches on it
  daily_loss_limit_pct: number | null
  daily_loss_limit: number | null // money
  unrealized_pnl: number | null // shown, not part of the daily limit
  blocked: boolean // daily loss limit reached: no new orders or pending fills until blocked_until
  block_reason: string | null
  blocked_until: number | null // unix s (next 00:00 UTC)
  pending_orders: number
  note: string
}

/** HTTP 409 detail when a portfolio-risk rule blocks an order (never overridable with `force`). */
export interface PaperRiskBlock {
  message: string
  code: 'risk_limit'
  rule: 'daily_loss' | 'open_risk' | 'group'
  risk: PaperRisk | null
}

/** Optional fields of the WS 'paper' message (account 1). */
export interface PaperLiveExtra {
  orders?: PaperOrder[]
  risk?: PaperRisk | null
}
