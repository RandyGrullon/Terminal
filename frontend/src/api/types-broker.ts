// Mirrors of NEXUS automática in the user's broker accounts (backend app.broker): Alpaca PAPER and Binance's testnets
// (practice only), and MetaTrader 5 of any broker (demo, or one REAL account the user authorized by its number).
// GET /api/broker/status and POST /api/broker/sync (Alpaca), GET /api/broker/binance/status and POST
// /api/broker/binance/sync (Binance testnets), GET /api/broker/mt5/status and POST /api/broker/mt5/sync (MT5) answer a
// BrokerStatus; keys and passwords are never sent back.
// One field per line: tools/contract_check.py compares these interfaces with the live server.

/** The practice account as the venue reports it (Binance testnets: futures margin balance as equity, spot USDT as
 *  cash, futures available balance as buying power, currency USDT). */
export interface BrokerAccount {
  equity: number | null
  cash: number | null
  buying_power: number | null
  status: string
  currency: string
  login?: number // MetaTrader 5 only: the account open in the terminal
  server?: string // MT5: the broker's server
  company?: string // MT5: the broker
  kind?: string // MT5: demo | contest | real
  leverage?: number // MT5: the account's leverage (1:N)
}

/** One position of NEXUS automática and what the mirror did with it. Prices in the market's currency. */
export interface BrokerMirrorEntry {
  pos_id: number
  symbol: string
  alpaca_symbol: string | null // the venue's symbol: AAPL, BTC/USD on Alpaca · BTCUSDT on Binance
  side: string // long | short
  status: string // working | open | waiting | closed | skipped | rejected | error
  status_label: string
  reason: string | null
  sim_qty: number
  held: number
  sim_entry: number
  fill_entry: number | null
  sim_exit: number | null
  fill_exit: number | null
  slip_entry_bps: number | null // positive = the practice fill was worse than the simulation
  slip_exit_bps: number | null
  sim_result_pct: number | null
  mirror_result_pct: number | null
  opened_at: number | null
  closed_at: number | null
}

/** Counts as «x de n» and the mean price differences (basis points, positive = worse than the simulation). */
export interface BrokerStats {
  entries: number
  mirrored: number
  skipped: number
  rejected: number
  closed: number
  slip_entry_bps: number | null
  slip_entry_n: number
  slip_exit_bps: number | null
  slip_exit_n: number
  result_diff_pct: number | null
  result_n: number
  text: string
}

export interface BrokerStatus {
  enabled: boolean
  provider: string // alpaca | binance_testnet
  paper: boolean
  configured: boolean
  connected: boolean
  account: BrokerAccount | null
  error: string | null
  last_sync_ms: number | null
  mirror_stocks: boolean
  mirror_crypto: boolean // spot crypto
  mirror_perps: boolean // crypto perpetuals (Binance testnets; always false on Alpaca)
  entries: BrokerMirrorEntry[]
  stats: BrokerStats
  note: string
  max_lots?: number // MT5: cap per order (lots)
  allow_real?: boolean // MT5: the user's switch to allow one REAL account
  blocked?: string | null // MT5: why it sends nothing to the account open now (a real one not authorized…)
}
