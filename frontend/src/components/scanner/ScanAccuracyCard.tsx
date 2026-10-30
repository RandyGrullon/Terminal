// "Precisión del escáner": honest out-of-sample accuracy of the scanner's logged predictions, plus a short Spanish
// explanation of the feedback loop (predict → wait → verify → learn).
import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { GraduationCap, RefreshCw, Target } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { PredictionStats } from '../../api/types'
import { fmtAgo, fmtInt, fmtNum } from '../../lib/format'
import { Card, ErrorBox, IconButton, Loading, Spinner, Stat } from '../ui'
import { brierTone, fmtRate, isNum, rateTone, useInterval } from '../markets/opsUtils'
import HitRateSparkline from './HitRateSparkline'

const REFRESH_MS = 60_000
const SMALL_SAMPLE = 30

function ScanAccuracyCard({ reloadKey }: { reloadKey: number }) {
  const [stats, setStats] = useState<PredictionStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loadedAt, setLoadedAt] = useState<number | null>(null)
  const alive = useRef(true)
  const seq = useRef(0)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const load = useCallback(async () => {
    const id = ++seq.current
    setLoading(true)
    try {
      const res = await api.predictions({ source: 'scanner', limit: 1 })
      if (!alive.current || id !== seq.current) return
      const s = res?.stats
      if (!s || typeof s !== 'object') throw new Error('Respuesta de predicciones no válida')
      setStats(s)
      setError(null)
      setLoadedAt(Date.now())
    } catch (err) {
      if (!alive.current || id !== seq.current) return
      setError(errorMessage(err))
    } finally {
      if (alive.current && id === seq.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load, reloadKey])
  useInterval(() => void load(), REFRESH_MS)

  const resolved = stats && isNum(stats.resolved) ? stats.resolved : 0
  return (
    <Card
      title={
        <>
          <Target size={14} /> Precisión del escáner
        </>
      }
      sub={loadedAt ? `Predicciones verificadas · act. ${fmtAgo(loadedAt)}` : 'Predicciones verificadas'}
      actions={<IconButton icon={loading && stats ? <Spinner /> : <RefreshCw size={14} />} title="Actualizar" size="sm" onClick={() => void load()} disabled={loading} />}
    >
      {!stats ? (
        error ? <ErrorBox error={error} onRetry={() => void load()} /> : <Loading label="Cargando precisión…" />
      ) : (
        <div className="col gap-2">
          <div className="scn-acc-stats">
            <Stat
              label="Acierto"
              value={fmtRate(stats.hit_rate)}
              tone={rateTone(stats.hit_rate)}
              hint="fuera de muestra"
              title="Porcentaje de predicciones resueltas cuya dirección fue correcta"
            />
            <Stat
              label="Brier"
              value={isNum(stats.brier) ? fmtNum(stats.brier, 3) : '—'}
              tone={brierTone(stats.brier)}
              hint="0 perfecto · 0,25 azar"
              title="Error cuadrático de la probabilidad P↑: cuanto más bajo, mejor calibrado"
            />
            <Stat label="Resueltas" value={fmtInt(stats.resolved)} hint={`de ${fmtInt(stats.total)} registradas`} />
            <Stat label="Pendientes" value={fmtInt(stats.pending)} hint="esperan su horizonte" />
          </div>
          <HitRateSparkline points={Array.isArray(stats.rolling) ? stats.rolling : []} />
          {resolved < SMALL_SAMPLE && (
            <div className="tiny warn">
              Muestra pequeña ({fmtInt(resolved)} resueltas): las métricas aún no son fiables. Se estabilizan con cientos de predicciones.
            </div>
          )}
          {error && <div className="tiny neg">{error}</div>}
        </div>
      )}
    </Card>
  )
}

export default memo(ScanAccuracyCard)

/** Short explanation of how the scanner learns from its own verified predictions. */
export const ScanHelpCard = memo(function ScanHelpCard() {
  return (
    <Card
      title={
        <>
          <GraduationCap size={14} /> Cómo aprende el escáner
        </>
      }
    >
      <ol className="scn-help">
        <li>
          <b>Predice antes de saber.</b> Cada activo escaneado deja registrada su probabilidad de subida (P↑) y su score en el momento del
          escaneo, antes de conocer el resultado.
        </li>
        <li>
          <b>Verifica después.</b> Cuando vence el horizonte (por defecto 4 h), el evaluador compara con el precio real y marca la predicción
          como acierto o fallo.
        </li>
        <li>
          <b>Aprende del resultado.</b> El modelo de cada mercado y temporalidad se actualiza con ese desenlace: las señales que acertaron
          ganan peso y las que fallaron lo pierden.
        </li>
        <li>
          <b>Modo profundo.</b> Los mejores candidatos pasan por NEXUS completo y por el filtro (Trade Gate), que decide TOMAR, ESPERAR o NO
          TOMAR y propone el SL «hasta aquí» y los objetivos.
        </li>
      </ol>
      <p className="tiny muted" style={{ margin: '8px 0 0' }}>
        Las métricas de precisión son honestas: solo cuentan predicciones registradas antes del resultado. Ningún modelo garantiza aciertos.
      </p>
    </Card>
  )
})
