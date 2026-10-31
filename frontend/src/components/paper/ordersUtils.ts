// Pending limit / stop orders and portfolio risk of the demo account: pure helpers (no React), shared by the order
// ticket, "¿Tomo esta operación?", the pending-orders table and the risk panel. Mirrors the server's rules so the
// user sees the problem before sending (the server checks everything again).
import type {
  Direction,
  PaperOrder,
  PaperOrderExpiry,
  PaperOrderKind,
  PaperRisk,
  PaperRiskBlock,
  PaperRiskGroup,
} from '../../api/types'
import { fmtPrice, type Tone } from '../../lib/format'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

export const MAX_EXPIRY_BARS = 1000
export const MAX_EXPIRY_DAYS = 365

export const KIND_LABEL: Record<Direction, Record<PaperOrderKind, string>> = {
  long: { limit: 'Compra límite', stop: 'Compra stop' },
  short: { limit: 'Venta límite', stop: 'Venta stop' },
}

/** How each order type behaves, in one line (demo wording). */
export const KIND_HELP: Record<PaperOrderKind, string> = {
  limit: 'Límite: se ejecuta si el precio llega a tu nivel, a ese precio o mejor. En un mercado real tocar el precio no garantiza la ejecución.',
  stop: 'Stop: se activa si el precio cruza tu nivel y entra a mercado (a ese precio o peor, con deslizamiento).',
}

export function kindLabel(side: Direction, kind: PaperOrderKind): string {
  return KIND_LABEL[side][kind]
}

/** The order type that enters at `price` given the market: buying below / selling above it is a limit, buying above /
 *  selling below it is a stop. null when there is no market price or the price is the market itself. */
export function orderKindFor(side: Direction, price: number | null, market: number | null): PaperOrderKind | null {
  if (!isNum(price) || !isNum(market) || price <= 0 || market <= 0 || price === market) return null
  const below = price < market
  return side === 'long' ? (below ? 'limit' : 'stop') : below ? 'stop' : 'limit'
}

/** Spanish problem with a pending order (same rules as the server), or null. `market` null = side not checked yet. */
export function validatePendingOrder(
  side: Direction,
  kind: PaperOrderKind,
  price: number | null,
  market: number | null,
  sl: number | null,
  tp: number | null,
): string | null {
  if (!isNum(price) || price <= 0) return 'Indica el precio de la orden (mayor que 0).'
  if (sl !== null && !(isNum(sl) && sl > 0)) return 'El stop loss debe ser mayor que 0.'
  if (tp !== null && !(isNum(tp) && tp > 0)) return 'El objetivo debe ser mayor que 0.'
  if (isNum(market) && market > 0) {
    const p = fmtPrice(price)
    const m = fmtPrice(market)
    if (kind === 'limit' && side === 'long' && price >= market)
      return `Una compra límite va por debajo del precio actual (${m}): a ${p} se ejecutaría ya. Para comprar si sube hasta ${p}, usa una compra stop.`
    if (kind === 'stop' && side === 'long' && price <= market)
      return `Una compra stop va por encima del precio actual (${m}). Para comprar más barato, a ${p}, usa una compra límite.`
    if (kind === 'limit' && side === 'short' && price <= market)
      return `Una venta límite va por encima del precio actual (${m}): a ${p} se ejecutaría ya. Para vender si cae hasta ${p}, usa una venta stop.`
    if (kind === 'stop' && side === 'short' && price >= market)
      return `Una venta stop va por debajo del precio actual (${m}). Para vender más caro, a ${p}, usa una venta límite.`
  }
  if (sl !== null && (side === 'long' ? sl >= price : sl <= price))
    return `En un ${side === 'long' ? 'largo' : 'corto'} el stop loss («hasta aquí») debe quedar ${side === 'long' ? 'por debajo' : 'por encima'} del precio de la orden.`
  if (tp !== null && (side === 'long' ? tp <= price : tp >= price))
    return `En un ${side === 'long' ? 'largo' : 'corto'} el objetivo debe quedar ${side === 'long' ? 'por encima' : 'por debajo'} del precio de la orden.`
  return null
}

// ----------------------------------------------------------------------------- expiry

export interface ExpiryDraft {
  mode: PaperOrderExpiry
  bars: number | null
  /** value of an <input type="datetime-local"> (the browser's local time), e.g. "2026-11-02T14:30" */
  date: string
}

export const DEFAULT_EXPIRY: ExpiryDraft = { mode: 'gtc', bars: 3, date: '' }

/** "2026-11-02T14:30" (local time) -> unix seconds, or null when it is not a valid date. */
export function localInputToUnix(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value)) return null
  const ms = new Date(value).getTime()
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null
}

/** unix seconds -> "2026-11-02T14:30" in local time (for the datetime-local input). */
export function unixToLocalInput(sec: number): string {
  const d = new Date(sec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** Request fields for the expiry, or a Spanish problem. */
export function expiryRequest(
  draft: ExpiryDraft,
  nowSec: number,
): { ok: true; fields: { expiry: PaperOrderExpiry; expire_bars?: number; expire_time?: number } } | { ok: false; problem: string } {
  if (draft.mode === 'gtc') return { ok: true, fields: { expiry: 'gtc' } }
  if (draft.mode === 'bars') {
    const n = draft.bars
    if (!isNum(n) || !Number.isInteger(n) || n < 1 || n > MAX_EXPIRY_BARS)
      return { ok: false, problem: `Indica cuántas velas es válida la orden: un número entero de 1 a ${MAX_EXPIRY_BARS}.` }
    return { ok: true, fields: { expiry: 'bars', expire_bars: n } }
  }
  const t = localInputToUnix(draft.date)
  if (t === null) return { ok: false, problem: 'Elige la fecha y hora de caducidad.' }
  if (t <= nowSec + 60) return { ok: false, problem: 'La fecha de caducidad debe estar en el futuro (al menos dentro de 1 minuto).' }
  if (t > nowSec + MAX_EXPIRY_DAYS * 86400) return { ok: false, problem: `La caducidad puede estar como mucho a ${MAX_EXPIRY_DAYS} días.` }
  return { ok: true, fields: { expiry: 'date', expire_time: t } }
}

// ----------------------------------------------------------------------------- parsing

export function isPaperOrder(v: unknown): v is PaperOrder {
  if (!v || typeof v !== 'object') return false
  const o = v as Record<string, unknown>
  return (
    isNum(o.id) &&
    typeof o.symbol === 'string' &&
    (o.side === 'long' || o.side === 'short') &&
    (o.kind === 'limit' || o.kind === 'stop') &&
    isNum(o.price)
  )
}

export function normalizeOrders(raw: unknown): PaperOrder[] {
  return Array.isArray(raw) ? raw.filter(isPaperOrder) : []
}

export function normalizeRisk(raw: unknown): PaperRisk | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.account_id) || typeof o.blocked !== 'boolean' || !isNum(o.open_risk)) return null
  const groups = Array.isArray(o.groups)
    ? (o.groups as unknown[]).filter(
        (g): g is PaperRiskGroup => !!g && typeof g === 'object' && typeof (g as PaperRiskGroup).group === 'string' && isNum((g as PaperRiskGroup).positions),
      )
    : []
  return { ...(o as unknown as PaperRisk), groups }
}

/** The 409 detail of a portfolio-risk block ({message, code: 'risk_limit', rule, risk}), or null (e.g. a gate 409). */
export function riskBlockDetail(data: unknown): PaperRiskBlock | null {
  const detail = data && typeof data === 'object' ? (data as { detail?: unknown }).detail : null
  if (!detail || typeof detail !== 'object') return null
  const o = detail as Record<string, unknown>
  if (o.code !== 'risk_limit' || typeof o.message !== 'string') return null
  const rule = o.rule === 'daily_loss' || o.rule === 'open_risk' || o.rule === 'group' ? o.rule : 'open_risk'
  return { message: o.message, code: 'risk_limit', rule, risk: normalizeRisk(o.risk) }
}

// ----------------------------------------------------------------------------- display

export const CANCELLED_BY_LABEL: Record<string, string> = {
  user: 'por ti',
  gate: 'por el filtro',
  risk: 'por el riesgo de cartera',
  levels: 'por los niveles',
  system: 'por el sistema',
}

export function orderStatusTone(o: PaperOrder): Tone {
  if (o.status === 'filled') return 'pos'
  if (o.status === 'pending') return o.executing ? 'warn' : 'accent'
  if (o.status === 'expired') return 'muted'
  return o.cancelled_by === 'user' ? 'muted' : 'warn'
}

/** "a 2,1 % del precio" style distance from the market to the order level (null without a live price). */
export function distanceText(o: PaperOrder): string | null {
  if (!isNum(o.distance_pct)) return null
  const d = Math.abs(o.distance_pct)
  return `${d.toLocaleString('es-ES', { maximumFractionDigits: d < 1 ? 2 : 1 })} % ${o.distance_pct < 0 ? 'por debajo' : 'por encima'}`
}

/** Fraction 0..1 of a limit used (null when the limit is off). */
export function usedFraction(value: number | null | undefined, limit: number | null | undefined): number | null {
  if (!isNum(limit) || limit <= 0 || !isNum(value)) return null
  return Math.max(0, Math.min(1, value / limit))
}

/** Tone of a used fraction: neg when reached, warn from 80 %. */
export function usageTone(frac: number | null): Tone {
  if (frac === null) return 'muted'
  return frac >= 1 - 1e-9 ? 'neg' : frac >= 0.8 ? 'warn' : 'pos'
}

/** Fraction of the daily loss limit consumed by the day's worst realized P&L (gains consume nothing). */
export function dailyUse(risk: PaperRisk): number | null {
  if (!isNum(risk.daily_loss_limit) || risk.daily_loss_limit <= 0) return null
  const loss = Math.max(0, -(isNum(risk.daily_low) ? Math.min(risk.daily_low, risk.daily_pnl) : risk.daily_pnl))
  return Math.max(0, Math.min(1, loss / risk.daily_loss_limit))
}

/** The day's worst realized loss as money for display: negative, or exactly 0 without a loss (never -0, which
 *  formats as "-0,00 $" and reads as a loss). */
export function dailyLossMoney(risk: PaperRisk): number {
  const worst = isNum(risk.daily_low) ? Math.min(risk.daily_low, risk.daily_pnl) : risk.daily_pnl
  const loss = isNum(worst) ? Math.max(0, -worst) : 0
  return loss > 0 ? -loss : 0
}
