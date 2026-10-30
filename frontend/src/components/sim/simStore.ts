// UI state of the Simulation page (FE-SIM): selected tab, forms and the last backtest / Monte Carlo results.
// Results live in memory for the whole app session (they survive tab switches and page navigation); the forms
// (minus symbols) are mirrored to localStorage as a per-viewer convenience. Every storage access is guarded.
import { create } from 'zustand'
import { api, errorMessage } from '../../api/client'
import type { BacktestResult, MonteCarloResult, SimRegime, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { SIM_REGIME_VALUES } from './labels'

export type SimTab = 'live' | 'backtest' | 'montecarlo'
export type BacktestBody = Parameters<typeof api.backtest>[0]
export type MonteCarloBody = Parameters<typeof api.montecarlo>[0]

export interface LiveForm {
  mode: 'synthetic' | 'replay'
  /** null = use the active symbol of the app. */
  symbol: string | null
  tf: Timeframe
  speed: number
  regime: SimRegime
  seed: number | null
  autoTrade: boolean
  warmup: number | null
  initialBalance: number | null
  startPrice: number | null
}

export interface BacktestForm {
  symbol: string | null
  tf: Timeframe | null
  limit: number | null
  initialCapital: number | null
  riskPct: number | null
  feePct: number | null
  slippagePct: number | null
  useGate: boolean
  entryThreshold: number | null
  slAtr: number | null
  horizon: number | null
}

export interface MonteCarloForm {
  mode: 'bootstrap' | 'synthetic'
  symbol: string | null
  tf: Timeframe | null
  paths: number | null
  bars: number | null
  seed: number | null
  initialCapital: number | null
}

export interface Job<B, R> {
  /** Body of the request that produced `result`. */
  body: B | null
  /** Body of the last attempt (used by "Reintentar"). */
  attempt: B | null
  result: R | null
  loading: boolean
  error: string | null
  startedAt: number | null
  finishedAt: number | null
  seq: number
}

/** Defaults read from the user's settings (GET /api/settings). */
export interface SettingsDefaults {
  accountSize: number | null
  riskPct: number | null
  feePct: number | null
  slippagePct: number | null
  entryThreshold: number | null
  slAtr: number | null
  horizon: number | null
  speed: number | null
}

export const SPEED_MIN = 0.5
export const SPEED_MAX = 20

export const clampSpeed = (v: number): number => Math.min(SPEED_MAX, Math.max(SPEED_MIN, Math.round(v * 2) / 2))

const DEFAULT_LIVE: LiveForm = {
  mode: 'synthetic',
  symbol: null,
  tf: '1h',
  speed: 4,
  regime: 'auto',
  seed: 7,
  autoTrade: true,
  warmup: null,
  initialBalance: null,
  startPrice: null,
}

const DEFAULT_BT: BacktestForm = {
  symbol: null,
  tf: null,
  limit: 2000,
  initialCapital: 10000,
  riskPct: 1,
  feePct: 0.1,
  slippagePct: 0.02,
  useGate: true,
  entryThreshold: null,
  slAtr: null,
  horizon: null,
}

const DEFAULT_MC: MonteCarloForm = {
  mode: 'bootstrap',
  symbol: null,
  tf: null,
  paths: 300,
  bars: 500,
  seed: 1,
  initialCapital: 10000,
}

const emptyJob = <B, R>(): Job<B, R> => ({
  body: null,
  attempt: null,
  result: null,
  loading: false,
  error: null,
  startedAt: null,
  finishedAt: null,
  seq: 0,
})

// ----------------------------------------------------------------------------- persistence

const STORAGE_KEY = 'nexus.sim.ui.v1'
const TABS: SimTab[] = ['live', 'backtest', 'montecarlo']

type Raw = Record<string, unknown>

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const bool = (v: unknown, d: boolean): boolean => (typeof v === 'boolean' ? v : d)
const tfOf = (v: unknown): Timeframe | null => (typeof v === 'string' && (TIMEFRAMES as string[]).includes(v) ? (v as Timeframe) : null)

interface Persisted {
  tab: SimTab
  live: LiveForm | null
  bt: BacktestForm | null
  mc: MonteCarloForm | null
}

function readPersisted(): Persisted {
  const out: Persisted = { tab: 'live', live: null, bt: null, mc: null }
  let raw: Raw | null = null
  try {
    const text = localStorage.getItem(STORAGE_KEY)
    raw = text ? (JSON.parse(text) as Raw) : null
  } catch {
    raw = null
  }
  if (!raw || typeof raw !== 'object') return out
  if (typeof raw.tab === 'string' && (TABS as string[]).includes(raw.tab)) out.tab = raw.tab as SimTab
  const l = raw.live as Raw | undefined
  if (l && typeof l === 'object') {
    out.live = {
      ...DEFAULT_LIVE,
      mode: l.mode === 'replay' ? 'replay' : 'synthetic',
      tf: tfOf(l.tf) ?? DEFAULT_LIVE.tf,
      speed: clampSpeed(num(l.speed) ?? DEFAULT_LIVE.speed),
      regime: typeof l.regime === 'string' && (SIM_REGIME_VALUES as string[]).includes(l.regime) ? (l.regime as SimRegime) : 'auto',
      seed: num(l.seed),
      autoTrade: bool(l.autoTrade, DEFAULT_LIVE.autoTrade),
      warmup: num(l.warmup),
      initialBalance: num(l.initialBalance),
      startPrice: num(l.startPrice),
    }
  }
  const b = raw.bt as Raw | undefined
  if (b && typeof b === 'object') {
    out.bt = {
      ...DEFAULT_BT,
      limit: num(b.limit) ?? DEFAULT_BT.limit,
      initialCapital: num(b.initialCapital) ?? DEFAULT_BT.initialCapital,
      riskPct: num(b.riskPct) ?? DEFAULT_BT.riskPct,
      feePct: num(b.feePct) ?? DEFAULT_BT.feePct,
      slippagePct: num(b.slippagePct) ?? DEFAULT_BT.slippagePct,
      useGate: bool(b.useGate, true),
      entryThreshold: num(b.entryThreshold),
      slAtr: num(b.slAtr),
      horizon: num(b.horizon),
    }
  }
  const m = raw.mc as Raw | undefined
  if (m && typeof m === 'object') {
    out.mc = {
      ...DEFAULT_MC,
      mode: m.mode === 'synthetic' ? 'synthetic' : 'bootstrap',
      paths: num(m.paths) ?? DEFAULT_MC.paths,
      bars: num(m.bars) ?? DEFAULT_MC.bars,
      seed: num(m.seed),
      initialCapital: num(m.initialCapital) ?? DEFAULT_MC.initialCapital,
    }
  }
  return out
}

// ----------------------------------------------------------------------------- result normalization

const arr = <T>(v: T[] | null | undefined): T[] => (Array.isArray(v) ? v : [])

function normalizeBacktest(r: BacktestResult): BacktestResult {
  if (!r || typeof r !== 'object' || !r.metrics || typeof r.metrics !== 'object') {
    throw new Error('El servidor devolvió un resultado de backtest incompleto.')
  }
  return {
    ...r,
    params: r.params && typeof r.params === 'object' ? r.params : {},
    ungated: r.ungated && typeof r.ungated === 'object' ? r.ungated : null,
    equity: arr(r.equity),
    benchmark: arr(r.benchmark),
    trades: arr(r.trades),
    markers: arr(r.markers),
    candles: arr(r.candles),
  }
}

function normalizeMonteCarlo(r: MonteCarloResult): MonteCarloResult {
  if (!r || typeof r !== 'object' || !Array.isArray(r.percentiles)) {
    throw new Error('El servidor devolvió un resultado de Monte Carlo incompleto.')
  }
  const pct = r.percentiles.filter((p) => p && Number.isFinite(p.step)).sort((a, b) => a.step - b.step)
  return {
    ...r,
    percentiles: pct,
    final_returns_pct: arr(r.final_returns_pct),
    max_drawdowns_pct: arr(r.max_drawdowns_pct),
    sample_paths: arr(r.sample_paths).filter((p) => Array.isArray(p)),
    notes: arr(r.notes).filter((n) => typeof n === 'string'),
  }
}

// ----------------------------------------------------------------------------- store

interface SimUiState {
  tab: SimTab
  setTab: (tab: SimTab) => void

  live: LiveForm
  patchLive: (p: Partial<LiveForm>, byUser?: boolean) => void

  btForm: BacktestForm
  patchBt: (p: Partial<BacktestForm>) => void
  bt: Job<BacktestBody, BacktestResult>
  runBacktest: (body: BacktestBody) => Promise<void>

  mcForm: MonteCarloForm
  patchMc: (p: Partial<MonteCarloForm>) => void
  mc: Job<MonteCarloBody, MonteCarloResult>
  runMonteCarlo: (body: MonteCarloBody) => Promise<void>

  defaults: SettingsDefaults | null
  settingsState: 'idle' | 'loading' | 'done' | 'error'
  /** Loads the user's settings once and uses them as defaults for forms the user has not edited yet. */
  loadSettingsDefaults: () => Promise<void>

  touched: { live: boolean; bt: boolean; mc: boolean }
}

const persisted = readPersisted()

function setting(settings: Record<string, unknown>, key: string): number | null {
  const v = settings[key]
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
}

export const useSimStore = create<SimUiState>((set, get) => ({
  tab: persisted.tab,
  setTab: (tab) => set({ tab }),

  live: persisted.live ?? DEFAULT_LIVE,
  patchLive: (p, byUser = true) =>
    set((s) => ({ live: { ...s.live, ...p }, touched: byUser ? { ...s.touched, live: true } : s.touched })),

  btForm: persisted.bt ?? DEFAULT_BT,
  patchBt: (p) => set((s) => ({ btForm: { ...s.btForm, ...p }, touched: { ...s.touched, bt: true } })),
  bt: emptyJob<BacktestBody, BacktestResult>(),
  runBacktest: async (body) => {
    const seq = get().bt.seq + 1
    set((s) => ({ bt: { ...s.bt, seq, attempt: body, loading: true, error: null, startedAt: Date.now() } }))
    try {
      const result = normalizeBacktest(await api.backtest(body))
      if (get().bt.seq !== seq) return
      set((s) => ({ bt: { ...s.bt, loading: false, result, body, finishedAt: Date.now() } }))
    } catch (err) {
      if (get().bt.seq !== seq) return
      set((s) => ({ bt: { ...s.bt, loading: false, error: errorMessage(err), finishedAt: Date.now() } }))
    }
  },

  mcForm: persisted.mc ?? DEFAULT_MC,
  patchMc: (p) => set((s) => ({ mcForm: { ...s.mcForm, ...p }, touched: { ...s.touched, mc: true } })),
  mc: emptyJob<MonteCarloBody, MonteCarloResult>(),
  runMonteCarlo: async (body) => {
    const seq = get().mc.seq + 1
    set((s) => ({ mc: { ...s.mc, seq, attempt: body, loading: true, error: null, startedAt: Date.now() } }))
    try {
      const result = normalizeMonteCarlo(await api.montecarlo(body))
      if (get().mc.seq !== seq) return
      set((s) => ({ mc: { ...s.mc, loading: false, result, body, finishedAt: Date.now() } }))
    } catch (err) {
      if (get().mc.seq !== seq) return
      set((s) => ({ mc: { ...s.mc, loading: false, error: errorMessage(err), finishedAt: Date.now() } }))
    }
  },

  defaults: null,
  settingsState: 'idle',
  loadSettingsDefaults: async () => {
    if (get().settingsState === 'loading' || get().settingsState === 'done') return
    set({ settingsState: 'loading' })
    try {
      const { settings } = await api.settings.get()
      const src = settings && typeof settings === 'object' ? (settings as Record<string, unknown>) : {}
      const d: SettingsDefaults = {
        accountSize: setting(src, 'risk.account_size'),
        riskPct: setting(src, 'risk.per_trade_pct'),
        feePct: setting(src, 'risk.fee_pct'),
        slippagePct: setting(src, 'risk.slippage_pct'),
        entryThreshold: setting(src, 'nexus.entry_threshold'),
        slAtr: setting(src, 'nexus.sl_atr'),
        horizon: setting(src, 'nexus.horizon'),
        speed: setting(src, 'sim.default_speed'),
      }
      set((s) => ({
        settingsState: 'done',
        defaults: d,
        live:
          s.touched.live || d.speed === null ? s.live : { ...s.live, speed: clampSpeed(d.speed) },
        btForm: s.touched.bt
          ? s.btForm
          : {
              ...s.btForm,
              initialCapital: d.accountSize ?? s.btForm.initialCapital,
              riskPct: d.riskPct ?? s.btForm.riskPct,
              feePct: d.feePct ?? s.btForm.feePct,
              slippagePct: d.slippagePct ?? s.btForm.slippagePct,
            },
        mcForm: s.touched.mc ? s.mcForm : { ...s.mcForm, initialCapital: d.accountSize ?? s.mcForm.initialCapital },
      }))
    } catch {
      // Settings are optional here: keep the built-in defaults and allow another attempt later.
      set({ settingsState: 'error' })
    }
  },

  touched: { live: persisted.live !== null, bt: persisted.bt !== null, mc: persisted.mc !== null },
}))

let lastSaved = ''
useSimStore.subscribe((s) => {
  const { symbol: _liveSymbol, ...live } = s.live
  const { symbol: _btSymbol, tf: _btTf, ...bt } = s.btForm
  const { symbol: _mcSymbol, tf: _mcTf, ...mc } = s.mcForm
  const payload = {
    tab: s.tab,
    live: s.touched.live ? live : undefined,
    bt: s.touched.bt ? bt : undefined,
    mc: s.touched.mc ? mc : undefined,
  }
  let text: string
  try {
    text = JSON.stringify(payload)
  } catch {
    return
  }
  if (text === lastSaved) return
  lastSaved = text
  try {
    localStorage.setItem(STORAGE_KEY, text)
  } catch {
    /* storage unavailable: ignore */
  }
})
