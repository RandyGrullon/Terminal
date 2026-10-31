// System status: version, uptime and every backend service with its state (/api/health).
import { memo, useCallback, useEffect, useState } from 'react'
import { CircleCheck, CircleX, CirclePause, RefreshCw, Server, TriangleAlert } from 'lucide-react'
import { api } from '../../api/client'
import type { Health } from '../../api/types'
import { fmtDateTime, fmtDuration, fmtNum } from '../../lib/format'
import { Card, ErrorBox, IconButton, Loading } from '../ui'

/** Spanish name of each backend service id of /api/health (backend/app/main.py SERVICES, plus ws and auth). */
/** Services without a background loop: idle (not "detenido") until the user starts a job. */
const ON_DEMAND = new Set(['validation'])

export const SERVICE_LABEL: Record<string, string> = {
  hub: 'Datos de mercado',
  macro: 'Semáforo de mercado',
  nexus: 'Motor NEXUS',
  scanner: 'Escáner',
  paper: 'Cuenta demo',
  evaluator: 'Verificador de predicciones',
  sim: 'Simulador',
  scripts: 'PyChart',
  ws: 'Tiempo real (WebSocket)',
  maintenance: 'Mantenimiento',
  alerts: 'Alertas externas',
  validation: 'Validación walk-forward',
  auth: 'Autenticación',
}

function short(v: unknown): string | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'boolean') return v ? 'sí' : 'no'
  if (typeof v === 'number') return Number.isFinite(v) ? (Number.isInteger(v) ? String(v) : fmtNum(v, 2)) : null
  if (typeof v === 'string') return v.length > 48 ? `${v.slice(0, 45)}…` : v
  if (Array.isArray(v)) return `${v.length} elem.`
  return null
}

function details(status: Record<string, unknown>): { text: string; full: string } {
  const parts: string[] = []
  for (const [k, v] of Object.entries(status)) {
    if (k === 'running' || k === 'error') continue
    const s = short(v)
    if (s !== null && s !== '') parts.push(`${k.replace(/_/g, ' ')}: ${s}`)
    if (parts.length >= 4) break
  }
  let full = ''
  try {
    full = JSON.stringify(status, null, 2)
  } catch {
    full = ''
  }
  return { text: parts.join(' · '), full }
}

function SystemCard({ onHealth }: { onHealth?: (h: Health | null) => void }) {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const h = await api.health()
      setHealth(h)
      setError(null)
      onHealth?.(h)
    } catch (e) {
      setError(e)
      onHealth?.(null)
    } finally {
      setLoading(false)
    }
  }, [onHealth])

  useEffect(() => {
    void load()
  }, [load])

  const services = Object.entries(health?.services ?? {}).sort(([a], [b]) => (SERVICE_LABEL[a] ?? a).localeCompare(SERVICE_LABEL[b] ?? b, 'es'))
  const loadErrors = Array.isArray(health?.load_errors) ? health.load_errors : []

  return (
    <Card
      title={
        <>
          <Server size={14} /> Sistema
        </>
      }
      sub={health ? `NEXUS Terminal v${health.version}` : 'Estado del servidor local'}
      actions={<IconButton size="sm" icon={<RefreshCw size={13} />} title="Actualizar" onClick={() => void load()} disabled={loading} />}
    >
      {error !== null && !health ? (
        <ErrorBox error={error} onRetry={() => void load()} />
      ) : !health ? (
        <Loading label="Consultando el servidor…" />
      ) : (
        <div className="col gap-2">
          {error !== null && <ErrorBox error={error} onRetry={() => void load()} />}
          <div className="kv">
            <span>Estado</span>
            <span className={health.ok ? 'pos' : 'neg'}>{health.ok ? 'Operativo' : 'Con problemas'}</span>
            <span>Versión</span>
            <span className="mono">{health.version}</span>
            <span>En marcha desde hace</span>
            <span>{fmtDuration(health.uptime_s)}</span>
            <span>Hora del servidor</span>
            <span>{fmtDateTime(health.server_time)}</span>
          </div>
          <div className="divider" />
          <div className="set-services">
            {services.map(([id, st]) => {
              const status = (st && typeof st === 'object' ? st : {}) as Record<string, unknown>
              const err = typeof status.error === 'string' && status.error ? status.error : null
              const running = status.running === true
              const d = details(status)
              return (
                <div key={id} className="set-service" title={d.full}>
                  {err ? (
                    <CircleX size={14} className="neg" />
                  ) : running ? (
                    <CircleCheck size={14} className="pos" />
                  ) : (
                    <CirclePause size={14} className="warn" />
                  )}
                  <div className="col gap-0 grow" style={{ minWidth: 0 }}>
                    <span className="small">
                      {SERVICE_LABEL[id] ?? id} <span className="tiny muted mono">{id}</span>
                    </span>
                    {err ? (
                      <span className="tiny neg set-wrap">{err}</span>
                    ) : (
                      <span className="tiny muted ellipsis">
                        {running
                          ? d.text || 'en marcha'
                          : ON_DEMAND.has(id)
                            ? 'en espera · se ejecuta cuando lo pides'
                            : `detenido${d.text ? ` · ${d.text}` : ''}`}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
            {!services.length && <span className="small muted">El servidor no informó servicios.</span>}
          </div>
          {loadErrors.length > 0 && (
            <div className="notice">
              <TriangleAlert size={15} style={{ flex: 'none', marginTop: 1 }} />
              <div className="col gap-1" style={{ minWidth: 0 }}>
                <strong>Módulos que no se pudieron cargar ({loadErrors.length})</strong>
                {loadErrors.map((e, i) => (
                  <span key={i} className="mono tiny set-wrap">
                    {e}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

export default memo(SystemCard)
