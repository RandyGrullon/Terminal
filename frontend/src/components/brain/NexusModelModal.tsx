// Detail of one NEXUS model (symbol + timeframe): what it learned per market regime (expert weights heatmap),
// how much each Trade Gate check matters, calibration and rolling out-of-sample accuracy.
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { NexusModel, Timeframe } from '../../api/types'
import { REGIME_LABEL, fmtBarTime, fmtInt, fmtNum, type Tone } from '../../lib/format'
import { ErrorBox, Loading, Modal, Stat } from '../ui'
import CalibrationChart from './CalibrationChart'
import TimelineChart from './TimelineChart'
import WeightBars from './WeightBars'
import { GATE_CHECK_LABEL, REGIME_ORDER, brierTone, fmtBrier, fmtRate, prettyKey, rateTone } from './learnFormat'

interface Props {
  target: { symbol: string; tf: string } | null
  onClose: () => void
}

function Heatmap({ model }: { model: NexusModel }) {
  const { regimes, experts, max } = useMemo(() => {
    const weights = model.learning?.weights ?? {}
    const regimeKeys = Object.keys(weights).sort((a, b) => {
      const ia = REGIME_ORDER.indexOf(a)
      const ib = REGIME_ORDER.indexOf(b)
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b)
    })
    const meta = new Map((model.experts ?? []).map((e) => [e.id, e]))
    const ids = new Set<string>()
    for (const r of regimeKeys) for (const id of Object.keys(weights[r] ?? {})) ids.add(id)
    let mx = 0
    for (const r of regimeKeys) for (const v of Object.values(weights[r] ?? {})) if (Number.isFinite(v)) mx = Math.max(mx, Math.abs(v))
    // Experts ordered by their average weight across regimes (most trusted first).
    const avg = (id: string) => regimeKeys.reduce((s, r) => s + (weights[r]?.[id] ?? 0), 0) / Math.max(1, regimeKeys.length)
    const list = Array.from(ids)
      .sort((a, b) => avg(b) - avg(a))
      .map((id) => ({ id, label: meta.get(id)?.label ?? prettyKey(id), description: meta.get(id)?.description ?? '' }))
    return { regimes: regimeKeys, experts: list, max: mx || 1 }
  }, [model])

  if (!regimes.length || !experts.length) return <div className="muted small">El modelo aún no tiene pesos por régimen.</div>
  const weights = model.learning.weights
  const best = new Map(
    regimes.map((r) => {
      let top = ''
      let tv = Number.NEGATIVE_INFINITY
      for (const e of experts) {
        const v = weights[r]?.[e.id]
        if (typeof v === 'number' && v > tv) {
          tv = v
          top = e.id
        }
      }
      return [r, top]
    }),
  )

  return (
    <div className="table-wrap brain-heat-wrap">
      <table className="table brain-heat">
        <thead>
          <tr>
            <th>Experto</th>
            {regimes.map((r) => (
              <th key={r} className="num">
                {REGIME_LABEL[r as keyof typeof REGIME_LABEL] ?? prettyKey(r)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {experts.map((e) => (
            <tr key={e.id}>
              <td title={e.description || e.id}>{e.label}</td>
              {regimes.map((r) => {
                const v = weights[r]?.[e.id]
                const ok = typeof v === 'number' && Number.isFinite(v)
                const k = ok ? Math.min(1, Math.abs(v) / max) : 0
                return (
                  <td
                    key={r}
                    className={`num mono ${best.get(r) === e.id ? 'bold' : ''}`}
                    style={{
                      background: ok ? `rgba(47, 107, 255, ${(0.06 + 0.7 * k).toFixed(3)})` : undefined,
                      color: k > 0.55 ? '#fff' : undefined,
                    }}
                    title={ok ? `${e.label} en ${REGIME_LABEL[r as keyof typeof REGIME_LABEL] ?? r}: ${fmtNum(v, 4)}` : 'sin dato'}
                  >
                    {ok ? fmtNum(v, 3) : '—'}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function NexusModelModal({ target, onClose }: Props) {
  const [model, setModel] = useState<NexusModel | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    if (!target) return
    setLoading(true)
    setError(null)
    try {
      const m = await api.nexusModel(target.symbol, target.tf as Timeframe)
      setModel(m)
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [target])

  useEffect(() => {
    setModel(null)
    if (target) void load()
  }, [target, load])

  const gateItems = useMemo(
    () =>
      Object.entries(model?.learning?.gate_weights ?? {}).map(([id, w]) => ({
        id,
        label: GATE_CHECK_LABEL[id] ?? prettyKey(id),
        weight: w,
      })),
    [model],
  )
  const gatePositive = gateItems.every((g) => !Number.isFinite(g.weight) || g.weight >= 0)
  const rolling = useMemo(() => (model?.rolling ?? []).map((r) => ({ time: r.time, hit_rate: r.hit_rate, brier: r.brier })), [model])

  const l = model?.learning
  const stats: { label: string; value: string; tone?: Tone; hint?: string }[] = l
    ? [
        { label: 'Actualizaciones', value: fmtInt(l.updates), hint: 'velas aprendidas' },
        { label: 'Acierto', value: fmtRate(l.hit_rate), tone: rateTone(l.hit_rate), hint: 'fuera de muestra' },
        { label: 'Brier', value: fmtBrier(l.brier), tone: brierTone(l.brier), hint: '0 perfecto · 0,25 azar' },
        { label: 'Memoria', value: fmtInt(l.memory_bars), hint: 'velas recordadas' },
        { label: 'Entrenado hasta', value: fmtBarTime(l.trained_until, target?.tf) },
      ]
    : []

  return (
    <Modal open={!!target} onClose={onClose} width={900} title={target ? `Modelo NEXUS · ${target.symbol} · ${target.tf}` : 'Modelo NEXUS'}>
      {loading && !model ? (
        <Loading label="Cargando modelo…" />
      ) : error ? (
        <ErrorBox error={error} onRetry={() => void load()} />
      ) : model ? (
        <div className="col gap-3">
          <div className="grid-auto">
            {stats.map((s) => (
              <Stat key={s.label} label={s.label} value={s.value} tone={s.tone} hint={s.hint} />
            ))}
          </div>
          <section className="col gap-2">
            <h4>Peso de cada experto según el régimen de mercado</h4>
            <p className="small muted brain-p">
              NEXUS combina varios “expertos” (tendencia, momento, volumen…). En cada régimen aprende a qué expertos hacer caso: cuanto
              más intenso el color, más pesa su opinión. En negrita, el experto más fiable de cada régimen.
            </p>
            <Heatmap model={model} />
          </section>
          <div className="brain-modal-grid">
            <section className="col gap-2">
              <h4>Qué revisa el filtro antes de dejarte entrar</h4>
              <p className="small muted brain-p">
                Importancia aprendida de cada comprobación del filtro “¿Tomo la operación?”, entrenada con operaciones ya cerradas.
              </p>
              <WeightBars
                items={gateItems}
                positiveOnly={gatePositive}
                posHint="favorece TOMAR"
                negHint="favorece NO TOMAR"
                limit={13}
              />
            </section>
            <section className="col gap-2">
              <h4>Calibración</h4>
              <CalibrationChart bins={model.calibration ?? []} />
            </section>
          </div>
          <section className="col gap-2">
            <h4>Acierto y Brier recientes (ventana móvil)</h4>
            <div className="brain-chart-frame">
              <TimelineChart points={rolling} height={240} tf={target?.tf} />
            </div>
          </section>
        </div>
      ) : null}
    </Modal>
  )
}
