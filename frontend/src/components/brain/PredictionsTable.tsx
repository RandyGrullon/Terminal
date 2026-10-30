// Log of every prediction (written BEFORE the outcome is known) and its later verification.
import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { CircleCheck, CircleX, Clock } from 'lucide-react'
import { api } from '../../api/client'
import type { PredictionRow } from '../../api/types'
import { fmtDateTime, fmtInt, fmtPct, fmtPrice, signTone } from '../../lib/format'
import { useApp } from '../../store'
import { Empty, ErrorBox, Select, Spinner } from '../ui'
import { fmtIn, prob01, toMs } from './learnFormat'

type SourceFilter = 'all' | 'nexus' | 'scanner'
type StateFilter = 'all' | 'resolved' | 'pending'
type LimitOpt = '100' | '200' | '500'

const SOURCE_LABEL: Record<string, string> = { nexus: 'NEXUS', scanner: 'Escáner' }

function Direction({ d }: { d: number }) {
  if (d > 0) return <span className="pos">↑ Alcista</span>
  if (d < 0) return <span className="neg">↓ Bajista</span>
  return <span className="muted">Neutral</span>
}

function Outcome({ row }: { row: PredictionRow }) {
  if (!row.resolved) {
    return (
      <span className="muted row gap-1 nowrap" title="Pendiente de verificar">
        <Clock size={13} /> Pendiente
      </span>
    )
  }
  if (row.correct === true) {
    return (
      <span className="pos row gap-1" title="Acertó">
        <CircleCheck size={14} /> Acierto
      </span>
    )
  }
  if (row.correct === false) {
    return (
      <span className="neg row gap-1" title="Falló">
        <CircleX size={14} /> Fallo
      </span>
    )
  }
  return <span className="muted">—</span>
}

function PredictionsTable({ refreshTick }: { refreshTick: number }) {
  const openChart = useApp((s) => s.openChart)
  const [source, setSource] = useState<SourceFilter>('all')
  const [state, setState] = useState<StateFilter>('all')
  const [limit, setLimit] = useState<LimitOpt>('200')
  const [rows, setRows] = useState<PredictionRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const seq = useRef(0)

  const load = useCallback(async () => {
    const my = ++seq.current
    setLoading(true)
    try {
      const r = await api.predictions({
        source: source === 'all' ? undefined : source,
        resolved: state === 'all' ? undefined : state === 'resolved',
        limit: Number(limit),
      })
      if (my !== seq.current) return
      setRows(Array.isArray(r?.predictions) ? r.predictions : [])
      setError(null)
    } catch (e) {
      if (my !== seq.current) return
      setError(e)
    } finally {
      if (my === seq.current) setLoading(false)
    }
  }, [source, state, limit])

  useEffect(() => {
    void load()
  }, [load, refreshTick])

  const sorted = rows ? [...rows].sort((a, b) => (toMs(b.created_at) ?? 0) - (toMs(a.created_at) ?? 0)) : []

  return (
    <div className="col gap-2">
      <div className="row wrap gap-2 brain-filters">
        <span className="label">Fuente</span>
        <Select
          value={source}
          onChange={setSource}
          options={[
            { value: 'all', label: 'Todas' },
            { value: 'nexus', label: 'NEXUS' },
            { value: 'scanner', label: 'Escáner' },
          ]}
        />
        <span className="label">Estado</span>
        <Select
          value={state}
          onChange={setState}
          options={[
            { value: 'all', label: 'Todas' },
            { value: 'resolved', label: 'Resueltas' },
            { value: 'pending', label: 'Pendientes' },
          ]}
        />
        <span className="label">Mostrar</span>
        <Select
          value={limit}
          onChange={setLimit}
          options={[
            { value: '100', label: '100' },
            { value: '200', label: '200' },
            { value: '500', label: '500' },
          ]}
        />
        {loading && <Spinner />}
        <div className="grow" />
        {rows && <span className="tiny muted">{fmtInt(rows.length)} filas</span>}
      </div>
      {error !== null && <ErrorBox error={error} onRetry={() => void load()} />}
      {rows === null && error === null ? (
        <div className="muted small brain-pad">
          <Spinner /> Cargando predicciones…
        </div>
      ) : rows && !rows.length ? (
        <Empty title="Sin predicciones con estos filtros">
          El escáner y NEXUS registran una predicción por activo analizado; aparecerán aquí en cuanto se generen.
        </Empty>
      ) : rows ? (
        <div className="table-wrap" style={{ maxHeight: 460 }}>
          <table className="table">
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Fuente</th>
                <th>Símbolo</th>
                <th>TF</th>
                <th className="num">Precio</th>
                <th>Dirección</th>
                <th className="num">P↑</th>
                <th>Objetivo</th>
                <th className="num">Resultado</th>
                <th>✓/✗</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => {
                const p = prob01(r.prob_up)
                const target = toMs(r.target_at)
                return (
                  <tr key={r.id}>
                    <td className="mono">{fmtDateTime(toMs(r.created_at))}</td>
                    <td>{SOURCE_LABEL[r.source] ?? r.source}</td>
                    <td>
                      <button type="button" className="brain-link mono" onClick={() => openChart(r.symbol)} title="Abrir en el gráfico">
                        {r.symbol}
                      </button>
                    </td>
                    <td className="mono">{r.tf}</td>
                    <td className="num mono">{fmtPrice(r.price)}</td>
                    <td>
                      <Direction d={r.direction} />
                    </td>
                    <td className={`num mono ${p === null ? '' : p >= 0.5 ? 'pos' : 'neg'}`}>{p === null ? '—' : `${Math.round(p * 100)} %`}</td>
                    <td className="mono" title={r.resolved ? undefined : fmtIn(target)}>
                      {fmtDateTime(target)}
                      {!r.resolved && <span className="tiny muted"> · {fmtIn(target)}</span>}
                    </td>
                    <td className={`num mono ${signTone(r.outcome_return_pct)}`}>{r.resolved ? fmtPct(r.outcome_return_pct) : '—'}</td>
                    <td>
                      <Outcome row={r} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  )
}

export default memo(PredictionsTable)
