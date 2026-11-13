// Types of the "NEXUS automática" feature (re-exported by types.ts). Same contract rules as types.ts.
// NEXUS's own demo account (account 3, simulated: never a broker). It trades on its own: on each closed bar of its
// timeframe it opens a position only when NEXUS signals COMPRAR / VENDER and the Trade Gate says TOMAR, and closes it
// on its exit rules, with the same stops, targets and portfolio risk limits as the user's demo account. The user's
// account (id 1) is separate: its WS 'paper' messages never carry account 3.
// One field per line: tools/contract_check.py parses these interfaces line by line.
import type { PaperAccount, Position, Timeframe } from './types'
import type { PaperRisk } from './types-paper-orders'

/** scanner = the best TOMAR opportunities of the latest scan · list = fixed symbols (`symbols`) · both = the two. */
export type AutopilotSource = 'scanner' | 'list' | 'both'
/** How it trades stocks and funds: auto = shares to bet on a rise and a put on a fall (a cash account cannot sell
 *  short) · shares = shares both ways (a simulated short sale) · options = a call or a put (theoretical prices). */
export type AutopilotStockInstrument = 'auto' | 'shares' | 'options'

/** Why a symbol is watched (Spanish, shown as is). */
export type AutopilotWhy = 'escáner' | 'lista' | 'posición abierta'

/** What NEXUS said on the last closed bar it decided for a watched symbol (most bars give no entry: ESPERAR /
 *  MANTENER, which the decision log does not list). */
export interface AutopilotLast {
  bar_time: number // ms: open time of that closed bar
  tf: Timeframe
  action: string | null // BUY | SELL | WAIT | HOLD_LONG | …; null = no analysis (missing data or an error)
  label: string // Spanish: "COMPRAR", "ESPERAR", "Sin análisis (faltan datos)", "Error al analizar"
}

export interface AutopilotWatch {
  symbol: string
  why: AutopilotWhy
  direction?: 'long' | 'short' | null // the scan's direction (escáner) or the open side; null = NEXUS decides
  last?: AutopilotLast | null // null until its first closed bar at the current timeframe
}

/** One decision of NEXUS's account (newest first in AutopilotStatus.log). */
export interface AutopilotLogEntry {
  ts: number // ms
  symbol: string // "—" when the entry is not about one symbol (e.g. "Activada")
  kind: 'open' | 'close' | 'skip' | 'info'
  text: string // Spanish
}

/** The scan its candidates came from (source scanner). */
export interface AutopilotScan {
  id: number
  finished_at: number | null // ms; null = not finished yet
  opportunities: number // TOMAR opportunities (gate TOMAR + NEXUS signal) in that scan
}

export interface AutopilotStatus {
  enabled: boolean
  account_id: number // 3
  source: AutopilotSource
  tf: Timeframe
  symbols: string[] // used when source = 'list'
  max_positions: number // 1..20
  risk_pct: number // % of equity risked per trade, 0.1..5
  scanner_top: number // 1..30: how many opportunities of the latest scan it may watch
  watching: AutopilotWatch[]
  scan: AutopilotScan | null
  log: AutopilotLogEntry[] // newest first, at most 30
  alerts: boolean // its trades as alerts by Telegram / email (setting alerts.autopilot, off by default)
  alert_channels: number // alert channels on and complete now (0 = its alerts would reach nobody)
  initial_balance: number // what its account starts with (autopilot.initial_balance, 1000 by default)
  max_exposure_pct: number // cash account: shares / funds / crypto held ≤ this % of equity (0 = only the 5× cap)
  max_drawdown_pct: number // loss brake: no new entries this % below the initial balance (0 = off)
  futures_margin_pct: number // index futures: equity tied up as approximate margin at most (0 = no futures)
  stock_instrument: AutopilotStockInstrument // stocks and funds: shares, options or NEXUS's choice
  options_premium_pct: number // options: the most premium paid per trade, % of equity (0 = no options)
  learn_guard: boolean // it learns from its losses: no entries where its recent trades lose with strong evidence
  paused_contexts: AutopilotPausedContext[] // where it does not open now (learn_guard), [] when off or none
  script_ids: number[] // saved PyChart strategies that also give it entries when NEXUS has none (at most 3)
  script_gate: boolean // their entries also need the gate's TOMAR
  scripts: AutopilotScript[] // those strategies with their last check
  health: AutopilotHealth // its health: decisions arriving, stops of the server / PC detected
  weekly: AutopilotWeekly | null // its last weekly report (Mondays), null until the first one
  brake: AutopilotBrake | null // null outside a snapshot
  note: string // honest Spanish note from the backend: shown verbatim
}

/** Its loss brake: equity `limit_pct` % below the initial balance stops new entries (open ones keep their exits). */
export interface AutopilotBrake {
  limit_pct: number
  drawdown_pct: number // how far its equity is below the initial balance now (≥ 0)
  active: boolean
}

/** GET /api/paper/autopilot · the answer of every POST below · WS 'paper' {type: 'paper_autopilot', ...}. */
export interface AutopilotSnapshot {
  account: PaperAccount // id 3
  positions: Position[] // open positions of account 3 (origin 'autopilot')
  history: Position[] // closed positions, newest first
  risk: PaperRisk | null // the portfolio risk rules also apply to account 3
  autopilot: AutopilotStatus
}

/** Honest verdict of the comparison with random entries (app.sim.autopilot_compare, rules of app.validation). */
export type AutopilotVerdict = 'none' | 'insufficient' | 'no_edge' | 'better' | 'worse'

/** NEXUS automática's side of the comparison (its compared closed trades). */
export interface AutopilotCompareNexus {
  n: number // compared trades
  wins: number // with R > 0
  mean_r: number | null // mean R per trade, net of commissions
}

/** The random entries behind the references (long and short, in the bars before each decision). */
export interface AutopilotCompareRandom {
  entries: number // random entries with a known outcome
  wins: number // of them with R > 0
  mean_r: number | null // mean of the per-trade references (R)
}

/** A context where its trades of the last 90 days lose with strong evidence (30 or more, the whole 99 % interval of
 *  the mean R below zero): its trader opens nothing there while learn_guard is on. */
export interface AutopilotPausedContext {
  dimension: string // regime | session | news | gate | side | market
  label: string // the dimension, e.g. «Sesión de entrada»
  key: string
  bucket_label: string // e.g. «Londres (3–8 h)»
  n: number
  wins: number
  mean_r: number
  ci: [number, number] // 99 % interval of the mean R (block bootstrap by day)
}

/** Its health: the newest decision and the 24/7 markets without a recent one (their candles stopped arriving). */
export interface AutopilotHealth {
  ok: boolean // false while a watched 24/7 market has had no decision for over three bars
  last_decision: number | null // close of the newest bar it decided (ms)
  stale: string[] // watched 24/7 markets without a recent decision
  downtime: AutopilotDowntime | null // the last stop detected (server stopped, PC asleep), shown for a day
  text: string // Spanish summary
}

/** A stop of the server (or the PC) while it was on: from its last heartbeat to when it ran again (ms). */
export interface AutopilotDowntime {
  from: number
  to: number
}

/** Its report of the last week (Monday 00:00 UTC to Monday): closed trades, «wins de trades», R and P&L. */
export interface AutopilotWeekly {
  week_start: number
  week_end: number
  trades: number
  wins: number
  total_r: number | null
  mean_r: number | null
  pnl: number
  text: string
  generated_at: number
}

/** Verdict of a group of its closed trades: none · small (under 30) · unclear (no interval, or it includes 0) ·
 *  loses / wins (the whole 95 % interval of the mean R below / above 0). */
export type AutopilotJournalVerdict = 'none' | 'small' | 'unclear' | 'loses' | 'wins'

/** One group of its closed trades (e.g. cause «En contra desde el principio», session «Londres»). */
export interface AutopilotJournalBucket {
  key: string
  label: string
  n: number // closed trades in the group
  wins: number // winners: «wins de n»
  mean_r: number | null // mean R, net of fees
  ci95: [number, number] | null // 95 % interval of the mean R (bootstrap by day; none below 10 trades or 8 days)
  verdict: AutopilotJournalVerdict
  verdict_label: string
}

/** One way of grouping its trades: cause · session · regime · news · gate · side · market. */
export interface AutopilotJournalGroup {
  dimension: string
  label: string
  buckets: AutopilotJournalBucket[]
}

/** GET /api/paper/autopilot/journal[?fresh=true]: the journal of its closed trades (app.sim.journal). */
export interface AutopilotJournal {
  generated_at: number
  trades: number
  wins: number
  mean_r: number | null
  ci95: [number, number] | null
  verdict: AutopilotJournalVerdict
  verdict_label: string
  mae_r_losers: number | null // mean adverse excursion of the losers (R)
  mfe_r_losers: number | null // mean favourable excursion of the losers (R): how far they went right before losing
  mae_r_winners: number | null
  mfe_r_winners: number | null
  groups: AutopilotJournalGroup[]
  insights: string[] // plain-Spanish findings, «x de n»; nothing concluded below 30 trades
  note: string
}

/** GET /api/paper/autopilot/compare[?fresh=true]: NEXUS automática against random entries in the same markets. */
export interface AutopilotComparison {
  generated_at: number // ms
  window_bars: number // random entries at each of these bars up to a decision
  trades: number // closed trades of NEXUS automática considered (the newest 200)
  compared: number // with a reference
  missing: number // without one: no candles of their market, or outcomes not known yet
  days: number // distinct UTC days of the compared decisions (the interval resamples whole days)
  nexus: AutopilotCompareNexus
  random: AutopilotCompareRandom
  diff_r: number | null // mean of R(NEXUS) − R(reference) per trade
  ci95: [number, number] | null // 95 % block bootstrap by day; null below 10 trades or 8 days
  verdict: AutopilotVerdict
  verdict_label: string // Spanish
  text: string // Spanish, shown verbatim
  note: string // the method, Spanish, shown verbatim
}

/** A saved PyChart strategy NEXUS automática follows (autopilot.script_ids): its name and its last error. */
export interface AutopilotScript {
  id: number
  name: string
  error: string | null // Spanish: why its last run gave no signal (a script error, not a strategy, no candles…)
  checked_at: number | null // ms of its last run (or of its check when chosen)
}

/** POST /api/paper/autopilot: only the fields being changed (400 {detail: '<Spanish>'} on a bad value). */
export interface AutopilotUpdate {
  enabled?: boolean
  source?: AutopilotSource
  symbols?: string[]
  tf?: Timeframe
  max_positions?: number
  risk_pct?: number
  scanner_top?: number
  alerts?: boolean
  max_exposure_pct?: number
  max_drawdown_pct?: number
  futures_margin_pct?: number
  stock_instrument?: AutopilotStockInstrument
  options_premium_pct?: number
  learn_guard?: boolean
  script_ids?: number[] // [] = none
  script_gate?: boolean
}
