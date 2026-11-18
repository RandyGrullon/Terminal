// The detail of one trade, opened by clicking it (NEXUS automática and its challenge accounts): why it opened, its target
// or why it has none, the parameters it took at its entry (Position.plan, app/sim/trade_plan.py) and how it is going or
// how it ended. A trade without a plan (the user's own) shows its numbers only.
import type { MouseEvent, KeyboardEvent } from 'react'
import { ChartCandlestick } from 'lucide-react'
import type { Position } from '../../api/types'
import { fmtDateTime, fmtR, fmtPct, fmtPrice } from '../../lib/format'
import { Button, Modal } from '../ui'
import { fmtMoney, isNum } from '../markets/opsUtils'
import { marketOf } from './paperUtils'

/** A click on a trade that was not on one of its own buttons or links (they keep their own action). */
export function isRowClick(e: MouseEvent<HTMLElement>): boolean {
  const target = e.target as HTMLElement | null
  return !target?.closest('button, a, input, select, textarea')
}

/** Props that make a tile or a row open the trade's detail by click or Enter / Space. */
export function detailProps(p: Position, onDetail: ((p: Position) => void) | undefined) {
  if (!onDetail) return {}
  return {
    role: 'button' as const,
    tabIndex: 0,
    title: 'Ver por qué abrió esta operación y qué parámetros tomó',
    onClick: (e: MouseEvent<HTMLElement>) => {
      if (isRowClick(e)) onDetail(p)
    },
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
        e.preventDefault()
        onDetail(p)
      }
    },
  }
}

export function TradeDetailBody({ p }: { p: Position }) {
  const m = marketOf(p)
  const plan = p.plan ?? null
  const open = p.status === 'open'
  const params = plan?.params ?? []
  return (
    <div className="td-body">
      <p className="tiny muted" style={{ margin: 0 }}>
        {open ? `Abierta el ${fmtDateTime(p.entry_time * 1000)}` : `Abierta el ${fmtDateTime(p.entry_time * 1000)} · cerrada el ${fmtDateTime(isNum(p.exit_time) ? p.exit_time * 1000 : null)}`}
        {' · '}
        {m.side}
      </p>

      <section className="td-section">
        <h4>Por qué abrió</h4>
        <p>{plan?.why ?? 'Operación sin explicación de NEXUS (la abriste tú o es de otra cuenta).'}</p>
      </section>

      <section className="td-section">
        <h4>Objetivo</h4>
        {plan ? (
          plan.target !== null ? (
            <p>
              <b className="mono">{fmtPrice(plan.target)}</b>
              {isNum(plan.target_r) ? ` · a ${plan.target_r.toLocaleString('es-ES', { maximumFractionDigits: 2 })}R de la entrada` : ''}
            </p>
          ) : (
            <p>
              <b>Sin objetivo:</b> {plan.no_target ?? 'sale con su stop y sus reglas de salida.'}
            </p>
          )
        ) : (
          <p>
            {[p.tp1, p.tp2, p.tp3].filter(isNum).map((t) => fmtPrice(t)).join(' · ') || 'Sin objetivos'}
          </p>
        )}
      </section>

      {params.length > 0 && (
        <section className="td-section">
          <h4>Parámetros que tomó al entrar</h4>
          <table className="td-params">
            <tbody>
              {params.map((x) => (
                <tr key={x.label}>
                  <th scope="row">{x.label}</th>
                  <td>{x.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="td-section">
        <h4>{open ? 'Cómo va' : 'Cómo terminó'}</h4>
        <table className="td-params">
          <tbody>
            <tr>
              <th scope="row">Entrada</th>
              <td className="mono">{fmtPrice(p.entry_price)}</td>
            </tr>
            <tr>
              <th scope="row">{open ? 'Precio ahora' : 'Salida'}</th>
              <td className="mono">
                {fmtPrice(open ? p.price : p.exit_price)}
                {!open && p.exit_label ? ` · ${p.exit_label}` : ''}
              </td>
            </tr>
            <tr>
              <th scope="row">Stop actual</th>
              <td className="mono">{fmtPrice(p.sl)}</td>
            </tr>
            <tr>
              <th scope="row">Resultado</th>
              <td className={`mono ${p.pnl >= 0 ? 'pos' : 'neg'}`}>
                {fmtMoney(p.pnl, true)} · {fmtPct(p.pnl_pct)} · {fmtR(p.r_multiple)}
              </td>
            </tr>
            {open && p.advice ? (
              <tr>
                <th scope="row">NEXUS ahora</th>
                <td>{p.advice}</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>
      <p className="tiny muted" style={{ margin: 0 }}>Simulado, sin dinero real. No es asesoría financiera.</p>
    </div>
  )
}

export default function TradeDetailModal({ p, onClose, onOpenChart }: { p: Position | null; onClose: () => void; onOpenChart?: (p: Position) => void }) {
  if (!p) return null
  const m = marketOf(p)
  return (
    <Modal
      open
      onClose={onClose}
      width={560}
      title={`${m.ticker} · ${p.side === 'long' ? 'Largo' : 'Corto'} · ${p.tf}`}
      footer={
        <>
          {onOpenChart && (
            <Button icon={<ChartCandlestick size={14} />} onClick={() => onOpenChart(p)}>
              Ver en el gráfico
            </Button>
          )}
          <Button variant="primary" onClick={onClose}>
            Cerrar
          </Button>
        </>
      }
    >
      <TradeDetailBody p={p} />
    </Modal>
  )
}
