// Side cards of NEXUS automática's trades (top right, with the toasts): every purchase or sale it opens — the product,
// how much, the entry, its money at the next target and at the stop — and every close with its real result. A
// "Ver operación" button opens the trade on its chart. With desktop notifications on (Ajustes → Alertas) and the tab in
// the background, each card is also a system notification. Fed by the default WS topic 'paper' ({type:
// 'paper_autopilot'}), so it works on every page; the first snapshot only learns what is open (lib/tradeAlert).
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChartCandlestick, X } from 'lucide-react'
import { api } from '../../api/client'
import type { Position } from '../../api/types'
import { socket } from '../../api/ws'
import { fmtDuration, fmtPrice, fmtR } from '../../lib/format'
import { diffSnapshot, type TradeAlert } from '../../lib/tradeAlert'
import { useApp } from '../../store'
import { Button, IconButton } from '../ui'
import { notifyDesktop } from '../macro/sound'
import { fmtMoney } from '../markets/opsUtils'
import { contractsText, normalizePositions } from './paperUtils'

/** Cards on screen at most (newest first). */
const MAX_CARDS = 4
/** A card hides itself after this long (the decision log and the history keep everything). */
const CARD_MS = 90_000

export const ALERT_NOTE = 'Antes de comisiones. No es una predicción: termina en el objetivo o en el stop, lo que llegue antes, o antes si NEXUS la cierra.'

function quantityText(a: TradeAlert): string {
  const p = a.position
  const units = p.qty_open > 0 ? p.qty_open : p.qty
  return contractsText(p, units) ?? `${units.toLocaleString('es-ES', { maximumFractionDigits: 6 })} unidades`
}

/** One line for a desktop notification (no markup). */
export function alertText(a: TradeAlert): { title: string; body: string } {
  if (a.kind === 'open') {
    const parts = [
      `${a.product} · ${a.speed}`,
      `${fmtMoney(a.notional)}${a.margin !== null ? ` (garantía ${fmtMoney(a.margin)})` : ''}`,
      a.target ? `objetivo ${fmtMoney(a.target.money, true)}` : null,
      a.stop ? `stop ${fmtMoney(a.stop.money, true)}` : null,
    ]
    return { title: `NEXUS · ${a.action} ${a.ticker}`, body: parts.filter(Boolean).join(' · ') }
  }
  const p = a.position
  return {
    title: `NEXUS · ${a.action} ${a.ticker} (cierra ${p.side === 'short' ? 'el corto' : 'la compra'})`,
    body: `Resultado ${fmtMoney(a.pnl, true)}${a.r !== null ? ` (${fmtR(a.r)})` : ''}${p.exit_label ? ` · ${p.exit_label}` : ''}`,
  }
}

export function TradeAlertCard({ alert: a, onClose, onShow }: { alert: TradeAlert; onClose: () => void; onShow: () => void }) {
  const p = a.position
  const buy = a.action === 'COMPRA'
  const won = (a.pnl ?? 0) > 0
  const tone = a.kind === 'open' ? (buy ? 'pos' : 'neg') : won ? 'pos' : (a.pnl ?? 0) < 0 ? 'neg' : 'muted'
  return (
    <div className={`trade-alert ${tone}`} role="status" aria-label={`${a.action} ${a.ticker}`}>
      <div className="row gap-2 trade-alert-head">
        <b className={buy ? 'pos' : 'neg'}>{a.action}</b>
        <b>{a.ticker}</b>
        {a.kind === 'close' && <span className="muted small">cierra {p.side === 'short' ? 'el corto' : 'la compra'}</span>}
        <span style={{ marginLeft: 'auto' }}>
          <IconButton size="sm" icon={<X size={13} />} title="Cerrar" onClick={onClose} />
        </span>
      </div>
      <div className="tiny muted">
        {a.product} · {a.speed} · NEXUS automática
      </div>
      {a.kind === 'open' ? (
        <div className="trade-alert-body small">
          <div>
            Monto <b className="mono">{fmtMoney(a.notional)}</b>
            {a.margin !== null && <span className="muted"> · garantía {fmtMoney(a.margin)}</span>}
            <span className="muted"> · {quantityText(a)}</span>
          </div>
          <div>
            Entrada <b className="mono">{fmtPrice(p.entry_price)}</b>
          </div>
          {a.target && (
            <div className="pos">
              Si llega a {a.target.label} ({fmtPrice(a.target.price)}): <b className="mono">{fmtMoney(a.target.money, true)}</b>
            </div>
          )}
          {a.stop && (
            <div className="neg">
              Si toca el stop ({fmtPrice(a.stop.price)}): <b className="mono">{fmtMoney(a.stop.money, true)}</b>
            </div>
          )}
          <div className="tiny muted">{ALERT_NOTE}</div>
        </div>
      ) : (
        <div className="trade-alert-body small">
          <div>
            Resultado{' '}
            <b className={`mono ${won ? 'pos' : (a.pnl ?? 0) < 0 ? 'neg' : ''}`}>
              {fmtMoney(a.pnl, true)}
              {a.r !== null ? ` (${fmtR(a.r)})` : ''}
            </b>
            {p.exit_label && <span className="muted"> · {p.exit_label}</span>}
          </div>
          <div className="muted">
            Entrada {fmtPrice(p.entry_price)} → salida {fmtPrice(p.exit_price)}
            {a.durationS !== null ? ` · duró ${fmtDuration(a.durationS)}` : ''}
          </div>
        </div>
      )}
      <div className="row">
        <Button size="sm" variant="ghost" icon={<ChartCandlestick size={13} />} onClick={onShow}>
          Ver operación
        </Button>
      </div>
    </div>
  )
}

export default function TradeAlerts() {
  const [cards, setCards] = useState<TradeAlert[]>([])
  const known = useRef<Set<number> | null>(null)
  const timers = useRef(new Map<string, number>())

  const drop = useCallback((key: string) => {
    setCards((c) => c.filter((x) => x.key !== key))
    const t = timers.current.get(key)
    if (t !== undefined) window.clearTimeout(t)
    timers.current.delete(key)
  }, [])

  const show = useCallback((p: Position) => useApp.getState().showTrade(p), [])

  const take = useCallback(
    (positions: Position[], history: Position[]) => {
      const { known: next, alerts } = diffSnapshot(known.current, positions, history)
      known.current = next
      if (alerts.length === 0) return
      setCards((c) => [...alerts.filter((a) => !c.some((x) => x.key === a.key)), ...c].slice(0, MAX_CARDS))
      for (const a of alerts) {
        if (!timers.current.has(a.key)) timers.current.set(a.key, window.setTimeout(() => drop(a.key), CARD_MS))
        const { title, body } = alertText(a)
        notifyDesktop({ title, body, tag: `nexus-trade-${a.key}`, onClick: () => show(a.position) })
      }
    },
    [drop, show],
  )

  useEffect(() => {
    let alive = true
    // what is open now, so the first live update does not announce old trades
    api.paper.autopilot
      .get()
      .then((snap) => {
        if (alive && known.current === null) take(normalizePositions(snap?.positions), normalizePositions(snap?.history))
      })
      .catch(() => undefined)
    const off = socket.on('paper_autopilot', (msg) => {
      const m = msg as unknown as { positions?: unknown; history?: unknown }
      take(normalizePositions(m.positions), normalizePositions(m.history))
    })
    const pending = timers.current
    return () => {
      alive = false
      off()
      for (const t of pending.values()) window.clearTimeout(t)
      pending.clear()
    }
  }, [take])

  if (cards.length === 0) return null
  return (
    <>
      {cards.map((a) => (
        <TradeAlertCard key={a.key} alert={a} onClose={() => drop(a.key)} onShow={() => show(a.position)} />
      ))}
    </>
  )
}
