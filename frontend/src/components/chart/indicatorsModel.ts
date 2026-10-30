// User indicators on the main chart: catalog (cached for the session), the active list persisted in the
// settings key "ui.chart_indicators", parameter normalization and incremental computation.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, errorMessage } from '../../api/client'
import type { IndicatorParam, IndicatorRequestItem, IndicatorSpec, Timeframe } from '../../api/types'
import { useApp } from '../../store'
import { isAbortError, type ParamMap, type ParamValue } from './chartUtils'

export const SETTINGS_KEY = 'ui.chart_indicators'
/** Generous cap: every catalog indicator can be added (several times), but the chart must stay usable. */
export const MAX_INDICATORS = 40

export const CATEGORY_ORDER = ['Tendencia', 'Momento', 'Volatilidad', 'Volumen', 'Soportes/Resistencias', 'Estadística', 'NEXUS']

export const SOURCE_OPTIONS = ['close', 'open', 'high', 'low', 'hl2', 'hlc3', 'ohlc4', 'volume']
export const SOURCE_LABEL: Record<string, string> = {
  close: 'Cierre',
  open: 'Apertura',
  high: 'Máximo',
  low: 'Mínimo',
  hl2: '(Máx + Mín) / 2',
  hlc3: '(Máx + Mín + Cierre) / 3',
  ohlc4: '(A + Máx + Mín + C) / 4',
  hlcc4: '(Máx + Mín + 2·Cierre) / 4',
  volume: 'Volumen',
}

export interface ActiveIndicator {
  uid: string
  id: string
  params: ParamMap
}

export interface IndicatorResultEntry {
  /** id + normalized params the values were computed with */
  key: string
  /** `${symbol}|${tf}` */
  scope: string
  /** scope + data/bar versions: older versions are recomputed */
  ver: string
  time: number[]
  outputs: Record<string, (number | null)[]>
  error: string | null
}

export type IndicatorStatus = 'ok' | 'pending' | 'error' | 'unknown'

export interface ResolvedIndicator {
  uid: string
  id: string
  spec: IndicatorSpec | null
  params: ParamMap
  /** Result for the current symbol/tf (may be from the previous bar while recomputing). */
  result: IndicatorResultEntry | null
  status: IndicatorStatus
  error: string | null
}

// ----------------------------------------------------------------------------- catalog

let catalogCache: IndicatorSpec[] | null = null
let catalogPromise: Promise<IndicatorSpec[]> | null = null

function validSpec(s: unknown): s is IndicatorSpec {
  if (!s || typeof s !== 'object') return false
  const o = s as Record<string, unknown>
  return typeof o.id === 'string' && typeof o.name === 'string' && Array.isArray(o.params) && Array.isArray(o.outputs)
}

export function loadCatalog(force = false): Promise<IndicatorSpec[]> {
  if (catalogCache && !force) return Promise.resolve(catalogCache)
  if (catalogPromise && !force) return catalogPromise
  const p = api.indicators
    .catalog()
    .then((res) => {
      const list = Array.isArray(res?.indicators) ? res.indicators.filter(validSpec) : []
      catalogCache = list.map((s) => ({
        ...s,
        category: typeof s.category === 'string' && s.category ? s.category : 'Otros',
        description: typeof s.description === 'string' ? s.description : '',
        overlay: !!s.overlay,
        params: s.params.filter((pp) => pp && typeof pp.name === 'string'),
        outputs: s.outputs.filter((o) => o && typeof o.key === 'string'),
      }))
      return catalogCache
    })
    .finally(() => {
      if (catalogPromise === p) catalogPromise = null
    })
  catalogPromise = p
  return p
}

// ----------------------------------------------------------------------------- params

export function newUid(): string {
  return `i${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`
}

export function paramOptions(p: IndicatorParam): string[] {
  if (Array.isArray(p.options) && p.options.length) return p.options.map(String)
  return p.type === 'source' ? SOURCE_OPTIONS : []
}

function coerceParam(p: IndicatorParam, raw: unknown): ParamValue {
  const def = p.default
  switch (p.type) {
    case 'int':
    case 'float': {
      const fallback = typeof def === 'number' && Number.isFinite(def) ? def : 0
      let v = typeof raw === 'number' ? raw : typeof raw === 'string' && raw.trim() !== '' ? Number(raw) : Number.NaN
      if (!Number.isFinite(v)) v = fallback
      if (p.type === 'int') v = Math.round(v)
      if (typeof p.min === 'number') v = Math.max(p.min, v)
      if (typeof p.max === 'number') v = Math.min(p.max, v)
      return v
    }
    case 'bool':
      return typeof raw === 'boolean' ? raw : typeof def === 'boolean' ? def : false
    default: {
      const opts = paramOptions(p)
      const v = typeof raw === 'string' ? raw : typeof raw === 'number' ? String(raw) : null
      if (v !== null && (opts.length === 0 || opts.includes(v))) return v
      return typeof def === 'string' ? def : (opts[0] ?? '')
    }
  }
}

export function defaultParams(spec: IndicatorSpec): ParamMap {
  const out: ParamMap = {}
  for (const p of spec.params) out[p.name] = coerceParam(p, p.default)
  return out
}

/** Keeps only the spec's params, coerced to their types and clamped to min/max. */
export function normalizeParams(spec: IndicatorSpec | null, params: ParamMap): ParamMap {
  if (!spec) return params
  const out: ParamMap = {}
  for (const p of spec.params) out[p.name] = coerceParam(p, params[p.name])
  return out
}

function stableParams(params: ParamMap): string {
  return JSON.stringify(
    Object.keys(params)
      .sort()
      .map((k) => [k, params[k]]),
  )
}

export function itemKey(id: string, params: ParamMap): string {
  return `${id}|${stableParams(params)}`
}

/** "RSI 14 close"-like summary of the parameters (booleans omitted). */
export function paramsSummary(spec: IndicatorSpec | null, params: ParamMap): string {
  const values = spec
    ? spec.params.filter((p) => p.type !== 'bool').map((p) => params[p.name])
    : Object.values(params).filter((v) => typeof v !== 'boolean')
  return values
    .filter((v) => v !== undefined && v !== '')
    .map((v) => (typeof v === 'number' ? v.toLocaleString('es-ES', { maximumFractionDigits: 4 }) : String(v)))
    .join(' ')
}

function parseStored(value: unknown): ActiveIndicator[] {
  if (!Array.isArray(value)) return []
  const out: ActiveIndicator[] = []
  const seen = new Set<string>()
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') continue
    const o = raw as Record<string, unknown>
    if (typeof o.id !== 'string' || !o.id) continue
    let uid = typeof o.uid === 'string' && o.uid ? o.uid : newUid()
    if (seen.has(uid)) uid = newUid()
    seen.add(uid)
    const params: ParamMap = {}
    if (o.params && typeof o.params === 'object') {
      for (const [k, v] of Object.entries(o.params as Record<string, unknown>)) {
        if (typeof v === 'number' || typeof v === 'boolean' || typeof v === 'string') params[k] = v
      }
    }
    out.push({ uid, id: o.id, params })
    if (out.length >= MAX_INDICATORS) break
  }
  return out
}

// ----------------------------------------------------------------------------- hook

export interface IndicatorsState {
  catalog: IndicatorSpec[] | null
  catalogError: unknown
  reloadCatalog: () => void
  /** Settings loaded (or failed): the list is authoritative from now on. */
  loaded: boolean
  items: ResolvedIndicator[]
  computing: boolean
  add: (spec: IndicatorSpec) => void
  update: (uid: string, params: ParamMap) => void
  reset: (uid: string) => void
  remove: (uid: string) => void
  clear: () => void
  retry: (uid: string) => void
}

export function useIndicators(scope: {
  symbol: string
  tf: Timeframe
  /** Candles of the current symbol/tf are loaded (compute only then). */
  ready: boolean
  dataVersion: number
  barVersion: number
}): IndicatorsState {
  const { symbol, tf, ready, dataVersion, barVersion } = scope
  const notify = useApp((s) => s.notify)
  const [catalog, setCatalog] = useState<IndicatorSpec[] | null>(catalogCache)
  const [catalogError, setCatalogError] = useState<unknown>(null)
  const [catalogNonce, setCatalogNonce] = useState(0)
  const [active, setActive] = useState<ActiveIndicator[]>([])
  const [loaded, setLoaded] = useState(false)
  const [results, setResults] = useState<Record<string, IndicatorResultEntry>>({})
  const [computing, setComputing] = useState(false)
  const [retryNonce, setRetryNonce] = useState(0)

  const dirtyRef = useRef(false)
  const pendingSaveRef = useRef<ActiveIndicator[] | null>(null)
  const saveWarnedRef = useRef(false)
  const resultsRef = useRef(results)
  resultsRef.current = results
  const activeRef = useRef(active)
  activeRef.current = active
  const runSeqRef = useRef(0)

  // --- catalog
  useEffect(() => {
    let alive = true
    setCatalogError(null)
    loadCatalog(catalogNonce > 0)
      .then((list) => {
        if (alive) setCatalog(list)
      })
      .catch((err) => {
        if (alive) setCatalogError(err)
      })
    return () => {
      alive = false
    }
  }, [catalogNonce])

  // --- stored list
  useEffect(() => {
    let alive = true
    api.settings
      .get()
      .then(({ settings }) => {
        if (!alive) return
        const stored = parseStored(settings?.[SETTINGS_KEY])
        setActive((cur) => {
          if (!dirtyRef.current) return stored
          const have = new Set(cur.map((c) => c.uid))
          return [...stored.filter((s) => !have.has(s.uid)), ...cur].slice(0, MAX_INDICATORS)
        })
      })
      .catch(() => {
        /* backend unavailable: start with an empty list, saving resumes on the next change */
      })
      .finally(() => {
        if (alive) setLoaded(true)
      })
    return () => {
      alive = false
    }
  }, [])

  // --- debounced persistence
  useEffect(() => {
    if (!loaded || !dirtyRef.current) return
    pendingSaveRef.current = active
    const timer = window.setTimeout(() => {
      const list = pendingSaveRef.current
      pendingSaveRef.current = null
      if (!list) return
      api.settings.update({ [SETTINGS_KEY]: list }).catch((err) => {
        if (!saveWarnedRef.current) {
          saveWarnedRef.current = true
          notify(`No se pudo guardar la lista de indicadores: ${errorMessage(err)}`, 'warn')
        }
      })
    }, 900)
    return () => window.clearTimeout(timer)
  }, [active, loaded, notify])

  useEffect(
    () => () => {
      const list = pendingSaveRef.current
      if (list) api.settings.update({ [SETTINGS_KEY]: list }).catch(() => undefined)
    },
    [],
  )

  // --- resolution against the catalog
  const specById = useMemo(() => new Map((catalog ?? []).map((s) => [s.id, s])), [catalog])
  const scopeKey = `${symbol}|${tf}`
  const ver = `${scopeKey}|${dataVersion}|${barVersion}`

  const requests = useMemo(() => {
    if (!catalog) return [] as IndicatorRequestItem[]
    return active
      .filter((a) => specById.has(a.id))
      .map((a) => ({ uid: a.uid, id: a.id, params: normalizeParams(specById.get(a.id) ?? null, a.params) }))
  }, [active, catalog, specById])
  const requestsKey = requests.map((r) => `${r.uid}=${itemKey(r.id, r.params)}`).join(';')

  // --- incremental compute: only items whose (id, params) or data version changed
  useEffect(() => {
    if (!ready || dataVersion === 0 || requests.length === 0) return
    const stale = requests.filter((r) => {
      const prev = resultsRef.current[r.uid]
      return !prev || prev.ver !== ver || prev.key !== itemKey(r.id, r.params)
    })
    if (stale.length === 0) return
    const ctrl = new AbortController()
    const timer = window.setTimeout(async () => {
      const run = ++runSeqRef.current
      setComputing(true)
      try {
        const res = await api.indicators.compute({ symbol, tf, limit: 1000, items: stale }, ctrl.signal)
        if (ctrl.signal.aborted) return
        const time = Array.isArray(res?.time) ? res.time : []
        const byUid = new Map((Array.isArray(res?.results) ? res.results : []).map((r) => [r.uid, r]))
        setResults((prev) => {
          const next = { ...prev }
          for (const it of stale) {
            const r = byUid.get(it.uid)
            next[it.uid] = {
              key: itemKey(it.id, it.params),
              scope: scopeKey,
              ver,
              time,
              outputs: r && r.outputs && typeof r.outputs === 'object' ? r.outputs : {},
              error: r ? (r.error ?? null) : 'El servidor no devolvió resultados para este indicador',
            }
          }
          return next
        })
      } catch (err) {
        if (isAbortError(err) || ctrl.signal.aborted) return
        const msg = errorMessage(err)
        setResults((prev) => {
          const next = { ...prev }
          for (const it of stale) {
            next[it.uid] = { key: itemKey(it.id, it.params), scope: scopeKey, ver, time: [], outputs: {}, error: msg }
          }
          return next
        })
      } finally {
        if (runSeqRef.current === run) setComputing(false)
      }
    }, 180)
    return () => {
      window.clearTimeout(timer)
      ctrl.abort()
    }
    // `requests` is represented by `requestsKey`; results are read through a ref to avoid loops.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, dataVersion, ver, requestsKey, retryNonce, symbol, tf, scopeKey])

  // An aborted run leaves `computing` on when nothing else is stale: reset it when the inputs settle.
  useEffect(() => {
    if (requests.length === 0 || !ready) setComputing(false)
  }, [requests.length, ready])

  const items = useMemo<ResolvedIndicator[]>(
    () =>
      active.map((a) => {
        const spec = specById.get(a.id) ?? null
        const params = normalizeParams(spec, a.params)
        const raw = results[a.uid]
        const result = raw && raw.scope === scopeKey ? raw : null
        let status: IndicatorStatus
        let error: string | null = null
        if (!spec) {
          if (catalog) {
            status = 'unknown'
            error = 'Este indicador ya no existe en el catálogo'
          } else if (catalogError) {
            status = 'error'
            error = `Catálogo no disponible: ${errorMessage(catalogError)}`
          } else {
            status = 'pending'
          }
        } else if (!result || result.key !== itemKey(a.id, params)) {
          status = result && !result.error ? 'ok' : 'pending'
        } else if (result.error) {
          status = 'error'
          error = result.error
        } else {
          status = 'ok'
        }
        return { uid: a.uid, id: a.id, spec, params, result, status, error }
      }),
    [active, specById, results, scopeKey, catalog, catalogError],
  )

  const mutate = useCallback((fn: (cur: ActiveIndicator[]) => ActiveIndicator[]) => {
    dirtyRef.current = true
    setActive(fn)
  }, [])

  const add = useCallback(
    (spec: IndicatorSpec) => {
      if (activeRef.current.length >= MAX_INDICATORS) {
        notify(`Máximo ${MAX_INDICATORS} indicadores a la vez`, 'warn')
        return
      }
      mutate((cur) => (cur.length >= MAX_INDICATORS ? cur : [...cur, { uid: newUid(), id: spec.id, params: defaultParams(spec) }]))
    },
    [mutate, notify],
  )

  const update = useCallback(
    (uid: string, params: ParamMap) => mutate((cur) => cur.map((a) => (a.uid === uid ? { ...a, params: { ...a.params, ...params } } : a))),
    [mutate],
  )

  const reset = useCallback(
    (uid: string) =>
      mutate((cur) =>
        cur.map((a) => {
          if (a.uid !== uid) return a
          const spec = catalogCache?.find((s) => s.id === a.id)
          return spec ? { ...a, params: defaultParams(spec) } : a
        }),
      ),
    [mutate],
  )

  const remove = useCallback(
    (uid: string) => {
      mutate((cur) => cur.filter((a) => a.uid !== uid))
      setResults((prev) => {
        if (!(uid in prev)) return prev
        const next = { ...prev }
        delete next[uid]
        return next
      })
    },
    [mutate],
  )

  const clear = useCallback(() => {
    mutate(() => [])
    setResults({})
  }, [mutate])

  const retry = useCallback((uid: string) => {
    setResults((prev) => {
      if (!(uid in prev)) return prev
      const next = { ...prev }
      delete next[uid]
      return next
    })
    setRetryNonce((n) => n + 1)
  }, [])

  const reloadCatalog = useCallback(() => setCatalogNonce((n) => n + 1), [])

  return { catalog, catalogError, reloadCatalog, loaded, items, computing, add, update, reset, remove, clear, retry }
}
