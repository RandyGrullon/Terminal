// «Ver operación» strip above the chart: which demo position is drawn, where it opened, where the price is, how far
// its next target and its stop are (or how it ended), and the honest note that the end is the target or the stop,
// never a prediction. Pure view: ChartPage passes the position and the live price.
import { ChartCandlestick, X } from 'lucide-react'
import type { Position } from '../../api/types'
import { fmtBarTime, fmtPct, fmtPrice, fmtR, signTone } from '../../lib/format'
import { TRADE_END_NOTE, tradeOf, tradeProgress } from '../../lib/tradeView'
import { Badge, Button, IconButton } from '../ui'

const ACCOUNT_LABEL: Record<number, string> = { 1: 'Mi cuenta demo', 3: 'NEXUS automática' }

export interface TradeFocusCardProps {
  position: Position
  /** The chart's symbol: when it is another market the strip offers to go back to the trade's chart. */
  chartSymbol: string
  price: number | null
  onShowChart: () => void
  onClose: () => void
}

export default function TradeFocusCard({ position, chartSymbol, price, onShowChart, onClose }: TradeFocusCardProps) {
  const t = tradeOf(position)
  const account = ACCOUNT_LABEL[position.account_id] ?? `Cuenta ${position.account_id}`
  const ticker = String(position.symbol || '').split(':').pop()
  const close = <IconButton icon={<X size={14} />} title="Dejar de mostrar la operación" size="sm" onClick={onClose} />
  if (!t) {
    return (
      <div className="chart-trade row gap-2" role="status">
        <span className="small">
          {position.instrument?.kind === 'option'
            ? 'Es una opción teórica: sus niveles son primas, no precios de la acción, así que aquí solo se ve la acción.'
            : 'Esta operación no tiene los precios necesarios para dibujarla.'}
        </span>
        {close}
      </div>
    )
  }
  if (t.symbol !== chartSymbol.toUpperCase()) {
    return (
      <div className="chart-trade row gap-2" role="status">
        <span className="small">
          Operación en <b>{ticker}</b> ({account}): estás viendo otro mercado.
        </span>
        <Button size="sm" variant="ghost" icon={<ChartCandlestick size={13} />} onClick={onShowChart}>
          Volver a su gráfico
        </Button>
        {close}
      </div>
    )
  }
  const long = t.side === 'long'
  const prog = t.open && price !== null ? tradeProgress(t, price) : null
  return (
    <div className="chart-trade" role="status" aria-label="Operación en el gráfico">
      <div className="row gap-2 chart-trade-head">
        <Badge tone={long ? 'pos' : 'neg'}>{long ? 'Largo' : 'Corto'}</Badge>
        <b>{ticker}</b>
        <span className="muted small">
          {account} · {t.tf} · {t.open ? 'abierta' : 'cerrada'}
        </span>
        <span style={{ marginLeft: 'auto' }}>{close}</span>
      </div>
      <div className="chart-trade-facts small">
        <span>
          Entrada <b className="mono">{fmtPrice(t.entryPrice)}</b> <span className="muted">({fmtBarTime(t.entryTime, t.tf)})</span>
        </span>
        {t.open ? (
          <>
            {prog && (
              <span>
                Ahora <b className="mono">{fmtPrice(prog.price)}</b>{' '}
                <span className={`mono ${signTone(prog.pnlPct)}`}>({fmtPct(prog.pnlPct)})</span>
              </span>
            )}
            {prog?.target && (
              <span className="pos">
                {prog.target.label} <b className="mono">{fmtPrice(prog.target.price)}</b>{' '}
                <span className="muted">
                  ({prog.toTargetPct !== null && prog.toTargetPct > 0 ? `faltan ${fmtPct(prog.toTargetPct, 2, false)}` : 'alcanzado'})
                </span>
              </span>
            )}
            {prog && (
              <span className="neg">
                {prog.stop.label} <b className="mono">{fmtPrice(prog.stop.price)}</b>{' '}
                <span className="muted">({prog.toStopPct > 0 ? `a ${fmtPct(prog.toStopPct, 2, false)}` : 'tocado'})</span>
              </span>
            )}
          </>
        ) : (
          <>
            <span>
              Salida <b className="mono">{fmtPrice(t.exitPrice)}</b> <span className="muted">({fmtBarTime(t.exitTime, t.tf)})</span>
            </span>
            {t.rMultiple !== null && <span className={`mono ${signTone(t.rMultiple)}`}>{fmtR(t.rMultiple)}</span>}
            {t.exitLabel && <span className="muted">{t.exitLabel}</span>}
          </>
        )}
      </div>
      {t.open && <div className="tiny muted">{TRADE_END_NOTE}</div>}
    </div>
  )
}
