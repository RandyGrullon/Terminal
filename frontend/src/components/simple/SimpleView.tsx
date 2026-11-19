// Modo simple: an app for the phone (installed as a PWA) with only what NEXUS automática's demo accounts are doing —
// its own and its running challenge accounts: the money, the trades it has open and closed, its buy-and-hold portfolio
// and, on a tap, why it opened each one (TradeDetailModal, a bottom sheet on a phone). Read-only; everything else is
// in the Pro mode. Same data as the Pro view: GET /api/paper/autopilot + the live socket.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Activity, Briefcase, ChevronRight, History, LayoutGrid, LogOut, PieChart } from 'lucide-react'
import { api } from '../../api/client'
import type { Position } from '../../api/types'
import type { AutopilotSnapshot } from '../../api/types-autopilot'
import type { WsStatus } from '../../api/ws'
import { fmtAgo, fmtDateTime, fmtInt, fmtPct, fmtPrice, fmtR } from '../../lib/format'
import { normalizeAutopilot, subscribeAutopilot } from '../paper/AutopilotView'
import TradeDetailModal from '../paper/TradeDetailModal'
import { Loading } from '../ui'
import './simple.css'

const money = (v: number | null | undefined, signed = false) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const s = Math.abs(v).toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return `${signed ? (v > 0 ? '+' : v < 0 ? '−' : '') : v < 0 ? '−' : ''}${s} $`
}
const tone = (v: number | null | undefined) => (v === null || v === undefined || Math.abs(v) < 0.005 ? '' : v > 0 ? 'pos' : 'neg')
const shortSym = (s: string) => s.replace(/^[A-Z_]+:/, '').replace(/\.P$/, '')

const EXIT_LABEL: Record<string, string> = {
  MANUAL: 'Cerrada a mano',
  SL: 'Tocó el stop',
  TP1: 'Objetivo 1',
  TP2: 'Objetivo 2',
  TP3: 'Objetivo 3',
  TIME: 'Por tiempo',
  BE: 'Sin pérdida (stop a la entrada)',
  SIGNAL: 'Por señal',
  EXIT: 'Por señal',
}
export const exitLabel = (r: string | null) => (r ? (EXIT_LABEL[r.toUpperCase()] ?? r) : 'Cerrada')

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
  noteAt: number | null
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
    noteAt: snap.autopilot?.log?.[0]?.ts ?? null,
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
      noteAt: c.log?.[0]?.ts ?? null,
    }))
  return [main, ...challenges]
}

export function TradeRow({ p, onDetail }: { p: Position; onDetail: (p: Position) => void }) {
  const open = p.status === 'open'
  const long = p.side === 'long'
  return (
    <button type="button" className={`sm-trade ${long ? 'is-long' : 'is-short'}`} onClick={() => onDetail(p)}>
      <span className="sm-trade-main">
        <span className="sm-trade-top">
          <b className="sm-sym">{shortSym(p.symbol)}</b>
          <span className={`sm-side ${long ? 'pos' : 'neg'}`}>{long ? 'Compra' : 'Venta'}</span>
          <span className="sm-chip">{p.tf}</span>
        </span>
        <span className="sm-trade-sub">
          {open
            ? `Entró a ${fmtPrice(p.entry_price)} · ${fmtAgo(p.entry_time * 1000)}`
            : `${exitLabel(p.exit_reason)} · ${fmtDateTime(p.entry_time * 1000)}`}
        </span>
        {p.plan?.why && <span className="sm-why">{p.plan.why}</span>}
      </span>
      <span className="sm-trade-side">
        <b className={`mono ${tone(p.pnl)}`}>{money(p.pnl, true)}</b>
        {p.r_multiple !== null && <span className={`sm-r mono ${tone(p.r_multiple)}`}>{fmtR(p.r_multiple)}</span>}
        <ChevronRight size={16} className="sm-chev" aria-hidden />
      </span>
    </button>
  )
}

function List({ items, empty, onDetail }: { items: Position[]; empty: ReactNode; onDetail: (p: Position) => void }) {
  return items.length ? (
    <div className="sm-list">
      {items.map((p) => (
        <TradeRow key={p.id} p={p} onDetail={onDetail} />
      ))}
    </div>
  ) : (
    <div className="sm-empty">{empty}</div>
  )
}

type Tab = 'home' | 'history' | 'portfolio'

function Hero({ acc }: { acc: SimpleAccount }) {
  const change = acc.equity - acc.initial
  const realized = acc.balance - acc.initial
  return (
    <section className="sm-hero" data-tone={tone(change) || undefined}>
      <div className="sm-hero-head">
        <span className="sm-label">Valor de la cuenta</span>
        {acc.status && <span className="sm-status">{acc.status}</span>}
      </div>
      <div className="sm-value mono">{money(acc.equity)}</div>
      <div className="sm-change-row">
        <span className={`sm-pill mono ${tone(change)}`}>
          {money(change, true)} · {fmtPct(acc.initial ? (change / acc.initial) * 100 : 0)}
        </span>
        <span className="tiny muted">desde {money(acc.initial)}</span>
      </div>
      {acc.target !== null && (
        <div className="sm-goal">
          <div className="sm-goal-bar">
            <span style={{ width: `${Math.max(0, Math.min(100, acc.progress ?? 0))}%` }} />
          </div>
          <div className="sm-goal-text tiny">
            <span>Meta {money(acc.target)}</span>
            <b>{fmtInt(Math.round(acc.progress ?? 0))} %</b>
          </div>
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
          <span>Ya cerrado</span>
          <b className={`mono ${tone(realized)}`}>{money(realized, true)}</b>
        </div>
        <div>
          <span>Ganadoras</span>
          <b className="mono">
            {fmtInt(acc.wins)} de {fmtInt(acc.trades)}
          </b>
        </div>
      </div>
      {acc.trades > 0 && acc.trades < 30 && <div className="sm-hint">Menos de 30 operaciones cerradas: todavía sin conclusiones.</div>}
    </section>
  )
}

export default function SimpleView({ wsStatus, onPro, onSignOut }: { wsStatus: WsStatus; onPro: () => void; onSignOut?: () => void }) {
  const [snap, setSnap] = useState<AutopilotSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [accId, setAccId] = useState<number | null>(null)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [tab, setTab] = useState<Tab>('home')
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
    const onVisible = () => document.visibilityState === 'visible' && void load()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive.current = false
      off()
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [load])

  useEffect(() => {
    window.scrollTo({ top: 0 })
  }, [tab, accId])

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
  const onDetail = (p: Position) => setDetailId(p.id)
  const trades = acc ? acc.positions.filter((p) => !p.hold) : []
  const holdings = acc ? acc.positions.filter((p) => p.hold) : []
  const holdValue = holdings.reduce((s, p) => s + (Number.isFinite(p.pnl) ? p.pnl : 0), 0)

  const tabs: { id: Tab; label: string; icon: ReactNode; count?: number }[] = [
    { id: 'home', label: 'Inicio', icon: <Activity size={20} />, count: trades.length || undefined },
    { id: 'history', label: 'Historial', icon: <History size={20} /> },
    { id: 'portfolio', label: 'Cartera', icon: <PieChart size={20} />, count: holdings.length || undefined },
  ]

  return (
    <div className="sm-app">
      <header className="sm-bar">
        <span className="brand-mark">N</span>
        <div className="sm-bar-title">
          <b>NEXUS</b>
          <span className={`sm-live ${wsStatus}`}>
            <i />
            {wsStatus === 'open' ? 'En vivo' : wsStatus === 'connecting' ? 'Conectando…' : 'Sin conexión'}
          </span>
        </div>
        <button type="button" className="sm-icon-btn" onClick={onPro} title="Modo Pro: todas las herramientas">
          <LayoutGrid size={18} />
          <span>Pro</span>
        </button>
        {onSignOut && (
          <button type="button" className="sm-icon-btn" onClick={onSignOut} title="Cerrar sesión" aria-label="Cerrar sesión">
            <LogOut size={18} />
          </button>
        )}
      </header>

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

      <main className="sm-content">
        {!snap && !error && <Loading label="Cargando la cuenta…" />}
        {error && !snap && <div className="errorbox">No se pudo cargar la cuenta: {error}</div>}
        {acc && tab === 'home' && (
          <>
            <Hero acc={acc} />
            {acc.note && (
              <div className="sm-note">
                <span className="sm-note-label">Última decisión{acc.noteAt ? ` · ${fmtAgo(acc.noteAt)}` : ''}</span>
                <span>{acc.note}</span>
              </div>
            )}
            <h2 className="sm-h">
              Operaciones abiertas <span className="sm-count">{fmtInt(trades.length)}</span>
            </h2>
            <List items={trades} onDetail={onDetail} empty="Sin operaciones abiertas. NEXUS abre una cuando su señal al cierre de una vela lo indica." />
            {acc.history.length > 0 && (
              <>
                <h2 className="sm-h">
                  Últimas cerradas
                  <button type="button" className="sm-link" onClick={() => setTab('history')}>
                    Ver todas <ChevronRight size={14} />
                  </button>
                </h2>
                <List items={acc.history.slice(0, 3)} onDetail={onDetail} empty={null} />
              </>
            )}
          </>
        )}
        {acc && tab === 'history' && (
          <>
            <h2 className="sm-h sm-h-first">
              Historial <span className="sm-count">{fmtInt(acc.history.length)}</span>
            </h2>
            <div className="sm-summary">
              <div>
                <span>Ganadoras</span>
                <b className="mono">
                  {fmtInt(acc.wins)} de {fmtInt(acc.trades)}
                </b>
              </div>
              <div>
                <span>Resultado cerrado</span>
                <b className={`mono ${tone(acc.balance - acc.initial)}`}>{money(acc.balance - acc.initial, true)}</b>
              </div>
            </div>
            <List items={acc.history} onDetail={onDetail} empty="Todavía ninguna operación cerrada." />
          </>
        )}
        {acc && tab === 'portfolio' && (
          <>
            <h2 className="sm-h sm-h-first">
              Cartera <span className="sm-count">{fmtInt(holdings.length)}</span>
            </h2>
            <div className="sm-summary">
              <div>
                <span>Activos</span>
                <b className="mono">{fmtInt(holdings.length)}</b>
              </div>
              <div>
                <span>Resultado</span>
                <b className={`mono ${tone(holdValue)}`}>{money(holdValue, true)}</b>
              </div>
            </div>
            <p className="sm-hint">Comprar y mantener: no busca objetivos; se queda hasta que la vendas o toque su stop de emergencia.</p>
            <List
              items={holdings}
              onDetail={onDetail}
              empty={
                <>
                  <Briefcase size={18} /> Esta cuenta no tiene cartera.
                </>
              }
            />
          </>
        )}
        <p className="sm-disclaimer">Simulado, sin dinero real. Herramienta educativa: no es asesoría financiera.</p>
      </main>

      <nav className="sm-tabs" aria-label="Secciones">
        {tabs.map((t) => (
          <button key={t.id} type="button" className={`sm-tab ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)} aria-current={tab === t.id ? 'page' : undefined}>
            <span className="sm-tab-icon">
              {t.icon}
              {t.count !== undefined && <span className="sm-badge">{t.count}</span>}
            </span>
            <span>{t.label}</span>
          </button>
        ))}
      </nav>

      <TradeDetailModal p={detail} onClose={() => setDetailId(null)} />
    </div>
  )
}
