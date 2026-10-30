// Pure SVG histogram with axis labels, reference lines and hover read-out (Monte Carlo distributions).
import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { buildBins, clean, niceTicks } from './stats'

export interface RefLine {
  value: number
  label: string
  color: string
  dashed?: boolean
}

interface Props {
  values: number[]
  formatX: (v: number) => string
  /** Bar colour for the bin [lo, hi). */
  colorFor?: (lo: number, hi: number) => string
  refLines?: RefLine[]
  /** Unit of the counts, plural ("trayectorias"). */
  unit: string
  ariaLabel: string
  height?: number
  /** Desired number of bins (auto from the sample size when omitted). */
  target?: number
}

const M = { left: 40, right: 12, top: 34, bottom: 26 }

const autoTarget = (n: number) => Math.min(36, Math.max(8, Math.round(Math.sqrt(n) * 1.2)))

function useWidth() {
  const ref = useRef<HTMLDivElement | null>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const read = () => {
      const w = Math.floor(el.clientWidth)
      setWidth((prev) => (prev === w ? prev : w))
    }
    read()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', read)
      return () => window.removeEventListener('resize', read)
    }
    const ro = new ResizeObserver(read)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return { ref, width }
}

export function Histogram(props: Props) {
  const { ref, width } = useWidth()
  const height = props.height ?? 200
  const [hover, setHover] = useState<number | null>(null)
  const bins = useMemo(() => buildBins(props.values, props.target ?? autoTarget(props.values.length)), [props.values, props.target])

  const plotW = Math.max(0, width - M.left - M.right)
  const plotH = Math.max(0, height - M.top - M.bottom)

  if (!bins) {
    return (
      <div ref={ref} className="sim-hist sim-hist-empty" style={{ height }}>
        Sin datos para dibujar la distribución.
      </div>
    )
  }

  const n = bins.counts.length
  const start = bins.start
  const end = clean(start + n * bins.step)
  const span = end - start || 1
  const x = (v: number) => M.left + ((v - start) / span) * plotW
  const yMax = Math.max(1, bins.maxCount)
  const y = (c: number) => M.top + plotH - (c / yMax) * plotH
  const xTicks = plotW > 0 ? niceTicks(start, end, Math.max(2, Math.floor(plotW / 72))) : []
  const yTicks = Array.from(new Set([0, Math.round(yMax / 2), yMax])).filter((v) => v >= 0)
  const colorFor = props.colorFor ?? (() => 'var(--accent)')
  const refs = (props.refLines ?? []).filter((r) => Number.isFinite(r.value) && r.value >= start && r.value <= end)

  const onMove = (e: ReactMouseEvent<SVGRectElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - box.left
    const i = Math.floor((px / Math.max(1, box.width)) * n)
    setHover(i >= 0 && i < n ? i : null)
  }

  let readout: string
  if (hover !== null) {
    const lo = clean(start + hover * bins.step)
    const hi = clean(lo + bins.step)
    const c = bins.counts[hover]
    const pct = bins.total ? (c / bins.total) * 100 : 0
    readout = `${props.formatX(lo)} a ${props.formatX(hi)}: ${c} ${props.unit} (${pct.toLocaleString('es-ES', { maximumFractionDigits: 1 })} %)`
  } else {
    readout = `${bins.total} ${props.unit} · pasa el cursor por las barras`
  }

  return (
    <div ref={ref} className="sim-hist" style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={props.ariaLabel}>
          <title>{props.ariaLabel}</title>
          {yTicks.map((t) => (
            <g key={`y${t}`}>
              <line x1={M.left} x2={M.left + plotW} y1={y(t)} y2={y(t)} className="sim-hist-grid" />
              <text x={M.left - 6} y={y(t) + 3.5} textAnchor="end">
                {t}
              </text>
            </g>
          ))}
          {bins.counts.map((c, i) => {
            if (c === 0) return null
            const lo = start + i * bins.step
            const hi = lo + bins.step
            const x0 = x(lo) + 0.5
            const w = Math.max(1, x(hi) - x(lo) - 1)
            return (
              <rect
                key={i}
                x={x0}
                y={y(c)}
                width={w}
                height={Math.max(0, M.top + plotH - y(c))}
                rx={1.5}
                style={{ fill: colorFor(lo, hi), opacity: hover === null || hover === i ? 0.9 : 0.45 }}
              />
            )
          })}
          <line x1={M.left} x2={M.left + plotW} y1={M.top + plotH} y2={M.top + plotH} className="sim-hist-axis" />
          {xTicks.map((t) => (
            <g key={`x${t}`}>
              <line x1={x(t)} x2={x(t)} y1={M.top + plotH} y2={M.top + plotH + 4} className="sim-hist-axis" />
              <text x={x(t)} y={M.top + plotH + 16} textAnchor="middle">
                {props.formatX(t)}
              </text>
            </g>
          ))}
          {refs.map((r, i) => {
            const rx = x(r.value)
            const anchor = rx < M.left + 50 ? 'start' : rx > M.left + plotW - 50 ? 'end' : 'middle'
            // Alternate label rows so nearby reference lines do not overlap.
            const ly = i % 2 === 0 ? M.top - 4 : M.top + 10
            return (
              <g key={`r${r.label}`}>
                <line
                  x1={rx}
                  x2={rx}
                  y1={M.top - 1}
                  y2={M.top + plotH}
                  style={{ stroke: r.color }}
                  strokeWidth={1.4}
                  strokeDasharray={r.dashed === false ? undefined : '4 3'}
                />
                <text x={rx + (anchor === 'start' ? 3 : anchor === 'end' ? -3 : 0)} y={ly} textAnchor={anchor} className="sim-hist-ref" style={{ fill: r.color }}>
                  {r.label}
                </text>
              </g>
            )
          })}
          <text x={M.left} y={12} textAnchor="start" className="sim-hist-readout">
            {readout}
          </text>
          <rect
            x={M.left}
            y={M.top}
            width={plotW}
            height={plotH}
            fill="transparent"
            onMouseMove={onMove}
            onMouseLeave={() => setHover(null)}
          />
        </svg>
      )}
    </div>
  )
}
