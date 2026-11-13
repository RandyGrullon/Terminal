// Global UI state (zustand). Per-viewer conveniences are mirrored to localStorage (wrapped in try/catch).
import { create } from 'zustand'
import type { Page, Timeframe, UiAction } from './api/types'
import { TIMEFRAMES } from './api/types'
import type { WsStatus } from './api/ws'

export type ToastLevel = 'info' | 'success' | 'warn' | 'error' | 'signal' | 'trade'

export interface Toast {
  id: number
  message: string
  level: ToastLevel
  ts: number
}

const PAGES: Page[] = ['chart', 'markets', 'scanner', 'sim', 'paper', 'studio', 'brain', 'copilot', 'settings']
const STORAGE_KEY = 'nexus.ui.v1'

interface Persisted {
  symbol: string
  tf: Timeframe
  terminalOpen: boolean
  terminalHeight: number
}

function loadPersisted(): Partial<Persisted> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as Partial<Persisted>) : {}
  } catch {
    return {}
  }
}

/** Section in the URL hash (#/scanner). An empty hash is the default section; an unknown one is null. */
function pageFromHash(): Page | null {
  const h = location.hash.replace(/^#\/?/, '') as Page
  if (!h) return 'chart'
  return PAGES.includes(h) ? h : null
}

const saved = loadPersisted()
const initialPage: Page = pageFromHash() ?? 'chart'

// Give the first history entry its #/page without adding an entry, so Back from the next section returns here.
try {
  if (location.hash !== `#/${initialPage}`) history.replaceState(history.state, '', `#/${initialPage}`)
} catch {
  /* history unavailable: ignore */
}

export interface AppState {
  page: Page
  setPage: (p: Page) => void
  symbol: string
  tf: Timeframe
  setSymbol: (s: string) => void
  setTf: (tf: Timeframe) => void
  /** Switch to the chart page showing `symbol` (and optionally `tf`). */
  openChart: (symbol: string, tf?: Timeframe) => void
  terminalOpen: boolean
  terminalHeight: number
  toggleTerminal: () => void
  setTerminalOpen: (open: boolean) => void
  setTerminalHeight: (h: number) => void
  wsStatus: WsStatus
  setWsStatus: (s: WsStatus) => void
  toasts: Toast[]
  notify: (message: string, level?: ToastLevel) => void
  dismiss: (id: number) => void
}

let toastSeq = 0

export const useApp = create<AppState>((set, get) => ({
  page: initialPage,
  setPage: (page) => {
    set({ page })
    // Every section change is a history entry: the browser's Back / Forward move between sections.
    if (location.hash !== `#/${page}`) history.pushState(null, '', `#/${page}`)
  },
  symbol: saved.symbol || 'BINANCE:BTCUSDT',
  tf: saved.tf && TIMEFRAMES.includes(saved.tf) ? saved.tf : '1h',
  setSymbol: (symbol) => set({ symbol: symbol.toUpperCase() }),
  setTf: (tf) => set({ tf }),
  openChart: (symbol, tf) => {
    set({ symbol: symbol.toUpperCase(), ...(tf ? { tf } : {}) })
    get().setPage('chart')
  },
  terminalOpen: saved.terminalOpen ?? true,
  terminalHeight: Math.min(Math.max(saved.terminalHeight ?? 240, 120), 800),
  toggleTerminal: () => set((s) => ({ terminalOpen: !s.terminalOpen })),
  setTerminalOpen: (terminalOpen) => set({ terminalOpen }),
  setTerminalHeight: (h) => set({ terminalHeight: Math.min(Math.max(Math.round(h), 120), 800) }),
  wsStatus: 'closed',
  setWsStatus: (wsStatus) => set({ wsStatus }),
  toasts: [],
  notify: (message, level = 'info') => {
    const id = ++toastSeq
    set((s) => ({ toasts: [...s.toasts.slice(-4), { id, message, level, ts: Date.now() }] }))
    window.setTimeout(() => get().dismiss(id), level === 'error' ? 9000 : 6000)
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

useApp.subscribe((s) => {
  try {
    const p: Persisted = { symbol: s.symbol, tf: s.tf, terminalOpen: s.terminalOpen, terminalHeight: s.terminalHeight }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(p))
  } catch {
    /* storage unavailable: ignore */
  }
})

/** Apply a UI action coming from the backend (terminal command results or WS "ui" messages).
 *  'clear' is handled by the Terminal component itself. */
export function runUiAction(a: UiAction): void {
  const s = useApp.getState()
  if (a.action === 'open_chart' && a.symbol) s.openChart(a.symbol, a.tf)
  else if (a.action === 'navigate' && a.page) s.setPage(a.page)
  else if (a.action === 'notify' && a.message) s.notify(a.message)
}

// Back / Forward (popstate) and edits of the #/page hash switch the section without adding history entries.
function syncPageFromHash(): void {
  const p = pageFromHash()
  if (p && p !== useApp.getState().page) useApp.setState({ page: p })
}
window.addEventListener('hashchange', syncPageFromHash)
window.addEventListener('popstate', syncPageFromHash)
