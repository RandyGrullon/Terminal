import {
  Component,
  Suspense,
  lazy,
  useEffect,
  useState,
  type ComponentType,
  type ErrorInfo,
  type LazyExoticComponent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from 'react'
import {
  Brain,
  ChartCandlestick,
  Code,
  FlaskConical,
  Globe,
  LogOut,
  Radar,
  Settings as SettingsIcon,
  Sparkles,
  SquareTerminal,
  TriangleAlert,
  Wallet,
  X,
  type LucideIcon,
} from 'lucide-react'
import { api } from './api/client'
import type { Health, Page, Timeframe } from './api/types'
import { socket } from './api/ws'
import { authEnabled, currentSession, signOut } from './auth/session'
import { Badge, Button, IconButton, Loading } from './components/ui'
import MarketPulse from './components/macro/MarketPulse'
import SymbolSearch from './components/SymbolSearch'
import Terminal from './components/terminal/Terminal'
// The sections' styles stay in the first-load stylesheet, in the order the single bundle had them (styles.css, from
// main.tsx, still last): the cascade and every page look exactly as before the code split. Loaded with each section
// they would land after styles.css and rules of equal specificity would change winner (e.g. `.stat .stat-value` vs
// `.scn-acc-stats .stat-value`). All of it is ~16 kB gzip; what loads on demand is the sections' code. A stylesheet
// that only a section imports and that is not listed here (e.g. brain/validation.css) loads with that section, after
// styles.css (Vite's default).
import './components/brain/brain.css'
import './components/nexus/nexus.css'
import './components/chart/chart.css'
import './components/markets/markets.css'
import './components/paper/paper.css'
import './components/scanner/scanner.css'
import './components/settings/settings.css'
import './components/sim/sim.css'
import './components/studio/studio.css'
import { runUiAction, useApp } from './store'

const NAV: { page: Page; label: string; icon: LucideIcon }[] = [
  { page: 'chart', label: 'Gráfico', icon: ChartCandlestick },
  { page: 'markets', label: 'Mercados', icon: Globe },
  { page: 'scanner', label: 'Escáner', icon: Radar },
  { page: 'sim', label: 'Simulación', icon: FlaskConical },
  { page: 'paper', label: 'Cuenta demo', icon: Wallet },
  { page: 'studio', label: 'PyChart', icon: Code },
  { page: 'brain', label: 'Aprendizaje', icon: Brain },
  { page: 'copilot', label: 'Copiloto', icon: Sparkles },
  { page: 'settings', label: 'Ajustes', icon: SettingsIcon },
]

// ----------------------------------------------------------------------------- sections, downloaded on demand
// Each page is its own chunk (React.lazy): the first load brings the shell plus the section in the URL, the rest is
// downloaded when it is opened (or, while the server is reachable, when the pointer / focus reaches its menu item).

type PageModule = { default: ComponentType }

/** A section's code could not be downloaded: no connection with the server, or a new build replaced the files while
 *  this tab was open (the old file names no longer exist). */
class PageLoadError extends Error {
  readonly page: Page
  constructor(page: Page, cause: unknown) {
    super(cause instanceof Error && cause.message ? cause.message : String(cause))
    this.name = 'PageLoadError'
    this.page = page
  }
}

interface LazyPage {
  /** Starts the download once, shared by every caller (prefetch, the visit itself). A failure is not kept here, but the
   *  browser keeps it: Chromium never requests a failed module file again in the same page (see prefetchOnIntent). */
  preload: () => Promise<PageModule>
  Component: LazyExoticComponent<ComponentType>
}

function lazyPage(page: Page, load: () => Promise<PageModule>): LazyPage {
  let pending: Promise<PageModule> | null = null
  const preload = () => {
    if (!pending) {
      pending = load().catch((err: unknown) => {
        pending = null
        throw new PageLoadError(page, err)
      })
    }
    return pending
  }
  return { preload, Component: lazy(preload) }
}

const PAGES: Record<Page, LazyPage> = {
  chart: lazyPage('chart', () => import('./pages/ChartPage')),
  markets: lazyPage('markets', () => import('./pages/MarketsPage')),
  scanner: lazyPage('scanner', () => import('./pages/ScannerPage')),
  sim: lazyPage('sim', () => import('./pages/SimulationPage')),
  paper: lazyPage('paper', () => import('./pages/PaperPage')),
  studio: lazyPage('studio', () => import('./pages/StudioPage')),
  brain: lazyPage('brain', () => import('./pages/BrainPage')),
  copilot: lazyPage('copilot', () => import('./pages/CopilotPage')),
  settings: lazyPage('settings', () => import('./pages/SettingsPage')),
}

const pageFor = (page: Page): LazyPage => PAGES[page] ?? PAGES.chart

/** Downloads a section's code ahead of the visit; a failure is reported by the visit itself (the page boundary). */
function prefetchPage(page: Page): void {
  pageFor(page)
    .preload()
    .catch(() => undefined)
}

/** Menu hover / focus: downloads the section ahead of the click, only while the server is reachable (live connection
 *  open, browser online). The browser keeps a failed module download for the life of the page, so a prefetch during a
 *  disconnection would leave that section unable to open until a reload, even after the server is back. The click
 *  itself always tries. */
function prefetchOnIntent(page: Page): void {
  if (useApp.getState().wsStatus !== 'open') return
  if (typeof navigator !== 'undefined' && navigator.onLine === false) return
  prefetchPage(page)
}

// The section in the URL starts downloading right away, in parallel with AuthGate's first request (App renders after).
prefetchPage(useApp.getState().page)

function PageView({ page }: { page: Page }) {
  const Section = pageFor(page).Component
  return <Section />
}

const LOADING_DELAY_MS = 150

/** Suspense fallback while a section downloads: empty for the first 150 ms (a local server is usually faster, so
 *  nothing flashes), then a light "Cargando sección…". */
function PageLoading() {
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const id = window.setTimeout(() => setVisible(true), LOADING_DELAY_MS)
    return () => window.clearTimeout(id)
  }, [])
  return (
    <div className="page" aria-busy="true">
      {visible && <Loading label="Cargando sección…" />}
    </div>
  )
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
    const { error } = this.state
    if (!error) return this.props.children
    // The browser keeps a failed module download for the life of the page (Chromium never requests that file again)
    // and so does React.lazy: a section that could not be downloaded only comes back with a reload.
    const download = error instanceof PageLoadError
    return (
      <div className="page">
        <div className="errorbox">
          <TriangleAlert size={16} />
          <div className="grow">
            <strong>{download ? 'No se pudo descargar esta sección.' : 'Esta sección tuvo un error y no se pudo mostrar.'}</strong>
            {download && (
              <div style={{ marginTop: 6 }}>
                Puede que se haya perdido la conexión con el servidor o que la aplicación se haya actualizado mientras
                esta pestaña estaba abierta. Recarga la página para intentarlo de nuevo.
              </div>
            )}
            <div className="mono tiny" style={{ marginTop: 6, whiteSpace: 'pre-wrap' }}>
              {error.message}
            </div>
          </div>
          {download ? (
            <Button size="sm" onClick={() => window.location.reload()}>
              Recargar página
            </Button>
          ) : (
            <Button size="sm" onClick={() => this.setState({ error: null })}>
              Reintentar
            </Button>
          )}
        </div>
      </div>
    )
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
    // Messages like "no operes hasta las 9:00" use the user's clock, also when the server runs in the cloud (UTC).
    let tz = ''
    try {
      tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''
    } catch {
      tz = ''
    }
    if (!tz) return
    api.settings
      .get()
      .then(({ settings }) => {
        if (settings['ui.timezone'] !== tz) return api.settings.update({ 'ui.timezone': tz })
      })
      .catch(() => undefined)
  }, [])

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
        <MarketPulse />
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
        {authEnabled() && (
          <>
            <span className="small muted ellipsis" style={{ maxWidth: 180 }} title="Sesión iniciada">
              {currentSession()?.email}
            </span>
            <IconButton icon={<LogOut size={15} />} title="Cerrar sesión" onClick={() => void signOut()} />
          </>
        )}
      </header>

      <nav className="app-nav" aria-label="Secciones">
        {NAV.map(({ page: p, label, icon: Icon }) => (
          <button
            key={p}
            type="button"
            className={`nav-item ${p === page ? 'active' : ''}`}
            onClick={() => setPage(p)}
            onPointerEnter={() => prefetchOnIntent(p)}
            onFocus={() => prefetchOnIntent(p)}
            title={label}
          >
            <Icon size={19} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      <main className="app-main">
        <div className="page-area">
          <PageBoundary key={page} name={page}>
            <Suspense fallback={<PageLoading />}>
              <PageView page={page} />
            </Suspense>
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
