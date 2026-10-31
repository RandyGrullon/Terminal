// Types of the realtime feature (re-exported by types.ts). Same contract rules as types.ts.
import type { Candle, Quote, Timeframe } from './types'

/** Where a live price comes from: an exchange (binance…), an API-key provider, TradingView or the local cache. */
export type QuoteSource =
  | 'binance'
  | 'okx'
  | 'coinbase'
  | 'kraken'
  | 'kucoin'
  | 'twelvedata'
  | 'polygon'
  | 'alphavantage'
  | 'tradingview'
  | 'cache'

/** Optional freshness fields of a Quote (all absent on older servers and on SIM quotes). */
export interface QuoteFreshnessFields {
  source?: QuoteSource | string
  /** true only when the source serves real-time data on the user's plan and the price is recent */
  realtime?: boolean
  /** announced (TradingView) or measured (API-key provider) delay of the price, seconds */
  delay_s?: number
  /** as reported by the provider; false = the price is the last one of a closed market */
  market_open?: boolean
}

export type RealtimeQuote = Quote & QuoteFreshnessFields

/** WS `candle` message with the optional source fields of the live price. */
export interface RealtimeCandleMessage extends QuoteFreshnessFields {
  type: 'candle'
  symbol: string
  tf: Timeframe
  candle: Candle
  closed: boolean
}

export type RealtimeProviderState =
  | 'sin_clave' // no key saved
  | 'sin_tiempo_real' // the provider's free plan has no live prices (Alpha Vantage)
  | 'en_espera' // ready, nothing to poll (or disabled / offline)
  | 'activo' // polling
  | 'limite' // plan limits reached: TradingView meanwhile
  | 'plan' // the plan does not include live quotes for these markets
  | 'error'

export interface RealtimeProvider {
  id: 'twelvedata' | 'polygon' | 'alphavantage' | string
  label: string
  configured: boolean
  /** the provider can serve live quotes on some plan (Twelve Data, Polygon) */
  live: boolean
  /** what is real time on which plan (public documentation), Spanish */
  plan: string
  doc_url: string
  state: RealtimeProviderState | string
  detail: string
  credits_min: number
  used_min: number
  /** 0 = no daily limit */
  credits_day: number
  used_day: number
}

export interface RealtimeSymbol {
  symbol: string
  provider: string
  interval_s: number | null
  /** ms of the last successful poll */
  last_at: number | null
  /** ms: provider time of the price */
  ts: number | null
  realtime: boolean | null
  delay_s: number | null
  market_open: boolean | null
  next_in_s: number | null
}

/** GET /api/realtime */
export interface RealtimeStatus {
  enabled: boolean
  offline: boolean
  providers: RealtimeProvider[]
  symbols: RealtimeSymbol[]
  polls: number
  errors: number
  last_error: string | null
  last_poll_at: number | null
}
