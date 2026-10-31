// Paper-trading ("Cuenta demo") helpers: defensive snapshot parsing, TP hits, NEXUS advice severity, exit labels,
// order validation, 409 gate-conflict parsing and the "gate verdict vs. outcome" breakdown.
import type { Direction, GateResult, PaperAccount, PaperSnapshot, Position, Verdict } from '../../api/types'
import { EXIT_REASON_LABEL, signTone, type Tone } from '../../lib/format'
import { isNum } from '../markets/opsUtils'

// ----------------------------------------------------------------------------- snapshot parsing

function isPosition(v: unknown): v is Position {
  if (!v || typeof v !== 'object') return false
  const o = v as Record<string, unknown>
  return isNum(o.id) && typeof o.symbol === 'string' && (o.side === 'long' || o.side === 'short')
}

export function normalizePositions(raw: unknown): Position[] {
  if (!Array.isArray(raw)) return []
  return raw.filter(isPosition).map((p) => ({ ...p, warnings: Array.isArray(p.warnings) ? p.warnings : [] }))
}

export function isAccount(v: unknown): v is PaperAccount {
  return !!v && typeof v === 'object' && isNum((v as PaperAccount).id) && isNum((v as PaperAccount).balance)
}

export function normalizeSnapshot(raw: unknown): PaperSnapshot | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isAccount(o.account)) return null
  const account = { ...o.account, auto_symbols: Array.isArray(o.account.auto_symbols) ? o.account.auto_symbols : [] }
  return { account, positions: normalizePositions(o.positions), history: normalizePositions(o.history) }
}

// ----------------------------------------------------------------------------- open positions

/** Reached take-profit levels: inferred from partial closes (1/3 at TP1, 1/3 at TP2), the stop moved to
 *  break-even (done after TP1) and the last price versus each level. */
export function tpHits(p: Position): [boolean, boolean, boolean] {
  const s = p.side === 'long' ? 1 : -1
  const closedFrac = isNum(p.qty) && p.qty > 0 && isNum(p.qty_open) ? 1 - p.qty_open / p.qty : 0
  const byQty = closedFrac >= 0.55 ? 2 : closedFrac >= 0.2 ? 1 : 0
  const ref = p.status === 'open' ? p.price : p.exit_price
  const beyond = (lvl: number | null) => isNum(lvl) && isNum(ref) && s * (ref - lvl) >= 0
  const tol = 1e-9 * Math.max(1, Math.abs(p.entry_price))
  const breakEven = isNum(p.sl) && isNum(p.entry_price) && s * (p.sl - p.entry_price) >= -tol
  const h3 = beyond(p.tp3)
  const h2 = h3 || byQty >= 2 || beyond(p.tp2)
  const h1 = h2 || byQty >= 1 || breakEven || beyond(p.tp1)
  return [h1, h2, h3]
}

/** True when the stop already protects the entry (break-even or better). */
export function stopProtects(p: Position): boolean {
  if (!isNum(p.sl) || !isNum(p.entry_price)) return false
  const s = p.side === 'long' ? 1 : -1
  return s * (p.sl - p.entry_price) >= -1e-9 * Math.max(1, Math.abs(p.entry_price))
}

/** Signed distance (in % of price) from the last price to a level. */
export function distPct(level: number | null | undefined, price: number | null | undefined): number | null {
  if (!isNum(level) || !isNum(price) || price === 0) return null
  return ((level - price) / price) * 100
}

export type AdviceLevel = 'close' | 'warn' | 'ok' | 'none'

/** How urgent the live NEXUS advice for a position is. "close" only when the advice itself says to close ("Cerrar
 *  ya: …", the monitor's exit decision): NEXUS' other warnings — a negative EV of holding ("vigila la salida"), a
 *  volatility spike, the market-conditions alert — are cautions while it keeps the position, never "cerrar". */
export function adviceLevel(p: Position): AdviceLevel {
  const warnings = Array.isArray(p.warnings) ? p.warnings : []
  const advice = (p.advice ?? '').trim()
  if (/^(cerrar|salir)/i.test(advice)) return 'close'
  if (warnings.some((w) => w.severity === 'warn' || w.severity === 'critical')) return 'warn'
  return advice ? 'ok' : 'none'
}

export function severityTone(sev: string | null | undefined): Tone {
  return sev === 'critical' ? 'neg' : sev === 'warn' ? 'warn' : 'info'
}

// ----------------------------------------------------------------------------- closed positions

export function exitInfo(p: Position): { label: string; tone: Tone } {
  const reason = (p.exit_reason ?? '').toUpperCase()
  const label = p.exit_label || EXIT_REASON_LABEL[reason] || p.exit_reason || '—'
  let tone: Tone
  if (reason.startsWith('TP')) tone = 'pos'
  else if (reason === 'SL') tone = isNum(p.pnl) && p.pnl >= 0 ? 'warn' : 'neg'
  else if (reason === 'MANUAL') tone = 'info'
  else if (reason === 'EARLY' || reason === 'TIME' || reason === 'SIGNAL' || reason === 'REVERSE') tone = 'warn'
  else tone = signTone(p.pnl)
  return { label, tone }
}

export interface VerdictOutcome {
  verdict: Verdict | null
  n: number
  wins: number
  winRate: number | null
  pnl: number
  avgR: number | null
}

/** Closed trades grouped by the gate verdict they had when opened: shows what the filter is worth. */
export function verdictBreakdown(history: Position[]): VerdictOutcome[] {
  const groups: (Verdict | null)[] = ['TAKE', 'WAIT', 'AVOID', null]
  return groups
    .map((verdict) => {
      const items = history.filter((p) => (p.gate_verdict ?? null) === verdict)
      const rs = items.map((p) => p.r_multiple).filter(isNum)
      const wins = items.filter((p) => isNum(p.pnl) && p.pnl > 0).length
      return {
        verdict,
        n: items.length,
        wins,
        winRate: items.length ? wins / items.length : null,
        pnl: items.reduce((acc, p) => acc + (isNum(p.pnl) ? p.pnl : 0), 0),
        avgR: rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : null,
      }
    })
    .filter((g) => g.n > 0)
}

// ----------------------------------------------------------------------------- orders

/** Validates optional custom SL / TP against the reference entry price. Returns a Spanish message or null. */
export function validateLevels(dir: Direction, ref: number | null, sl: number | null, tp: number | null): string | null {
  if (sl !== null && !(sl > 0)) return 'El stop loss debe ser mayor que 0.'
  if (tp !== null && !(tp > 0)) return 'El objetivo debe ser mayor que 0.'
  if (ref !== null && sl !== null) {
    if (dir === 'long' && sl >= ref) return 'En un largo el stop loss («hasta aquí») debe quedar por debajo del precio de entrada.'
    if (dir === 'short' && sl <= ref) return 'En un corto el stop loss («hasta aquí») debe quedar por encima del precio de entrada.'
  }
  if (ref !== null && tp !== null) {
    if (dir === 'long' && tp <= ref) return 'En un largo el objetivo debe quedar por encima del precio de entrada.'
    if (dir === 'short' && tp >= ref) return 'En un corto el objetivo debe quedar por debajo del precio de entrada.'
  }
  return null
}

/** Parses the 409 body of POST /api/paper/order: {detail: {message, gate}} (or a plain string detail). */
export function conflictDetail(data: unknown): { message: string | null; gate: GateResult | null } {
  const detail = data && typeof data === 'object' ? (data as { detail?: unknown }).detail : null
  if (typeof detail === 'string') return { message: detail, gate: null }
  if (!detail || typeof detail !== 'object') return { message: null, gate: null }
  const o = detail as { message?: unknown; gate?: unknown }
  const gate = o.gate && typeof o.gate === 'object' && 'verdict' in o.gate ? (o.gate as GateResult) : null
  return { message: typeof o.message === 'string' ? o.message : null, gate }
}

export function isGateResult(v: unknown): v is GateResult {
  if (!v || typeof v !== 'object') return false
  const o = v as Record<string, unknown>
  return (o.verdict === 'TAKE' || o.verdict === 'WAIT' || o.verdict === 'AVOID') && (o.direction === 'long' || o.direction === 'short')
}
