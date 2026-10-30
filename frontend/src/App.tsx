import { Component, useEffect, useState, type ComponentType, type ErrorInfo, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react'
import {
  Brain,
  ChartCandlestick,
  Code,
  FlaskConical,
  Globe,
  Radar,
  Settings as SettingsIcon,
  SquareTerminal,
  TriangleAlert,
  Wallet,
  X,
  type LucideIcon,
} from 'lucide-react'
import { api } from './api/client'
import type { Health, Page, Timeframe } from './api/types'
import { socket } from './api/ws'
import { Badge, Button, IconButton } from './components/ui'
import SymbolSearch from './components/SymbolSearch'
import Terminal from './components/terminal/Terminal'
import BrainPage from './pages/BrainPage'
import ChartPage from './pages/ChartPage'
import MarketsPage from './pages/MarketsPage'
import PaperPage from './pages/PaperPage'
import ScannerPage from './pages/ScannerPage'
import SettingsPage from './pages/SettingsPage'
import SimulationPage from './pages/SimulationPage'
import StudioPage from './pages/StudioPage'
import { runUiAction, useApp } from './store'

const NAV: { page: Page; label: string; icon: LucideIcon }[] = [
  { page: 'chart', label: 'Gráfico', icon: ChartCandlestick },
  { page: 'markets', label: 'Mercados', icon: Globe },
  { page: 'scanner', label: 'Escáner', icon: Radar },
  { page: 'sim', label: 'Simulación', icon: FlaskConical },
  { page: 'paper', label: 'Cuenta demo', icon: Wallet },
  { page: 'studio', label: 'PyChart', icon: Code },
  { page: 'brain', label: 'Aprendizaje', icon: Brain },
  { page: 'settings', label: 'Ajustes', icon: SettingsIcon },
]

const PAGES: Record<Page, ComponentType> = {
  chart: ChartPage,
  markets: MarketsPage,
  scanner: ScannerPage,
  sim: SimulationPage,
  paper: PaperPage,
  studio: StudioPage,
  brain: BrainPage,
  settings: SettingsPage,
}

const HEADER_TFS: Timeframe[] = ['1m', '5m', '15m', '1h', '4h', '1d', '1w']

class PageBoundary extends Component<{ children: ReactNode; name: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`Error en la página ${this.props.name}`, error, info.componentStack)
  }
  render() {
    if (this.state.error) {
      return (
        <div className="page">
          <div className="errorbox">
            <TriangleAlert size={16} />
            <div className="grow">
              <strong>Esta sección tuvo un error y no se pudo mostrar.</strong>
              <div className="mono tiny" style={{ marginTop: 6, whiteSpace: 'pre-wrap' }}>
                {this.state.error.message}
              </div>
            </div>
            <Button size="sm" onClick={() => this.setState({ error: null })}>
              Reintentar
            </Button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

function HeaderClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  return (
    <span className="mono small text-2" title="Hora local">
      {now.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
    </span>
  )
}

export default function App() {
  const page = useApp((s) => s.page)
  const setPage = useApp((s) => s.setPage)
  const tf = useApp((s) => s.tf)
  const setTf = useApp((s) => s.setTf)
  const symbol = useApp((s) => s.symbol)
  const terminalOpen = useApp((s) => s.terminalOpen)
  const terminalHeight = useApp((s) => s.terminalHeight)
  const toggleTerminal = useApp((s) => s.toggleTerminal)
  const setTerminalHeight = useApp((s) => s.setTerminalHeight)
  const wsStatus = useApp((s) => s.wsStatus)
  const setWsStatus = useApp((s) => s.setWsStatus)
  const toasts = useApp((s) => s.toasts)
  const dismiss = useApp((s) => s.dismiss)
  const notify = useApp((s) => s.notify)
  const [health, setHealth] = useState<Health | null>(null)

  useEffect(() => {
    socket.connect()
    return socket.onStatus(setWsStatus)
  }, [setWsStatus])

  useEffect(() => {
    let last = 0
    const offUi = socket.on('ui', (m) => runUiAction(m))
    const offEv = socket.on('event', ({ event }) => {
      if (event.level !== 'signal' && event.level !== 'trade') return
      const now = Date.now()
      if (now - last < 2500) return
      last = now
      notify(event.message, event.level === 'trade' ? 'trade' : 'signal')
    })
    return () => {
      offUi()
      offEv()
    }
  }, [notify])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && (e.key === '`' || e.code === 'Backquote')) {
        e.preventDefault()
        toggleTerminal()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggleTerminal])

  useEffect(() => {
    let alive = true
    const load = () =>
      api
        .health()
        .then((h) => alive && setHealth(h))
        .catch(() => alive && setHealth(null))
    load()
    const id = window.setInterval(load, 30000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [])

  const onResizeStart = (e: ReactMouseEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = terminalHeight
    const onMove = (ev: MouseEvent) => setTerminalHeight(startH + (startY - ev.clientY))
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      window.dispatchEvent(new Event('resize'))
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const PageComponent = PAGES[page] ?? ChartPage
  const loadErrors = health?.load_errors ?? []

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">N</span>
          NEXUS <small>TERMINAL</small>
        </div>
        <div style={{ width: 340, maxWidth: '32vw' }}>
          <SymbolSearch />
        </div>
        <div className="btn-group" role="group" aria-label="Temporalidad">
          {HEADER_TFS.map((t) => (
            <button key={t} type="button" className={`btn btn-sm ${t === tf ? 'active' : ''}`} onClick={() => setTf(t)}>
              {t}
            </button>
          ))}
        </div>
        <span className="mono small text-2 ellipsis" title="Símbolo activo">
          {symbol}
        </span>
        <div className="header-spacer" />
        {loadErrors.length > 0 && (
          <Badge tone="warn" title={loadErrors.join('\n')}>
            <TriangleAlert size={12} /> {loadErrors.length} módulo(s) con error
          </Badge>
        )}
        <span className="row gap-1 small muted nowrap" title="Conexión en tiempo real">
          <span className={`ws-dot ${wsStatus}`} />
          {wsStatus === 'open' ? 'En vivo' : wsStatus === 'connecting' ? 'Conectando…' : 'Sin conexión'}
        </span>
        <HeaderClock />
        <IconButton
          icon={<SquareTerminal size={16} />}
          title="Terminal (Ctrl+`)"
          active={terminalOpen}
          onClick={toggleTerminal}
        />
      </header>

      <nav className="app-nav" aria-label="Secciones">
        {NAV.map(({ page: p, label, icon: Icon }) => (
          <button key={p} type="button" className={`nav-item ${p === page ? 'active' : ''}`} onClick={() => setPage(p)} title={label}>
            <Icon size={19} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      <main className="app-main">
        <div className="page-area">
          <PageBoundary key={page} name={page}>
            <PageComponent />
          </PageBoundary>
        </div>
        {terminalOpen && (
          <div className="terminal-dock" style={{ height: terminalHeight }}>
            <div className="terminal-resizer" onMouseDown={onResizeStart} title="Arrastra para cambiar el tamaño" />
            <PageBoundary name="terminal">
              <Terminal />
            </PageBoundary>
          </div>
        )}
      </main>

      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.level}`}>
            <div className="grow">{t.message}</div>
            <IconButton size="sm" icon={<X size={13} />} title="Cerrar" onClick={() => dismiss(t.id)} />
          </div>
        ))}
      </div>
    </div>
  )
}
