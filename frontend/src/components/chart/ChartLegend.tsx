// Top-left legend over the main chart: OHLC of the hovered (or last) bar, NEXUS lines, score pane values and
// every user indicator with its color swatches, values, status and edit / remove buttons.
import { memo, useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, RotateCcw, Settings2, TriangleAlert, X } from 'lucide-react'
import type { Candle, NexusBars } from '../../api/types'
import { COLORS } from '../../lib/chart'
import { fmtCompact, fmtPct, fmtPrice, fmtProb, fmtScore } from '../../lib/format'
import { Spinner } from '../ui'
import { candleIndexAt, fmtAuto, safeColor, valueAt, type Emitter } from './chartUtils'
import { paramsSummary, type ResolvedIndicator } from './indicatorsModel'

interface Props {
  hover: Emitter<number | null>
  getCandles: () => Candle[]
  getPrecision: () => number
  bars: NexusBars | null
  showEma: boolean
  showScore: boolean
  indicators: ResolvedIndicator[]
  onEdit: (uid: string) => void
  onRemove: (uid: string) => void
  onRetry: (uid: string) => void
}

function ChartLegend(props: Props) {
  const { hover, bars, indicators } = props
  const [t, setT] = useState<number | null>(null)
  const [tick, setTick] = useState(0)
  const [collapsed, setCollapsed] = useState(false)

  useEffect(
    () =>
      hover.on((v) => {
        setT(v)
        setTick((n) => (n + 1) % 1_000_000)
      }),
    [hover],
  )

  const candles = props.getCandles()
  const i = candleIndexAt(candles, t)
  const c = i >= 0 ? candles[i] : null
  const prev = i > 0 ? candles[i - 1] : null
  const decimals = props.getPrecision()
  const chg = c && prev && prev.close ? ((c.close - prev.close) / prev.close) * 100 : null
  const tone = chg === null ? 'muted' : chg >= 0 ? 'pos' : 'neg'

  const emaRows: { label: string; color: string; key: 'ema_fast' | 'ema_slow' | 'ema_trend' }[] = [
    { label: 'EMA rápida', color: COLORS.accent, key: 'ema_fast' },
    { label: 'EMA lenta', color: COLORS.purple, key: 'ema_slow' },
    { label: 'EMA tendencia', color: COLORS.muted, key: 'ema_trend' },
  ]

  return (
    <div className="legend chart-legend" data-tick={tick}>
      {c && (
        <div className="legend-item chart-legend-ohlc mono">
          <span className="muted">A</span>
          {fmtPrice(c.open, decimals)}
          <span className="muted">Máx</span>
          {fmtPrice(c.high, decimals)}
          <span className="muted">Mín</span>
          {fmtPrice(c.low, decimals)}
          <span className="muted">C</span>
          <span className={tone}>{fmtPrice(c.close, decimals)}</span>
          {chg !== null && <span className={tone}>{fmtPct(chg)}</span>}
          <span className="muted">Vol</span>
          {fmtCompact(c.volume)}
        </div>
      )}

      {bars && props.showEma && (
        <div className="legend-item">
          <span className="chart-legend-name">NEXUS</span>
          {emaRows.map((r) => (
            <span key={r.key} className="row gap-1" title={r.label}>
              <span className="swatch" style={{ background: r.color }} />
              <span className="mono">{fmtPrice(valueAt(bars.time, bars[r.key], t), decimals)}</span>
            </span>
          ))}
        </div>
      )}

      {bars && props.showScore && (
        <div className="legend-item" title="Panel NEXUS: puntuación (-1 a +1) y probabilidad de subida reescalada (2·P−1)">
          <span className="chart-legend-name">Puntuación</span>
          <span className="mono" style={{ color: (valueAt(bars.time, bars.score, t) ?? 0) >= 0 ? COLORS.pos : COLORS.neg }}>
            {fmtScore(valueAt(bars.time, bars.score, t))}
          </span>
          <span className="swatch" style={{ background: COLORS.cyan }} />
          <span className="muted">P↑</span>
          <span className="mono">{fmtProb(valueAt(bars.time, bars.prob_up, t))}</span>
        </div>
      )}

      {indicators.length > 0 && (
        <button
          type="button"
          className="legend-item chart-legend-toggle"
          onClick={() => setCollapsed((v) => !v)}
          title={collapsed ? 'Mostrar indicadores' : 'Ocultar la lista de indicadores'}
        >
          {collapsed ? <ChevronDown size={12} /> : <ChevronUp size={12} />}
          {indicators.length} indicador{indicators.length === 1 ? '' : 'es'}
        </button>
      )}

      {!collapsed &&
        indicators.map((ind) => {
          const name = ind.spec?.name ?? ind.id
          const summary = paramsSummary(ind.spec, ind.params)
          const outputs = ind.spec?.outputs ?? []
          const r = ind.result
          return (
            <div key={ind.uid} className={`legend-item chart-legend-ind ${ind.status === 'error' || ind.status === 'unknown' ? 'is-error' : ''}`}>
              <span className="row gap-0" style={{ gap: 2 }}>
                {(outputs.length ? outputs.slice(0, 3) : [{ key: '_', color: COLORS.muted }]).map((o, k) => (
                  <span key={o.key} className="swatch" style={{ background: safeColor(o.color, k === 0 ? COLORS.accent : COLORS.purple) }} />
                ))}
              </span>
              <span className="chart-legend-name" title={ind.spec?.description ?? ''}>
                {name}
                {summary && <span className="muted"> {summary}</span>}
              </span>
              {ind.status === 'ok' &&
                r &&
                outputs.slice(0, 4).map((o) => (
                  <span key={o.key} className="mono" style={{ color: safeColor(o.color) }} title={o.title}>
                    {fmtAuto(valueAt(r.time, r.outputs[o.key], t))}
                  </span>
                ))}
              {ind.status === 'pending' && c && <Spinner />}
              {(ind.status === 'error' || ind.status === 'unknown') && (
                <span className="row gap-1 neg chart-legend-err" title={ind.error ?? 'Error'}>
                  <TriangleAlert size={12} />
                  <span className="ellipsis">{ind.error ?? 'Error'}</span>
                </span>
              )}
              <span className="chart-legend-actions">
                {ind.status === 'error' && (
                  <button type="button" className="chart-legend-btn" title="Reintentar" onClick={() => props.onRetry(ind.uid)}>
                    <RotateCcw size={12} />
                  </button>
                )}
                {ind.spec && (
                  <button type="button" className="chart-legend-btn" title="Editar parámetros" onClick={() => props.onEdit(ind.uid)}>
                    <Settings2 size={12} />
                  </button>
                )}
                <button type="button" className="chart-legend-btn" title="Quitar indicador" onClick={() => props.onRemove(ind.uid)}>
                  <X size={12} />
                </button>
              </span>
            </div>
          )
        })}
    </div>
  )
}

export default memo(ChartLegend)
