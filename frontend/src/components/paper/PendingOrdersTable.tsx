// Pending limit / stop orders of the demo account (simulated, never sent to a broker): level, distance to the market,
// stop / target, size, validity and the gate verdict at placement; cancel or open the chart. Below, the latest orders
// that were filled, cancelled (with the reason: the filter at the fill, a risk limit, a gap...) or expired.
import { memo, useState } from 'react'
import { ChartCandlestick, ChevronDown, ChevronRight, Hourglass, ShieldAlert } from 'lucide-react'
import type { PaperOrder } from '../../api/types'
import { fmtDateTime, fmtPrice, splitSymbol } from '../../lib/format'
import { Badge, Button, IconButton, VerdictBadge } from '../ui'
import { fmtQty, isNum } from '../markets/opsUtils'
import { CANCELLED_BY_LABEL, distanceText, orderStatusTone } from './ordersUtils'

interface Props {
  orders: PaperOrder[]
  recent: PaperOrder[]
  cancellingId: number | null
  onCancel: (o: PaperOrder) => void
  onOpenChart: (o: PaperOrder) => void
}

function sizeText(o: PaperOrder): string {
  if (isNum(o.qty)) return `${fmtQty(o.qty)} u.`
  return isNum(o.risk_pct) ? `riesgo ${o.risk_pct.toLocaleString('es-ES')} %` : 'riesgo por defecto'
}

const PendingRow = memo(function PendingRow(props: {
  o: PaperOrder
  cancelling: boolean
  busy: boolean
  onCancel: (o: PaperOrder) => void
  onOpenChart: (o: PaperOrder) => void
}) {
  const { o, cancelling, busy, onCancel, onOpenChart } = props
  const { exchange, ticker } = splitSymbol(o.symbol)
  const dist = distanceText(o)
  return (
    <tr>
      <td>
        <div className="pp-sym">
          <button type="button" onClick={() => onOpenChart(o)} title={`Abrir ${o.symbol} en el gráfico`}>
            {ticker}
          </button>
          <span className="pp-sub">
            {exchange || '—'} · {o.tf}
          </span>
        </div>
      </td>
      <td>
        <div className="pp-stack" style={{ alignItems: 'flex-start', gap: 2 }}>
          <Badge tone={o.side === 'long' ? 'pos' : 'neg'}>{o.label}</Badge>
          <span className="pp-sub">#{o.id}</span>
        </div>
      </td>
      <td className="num mono">
        <div className="pp-stack">
          <span className="bold">{fmtPrice(o.price)}</span>
          <span className="pp-sub" title="Distancia desde el último precio en vivo">
            {dist ? `${dist} · ${fmtPrice(o.market_price)}` : 'sin precio en vivo'}
          </span>
        </div>
      </td>
      <td className="num mono">
        <div className="pp-stack">
          <span className="neg">{isNum(o.sl) ? fmtPrice(o.sl) : 'NEXUS'}</span>
          <span className="pp-sub pos">{isNum(o.tp) ? fmtPrice(o.tp) : 'objetivos NEXUS'}</span>
        </div>
      </td>
      <td className="small">{sizeText(o)}</td>
      <td>
        <span className="small">{o.expiry_text}</span>
      </td>
      <td>
        <div className="pp-stack" style={{ alignItems: 'flex-start', gap: 2 }}>
          {o.gate_verdict ? <VerdictBadge verdict={o.gate_verdict} score={o.gate_score} /> : <span className="tiny muted">sin filtro</span>}
          {o.forced && <span className="pp-sub warn">dejada contra el filtro</span>}
          {o.executing && (
            <span className="pp-sub warn row gap-1">
              <Hourglass size={11} /> ejecutándose: el filtro decide
            </span>
          )}
        </div>
      </td>
      <td className="pp-sticky">
        <div className="pp-actions">
          <Button size="sm" loading={cancelling} disabled={busy} onClick={() => onCancel(o)} title="Cancelar la orden pendiente (demo)">
            Cancelar
          </Button>
          <IconButton icon={<ChartCandlestick size={14} />} title="Ver gráfico" size="sm" onClick={() => onOpenChart(o)} />
        </div>
      </td>
    </tr>
  )
})

function Recent({ items }: { items: PaperOrder[] }) {
  const [open, setOpen] = useState(false)
  if (!items.length) return null
  return (
    <div className="pp-orders-recent">
      <button type="button" className="pp-orders-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Últimas órdenes cerradas ({items.length})
      </button>
      {open && (
        <ul className="pp-orders-list">
          {items.map((o) => (
            <li key={o.id}>
              <Badge tone={orderStatusTone(o)}>{o.status_label}</Badge>
              <span className="small">
                <b>{splitSymbol(o.symbol).ticker}</b> · {o.label.toLowerCase()} a <span className="mono">{fmtPrice(o.price)}</span>
                {o.status === 'filled' && isNum(o.fill_price) && (
                  <>
                    {' '}
                    → posición #{o.position_id} a <span className="mono">{fmtPrice(o.fill_price)}</span>
                  </>
                )}
                {o.status === 'cancelled' && o.cancelled_by && <span className="muted"> · {CANCELLED_BY_LABEL[o.cancelled_by] ?? o.cancelled_by}</span>}
              </span>
              {o.reason && o.cancelled_by !== 'user' && (
                <span className={`tiny ${o.cancelled_by === 'gate' || o.cancelled_by === 'risk' ? 'warn' : 'muted'} row gap-1`}>
                  {(o.cancelled_by === 'gate' || o.cancelled_by === 'risk') && <ShieldAlert size={11} />} {o.reason}
                </span>
              )}
              <span className="tiny muted">{fmtDateTime(o.updated_at ?? o.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function PendingOrdersTable({ orders, recent, cancellingId, onCancel, onOpenChart }: Props) {
  return (
    <>
      {orders.length > 0 && (
        <div className="table-wrap pp-table-wrap">
          <table className="table pp-table">
            <thead>
              <tr>
                <th>Símbolo</th>
                <th>Orden</th>
                <th className="num" title="Precio de la orden y distancia desde el último precio en vivo">
                  Nivel
                </th>
                <th className="num" title="Stop loss («hasta aquí») y objetivo. Vacíos: los de NEXUS, calculados al ejecutarse">
                  SL · objetivo
                </th>
                <th>Tamaño</th>
                <th>Validez</th>
                <th title="Veredicto del filtro al dejar la orden; al ejecutarse se vuelve a consultar">Filtro</th>
                <th className="num pp-sticky">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <PendingRow key={o.id} o={o} cancelling={cancellingId === o.id} busy={cancellingId !== null} onCancel={onCancel} onOpenChart={onOpenChart} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Recent items={recent} />
    </>
  )
}

export default memo(PendingOrdersTable)
