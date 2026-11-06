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
}
