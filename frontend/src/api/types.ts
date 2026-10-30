// =============================================================================
// CANONICAL JSON CONTRACT between backend (FastAPI) and frontend.
// The backend MUST produce exactly these shapes (snake_case keys).
// Time rules:  `time` / `*_time`  -> unix SECONDS (UTC)   (bars, trades)
//              `ts` / `*_at`      -> unix MILLISECONDS    (events, records)
// Numbers are never NaN/Infinity on the wire: missing values are null.
// All user-facing strings (labels, messages, reasons) are in Spanish.
// =============================================================================

export type Timeframe = '1m' | '3m' | '5m' | '15m' | '30m' | '1h' | '2h' | '4h' | '6h' | '12h' | '1d' | '1w'
export const TIMEFRAMES: Timeframe[] = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '1w']

export type Page = 'chart' | 'markets' | 'scanner' | 'sim' | 'paper' | 'studio' | 'brain' | 'settings'

export type Level =
  | 'debug' | 'info' | 'success' | 'warn' | 'error'
  | 'signal' | 'trade' | 'learn' | 'scan' | 'system'

export interface NexusEvent {
  id: number
  ts: number
  level: Level
  source: string
  message: string
  data?: unknown
}

// ----------------------------------------------------------------------------- market data

export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

/** full = exchange history, keyed = API-key provider, recorder = built locally from live snapshots */
export type HistoryKind = 'full' | 'keyed' | 'recorder' | 'synthetic' | 'none'

export interface SymbolInfo {
  symbol: string // "BINANCE:BTCUSDT" (TradingView style EXCHANGE:TICKER)
  ticker: string // "BTCUSDT"
  exchange: string // "BINANCE"
  description: string
  type: string // spot | stock | forex | futures | index | fund | bond | cfd | crypto | dr | ...
  market: string // crypto | america | forex | futures | cfd | bond | coin | <country>
  currency: string
  pricescale: number | null
  logoid: string | null
  provider: string // binance | okx | coinbase | kraken | kucoin | twelvedata | alphavantage | polygon | recorder | synthetic
  history: HistoryKind
}

export interface Quote {
  symbol: string
  price: number | null
  change_pct: number | null
  change: number | null
  volume: number | null
  high: number | null
  low: number | null
  open: number | null
  ts: number
}

export interface MarketInfo {
  id: string
  name: string
  count: number | null
}

export interface SearchResult extends SymbolInfo {
  price?: number | null
  change_pct?: number | null
}

export interface UniverseRow extends SymbolInfo {
  close: number | null
  change_pct: number | null
  volume: number | null
  rating: number | null // TradingView Recommend.All  (-1..1)
  rsi: number | null
  adx: number | null
  atr: number | null
  perf_w: number | null
  perf_1m: number | null
  volatility_d: number | null
  market_cap: number | null
  quick_score: number | null // NEXUS quick model (-1..1)
  quick_prob: number | null // P(up) (0..1)
}

export interface UniverseResponse {
  total: number
  rows: UniverseRow[]
}

export interface CandlesResponse {
  symbol: string
  tf: Timeframe
  provider: string
  history: HistoryKind
  note: string | null
  candles: Candle[]
}

export interface WatchItem extends SymbolInfo {
  quote: Quote | null
}

export interface SymbolDetail extends SymbolInfo {
  quote: Quote | null
  technicals: Record<string, number | null> | null
}

// ----------------------------------------------------------------------------- NEXUS engine

/** Action at the current bar. */
export type NexusAction = 'BUY' | 'SELL' | 'HOLD_LONG' | 'HOLD_SHORT' | 'WAIT' | 'EXIT_LONG' | 'EXIT_SHORT'
export type Regime = 'trend_up' | 'trend_down' | 'range' | 'volatile' | 'unknown'
/** TAKE = tomar la operación, WAIT = esperar confirmación, AVOID = NO tomar */
export type Verdict = 'TAKE' | 'WAIT' | 'AVOID'
export type Direction = 'long' | 'short'

export type MarkerKind =
  | 'BUY' | 'SELL'
  | 'TP1' | 'TP2' | 'TP3'
  | 'EXIT_SL' | 'EXIT_TRAIL' | 'EXIT_EARLY' | 'EXIT_REVERSE' | 'EXIT_TIME' | 'EXIT_SIGNAL'
  | 'REJECTED_LONG' | 'REJECTED_SHORT'
  | 'WARN'

export interface ChartMarker {
  time: number
  kind: MarkerKind | string
  position: 'aboveBar' | 'belowBar' | 'inBar'
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square'
  color: string
  text: string
  tooltip?: string
  price?: number
}

export interface GateCheck {
  id: string // trend_alignment | regime | momentum | room_to_target | risk_reward | overextension | volatility | volume | probability | edge_history | expected_value | recent_performance | confluence | ...
  label: string // "Tendencia mayor a favor"
  passed: boolean | null // null = neutral / not applicable
  critical: boolean // a failed critical check forces AVOID
  weight: number // learned importance (0..1)
  value?: number | string | null
  detail: string // "EMA 4h ascendente (+0.8 ATR)"
}

export interface GateResult {
  direction: Direction
  verdict: Verdict
  verdict_label: string // "TOMAR" | "ESPERAR" | "NO TOMAR"
  score: number // 0..100
  win_prob: number // 0..1 learned probability that the trade reaches TP1 before SL
  rr: number // reward/risk to TP2
  ev_r: number // expected value in R after fees
  entry: number
  sl: number
  tp1: number
  tp2: number
  tp3: number
  checks: GateCheck[]
  reasons_for: string[]
  reasons_against: string[]
  summary: string // one line, Spanish: "NO TOMAR: mercado lateral y resistencia a 0.4 ATR"
  time: number
}

export interface PositionState {
  side: Direction
  entry_price: number
  entry_time: number
  sl: number // current stop ("HASTA AQUÍ")
  tp1: number
  tp2: number
  tp3: number
  trail: number | null
  tp_hits: number // 0..3
  size_left: number // fraction of the position still open (1, 0.66, 0.33)
  unrealized_pct: number
  r_multiple: number
  bars_in_trade: number
  ev_hold_r: number // expected value of holding, in R (negative => better to close)
}

export interface NexusWarning {
  code: string // EARLY_EXIT | NEAR_SL | DIVERGENCE | VOL_SPIKE | TIME_STOP | COUNTER_TREND | REGIME_CHANGE | LOW_DATA | ...
  severity: 'info' | 'warn' | 'critical'
  message: string
}

export interface Contribution {
  expert: string
  label: string
  signal: number // -1..1
  weight: number // 0..1 (current regime)
  contribution: number // signal * weight
}

export interface NexusCurrent {
  time: number
  price: number
  action: NexusAction
  action_label: string // COMPRAR | VENDER | MANTENER LARGO | MANTENER CORTO | ESPERAR | CERRAR LARGO | CERRAR CORTO
  score: number // -1..1
  prob_up: number // 0..1
  confidence: number // 0..1
  regime: Regime
  regime_label: string
  position: PositionState | null
  gate: { long: GateResult; short: GateResult }
  advice: string // one line, Spanish, actionable
  warnings: NexusWarning[]
  contributions: Contribution[]
  levels: { support: number[]; resistance: number[] }
  reasons: string[]
}

/** Per-bar arrays aligned with `time`. pos_* are null while flat (draws the SL/TP "hasta aquí" lines). */
export interface NexusBars {
  time: number[]
  score: (number | null)[]
  prob_up: (number | null)[]
  action: string[]
  regime: string[]
  ema_fast: (number | null)[]
  ema_slow: (number | null)[]
  ema_trend: (number | null)[]
  pos_entry: (number | null)[]
  pos_sl: (number | null)[]
  pos_tp1: (number | null)[]
  pos_tp2: (number | null)[]
  pos_tp3: (number | null)[]
}

export interface Trade {
  id: number
  side: Direction
  entry_time: number
  entry_price: number
  exit_time: number | null
  exit_price: number | null
  sl_initial: number
  tp1: number
  tp2: number
  tp3: number
  exit_reason: string | null // SL | TRAIL | TP3 | EARLY | REVERSE | TIME | SIGNAL | OPEN
  exit_label: string | null // Spanish
  pnl_pct: number | null
  r_multiple: number | null
  bars: number
  mfe_pct: number
  mae_pct: number
  gate_score: number | null
}

export interface ConePoint {
  time: number
  p10: number
  p25: number
  p50: number
  p75: number
  p90: number
}

export interface Prediction {
  horizon_bars: number
  prob_up: number
  expected_return_pct: number
  cone: ConePoint[]
  method: string
}

export interface NexusStats {
  trades: number
  win_rate: number | null
  profit_factor: number | null
  total_return_pct: number
  max_dd_pct: number
  sharpe: number | null
  avg_r: number | null
  expectancy_pct: number | null
  rejected_signals: number
  rejected_would_win_rate: number | null // of the signals the gate rejected, % that would have won
}

export interface NexusLearning {
  updates: number
  hit_rate: number | null
  brier: number | null
  weights: Record<string, Record<string, number>> // regime -> expert -> weight
  gate_weights: Record<string, number> // check id -> learned weight
  trained_until: number | null
  memory_bars: number
}

export interface QuickSignal {
  symbol: string
  tf: string
  score: number
  prob_up: number
  action: 'BUY' | 'SELL' | 'WAIT'
  confidence: number
  rating: number | null
  price: number | null
  reasons: string[]
  model_updates: number
  time: number
}

export interface NexusResponse {
  symbol: string
  tf: Timeframe
  provider: string
  history: HistoryKind
  note: string | null
  ok: boolean // false when there is not enough history (see message + quick)
  message: string | null
  params: Record<string, unknown>
  bars: NexusBars
  markers: ChartMarker[]
  trades: Trade[]
  current: NexusCurrent | null
  prediction: Prediction | null
  learning: NexusLearning
  stats: NexusStats
  quick: QuickSignal | null
}

export interface CalibrationBin {
  bin: number
  predicted: number
  observed: number
  count: number
}

export interface NexusModel {
  symbol: string
  tf: string
  learning: NexusLearning
  calibration: CalibrationBin[]
  rolling: { time: number; hit_rate: number; brier: number }[]
  experts: { id: string; label: string; description: string }[]
}

// ----------------------------------------------------------------------------- indicator library

export interface IndicatorParam {
  name: string
  label: string
  type: 'int' | 'float' | 'bool' | 'select' | 'source'
  default: number | boolean | string
  min?: number
  max?: number
  step?: number
  options?: string[]
}

export interface IndicatorOutput {
  key: string
  title: string
  type: 'line' | 'histogram' | 'area' | 'dots' | 'step'
  color: string
}

export interface IndicatorSpec {
  id: string
  name: string
  category: string // Tendencia | Momento | Volatilidad | Volumen | Soportes/Resistencias | Estadística | NEXUS
  overlay: boolean // true = drawn over price, false = own pane
  description: string
  params: IndicatorParam[]
  outputs: IndicatorOutput[]
  levels?: number[] // horizontal guide levels (e.g. 70/30)
  range?: [number, number]
}

export interface IndicatorRequestItem {
  uid: string
  id: string
  params: Record<string, number | boolean | string>
}

export interface IndicatorComputeResponse {
  time: number[]
  results: {
    uid: string
    id: string
    outputs: Record<string, (number | null)[]>
    error: string | null
  }[]
}

// ----------------------------------------------------------------------------- backtest / monte carlo

export interface EquityPoint {
  time: number
  equity: number
  drawdown_pct: number
}

export interface BacktestMetrics {
  initial_capital: number
  final_equity: number
  total_return_pct: number
  cagr_pct: number | null
  buy_hold_return_pct: number
  trades: number
  win_rate: number | null
  profit_factor: number | null
  avg_win_pct: number | null
  avg_loss_pct: number | null
  expectancy_pct: number | null
  avg_r: number | null
  max_dd_pct: number
  sharpe: number | null
  sortino: number | null
  calmar: number | null
  exposure_pct: number
  avg_bars_in_trade: number | null
  best_trade_pct: number | null
  worst_trade_pct: number | null
  max_consecutive_losses: number
  fees_paid: number
  rejected_signals: number
  rejected_would_win_rate: number | null
  gate_lift_pct: number | null // return improvement of gated vs ungated run
}

export interface BacktestResult {
  symbol: string
  tf: string
  bars: number
  start_time: number
  end_time: number
  params: Record<string, unknown>
  metrics: BacktestMetrics
  ungated: BacktestMetrics | null // same run without the gate, for comparison
  equity: EquityPoint[]
  benchmark: { time: number; equity: number }[]
  trades: Trade[]
  markers: ChartMarker[]
  candles: Candle[]
}

export interface MonteCarloResult {
  mode: 'bootstrap' | 'synthetic'
  paths: number
  steps: number
  percentiles: { step: number; p5: number; p25: number; p50: number; p75: number; p95: number }[] // equity as % of initial (100 = flat)
  final_returns_pct: number[]
  max_drawdowns_pct: number[]
  prob_profit: number
  prob_ruin: number // P(drawdown >= 50%)
  median_return_pct: number
  expected_return_pct: number
  var95_pct: number
  cvar95_pct: number
  sample_paths: number[][]
  notes: string[]
}

// ----------------------------------------------------------------------------- simulator / paper trading

export type SimRegime = 'auto' | 'bull' | 'bear' | 'range' | 'volatile' | 'crash'

export interface SimStartRequest {
  mode: 'synthetic' | 'replay'
  symbol?: string // replay: real symbol; synthetic: ignored (SIM:NEXUS)
  tf?: Timeframe
  speed?: number // bars per second (0.5..20)
  seed?: number
  regime?: SimRegime
  start_price?: number
  warmup?: number // bars revealed before playback starts
  auto_trade?: boolean
  initial_balance?: number
}

export interface PaperAccount {
  id: number
  name: string
  balance: number
  equity: number
  initial_balance: number
  realized_pnl: number
  unrealized_pnl: number
  return_pct: number
  trades: number
  wins: number
  losses: number
  win_rate: number | null
  open_positions: number
  auto_trade: boolean
  auto_symbols: string[]
  auto_tf: string
}

export interface Position {
  id: number
  account_id: number
  symbol: string
  tf: string
  side: Direction
  qty: number
  qty_open: number
  entry_price: number
  entry_time: number
  sl: number
  tp1: number | null
  tp2: number | null
  tp3: number | null
  trail: number | null
  tp_hits?: number // 0..3 objectives already reached
  status: 'open' | 'closed'
  price: number | null // last known price
  exit_price: number | null
  exit_time: number | null
  pnl: number
  pnl_pct: number
  r_multiple: number | null
  exit_reason: string | null
  exit_label: string | null
  origin: 'manual' | 'auto' | 'sim' | 'script'
  gate_verdict: Verdict | null
  gate_score: number | null
  advice: string | null // live NEXUS advice for this position ("Cerrar ya: EV negativo")
  warnings: NexusWarning[]
}

export interface PaperSnapshot {
  account: PaperAccount
  positions: Position[]
  history: Position[]
}

export interface SimState {
  running: boolean
  paused: boolean
  mode: 'synthetic' | 'replay' | null
  symbol: string | null
  tf: string | null
  speed: number
  regime: string | null
  bars_done: number
  bars_total: number | null
  price: number | null
  started_at: number | null
  auto_trade: boolean
  account: PaperAccount | null
  last_action: string | null
  stats: { trades: number; win_rate: number | null; pnl_pct: number } | null
  market_regime?: string | null // synthetic market's current regime
  seed?: number | null
}

export interface SimSnapshot {
  state: SimState
  candles: Candle[]
  nexus: NexusResponse | null
  positions: Position[]
}

// ----------------------------------------------------------------------------- scanner / learning

export interface ScanRun {
  id: number
  market: string
  tf: string
  started_at: number
  finished_at: number | null
  n_symbols: number
  status: 'running' | 'done' | 'error'
  deep: boolean
  message: string | null
}

export interface ScanRow {
  rank: number
  symbol: string
  description: string
  type: string
  price: number | null
  change_pct: number | null
  volume: number | null
  score: number // -1..1
  prob_up: number
  action: 'BUY' | 'SELL' | 'WAIT'
  confidence: number
  rating: number | null
  deep: boolean // full NEXUS + gate was run on this symbol
  verdict: Verdict | null
  gate_score: number | null
  direction: Direction | null
  regime: string | null
  sl: number | null
  tp1: number | null
  tp2: number | null
  expected_move_pct: number | null
  reasons: string[]
}

export interface PredictionRow {
  id: number
  source: 'nexus' | 'scanner'
  symbol: string
  tf: string
  created_at: number
  target_at: number
  price: number
  direction: 1 | -1 | 0
  prob_up: number
  score: number
  resolved: boolean
  outcome_price: number | null
  outcome_return_pct: number | null
  correct: boolean | null
}

export interface PredictionStats {
  total: number
  resolved: number
  pending: number
  hit_rate: number | null
  brier: number | null
  by_source: Record<string, { resolved: number; hit_rate: number | null; brier: number | null }>
  rolling: { time: number; hit_rate: number; n: number }[]
}

export interface LearningSummary {
  scanner_model: {
    key: string
    updates: number
    hit_rate: number | null
    brier: number | null
    features: { name: string; label: string; weight: number }[]
    calibration: CalibrationBin[]
  } | null
  nexus_models: {
    symbol: string
    tf: string
    updates: number
    hit_rate: number | null
    brier: number | null
    trained_until: number | null
  }[]
  gate: {
    decisions: number
    taken: number
    avoided: number
    avoided_would_lose_rate: number | null // % of AVOID verdicts that would indeed have lost
    taken_win_rate: number | null
  }
  predictions: PredictionStats
  timeline: { time: number; hit_rate: number; brier: number; n: number }[]
}

// ----------------------------------------------------------------------------- PyChart scripting

export interface ScriptMeta {
  id: number
  name: string
  description: string
  is_example: boolean
  updated_at: number
}

export interface Script extends ScriptMeta {
  code: string
  created_at: number
}

export interface ScriptError {
  line: number | null
  col: number | null
  message: string
  kind: 'syntax' | 'security' | 'runtime' | 'timeout'
}

export interface ScriptInput {
  name: string
  title: string
  type: 'int' | 'float' | 'bool' | 'string' | 'select' | 'source'
  default: unknown
  min?: number
  max?: number
  step?: number
  options?: string[]
}

export interface ScriptMetaInfo {
  title: string
  overlay: boolean
  kind: 'indicator' | 'strategy'
}

export interface CompileResult {
  ok: boolean
  errors: ScriptError[]
  warnings: string[]
  inputs: ScriptInput[]
  meta: ScriptMetaInfo
}

export interface ScriptPlot {
  id: string
  title: string
  color: string
  linewidth: number
  style: 'line' | 'histogram' | 'area' | 'step' | 'circles' | 'columns'
  overlay: boolean
  pane: number // 0 = price pane; 1.. = separate panes
  data: (number | null)[] // aligned with ScriptRunResult.time
}

export interface ScriptRunResult {
  ok: boolean
  errors: ScriptError[]
  stdout: string
  duration_ms: number
  meta: ScriptMetaInfo
  time: number[]
  plots: ScriptPlot[]
  markers: ChartMarker[]
  hlines: { price: number; title: string; color: string; style: 'solid' | 'dashed' | 'dotted' }[]
  bgcolors: { time: number; color: string }[]
  alerts: { time: number; title: string; message: string }[]
  strategy: BacktestResult | null
  candles: Candle[]
}

export interface ScriptReference {
  sections: { title: string; items: { name: string; signature: string; doc: string }[] }[]
}

// ----------------------------------------------------------------------------- terminal / system

export interface TermLine {
  text: string
  level: Level | 'muted'
}

export interface UiAction {
  action: 'open_chart' | 'navigate' | 'clear' | 'notify'
  symbol?: string
  tf?: Timeframe
  page?: Page
  message?: string
}

export interface CommandResult {
  ok: boolean
  command: string
  lines: TermLine[]
  actions: UiAction[]
  data?: unknown
}

export interface CommandInfo {
  name: string
  help: string
  usage: string
  group: string
  aliases: string[]
}

export interface Health {
  ok: boolean
  version: string
  uptime_s: number
  server_time: number
  db: { path: string; size_bytes: number; dialect?: 'sqlite' | 'postgres' }
  auth?: 'none' | 'supabase'
  services: Record<string, Record<string, unknown>>
  load_errors: string[]
}

export interface AuthConfig {
  mode: 'none' | 'supabase'
  supabase_url: string | null
  supabase_anon_key: string | null
}

export type Settings = Record<string, unknown>

export interface DbStats {
  path: string
  size_bytes: number
  tables: Record<string, number>
}

// ----------------------------------------------------------------------------- market conditions (macro semáforo)

/** green = operar con normalidad · amber = precaución · red = no operar ahora */
export type MacroLevel = 'green' | 'amber' | 'red'

export interface MacroEvent {
  id: string
  title: string // original (English) title from the source
  title_es: string // Spanish title shown to the user ("IPC de EE. UU. (mensual)")
  currency: string // USD | EUR | GBP | JPY | ...
  time: number // unix s
  impact: 'high' | 'medium' | 'low' | 'holiday'
  forecast: string | null
  previous: string | null
  source: string // forexfactory | federalreserve
  countdown_s: number // seconds until the event (negative = already happened)
}

export interface MacroMetric {
  id: string // vix | us_futures | dxy | us10y | move | crypto_fng | funding ...
  label: string // "Miedo en bolsa (VIX)"
  value: number | null
  display: string // "22,65"
  change_pct: number | null
  change_display: string // "+31,8 %" or "+8 pb"
  level: 'ok' | 'warn' | 'danger'
  note: string // "Volatilidad alta: reduce el tamaño"
}

export interface MarketConditions {
  level: MacroLevel
  label: string // "Operar con normalidad" | "Precaución" | "No operar ahora"
  headline: string // one sentence: "IPC de EE. UU. en 24 min: espera a que pase la volatilidad."
  advice: string // what to do: "Reduce el tamaño a la mitad y evita entrar antes del dato."
  reasons: string[] // at most 3
  scope: string // "global" | "crypto" | "forex:EUR/USD" | "us_equities" ...
  symbol: string | null
  blackout: { active: boolean; until: number | null; event: string | null } // "no operar" window; until = unix s
  next_event: MacroEvent | null // next high-impact event relevant to the scope
  events: MacroEvent[] // upcoming relevant events (max ~5)
  metrics: MacroMetric[] // at most 6, only the ones that matter
  updated_at: number // ms
  stale: boolean // data older than expected (source unavailable)
}

export interface MacroAlert {
  kind: 'level' | 'event_soon' | 'event_now' | 'blackout_end' | 'volatility'
  level: MacroLevel
  message: string
}

// ----------------------------------------------------------------------------- websocket

export type ServerMessage =
  | { type: 'hello'; version: string; server_time: number; topics: string[] }
  | { type: 'event'; event: NexusEvent }
  | ({ type: 'command_result'; id: string } & CommandResult)
  | { type: 'candle'; symbol: string; tf: Timeframe; candle: Candle; closed: boolean }
  | { type: 'nexus'; symbol: string; tf: Timeframe; current: NexusCurrent; markers: ChartMarker[] }
  | {
      type: 'sim'
      event: 'bar' | 'state' | 'started' | 'stopped' | 'reset'
      state: SimState
      bar?: Candle
      current?: NexusCurrent | null
      markers?: ChartMarker[]
      positions?: Position[]
    }
  | { type: 'paper'; account: PaperAccount; positions: Position[] }
  | { type: 'scanner'; event: 'started' | 'progress' | 'done' | 'error'; run_id: number; progress?: number; message?: string }
  | { type: 'macro'; conditions: MarketConditions; alert?: MacroAlert | null }
  | ({ type: 'ui' } & UiAction)
  | { type: 'pong' }

export type ServerMessageType = ServerMessage['type']
