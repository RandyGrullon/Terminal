// Local database: path, size, rows per table and a "compact" (VACUUM) action.
import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { Archive, Database, RefreshCw } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { DbStats } from '../../api/types'
import { fmtBytes, fmtInt } from '../../lib/format'
import { useApp } from '../../store'
import { Button, Card, ErrorBox, IconButton, Loading } from '../ui'

const TABLE_LABEL: Record<string, string> = {
  meta: 'Metadatos',
  settings: 'Ajustes',
  symbols: 'Símbolos conocidos',
  candles: 'Velas (caché y grabador)',
  watchlist: 'Lista de seguimiento',
  model_state: 'Modelos aprendidos',
  signals: 'Señales',
  predictions: 'Predicciones',
  gate_log: 'Decisiones del filtro',
  scan_runs: 'Escaneos',
  scan_results: 'Resultados de escaneo',
  paper_accounts: 'Cuentas demo',
  paper_positions: 'Posiciones demo',
  paper_fills: 'Ejecuciones demo',
  events: 'Eventos',
  scripts: 'Scripts PyChart',
  backtests: 'Backtests',
}

function DbCard() {
  const notify = useApp((s) => s.notify)
  const [stats, setStats] = useState<DbStats | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [vacuuming, setVacuuming] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setStats(await api.db.stats())
      setError(null)
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const vacuum = async () => {
    const before = stats?.size_bytes ?? null
    setVacuuming(true)
    try {
      const r = await api.db.vacuum()
      notify(`Base de datos compactada: ${fmtBytes(before)} → ${fmtBytes(r?.size_bytes)}`, 'success')
      await load()
    } catch (e) {
      notify(`No se pudo compactar: ${errorMessage(e)}`, 'error')
    } finally {
      setVacuuming(false)
    }
  }

  const tables = useMemo(
    () => Object.entries(stats?.tables ?? {}).sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0) || a[0].localeCompare(b[0])),
    [stats],
  )
  const totalRows = tables.reduce((s, [, n]) => s + (Number.isFinite(n) ? n : 0), 0)
  const cloud = stats?.dialect === 'postgres'

  return (
    <Card
      title={
        <>
          <Database size={14} /> {cloud ? 'Base de datos en la nube' : 'Base de datos local'}
        </>
      }
      sub={cloud ? 'PostgreSQL de tu proyecto (Supabase): tus datos están en tu propia base' : 'SQLite en tu equipo: nada sale de él'}
      actions={<IconButton size="sm" icon={<RefreshCw size={13} />} title="Actualizar" onClick={() => void load()} disabled={loading} />}
    >
      {error !== null && !stats ? (
        <ErrorBox error={error} onRetry={() => void load()} />
      ) : !stats ? (
        <Loading label="Leyendo la base de datos…" />
      ) : (
        <div className="col gap-2">
          <div className="kv">
            <span>Archivo</span>
            <span className="mono ellipsis set-path" title={stats.path}>
              {stats.path}
            </span>
            <span>Tamaño</span>
            <span className="mono">{fmtBytes(stats.size_bytes)}</span>
            <span>Filas totales</span>
            <span className="mono">{fmtInt(totalRows)}</span>
          </div>
          <div className="table-wrap set-tables">
            <table className="table">
              <thead>
                <tr>
                  <th>Tabla</th>
                  <th className="num">Filas</th>
                </tr>
              </thead>
              <tbody>
                {tables.map(([name, n]) => (
                  <tr key={name}>
                    <td>
                      <span className="small">{TABLE_LABEL[name] ?? name}</span> <span className="tiny muted mono">{name}</span>
                    </td>
                    <td className="num mono">{fmtInt(n)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Button icon={<Archive size={14} />} onClick={() => void vacuum()} loading={vacuuming} title="Reorganiza el archivo y libera el espacio no usado">
            Compactar base de datos
          </Button>
          <span className="tiny muted">
            Compactar reescribe el archivo para recuperar espacio tras borrar datos. Puede tardar unos segundos con muchas velas guardadas.
          </span>
        </div>
      )}
    </Card>
  )
}

export default memo(DbCard)
