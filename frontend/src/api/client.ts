// Typed REST client for the NEXUS backend. Same origin in local mode; VITE_API_BASE in cloud mode, where every
// call carries the Supabase session token (refreshed and retried once on 401). A 503 because the backend could not
// verify the session (Supabase unreachable) or because this instance is in standby while another one is active
// (deploys) is transient: the session is kept and GET requests are retried.
import { authEnabled, currentSession, ensureFreshSession, getAccessToken, refreshSession, signOut, type Session } from '../auth/session'
import { apiUrl } from '../lib/config'
import type {
  AutopilotComparison,
  AutopilotSnapshot,
  AutopilotUpdate,
  BacktestResult,
  CandlesResponse,
  CommandInfo,
  CommandResult,
  CompileResult,
  DbStats,
  Direction,
  GateResult,
  Health,
  IndicatorComputeResponse,
  IndicatorRequestItem,
  IndicatorSpec,
  LearningSummary,
  MacroCalendarResponse,
  MarketConditions,
  MarketInfo,
  MonteCarloResult,
  NexusEvent,
  NexusModel,
  NexusResponse,
  PaperOrder,
  PaperOrderRequest,
  PaperOrderResponse,
  PaperRisk,
  PaperSnapshot,
  Position,
  PredictionRow,
  PredictionStats,
  Quote,
  QuickSignal,
  RealtimeStatus,
  ScanRow,
  ScanRun,
  Script,
  ScriptMeta,
  ScriptReference,
  ScriptRunResult,
  SearchResult,
  Settings,
  SimSnapshot,
  SimStartRequest,
  SimState,
  SkillReport,
  SymbolDetail,
  Timeframe,
  UniverseResponse,
  WatchItem,
} from './types'

export class ApiError extends Error {
  status: number
  data: unknown
  constructor(message: string, status: number, data: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.data = data
  }
}

type Query = Record<string, string | number | boolean | null | undefined>

interface RequestOptions {
  query?: Query
  body?: unknown
  signal?: AbortSignal
}

/** Waits before retrying a GET that got a transient 503 (see isTransient503). */
const TRANSIENT_RETRY_DELAYS_MS = [1000, 3000]

/** Transient 503s, never a reason to sign out: "no se pudo verificar tu sesión…" (app.auth: Supabase unreachable) and
 *  "Instancia en espera…" (app.main StandbyGate: another instance holds the database, e.g. during a deploy). Both come
 *  with Retry-After; they are recognised by their detail and retried after the fixed delays below. Other 503s mean a
 *  module is not available and are not retried. */
function isTransient503(status: number, data: unknown): boolean {
  const detail = (data as { detail?: unknown } | null)?.detail
  return status === 503 && typeof detail === 'string' && /verificar (tu|la) sesi[oó]n|^instancia en espera/i.test(detail.trim())
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'))
    const onAbort = () => {
      window.clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function errorText(status: number, data: unknown): string {
  const detail = (data as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail) return String((detail as { message: unknown }).message)
  if (Array.isArray(detail) && detail.length) return 'Parámetros no válidos: ' + detail.map((d) => d?.msg ?? '').join('; ')
  return `Error ${status}`
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}, retried = false): Promise<T> {
  let url = apiUrl(path)
  if (opts.query) {
    const params = new URLSearchParams()
    for (const [k, v] of Object.entries(opts.query)) {
      if (v !== undefined && v !== null && v !== '') params.set(k, String(v))
    }
    const qs = params.toString()
    if (qs) url += (url.includes('?') ? '&' : '?') + qs
  }
  for (let attempt = 0; ; attempt++) {
    await ensureFreshSession()
    const headers: Record<string, string> = {}
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json'
    const token = getAccessToken()
    if (token) headers.Authorization = `Bearer ${token}`
    let res: Response
    try {
      res = await fetch(url, {
        method,
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: opts.signal,
      })
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') throw err
      throw new ApiError('No hay conexión con el servidor NEXUS', 0, null)
    }
    if (res.status === 401 && authEnabled() && currentSession()) {
      if (retried) {
        // Even a freshly refreshed token is refused: this account may no longer use this NEXUS instance.
        void signOut({ scope: 'local' })
      } else {
        let fresh: Session | null
        try {
          fresh = await refreshSession()
        } catch (err) {
          // Supabase could not refresh the token right now: keep the session (a retry is scheduled).
          throw new ApiError(errorMessage(err), 401, null)
        }
        if (fresh) return request<T>(method, path, opts, true)
        // null: the refresh token was rejected for good and the session is closed (login screen).
      }
    }
    const text = await res.text()
    let data: unknown = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        data = text
      }
    }
    if (method.toUpperCase() === 'GET' && attempt < TRANSIENT_RETRY_DELAYS_MS.length && isTransient503(res.status, data)) {
      await wait(TRANSIENT_RETRY_DELAYS_MS[attempt], opts.signal)
      continue
    }
    if (!res.ok) throw new ApiError(errorText(res.status, data), res.status, data)
    return data as T
  }
}

const get = <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>('GET', path, { query, signal })
const post = <T>(path: string, body?: unknown, query?: Query) => request<T>('POST', path, { body: body ?? {}, query })
const put = <T>(path: string, body?: unknown) => request<T>('PUT', path, { body: body ?? {} })
const del = <T>(path: string, query?: Query) => request<T>('DELETE', path, { query })

export const api = {
  // ------------------------------------------------------------------ system
  health: () => get<Health>('/api/health'),
  settings: {
    get: () => get<{ settings: Settings }>('/api/settings'),
    update: (values: Settings) => put<{ settings: Settings }>('/api/settings', values),
  },
  events: (q: { limit?: number; level?: string; source?: string } = {}) => get<{ events: NexusEvent[] }>('/api/events', q),
  terminal: {
    commands: () => get<{ commands: CommandInfo[] }>('/api/terminal/commands'),
    exec: (text: string) => post<CommandResult>('/api/terminal', { text }),
  },
  db: {
    stats: () => get<DbStats>('/api/db/stats'),
    vacuum: () => post<{ ok: boolean; size_bytes: number }>('/api/db/vacuum'),
  },

  // ------------------------------------------------------------------ market data
  markets: () => get<{ markets: MarketInfo[] }>('/api/markets'),
  search: (q: string, market?: string, limit = 30, signal?: AbortSignal) =>
    get<{ results: SearchResult[] }>('/api/symbols/search', { q, market, limit }, signal),
  universe: (q: { market: string; offset?: number; limit?: number; sort?: string; order?: 'asc' | 'desc'; q?: string; type?: string }) =>
    get<UniverseResponse>('/api/universe', q),
  candles: (symbol: string, tf: Timeframe, limit = 1000, signal?: AbortSignal) =>
    get<CandlesResponse>('/api/candles', { symbol, tf, limit }, signal),
  quotes: (symbols: string[]) => get<{ quotes: Record<string, Quote> }>('/api/quotes', { symbols: symbols.join(',') }),
  /** API-key real-time quotes: what each plan serves live, credits used, symbols polled (503 without the service). */
  realtime: () => get<RealtimeStatus>('/api/realtime'),
  symbol: (symbol: string) => get<SymbolDetail>('/api/symbol', { symbol }),
  watchlist: {
    list: () => get<{ items: WatchItem[] }>('/api/watchlist'),
    add: (symbol: string) => post<{ items: WatchItem[] }>('/api/watchlist', { symbol }),
    remove: (symbol: string) => del<{ items: WatchItem[] }>('/api/watchlist', { symbol }),
  },

  // ------------------------------------------------------------------ NEXUS / gate / indicators
  nexus: (symbol: string, tf: Timeframe, limit?: number, signal?: AbortSignal) =>
    get<NexusResponse>('/api/nexus', { symbol, tf, limit }, signal),
  nexusModel: (symbol: string, tf: Timeframe) => get<NexusModel>('/api/nexus/model', { symbol, tf }),
  nexusReset: (symbol: string, tf: Timeframe) => post<{ ok: boolean }>('/api/nexus/reset', { symbol, tf }),
  gateCheck: (q: { symbol: string; tf: Timeframe; direction: Direction; entry?: number; sl?: number; tp?: number }) =>
    get<GateResult>('/api/gate/check', q),
  indicators: {
    catalog: () => get<{ indicators: IndicatorSpec[] }>('/api/indicators/catalog'),
    compute: (body: { symbol: string; tf: Timeframe; limit?: number; items: IndicatorRequestItem[] }, signal?: AbortSignal) =>
      request<IndicatorComputeResponse>('POST', '/api/indicators/compute', { body, signal }),
  },

  // ------------------------------------------------------------------ simulation
  backtest: (body: {
    symbol: string
    tf: Timeframe
    limit?: number
    params?: Record<string, unknown>
    initial_capital?: number
    risk_pct?: number
    fee_pct?: number
    slippage_pct?: number
    use_gate?: boolean
  }) => post<BacktestResult>('/api/backtest', body),
  montecarlo: (body: {
    mode: 'bootstrap' | 'synthetic'
    symbol?: string
    tf?: Timeframe
    paths?: number
    bars?: number
    seed?: number
    initial_capital?: number
  }) => post<MonteCarloResult>('/api/montecarlo', body),
  sim: {
    start: (body: SimStartRequest) => post<{ state: SimState }>('/api/sim/start', body),
    stop: () => post<{ state: SimState }>('/api/sim/stop'),
    config: (body: { speed?: number; regime?: string; paused?: boolean; auto_trade?: boolean }) =>
      post<{ state: SimState }>('/api/sim/config', body),
    state: () => get<SimSnapshot>('/api/sim/state'),
  },

  // ------------------------------------------------------------------ paper trading
  paper: {
    account: () => get<PaperSnapshot>('/api/paper/account'),
    order: (body: {
      symbol: string
      side: Direction
      tf?: Timeframe
      qty?: number
      risk_pct?: number
      sl?: number
      tp?: number
      force?: boolean
    }) => post<{ position: Position; gate: GateResult | null }>('/api/paper/order', body),
    close: (body: { position_id?: number; symbol?: string; all?: boolean }) => post<{ closed: Position[] }>('/api/paper/close', body),
    reset: (initial_balance?: number) => post<PaperSnapshot>('/api/paper/reset', { initial_balance }),
    auto: (body: { enabled: boolean; symbols?: string[]; tf?: Timeframe }) =>
      post<{ auto: { enabled: boolean; symbols: string[]; tf: string } }>('/api/paper/auto', body),
    // Pending limit / stop orders (simulated) and the portfolio risk of the demo account. A portfolio-risk block answers
    // 409 {message, code: 'risk_limit', rule, risk} and can never be forced; a NO TOMAR of the gate answers 409 too.
    orders: (status: 'pending' | 'all' = 'pending', limit = 50) => get<{ orders: PaperOrder[] }>('/api/paper/orders', { status, limit }),
    placeOrder: (body: PaperOrderRequest) => post<PaperOrderResponse>('/api/paper/orders', body),
    cancelOrders: (body: { order_id?: number; symbol?: string; all?: boolean }) =>
      post<{ cancelled: PaperOrder[] }>('/api/paper/orders/cancel', body),
    risk: () => get<PaperRisk>('/api/paper/risk'),
    // NEXUS's own demo account (account 3, "NEXUS automática"): it trades on its own with NEXUS's closed-bar signals
    // and the gate. The user changes how it trades (only the fields sent), resets it or closes its positions by hand.
    // Every call answers the whole AutopilotSnapshot; a bad value answers 400 {detail: '<Spanish>'}.
    autopilot: {
      get: () => get<AutopilotSnapshot>('/api/paper/autopilot'),
      update: (body: AutopilotUpdate) => post<AutopilotSnapshot>('/api/paper/autopilot', body),
      reset: (initial_balance?: number) => post<AutopilotSnapshot>('/api/paper/autopilot/reset', { initial_balance }),
      close: (body: { position_id?: number; all?: boolean }) => post<AutopilotSnapshot>('/api/paper/autopilot/close', body),
      // Against random entries in the same markets (computed on demand, cached by the server; fresh = recompute)
      compare: (fresh = false) => get<AutopilotComparison>('/api/paper/autopilot/compare', fresh ? { fresh: true } : undefined),
    },
  },

  // ------------------------------------------------------------------ scanner / learning
  scanner: {
    run: (body: { market: string; tf: Timeframe; limit?: number; deep?: boolean; deep_top?: number }) =>
      post<{ run_id: number }>('/api/scanner/run', body),
    results: (runId?: number) => get<{ run: ScanRun | null; results: ScanRow[] }>('/api/scanner/results', { run_id: runId }),
    runs: (limit = 20) => get<{ runs: ScanRun[] }>('/api/scanner/runs', { limit }),
    quick: (symbol: string, tf: Timeframe) => get<QuickSignal>('/api/scanner/quick', { symbol, tf }),
  },
  predictions: (q: { source?: string; symbol?: string; resolved?: boolean; limit?: number } = {}) =>
    get<{ predictions: PredictionRow[]; stats: PredictionStats }>('/api/predictions', q),
  learning: () => get<LearningSummary>('/api/learning/summary'),
  /** Skill against the reference (always the most frequent outcome) and climatology. days <= 0 = every stored row,
   *  omitted = 7. An older server without the route answers 404 (components/brain/skill.ts turns it into null). */
  learningSkill: (q: { source?: 'scanner' | 'nexus'; days?: number; model_key?: string } = {}) =>
    get<SkillReport>('/api/learning/skill', q),

  // ------------------------------------------------------------------ market conditions (macro semáforo)
  macro: (symbol?: string) => get<MarketConditions>('/api/macro', { symbol }),
  /** days 1..60, impact 'high,medium' | 'all' | …, currency 'USD,EUR'. `coverage` is absent on older servers. */
  macroCalendar: (q: { days?: number; impact?: string; currency?: string } = {}) =>
    get<MacroCalendarResponse>('/api/macro/calendar', q),

  // ------------------------------------------------------------------ PyChart scripts
  scripts: {
    list: () => get<{ scripts: ScriptMeta[] }>('/api/scripts'),
    get: (id: number) => get<Script>(`/api/scripts/${id}`),
    create: (body: { name: string; code: string; description?: string }) => post<Script>('/api/scripts', body),
    update: (id: number, body: { name?: string; code?: string; description?: string }) => put<Script>(`/api/scripts/${id}`, body),
    remove: (id: number) => del<{ ok: boolean }>(`/api/scripts/${id}`),
    compile: (code: string) => post<CompileResult>('/api/scripts/compile', { code }),
    run: (body: { code: string; symbol: string; tf: Timeframe; limit?: number; inputs?: Record<string, unknown> }) =>
      post<ScriptRunResult>('/api/scripts/run', body),
    reference: () => get<ScriptReference>('/api/scripts/reference'),
  },
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}
