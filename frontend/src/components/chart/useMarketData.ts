// Data flow of the chart workspace for one (symbol, tf):
//  - candles + NEXUS fetched in parallel (aborted when the symbol/tf changes),
//  - live candles from the WebSocket topic `market:{SYMBOL}:{tf}` applied imperatively (no React re-render),
//  - when a bar closes: debounced (1.5 s) NEXUS refresh + `barVersion` bump (indicators recompute), zoom kept,
//  - `nexus` socket messages update `current` and merge new markers immediately, then NEXUS is read again unless the
//    bar-close refresh is still pending (a semáforo veto of the closed bar is stored before that message),
//  - semáforo changes (level / "no operar" window) re-read NEXUS through `refreshNexus` (ChartMacro.tsx),
//  - polling fallback when the socket is down or silent, full soft reload after a reconnect.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import type { Candle, CandlesResponse, NexusResponse, Timeframe } from '../../api/types'
import { marketTopic, socket } from '../../api/ws'
import { isAbortError, mergeMarkers, validMarkers } from './chartUtils'

const BAR_CLOSE_DEBOUNCE_MS = 1500
const TICK_COALESCE_MS = 120
const LIVE_SILENT_MS = 90_000
const FAST_TFS = new Set<Timeframe>(['1m', '3m', '5m', '15m'])

interface Scoped<T> {
  scope: string
  value: T
  at: number
}

export interface LiveTick {
  price: number
  time: number
  /** Close of the bar before `time` once a new bar started live (null = the loaded candles hold that bar). */
  prevClose: number | null
}

export interface MarketData {
  candles: CandlesResponse | null
  candlesLoading: boolean
  candlesError: unknown
  nexus: NexusResponse | null
  nexusLoading: boolean
  nexusRefreshing: boolean
  nexusError: unknown
  /** ms timestamp of the last NEXUS update for this symbol/tf */
  nexusUpdatedAt: number | null
  /** Bumped on every successful candle load (indicators recompute everything). */
  dataVersion: number
  /** Bumped (debounced) after each closed bar. */
  barVersion: number
  live: LiveTick | null
  /** Soft reload of candles + NEXUS (keeps the chart zoom). */
  reload: () => void
  refreshNexus: () => void
}

function normalizeNexus(res: NexusResponse): NexusResponse {
  if (!res || typeof res !== 'object') throw new Error('Respuesta de NEXUS no válida')
  return {
    ...res,
    ok: !!res.ok,
    markers: validMarkers(res.markers),
    trades: Array.isArray(res.trades) ? res.trades : [],
    params: res.params && typeof res.params === 'object' ? res.params : {},
  }
}

function validCandle(c: unknown): c is Candle {
  if (!c || typeof c !== 'object') return false
  const k = c as Record<string, unknown>
  return ['time', 'open', 'high', 'low', 'close'].every((f) => typeof k[f] === 'number' && Number.isFinite(k[f] as number))
}

export function useMarketData(symbol: string, tf: Timeframe, onCandle: (c: Candle) => void): MarketData {
  const scope = `${symbol}|${tf}`
  const [candlesS, setCandlesS] = useState<Scoped<CandlesResponse> | null>(null)
  const [candlesLoading, setCandlesLoading] = useState(true)
  const [candlesErr, setCandlesErr] = useState<Scoped<unknown> | null>(null)
  const [nexusS, setNexusS] = useState<Scoped<NexusResponse> | null>(null)
  const [nexusLoading, setNexusLoading] = useState(true)
  const [nexusRefreshing, setNexusRefreshing] = useState(false)
  const [nexusErr, setNexusErr] = useState<Scoped<unknown> | null>(null)
  const [dataVersion, setDataVersion] = useState(0)
  const [barVersion, setBarVersion] = useState(0)
  const [liveS, setLiveS] = useState<Scoped<LiveTick> | null>(null)

  const scopeRef = useRef({ symbol, tf, scope })
  scopeRef.current = { symbol, tf, scope }
  const onCandleRef = useRef(onCandle)
  onCandleRef.current = onCandle

  const mainCtrlRef = useRef<AbortController | null>(null)
  const nexusCtrlRef = useRef<AbortController | null>(null)
  const pollCtrlRef = useRef<AbortController | null>(null)
  const loadedRef = useRef(false)
  const lastTimeRef = useRef(0)
  /** Close of the newest bar seen (loaded or live), and of the bar before the live one after a new bar started. */
  const lastCloseRef = useRef<number | null>(null)
  const prevCloseRef = useRef<number | null>(null)
  const lastLiveAtRef = useRef(0)
  const barTimerRef = useRef<number | null>(null)
  const tickTimerRef = useRef<number | null>(null)
  const pendingTickRef = useRef<LiveTick | null>(null)
  /** scope whose NEXUS data is held (and whether it was ok) */
  const nexusHeldRef = useRef<{ scope: string; ok: boolean } | null>(null)
  const candlesHeldRef = useRef<string | null>(null)
  /** NEXUS requests are numbered: an answer older than the one on screen is dropped (the verdict merges the
   *  semáforo at request time, so a slow initial load must not overwrite a newer refresh). */
  const nexusSeqRef = useRef(0)
  const nexusShownSeqRef = useRef(0)

  const setNexusData = useCallback((sc: string, value: NexusResponse) => {
    nexusHeldRef.current = { scope: sc, ok: value.ok }
    setNexusS({ scope: sc, value, at: Date.now() })
    setNexusErr(null)
  }, [])

  const load = useCallback(
    (soft: boolean) => {
      const { symbol: sym, tf: t, scope: sc } = scopeRef.current
      mainCtrlRef.current?.abort()
      nexusCtrlRef.current?.abort()
      const ctrl = new AbortController()
      mainCtrlRef.current = ctrl
      setCandlesLoading(true)
      setNexusLoading(true)
      setCandlesErr(null)
      setNexusErr(null)
      if (!soft) {
        loadedRef.current = false
        lastTimeRef.current = 0
        prevCloseRef.current = null
      }
      lastLiveAtRef.current = Date.now()

      api
        .candles(sym, t, 1000, ctrl.signal)
        .then((res) => {
          if (ctrl.signal.aborted) return
          if (!res || typeof res !== 'object') throw new Error('Respuesta de velas no válida')
          const candles = Array.isArray(res.candles) ? res.candles.filter(validCandle) : []
          setCandlesS({ scope: sc, value: { ...res, candles }, at: Date.now() })
          candlesHeldRef.current = sc
          lastTimeRef.current = candles.length ? candles[candles.length - 1].time : 0
          lastCloseRef.current = candles.length ? candles[candles.length - 1].close : null
          prevCloseRef.current = null
          loadedRef.current = true
          setDataVersion((v) => v + 1)
        })
        .catch((err) => {
          if (isAbortError(err) || ctrl.signal.aborted) return
          // A failed soft reload keeps the data already on screen.
          if (!soft || candlesHeldRef.current !== sc) setCandlesErr({ scope: sc, value: err, at: Date.now() })
          else console.warn('Recarga de velas fallida', err)
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setCandlesLoading(false)
        })

      const seq = ++nexusSeqRef.current
      api
        .nexus(sym, t, undefined, ctrl.signal)
        .then((res) => {
          if (ctrl.signal.aborted || seq < nexusShownSeqRef.current) return
          nexusShownSeqRef.current = seq
          setNexusData(sc, normalizeNexus(res))
        })
        .catch((err) => {
          if (isAbortError(err) || ctrl.signal.aborted || seq < nexusShownSeqRef.current) return
          if (!soft || nexusHeldRef.current?.scope !== sc) setNexusErr({ scope: sc, value: err, at: Date.now() })
          else console.warn('Recarga de NEXUS fallida', err)
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setNexusLoading(false)
        })
    },
    [setNexusData],
  )

  const refreshNexus = useCallback(() => {
    const { symbol: sym, tf: t, scope: sc } = scopeRef.current
    nexusCtrlRef.current?.abort()
    const ctrl = new AbortController()
    nexusCtrlRef.current = ctrl
    setNexusRefreshing(true)
    // Retrying after a failure with nothing on screen: show the loading state instead of the old error.
    if (nexusHeldRef.current?.scope !== sc) setNexusErr(null)
    const seq = ++nexusSeqRef.current
    api
      .nexus(sym, t, undefined, ctrl.signal)
      .then((res) => {
        if (ctrl.signal.aborted || scopeRef.current.scope !== sc || seq < nexusShownSeqRef.current) return
        nexusShownSeqRef.current = seq
        setNexusData(sc, normalizeNexus(res))
      })
      .catch((err) => {
        if (isAbortError(err) || ctrl.signal.aborted || scopeRef.current.scope !== sc || seq < nexusShownSeqRef.current) return
        if (nexusHeldRef.current?.scope !== sc) setNexusErr({ scope: sc, value: err, at: Date.now() })
        else console.warn('Actualización de NEXUS fallida', err)
      })
      .finally(() => {
        if (nexusCtrlRef.current === ctrl) setNexusRefreshing(false)
      })
  }, [setNexusData])

  const scheduleBarClose = useCallback(() => {
    if (barTimerRef.current !== null) window.clearTimeout(barTimerRef.current)
    barTimerRef.current = window.setTimeout(() => {
      barTimerRef.current = null
      refreshNexus()
      setBarVersion((v) => v + 1)
    }, BAR_CLOSE_DEBOUNCE_MS)
  }, [refreshNexus])

  const applyCandle = useCallback(
    (c: Candle) => {
      if (!validCandle(c) || !loadedRef.current) return
      const last = lastTimeRef.current
      if (c.time < last) return
      const isNewBar = last > 0 && c.time > last
      lastTimeRef.current = c.time
      if (isNewBar) prevCloseRef.current = lastCloseRef.current
      lastCloseRef.current = c.close
      onCandleRef.current(c)
      // Coalesce bursts of ticks into at most ~8 React updates per second (timers also run in background tabs).
      pendingTickRef.current = { price: c.close, time: c.time, prevClose: prevCloseRef.current }
      if (tickTimerRef.current === null) {
        tickTimerRef.current = window.setTimeout(() => {
          tickTimerRef.current = null
          const tick = pendingTickRef.current
          if (tick) setLiveS({ scope: scopeRef.current.scope, value: tick, at: Date.now() })
        }, TICK_COALESCE_MS)
      }
      // A new bar implies the previous one closed (covers feeds that never send closed=true).
      if (isNewBar) scheduleBarClose()
    },
    [scheduleBarClose],
  )

  // --- (symbol, tf) change: full load
  useEffect(() => {
    load(false)
    return () => {
      mainCtrlRef.current?.abort()
      nexusCtrlRef.current?.abort()
      if (barTimerRef.current !== null) window.clearTimeout(barTimerRef.current)
      barTimerRef.current = null
      if (tickTimerRef.current !== null) window.clearTimeout(tickTimerRef.current)
      tickTimerRef.current = null
      pendingTickRef.current = null
    }
  }, [scope, load])

  // --- live socket
  useEffect(() => {
    const sym = symbol.toUpperCase()
    const offSub = socket.subscribe(marketTopic(symbol, tf))
    const offCandle = socket.on('candle', (m) => {
      if (!m || typeof m.symbol !== 'string' || m.symbol.toUpperCase() !== sym || m.tf !== tf || !m.candle) return
      lastLiveAtRef.current = Date.now()
      applyCandle(m.candle)
      if (m.closed) scheduleBarClose()
    })
    const offNexus = socket.on('nexus', (m) => {
      if (!m || typeof m.symbol !== 'string' || m.symbol.toUpperCase() !== sym || m.tf !== tf) return
      const held = nexusHeldRef.current
      if (!held || held.scope !== scopeRef.current.scope || !held.ok) {
        refreshNexus()
        return
      }
      setNexusS((prev) => {
        if (!prev || prev.scope !== scopeRef.current.scope) return prev
        return {
          scope: prev.scope,
          at: Date.now(),
          value: {
            ...prev.value,
            current: m.current ?? prev.value.current,
            markers: mergeMarkers(prev.value.markers, validMarkers(m.markers)),
          },
        }
      })
      // The server sends this message after deciding the closed bar and storing any semáforo veto of its entry. A
      // bar-close refresh that reached it earlier still holds the vetoed entry: its marker survives the merge above and
      // its level lines (bars.pos_*) are not in this message. Read NEXUS again (at most one extra request per closed
      // bar, usually answered from the server's 5 s cache of that view); a pending bar-close refresh runs after this
      // message anyway, so it is not doubled.
      if (barTimerRef.current === null) refreshNexus()
    })
    return () => {
      offSub()
      offCandle()
      offNexus()
    }
  }, [symbol, tf, applyCandle, scheduleBarClose, refreshNexus])

  // --- polling fallback when the live feed is down or silent
  useEffect(() => {
    const every = FAST_TFS.has(tf) ? 60_000 : 120_000
    const id = window.setInterval(() => {
      if (document.hidden || !loadedRef.current) return
      const silent = Date.now() - lastLiveAtRef.current > LIVE_SILENT_MS
      if (socket.status === 'open' && !silent) return
      const sc = scopeRef.current.scope
      pollCtrlRef.current?.abort()
      const ctrl = new AbortController()
      pollCtrlRef.current = ctrl
      api
        .candles(scopeRef.current.symbol, scopeRef.current.tf, 3, ctrl.signal)
        .then((res) => {
          if (ctrl.signal.aborted || scopeRef.current.scope !== sc) return
          const list = Array.isArray(res?.candles) ? res.candles.filter(validCandle) : []
          list.sort((a, b) => a.time - b.time).forEach(applyCandle)
        })
        .catch(() => undefined)
    }, every)
    return () => {
      window.clearInterval(id)
      pollCtrlRef.current?.abort()
    }
  }, [scope, tf, applyCandle])

  // --- soft reload after the socket reconnects (fills any gap in the live feed)
  useEffect(() => {
    let seenOpen = false
    let dropped = false
    return socket.onStatus((s) => {
      if (s === 'open') {
        if (seenOpen && dropped && loadedRef.current) load(true)
        seenOpen = true
        dropped = false
      } else if (s === 'closed' && seenOpen) {
        dropped = true
      }
    })
  }, [load])

  const reload = useCallback(() => load(true), [load])

  const candles = candlesS && candlesS.scope === scope ? candlesS.value : null
  const candlesError = candlesErr && candlesErr.scope === scope ? candlesErr.value : null
  const nexus = nexusS && nexusS.scope === scope ? nexusS.value : null
  const nexusError = nexusErr && nexusErr.scope === scope ? nexusErr.value : null
  const live = liveS && liveS.scope === scope ? liveS.value : null

  return {
    candles,
    candlesLoading: candlesLoading || (!candles && !candlesError),
    candlesError,
    nexus,
    nexusLoading: nexusLoading || (!nexus && !nexusError),
    nexusRefreshing,
    nexusError,
    nexusUpdatedAt: nexus && nexusS ? nexusS.at : null,
    dataVersion,
    barVersion,
    live,
    reload,
    refreshNexus,
  }
}
