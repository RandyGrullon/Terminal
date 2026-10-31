// Reliability diagram: predicted probability (x) vs. observed frequency (y). Points on the diagonal mean the
// model's probabilities can be taken at face value; point size grows with the number of predictions in the bin.
import { memo, useMemo } from 'react'
import type { CalibrationBin } from '../../api/types'
import { fmtNum } from '../../lib/format'
import { plural } from '../../lib/plural'
import { prob01 } from './learnFormat'

const W = 300
const H = 260
const M = { top: 12, right: 12, bottom: 38, left: 44 }
const PW = W - M.left - M.right
const PH = H - M.top - M.bottom
const TICKS = [0, 0.25, 0.5, 0.75, 1]

const x = (p: number) => M.left + p * PW
const y = (o: number) => M.top + (1 - o) * PH

function CalibrationChart({ bins, compact }: { bins: CalibrationBin[]; compact?: boolean }) {
  const pts = useMemo(() => {
    const out: { p: number; o: number; n: number }[] = []
    for (const b of bins ?? []) {
      const p = prob01(b?.predicted)
      const o = prob01(b?.observed)
      const n = typeof b?.count === 'number' && Number.isFinite(b.count) ? b.count : 0
      if (p === null || o === null || n <= 0) continue
      out.push({ p: Math.min(1, Math.max(0, p)), o: Math.min(1, Math.max(0, o)), n })
    }
    return out.sort((a, b) => a.p - b.p)
  }, [bins])

  const total = pts.reduce((s, p) => s + p.n, 0)
  const maxN = pts.reduce((m, p) => Math.max(m, p.n), 0)
  // Expected calibration error (weighted mean |predicted - observed|), in percentage points.
  const ece = total ? (pts.reduce((s, p) => s + p.n * Math.abs(p.p - p.o), 0) / total) * 100 : null

  if (!pts.length) {
    return (
      <div className="brain-cal-empty">
        <span className="muted small">Sin datos de calibración todavía: se necesitan predicciones resueltas.</span>
      </div>
    )
  }

  return (
    <div className="brain-cal">
      <svg viewBox={`0 0 ${W} ${H}`} className="brain-cal-svg" role="img" aria-label="Diagrama de calibración">
        {TICKS.map((t) => (
          <g key={t}>
            <line className="brain-cal-grid" x1={x(t)} x2={x(t)} y1={M.top} y2={M.top + PH} />
            <line className="brain-cal-grid" x1={M.left} x2={M.left + PW} y1={y(t)} y2={y(t)} />
            <text className="brain-cal-tick" x={x(t)} y={M.top + PH + 14} textAnchor="middle">
              {Math.round(t * 100)}
            </text>
            <text className="brain-cal-tick" x={M.left - 6} y={y(t) + 3.5} textAnchor="end">
              {Math.round(t * 100)}
            </text>
          </g>
        ))}
        <rect className="brain-cal-frame" x={M.left} y={M.top} width={PW} height={PH} />
        <line className="brain-cal-diag" x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} />
        {/* Legend in the top-left corner (low predicted / high observed is rarely populated). */}
        <line className="brain-cal-diag" x1={x(0.03)} y1={y(0.95)} x2={x(0.11)} y2={y(0.95)} />
        <text className="brain-cal-note" x={x(0.13)} y={y(0.95) + 3.5} textAnchor="start">
          calibración perfecta
        </text>
        <polyline className="brain-cal-line" points={pts.map((p) => `${x(p.p)},${y(p.o)}`).join(' ')} />
        {pts.map((p, i) => {
          const r = 3 + 7 * Math.sqrt(maxN ? p.n / maxN : 0)
          const off = p.o - p.p
          return (
            <circle key={i} className={`brain-cal-dot ${Math.abs(off) > 0.1 ? 'off' : ''}`} cx={x(p.p)} cy={y(p.o)} r={r}>
              <title>
                {`Predicho ${fmtNum(p.p * 100, 0)} % · observado ${fmtNum(p.o * 100, 0)} % · ${plural(p.n, 'predicción', 'predicciones')}`}
              </title>
            </circle>
          )
        })}
        <text className="brain-cal-axis" x={M.left + PW / 2} y={H - 6} textAnchor="middle">
          Probabilidad predicha (%)
        </text>
        <text className="brain-cal-axis" transform={`translate(12 ${M.top + PH / 2}) rotate(-90)`} textAnchor="middle">
          Frecuencia observada (%)
        </text>
      </svg>
      {!compact && (
        <div className="tiny muted brain-cal-foot">
          {plural(total, 'predicción', 'predicciones')} en {plural(pts.length, 'grupo', 'grupos')}
          {ece !== null && (
            <>
              {' '}
              · error medio de calibración <strong className={ece <= 5 ? 'pos' : ece <= 10 ? 'warn' : 'neg'}>{fmtNum(ece, 1)} pp</strong>
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default memo(CalibrationChart)
