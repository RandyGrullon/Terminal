// The side cards of NEXUS automática's trades (TradeAlerts): what it bought or sold, how much, and the two ways the trade
// can end — its money at the next target and at the stop (before costs; never an "expected" profit: the published
// validation found no edge, so a single expected number would mislead) — and, once closed, its real result. Built by
// diffing the account's snapshots (WS 'paper' {type: 'paper_autopilot'}): a new open id is an opening, an open id that
// moved to the history is a close. Pure: no React, no formatting of money here.
import type { Position } from '../api/types'
import { styleOf, STYLE_WORD } from './tradeStyle'

export type AlertAction = 'COMPRA' | 'VENTA'

export interface AlertLevel {
  label: string
  price: number
  /** Money at that price for the whole open quantity, before costs (positive = gain). */
  money: number
}

export interface TradeAlert {
  key: string
  kind: 'open' | 'close'
  /** Opening a long or closing a short is a purchase; opening a short or closing a long is a sale. */
  action: AlertAction
  position: Position
  ticker: string
  /** "Perpetuo 10x", "Cripto al contado", "Acción o ETF", "Futuro MES", "Opción put sobre AAPL"… */
  product: string
  /** "5m · rápida" */
  speed: string
  /** Value of what it bought or sold (quantity × entry price; an option: the premium paid). */
  notional: number | null
  /** Collateral of a future or perpetual (contracts × margin), null otherwise. */
  margin: number | null
  target: AlertLevel | null
  stop: AlertLevel | null
  /** Closed trades only. */
  pnl: number | null
  r: number | null
  durationS: number | null
}

const CRYPTO_EXCHANGES = new Set([
  'BINANCE', 'OKX', 'COINBASE', 'KRAKEN', 'KUCOIN', 'BYBIT', 'HTX', 'MEXC', 'BITGET', 'GEMINI', 'BITSTAMP', 'GATEIO',
  'BITFINEX', 'CRYPTO', 'CRYPTOCOM', 'POLONIEX', 'BITMEX', 'DERIBIT', 'UPBIT',
])
const CFD_EXCHANGES = new Set(['CAPITALCOM', 'OANDA', 'FX', 'FOREXCOM', 'PEPPERSTONE', 'ICMARKETS', 'FXCM', 'SAXO', 'EIGHTCAP'])

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function split(symbol: string): { exchange: string; ticker: string } {
  const s = String(symbol || '').toUpperCase()
  const i = s.indexOf(':')
  return i >= 0 ? { exchange: s.slice(0, i), ticker: s.slice(i + 1) } : { exchange: '', ticker: s }
}

export function productOf(p: Position): string {
  const inst = p.instrument
  if (inst?.kind === 'option') {
    const under = split(inst.underlying ?? '').ticker || inst.name
    return `Opción ${inst.right === 'put' ? 'put' : 'call'} sobre ${under}`
  }
  if (inst?.kind === 'future') {
    if (inst.perpetual) return fin(inst.leverage) ? `Perpetuo ${Math.round(inst.leverage)}x` : 'Perpetuo'
    return `Futuro ${inst.root ?? ''}`.trim()
  }
  const { exchange, ticker } = split(p.symbol)
  if (CRYPTO_EXCHANGES.has(exchange) || /(USDT|USDC|FDUSD)$/.test(ticker)) return 'Cripto al contado'
  if (CFD_EXCHANGES.has(exchange)) return 'CFD'
  return 'Acción o ETF'
}

function units(p: Position): number {
  return fin(p.qty_open) && p.qty_open > 0 ? p.qty_open : fin(p.qty) ? p.qty : 0
}

/** The next target (first not reached) and the stop that closes it now (the dynamic stop when tighter). */
function levels(p: Position): { target: AlertLevel | null; stop: AlertLevel | null } {
  const s = p.side === 'short' ? -1 : 1
  const qty = units(p)
  const tps = [p.tp1, p.tp2, p.tp3]
  const hits = fin(p.tp_hits) ? p.tp_hits : 0
  let target: AlertLevel | null = null
  for (let i = hits; i < 3; i++) {
    const price = tps[i]
    if (fin(price)) {
      target = { label: `TP${i + 1}`, price, money: s * (price - p.entry_price) * qty }
      break
    }
  }
  let stopPrice = fin(p.sl) ? p.sl : null
  if (fin(p.trail) && stopPrice !== null && (s > 0 ? p.trail > stopPrice : p.trail < stopPrice)) stopPrice = p.trail
  const stop = stopPrice !== null ? { label: 'Stop', price: stopPrice, money: s * (stopPrice - p.entry_price) * qty } : null
  return { target, stop }
}

function base(p: Position, kind: 'open' | 'close'): Omit<TradeAlert, 'target' | 'stop' | 'pnl' | 'r' | 'durationS'> {
  const long = p.side !== 'short'
  const inst = p.instrument
  const margin =
    inst?.kind === 'future' && fin(inst.margin) && fin(inst.multiplier) && inst.multiplier > 0
      ? (units(p) / inst.multiplier) * inst.margin
      : null
  const style = styleOf(p.tf)
  return {
    key: `${kind}-${p.id}`,
    kind,
    action: kind === 'open' ? (long ? 'COMPRA' : 'VENTA') : long ? 'VENTA' : 'COMPRA',
    position: p,
    ticker: split(p.symbol).ticker,
    product: productOf(p),
    speed: `${p.tf} · ${STYLE_WORD[style]}`,
    notional: fin(p.entry_price) ? units(p) * p.entry_price : null,
    margin,
  }
}

export function openAlert(p: Position): TradeAlert {
  const { target, stop } = levels(p)
  return { ...base(p, 'open'), target, stop, pnl: null, r: null, durationS: null }
}

export function closeAlert(p: Position): TradeAlert {
  const dur = fin(p.exit_time) && fin(p.entry_time) ? Math.max(0, p.exit_time - p.entry_time) : null
  return {
    ...base(p, 'close'),
    notional: fin(p.qty) && fin(p.entry_price) ? p.qty * p.entry_price : null,
    target: null,
    stop: null,
    pnl: fin(p.pnl) ? p.pnl : null,
    r: fin(p.r_multiple) ? p.r_multiple : null,
    durationS: dur,
  }
}

/** Alerts between two snapshots of the account: openings (new open ids) and closes (open ids now in the history).
 *  `known` null = the first snapshot: it only learns what is open (no flood of cards when the app loads). */
export function diffSnapshot(
  known: ReadonlySet<number> | null,
  positions: readonly Position[],
  history: readonly Position[],
): { known: Set<number>; alerts: TradeAlert[] } {
  const now = new Set(positions.map((p) => p.id))
  if (known === null) return { known: now, alerts: [] }
  const alerts: TradeAlert[] = []
  for (const p of positions) if (!known.has(p.id)) alerts.push(openAlert(p))
  for (const id of known) {
    if (now.has(id)) continue
    const done = history.find((h) => h.id === id)
    if (done) alerts.push(closeAlert(done))
  }
  return { known: now, alerts }
}
