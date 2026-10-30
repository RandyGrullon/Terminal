// Sortable table of backtest trades. Clicking a row focuses that trade on the price chart.
import { memo, useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react'
import type { Trade } from '../../api/types'
import { DIRECTION_LABEL, EXIT_REASON_LABEL, fmtBarTime, fmtInt, fmtNum, fmtPct, fmtPrice, fmtR, signTone } from '../../lib/format'
import { Badge, Card, Empty } from '../ui'

type SortKey =
  | 'entry_time'
  | 'exit_time'
  | 'side'
  | 'entry_price'
  | 'exit_price'
  | 'r_multiple'
  | 'pnl_pct'
  | 'exit_label'
  | 'bars'
  | 'mfe_pct'
  | 'gate_score'

interface Column {
  key: SortKey
  label: string
  num?: boolean
  title?: string
}

const COLUMNS: Column[] = [
  { key: 'entry_time', label: 'Entrada' },
  { key: 'exit_time', label: 'Salida' },
  { key: 'side', label: 'Lado' },
  { key: 'entry_price', label: 'Precio entrada', num: true },
  { key: 'exit_price', label: 'Precio salida', num: true },
  { key: 'r_multiple', label: 'R', num: true, title: 'Resultado en múltiplos del riesgo inicial' },
  { key: 'pnl_pct', label: 'P&L %', num: true },
  { key: 'exit_label', label: 'Motivo' },
  { key: 'bars', label: 'Barras', num: true },
  { key: 'mfe_pct', label: 'MFE / MAE', num: true, title: 'Máximo a favor / máximo en contra durante la operación' },
  { key: 'gate_score', label: 'Filtro', num: true, title: 'Puntuación del filtro (0-100) al entrar' },
]

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function reasonOf(t: Trade): string {
  if (t.exit_label) return t.exit_label
  if (t.exit_reason) return EXIT_REASON_LABEL[t.exit_reason] ?? t.exit_reason
  return t.exit_time ? '—' : 'Abierta'
}

function sortValue(t: Trade, key: SortKey): number | string | null {
  switch (key) {
    case 'side':
      return t.side
    case 'exit_label':
      return reasonOf(t)
    default: {
      const v = t[key]
      return isNum(v) ? v : null
    }
  }
}

export const TradesTable = memo(function TradesTable(props: {
  trades: Trade[]
  tf: string
  selectedId: number | null
  onSelect: (id: number | null) => void
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'entry_time', dir: 'desc' })

  const rows = useMemo(() => {
    const factor = sort.dir === 'asc' ? 1 : -1
    return [...props.trades].sort((a, b) => {
      const va = sortValue(a, sort.key)
      const vb = sortValue(b, sort.key)
      if (va === null && vb === null) return 0
      if (va === null) return 1
      if (vb === null) return -1
      if (typeof va === 'string' || typeof vb === 'string') return String(va).localeCompare(String(vb), 'es') * factor
      return (va - vb) * factor
    })
  }, [props.trades, sort])

  const stats = useMemo(() => {
    const closed = props.trades.filter((t) => isNum(t.pnl_pct))
    const wins = closed.filter((t) => (t.pnl_pct as number) > 0).length
    return { closed: closed.length, wins }
  }, [props.trades])

  const toggle = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: key === 'side' || key === 'exit_label' ? 'asc' : 'desc' }))

  return (
    <Card
      title="Operaciones"
      sub={
        props.trades.length
          ? `${fmtInt(props.trades.length)} operaciones · ${fmtInt(stats.wins)} ganadoras de ${fmtInt(stats.closed)} cerradas`
          : undefined
      }
      flush
    >
      {props.trades.length === 0 ? (
        <Empty title="Sin operaciones en este periodo">
          NEXUS no encontró señales que pasaran sus reglas. Prueba con más velas, otra temporalidad o un umbral de entrada menor.
        </Empty>
      ) : (
        <div className="table-wrap sim-trades-wrap">
          <table className="table">
            <thead>
              <tr>
                <th className="num">#</th>
                {COLUMNS.map((c) => {
                  const active = sort.key === c.key
                  return (
                    <th
                      key={c.key}
                      className={`sortable ${c.num ? 'num' : ''}`}
                      title={c.title}
                      onClick={() => toggle(c.key)}
                      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
                    >
                      <span className="sim-th">
                        {c.label}
                        {active ? sort.dir === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} /> : <ChevronsUpDown size={11} className="sim-th-idle" />}
                      </span>
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const sel = t.id === props.selectedId
                return (
                  <tr
                    key={t.id}
                    className={`clickable ${sel ? 'sim-selected' : ''}`}
                    onClick={() => props.onSelect(sel ? null : t.id)}
                    title="Ver esta operación en el gráfico"
                  >
                    <td className="num mono muted">{t.id}</td>
                    <td className="mono">{fmtBarTime(t.entry_time, props.tf)}</td>
                    <td className="mono">{t.exit_time ? fmtBarTime(t.exit_time, props.tf) : <span className="muted">Abierta</span>}</td>
                    <td>
                      <Badge tone={t.side === 'long' ? 'pos' : 'neg'}>{DIRECTION_LABEL[t.side] ?? t.side}</Badge>
                    </td>
                    <td className="num mono">{fmtPrice(t.entry_price)}</td>
                    <td className="num mono">{fmtPrice(t.exit_price)}</td>
                    <td className={`num mono ${signTone(t.r_multiple)}`}>{fmtR(t.r_multiple)}</td>
                    <td className={`num mono ${signTone(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td>
                    <td>{reasonOf(t)}</td>
                    <td className="num mono">{fmtInt(t.bars)}</td>
                    <td className="num mono">
                      <span className="pos">{fmtPct(t.mfe_pct, 1)}</span> <span className="muted">/</span>{' '}
                      <span className="neg">{fmtPct(isNum(t.mae_pct) ? -Math.abs(t.mae_pct) || 0 : null, 1)}</span>
                    </td>
                    <td className="num mono">{isNum(t.gate_score) ? fmtNum(t.gate_score, 0) : '—'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
})
