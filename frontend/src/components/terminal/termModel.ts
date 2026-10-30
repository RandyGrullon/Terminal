// Terminal line model: entries, categories (level chips), symbol detection, export format and the
// per-viewer persistence (command history + chosen chip) kept in localStorage.
import type { Level, NexusEvent } from '../../api/types'
import { LEVEL_TONE, toneColor } from '../../lib/format'

export type EntryKind = 'event' | 'cmd' | 'out' | 'sys'
export type EntryLevel = Level | 'muted'

export interface TermEntry {
  /** Unique, increasing id (React key + "new lines" counter). */
  id: number
  kind: EntryKind
  /** unix ms */
  ts: number
  /** precomputed HH:MM:SS (local) */
  clock: string
  level: EntryLevel
  source: string
  text: string
  eventId: number | null
  /** Clickable command hints (welcome line). */
  tips?: string[]
}

export const MAX_LINES = 4000

const LEVELS: ReadonlySet<string> = new Set(['debug', 'info', 'success', 'warn', 'error', 'signal', 'trade', 'learn', 'scan', 'system', 'muted'])

export function asLevel(v: unknown): EntryLevel {
  return typeof v === 'string' && LEVELS.has(v) ? (v as EntryLevel) : 'info'
}

let seq = 0
const pad2 = (n: number) => (n < 10 ? `0${n}` : String(n))

export function clockOf(ms: number): string {
  const d = new Date(ms)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

/** Entries are single-line (fixed row height): multi-line text is split into several entries. */
export function makeEntries(
  kind: EntryKind,
  text: string,
  level: EntryLevel,
  opts: { source?: string; ts?: number; eventId?: number | null; tips?: string[] } = {},
): TermEntry[] {
  const ts = opts.ts ?? Date.now()
  const clock = clockOf(ts)
  const parts = String(text ?? '').replace(/\r/g, '').replace(/\t/g, '    ').split('\n')
  return parts.map((part, i) => ({
    id: ++seq,
    kind,
    ts,
    clock,
    level,
    source: opts.source ?? '',
    text: part,
    eventId: i === 0 ? opts.eventId ?? null : null,
    tips: i === 0 ? opts.tips : undefined,
  }))
}

export function eventEntries(ev: NexusEvent): TermEntry[] {
  const ts = typeof ev.ts === 'number' && Number.isFinite(ev.ts) ? ev.ts : Date.now()
  return makeEntries('event', ev.message ?? '', asLevel(ev.level), { source: ev.source ?? '', ts, eventId: ev.id })
}

// ----------------------------------------------------------------------------- categories (level chips)

export type Category = 'all' | 'signal' | 'trade' | 'scan' | 'learn' | 'system' | 'error'

export const CATEGORIES: { id: Category; label: string; title: string }[] = [
  { id: 'all', label: 'Todo', title: 'Todos los eventos (sin depuración) y tus comandos' },
  { id: 'signal', label: 'Señales', title: 'Señales de NEXUS, rechazos del filtro y avisos de salida anticipada' },
  { id: 'trade', label: 'Operaciones', title: 'Aperturas, cierres, TP/SL de la cuenta demo y del simulador' },
  { id: 'scan', label: 'Escáner', title: 'Escaneos de mercado y oportunidades detectadas' },
  { id: 'learn', label: 'Aprendizaje', title: 'Predicciones verificadas y actualizaciones de los modelos' },
  { id: 'system', label: 'Sistema', title: 'Servicios, conexión, ajustes y depuración' },
  { id: 'error', label: 'Errores', title: 'Errores y avisos' },
]

const SIGNAL_SOURCES = new Set(['nexus', 'gate', 'watch', 'watcher'])
const TRADE_SOURCES = new Set(['paper', 'sim', 'simulator'])
const SCAN_SOURCES = new Set(['scanner'])
const LEARN_SOURCES = new Set(['evaluator', 'learning', 'learn'])

export function inCategory(e: TermEntry, cat: Category): boolean {
  if (e.kind !== 'event') return true // command input/output and system notes are always shown
  const src = e.source.toLowerCase()
  switch (cat) {
    case 'all':
      return e.level !== 'debug'
    case 'signal':
      return e.level === 'signal' || (SIGNAL_SOURCES.has(src) && e.level !== 'debug')
    case 'trade':
      return e.level === 'trade' || (TRADE_SOURCES.has(src) && e.level !== 'debug')
    case 'scan':
      return e.level === 'scan' || (SCAN_SOURCES.has(src) && e.level !== 'debug')
    case 'learn':
      return e.level === 'learn' || (LEARN_SOURCES.has(src) && e.level !== 'debug')
    case 'system':
      return e.level === 'system' || e.level === 'debug' || e.level === 'success' || e.level === 'info'
    case 'error':
      return e.level === 'error' || e.level === 'warn'
    default:
      return true
  }
}

export function countCategories(lines: TermEntry[]): Record<Category, number> {
  const out: Record<Category, number> = { all: 0, signal: 0, trade: 0, scan: 0, learn: 0, system: 0, error: 0 }
  for (const e of lines) {
    if (e.kind !== 'event') continue
    for (const c of CATEGORIES) if (inCategory(e, c.id)) out[c.id]++
  }
  return out
}

// ----------------------------------------------------------------------------- presentation

export const LEVEL_TAG: Record<EntryLevel, string> = {
  debug: 'DEPUR',
  info: 'INFO',
  success: 'OK',
  warn: 'AVISO',
  error: 'ERROR',
  signal: 'SEÑAL',
  trade: 'OPER',
  learn: 'APREND',
  scan: 'ESCÁN',
  system: 'SIST',
  muted: '',
}

export function levelColor(level: EntryLevel): string {
  if (level === 'info') return 'var(--text)'
  if (level === 'signal') return 'var(--accent-2)'
  return toneColor(LEVEL_TONE[level] ?? 'muted')
}

/** TradingView-style symbols inside free text ("BINANCE:BTCUSDT", "NASDAQ:AAPL", "SIM:NEXUS"). */
const SYMBOL_RE = /\b[A-Z0-9_]{2,20}:[A-Z0-9._!]{1,30}\b/g
// Uppercase prefixes that look like an exchange but are labels ("SL:1.2", "TP1:3") – never linked.
const NOT_EXCHANGES = new Set(['SL', 'TP', 'TP1', 'TP2', 'TP3', 'RR', 'EV', 'ATR', 'RSI', 'ADX', 'MACD', 'EMA', 'SMA', 'ID', 'URL', 'HTTP', 'HTTPS', 'PNL', 'TF', 'OK', 'NOTA'])

export type TextPart = string | { symbol: string }

export function splitSymbols(text: string): TextPart[] {
  if (!text || text.indexOf(':') < 0) return [text]
  const out: TextPart[] = []
  let last = 0
  for (const m of text.matchAll(SYMBOL_RE)) {
    const sym = m[0]
    const at = m.index ?? 0
    const exchange = sym.slice(0, sym.indexOf(':'))
    if (/^\d+$/.test(exchange) || NOT_EXCHANGES.has(exchange)) continue // clock times ("14:03"), labels
    if (at > last) out.push(text.slice(last, at))
    out.push({ symbol: sym })
    last = at + sym.length
  }
  if (last === 0) return [text]
  if (last < text.length) out.push(text.slice(last))
  return out
}

export function exportLine(e: TermEntry): string {
  if (e.kind === 'event') return `${e.clock} ${(LEVEL_TAG[e.level] || e.level).padEnd(6)} [${e.source}] ${e.text}`
  if (e.kind === 'cmd') return `${e.clock} nexus> ${e.text}`
  return `${e.clock}        ${e.tips ? `${e.text}${e.tips.map((t) => `'${t}'`).join(' · ')}` : e.text}`
}

// ----------------------------------------------------------------------------- persistence (per viewer)

const HISTORY_KEY = 'nexus.term.history.v1'
const PREFS_KEY = 'nexus.term.prefs.v1'
export const HISTORY_MAX = 200

export function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    const arr: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string' && x.trim() !== '').slice(-HISTORY_MAX) : []
  } catch {
    return []
  }
}

export function saveHistory(items: string[]): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(-HISTORY_MAX)))
  } catch {
    /* storage unavailable: history lives only in memory */
  }
}

export function loadCategory(): Category {
  try {
    const raw = localStorage.getItem(PREFS_KEY)
    const v = raw ? (JSON.parse(raw) as { category?: unknown }).category : null
    return CATEGORIES.some((c) => c.id === v) ? (v as Category) : 'all'
  } catch {
    return 'all'
  }
}

export function saveCategory(category: Category): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({ category }))
  } catch {
    /* ignore */
  }
}

export function longestCommonPrefix(items: string[]): string {
  if (!items.length) return ''
  let prefix = items[0]
  for (const s of items) {
    let i = 0
    while (i < prefix.length && i < s.length && prefix[i] === s[i]) i++
    prefix = prefix.slice(0, i)
    if (!prefix) break
  }
  return prefix
}
