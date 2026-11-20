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

/** One asset of the buy-and-hold portfolio and its target share (%). */
export interface AutopilotHoldItem {
  symbol: string
  weight: number
}

export interface AutopilotStatus {
  enabled: boolean
  account_id: number // 3
  source: AutopilotSource
  tf: Timeframe // its main (fastest) timeframe
  tfs: Timeframe[] // every timeframe it trades at once, fastest first (1..3: quick / measured / long-term trades)
  hold_pct: number // buy-and-hold portfolio: % of its equity (0 = off)
  hold_basket: AutopilotHoldItem[] // its assets and target shares (weights add up to 100)
  hold_stop_pct: number // emergency stop of each holding, % below its purchase
  symbols: string[] // used when source = 'list'
  max_positions: number // 1..20
  risk_pct: number // % of equity risked per trade, 0.1..5
  lots: boolean // sizes its trades in MT5 lots (steps of 0.01 lot: gold 100 oz, crypto 1 coin, stocks 1 share)
  lot_max_risk_pct: number // 0.5..25: the most the minimum lot (0.01) may risk at its stop, % of equity
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
  goal: AutopilotGoal | null // its running challenge (null when none, and outside a snapshot)
  goal_last: AutopilotGoalResult | null // how its last challenge ended (null until the first one)
  session_aggressive: boolean // its aggressive mode during the New York stock session only (setting)
  per_tf_positions: boolean // one position per market and timeframe (else one per market)
  session: AutopilotSession // the New York session now and whether that mode applies
  markets: AutopilotMarket[] // market hours: crypto and gold 24/7, the New York exchange, CME index futures
  note: string // honest Spanish note from the backend: shown verbatim
}

/** Its challenge («reto»): reach `target` equity, by `until` if set. The server itself closes its trades (never its
 * portfolio) at the target or the deadline, then goes back to its saved settings (`then` 'normal') or pauses. The
 * aggressive mode overrides some settings while it lasts (described by `mode_text`); the saved ones never change. */
export interface AutopilotGoal {
  target: number
  until: number | null // deadline, unix seconds (null = none)
  started_at: number // unix seconds
  start_equity: number
  aggressive: boolean
  then: 'normal' | 'pause'
  equity: number // the account's equity now
  pnl: number // equity - start_equity
  remaining: number // what is left to the target (≥ 0)
  progress_pct: number // 0..100, from start_equity to target
  seconds_left: number | null // to the deadline (null without one)
  mode_text: string | null // Spanish, the aggressive mode's settings (null when off): shown verbatim
  note: string // Spanish honesty note: shown verbatim
}

/** How a challenge ended: reached (target), time (deadline; `late` when the server was off then) or cancelled. */
export interface AutopilotGoalResult {
  outcome: 'reached' | 'time' | 'cancelled' | 'brake' // brake: challenge accounts only
  target: number
  until: number | null
  started_at: number
  ended_at: number // unix seconds
  start_equity: number
  end_equity: number
  pnl: number
  pnl_pct: number
  trades: number // its trades closed during the challenge (the ones closed at its end included)
  wins: number
  losses: number
  closed: number // trades it closed when it ended
  aggressive: boolean
  late: boolean
  text: string // Spanish summary: shown verbatim
}

/** The New York stock session (Mon–Fri 9:30–16:00 New York time, NYSE holidays and early closes). Open: the times of
 * this session; closed: those of the next one. `active`: its aggressive mode applies now (enabled and open). */
export interface AutopilotSession {
  enabled: boolean
  active: boolean
  open: boolean
  opens_at: number // unix seconds
  closes_at: number // unix seconds
  early_close: boolean // that session closes at 13:00
  known: boolean // false past the holiday lists of the server
  mode_text: string // Spanish, the aggressive mode's settings: shown verbatim
}

/** One market NEXUS automática may trade and its hours now: opens_at while closed, closes_at while open (null: never). */
export interface AutopilotMarket {
  key: 'crypto' | 'ny' | 'cme'
  label: string // Spanish
  open: boolean
  opens_at: number | null // unix seconds
  closes_at: number | null // unix seconds
  note: string // Spanish: its schedule, shown verbatim
}

/** POST /api/paper/autopilot/goal: start a challenge (400 {detail} in Spanish on a bad value or while one runs). */
export interface AutopilotGoalRequest {
  target_equity: number
  until?: number | null // unix seconds
  aggressive?: boolean
  then?: 'normal' | 'pause'
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
  challenges: AutopilotChallenge[] // its challenge accounts: running first, then the newest (never the 1k account)
  twin: AutopilotTwin | null // the 1k account (POST /api/paper/autopilot/twin creates it), null when there is none
  twins: AutopilotTwin[] // every running small account (1k, 500, 100…), the largest first; twin = the first
  vault: AutopilotVault | null // the vault: the 10k account's realized excess over its initial balance
  main_goal: AutopilotMainGoal | null // the 10k account's goal (autopilot.main_target; null = none)
}

/** The 10k account's goal: its total is the account's equity plus what it put in the vault. */
export interface AutopilotMainGoal {
  target: number
  total: number // equity + saved
  saved: number // what it put in the vault
  initial: number
  progress_pct: number // 0..100 from its initial balance to the target
  remaining: number
  reached: boolean
}

/** The 1k account («NEXUS 1k», kind 'twin'): a second account of NEXUS automática with its own balance, the same
 * candidates and signals, and the small-account mindset (`mindset`, shown verbatim). No target, no deadline, never
 * aggressive; its brake stops new entries but never ends it. */
export interface AutopilotTwin {
  id: number
  name: string
  status: string
  kind: 'twin'
  initial: number
  brake_pct: number
  started_at: number
  account: PaperAccount
  positions: Position[]
  history: Position[] // its last 30 closed, newest first
  log: AutopilotLogEntry[]
  brake: AutopilotBrake
  mindset: string // Spanish: its rules, verbatim
  target: number | null // its goal (10× its initial balance by default: 1.000 $ -> 10.000 $; it keeps its gains)
  progress_pct: number | null // 0..100
  remaining: number | null
  reached_at: number | null // unix s when it first reached the goal (it keeps trading)
}

/** What one account put in the vault (also 0 for an account that never did). */
export interface AutopilotVaultAccount {
  account_id: number
  name: string
  amount: number
}

/** One sweep into the vault. */
export interface AutopilotVaultEntry {
  ts: number // unix seconds
  account_id: number
  name: string
  amount: number
  base: number // the account's initial balance it went back to
}

/** The vault (setting autopilot.vault): a number that never trades. */
export interface AutopilotVault {
  enabled: boolean
  total: number
  by_account: AutopilotVaultAccount[]
  entries: AutopilotVaultEntry[] // the last 20, newest first
  note: string
}

/** A challenge account («cuenta de reto»): its own balance, target, optional deadline and loss brake, traded by NEXUS's
 * brain (account 3's candidates and signals) in the aggressive mode and sized on its own equity. The server ends it by
 * itself: reached (target), time (deadline), brake (brake on with nothing open) or cancelled (by hand). */
export interface AutopilotChallenge {
  id: number // its paper account id (101…)
  name: string
  status: 'running' | 'reached' | 'time' | 'brake' | 'cancelled'
  initial: number
  target: number
  until: number | null // unix seconds
  brake_pct: number
  aggressive: boolean
  started_at: number // unix seconds
  ended_at: number | null
  account: PaperAccount
  positions: Position[] // open
  history: Position[] // its last 10 closed, newest first
  log: AutopilotLogEntry[] // its last 15 decisions, newest first
  brake: AutopilotBrake
  progress_pct: number // 0..100 from initial to target
  remaining: number // to the target (≥ 0)
  seconds_left: number | null // to its deadline while it runs
  result: AutopilotGoalResult | null // how it ended
}

/** POST /api/paper/autopilot/challenges (400 {detail} in Spanish on a bad value). */
export interface AutopilotChallengeRequest {
  initial_balance: number
  target_equity: number
  until?: number | null // unix seconds
  brake_pct?: number | null // 5..95 (default 50)
  name?: string | null
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
  tfs?: Timeframe[] // 1..3 timeframes at once; `tf` alone goes back to one
  lots?: boolean // MT5 lots on / off
  lot_max_risk_pct?: number // 0.5..25
  session_aggressive?: boolean // aggressive mode during the New York session only
  per_tf_positions?: boolean // one position per market and timeframe
}

/** GET /api/paper/autopilot/calendar: the closed trades of NEXUS automática's accounts with exit in [start, end). */
export interface AutopilotCalendar {
  start: number // unix s
  end: number
  trades: CalendarTrade[] // newest first
  truncated: boolean
  note: string
}

/** One closed trade in the calendar (bitácora): how it opened, how it closed and why it won or lost. */
export interface CalendarTrade {
  id: number
  account_id: number
  account: string
  symbol: string
  tf: string
  side: 'long' | 'short'
  qty: number
  entry_time: number
  exit_time: number
  entry_price: number | null
  exit_price: number | null
  pnl: number
  r: number | null
  result: 'win' | 'loss' | 'flat'
  exit_reason: string | null
  exit_label: string | null
  cause: string // app.sim.journal.CAUSE_LABEL key
  cause_label: string // Spanish
  mfe_r: number | null
  mae_r: number | null
  held_s: number | null
  why: string | null // why it opened (Spanish)
  strategy: string | null
  nexus_read: string | null
  gate_line: string | null
  regime: string | null
  lesson: string // Spanish: why it ended as it did
}
