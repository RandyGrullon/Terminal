// Inline SVG sparkline of the scanner's rolling hit rate (resolved predictions per day) with the 50 % coin-flip line
// and faint bars for the sample size of each point (hover a point for its exact numbers).
import { memo, useId, useMemo } from 'react'
import { fmtBarTime, fmtInt, fmtProb } from '../../lib/format'
import { isNum } from '../markets/opsUtils'

export interface RollingPoint {
  time: number
  hit_rate: number
  n: number
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
        .map((p) => ({ time: p.time, hit: Math.max(0, Math.min(1, p.hit_rate)), n: isNum(p.n) ? Math.max(0, p.n) : 0 }))
        .sort((a, b) => a.time - b.time),
    [points],
  )

  const geo = useMemo(() => {
    if (data.length < 2) return null
    const vals = data.map((d) => d.hit)
    const lo = Math.max(0, Math.min(0.35, Math.min(...vals) - 0.05))
    const hi = Math.min(1, Math.max(0.75, Math.max(...vals) + 0.05))
    const step = W / data.length
    const x = (i: number) => step * i + step / 2
    const y = (v: number) => PAD_Y + (1 - (v - lo) / (hi - lo)) * (H - 2 * PAD_Y)
    const line = data.map((d, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${y(d.hit).toFixed(2)}`).join(' ')
    const area = `${line} L${x(data.length - 1).toFixed(2)},${H} L${x(0).toFixed(2)},${H} Z`
    const maxN = Math.max(1, ...data.map((d) => d.n))
    return { step, x, y50: y(0.5), line, area, maxN }
  }, [data])

  if (!geo) {
    return <div className="tiny muted">Aún no hay suficientes predicciones resueltas para dibujar la curva de acierto.</div>
  }
  const last = data[data.length - 1]
  const lastTone = last.hit >= 0.55 ? 'var(--pos)' : last.hit < 0.5 ? 'var(--neg)' : 'var(--warn)'

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
        <line x1={0} x2={W} y1={geo.y50} y2={geo.y50} stroke="var(--muted)" strokeDasharray="4 3" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        <path d={geo.line} fill="none" stroke="var(--accent-2)" strokeWidth={1.6} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        {data.map((d, i) => (
          <rect key={`h${d.time}`} x={geo.x(i) - geo.step / 2} width={geo.step} y={0} height={H} fill="transparent">
            <title>{`${fmtBarTime(d.time, '1d')}: acierto ${fmtProb(d.hit, 1)} · ${fmtInt(d.n)} predicciones`}</title>
          </rect>
        ))}
      </svg>
      <div className="scn-spark-legend">
        <span>{fmtBarTime(data[0].time, '1d')}</span>
        <span>
          línea discontinua = 50 % (azar) · último: <b style={{ color: lastTone }}>{fmtProb(last.hit, 1)}</b>
        </span>
        <span>{fmtBarTime(last.time, '1d')}</span>
      </div>
    </div>
  )
}

export default memo(HitRateSparkline)
