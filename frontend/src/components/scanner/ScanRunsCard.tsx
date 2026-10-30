// Scan run history: pick any past run to show its results (the newest is shown by default).
import { memo } from 'react'
import { History, Layers, RefreshCw } from 'lucide-react'
import type { MarketInfo, ScanRun } from '../../api/types'
import { fmtAgo, fmtDateTime, fmtDuration, fmtInt } from '../../lib/format'
import { Badge, Button, Card, Empty, ErrorBox, IconButton, Loading, Spinner } from '../ui'
import { isNum, marketLabel } from '../markets/opsUtils'
import { runStatusInfo } from './scanUtils'

interface Props {
  runs: ScanRun[]
  loading: boolean
  error: string | null
  markets: MarketInfo[]
  /** Run currently displayed in the table. */
  shownId: number | null
  /** True when the table follows the newest run automatically. */
  followLatest: boolean
  onSelect: (id: number) => void
  onLatest: () => void
  onRefresh: () => void
}

function duration(run: ScanRun): string {
  if (!isNum(run.finished_at) || !isNum(run.started_at)) return ''
  const s = Math.max(0, Math.round((run.finished_at - run.started_at) / 1000))
  return s < 60 ? `${s} s` : fmtDuration(s)
}

function ScanRunsCard({ runs, loading, error, markets, shownId, followLatest, onSelect, onLatest, onRefresh }: Props) {
  return (
    <Card
      title={
        <>
          <History size={14} /> Historial de escaneos
        </>
      }
      sub={followLatest ? 'Mostrando el más reciente' : 'Mostrando un escaneo anterior'}
      actions={
        <>
          {!followLatest && (
            <Button size="sm" onClick={onLatest} title="Volver al escaneo más reciente">
              Último
            </Button>
          )}
          <IconButton icon={loading ? <Spinner /> : <RefreshCw size={14} />} title="Actualizar historial" size="sm" onClick={onRefresh} disabled={loading} />
        </>
      }
      flush
    >
      {error && runs.length === 0 ? (
        <div style={{ padding: 12 }}>
          <ErrorBox error={error} onRetry={onRefresh} />
        </div>
      ) : loading && runs.length === 0 ? (
        <Loading label="Cargando historial…" />
      ) : runs.length === 0 ? (
        <Empty icon={<History size={22} />} title="Sin escaneos todavía">
          Lanza el primero con «Escanear».
        </Empty>
      ) : (
        <ul className="scn-runs">
          {runs.map((run) => {
            const st = runStatusInfo(run.status)
            const active = run.id === shownId
            const dur = duration(run)
            return (
              <li
                key={run.id}
                className={`scn-run ${active ? 'active' : ''}`}
                role="button"
                tabIndex={0}
                aria-pressed={active}
                title={run.message ?? undefined}
                onClick={() => onSelect(run.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelect(run.id)
                  }
                }}
              >
                <span className="scn-run-id">#{run.id}</span>
                <span className="scn-run-main">
                  <b className="ellipsis">{marketLabel(run.market, markets)}</b>
                  <span className="mono text-2">{run.tf}</span>
                  {run.deep && (
                    <span title="Con análisis profundo (NEXUS + filtro)" className="row" style={{ color: 'var(--purple)' }}>
                      <Layers size={12} />
                    </span>
                  )}
                </span>
                <span className="row gap-1">
                  {run.status === 'running' && <Spinner />}
                  <Badge tone={st.tone}>{st.label}</Badge>
                </span>
                <span className="scn-run-sub">
                  {fmtInt(run.n_symbols)} activos · {fmtDateTime(run.started_at)} ({fmtAgo(run.started_at)}){dur ? ` · ${dur}` : ''}
                  {run.status === 'error' && run.message ? ` · ${run.message}` : ''}
                </span>
              </li>
            )
          })}
        </ul>
      )}
      {error && runs.length > 0 && <div className="tiny neg" style={{ padding: '6px 12px' }}>{error}</div>}
    </Card>
  )
}

export default memo(ScanRunsCard)
