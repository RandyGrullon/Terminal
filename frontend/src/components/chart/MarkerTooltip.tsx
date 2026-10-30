// Floating explanation of the NEXUS markers under the crosshair (why a signal was taken, rejected or closed).
import { memo, useEffect, useState } from 'react'
import type { ChartMarker } from '../../api/types'
import { isRejectedMarker, type Emitter } from './chartUtils'

export interface TooltipState {
  x: number
  y: number
  markers: ChartMarker[]
}

const WIDTH = 270

function MarkerTooltip(props: { bus: Emitter<TooltipState | null>; getSize: () => { width: number; height: number } }) {
  const { bus, getSize } = props
  const [state, setState] = useState<TooltipState | null>(null)

  useEffect(() => bus.on(setState), [bus])

  if (!state || state.markers.length === 0) return null
  const { width, height } = getSize()
  const left = state.x + 16 + WIDTH > width ? Math.max(4, state.x - 16 - WIDTH) : state.x + 16
  const top = Math.min(Math.max(4, state.y + 12), Math.max(4, height - 40 - state.markers.length * 48))

  return (
    <div className="chart-tooltip" style={{ left, top, width: WIDTH }} role="tooltip">
      {state.markers.map((m, i) => (
        <div key={`${m.kind}-${i}`} className="chart-tooltip-row">
          <div className="row gap-1">
            <span className="chart-tooltip-dot" style={{ background: m.color }} />
            <strong style={{ color: m.color }}>{m.text || m.kind}</strong>
            {isRejectedMarker(m) && <span className="badge badge-warn">filtro: no tomar</span>}
          </div>
          {m.tooltip && <div className="chart-tooltip-text">{m.tooltip}</div>}
        </div>
      ))}
    </div>
  )
}

export default memo(MarkerTooltip)
