// "Mercados": every TradingView asset (≈458k) by market, with server-side search, filters and sorting,
// TradingView technicals and the NEXUS quick read. Row click opens the chart; ★ adds to the watchlist.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Globe, RefreshCw, Search, X } from 'lucide-react'
import { api, errorMessage } from '../api/client'
import type { UniverseResponse, UniverseRow } from '../api/types'
import { fmtAgo, fmtCompact, fmtInt } from '../lib/format'
import { useApp } from '../store'
import { Button, Card, Empty, ErrorBox, Loading, Select, Spinner, Tabs, Toggle } from '../components/ui'
import UniverseTable, { SORT_KEYS, type SortKey } from '../components/markets/UniverseTable'
import {
  MAIN_MARKETS,
  isBool,
  isMainMarket,
  isNum,
  isString,
  marketLabel,
  useDebounced,
  useInterval,
  useMarkets,
  usePersistentState,
} from '../components/markets/opsUtils'
import '../components/markets/markets.css'

const PAGE_SIZE = 100
const AUTO_REFRESH_MS = 60000

const TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Todos los tipos' },
  { value: 'stock', label: 'Acciones' },
  { value: 'fund', label: 'Fondos / ETF' },
  { value: 'dr', label: 'Recibos (DR)' },
  { value: 'index', label: 'Índices' },
  { value: 'futures', label: 'Futuros' },
  { value: 'forex', label: 'Divisas' },
  { value: 'spot', label: 'Cripto (spot)' },
  { value: 'swap', label: 'Cripto perpetuos' },
  { value: 'cfd', label: 'CFD' },
  { value: 'bond', label: 'Bonos' },
  { value: 'structured', label: 'Estructurados' },
]

interface MarketsPrefs {
  market: string
  sort: SortKey
  order: 'asc' | 'desc'
  type: string
}

const DEFAULT_PREFS: MarketsPrefs = { market: 'crypto', sort: 'volume', order: 'desc', type: '' }

function isPrefs(v: unknown): v is MarketsPrefs {
  if (!v || typeof v !== 'object') return false
  const p = v as Record<string, unknown>
  return (
    isString(p.market) &&
    /^[a-z0-9_]{2,32}$/.test(p.market) &&
    SORT_KEYS.includes(p.sort as SortKey) &&
    (p.order === 'asc' || p.order === 'desc') &&
    isString(p.type)
  )
}

function dedupe(rows: UniverseRow[]): UniverseRow[] {
  const seen = new Set<string>()
  const out: UniverseRow[] = []
  for (const r of rows) {
    if (!r || typeof r.symbol !== 'string' || seen.has(r.symbol)) continue
    seen.add(r.symbol)
    out.push(r)
  }
  return out
}

export default function MarketsPage() {
  const notify = useApp((s) => s.notify)
  const openChart = useApp((s) => s.openChart)
  const { markets, error: marketsError, reload: reloadMarkets } = useMarkets()

  const [prefs, setPrefs] = usePersistentState<MarketsPrefs>('nexus.markets.v1', DEFAULT_PREFS, isPrefs)
  const [autoRefresh, setAutoRefresh] = usePersistentState<boolean>('nexus.markets.auto.v1', false, isBool)
  const [qInput, setQInput] = useState('')
  const debouncedQ = useDebounced(qInput.trim(), 400)
  const [q, setQ] = useState('')
  const [page, setPage] = useState(0)

  const [data, setData] = useState<UniverseResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [watch, setWatch] = useState<Set<string>>(() => new Set())
  const seq = useRef(0)

  const { market, sort, order, type } = prefs

  // Server-side search: apply the debounced text (Enter applies immediately). Filters reset the page in the
  // same update so that only one request is made.
  useEffect(() => {
    setQ(debouncedQ)
    setPage(0)
  }, [debouncedQ])

  const load = useCallback(
    async (silent = false) => {
      const my = ++seq.current
      if (!silent) setLoading(true)
      try {
        const res = await api.universe({
          market,
          offset: page * PAGE_SIZE,
          limit: PAGE_SIZE,
          sort,
          order,
          q: q || undefined,
          type: type || undefined,
        })
        if (my !== seq.current) return
        setData({ total: isNum(res?.total) ? res.total : 0, rows: dedupe(Array.isArray(res?.rows) ? res.rows : []) })
        setError(null)
        setUpdatedAt(Date.now())
      } catch (err) {
        if (my !== seq.current) return
        setError(err)
      } finally {
        if (my === seq.current) setLoading(false)
      }
    },
    [market, page, sort, order, q, type],
  )

  useEffect(() => {
    void load()
  }, [load])

  useInterval(
    () => {
      if (!document.hidden) void load(true)
    },
    autoRefresh ? AUTO_REFRESH_MS : null,
  )

  // Watchlist (★).
  useEffect(() => {
    let alive = true
    api.watchlist
      .list()
      .then((res) => {
        if (alive) setWatch(new Set((Array.isArray(res?.items) ? res.items : []).map((i) => i.symbol)))
      })
      .catch(() => {
        /* the ★ column still works optimistically */
      })
    return () => {
      alive = false
    }
  }, [])

  const watchRef = useRef(watch)
  watchRef.current = watch
  const toggleWatch = useCallback(
    async (symbol: string) => {
      const had = watchRef.current.has(symbol)
      const flip = (prev: Set<string>, add: boolean) => {
        const next = new Set(prev)
        if (add) next.add(symbol)
        else next.delete(symbol)
        return next
      }
      setWatch((prev) => flip(prev, !had))
      try {
        const res = had ? await api.watchlist.remove(symbol) : await api.watchlist.add(symbol)
        if (Array.isArray(res?.items)) setWatch(new Set(res.items.map((i) => i.symbol)))
        notify(had ? `${symbol} quitado de tu lista` : `${symbol} añadido a tu lista: NEXUS lo vigilará y avisará en la terminal`, 'success')
      } catch (err) {
        setWatch((prev) => flip(prev, had))
        notify(`No se pudo actualizar tu lista: ${errorMessage(err)}`, 'error')
      }
    },
    [notify],
  )

  const onOpen = useCallback((symbol: string) => openChart(symbol), [openChart])

  const setMarket = (m: string) => {
    if (!m || m === market) return
    setData(null)
    setError(null)
    setPage(0)
    setPrefs({ ...prefs, market: m })
  }

  const onSort = useCallback(
    (key: SortKey) => {
      setPage(0)
      setPrefs(key === sort ? { ...prefs, order: order === 'desc' ? 'asc' : 'desc' } : { ...prefs, sort: key, order: 'desc' })
    },
    [prefs, setPrefs, sort, order],
  )

  // Market selector: main tabs + countries / other markets.
  const mainTabs = useMemo(
    () =>
      MAIN_MARKETS.map((m) => {
        const info = markets.find((x) => x.id === m.id)
        return {
          id: m.id,
          label: (
            <>
              {m.label}
              {info && isNum(info.count) && <span className="mk-tab-count">{fmtCompact(info.count)}</span>}
            </>
          ),
        }
      }),
    [markets],
  )
  const others = useMemo(
    () =>
      markets
        .filter((m) => !isMainMarket(m.id))
        .sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id, 'es'))
        .map((m) => ({ value: m.id, label: `${m.name || m.id}${isNum(m.count) ? ` (${fmtCompact(m.count)})` : ''}` })),
    [markets],
  )
  const otherOptions = useMemo(() => {
    const opts = [{ value: '', label: others.length ? 'Países y otros mercados…' : 'Sin otros mercados' }, ...others]
    if (!isMainMarket(market) && !others.some((o) => o.value === market)) opts.push({ value: market, label: marketLabel(market, markets) })
    return opts
  }, [others, market, markets])

  const grandTotal = useMemo(() => {
    const g = markets.find((m) => m.id === 'global')
    if (g && isNum(g.count)) return g.count
    if (market === 'global' && data && !q && !type) return data.total
    return null
  }, [markets, market, data, q, type])

  const total = data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const rows = data?.rows ?? []
  const from = total ? page * PAGE_SIZE + 1 : 0
  const to = Math.min(total, page * PAGE_SIZE + rows.length)
  const mLabel = marketLabel(market, markets)

  return (
    <div className="mk-page">
      <div className="page-header">
        <Globe size={18} className="accent" />
        <div className="col gap-0">
          <h1 className="mk-title">Todos los activos de TradingView</h1>
          <span className="sub">
            {grandTotal !== null ? `${fmtInt(grandTotal)} activos en TradingView` : 'Acciones, cripto, forex, futuros, CFD, bonos e índices'}
            {' · '}haz clic en una fila para abrir el gráfico con NEXUS
          </span>
        </div>
        <div className="grow" />
        <Toggle checked={autoRefresh} onChange={setAutoRefresh} label="Auto-actualizar (60 s)" />
        <Button size="sm" icon={<RefreshCw size={13} />} onClick={() => void load()} loading={loading} title="Actualizar ahora">
          Actualizar
        </Button>
      </div>

      <div className="mk-markets">
        <div className="mk-tabs">
          <Tabs tabs={mainTabs} value={isMainMarket(market) ? market : ''} onChange={setMarket} />
        </div>
        <Select
          value={isMainMarket(market) ? '' : market}
          options={otherOptions}
          onChange={(v) => v && setMarket(v)}
          title="Mercados por país y otros"
          style={{ width: 230 }}
        />
      </div>
      {marketsError && (
        <div className="notice small">
          No se pudo cargar la lista de mercados ({marketsError}). Se muestran los principales.
          <Button size="sm" variant="ghost" onClick={reloadMarkets}>
            Reintentar
          </Button>
        </div>
      )}

      <div className="mk-toolbar">
        <div className="mk-search">
          <Search size={13} className="mk-search-ico" />
          <input
            className="input"
            value={qInput}
            placeholder={`Buscar en ${mLabel} (nombre o ticker)…`}
            aria-label="Buscar activos"
            spellCheck={false}
            onChange={(e) => setQInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                setQ(qInput.trim())
                setPage(0)
              } else if (e.key === 'Escape') {
                setQInput('')
                setQ('')
                setPage(0)
              }
            }}
          />
          {qInput && (
            <button
              type="button"
              className="mk-search-clr"
              title="Borrar búsqueda"
              onClick={() => {
                setQInput('')
                setQ('')
                setPage(0)
              }}
            >
              <X size={12} />
            </button>
          )}
        </div>
        <Select value={type} options={TYPE_OPTIONS} onChange={(v) => {
            setPage(0)
            setPrefs({ ...prefs, type: v })
          }} title="Tipo de activo" style={{ width: 170 }} />
        <span className="small muted mk-count">
          {data ? (
            <>
              <strong className="text-2">{fmtInt(total)}</strong> {total === 1 ? 'activo' : 'activos'} en {mLabel}
              {q && <> · «{q}»</>}
            </>
          ) : loading ? (
            'Cargando…'
          ) : null}
        </span>
        <div className="grow" />
        {loading && data && <Spinner />}
        {updatedAt && <span className="tiny muted nowrap">Actualizado {fmtAgo(updatedAt)}</span>}
      </div>

      {error !== null && data && <ErrorBox error={error} onRetry={() => void load()} />}

      <Card className="mk-card" bodyClassName="mk-card-body" flush>
        {!data && loading ? (
          <Loading label={`Cargando ${mLabel}…`} />
        ) : !data && error !== null ? (
          <div className="mk-pad">
            <ErrorBox error={error} onRetry={() => void load()} />
          </div>
        ) : rows.length === 0 ? (
          <Empty icon={<Search size={22} />} title="No hay activos que coincidan">
            {q || type ? 'Prueba con otro texto o quita el filtro de tipo.' : 'Este mercado no devolvió activos ahora mismo.'}
          </Empty>
        ) : (
          <div className={`table-wrap mk-table-wrap ${loading ? 'mk-dim' : ''}`}>
            <UniverseTable rows={rows} sort={sort} order={order} onSort={onSort} watch={watch} onToggleWatch={toggleWatch} onOpen={onOpen} />
          </div>
        )}
        <div className="mk-pager">
          <span className="small muted">
            {total ? (
              <>
                Mostrando <span className="mono text-2">{fmtInt(from)}</span>–<span className="mono text-2">{fmtInt(to)}</span> de{' '}
                <span className="mono text-2">{fmtInt(total)}</span>
              </>
            ) : (
              '—'
            )}
          </span>
          <div className="grow" />
          <div className="btn-group">
            <Button size="sm" icon={<ChevronsLeft size={13} />} disabled={page === 0 || loading} onClick={() => setPage(0)} title="Primera página" />
            <Button size="sm" icon={<ChevronLeft size={13} />} disabled={page === 0 || loading} onClick={() => setPage(page - 1)} title="Página anterior" />
            <Button size="sm" disabled title="Página actual">
              {fmtInt(page + 1)} / {fmtInt(pages)}
            </Button>
            <Button size="sm" icon={<ChevronRight size={13} />} disabled={page >= pages - 1 || loading} onClick={() => setPage(page + 1)} title="Página siguiente" />
            <Button size="sm" icon={<ChevronsRight size={13} />} disabled={page >= pages - 1 || loading} onClick={() => setPage(pages - 1)} title="Última página" />
          </div>
        </div>
      </Card>
      <p className="disclaimer mk-foot">
        Datos del escáner público de TradingView (con caché y retraso posible). La lectura «NEXUS rápido» es una probabilidad estimada, no una
        garantía ni una recomendación de inversión.
      </p>
    </div>
  )
}
