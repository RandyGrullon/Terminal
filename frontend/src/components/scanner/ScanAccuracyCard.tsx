// "Precisión del escáner": honest out-of-sample accuracy of the scanner's logged predictions compared with its
// reference (three outcomes: always predicting the most frequent one, never a fixed 50 %) and the Brier score with
// climatology, plus a short Spanish explanation of the feedback loop (predict → wait → verify → learn).
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { GraduationCap, RefreshCw, Target } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { PredictionStats } from '../../api/types'
import { fmtAgo, fmtInt, fmtNum, fmtProb } from '../../lib/format'
import { Card, ErrorBox, IconButton, Loading, Spinner, Stat } from '../ui'
import { fmtRate, isNum, useInterval } from '../markets/opsUtils'
import { MIN_WINDOWS, bssTone, completeDays, fmtBss, fmtOfN, sameRowsDay, scannerGroup, skillTone, useSkillReport } from '../brain/skill'
import HitRateSparkline, { type RollingPoint } from './HitRateSparkline'

const REFRESH_MS = 60_000

function ScanAccuracyCard({ reloadKey }: { reloadKey: number }) {
  const [stats, setStats] = useState<PredictionStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loadedAt, setLoadedAt] = useState<number | null>(null)
  const [tick, setTick] = useState(0)
  const alive = useRef(true)
  const seq = useRef(0)
  const skill = useSkillReport('7', reloadKey * 1000 + tick, 'scanner')

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

  const refresh = useCallback(() => {
    void load()
    setTick((t) => t + 1)
  }, [load])

  useEffect(() => {
    void load()
  }, [load, reloadKey])
  useInterval(refresh, REFRESH_MS)

  const g = scannerGroup(skill.report)
  const points = useMemo<RollingPoint[]>(() => {
    // only the days the 7-day report covers entirely with the same predictions (its first day starts mid-day)
    const refs = completeDays(skill.report, g)
    return (Array.isArray(stats?.rolling) ? stats.rolling : []).map((r) => ({
      ...r,
      reference: sameRowsDay(refs, r.time, r.n)?.reference ?? null,
    }))
  }, [stats, g, skill.report])

  const hasSkill = !!g && g.n > 0
  return (
    <Card
      title={
        <>
          <Target size={14} /> Precisión del escáner
        </>
      }
      sub={loadedAt ? `Predicciones verificadas · act. ${fmtAgo(loadedAt)}` : 'Predicciones verificadas'}
      actions={<IconButton icon={loading && stats ? <Spinner /> : <RefreshCw size={14} />} title="Actualizar" size="sm" onClick={refresh} disabled={loading} />}
    >
      {!stats ? (
        error ? <ErrorBox error={error} onRetry={() => void load()} /> : <Loading label="Cargando precisión…" />
      ) : (
        <div className="col gap-2">
          <div className="scn-acc-stats">
            <Stat
              label="Acierto"
              value={hasSkill ? fmtRate(g.accuracy) : fmtRate(stats.hit_rate)}
              tone={skillTone(g)}
              hint={hasSkill ? `${fmtOfN(g.hits, g.n)} · últimos 7 días` : 'fuera de muestra · histórico'}
              title="Predicciones resueltas cuyo resultado coincidió: subida, bajada o movimiento dentro de la banda neutral"
            />
            <Stat
              label="Referencia"
              value={hasSkill && g.reference ? fmtProb(g.reference.accuracy, 1) : '—'}
              hint={hasSkill && g.reference ? g.reference.label : 'resultado más frecuente del periodo'}
              title="Lo que habría acertado quien dijera siempre el resultado más frecuente del mismo periodo (se conoce a posteriori)"
            />
            <Stat
              label="Brier"
              value={isNum(hasSkill ? g.brier : stats.brier) ? fmtNum(hasSkill ? g.brier : stats.brier, 3) : '—'}
              tone={bssTone(g)}
              hint={hasSkill && isNum(g.brier_ref) ? `climatología ${fmtNum(g.brier_ref, 3)} · habilidad ${fmtBss(g.bss)}` : 'menor es mejor'}
              title="Error cuadrático de P↑ frente al de la climatología (decir siempre la frecuencia de subida del periodo)"
            />
            <Stat label="Resueltas" value={fmtInt(stats.resolved)} hint={`de ${fmtInt(stats.total)} registradas`} />
            <Stat label="Pendientes" value={fmtInt(stats.pending)} hint="esperan su horizonte" />
          </div>
          <HitRateSparkline points={points} />
          {hasSkill ? (
            <div className={`tiny ${skillTone(g) === 'muted' ? 'muted' : skillTone(g)}`}>{g.verdict_text}</div>
          ) : skill.unavailable ? (
            <div className="tiny muted">Este servidor no ofrece todavía la comparación con la referencia: con tres resultados posibles, el 50 % no es el azar.</div>
          ) : null}
          {hasSkill && g.windows < MIN_WINDOWS && (
            <div className="tiny warn">
              Muestra pequeña: {fmtInt(g.windows)} de {MIN_WINDOWS} ventanas independientes necesarias. Las predicciones de un mismo escaneo se
              mueven juntas: {fmtInt(g.n)} resueltas en 7 días no son {fmtInt(g.n)} pruebas independientes.
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
          <b>Predice antes de saber.</b> Cada activo escaneado deja registrada su predicción en el momento del escaneo: sube, baja o neutral
          (se espera un movimiento menor que la banda neutral, ±0,5 % a 4 h), con su probabilidad de subida (P↑).
        </li>
        <li>
          <b>Lee el mercado entero.</b> Usa el contexto del escaneo (amplitud a 1 h, 4 h y 1 día, tendencia de BTC/ETH, dispersión, régimen) y
          compara cada activo con la media de su escaneo, así una subida de todo el mercado no se cuenta como 150 lecciones distintas.
        </li>
        <li>
          <b>Verifica después.</b> Cuando vence el horizonte (por defecto 4 h), el evaluador compara con el precio real: acierto o fallo.
        </li>
        <li>
          <b>Aprende del resultado.</b> Cada escaneo es una lección para la parte de mercado y las diferencias entre activos lo son para la
          parte de cada activo; la probabilidad se recalibra (Platt) cuando hay muestra suficiente.
        </li>
        <li>
          <b>Modo profundo.</b> Los mejores candidatos pasan por NEXUS completo y por el filtro (Trade Gate), que decide TOMAR, ESPERAR o NO
          TOMAR y propone el SL «hasta aquí» y los objetivos. Un TOMAR solo cuenta como oportunidad cuando NEXUS también da la señal de
          entrada en esa dirección; si no, aparece como «Filtro favorable · sin señal NEXUS».
        </li>
      </ol>
      <p className="tiny muted" style={{ margin: '8px 0 0' }}>
        Las métricas son honestas: solo cuentan predicciones registradas antes del resultado y se comparan con la referencia (acertar siempre el
        resultado más frecuente del periodo), no con un 50 %. Ningún modelo garantiza aciertos.
      </p>
    </Card>
  )
})
