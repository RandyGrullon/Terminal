// Typed REST client for the NEXUS backend. All calls are relative (same origin / Vite proxy).
import type {
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
  MarketInfo,
  MonteCarloResult,
  NexusEvent,
  NexusModel,
  NexusResponse,
  PaperSnapshot,
  Position,
  PredictionRow,
  PredictionStats,
  Quote,
  QuickSignal,
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

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  let url = path
  if (opts.query) {
    const params = new URLSearchParams()
    for (const [k, v] of Object.entries(opts.query)) {
      if (v !== undefined && v !== null && v !== '') params.set(k, String(v))
    }
    const qs = params.toString()
    if (qs) url += (url.includes('?') ? '&' : '?') + qs
  }
  let res: Response
  try {
    res = await fetch(url, {
      method,
      headers: opts.body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    })
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err
    throw new ApiError('No hay conexión con el servidor NEXUS', 0, null)
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
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail
    let message = `Error ${res.status}`
    if (typeof detail === 'string') message = detail
    else if (detail && typeof detail === 'object' && 'message' in detail) message = String((detail as { message: unknown }).message)
    else if (Array.isArray(detail) && detail.length) message = 'Parámetros no válidos: ' + detail.map((d) => d?.msg ?? '').join('; ')
    throw new ApiError(message, res.status, data)
  }
  return data as T
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
