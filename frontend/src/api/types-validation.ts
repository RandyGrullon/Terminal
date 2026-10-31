// Types of the validation feature (re-exported by types.ts). Same contract rules as types.ts.
// Walk-forward validation of NEXUS + Trade Gate (backend app.validation, endpoints /api/validation/*):
//   POST /api/validation/run ValidationRunRequest -> ValidationStatus (400 invalid input, 409 a job is running)
//   GET  /api/validation/status                  -> ValidationStatus
//   GET  /api/validation/latest                  -> ValidationLatestResponse
// R multiples are net of commissions and slippage; *_pct values are percent units; rates are fractions 0..1;
// times: *_time = unix seconds (bar open), *_at = unix milliseconds.

export type ValidationVerdictCode = 'insufficient' | 'negative' | 'no_edge' | 'edge'

/** Honest verdict: label "Sin evidencia de ventaja" unless every condition holds ("Indicios de ventaja"). */
export interface ValidationVerdict {
  code: ValidationVerdictCode
  label: string
  text: string
}

export interface ValidationSummary extends ValidationVerdict {
  /** "SYMBOL tf" for a single series, a timeframe ("1h") or "all"; "none" when nothing was validated. */
  scope: string
}

/** Statistics of a trade variant: trades with a known outcome only. */
export interface ValidationRStats {
  trades: number
  wins: number
  win_rate: number | null
  /** Mean R per trade after costs. */
  expectancy_r: number | null
  /** 95 % bootstrap interval of the expectancy; null with fewer than 10 trades (reports stored before that rule may
   *  still carry one: the card hides it). Below 30 trades it is shown as "muy incierto". */
  ci95: [number, number] | null
  /** Null without trades or without losing trades. */
  profit_factor: number | null
  total_r: number
  /** Closed-trade equity compounding `risk_pct` per R, in time order: the same measure as the random sequences'
   *  `trade_max_dd_p50`, so these two drawdowns are the comparable pair. */
  trade_return_pct: number
  trade_max_dd_pct: number
  /** Simulated account (per-bar equity, `risk_pct` of equity per trade): `nexus` and `ungated` only. Its drawdown is
   *  per bar (open trades marked to market), comparable with buy and hold, not with the random sequences. */
  return_pct?: number | null
  max_dd_pct?: number | null
  /** Annualised Sharpe of the per-bar account returns. */
  sharpe?: number | null
}

export interface ValidationEquityStats {
  return_pct: number | null
  max_dd_pct: number | null
  sharpe: number | null
}

/** Monte Carlo of random entries: same trades per fold, same long/short mix, NEXUS' levels, mechanical exits. */
export interface ValidationRandomBase {
  runs: number
  trades: number
  /** Mean of the expectancy of the random sequences. */
  expectancy_r: number | null
  /** p5, p50, p95 of the expectancy of the random sequences. */
  expectancy_band: [number, number, number] | null
  win_rate: number | null
  profit_factor_p50: number | null
  /** Median max drawdown of the closed-trade equity of the random sequences (compare with `trade_max_dd_pct`). */
  trade_max_dd_p50: number | null
  /** Where the compared variant falls inside the random sequences (0..100). */
  percentile: number | null
  /** One-sided p-value of "better than random". */
  p_value: number | null
}

/** The random baseline of NEXUS + gate. */
export interface ValidationRandom extends ValidationRandomBase {
  /** Same comparison with NEXUS' entries re-run through the mechanical exits ("mismas salidas"). */
  percentile_mechanical: number | null
  p_value_mechanical: number | null
}

export interface ValidationSection {
  /** NEXUS + Trade Gate: the product. */
  nexus: ValidationRStats
  /** NEXUS taking every candidate (no gate). */
  ungated: ValidationRStats
  /** NEXUS + gate entries with the mechanical exits only. */
  mechanical: ValidationRStats
  buy_hold: ValidationEquityStats
  random: ValidationRandom
  /** NEXUS without the gate: its entries with the mechanical exits only. */
  ungated_mechanical?: ValidationRStats
  /** Random entries matched to NEXUS without the gate (its own trade counts and side mix). */
  random_ungated?: ValidationRandom
}

export interface ValidationFold extends ValidationSection {
  index: number
  start_time: number
  end_time: number
  bars: number
}

export interface ValidationGateEffect {
  trades_gated: number
  trades_ungated: number
  /** Mean R with the gate − mean R without it. */
  diff_r: number | null
  ci95: [number, number] | null
  code: 'insufficient' | 'better' | 'worse' | 'unclear'
  text: string
}

export interface ValidationSeries {
  symbol: string
  tf: string
  provider: string | null
  history: string
  note: string | null
  bars: number
  start_time: number
  end_time: number
  /** First decision bar that counts (after the warm-up). */
  test_start_time: number
  warmup_bars: number
  /** Missing bars inside the series: 24/7 markets only (crypto, SIM:*); null for markets with sessions, whose nights,
   *  weekends and holidays are not missing data. */
  gaps: number | null
  fee_pct: number
  slippage_pct: number
  risk_pct: number
  params: {
    entry_threshold: number
    sl_atr: number
    tp_r: number[]
    trail_atr: number
    time_stop_bars: number
    take_score: number
    allow_short: boolean
  }
  overall: ValidationSection
  folds: ValidationFold[]
  folds_positive: number
  folds_with_trades: number
  folds_positive_ungated?: number
  folds_with_trades_ungated?: number
  gate_effect: ValidationGateEffect
  /** Verdict of the product (NEXUS + gate). */
  verdict: ValidationVerdict
  /** Same rules applied to NEXUS without the gate against its own random baseline. */
  verdict_ungated?: ValidationVerdict
  /** NEXUS trades still open at the end of the data (excluded from the statistics). */
  open_trades: number
  /** Signals the gate rejected over the whole run, warm-up included (the engine's own count). */
  rejected_signals: number
  exit_reasons: Record<string, number>
  side_mix: { long: number; short: number }
  mechanical_exits: number
}

/** Entry signal vs exits of pooled trades (block intervals): the same entries with the mechanical exits. */
export interface ValidationExitSplit {
  mechanical_r?: number | null
  /** Mechanical-exit R over random entries with the same exits (same fold and side): the entry signal alone. */
  mechanical_excess_r?: number | null
  mechanical_excess_ci95?: [number, number] | null
  /** What the model exits (early exit, signal, reverse) add per trade over the mechanical exits (paired). */
  exit_effect_r?: number | null
  exit_effect_ci95?: [number, number] | null
}

/** Pooled trades of NEXUS without the gate in a group. */
export interface ValidationGroupVariant extends ValidationExitSplit {
  trades: number
  wins: number
  win_rate: number | null
  expectancy_r: number | null
  ci95_blocks: [number, number] | null
  ci95_iid: [number, number] | null
  blocks: number
  excess_r: number | null
  excess_ci95: [number, number] | null
  positive_series: number
  verdict: ValidationVerdict
}

/** Several series pooled (one timeframe, or "all"): intervals by time blocks across assets. */
export interface ValidationGroup extends ValidationExitSplit {
  key: string
  label: string
  series: number
  assets: number
  symbols: string[]
  tfs: string[]
  /** NEXUS + gate trades pooled (the fields below up to excess_ci95). */
  trades: number
  wins: number
  win_rate: number | null
  expectancy_r: number | null
  /** Resampling whole time blocks: correlated assets are not counted as independent evidence. */
  ci95_blocks: [number, number] | null
  /** What the interval would be if every trade were independent (shown for comparison only). */
  ci95_iid: [number, number] | null
  block_days: number
  blocks: number
  /** Mean R over a random entry of the same fold and side. */
  excess_r: number | null
  excess_ci95: [number, number] | null
  ungated_expectancy_r: number | null
  ungated?: ValidationGroupVariant
  nexus_return_pct_avg: number | null
  buy_hold_return_pct_avg: number | null
  random_expectancy_r_avg: number | null
  positive_series: number
  edge_series: number
  avg_correlation: number | null
  correlation_pairs: number
  effective_assets: number | null
  verdict: ValidationVerdict
}

export interface ValidationConfig {
  folds: number
  warmup_bars: number
  random_runs: number
  bootstrap_runs: number
  risk_pct: number
  slippage_pct: number
  seed: number
}

export interface ValidationRequestInfo {
  symbols: string[]
  tfs: string[]
  bars: number
}

export interface ValidationSeriesError {
  symbol: string
  tf: string
  message: string
}

export interface ValidationReport {
  version: number
  source: 'app' | 'cli'
  started_at: number
  generated_at: number
  duration_s: number
  request: ValidationRequestInfo
  config: ValidationConfig
  summary: ValidationSummary
  groups: ValidationGroup[]
  series: ValidationSeries[]
  errors: ValidationSeriesError[]
  method: string[]
  disclaimer: string
}

export type ValidationJobState = 'running' | 'done' | 'error' | 'cancelled'

export interface ValidationJob {
  id: number
  state: ValidationJobState
  request: ValidationRequestInfo
  total: number
  done: number
  /** "SYMBOL tf" being processed. */
  current: string | null
  message: string
  started_at: number
  finished_at: number | null
  errors: { series: string | null; message: string }[]
  error: string | null
  summary: ValidationSummary | null
}

export interface ValidationStatus {
  /** A job is in progress. */
  running: boolean
  /** The last job's state, "idle" when none ran since the server started. */
  state: ValidationJobState | 'idle'
  job: ValidationJob | null
  /** Default request (settings validation.symbols / validation.tfs / validation.bars). */
  defaults: ValidationRequestInfo
}

export interface ValidationLatestResponse {
  report: ValidationReport | null
}

export interface ValidationRunRequest {
  symbols?: string[]
  tfs?: string[]
  bars?: number
}
