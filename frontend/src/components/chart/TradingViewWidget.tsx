// Official free TradingView chart widget (https://s3.tradingview.com/tv.js): shows ANY TradingView asset with
// TradingView's own chart and indicator menu. The script is loaded once per session.
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { Tv } from 'lucide-react'
import type { Timeframe } from '../../api/types'
import { Empty, ErrorBox, Loading } from '../ui'

interface TradingViewWidgetOptions {
  container_id: string
  symbol: string
  interval: string
  theme: 'dark' | 'light'
  locale: string
  autosize: boolean
  timezone: string
  allow_symbol_change: boolean
  style?: string
  enable_publishing?: boolean
  withdateranges?: boolean
  hide_side_toolbar?: boolean
  details?: boolean
  calendar?: boolean
}

declare global {
  interface Window {
    TradingView?: {
      widget: new (options: TradingViewWidgetOptions) => unknown
    }
  }
}

const TV_SRC = 'https://s3.tradingview.com/tv.js'
const SCRIPT_ID = 'nexus-tradingview-tvjs'
const LOAD_TIMEOUT_MS = 20_000

export const TV_INTERVAL: Record<Timeframe, string> = {
  '1m': '1',
  '3m': '3',
  '5m': '5',
  '15m': '15',
  '30m': '30',
  '1h': '60',
  '2h': '120',
  '4h': '240',
  '6h': '360',
  '12h': '720',
  '1d': 'D',
  '1w': 'W',
}

let tvPromise: Promise<void> | null = null

function loadTradingView(): Promise<void> {
  if (window.TradingView?.widget) return Promise.resolve()
  if (tvPromise) return tvPromise
  tvPromise = new Promise<void>((resolve, reject) => {
    document.getElementById(SCRIPT_ID)?.remove()
    const script = document.createElement('script')
    script.id = SCRIPT_ID
    script.src = TV_SRC
    script.async = true
    let timer = 0
    function fail(message: string) {
      window.clearTimeout(timer)
      script.remove()
      tvPromise = null
      reject(new Error(message))
    }
    timer = window.setTimeout(
      () => fail('TradingView tardó demasiado en responder. Revisa tu conexión a Internet.'),
      LOAD_TIMEOUT_MS,
    )
    script.onload = () => {
      window.clearTimeout(timer)
      if (window.TradingView?.widget) resolve()
      else fail('El script de TradingView se cargó pero no está disponible.')
    }
    script.onerror = () => fail('No se pudo cargar el gráfico de TradingView (¿sin conexión a Internet o bloqueado por la red?).')
    document.head.appendChild(script)
  })
  return tvPromise
}

function TradingViewWidget(props: { symbol: string; tf: Timeframe }) {
  const { symbol, tf } = props
  const hostRef = useRef<HTMLDivElement | null>(null)
  const containerId = useMemo(() => `nexus_tv_${Math.random().toString(36).slice(2, 10)}`, [])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const simulated = symbol.toUpperCase().startsWith('SIM:')

  useEffect(() => {
    if (simulated) return
    const host = hostRef.current
    let alive = true
    setState('loading')
    setError(null)
    loadTradingView()
      .then(() => {
        if (!alive || !host || !window.TradingView) return
        host.innerHTML = ''
        const target = document.createElement('div')
        target.id = containerId
        target.style.width = '100%'
        target.style.height = '100%'
        host.appendChild(target)
        try {
          new window.TradingView.widget({
            container_id: containerId,
            symbol,
            interval: TV_INTERVAL[tf] ?? '60',
            theme: 'dark',
            locale: 'es',
            autosize: true,
            timezone: 'exchange',
            allow_symbol_change: true,
            style: '1',
            enable_publishing: false,
            withdateranges: true,
            hide_side_toolbar: false,
          })
          setState('ready')
        } catch (err) {
          setState('error')
          setError(`No se pudo crear el gráfico de TradingView: ${err instanceof Error ? err.message : String(err)}`)
        }
      })
      .catch((err) => {
        if (!alive) return
        setState('error')
        setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      alive = false
      if (host) host.innerHTML = ''
    }
  }, [symbol, tf, attempt, containerId, simulated])

  if (simulated) {
    return (
      <div className="chart-tv">
        <Empty icon={<Tv size={22} />} title="Símbolo simulado">
          {symbol} es un mercado sintético de NEXUS y no existe en TradingView. Usa la vista NEXUS.
        </Empty>
      </div>
    )
  }

  return (
    <div className="chart-tv">
      <div ref={hostRef} className="chart-tv-host" />
      {state === 'loading' && (
        <div className="chart-overlay">
          <Loading label="Cargando TradingView…" />
        </div>
      )}
      {state === 'error' && (
        <div className="chart-overlay">
          <div style={{ maxWidth: 460 }}>
            <ErrorBox error={error ?? 'Error desconocido'} onRetry={() => setAttempt((n) => n + 1)} />
          </div>
        </div>
      )}
    </div>
  )
}

export default memo(TradingViewWidget)
