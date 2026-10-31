// "Aprendizaje": how NEXUS learns from its own verified predictions (feedback loop), with honest out-of-sample
// statistics, calibration, what the models weigh and the full prediction log.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Brain, RefreshCw, ShieldCheck, Telescope } from 'lucide-react'
import { api, errorMessage } from '../api/client'
import type { LearningSummary, Timeframe } from '../api/types'
import { socket } from '../api/ws'
import { fmtClock, fmtInt } from '../lib/format'
import { plural } from '../lib/plural'
import { useApp } from '../store'
import { Button, Card, Disclaimer, ErrorBox, Loading, Modal, Stat } from '../components/ui'
import CalibrationChart from '../components/brain/CalibrationChart'
import GateLearningCard from '../components/brain/GateLearningCard'
import LoopExplainer from '../components/brain/LoopExplainer'
import NexusModelModal from '../components/brain/NexusModelModal'
import NexusModelsTable from '../components/brain/NexusModelsTable'
import PredictionsTable from '../components/brain/PredictionsTable'
import TimelineChart from '../components/brain/TimelineChart'
import WeightBars from '../components/brain/WeightBars'
import { MIN_EVIDENCE_N, evidenceTone, filterEvidence, fmtOf } from '../components/brain/evidence'
import { brierTone, fmtBrier, fmtRate, prettyKey, prob01, rateTone } from '../components/brain/learnFormat'
import '../components/brain/brain.css'

const REFRESH_MS = 30000
const arr = <T,>(v: T[] | null | undefined): T[] => (Array.isArray(v) ? v : [])

function normalize(d: LearningSummary | null | undefined): LearningSummary {
  const p = d?.predictions
  return {
    scanner_model: d?.scanner_model
      ? {
          ...d.scanner_model,
          features: arr(d.scanner_model.features),
          calibration: arr(d.scanner_model.calibration),
        }
      : null,
    nexus_models: arr(d?.nexus_models).filter((m) => m && typeof m.symbol === 'string'),
    gate: {
      decisions: d?.gate?.decisions ?? 0,
      taken: d?.gate?.taken ?? 0,
      avoided: d?.gate?.avoided ?? 0,
      avoided_would_lose_rate: d?.gate?.avoided_would_lose_rate ?? null,
      taken_win_rate: d?.gate?.taken_win_rate ?? null,
      // Denominators of the two rates (verified decisions); absent on older servers.
      avoided_judged: d?.gate?.avoided_judged,
      taken_judged: d?.gate?.taken_judged,
    },
    predictions: {
      total: p?.total ?? 0,
      resolved: p?.resolved ?? 0,
      pending: p?.pending ?? 0,
      hit_rate: p?.hit_rate ?? null,
      brier: p?.brier ?? null,
      by_source: p?.by_source && typeof p.by_source === 'object' ? p.by_source : {},
      rolling: arr(p?.rolling),
    },
    timeline: arr(d?.timeline),
  }
}

type ModelRef = { symbol: string; tf: string }

const SOURCE_LABEL: Record<string, string> = { nexus: 'NEXUS (señales)', scanner: 'Escáner (mercado)' }

export default function BrainPage() {
  const notify = useApp((s) => s.notify)
  const [data, setData] = useState<LearningSummary | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [tick, setTick] = useState(0)
  const [viewTarget, setViewTarget] = useState<ModelRef | null>(null)
  const [resetTarget, setResetTarget] = useState<ModelRef | null>(null)
  const [resetting, setResetting] = useState(false)
  const seq = useRef(0)

  const load = useCallback(async () => {
    const my = ++seq.current
    setLoading(true)
    try {
      const d = await api.learning()
      if (my !== seq.current) return
      setData(normalize(d))
      setError(null)
      setUpdatedAt(Date.now())
    } catch (e) {
      if (my !== seq.current) return
      setError(e)
    } finally {
      if (my === seq.current) setLoading(false)
    }
  }, [])

  const refreshAll = useCallback(() => {
    void load()
    setTick((t) => t + 1)
  }, [load])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => {
      if (!document.hidden) refreshAll()
    }, REFRESH_MS)
    return () => window.clearInterval(id)
  }, [load, refreshAll])

  // A "learn" event means predictions were just verified: refresh shortly after (debounced).
  useEffect(() => {
    let timer: number | null = null
    const off = socket.on('event', ({ event }) => {
      if (event.level !== 'learn') return
      if (timer !== null) window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        timer = null
        refreshAll()
      }, 2000)
    })
    return () => {
      off()
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [refreshAll])

  const doReset = async () => {
    if (!resetTarget) return
    setResetting(true)
    try {
      await api.nexusReset(resetTarget.symbol, resetTarget.tf as Timeframe)
      notify(`Modelo ${resetTarget.symbol} ${resetTarget.tf} reiniciado: volverá a aprender desde cero`, 'success')
      setResetTarget(null)
      void load()
    } catch (e) {
      notify(`No se pudo reiniciar: ${errorMessage(e)}`, 'error')
    } finally {
      setResetting(false)
    }
  }

  const onView = useCallback((r: ModelRef) => setViewTarget({ symbol: r.symbol, tf: r.tf }), [])
  const onReset = useCallback((r: ModelRef) => setResetTarget({ symbol: r.symbol, tf: r.tf }), [])
  const closeView = useCallback(() => setViewTarget(null), [])

  const timeline = useMemo(() => arr(data?.timeline).map((t) => ({ time: t.time, hit_rate: t.hit_rate, brier: t.brier, n: t.n })), [data])
  const features = useMemo(
    () => arr(data?.scanner_model?.features).map((f) => ({ id: f.name, label: f.label || prettyKey(f.name), weight: f.weight })),
    [data],
  )
  const bySource = useMemo(() => Object.entries(data?.predictions.by_source ?? {}), [data])

  const header = (
    <div className="page-header">
      <Brain size={22} className="accent" />
      <div className="col gap-0">
        <h1>Aprendizaje: cómo NEXUS se retroalimenta</h1>
        <span className="sub">Predicciones registradas antes del resultado, verificadas después y usadas para mejorar los modelos.</span>
      </div>
      <div className="grow" />
      {updatedAt && <span className="tiny muted">Actualizado {fmtClock(updatedAt)} · se refresca cada 30 s</span>}
      <Button size="sm" icon={<RefreshCw size={14} />} onClick={refreshAll} loading={loading}>
        Actualizar
      </Button>
    </div>
  )

  if (!data) {
    return (
      <div className="page brain-page">
        {header}
        <Card>
          <LoopExplainer />
        </Card>
        {error ? <ErrorBox error={error} onRetry={() => void load()} /> : <Loading label="Cargando estadísticas de aprendizaje…" />}
      </div>
    )
  }

  const p = data.predictions
  const g = data.gate
  const sm = data.scanner_model
  // The filter's track record is only colored when it says something beyond noise (see brain/evidence.ts).
  const avoidedLose = prob01(g.avoided_would_lose_rate)
  const gateEvidence = filterEvidence({
    total: g.avoided,
    resolved: g.avoided_judged,
    winRate: avoidedLose === null ? null : 1 - avoidedLose,
    takenN: g.taken_judged,
    takenWinRate: prob01(g.taken_win_rate),
  })
  // "x de n verificadas" (outcome known), flagged as a small sample below MIN_EVIDENCE_N.
  const ofKnown = (t: typeof gateEvidence.taken, losses: boolean): string | null => {
    const s = fmtOf(t, losses, ['verificada', 'verificadas'])
    return s && t && t.n < MIN_EVIDENCE_N ? `${s} · muestra pequeña` : s
  }
  const takenOf = ofKnown(gateEvidence.taken, false)
  const avoidedOf = ofKnown(gateEvidence.rejected, true)
  const avoidedTitle =
    'No habrían llegado a TP1 antes del stop: pérdida o cierre por tiempo (no siempre una pérdida). Solo se colorea cuando ' +
    `hay al menos ${MIN_EVIDENCE_N} casos verificados y la diferencia con las tomadas supera el margen de error`
  const takenTitle = `Llegaron a TP1 antes del stop. Solo se colorea con al menos ${MIN_EVIDENCE_N} casos verificados`

  return (
    <div className="page brain-page">
      {header}
      {error !== null && (
        <ErrorBox error={`No se pudo actualizar (se muestran los últimos datos): ${errorMessage(error)}`} onRetry={() => void load()} />
      )}

      <Card title="Cómo funciona el ciclo de aprendizaje" sub="Sin mirar el futuro: primero se apuesta, luego se comprueba.">
        <LoopExplainer />
      </Card>

      <div className="grid-4">
        <Stat label="Predicciones totales" value={fmtInt(p.total)} hint="registradas antes del resultado" />
        <Stat label="Resueltas" value={fmtInt(p.resolved)} hint="verificadas con el precio real" />
        <Stat label="Pendientes" value={fmtInt(p.pending)} hint="esperando su horizonte" />
        <Stat label="Acierto" value={fmtRate(p.hit_rate)} tone={rateTone(p.hit_rate)} hint="fuera de muestra · 50 % = azar" />
        <Stat label="Brier" value={fmtBrier(p.brier)} tone={brierTone(p.brier)} hint="0 = perfecto · 0,25 = azar" />
        <Stat
          label="Decisiones del filtro"
          value={fmtInt(g.decisions)}
          hint={`${fmtInt(g.taken)} TOMAR · ${fmtInt(g.avoided)} NO TOMAR · resto ESPERAR`}
        />
        <Stat
          label="Evitadas sin llegar a TP1"
          value={fmtRate(g.avoided_would_lose_rate)}
          tone={evidenceTone(gateEvidence)}
          hint={avoidedOf ?? '% de las evitadas ya verificadas'}
          title={avoidedTitle}
        />
        <Stat
          label="Acierto de las tomadas"
          value={fmtRate(g.taken_win_rate)}
          tone={gateEvidence.taken && gateEvidence.taken.n >= MIN_EVIDENCE_N ? rateTone(g.taken_win_rate, 50, 40) : 'muted'}
          hint={takenOf ?? '% de las tomadas que llegaron a TP1'}
          title={takenTitle}
        />
      </div>

      <div className="brain-row">
        <Card
          title="Evolución del acierto"
          sub="Acierto y Brier de las predicciones resueltas a lo largo del tiempo"
          flush
          className="brain-main-card"
        >
          {/* One point per UTC day: drawn as a daily session (its UTC date), not shifted to the local clock. */}
          <TimelineChart points={timeline} height={280} tf="1d" />
          {bySource.length > 0 && (
            <div className="brain-sources">
              {bySource.map(([src, s]) => (
                <div key={src} className="brain-source">
                  <span className="text-2">{SOURCE_LABEL[src] ?? prettyKey(src)}</span>
                  <span className="mono">{plural(s?.resolved, 'resuelta', 'resueltas')}</span>
                  <span className={`mono ${rateTone(s?.hit_rate)}`}>acierto {fmtRate(s?.hit_rate)}</span>
                  <span className={`mono ${brierTone(s?.brier)}`}>Brier {fmtBrier(s?.brier)}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
        <Card
          title="Calibración del escáner"
          sub="¿Cuando dice 70 %, ocurre el 70 % de las veces?"
          actions={sm ? <span className="tiny muted mono">{sm.key}</span> : undefined}
        >
          {sm ? (
            <CalibrationChart bins={sm.calibration} />
          ) : (
            <div className="muted small">El modelo del escáner aún no se ha entrenado. Ejecuta un escaneo para empezar.</div>
          )}
        </Card>
      </div>

      <div className="brain-row even">
        <Card
          title={
            <>
              <ShieldCheck size={15} className="pos" /> Filtro “¿Tomo la operación?”
            </>
          }
          sub="Lo importante: cuándo NO entrar. Medido sobre decisiones ya verificadas."
        >
          <GateLearningCard gate={g} />
        </Card>
        <Card
          title={
            <>
              <Telescope size={15} className="info" /> Qué mira el modelo del escáner
            </>
          }
          sub={
            sm
              ? `${plural(sm.updates, 'actualización', 'actualizaciones')} · acierto ${fmtRate(sm.hit_rate)} · Brier ${fmtBrier(sm.brier)}`
              : 'Pesos aprendidos de cada indicador'
          }
        >
          {sm ? (
            <WeightBars items={features} posHint="empuja al alza" negHint="empuja a la baja" />
          ) : (
            <div className="muted small">Sin modelo del escáner todavía.</div>
          )}
        </Card>
      </div>

      <Card title="Modelos NEXUS por activo" sub="Cada símbolo y temporalidad aprende sus propios pesos y su calibración" flush>
        <NexusModelsTable rows={data.nexus_models} onView={onView} onReset={onReset} />
      </Card>

      <Card title="Registro de predicciones" sub="Cada fila se escribió antes de conocer el resultado; la verificación llega al vencer el objetivo">
        <PredictionsTable refreshTick={tick} />
      </Card>

      <Disclaimer />

      <NexusModelModal target={viewTarget} onClose={closeView} />

      <Modal
        open={!!resetTarget}
        onClose={() => setResetTarget(null)}
        title="Reiniciar modelo NEXUS"
        width={460}
        footer={
          <>
            <Button onClick={() => setResetTarget(null)}>Cancelar</Button>
            <Button variant="danger" loading={resetting} onClick={() => void doReset()}>
              Reiniciar
            </Button>
          </>
        }
      >
        <p style={{ margin: 0 }}>
          ¿Reiniciar el modelo de <strong className="mono">{resetTarget?.symbol}</strong> en <strong>{resetTarget?.tf}</strong>? Se borrará
          lo aprendido (pesos, calibración y memoria) y volverá a aprender desde cero con el historial disponible.
        </p>
      </Modal>
    </div>
  )
}
