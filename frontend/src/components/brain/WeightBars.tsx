// Centered horizontal bars for learned weights (scanner features, gate checks): sorted by |weight|, green pushes
// towards "up / take", red towards "down / avoid".
import { memo, useMemo, useState } from 'react'
import { fmtNum } from '../../lib/format'

export interface WeightItem {
  id: string
  label: string
  weight: number
}

interface Props {
  items: WeightItem[]
  limit?: number
  /** All weights >= 0 (e.g. importances): draw from the left edge instead of the center. */
  positiveOnly?: boolean
  posHint?: string
  negHint?: string
}

function WeightBars({ items, limit = 14, positiveOnly, posHint, negHint }: Props) {
  const [all, setAll] = useState(false)
  const rows = useMemo(
    () =>
      items
        .filter((i) => typeof i.weight === 'number' && Number.isFinite(i.weight))
        .sort((a, b) => Math.abs(b.weight) - Math.abs(a.weight)),
    [items],
  )
  const maxAbs = rows.reduce((m, r) => Math.max(m, Math.abs(r.weight)), 0) || 1
  const shown = all ? rows : rows.slice(0, limit)

  if (!rows.length) return <div className="muted small brain-pad">Sin pesos aprendidos todavía.</div>

  return (
    <div className="brain-weights">
      {(posHint || negHint) && !positiveOnly && (
        <div className="brain-weight-row brain-weights-axis tiny muted">
          <span />
          <span className="brain-weights-hints">
            <span className="ellipsis">← {negHint ?? 'negativo'}</span>
            <span className="ellipsis">{posHint ?? 'positivo'} →</span>
          </span>
          <span />
        </div>
      )}
      {shown.map((r) => {
        const frac = Math.min(1, Math.abs(r.weight) / maxAbs)
        const pos = r.weight >= 0
        return (
          <div key={r.id} className="brain-weight-row" title={`${r.label} (${r.id}): ${fmtNum(r.weight, 4)}`}>
            <span className="brain-weight-label ellipsis">{r.label}</span>
            <span className={`brain-weight-track ${positiveOnly ? 'left' : ''}`}>
              {!positiveOnly && <span className="brain-weight-mid" />}
              <span
                className={`brain-weight-bar ${pos ? 'pos' : 'neg'}`}
                style={
                  positiveOnly
                    ? { left: 0, width: `${frac * 100}%` }
                    : pos
                      ? { left: '50%', width: `${frac * 50}%` }
                      : { left: `${50 - frac * 50}%`, width: `${frac * 50}%` }
                }
              />
            </span>
            <span className={`brain-weight-val mono ${pos ? 'pos' : 'neg'}`}>
              {r.weight > 0 && !positiveOnly ? '+' : ''}
              {fmtNum(r.weight, Math.abs(r.weight) >= 10 ? 1 : 3)}
            </span>
          </div>
        )
      })}
      {rows.length > limit && (
        <button type="button" className="brain-more" onClick={() => setAll((a) => !a)}>
          {all ? 'Ver menos' : `Ver los ${rows.length}`}
        </button>
      )}
    </div>
  )
}

export default memo(WeightBars)
