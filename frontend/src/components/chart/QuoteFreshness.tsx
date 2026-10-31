// Freshness badge of the chart toolbar: where the price on screen comes from and how fresh it is, never claiming
// more than the source says ("Tiempo real · Twelve Data", "Retraso 15 min · TradingView", "Mercado cerrado · …",
// "Desactualizado · hace 2 h"). Inputs: the source fields of the live WS candles of this chart (backend live feed),
// the quote re-read every 60 s (/api/quotes: no provider credits are spent per read) and, for the tooltip, what each
// API-key plan serves in real time (/api/realtime, every 5 min).
import { useEffect, useRef, useState } from 'react'
import { Clock, FlaskConical, Hourglass, Moon, Radio, TriangleAlert } from 'lucide-react'
import { api, request } from '../../api/client'
import { socket } from '../../api/ws'
import type { CandlesResponse, RealtimeCandleMessage, RealtimeQuote, RealtimeStatus, Timeframe } from '../../api/types'
import {
  QUOTE_POLL_MS,
  STATUS_POLL_MS,
  describeFreshness,
  type Freshness,
  type FreshnessKind,
  type LiveSourceInfo,
} from '../markets/freshness'

const ICONS: Record<FreshnessKind, typeof Radio> = {
  realtime: Radio,
  recent: Clock,
  delayed: Hourglass,
  closed: Moon,
  stale: TriangleAlert,
  synthetic: FlaskConical,
  unknown: Clock,
}

/** Presentational badge (same look as ui.tsx Badge). */
export function QuoteFreshnessBadge({ freshness }: { freshness: Freshness | null }) {
  if (!freshness) return null
  const Icon = ICONS[freshness.kind]
  return (
    <span className={`badge badge-${freshness.tone} chart-fresh`} title={freshness.title} aria-label={`Origen del precio: ${freshness.label}`}>
      <Icon size={11} /> {freshness.label}
    </span>
  )
}

function sameInfo(a: LiveSourceInfo, b: LiveSourceInfo): boolean {
  return a.source === b.source && a.realtime === b.realtime && a.delay_s === b.delay_s
}

/** Badge of the chart toolbar for (symbol, tf); `candles` = the chart's candles (fallback when no quote). */
export default function QuoteFreshness({ symbol, tf, candles }: { symbol: string; tf: Timeframe; candles: CandlesResponse | null }) {
  const sym = symbol.toUpperCase()
  const scope = `${sym}|${tf}`
  const [quote, setQuote] = useState<{ symbol: string; quote: RealtimeQuote | null; at: number } | null>(null)
  const [live, setLive] = useState<{ scope: string; info: LiveSourceInfo } | null>(null)
  const [status, setStatus] = useState<RealtimeStatus | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const busy = useRef(false)

  // quote of the symbol: every 60 s while the page is visible (served from the server caches, no credits spent)
  useEffect(() => {
    let alive = true
    const load = () => {
      if (document.hidden || busy.current) return
      busy.current = true
      api
        .quotes([sym])
        .then((res) => {
          if (!alive) return
          const q = (res?.quotes ?? {})[sym] as RealtimeQuote | undefined
          setQuote({ symbol: sym, quote: q && typeof q === 'object' ? q : null, at: Date.now() })
        })
        .catch(() => undefined)
        .finally(() => {
          busy.current = false
        })
    }
    load()
    const id = window.setInterval(load, QUOTE_POLL_MS)
    const onVisible = () => {
      if (!document.hidden) load()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive = false
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [sym])

  // source of the live WS candles of this chart (the topic itself is subscribed by useMarketData)
  useEffect(
    () =>
      socket.on('candle', (m) => {
        const msg = m as unknown as RealtimeCandleMessage
        if (!msg || typeof msg.symbol !== 'string' || msg.symbol.toUpperCase() !== sym || msg.tf !== tf || !msg.source) return
        const info: LiveSourceInfo = {
          source: String(msg.source),
          realtime: typeof msg.realtime === 'boolean' ? msg.realtime : undefined,
          delay_s: typeof msg.delay_s === 'number' ? msg.delay_s : undefined,
          at: Date.now(),
        }
        // re-render at most every 10 s while the source does not change (crypto ticks arrive every few seconds)
        setLive((prev) => (prev && prev.scope === scope && sameInfo(prev.info, info) && info.at - prev.info.at < 10_000 ? prev : { scope, info }))
      }),
    [sym, tf, scope],
  )

  // what each API-key plan serves in real time (tooltip), every 5 min; an older server without the endpoint is fine
  useEffect(() => {
    let alive = true
    const load = () => {
      request<RealtimeStatus>('GET', '/api/realtime')
        .then((s) => {
          if (alive && s && Array.isArray(s.providers)) setStatus(s)
        })
        .catch(() => undefined)
    }
    load()
    const id = window.setInterval(load, STATUS_POLL_MS)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [])

  // clock of the ages shown ("hace 3 min")
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 15_000)
    return () => window.clearInterval(id)
  }, [])

  const q = quote && quote.symbol === sym ? quote : null
  const l = live && live.scope === scope ? live.info : null
  const freshness = describeFreshness({
    symbol: sym,
    quote: q?.quote ?? null,
    quoteAt: q?.at ?? null,
    live: l,
    candles,
    tf,
    nowMs: Math.max(now, l?.at ?? 0, q?.at ?? 0),
    status,
  })
  return <QuoteFreshnessBadge freshness={freshness} />
}
