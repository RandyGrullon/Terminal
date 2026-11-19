// Modo simple: only what NEXUS automática's demo accounts are doing (its own and its challenge accounts): the money,
// the trades it opened and closed, and, on a tap, why it opened each one (TradeDetailModal). Read-only and made for a
// phone; everything else stays in the Pro mode. Same data as the Pro view: GET /api/paper/autopilot + the live socket.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api/client'
import type { Position } from '../../api/types'
import type { AutopilotSnapshot } from '../../api/types-autopilot'
import { fmtAgo, fmtDateTime, fmtInt, fmtPct, fmtPrice, fmtR } from '../../lib/format'
import { normalizeAutopilot, subscribeAutopilot } from '../paper/AutopilotView'
import TradeDetailModal from '../paper/TradeDetailModal'
import { Disclaimer, Loading } from '../ui'
import './simple.css'

const money = (v: number | null | undefined, signed = false) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const s = Math.abs(v).toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return `${signed ? (v > 0 ? '+' : v < 0 ? '−' : '') : v < 0 ? '−' : ''}${s} $`
}
const tone = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? '' : v > 0 ? 'pos' : 'neg')
const shortSym = (s: string) => s.replace(/^[A-Z_]+:/, '')

export interface SimpleAccount {
  id: number
  name: string
  equity: number
  balance: number
  initial: number
  unrealized: number
  trades: number
  wins: number
  positions: Position[]
  history: Position[]
  target: number | null
  progress: number | null
  status: string | null
  note: string | null
}

const CHALLENGE_STATUS: Record<string, string> = {
  running: 'En curso',
  reached: 'Meta alcanzada',
  time: 'Se acabó el plazo',
  brake: 'Freno de pérdida',
  cancelled: 'Terminada',
}

/** NEXUS automática plus its running challenge accounts (ended ones are left to the Pro mode). */
export function accountsOf(snap: AutopilotSnapshot): SimpleAccount[] {
  const a = snap.account
  const main: SimpleAccount = {
    id: a.id,
    name: a.name || 'NEXUS automática',
    equity: a.equity,
    balance: a.balance,
    initial: a.initial_balance,
    unrealized: a.unrealized_pnl,
    trades: a.trades,
    wins: a.wins,
    positions: snap.positions,
    history: snap.history,
    target: null,
    progress: null,
    status: snap.autopilot?.enabled ? 'Operando' : 'En pausa',
    note: snap.autopilot?.log?.[0]?.text ?? null,
  }
  const challenges = (snap.challenges ?? [])
    .filter((c) => c.status === 'running')
    .map<SimpleAccount>((c) => ({
      id: c.id,
      name: c.name,
      equity: c.account.equity,
      balance: c.account.balance,
      initial: c.initial,
      unrealized: c.account.unrealized_pnl,
      trades: c.account.trades,
      wins: c.account.wins,
      positions: c.positions,
      history: c.history,
      target: c.target,
      progress: c.progress_pct,
      status: CHALLENGE_STATUS[c.status] ?? c.status,
      note: c.log?.[0]?.text ?? null,
    }))
  return [main, ...challenges]
}

function TradeRow({ p, onDetail }: { p: Position; onDetail: (p: Position) => void }) {
  const open = p.status === 'open'
  const why = p.plan?.why ?? null
  return (
    <button type="button" className="sm-trade" onClick={() => onDetail(p)}>
      <span className="sm-trade-top">
        <b>{shortSym(p.symbol)}</b>
        <span className={`sm-side ${p.side === 'long' ? 'pos' : 'neg'}`}>{p.side === 'long' ? 'Compra' : 'Venta'}</span>
        <span className="sm-chip">{p.tf}</span>
        <span className={`sm-pnl mono ${tone(p.pnl)}`}>{money(p.pnl, true)}</span>
      </span>
      <span className="sm-trade-sub">
        {open
          ? `Entró a ${fmtPrice(p.entry_price)} · ${fmtAgo(p.entry_time * 1000)}`
          : `${p.exit_reason ?? 'Cerrada'}${p.r_multiple !== null ? ` · ${fmtR(p.r_multiple)}` : ''} · ${fmtDateTime(p.entry_time * 1000)}`}
      </span>
      {why && <span className="sm-why">{why}</span>}
    </button>
  )
}

export function SimpleAccountView({ acc, onDetail }: { acc: SimpleAccount; onDetail: (p: Position) => void }) {
  const change = acc.equity - acc.initial
  const realized = acc.balance - acc.initial
  const closed = acc.history
  // the trades it opened by signal first; the buy-and-hold holdings (hold) have their own list at the end
  const trades = acc.positions.filter((p) => !p.hold)
  const holdings = acc.positions.filter((p) => p.hold)
  return (
    <>
      <section className="sm-hero">
        <div className="sm-label">Valor de la cuenta</div>
        <div className="sm-value mono">{money(acc.equity)}</div>
        <div className={`sm-change mono ${tone(change)}`}>
          {money(change, true)} ({fmtPct(acc.initial ? (change / acc.initial) * 100 : 0)}) desde {money(acc.initial)}
        </div>
        {acc.target !== null && (
          <div className="sm-goal">
            <div className="sm-goal-bar">
              <span style={{ width: `${Math.max(0, Math.min(100, acc.progress ?? 0))}%` }} />
            </div>
            <span className="tiny muted">
              Meta {money(acc.target)} · {fmtInt(Math.round(acc.progress ?? 0))} % del camino
            </span>
          </div>
        )}
        <div className="sm-figs">
          <div>
            <span>Efectivo</span>
            <b className="mono">{money(acc.balance)}</b>
          </div>
          <div>
            <span>En juego</span>
            <b className={`mono ${tone(acc.unrealized)}`}>{money(acc.unrealized, true)}</b>
          </div>
          <div>
            <span>Ya ganado o perdido</span>
            <b className={`mono ${tone(realized)}`}>{money(realized, true)}</b>
          </div>
          <div>
            <span>Ganadoras</span>
            <b className="mono">
              {fmtInt(acc.wins)} de {fmtInt(acc.trades)}
            </b>
          </div>
        </div>
        {acc.trades > 0 && acc.trades < 30 && <div className="tiny muted">Menos de 30 operaciones cerradas: todavía sin conclusiones.</div>}
        {acc.note && (
          <div className="sm-note">
            <span className="sm-dot" /> {acc.status} · {acc.note}
          </div>
        )}
      </section>

      <section className="sm-block">
        <h3>
          Operaciones abiertas <span className="sm-count">{fmtInt(trades.length)}</span>
        </h3>
        {trades.length ? (
          trades.map((p) => <TradeRow key={p.id} p={p} onDetail={onDetail} />)
        ) : (
          <div className="sm-empty">Sin operaciones abiertas. NEXUS abre una cuando su señal al cierre de una vela lo indica.</div>
        )}
      </section>

      <section className="sm-block">
        <h3>
          Cerradas <span className="sm-count">{fmtInt(closed.length)}</span>
        </h3>
        {closed.length ? (
          closed.slice(0, 30).map((p) => <TradeRow key={p.id} p={p} onDetail={onDetail} />)
        ) : (
          <div className="sm-empty">Todavía ninguna.</div>
        )}
      </section>

      {holdings.length > 0 && (
        <section className="sm-block">
          <h3>
            Cartera (comprar y mantener) <span className="sm-count">{fmtInt(holdings.length)}</span>
          </h3>
          {holdings.map((p) => <TradeRow key={p.id} p={p} onDetail={onDetail} />)}
        </section>
      )}
    </>
  )
}

export default function SimpleView() {
  const [snap, setSnap] = useState<AutopilotSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [accId, setAccId] = useState<number | null>(null)
  const [detailId, setDetailId] = useState<number | null>(null)
  const alive = useRef(true)

  const load = useCallback(async () => {
    try {
      const s = normalizeAutopilot(await api.paper.autopilot.get())
      if (!alive.current) return
      if (s) {
        setSnap(s)
        setError(null)
      }
    } catch (err) {
      if (alive.current) setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    alive.current = true
    void load()
    const off = subscribeAutopilot((s) => alive.current && setSnap(s))
    const id = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, 30000)
    return () => {
      alive.current = false
      off()
      window.clearInterval(id)
    }
  }, [load])

  const accounts = useMemo(() => (snap ? accountsOf(snap) : []), [snap])
  const acc = accounts.find((a) => a.id === accId) ?? accounts[0] ?? null
  const detail = useMemo(() => {
    if (detailId === null) return null
    for (const a of accounts) {
      const p = a.positions.find((x) => x.id === detailId) ?? a.history.find((x) => x.id === detailId)
      if (p) return p
    }
    return null
  }, [accounts, detailId])

  return (
    <div className="sm-page">
      {accounts.length > 1 && (
        <div className="sm-accounts" role="tablist" aria-label="Cuentas demo">
          {accounts.map((a) => (
            <button
              key={a.id}
              type="button"
              role="tab"
              aria-selected={a.id === acc?.id}
              className={`sm-acc ${a.id === acc?.id ? 'active' : ''}`}
              onClick={() => setAccId(a.id)}
            >
              <span>{a.name}</span>
              <b className={`mono ${tone(a.equity - a.initial)}`}>{money(a.equity)}</b>
            </button>
          ))}
        </div>
      )}
      {!snap && !error && <Loading label="Cargando la cuenta…" />}
      {error && !snap && <div className="errorbox">No se pudo cargar la cuenta: {error}</div>}
      {acc && <SimpleAccountView acc={acc} onDetail={(p) => setDetailId(p.id)} />}
      <Disclaimer />
      <TradeDetailModal p={detail} onClose={() => setDetailId(null)} />
    </div>
  )
}
