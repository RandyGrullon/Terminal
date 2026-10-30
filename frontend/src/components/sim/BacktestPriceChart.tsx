// Backtest price chart: candles + volume + trade markers (entries, exits, rejected signals) + per-trade SL/TP lines.
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ISeriesApi, MouseEventParams, Time } from 'lightweight-charts'
import { MousePointer2, X } from 'lucide-react'
import type { BacktestResult, ChartMarker, Trade } from '../../api/types'
import {
  COLORS,
  LevelLines,
  LineStyle,
  MarkerLayer,
  TradeLevelsLayer,
  addCandles,
  addVolume,
  fromTs,
  toCandleData,
  toVolumeData,
  ts,
  useChart,
} from '../../lib/chart'
import { DIRECTION_LABEL, fmtBarTime, fmtPct, fmtR, signTone } from '../../lib/format'
import { Button, Card, Toggle } from '../ui'
import { isRejectedMarker } from './labels'
import { MarkerTooltip, type MarkerHover } from './MarkerTooltip'
import { applyPricePrecision, barSeconds, indexMarkers, isValidMarker, sanitizeCandles, tradeLevelBars } from './series'

interface Layers {
  candles: ISeriesApi<'Candlestick'>
  volume: ISeriesApi<'Histogram'>
  markers: MarkerLayer
  levels: TradeLevelsLayer
  selected: LevelLines
}

export function BacktestPriceChart(props: { result: BacktestResult; selected: Trade | null; onClearSelection: () => void }) {
  const { result, selected } = props
  const { ref, chart } = useChart()
  const layers = useRef<Layers | null>(null)
  const [showRejected, setShowRejected] = useState(true)
  const [showLevels, setShowLevels] = useState(true)
  const [hover, setHover] = useState<MarkerHover | null>(null)

  const candles = useMemo(() => sanitizeCandles(result.candles), [result])
  const markers = useMemo<ChartMarker[]>(() => result.markers.filter(isValidMarker), [result])
  const rejectedCount = useMemo(() => markers.filter(isRejectedMarker).length, [markers])
  const visibleMarkers = useMemo(() => (showRejected ? markers : markers.filter((m) => !isRejectedMarker(m))), [markers, showRejected])
  const markerIndex = useMemo(() => indexMarkers(visibleMarkers), [visibleMarkers])
  const indexRef = useRef(markerIndex)
  indexRef.current = markerIndex

  useEffect(() => {
    if (!chart) return
    const candleSeries = addCandles(chart)
    const volume = addVolume(chart)
    const levels = new TradeLevelsLayer(chart)
    const markerLayer = new MarkerLayer(candleSeries)
    const selectedLines = new LevelLines(candleSeries)
    layers.current = { candles: candleSeries, volume, markers: markerLayer, levels, selected: selectedLines }
    const onMove = (param: MouseEventParams<Time>) => {
      const el = ref.current
      if (param.time === undefined || !param.point || !el) {
        setHover(null)
        return
      }
      const t = fromTs(param.time)
      const ms = indexRef.current.get(t) ?? []
      setHover(ms.length ? { x: param.point.x, y: param.point.y, width: el.clientWidth, height: el.clientHeight, time: t, markers: ms } : null)
    }
    chart.subscribeCrosshairMove(onMove)
    return () => {
      // The chart was already removed by useChart's cleanup: do not touch its series (it would redraw a disposed chart).
      try {
        chart.unsubscribeCrosshairMove(onMove)
      } catch {
        /* chart already removed */
      }
      layers.current = null
    }
  }, [chart, ref])

  useEffect(() => {
    const l = layers.current
    if (!chart || !l) return
    try {
      l.candles.setData(toCandleData(candles))
      l.volume.setData(toVolumeData(candles))
      applyPricePrecision(l.candles, candles.length ? candles[candles.length - 1].close : null)
      chart.timeScale().fitContent()
    } catch (err) {
      console.error('No se pudo dibujar el gráfico de precio del backtest', err)
    }
  }, [chart, candles])

  useEffect(() => {
    const l = layers.current
    if (!chart || !l) return
    try {
      l.markers.set(visibleMarkers)
    } catch (err) {
      console.error('No se pudieron dibujar las marcas del backtest', err)
    }
  }, [chart, visibleMarkers])

  useEffect(() => {
    const l = layers.current
    if (!chart || !l) return
    try {
      if (showLevels) l.levels.set(tradeLevelBars(candles, result.trades))
      else l.levels.clear()
    } catch (err) {
      console.error('No se pudieron dibujar los niveles de las operaciones', err)
    }
  }, [chart, candles, result.trades, showLevels])

  useEffect(() => {
    const l = layers.current
    if (!chart || !l) return
    if (!selected) {
      l.selected.clear()
      return
    }
    l.selected.set([
      { price: selected.entry_price, color: COLORS.text, title: 'ENTRADA', style: LineStyle.Solid },
      { price: selected.sl_initial, color: COLORS.neg, title: 'SL · HASTA AQUÍ', style: LineStyle.Solid, width: 2 },
      { price: selected.tp1, color: COLORS.tp1, title: 'TP1' },
      { price: selected.tp2, color: COLORS.tp2, title: 'TP2' },
      { price: selected.tp3, color: COLORS.tp3, title: 'TP3' },
      { price: selected.exit_price, color: COLORS.warn, title: 'SALIDA', style: LineStyle.Dotted },
    ])
    if (!candles.length) return
    const span = barSeconds(candles)
    const first = candles[0].time
    const lastT = candles[candles.length - 1].time
    const exitT = selected.exit_time ?? lastT
    const pad = Math.max(span * 25, (exitT - selected.entry_time) * 0.6)
    const from = Math.max(first, selected.entry_time - pad)
    const to = Math.min(lastT, exitT + pad)
    if (to > from) {
      try {
        chart.timeScale().setVisibleRange({ from: ts(from), to: ts(to) })
      } catch {
        /* range outside data: keep the current view */
      }
    }
  }, [chart, selected, candles])

  return (
    <Card
      title="Precio y operaciones"
      sub={
        selected ? (
          <span>
            Operación #{selected.id}: {DIRECTION_LABEL[selected.side] ?? selected.side} {fmtBarTime(selected.entry_time, result.tf)} →{' '}
            {selected.exit_time ? fmtBarTime(selected.exit_time, result.tf) : 'abierta'} ·{' '}
            <b className={signTone(selected.pnl_pct)}>{fmtPct(selected.pnl_pct)}</b> · <b className={signTone(selected.r_multiple)}>{fmtR(selected.r_multiple)}</b>
            {selected.exit_label ? ` · ${selected.exit_label}` : ''}
          </span>
        ) : (
          'Haz clic en una operación de la tabla para ver su SL «hasta aquí», sus objetivos y su salida'
        )
      }
      actions={
        <>
          <Toggle checked={showRejected} onChange={setShowRejected} label={`Rechazadas (${rejectedCount})`} />
          <Toggle checked={showLevels} onChange={setShowLevels} label="Niveles SL/TP" />
          {selected && (
            <Button size="sm" variant="ghost" icon={<X size={13} />} onClick={props.onClearSelection}>
              Quitar selección
            </Button>
          )}
        </>
      }
      flush
    >
      <div className="chart-box sim-chart-frame sim-h-price">
        <div ref={ref} className="sim-chart-canvas" />
        {candles.length > 0 && (
          <div className="legend">
            <div className="legend-item">
              <b>{result.symbol}</b> <span className="muted">{result.tf}</span>
            </div>
            {showLevels && (
              <div className="legend-item">
                <span className="swatch" style={{ background: COLORS.neg }} /> SL inicial · hasta aquí
                <span className="swatch" style={{ background: COLORS.tp2 }} /> TP1-TP3
              </div>
            )}
            <div className="legend-item sim-chart-hint">
              <MousePointer2 size={11} /> Marcas pequeñas = señales rechazadas por el filtro (NO TOMAR / ESPERAR); pasa el cursor para ver el motivo
            </div>
          </div>
        )}
        <MarkerTooltip hover={hover} tf={result.tf} />
        {candles.length === 0 && (
          <div className="chart-overlay">
            <span className="muted small">El backtest no devolvió velas para dibujar.</span>
          </div>
        )}
      </div>
    </Card>
  )
}
