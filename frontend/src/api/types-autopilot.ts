// Types of the "NEXUS automática" feature (re-exported by types.ts). Same contract rules as types.ts.
// NEXUS's own demo account (account 3, simulated: never a broker). It trades on its own: on each closed bar of its
// timeframe it opens a position only when NEXUS signals COMPRAR / VENDER and the Trade Gate says TOMAR, and closes it
// on its exit rules, with the same stops, targets and portfolio risk limits as the user's demo account. The user's
// account (id 1) is separate: its WS 'paper' messages never carry account 3.
// One field per line: tools/contract_check.py parses these interfaces line by line.
import type { PaperAccount, Position, Timeframe } from './types'
import type { PaperRisk } from './types-paper-orders'

/** scanner = the best TOMAR opportunities of the latest scan · list = fixed symbols (`symbols`). */
export type AutopilotSource = 'scanner' | 'list'

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
  note: string // honest Spanish note from the backend: shown verbatim
}

/** GET /api/paper/autopilot · the answer of every POST below · WS 'paper' {type: 'paper_autopilot', ...}. */
export interface AutopilotSnapshot {
  account: PaperAccount // id 3
  positions: Position[] // open positions of account 3 (origin 'autopilot')
  history: Position[] // closed positions, newest first
  risk: PaperRisk | null // the portfolio risk rules also apply to account 3
  autopilot: AutopilotStatus
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
}
