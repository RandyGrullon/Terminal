// Watchlist card: quotes refreshed every 15 s (paused while the tab is hidden; a stale quote is dimmed, never shown
// as live), click to open a symbol, star to add / remove the current symbol, x to remove a row. Each row says how
// fresh its price is when the source tells ("Tiempo real", "Retraso 15 min", "Cerrado"…, components/markets/freshness.ts),
// with the full sentence in the tooltip; nothing is claimed when the source does not say.
import { memo, useCallback, useEffect, useState } from 'react'
import { ListPlus, RefreshCw, Star, X } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { WatchItem } from '../../api/types'
import { fmtPct, fmtPrice, signTone, splitSymbol, type Tone } from '../../lib/format'
import { useApp } from '../../store'
import { describeFreshness, type Freshness } from '../markets/freshness'
import { Card, Empty, ErrorBox, IconButton, Loading, Spinner } from '../ui'
import './nexus.css'

const REFRESH_MS = 15_000

/** Short words of a row: the badge labels are "<estado> · <fuente>" and the exchange is already on the row. */
function shortLabel(f: Freshness): string | null {
  const head = f.label.split(' · ')[0]
  switch (f.kind) {
    case 'realtime':
      return 'Tiempo real'
    case 'delayed': // "Retraso 15 min"
      return head
    case 'closed':
      return 'Cerrado'
    case 'stale':
      return 'Desactualizado'
    case 'recent': // "Twelve Data · hace 3 min"
      return f.label.split(' · ').pop() ?? f.label
    case 'synthetic':
      return 'Sintético'
    default:
      return null // only the source name: it does not say whether the price is live or delayed
  }
}

/** Freshness of one row's quote (`readAt` = client ms of the read that brought it), or null. */
export function rowFreshness(item: WatchItem, readAt: number | null, nowMs: number): { text: string; tone: Tone; title: string } | null {
  const f = describeFreshness({ symbol: item.symbol, quote: item.quote, quoteAt: readAt, live: null, candles: null, tf: '1h', nowMs })
  const text = f ? shortLabel(f) : null
  return f && text ? { text, tone: f.tone, title: f.title } : null
}

function Watchlist() {
  const symbol = useApp((s) => s.symbol)
  const setSymbol = useApp((s) => s.setSymbol)
  const notify = useApp((s) => s.notify)
  const [items, setItems] = useState<WatchItem[] | null>(null)
  const [readAt, setReadAt] = useState<number | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let alive = true
    let timer = 0
    const load = async () => {
      if (!document.hidden) {
        setLoading(true)
        try {
          const res = await api.watchlist.list()
          if (!alive) return
          setItems(Array.isArray(res?.items) ? res.items.filter((i) => i && typeof i.symbol === 'string') : [])
          setReadAt(Date.now())
          setError(null)
        } catch (err) {
          if (alive) setError(err)
        } finally {
          if (alive) setLoading(false)
        }
      }
      if (alive) timer = window.setTimeout(load, REFRESH_MS)
    }
    void load()
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [nonce])

  const upper = symbol.toUpperCase()
  const inList = !!items?.some((i) => i.symbol.toUpperCase() === upper)
  const ticker = splitSymbol(symbol).ticker

  const mutate = useCallback(
    async (sym: string, op: 'add' | 'remove') => {
      setBusy(sym)
      try {
        const res = op === 'add' ? await api.watchlist.add(sym) : await api.watchlist.remove(sym)
        setItems(Array.isArray(res?.items) ? res.items : [])
        setReadAt(Date.now())
        setError(null)
      } catch (err) {
        const t = splitSymbol(sym).ticker
        notify(`${op === 'add' ? 'No se pudo añadir' : 'No se pudo quitar'} ${t}: ${errorMessage(err)}`, 'error')
      } finally {
        setBusy(null)
      }
    },
    [notify],
  )

  return (
    <Card
      title={
        <>
          <Star size={14} /> Lista de seguimiento
        </>
      }
      actions={
        <>
          {loading && items !== null && <Spinner />}
          <IconButton
            size="sm"
            icon={busy === symbol ? <Spinner /> : <Star size={14} fill={inList ? 'currentColor' : 'none'} />}
            active={inList}
            title={inList ? `Quitar ${ticker} de la lista` : `Añadir ${ticker} a la lista`}
            disabled={busy !== null}
            onClick={() => void mutate(symbol, inList ? 'remove' : 'add')}
          />
          <IconButton size="sm" icon={<RefreshCw size={13} />} title="Actualizar cotizaciones" onClick={() => setNonce((n) => n + 1)} />
        </>
      }
      flush
    >
      {items === null && !error && <Loading label="Cargando la lista…" />}
      {!!error && (
        <div style={{ padding: 10 }}>
          <ErrorBox error={error} onRetry={() => setNonce((n) => n + 1)} />
        </div>
      )}
      {items !== null && items.length === 0 && !error && (
        <Empty icon={<ListPlus size={20} />} title="Tu lista está vacía">
          Pulsa la estrella para añadir {ticker}.
        </Empty>
      )}
      {items !== null && items.length > 0 && (
        <div className="chart-wl" role="list">
          {items.map((it) => {
            const q = it.quote
            const parts = splitSymbol(it.symbol)
            const active = it.symbol.toUpperCase() === upper
            const tone = signTone(q?.change_pct)
            const fresh = rowFreshness(it, readAt, Date.now())
            return (
              <div
                key={it.symbol}
                role="listitem"
                tabIndex={0}
                className={`chart-wl-row ${active ? 'active' : ''}`}
                title={`${it.description || it.symbol}${it.history ? ` · historial: ${it.history}` : ''}`}
                onClick={() => setSymbol(it.symbol)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    setSymbol(it.symbol)
                  }
                }}
              >
                <div className="col gap-0 grow" style={{ minWidth: 0 }}>
                  <span className="chart-wl-ticker ellipsis">{it.ticker || parts.ticker}</span>
                  <span className="chart-wl-ex ellipsis">
                    {it.exchange || parts.exchange || it.type}
                    {fresh && (
                      <>
                        {' · '}
                        <span className={fresh.tone === 'muted' ? undefined : fresh.tone} title={fresh.title}>
                          {fresh.text}
                        </span>
                      </>
                    )}
                  </span>
                </div>
                <span
                  className={`mono chart-wl-price ${q?.stale ? 'muted' : ''}`}
                  title={q?.stale ? 'Precio desactualizado: no hay datos recientes de este activo' : undefined}
                >
                  {fmtPrice(q?.price)}
                </span>
                <span className={`mono chart-wl-chg ${tone === 'muted' ? 'muted' : tone}`}>{fmtPct(q?.change_pct)}</span>
                <button
                  type="button"
                  className="chart-wl-x"
                  title={`Quitar ${it.ticker || parts.ticker}`}
                  disabled={busy !== null}
                  onClick={(e) => {
                    e.stopPropagation()
                    void mutate(it.symbol, 'remove')
                  }}
                >
                  <X size={12} />
                </button>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}

export default memo(Watchlist)
