// Inline SVG sparkline of the scanner's daily hit rate (resolved predictions per day) with, when known, the dashed
// reference of each day (always predicting that day's most frequent outcome: with three outcomes there is no fixed
// 50 % "coin flip") and faint bars for the sample size of each point (hover a point for its exact numbers).
import { memo, useId, useMemo } from 'react'
import { fmtBarTime, fmtInt, fmtProb } from '../../lib/format'
import { isNum } from '../markets/opsUtils'

export interface RollingPoint {
  time: number
  hit_rate: number
  n: number
  /** Hit rate of always predicting the most frequent outcome of the same day (0..1), when known. */
  reference?: number | null
}

const W = 300
const H = 76
const PAD_Y = 4
const BAR_H = 14

function HitRateSparkline({ points }: { points: RollingPoint[] }) {
  const gradId = `scn-spark-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const data = useMemo(
    () =>
      (Array.isArray(points) ? points : [])
        .filter((p) => p && isNum(p.time) && isNum(p.hit_rate))
        .map((p) => ({
          time: p.time,
          hit: Math.max(0, Math.min(1, p.hit_rate)),
          n: isNum(p.n) ? Math.max(0, p.n) : 0,
          ref: isNum(p.reference) ? Math.max(0, Math.min(1, p.reference)) : null,
        }))
        .sort((a, b) => a.time - b.time),
    [points],
  )

  const geo = useMemo(() => {
    if (data.length < 2) return null
    const vals = data.flatMap((d) => (d.ref === null ? [d.hit] : [d.hit, d.ref]))
    const lo = Math.max(0, Math.min(0.35, Math.min(...vals) - 0.05))
    const hi = Math.min(1, Math.max(0.75, Math.max(...vals) + 0.05))
    const step = W / data.length
    const x = (i: number) => step * i + step / 2
    const y = (v: number) => PAD_Y + (1 - (v - lo) / (hi - lo)) * (H - 2 * PAD_Y)
    const line = data.map((d, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${y(d.hit).toFixed(2)}`).join(' ')
    const area = `${line} L${x(data.length - 1).toFixed(2)},${H} L${x(0).toFixed(2)},${H} Z`
    let refLine = ''
    data.forEach((d, i) => {
      if (d.ref === null) return
      refLine += `${refLine && data[i - 1]?.ref !== null ? 'L' : 'M'}${x(i).toFixed(2)},${y(d.ref).toFixed(2)} `
    })
    const maxN = Math.max(1, ...data.map((d) => d.n))
    return { step, x, line, area, maxN, refLine: refLine.trim() }
  }, [data])

  if (!geo) {
    return <div className="tiny muted">Aún no hay suficientes predicciones resueltas para dibujar la curva de acierto.</div>
  }
  const last = data[data.length - 1]
  // Never coloured: one day is a handful of correlated horizon windows, far from the 30 a conclusion needs (the verdict
  // with its interval is shown next to the chart). Its reference is in the tooltip and the dashed line.
  const lastTone = 'var(--text)'

  return (
    <div className="scn-spark-wrap">
      <svg className="scn-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Acierto diario del escáner">
        <defs>
          <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="var(--accent-2)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--accent-2)" stopOpacity="0" />
          </linearGradient>
        </defs>
        {data.map((d, i) => {
          const h = (d.n / geo.maxN) * BAR_H
          return <rect key={`b${d.time}`} x={geo.x(i) - geo.step * 0.3} width={geo.step * 0.6} y={H - h} height={h} fill="var(--border-2)" opacity={0.6} />
        })}
        <path d={geo.area} fill={`url(#${gradId})`} />
        {geo.refLine && (
          <path d={geo.refLine} fill="none" stroke="var(--muted)" strokeDasharray="4 3" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        )}
        <path d={geo.line} fill="none" stroke="var(--accent-2)" strokeWidth={1.6} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        {data.map((d, i) => (
          <rect key={`h${d.time}`} x={geo.x(i) - geo.step / 2} width={geo.step} y={0} height={H} fill="transparent">
            <title>
              {`${fmtBarTime(d.time, '1d')}: acierto ${fmtProb(d.hit, 1)} · ${fmtInt(d.n)} predicciones` +
                (d.ref === null ? '' : ` · referencia ${fmtProb(d.ref, 1)}`)}
            </title>
          </rect>
        ))}
      </svg>
      <div className="scn-spark-legend">
        <span>{fmtBarTime(data[0].time, '1d')}</span>
        <span>
          {geo.refLine ? 'discontinua = referencia del día (resultado más frecuente)' : 'sin referencia diaria'} · último:{' '}
          <b style={{ color: lastTone }}>{fmtProb(last.hit, 1)}</b>
        </span>
        <span>{fmtBarTime(last.time, '1d')}</span>
      </div>
    </div>
  )
}

export default memo(HitRateSparkline)
