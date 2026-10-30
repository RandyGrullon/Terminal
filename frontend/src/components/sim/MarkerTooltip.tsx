// Hover card that explains the NEXUS markers of a bar (entries, exits, and why a signal was rejected).
import type { ChartMarker } from '../../api/types'
import { fmtBarTime, fmtPrice } from '../../lib/format'
import { isRejectedMarker, markerTitle } from './labels'

export interface MarkerHover {
  /** Cursor position inside the chart container (px). */
  x: number
  y: number
  /** Container size, used to keep the card inside. */
  width: number
  height: number
  /** Bar time (unix seconds). */
  time: number
  markers: ChartMarker[]
}

const CARD_W = 300

export function MarkerTooltip(props: { hover: MarkerHover | null; tf?: string | null }) {
  const h = props.hover
  if (!h || h.markers.length === 0) return null
  const left = h.x + 16 + CARD_W > h.width ? Math.max(4, h.x - 16 - CARD_W) : h.x + 16
  const top = Math.max(4, Math.min(h.y + 14, h.height - 40 - h.markers.length * 44))
  return (
    <div className="sim-tip" style={{ left, top, width: CARD_W }} role="tooltip">
      <div className="tiny muted">{fmtBarTime(h.time, props.tf ?? undefined)}</div>
      {h.markers.map((m, i) => {
        const title = markerTitle(m)
        const showText = m.text && m.text !== title
        return (
          <div key={`${m.kind}-${i}`} className="sim-tip-row">
            <span className="sim-tip-dot" style={{ background: m.color || 'var(--muted)' }} />
            <div className="col gap-0" style={{ minWidth: 0 }}>
              <strong className={isRejectedMarker(m) ? 'warn' : undefined}>{title}</strong>
              {showText && <span className="text-2">{m.text}</span>}
              {m.tooltip && <span className="text-2 sim-tip-detail">{m.tooltip}</span>}
              {typeof m.price === 'number' && Number.isFinite(m.price) && <span className="tiny muted mono">Precio {fmtPrice(m.price)}</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}
