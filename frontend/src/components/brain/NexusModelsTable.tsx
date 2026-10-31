// Table of the per-asset NEXUS models (one per symbol + timeframe) with "Ver modelo" and "Reiniciar" actions.
import { memo, useMemo, useState } from 'react'
import { ChartCandlestick, Eye, RotateCcw } from 'lucide-react'
import type { LearningSummary } from '../../api/types'
import { fmtBarTime, fmtInt } from '../../lib/format'
import { useApp } from '../../store'
import { Button, Empty, IconButton } from '../ui'
import { fmtBrier, fmtRate } from './learnFormat'

// No green/red against a fixed 50 % / 0,25: the engine's sign hit rate is only meaningful against the share of rising
// bars of the same period (see "Habilidad frente a la referencia" for the verified predictions).
const RATE_TITLE = 'Acierto de la dirección (sube / baja) del propio motor; compáralo con la frecuencia de subidas, no con un 50 %'
const BRIER_TITLE = 'Brier del motor: 0 perfecto; 0,25 = decir siempre 50 % (la referencia real es la climatología del periodo)'

type Row = LearningSummary['nexus_models'][number]
type SortKey = 'symbol' | 'updates' | 'hit_rate' | 'brier' | 'trained_until'

interface Props {
  rows: Row[]
  onView: (row: Row) => void
  onReset: (row: Row) => void
}

const COLS: { key: SortKey; label: string; num?: boolean }[] = [
  { key: 'symbol', label: 'Símbolo' },
  { key: 'updates', label: 'Actualizaciones', num: true },
  { key: 'hit_rate', label: 'Acierto', num: true },
  { key: 'brier', label: 'Brier', num: true },
  { key: 'trained_until', label: 'Entrenado hasta', num: true },
]

function NexusModelsTable({ rows, onView, onReset }: Props) {
  const openChart = useApp((s) => s.openChart)
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'updates', dir: -1 })

  const sorted = useMemo(() => {
    const val = (r: Row): number | string => {
      if (sort.key === 'symbol') return `${r.symbol}|${r.tf}`
      const v = r[sort.key]
      return typeof v === 'number' && Number.isFinite(v) ? v : Number.NEGATIVE_INFINITY
    }
    return [...rows].sort((a, b) => {
      const va = val(a)
      const vb = val(b)
      const c = typeof va === 'string' && typeof vb === 'string' ? va.localeCompare(vb) : (va as number) - (vb as number)
      return c * sort.dir
    })
  }, [rows, sort])

  if (!rows.length) {
    return (
      <Empty title="Todavía no hay modelos NEXUS entrenados">
        Abre un activo en el gráfico o añade símbolos a la lista de seguimiento: cada símbolo y temporalidad aprende su propio modelo.
      </Empty>
    )
  }

  const header = (c: (typeof COLS)[number]) => (
    <th
      key={c.key}
      className={`sortable ${c.num ? 'num' : ''}`}
      onClick={() => setSort((s) => ({ key: c.key, dir: s.key === c.key ? ((-s.dir) as 1 | -1) : c.key === 'symbol' ? 1 : -1 }))}
      aria-sort={sort.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}
    >
      {c.label}
      {sort.key === c.key ? (sort.dir === 1 ? ' ▲' : ' ▼') : ''}
    </th>
  )

  return (
    <div className="table-wrap" style={{ maxHeight: 360 }}>
      <table className="table">
        <thead>
          <tr>
            {header(COLS[0])}
            <th>TF</th>
            {COLS.slice(1).map(header)}
            <th className="num">Acciones</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={`${r.symbol}|${r.tf}`}>
              <td className="mono">{r.symbol}</td>
              <td className="mono">{r.tf}</td>
              <td className="num">{fmtInt(r.updates)}</td>
              <td className="num" title={RATE_TITLE}>
                {fmtRate(r.hit_rate)}
              </td>
              <td className="num" title={BRIER_TITLE}>
                {fmtBrier(r.brier)}
              </td>
              <td className="num">{fmtBarTime(r.trained_until, r.tf)}</td>
              <td className="num">
                <div className="row gap-1 end">
                  <Button size="sm" icon={<Eye size={13} />} onClick={() => onView(r)}>
                    Ver modelo
                  </Button>
                  <IconButton size="sm" icon={<ChartCandlestick size={14} />} title="Abrir en el gráfico" onClick={() => openChart(r.symbol)} />
                  <IconButton size="sm" icon={<RotateCcw size={14} />} title="Reiniciar (olvidar lo aprendido)" onClick={() => onReset(r)} />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default memo(NexusModelsTable)
