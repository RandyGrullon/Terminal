// Sortable scanner results table. Click a row to see the reasons; click the symbol to open its chart.
import { memo, useCallback, useState } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronRight, TrendingDown, TrendingUp } from 'lucide-react'
import type { Regime, ScanRow, Timeframe } from '../../api/types'
import { REGIME_LABEL, fmtCompact, fmtPct, fmtPrice, fmtProb, fmtScore, regimeTone, signTone, toneColor } from '../../lib/format'
import { useApp } from '../../store'
import { ActionBadge, Badge, Meter, VerdictBadge } from '../ui'
import { isNum, probTone, ratingInfo, typeLabel } from '../markets/opsUtils'
import { SCAN_ACTION_LABEL, type SortKey, type SortState } from './scanUtils'

interface Props {
  rows: ScanRow[]
  sort: SortState
  onSort: (key: SortKey) => void
  tf: Timeframe | undefined
}

const COLUMNS: { key: SortKey | null; label: string; title?: string; num?: boolean; stick?: boolean }[] = [
  { key: 'rank', label: '#' },
  { key: 'symbol', label: 'Símbolo', stick: true },
  { key: 'price', label: 'Precio', num: true },
  { key: 'change_pct', label: 'Cambio %', num: true },
  { key: 'score', label: 'Score', title: 'Puntuación del modelo (−1 venta … +1 compra)' },
  { key: 'prob_up', label: 'P↑', num: true, title: 'Probabilidad estimada de subida en el horizonte del escáner' },
  { key: 'action', label: 'Acción' },
  { key: 'verdict', label: 'Veredicto', title: 'Veredicto del filtro (Trade Gate) y su puntuación 0-100' },
  { key: 'regime', label: 'Régimen' },
  { key: null, label: 'SL / TP1 / TP2', title: 'Niveles propuestos por NEXUS: SL «hasta aquí» y objetivos' },
  { key: 'expected_move_pct', label: 'Mov. esperado', num: true, title: 'Movimiento esperado en el horizonte de NEXUS' },
  { key: 'confidence', label: 'Confianza' },
]

function regimeLabel(r: string): string {
  return REGIME_LABEL[r as Regime] ?? r
}

function Levels({ row }: { row: ScanRow }) {
  if (!isNum(row.sl) && !isNum(row.tp1) && !isNum(row.tp2)) return <span className="muted">—</span>
  return (
    <div className="scn-levels">
      <span>SL</span>
      <span className="neg">{fmtPrice(row.sl)}</span>
      <span>TP1</span>
      <span className="pos">{fmtPrice(row.tp1)}</span>
      <span>TP2</span>
      <span className="pos">{fmtPrice(row.tp2)}</span>
    </div>
  )
}

const Row = memo(function Row(props: { row: ScanRow; open: boolean; onToggle: (symbol: string) => void; onOpenChart: (symbol: string) => void }) {
  const { row: r, open, onToggle, onOpenChart } = props
  const scoreTone = signTone(r.score)
  const rating = ratingInfo(r.rating)
  return (
    <>
      <tr className={`clickable ${open ? 'scn-open' : ''}`} onClick={() => onToggle(r.symbol)} aria-expanded={open}>
        <td className="mono muted">
          <span className="row gap-1">
            <ChevronRight size={13} className="scn-chev" />
            {r.rank}
          </span>
        </td>
        <td className="scn-stick">
          <div className="scn-sym">
            <button
              type="button"
              title={`Abrir ${r.symbol} en el gráfico`}
              onClick={(e) => {
                e.stopPropagation()
                onOpenChart(r.symbol)
              }}
            >
              {r.symbol}
            </button>
            {r.description && <span className="scn-desc" title={r.description}>{r.description}</span>}
          </div>
        </td>
        <td className="num mono">{fmtPrice(r.price)}</td>
        <td className="num mono" style={{ color: toneColor(signTone(r.change_pct)) }}>
          {fmtPct(r.change_pct)}
        </td>
        <td>
          <div className="scn-score" title={`Score ${fmtScore(r.score)}`}>
            <Meter value={r.score} centered tone={scoreTone} height={6} />
            <span className="mono small" style={{ color: toneColor(scoreTone) }}>
              {fmtScore(r.score)}
            </span>
          </div>
        </td>
        <td className="num mono" style={{ color: toneColor(probTone(r.prob_up)) }}>
          {fmtProb(r.prob_up)}
        </td>
        <td>
          <ActionBadge action={r.action} label={SCAN_ACTION_LABEL[r.action]} />
        </td>
        <td>
          {r.deep && r.verdict ? (
            <span className="row gap-1" title={r.direction ? `Evaluado en ${r.direction === 'long' ? 'largo' : 'corto'}` : undefined}>
              <VerdictBadge verdict={r.verdict} score={r.gate_score} />
              {r.direction === 'long' ? <TrendingUp size={13} className="pos" /> : r.direction === 'short' ? <TrendingDown size={13} className="neg" /> : null}
            </span>
          ) : (
            <span className="tiny muted" title="Solo modelo rápido: activa el modo profundo para pasar el filtro">
              {r.deep ? 'sin veredicto' : 'sin análisis'}
            </span>
          )}
        </td>
        <td>{r.regime ? <Badge tone={regimeTone(r.regime)}>{regimeLabel(r.regime)}</Badge> : <span className="muted">—</span>}</td>
        <td>
          <Levels row={r} />
        </td>
        <td className="num mono" style={{ color: toneColor(signTone(r.expected_move_pct)) }}>
          {fmtPct(r.expected_move_pct)}
        </td>
        <td>
          <div className="scn-conf" title={`Confianza ${fmtProb(r.confidence)}`}>
            <Meter value={r.confidence} tone="accent" height={5} />
            <span className="mono tiny text-2">{fmtProb(r.confidence)}</span>
          </div>
        </td>
      </tr>
      {open && (
        <tr className="scn-detail">
          <td colSpan={COLUMNS.length}>
            {r.reasons.length > 0 ? (
              <ul className="scn-reasons">
                {r.reasons.map((reason, i) => (
                  <li key={i}>{reason}</li>
                ))}
              </ul>
            ) : (
              <span className="tiny muted">El escáner no devolvió motivos para este activo.</span>
            )}
            <div className="scn-detail-meta">
              {r.type && <span>Tipo: {typeLabel(r.type)}</span>}
              <span>
                Calificación TradingView: <b style={{ color: toneColor(rating.tone) }}>{rating.label}</b>
              </span>
              <span>Volumen: {fmtCompact(r.volume)}</span>
              <span>{r.deep ? 'Análisis profundo: NEXUS completo + filtro' : 'Solo modelo rápido (sin NEXUS completo)'}</span>
            </div>
          </td>
        </tr>
      )}
    </>
  )
})

function SortHeader(props: { col: (typeof COLUMNS)[number]; sort: SortState; onSort: (k: SortKey) => void }) {
  const { col, sort, onSort } = props
  if (!col.key) return <th title={col.title}>{col.label}</th>
  const key = col.key
  const active = sort.key === key
  const Icon = active ? (sort.dir === 'asc' ? ArrowUp : ArrowDown) : ArrowUpDown
  return (
    <th
      className={`sortable ${col.num ? 'num' : ''} ${active ? 'active' : ''} ${col.stick ? 'scn-stick' : ''}`}
      title={col.title ?? `Ordenar por ${col.label}`}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
      onClick={() => onSort(key)}
    >
      <span className="scn-sort">
        {col.label}
        <Icon size={11} style={{ opacity: active ? 1 : 0.35 }} />
      </span>
    </th>
  )
}

function ScanTable({ rows, sort, onSort, tf }: Props) {
  const [open, setOpen] = useState<Set<string>>(() => new Set())
  const toggle = useCallback((symbol: string) => {
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(symbol)) next.delete(symbol)
      else next.add(symbol)
      return next
    })
  }, [])
  const openChart = useCallback((symbol: string) => useApp.getState().openChart(symbol, tf), [tf])

  return (
    <div className="table-wrap scn-table-wrap">
      <table className="table scn-table">
        <thead>
          <tr>
            {COLUMNS.map((c) => (
              <SortHeader key={c.label} col={c} sort={sort} onSort={onSort} />
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <Row key={`${r.rank}:${r.symbol}`} row={r} open={open.has(r.symbol)} onToggle={toggle} onOpenChart={openChart} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default memo(ScanTable)
