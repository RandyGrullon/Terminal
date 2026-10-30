// Universe table (Markets page): one row per TradingView asset with technicals and the NEXUS quick read.
// Sorting is server-side: header clicks only report the TradingView column to sort by.
import { memo } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown, Star } from 'lucide-react'
import type { UniverseRow } from '../../api/types'
import { fmtCompact, fmtNum, fmtPct, fmtPrice, fmtProb, fmtScore, signTone, splitSymbol, toneColor } from '../../lib/format'
import { Meter } from '../ui'
import { isNum, probTone, ratingInfo, typeLabel } from './opsUtils'

export type SortKey =
  | 'close'
  | 'change'
  | 'volume'
  | 'market_cap_basic'
  | 'Recommend.All'
  | 'RSI'
  | 'ADX'
  | 'Perf.W'
  | 'Perf.1M'
  | 'Volatility.D'

export const SORT_KEYS: SortKey[] = ['close', 'change', 'volume', 'market_cap_basic', 'Recommend.All', 'RSI', 'ADX', 'Perf.W', 'Perf.1M', 'Volatility.D']

interface Column {
  id: string
  label: string
  sort?: SortKey
  num?: boolean
  title?: string
}

const COLUMNS: Column[] = [
  { id: 'star', label: '★', title: 'Tu lista de seguimiento: NEXUS vigila estos símbolos y avisa en la terminal' },
  { id: 'symbol', label: 'Símbolo' },
  { id: 'price', label: 'Precio', sort: 'close', num: true },
  { id: 'change', label: 'Cambio %', sort: 'change', num: true, title: 'Cambio de la sesión' },
  { id: 'volume', label: 'Volumen', sort: 'volume', num: true },
  { id: 'rating', label: 'Rating TV', sort: 'Recommend.All', title: 'Rating técnico de TradingView: −1 venta fuerte … +1 compra fuerte' },
  { id: 'rsi', label: 'RSI', sort: 'RSI', num: true, title: 'RSI(14) diario: > 70 sobrecompra, < 30 sobreventa' },
  { id: 'adx', label: 'ADX', sort: 'ADX', num: true, title: 'ADX(14): > 25 tendencia fuerte, < 20 sin tendencia' },
  { id: 'perf_w', label: 'Semana %', sort: 'Perf.W', num: true, title: 'Rendimiento de la última semana' },
  { id: 'perf_1m', label: 'Mes %', sort: 'Perf.1M', num: true, title: 'Rendimiento del último mes' },
  { id: 'vol_d', label: 'Volat.', sort: 'Volatility.D', num: true, title: 'Volatilidad diaria (%)' },
  { id: 'mcap', label: 'Cap. mercado', sort: 'market_cap_basic', num: true, title: 'Capitalización de mercado' },
  {
    id: 'quick',
    label: 'NEXUS rápido',
    title: 'Lectura rápida del modelo NEXUS del escáner: probabilidad de subida y score (−1 … +1). No es una orden.',
  },
]

function rsiTone(v: number | null): string {
  if (!isNum(v)) return 'muted'
  if (v >= 70) return 'warn'
  if (v <= 30) return 'info'
  return ''
}

const Row = memo(function Row(props: {
  r: UniverseRow
  watched: boolean
  onToggleWatch: (symbol: string) => void
  onOpen: (symbol: string) => void
}) {
  const { r, watched } = props
  const { exchange, ticker } = splitSymbol(r.symbol)
  const rating = ratingInfo(r.rating)
  const quickTone = signTone(r.quick_score)
  const rsiT = rsiTone(r.rsi)
  return (
    <tr className="clickable" onClick={() => props.onOpen(r.symbol)} title={`Abrir ${r.symbol} en el gráfico`}>
      <td className="mk-star-cell">
        <button
          type="button"
          className={`mk-star ${watched ? 'on' : ''}`}
          aria-pressed={watched}
          title={watched ? 'Quitar de tu lista de seguimiento' : 'Añadir a tu lista de seguimiento'}
          onClick={(e) => {
            e.stopPropagation()
            props.onToggleWatch(r.symbol)
          }}
        >
          <Star size={13} fill={watched ? 'currentColor' : 'none'} />
        </button>
      </td>
      <td className="mk-sym-cell">
        <div className="mk-sym">
          <span className="mk-ticker">{r.ticker || ticker}</span>
          <span className="mk-exch">{r.exchange || exchange}</span>
          {r.type && <span className="mk-type">{typeLabel(r.type)}</span>}
        </div>
        <div className="mk-desc" title={r.description}>
          {r.description || '—'}
        </div>
      </td>
      <td className="num mono" title={r.currency ? `Moneda: ${r.currency}` : undefined}>
        {fmtPrice(r.close)}
      </td>
      <td className="num mono" style={{ color: toneColor(signTone(r.change_pct)) }}>
        {fmtPct(r.change_pct)}
      </td>
      <td className="num mono">{fmtCompact(r.volume)}</td>
      <td>
        {isNum(r.rating) ? (
          <div className="mk-meter-cell" title={`Rating ${fmtNum(r.rating, 2)}`}>
            <span className={`mk-meter-label ${rating.tone}`}>{rating.label}</span>
            <Meter value={r.rating} centered tone={rating.tone === 'muted' ? 'info' : rating.tone} height={4} />
          </div>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
      <td className={`num mono ${rsiT}`} title={isNum(r.rsi) ? (r.rsi >= 70 ? 'Sobrecompra' : r.rsi <= 30 ? 'Sobreventa' : undefined) : undefined}>
        {fmtNum(r.rsi, 1)}
      </td>
      <td className={`num mono ${isNum(r.adx) ? (r.adx >= 25 ? 'bold' : r.adx < 20 ? 'muted' : '') : 'muted'}`}>{fmtNum(r.adx, 1)}</td>
      <td className="num mono" style={{ color: toneColor(signTone(r.perf_w)) }}>
        {fmtPct(r.perf_w, 1)}
      </td>
      <td className="num mono" style={{ color: toneColor(signTone(r.perf_1m)) }}>
        {fmtPct(r.perf_1m, 1)}
      </td>
      <td className="num mono">{isNum(r.volatility_d) ? fmtPct(r.volatility_d, 2, false) : '—'}</td>
      <td className="num mono">{fmtCompact(r.market_cap)}</td>
      <td>
        {isNum(r.quick_score) || isNum(r.quick_prob) ? (
          <div className="mk-meter-cell" title={`Score ${fmtScore(r.quick_score)} · P(sube) ${fmtProb(r.quick_prob)}`}>
            <span className={`mk-meter-label mono ${probTone(r.quick_prob)}`}>
              P↑ {fmtProb(r.quick_prob)} <span className="muted">· {fmtScore(r.quick_score)}</span>
            </span>
            <Meter value={isNum(r.quick_score) ? r.quick_score : 0} centered tone={quickTone === 'muted' ? 'info' : quickTone} height={4} />
          </div>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
    </tr>
  )
})

export default function UniverseTable(props: {
  rows: UniverseRow[]
  sort: SortKey
  order: 'asc' | 'desc'
  onSort: (key: SortKey) => void
  watch: Set<string>
  onToggleWatch: (symbol: string) => void
  onOpen: (symbol: string) => void
}) {
  const { sort, order } = props
  return (
    <table className="table mk-table">
      <thead>
        <tr>
          {COLUMNS.map((c) => {
            const activeSort = c.sort !== undefined && c.sort === sort
            return (
              <th
                key={c.id}
                className={`${c.num ? 'num' : ''} ${c.sort ? 'sortable' : ''} ${activeSort ? 'mk-sorted' : ''}`}
                title={c.title ?? (c.sort ? `Ordenar por ${c.label}` : undefined)}
                aria-sort={activeSort ? (order === 'asc' ? 'ascending' : 'descending') : undefined}
                onClick={c.sort ? () => props.onSort(c.sort as SortKey) : undefined}
              >
                <span className="mk-th">
                  {c.label}
                  {c.sort &&
                    (activeSort ? (
                      order === 'asc' ? (
                        <ArrowUp size={11} />
                      ) : (
                        <ArrowDown size={11} />
                      )
                    ) : (
                      <ArrowUpDown size={10} className="mk-sort-hint" />
                    ))}
                </span>
              </th>
            )
          })}
        </tr>
      </thead>
      <tbody>
        {props.rows.map((r) => (
          <Row key={r.symbol} r={r} watched={props.watch.has(r.symbol)} onToggleWatch={props.onToggleWatch} onOpen={props.onOpen} />
        ))}
      </tbody>
    </table>
  )
}
