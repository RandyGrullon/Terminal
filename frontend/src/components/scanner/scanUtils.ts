// Scanner helpers: result filters, sorting, defensive normalization of server payloads and run status labels.
import type { NexusAction, ScanRow, ScanRun, Timeframe, Verdict } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { ACTION_LABEL, type Tone } from '../../lib/format'
import { isNum } from '../markets/opsUtils'

// ----------------------------------------------------------------------------- gate vs NEXUS signal
// Deep rows say what NEXUS itself signals (`nexus_action`) and whether the gate TOMAR is a real opportunity
// (`opportunity`: the gate says TOMAR and NEXUS gives that same entry, BUY for long / SELL for short; contract I8).

/** "Oportunidad TOMAR": gate TOMAR backed by the NEXUS signal (`opportunity === true`). Only rows without the field
 *  (runs stored before it existed, or older servers: null / absent) fall back to the gate verdict alone. */
export function isOpportunity(r: ScanRow): boolean {
  const o = r.opportunity
  return typeof o === 'boolean' ? o : r.verdict === 'TAKE'
}

/** Gate TOMAR without a NEXUS entry signal: shown as "Filtro favorable · sin señal NEXUS", never counted as TOMAR. */
export function isFavorableWithoutSignal(r: ScanRow): boolean {
  return r.deep && r.verdict === 'TAKE' && !isOpportunity(r)
}

/** Gate TOMAR from a run stored before the NEXUS signal was checked: shown (and counted) as TOMAR, as that run did. */
export function isLegacyTake(r: ScanRow): boolean {
  return r.deep && r.verdict === 'TAKE' && typeof r.opportunity !== 'boolean'
}

/** Why a favorable row is not an opportunity, naming NEXUS' own action when the server sent it. */
export function favorableNote(r: ScanRow): string {
  const what = r.direction === 'short' ? 'venta' : 'compra'
  const says = r.nexus_action ? ` (dice ${ACTION_LABEL[r.nexus_action] ?? r.nexus_action})` : ''
  return `El filtro aprueba la idea, pero NEXUS no da la señal de ${what} en esa vela${says}: no cuenta como oportunidad TOMAR`
}

// ----------------------------------------------------------------------------- filters

export type ScanFilter = 'all' | 'take' | 'buy' | 'sell' | 'avoid' | 'shallow'

export const SCAN_FILTERS: { id: ScanFilter; label: string; title: string; test: (r: ScanRow) => boolean }[] = [
  { id: 'all', label: 'Todas', title: 'Todos los activos escaneados', test: () => true },
  {
    id: 'take',
    label: 'TOMAR',
    title: 'Oportunidades TOMAR: el filtro (Trade Gate) aprueba y NEXUS da la señal de entrada en esa dirección',
    test: isOpportunity,
  },
  { id: 'buy', label: 'Compras', title: 'Señal rápida de compra', test: (r) => r.action === 'BUY' },
  { id: 'sell', label: 'Ventas', title: 'Señal rápida de venta', test: (r) => r.action === 'SELL' },
  { id: 'avoid', label: 'Evitar', title: 'El filtro dice NO TOMAR', test: (r) => r.verdict === 'AVOID' },
  { id: 'shallow', label: 'Sin análisis profundo', title: 'Solo modelo rápido (sin NEXUS completo ni filtro)', test: (r) => !r.deep },
]

export function filterRows(rows: ScanRow[], filter: ScanFilter, text: string): ScanRow[] {
  const f = SCAN_FILTERS.find((x) => x.id === filter) ?? SCAN_FILTERS[0]
  const q = text.trim().toUpperCase()
  return rows.filter(
    (r) => f.test(r) && (!q || r.symbol.toUpperCase().includes(q) || (r.description ?? '').toUpperCase().includes(q)),
  )
}

export function countByFilter(rows: ScanRow[]): Record<ScanFilter, number> {
  const out = { all: 0, take: 0, buy: 0, sell: 0, avoid: 0, shallow: 0 } as Record<ScanFilter, number>
  for (const r of rows) for (const f of SCAN_FILTERS) if (f.test(r)) out[f.id]++
  return out
}

// ----------------------------------------------------------------------------- sorting

export type SortKey =
  | 'rank'
  | 'symbol'
  | 'price'
  | 'change_pct'
  | 'score'
  | 'prob_up'
  | 'action'
  | 'verdict'
  | 'regime'
  | 'expected_move_pct'
  | 'confidence'

export type SortDir = 'asc' | 'desc'
export interface SortState {
  key: SortKey
  dir: SortDir
}

/** Direction used the first time a column is clicked. */
export const FIRST_DIR: Record<SortKey, SortDir> = {
  rank: 'asc',
  symbol: 'asc',
  price: 'desc',
  change_pct: 'desc',
  score: 'desc',
  prob_up: 'desc',
  action: 'desc',
  verdict: 'desc',
  regime: 'asc',
  expected_move_pct: 'desc',
  confidence: 'desc',
}

const VERDICT_RANK: Record<Verdict, number> = { TAKE: 3, WAIT: 2, AVOID: 1 }
const ACTION_RANK: Record<string, number> = { BUY: 3, WAIT: 2, SELL: 1 }

function sortValue(r: ScanRow, key: SortKey): number | string | null {
  switch (key) {
    case 'symbol':
      return r.symbol
    case 'regime':
      return r.regime || null
    case 'action':
      return ACTION_RANK[r.action] ?? null
    case 'verdict': {
      // TOMAR > filtro favorable sin señal NEXUS > ESPERAR > NO TOMAR, then by gate score inside each group.
      if (!r.verdict) return null
      const rank = isFavorableWithoutSignal(r) ? 2.5 : VERDICT_RANK[r.verdict]
      return rank * 1000 + (isNum(r.gate_score) ? r.gate_score : 0)
    }
    default: {
      const v = r[key]
      return isNum(v) ? v : null
    }
  }
}

/** Stable sort; missing values always go last whatever the direction. */
export function sortRows(rows: ScanRow[], sort: SortState): ScanRow[] {
  const sign = sort.dir === 'asc' ? 1 : -1
  return rows
    .map((r, i) => ({ r, i, v: sortValue(r, sort.key) }))
    .sort((a, b) => {
      if (a.v === null && b.v === null) return a.i - b.i
      if (a.v === null) return 1
      if (b.v === null) return -1
      const c = typeof a.v === 'string' || typeof b.v === 'string' ? String(a.v).localeCompare(String(b.v)) : a.v - b.v
      return c !== 0 ? c * sign : a.i - b.i
    })
    .map((x) => x.r)
}

// ----------------------------------------------------------------------------- payload normalization

const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : d)
const numOrNull = (v: unknown): number | null => (isNum(v) ? v : null)
const VERDICTS: Verdict[] = ['TAKE', 'WAIT', 'AVOID']
const isNexusAction = (v: unknown): v is NexusAction => typeof v === 'string' && Object.prototype.hasOwnProperty.call(ACTION_LABEL, v)

/** Validates one result row coming from the server (drops rows without a symbol, fills safe defaults). */
export function normalizeRow(raw: unknown, index: number): ScanRow | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const symbol = str(o.symbol).trim()
  if (!symbol) return null
  const action = o.action === 'BUY' || o.action === 'SELL' ? o.action : 'WAIT'
  const verdict = VERDICTS.includes(o.verdict as Verdict) ? (o.verdict as Verdict) : null
  const direction = o.direction === 'long' || o.direction === 'short' ? o.direction : null
  return {
    rank: isNum(o.rank) ? o.rank : index + 1,
    symbol,
    description: str(o.description),
    type: str(o.type),
    price: numOrNull(o.price),
    change_pct: numOrNull(o.change_pct),
    volume: numOrNull(o.volume),
    score: isNum(o.score) ? o.score : 0,
    prob_up: isNum(o.prob_up) ? o.prob_up : 0.5,
    action,
    confidence: isNum(o.confidence) ? o.confidence : 0,
    rating: numOrNull(o.rating),
    deep: o.deep === true,
    verdict,
    gate_score: numOrNull(o.gate_score),
    direction,
    regime: typeof o.regime === 'string' && o.regime ? o.regime : null,
    sl: numOrNull(o.sl),
    tp1: numOrNull(o.tp1),
    tp2: numOrNull(o.tp2),
    expected_move_pct: numOrNull(o.expected_move_pct),
    reasons: Array.isArray(o.reasons) ? o.reasons.filter((x): x is string => typeof x === 'string' && x.trim() !== '') : [],
    nexus_action: isNexusAction(o.nexus_action) ? o.nexus_action : null,
    opportunity: typeof o.opportunity === 'boolean' ? o.opportunity : null,
  }
}

export function normalizeRows(raw: unknown): ScanRow[] {
  if (!Array.isArray(raw)) return []
  const out: ScanRow[] = []
  raw.forEach((r, i) => {
    const row = normalizeRow(r, i)
    if (row) out.push(row)
  })
  return out
}

export function normalizeRuns(raw: unknown): ScanRun[] {
  if (!Array.isArray(raw)) return []
  return raw.filter((r): r is ScanRun => !!r && typeof r === 'object' && isNum((r as ScanRun).id))
}

// ----------------------------------------------------------------------------- run lifecycle

/** What to do with the results table when a run finishes: 'refresh' reloads it in place (it is the run on screen,
 *  picked while it was running: its final results replace "sigue en curso"); 'show' switches the table to it; 'offer'
 *  keeps the run the user picked in the history and only announces the new one (banner). Background (automatic) scans
 *  never replace a run the user selected; runs launched from the page, or a table that follows the newest run, switch.
 *  followed=false: a run this page was not following (another scan was), which is only ever refreshed or offered. */
export function onRunFinished(
  run: { id?: number; startedByUser: boolean; followed?: boolean },
  selectedId: number | null,
  shownId: number | null = null,
): 'show' | 'refresh' | 'offer' {
  if (run.id !== undefined && (selectedId ?? shownId) === run.id) return 'refresh'
  if (run.followed === false) return 'offer'
  return run.startedByUser || selectedId === null ? 'show' : 'offer'
}

// ----------------------------------------------------------------------------- labels

export const SCAN_ACTION_LABEL: Record<string, string> = { BUY: 'COMPRAR', SELL: 'VENDER', WAIT: 'ESPERAR' }

export function runStatusInfo(status: string | null | undefined): { label: string; tone: Tone } {
  if (status === 'running') return { label: 'En curso', tone: 'info' }
  if (status === 'done') return { label: 'Completado', tone: 'pos' }
  if (status === 'error') return { label: 'Error', tone: 'neg' }
  return { label: status || '—', tone: 'muted' }
}

export function asTf(v: unknown, fallback: Timeframe): Timeframe {
  return typeof v === 'string' && (TIMEFRAMES as string[]).includes(v) ? (v as Timeframe) : fallback
}

export function clamp01(v: number | null | undefined): number {
  return isNum(v) ? Math.max(0, Math.min(1, v)) : 0
}
