// Shared helpers for the Markets, Scanner and Paper pages (FE-OPS): market list, labels, rates and hooks.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorMessage } from '../../api/client'
import type { MarketInfo, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { fmtPct, type Tone } from '../../lib/format'

export const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

// ----------------------------------------------------------------------------- rates & labels

/** Rates may arrive as fractions (0..1) or percentages (0..100): returns 0..1. */
export function rate01(v: number | null | undefined): number | null {
  if (!isNum(v)) return null
  return Math.abs(v) > 1.000001 ? v / 100 : v
}

export function fmtRate(v: number | null | undefined, digits = 1): string {
  const r = rate01(v)
  return r === null ? '—' : fmtPct(r * 100, digits, false)
}

/** Hit-rate tone: green when clearly better than a coin flip, red when worse. */
export function rateTone(v: number | null | undefined, good = 0.55, bad = 0.5): Tone {
  const r = rate01(v)
  if (r === null) return 'muted'
  if (r >= good) return 'pos'
  if (r < bad) return 'neg'
  return 'warn'
}

/** Brier score: 0 = perfect, 0.25 = coin flip. */
export function brierTone(b: number | null | undefined): Tone {
  if (!isNum(b)) return 'muted'
  if (b <= 0.235) return 'pos'
  if (b > 0.255) return 'neg'
  return 'warn'
}

/** TradingView technical rating (Recommend.All, −1..1) → Spanish label. */
export function ratingInfo(r: number | null | undefined): { label: string; tone: Tone } {
  if (!isNum(r)) return { label: '—', tone: 'muted' }
  if (r >= 0.5) return { label: 'Compra fuerte', tone: 'pos' }
  if (r >= 0.1) return { label: 'Compra', tone: 'pos' }
  if (r > -0.1) return { label: 'Neutral', tone: 'muted' }
  if (r > -0.5) return { label: 'Venta', tone: 'neg' }
  return { label: 'Venta fuerte', tone: 'neg' }
}

/** P(up) tone: green ≥ 55 %, red ≤ 45 %. */
export function probTone(p: number | null | undefined): Tone {
  if (!isNum(p)) return 'muted'
  if (p >= 0.55) return 'pos'
  if (p <= 0.45) return 'neg'
  return 'muted'
}

export const TYPE_LABEL: Record<string, string> = {
  stock: 'acción',
  fund: 'fondo',
  dr: 'DR',
  index: 'índice',
  futures: 'futuro',
  forex: 'forex',
  spot: 'cripto',
  crypto: 'cripto',
  swap: 'perpetuo',
  cfd: 'CFD',
  bond: 'bono',
  structured: 'estructurado',
  economic: 'economía',
  commodity: 'materia prima',
}

export function typeLabel(t: string | null | undefined): string {
  if (!t) return ''
  return TYPE_LABEL[t.toLowerCase()] ?? t
}

export const ORIGIN_LABEL: Record<string, string> = { manual: 'Manual', auto: 'Auto', sim: 'Simulación', script: 'Script' }

export function asTimeframe(tf: string | null | undefined): Timeframe | undefined {
  return tf && (TIMEFRAMES as string[]).includes(tf) ? (tf as Timeframe) : undefined
}

/** Signed money amount: "+1.234,56 $". */
export function fmtMoney(v: number | null | undefined, signed = false): string {
  if (!isNum(v)) return '—'
  const s = v.toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return `${signed && v > 0 ? '+' : ''}${s} $`
}

/** Quantities can be tiny (crypto) or large (stocks): adaptive decimals. */
export function fmtQty(v: number | null | undefined): string {
  if (!isNum(v)) return '—'
  const a = Math.abs(v)
  const d = a >= 1000 ? 0 : a >= 1 ? 3 : a >= 0.01 ? 4 : 6
  return v.toLocaleString('es-ES', { minimumFractionDigits: 0, maximumFractionDigits: d })
}

// ----------------------------------------------------------------------------- markets

export const MAIN_MARKETS: { id: string; label: string }[] = [
  { id: 'global', label: 'Global' },
  { id: 'america', label: 'EE. UU.' },
  { id: 'crypto', label: 'Cripto' },
  { id: 'forex', label: 'Forex' },
  { id: 'futures', label: 'Futuros' },
  { id: 'cfd', label: 'CFD' },
  { id: 'bond', label: 'Bonos' },
]
const MAIN_IDS = new Set(MAIN_MARKETS.map((m) => m.id))

export const isMainMarket = (id: string) => MAIN_IDS.has(id)

export const FALLBACK_MARKETS: MarketInfo[] = MAIN_MARKETS.map((m) => ({ id: m.id, name: m.label, count: null }))

export function marketLabel(id: string, markets: MarketInfo[]): string {
  const main = MAIN_MARKETS.find((m) => m.id === id)
  if (main) return main.label
  const found = markets.find((m) => m.id === id)
  if (found?.name) return found.name
  return id ? id.charAt(0).toUpperCase() + id.slice(1) : '—'
}

const MARKETS_TTL_MS = 10 * 60 * 1000
let marketsCache: { at: number; markets: MarketInfo[] } | null = null
let marketsInflight: Promise<MarketInfo[]> | null = null

function fetchMarkets(force: boolean): Promise<MarketInfo[]> {
  if (!force && marketsCache && Date.now() - marketsCache.at < MARKETS_TTL_MS) return Promise.resolve(marketsCache.markets)
  if (marketsInflight) return marketsInflight
  marketsInflight = api
    .markets()
    .then((res) => {
      const list = (Array.isArray(res?.markets) ? res.markets : []).filter(
        (m): m is MarketInfo => !!m && typeof m.id === 'string' && m.id !== '',
      )
      if (!list.length) throw new Error('El servidor no devolvió ningún mercado')
      marketsCache = { at: Date.now(), markets: list }
      return list
    })
    .finally(() => {
      marketsInflight = null
    })
  return marketsInflight
}

/** TradingView markets (cached 10 min). Falls back to the main markets when the endpoint fails. */
export function useMarkets() {
  const [markets, setMarkets] = useState<MarketInfo[]>(marketsCache?.markets ?? FALLBACK_MARKETS)
  const [loading, setLoading] = useState(!marketsCache)
  const [error, setError] = useState<string | null>(null)
  const alive = useRef(true)

  const load = useCallback((force = false) => {
    setLoading(true)
    fetchMarkets(force)
      .then((list) => {
        if (!alive.current) return
        setMarkets(list)
        setError(null)
      })
      .catch((err: unknown) => {
        if (!alive.current) return
        setError(errorMessage(err))
      })
      .finally(() => {
        if (alive.current) setLoading(false)
      })
  }, [])

  useEffect(() => {
    alive.current = true
    load(false)
    return () => {
      alive.current = false
    }
  }, [load])

  return { markets, loading, error, reload: () => load(true) }
}

// ----------------------------------------------------------------------------- hooks

/** setInterval with the latest callback; `ms = null` disables it. */
export function useInterval(fn: () => void, ms: number | null): void {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    if (ms === null) return
    const id = window.setInterval(() => ref.current(), ms)
    return () => window.clearInterval(id)
  }, [ms])
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(id)
  }, [value, ms])
  return v
}

/** Per-viewer UI preference mirrored to localStorage (wrapped in try/catch; falls back to `initial`). */
export function usePersistentState<T>(key: string, initial: T, validate: (v: unknown) => v is T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key)
      if (raw !== null) {
        const parsed: unknown = JSON.parse(raw)
        if (validate(parsed)) return parsed
      }
    } catch {
      /* storage unavailable or corrupt */
    }
    return initial
  })
  const set = useCallback(
    (v: T) => {
      setValue(v)
      try {
        localStorage.setItem(key, JSON.stringify(v))
      } catch {
        /* storage unavailable */
      }
    },
    [key],
  )
  return [value, set]
}

export const isString = (v: unknown): v is string => typeof v === 'string'
export const isBool = (v: unknown): v is boolean => typeof v === 'boolean'
