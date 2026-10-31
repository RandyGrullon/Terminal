// Quote freshness, as the UI says it (pure: no I/O, every input explicit): where a price comes from and how fresh it
// is, never claiming more than the source does ("Tiempo real · Twelve Data", "Retraso 15 min · TradingView",
// "Mercado cerrado · Twelve Data", "Desactualizado · hace 2 h"). Used by the chart toolbar badge
// (components/chart/QuoteFreshness.tsx) and usable by any list that shows quotes.
import type { CandlesResponse, RealtimeQuote, RealtimeStatus, Timeframe } from '../../api/types'
import { fmtPrice, type Tone } from '../../lib/format'

export const QUOTE_POLL_MS = 60_000
export const STATUS_POLL_MS = 300_000
/** a live WS update older than this no longer describes the price on screen (the quote decides) */
export const LIVE_INFO_MS = 90_000
/** a quote read longer ago than this is not used */
export const QUOTE_INFO_MS = 180_000
/** a price claimed real time but older than this (paused tab, feed stopped) is shown as outdated */
export const REALTIME_MAX_AGE_MS = 300_000
/** an API-key quote (minute precision, polled every few dozen seconds on free plans) older than this is shown with
 *  its age instead of "Tiempo real" */
export const KEYED_LIVE_MAX_S = 150

export const SOURCE_LABEL: Record<string, string> = {
  binance: 'Binance',
  okx: 'OKX',
  coinbase: 'Coinbase',
  kraken: 'Kraken',
  kucoin: 'KuCoin',
  twelvedata: 'Twelve Data',
  polygon: 'Polygon',
  alphavantage: 'Alpha Vantage',
  tradingview: 'TradingView',
  cache: 'caché local',
}
const EXCHANGES = new Set(['binance', 'okx', 'coinbase', 'kraken', 'kucoin'])
const KEYED = new Set(['twelvedata', 'polygon', 'alphavantage'])

export type FreshnessKind = 'realtime' | 'recent' | 'delayed' | 'closed' | 'stale' | 'synthetic' | 'unknown'

export interface Freshness {
  kind: FreshnessKind
  label: string
  tone: Tone
  title: string
}

/** Source fields of the newest live candle of this chart (`at` = client ms when it arrived). */
export interface LiveSourceInfo {
  source: string
  realtime?: boolean
  delay_s?: number
  at: number
}

export interface FreshnessInput {
  symbol: string
  quote: RealtimeQuote | null
  /** client ms when the quote was read */
  quoteAt: number | null
  live: LiveSourceInfo | null
  candles: CandlesResponse | null
  tf: Timeframe
  nowMs: number
  status?: RealtimeStatus | null
}

/** "45 s" / "12 min" / "3 h" / "2 días" */
export function agoText(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s} s`
  if (s < 90 * 60) return `${Math.round(s / 60)} min`
  if (s < 48 * 3600) return `${Math.round(s / 3600)} h`
  const d = Math.round(s / 86400)
  return d === 1 ? '1 día' : `${d} días`
}

function sourceName(source: string | undefined): string {
  return source ? (SOURCE_LABEL[source] ?? source) : ''
}

const TF_SECONDS: Record<string, number> = {
  '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600,
  '12h': 43200, '1d': 86400, '1w': 604800,
}

function planNote(status: RealtimeStatus | null | undefined, provider: string): string {
  const p = status?.providers?.find((x) => x.id === provider)
  return p?.plan ? ` ${p.label}: ${p.plan}` : ''
}

function cadenceNote(status: RealtimeStatus | null | undefined, symbol: string): string {
  const s = status?.symbols?.find((x) => x.symbol === symbol)
  return s?.interval_s ? ` Se consulta cada ~${Math.round(s.interval_s)} s para respetar los límites de tu plan.` : ''
}

/** What the badge says (null: nothing worth showing yet). Pure: every input is explicit. */
export function describeFreshness(input: FreshnessInput): Freshness | null {
  const { symbol, quote, quoteAt, live, candles, nowMs, status } = input
  if (symbol.toUpperCase().startsWith('SIM:')) {
    return {
      kind: 'synthetic',
      label: 'Sintético',
      tone: 'purple',
      title: 'Mercado sintético del simulador de NEXUS: los precios son simulados, no reales.',
    }
  }
  const liveOk = live && nowMs - live.at <= LIVE_INFO_MS ? live : null
  const quoteOk = quote && quoteAt !== null && nowMs - quoteAt <= QUOTE_INFO_MS ? quote : null
  const source = liveOk?.source ?? quoteOk?.source ?? ''
  const name = sourceName(source)
  const tail = name ? ` · ${name}` : ''
  const priceAge = quoteOk && typeof quoteOk.ts === 'number' ? (nowMs - quoteOk.ts) / 1000 : null
  if (quoteOk?.stale && !(liveOk?.realtime === true && EXCHANGES.has(liveOk.source))) {
    const age = priceAge ?? 0
    return {
      kind: 'stale',
      label: `Desactualizado · hace ${agoText(age)}`,
      tone: 'neg',
      title: `No hay datos recientes de este activo: el último precio guardado (${fmtPrice(quoteOk.price)}) es de hace ${agoText(age)}. No es un precio en vivo.`,
    }
  }
  if (quoteOk?.market_open === false && (!liveOk || KEYED.has(liveOk.source))) {
    // TradingView's `ts` is when it answered, not when the price was set: no age for it
    const age = priceAge !== null && source !== 'tradingview' ? ` (hace ${agoText(priceAge)})` : ''
    return {
      kind: 'closed',
      label: `Mercado cerrado${tail}`,
      tone: 'muted',
      title: `El mercado está cerrado: ${fmtPrice(quoteOk.price)} es el último precio de ${name || 'la fuente'}${age}.`,
    }
  }
  const delay = liveOk ? liveOk.delay_s : quoteOk?.delay_s
  if (typeof delay === 'number' && delay >= 60) {
    const minutes = Math.round(delay / 60)
    const hint =
      source === 'tradingview'
        ? ' Con una clave gratuita de Twelve Data (Ajustes → Proveedores) las acciones de EE. UU. y el forex van en tiempo real.' +
          planNote(status, 'twelvedata')
        : ''
    return {
      kind: 'delayed',
      label: `Retraso ${minutes} min${tail}`,
      tone: 'warn',
      title: `${name || 'La fuente'} ${source === 'tradingview' ? 'anuncia' : 'da'} un retraso de ${minutes} min para este mercado: el precio y la vela en curso son de hace unos ${minutes} min.${hint}`,
    }
  }
  const realtime = liveOk ? liveOk.realtime === true : quoteOk?.realtime === true
  if (realtime && name) {
    if (!liveOk && priceAge !== null && priceAge * 1000 > REALTIME_MAX_AGE_MS) {
      return {
        kind: 'stale',
        label: `Desactualizado · hace ${agoText(priceAge)}`,
        tone: 'neg',
        title: `La última cotización de ${name} es de hace ${agoText(priceAge)}: no se está actualizando.`,
      }
    }
    if (!liveOk && priceAge !== null && KEYED.has(source) && priceAge > KEYED_LIVE_MAX_S) {
      return {
        kind: 'recent',
        label: `${name} · hace ${agoText(priceAge)}`,
        tone: 'info',
        title: `Último precio en tiempo real de ${name}, de hace ${agoText(priceAge)}: se consulta cada cierto tiempo para no superar los límites de tu plan.${cadenceNote(status, symbol)}${planNote(status, source)}`,
      }
    }
    const what = EXCHANGES.has(source)
      ? `Precio en tiempo real del exchange (${name}).`
      : KEYED.has(source)
        ? `Precio en tiempo real de ${name} con tu clave.${planNote(status, source)}${cadenceNote(status, symbol)}`
        : `${name} anuncia datos en tiempo real para este activo.`
    const age = priceAge !== null && !liveOk ? ` Última cotización hace ${agoText(priceAge)}.` : ''
    return { kind: 'realtime', label: `Tiempo real${tail}`, tone: 'pos', title: `${what}${age}` }
  }
  if (name) {
    return {
      kind: 'unknown',
      label: name,
      tone: 'muted',
      title: `Precio de ${name}; la fuente no indica si es en tiempo real o con retraso.`,
    }
  }
  const last = candles?.candles?.length ? candles.candles[candles.candles.length - 1] : null
  if (candles?.stale && last) {
    const age = nowMs / 1000 - (last.time + (TF_SECONDS[input.tf] ?? 0))
    return {
      kind: 'stale',
      label: `Desactualizado · hace ${agoText(age)}`,
      tone: 'neg',
      title: candles.note || 'La última vela es mucho más antigua de lo esperado: no son datos en vivo.',
    }
  }
  return null
}
