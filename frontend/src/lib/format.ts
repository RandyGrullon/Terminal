// Formatting helpers, Spanish label maps and semantic tones shared by every page.
import type { Level, NexusAction, Regime, Verdict } from '../api/types'

export type Tone = 'pos' | 'neg' | 'warn' | 'info' | 'muted' | 'accent' | 'purple'

export const ACTION_LABEL: Record<NexusAction, string> = {
  BUY: 'COMPRAR',
  SELL: 'VENDER',
  HOLD_LONG: 'MANTENER LARGO',
  HOLD_SHORT: 'MANTENER CORTO',
  WAIT: 'ESPERAR',
  EXIT_LONG: 'CERRAR LARGO',
  EXIT_SHORT: 'CERRAR CORTO',
}

export const VERDICT_LABEL: Record<Verdict, string> = { TAKE: 'TOMAR', WAIT: 'ESPERAR', AVOID: 'NO TOMAR' }

export const REGIME_LABEL: Record<Regime, string> = {
  trend_up: 'Tendencia alcista',
  trend_down: 'Tendencia bajista',
  range: 'Lateral',
  volatile: 'Volátil',
  unknown: 'Desconocido',
}

export const DIRECTION_LABEL = { long: 'Largo', short: 'Corto' } as const

export const EXIT_REASON_LABEL: Record<string, string> = {
  SL: 'Stop loss',
  TRAIL: 'Stop dinámico',
  TP1: 'Objetivo 1',
  TP2: 'Objetivo 2',
  TP3: 'Objetivo 3',
  EARLY: 'Salida anticipada',
  REVERSE: 'Señal contraria',
  TIME: 'Tiempo agotado',
  SIGNAL: 'Señal de salida',
  MANUAL: 'Manual',
  OPEN: 'Abierta',
  EXPIRY: 'Vencimiento de la opción',
}

export function actionTone(action: string | null | undefined): Tone {
  switch (action) {
    case 'BUY':
    case 'HOLD_LONG':
      return 'pos'
    case 'SELL':
    case 'HOLD_SHORT':
      return 'neg'
    case 'EXIT_LONG':
    case 'EXIT_SHORT':
      return 'warn'
    default:
      return 'muted'
  }
}

export function verdictTone(v: Verdict | string | null | undefined): Tone {
  return v === 'TAKE' ? 'pos' : v === 'AVOID' ? 'neg' : v === 'WAIT' ? 'warn' : 'muted'
}

export function regimeTone(r: string | null | undefined): Tone {
  return r === 'trend_up' ? 'pos' : r === 'trend_down' ? 'neg' : r === 'volatile' ? 'warn' : 'info'
}

export function signTone(v: number | null | undefined): Tone {
  if (v === null || v === undefined || !Number.isFinite(v) || v === 0) return 'muted'
  return v > 0 ? 'pos' : 'neg'
}

export const LEVEL_TONE: Record<Level | 'muted', Tone> = {
  debug: 'muted',
  info: 'muted',
  success: 'pos',
  warn: 'warn',
  error: 'neg',
  signal: 'accent',
  trade: 'purple',
  learn: 'info',
  scan: 'info',
  system: 'muted',
  muted: 'muted',
}

/** CSS color variable for a tone, e.g. toneColor('pos') -> 'var(--pos)'. */
export function toneColor(t: Tone): string {
  return t === 'muted' ? 'var(--muted)' : `var(--${t})`
}

// ----------------------------------------------------------------------------- numbers

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

export function priceDecimals(price: number): number {
  const a = Math.abs(price)
  if (a >= 10000) return 1
  if (a >= 1000) return 2
  if (a >= 10) return 2
  if (a >= 1) return 4
  if (a >= 0.01) return 5
  return 8
}

export function fmtPrice(v: number | null | undefined, decimals?: number): string {
  if (!isNum(v)) return '—'
  const d = decimals ?? priceDecimals(v)
  return v.toLocaleString('es-ES', { minimumFractionDigits: d, maximumFractionDigits: d })
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (!isNum(v)) return '—'
  return v.toLocaleString('es-ES', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function fmtInt(v: number | null | undefined): string {
  if (!isNum(v)) return '—'
  return Math.round(v).toLocaleString('es-ES')
}

export function fmtCompact(v: number | null | undefined): string {
  if (!isNum(v)) return '—'
  return new Intl.NumberFormat('es-ES', { notation: 'compact', maximumFractionDigits: 1 }).format(v)
}

/** 1.234 -> "+1,23 %" (value already in percent units). */
export function fmtPct(v: number | null | undefined, digits = 2, signed = true): string {
  if (!isNum(v)) return '—'
  const s = v.toLocaleString('es-ES', { minimumFractionDigits: digits, maximumFractionDigits: digits })
  return `${signed && v > 0 ? '+' : ''}${s} %`
}

/** 0.64 -> "64 %" */
export function fmtProb(p: number | null | undefined, digits = 0): string {
  if (!isNum(p)) return '—'
  return `${(p * 100).toLocaleString('es-ES', { maximumFractionDigits: digits, minimumFractionDigits: digits })} %`
}

export function fmtR(r: number | null | undefined, digits = 2): string {
  if (!isNum(r)) return '—'
  const v = Number(r.toFixed(digits)) || 0 // a value that rounds to zero is "0,00R", never "-0,00R"
  return `${v > 0 ? '+' : ''}${v.toLocaleString('es-ES', { maximumFractionDigits: digits, minimumFractionDigits: digits })}R`
}

export function fmtScore(s: number | null | undefined): string {
  if (!isNum(s)) return '—'
  return `${s > 0 ? '+' : ''}${s.toFixed(2)}`
}

export function fmtBytes(n: number | null | undefined): string {
  if (!isNum(n)) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1048576).toFixed(1)} MB`
}

// ----------------------------------------------------------------------------- time

/** Bar time (unix seconds) -> "30/10 14:00" in local time, or the date only for 1d / 1w bars. Those open at 00:00 UTC
 *  (weeks on Monday), so their date is the UTC session date: in UTC-negative zones the local date would be the day
 *  before (same rule as the charts, see lib/chart.ts `ts`). */
export function fmtBarTime(sec: number | null | undefined, tf?: string | null): string {
  if (!isNum(sec)) return '—'
  const d = new Date(sec * 1000)
  if (tf === '1d' || tf === '1w') return d.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit', year: '2-digit', timeZone: 'UTC' })
  return d.toLocaleString('es-ES', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

/** unix ms -> "14:03:21" */
export function fmtClock(ms: number | null | undefined): string {
  if (!isNum(ms)) return '—'
  return new Date(ms).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** unix ms -> "30/10/26 14:03" */
export function fmtDateTime(ms: number | null | undefined): string {
  if (!isNum(ms)) return '—'
  return new Date(ms).toLocaleString('es-ES', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' })
}

/** unix ms -> "hace 5 min" */
export function fmtAgo(ms: number | null | undefined): string {
  if (!isNum(ms)) return '—'
  const s = Math.round((Date.now() - ms) / 1000)
  if (s < 5) return 'ahora'
  if (s < 60) return `hace ${s} s`
  if (s < 3600) return `hace ${Math.round(s / 60)} min`
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`
  return `hace ${Math.round(s / 86400)} d`
}

export function fmtDuration(sec: number | null | undefined): string {
  if (!isNum(sec)) return '—'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h >= 48) return `${Math.round(h / 24)} d`
  if (h > 0) return `${h} h ${m} min`
  return `${m} min`
}

/** "BINANCE:BTCUSDT" -> { exchange: "BINANCE", ticker: "BTCUSDT" } */
export function splitSymbol(symbol: string): { exchange: string; ticker: string } {
  const i = symbol.indexOf(':')
  return i >= 0 ? { exchange: symbol.slice(0, i), ticker: symbol.slice(i + 1) } : { exchange: '', ticker: symbol }
}
