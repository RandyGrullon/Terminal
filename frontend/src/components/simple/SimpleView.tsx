// Modo simple: an app for the phone (installed as a PWA) with only what NEXUS automática's demo accounts are doing —
// its own and its running challenge accounts: the money, the trades it has open and closed, its buy-and-hold portfolio
// and, on a tap, why it opened each one (TradeDetailModal, a bottom sheet on a phone). Read-only; everything else is
// in the Pro mode. Same data as the Pro view: GET /api/paper/autopilot + the live socket.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Activity, Briefcase, CalendarDays, ChevronDown, ChevronRight, CircleHelp, History, LayoutGrid, LogOut, PieChart, PiggyBank, Plus, Server } from 'lucide-react'
import { api } from '../../api/client'
import type { Position } from '../../api/types'
import type { AutopilotSnapshot } from '../../api/types-autopilot'
import type { WsStatus } from '../../api/ws'
import { fmtAgo, fmtDateTime, fmtInt, fmtPct, fmtPrice, fmtR } from '../../lib/format'
import { normalizeAutopilot, subscribeAutopilot } from '../paper/AutopilotView'
import TradeDetailModal from '../paper/TradeDetailModal'
import { TONE_LABEL, accountLabel, toneOf, twinLabel, twinTone, twinsOf, vaultOf, type AccountTone } from '../paper/NexusAccounts'
import '../paper/nexus-accounts.css'
import { Button, Loading, Modal } from '../ui'
import { GLOSSARY, friendlyReason, nexusNow, priceBarOf, sideWords, sourceOf, speedOf, todayOf, type PriceBar } from './friendly'
import ServerView from './ServerView'
import CalendarView from './CalendarView'
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
  tone: AccountTone // 10k blue, 1k yellow, challenges neutral
  saved: number // what it put in the vault
  mindset: string | null // the 1k account's rules
  goalNote: string | null // how its goal is counted (the 10k account: account + vault)
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
    tone: '10k',
    saved: vaultOf(snap.vault, a.id),
    mindset: null,
    goalNote: snap.main_goal ? `Cuenta + vault: ${money(snap.main_goal.total)}` : null,
  }
  if (snap.main_goal) {
    main.target = snap.main_goal.target
    main.progress = snap.main_goal.progress_pct
  }
  const twin: SimpleAccount[] = twinsOf(snap).map((t) => ({
          id: t.id,
          name: twinLabel(t.initial),
          equity: t.account.equity,
          balance: t.account.balance,
          initial: t.initial,
          unrealized: t.account.unrealized_pnl,
          trades: t.account.trades,
          wins: t.account.wins,
          positions: t.positions,
          history: t.history,
          target: t.target,
          progress: t.progress_pct,
          status: t.reached_at ? '¡Meta alcanzada!' : t.brake.active ? 'Freno activo' : snap.autopilot?.enabled ? 'Operando' : 'En pausa',
          note: t.log?.[0]?.text ?? null,
          noteAt: t.log?.[0]?.ts ?? null,
          tone: twinTone(t.initial),
          saved: vaultOf(snap.vault, t.id),
          mindset: t.mindset || null,
          goalNote: null,
        }))
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
      tone: 'other' as AccountTone,
      saved: 0,
      mindset: null,
      goalNote: null,
    }))
  return [{ ...main, name: 'Cuenta 10k' }, ...twin, ...challenges]
}

function PriceBarView({ bar, p }: { bar: PriceBar; p: Position }) {
  const winning = p.pnl >= 0 // the result, fees included (the price can sit at the entry with the trade still losing)
  return (
    <span
      className="sm-bar-track"
      aria-label={`Precio ${fmtPrice(p.price)}: entre el stop ${fmtPrice(bar.stop)} y ${bar.hasTarget ? `el objetivo ${fmtPrice(bar.end)}` : 'sin objetivo fijo'}`}
    >
      <span className="sm-bar-rail">
        <span className="sm-bar-loss" style={{ width: `${bar.entry * 100}%` }} />
        <span className="sm-bar-entry" style={{ left: `${bar.entry * 100}%` }} />
        <span className={`sm-bar-now ${winning ? 'pos' : 'neg'}`} style={{ left: `${bar.now * 100}%` }} />
      </span>
      <span className="sm-bar-labels">
        <span>Stop {fmtPrice(bar.stop)}</span>
        <span>{bar.hasTarget ? `Objetivo ${fmtPrice(bar.end)}` : bar.endLabel}</span>
      </span>
    </span>
  )
}

const resultWord = (p: Position) =>
  p.status === 'open' ? (p.pnl >= 0 ? 'ganando' : 'perdiendo') : p.pnl > 0 ? 'ganó' : p.pnl < 0 ? 'perdió' : 'quedó igual'

export function TradeRow({ p, onDetail, tone: accTone, showAccount = false }: { p: Position; onDetail: (p: Position) => void; tone?: AccountTone; showAccount?: boolean }) {
  const open = p.status === 'open'
  const long = p.side === 'long'
  const side = sideWords(p)
  const src = sourceOf(p)
  const bar = priceBarOf(p)
  return (
    <button type="button" className={`sm-trade ${long ? 'is-long' : 'is-short'}${accTone ? ` t-${accTone}` : ''}`} onClick={() => onDetail(p)}>
      <span className="sm-trade-row">
        <span className="sm-trade-main">
          <span className="sm-trade-top">
            {showAccount && accTone && <span className={`sm-acct-chip t-${accTone}`}>{TONE_LABEL[accTone].replace('Cuenta ', '')}</span>}
            <b className="sm-sym">{shortSym(p.symbol)}</b>
            <span className={`sm-side ${long ? 'pos' : 'neg'}`}>{side.label}</span>
            <span className="sm-chip">{p.hold ? src.short : `${src.short} · ${speedOf(p.tf).label.toLowerCase()}`}</span>
          </span>
          <span className="sm-trade-sub">
            {open
              ? `Abierta ${fmtAgo(p.entry_time * 1000)} · ${side.hope}`
              : `${exitLabel(p.exit_reason)} · ${fmtDateTime((p.exit_time ?? p.entry_time) * 1000)}`}
          </span>
        </span>
        <span className="sm-trade-side">
          <b className={`mono ${tone(p.pnl)}`}>{money(p.pnl, true)}</b>
          <span className={`sm-r ${tone(p.pnl)}`}>{resultWord(p)}</span>
          <ChevronRight size={16} className="sm-chev" aria-hidden />
        </span>
      </span>
      <span className="sm-why">{friendlyReason(p)}</span>
      {bar && <PriceBarView bar={bar} p={p} />}
    </button>
  )
}

/** An open trade as a square card (the grid of open trades): account, symbol, direction, result, price bar. */
export function TradeCard({ p, onDetail, tone: accTone, showAccount = false }: { p: Position; onDetail: (p: Position) => void; tone?: AccountTone; showAccount?: boolean }) {
  const long = p.side === 'long'
  const side = sideWords(p)
  const src = sourceOf(p)
  const bar = priceBarOf(p)
  return (
    <button
      type="button"
      className={`sm-card ${long ? 'is-long' : 'is-short'}${accTone ? ` t-${accTone}` : ''}`}
      onClick={() => onDetail(p)}
      aria-label={`${shortSym(p.symbol)} ${side.label}: ${money(p.pnl, true)}, ${resultWord(p)}`}
    >
      <span className="sm-card-top">
        {showAccount && accTone && <span className={`sm-acct-chip t-${accTone}`}>{TONE_LABEL[accTone].replace('Cuenta ', '')}</span>}
        <span className={`sm-side ${long ? 'pos' : 'neg'}`}>{side.label}</span>
      </span>
      <b className="sm-card-sym">{shortSym(p.symbol)}</b>
      <span className="sm-card-pnl">
        <b className={`mono ${tone(p.pnl)}`}>{money(p.pnl, true)}</b>
        <span className={`sm-r ${tone(p.pnl)}`}>{resultWord(p)}</span>
      </span>
      <span className="sm-card-sub">
        {p.hold ? src.short : speedOf(p.tf).label} · {fmtAgo(p.entry_time * 1000)}
      </span>
      {bar && (
        <span className="sm-card-bar" aria-hidden>
          <span className="sm-bar-rail">
            <span className="sm-bar-loss" style={{ width: `${bar.entry * 100}%` }} />
            <span className="sm-bar-entry" style={{ left: `${bar.entry * 100}%` }} />
            <span className={`sm-bar-now ${p.pnl >= 0 ? 'pos' : 'neg'}`} style={{ left: `${bar.now * 100}%` }} />
          </span>
        </span>
      )}
    </button>
  )
}

/** A section that folds (accordion), remembering per device whether it is open. */
function Fold({ id, title, count, defaultOpen = false, action, children }: { id: string; title: ReactNode; count?: number; defaultOpen?: boolean; action?: ReactNode; children: ReactNode }) {
  const key = `nexus.fold.${id}`
  const [open, setOpen] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(key)
      return v === null ? defaultOpen : v === '1'
    } catch {
      return defaultOpen
    }
  })
  const toggle = () => {
    setOpen((o) => {
      try {
        localStorage.setItem(key, o ? '0' : '1')
      } catch {
        /* private mode: not remembered */
      }
      return !o
    })
  }
  return (
    <section className={`sm-fold ${open ? 'open' : ''}`}>
      <h2 className="sm-h sm-fold-head">
        <button type="button" className="sm-fold-btn" aria-expanded={open} onClick={toggle}>
          <ChevronDown size={16} className="sm-fold-chev" aria-hidden />
          {title}
          {count !== undefined && <span className="sm-count">{fmtInt(count)}</span>}
        </button>
        {action}
      </h2>
      {open && <div className="sm-fold-body">{children}</div>}
    </section>
  )
}

function TradeSheet({ p, onClose, onFull }: { p: Position | null; onClose: () => void; onFull: () => void }) {
  if (!p) return null
  const open = p.status === 'open'
  const side = sideWords(p)
  const speed = speedOf(p.tf)
  const src = sourceOf(p)
  const bar = priceBarOf(p)
  const param = (re: RegExp) => p.plan?.params?.find((x) => re.test(x.label))?.value ?? null
  const target = p.plan?.target ?? p.tp1
  const rows: [string, string | null][] = [
    ['Entró a', fmtPrice(p.entry_price)],
    [open ? 'Precio ahora' : 'Salió a', fmtPrice(open ? p.price : p.exit_price)],
    ['Stop (corta la pérdida)', p.hold ? `${fmtPrice(p.sl)} (de emergencia)` : fmtPrice(p.sl)],
    ['Objetivo', target !== null && target !== undefined ? fmtPrice(target) : 'sin objetivo fijo'],
    ['Si toca el stop inicial', param(/^Pérdida si toca el stop/)],
    ['Tamaño', param(/^Tamaño/)],
  ]
  return (
    <Modal
      open
      onClose={onClose}
      title={
        <span className="sm-sheet-title">
          {shortSym(p.symbol)} <span className={p.side === 'long' ? 'pos' : 'neg'}>{side.label}</span>
        </span>
      }
      footer={
        <>
          <Button variant="ghost" onClick={onFull}>
            Detalle técnico
          </Button>
          <Button onClick={onClose}>Listo</Button>
        </>
      }
    >
      <div className="sm-sheet">
        <div className="sm-sheet-result">
          <b className={`mono ${tone(p.pnl)}`}>{money(p.pnl, true)}</b>
          <span>
            {open ? `Abierta ${fmtAgo(p.entry_time * 1000)} · va ${resultWord(p)}` : `${exitLabel(p.exit_reason)} · ${resultWord(p)}`}
            {p.r_multiple !== null ? ` (${fmtR(p.r_multiple)})` : ''}
          </span>
        </div>
        <section>
          <h4>¿Por qué la abrió?</h4>
          <p>{friendlyReason(p)}</p>
          {p.plan?.why && <p className="sm-sheet-tech">{p.plan.why}</p>}
        </section>
        {bar && (
          <section>
            <h4>¿Dónde va el precio?</h4>
            <PriceBarView bar={bar} p={p} />
          </section>
        )}
        {open && p.plan?.no_target && (
          <section>
            <h4>¿Cuándo sale?</h4>
            <p>{p.plan.no_target}</p>
          </section>
        )}
        <section>
          <h4>Los números</h4>
          <dl className="sm-sheet-rows">
            {rows
              .filter(([, v]) => v)
              .map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd className="mono">{v}</dd>
                </div>
              ))}
          </dl>
        </section>
        <div className="sm-sheet-tags">
          <span className="sm-chip">{src.kind === 'strategy' ? `Estrategia ${src.strategy}` : src.kind === 'hold' ? 'Cartera de largo plazo' : 'Señal de NEXUS'}</span>
          {!p.hold && <span className="sm-chip">{`${speed.label} · ${speed.detail}`}</span>}
          <span className="sm-chip">{side.hope}</span>
        </div>
        <p className="sm-sheet-tech">Simulado, sin dinero real. No es asesoría financiera.</p>
      </div>
    </Modal>
  )
}

function HelpSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title="¿Cómo leer esto?" footer={<Button onClick={onClose}>Entendido</Button>}>
      <div className="sm-sheet">
        <p>
          NEXUS opera solo en cuentas <b>simuladas</b>: nada de esto es dinero real. Toca cualquier operación para ver por qué la abrió.
        </p>
        <dl className="sm-glossary">
          {GLOSSARY.map((g) => (
            <div key={g.term}>
              <dt>{g.term}</dt>
              <dd>{g.text}</dd>
            </div>
          ))}
        </dl>
        <p className="sm-sheet-tech">La validación de NEXUS no encontró evidencia de ventaja: puede ganar o perder. No es asesoría financiera.</p>
      </div>
    </Modal>
  )
}

function List({
  items,
  empty,
  onDetail,
  toneFor,
  showAccount = false,
  grid = false,
}: {
  items: Position[]
  empty: ReactNode
  onDetail: (p: Position) => void
  toneFor?: (p: Position) => AccountTone
  showAccount?: boolean
  grid?: boolean
}) {
  return items.length ? (
    grid ? (
      <div className="sm-grid">
        {items.map((p) => (
          <TradeCard key={p.id} p={p} onDetail={onDetail} tone={toneFor?.(p)} showAccount={showAccount} />
        ))}
      </div>
    ) : (
      <div className="sm-list">
        {items.map((p) => (
          <TradeRow key={p.id} p={p} onDetail={onDetail} tone={toneFor?.(p)} showAccount={showAccount} />
        ))}
      </div>
    )
  ) : (
    <div className="sm-empty">{empty}</div>
  )
}

type Tab = 'home' | 'history' | 'calendar' | 'portfolio' | 'server'

function Hero({ acc }: { acc: SimpleAccount }) {
  const change = acc.equity - acc.initial
  const realized = acc.balance - acc.initial
  return (
    <section className={`sm-hero acct-${acc.tone}`} data-tone={tone(change) || undefined}>
      <div className="sm-hero-head">
        <span className="sm-label">
          <span className={`sm-acct-dot t-${acc.tone}`} aria-hidden /> {acc.name}
        </span>
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
            <span>
              Reto {money(acc.target)}
              {acc.goalNote ? ` · ${acc.goalNote}` : ''}
            </span>
            <b>{(acc.progress ?? 0).toFixed(1).replace('.', ',')} %</b>
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
      {acc.saved > 0 && (
        <div className="sm-saved">
          <PiggyBank size={14} aria-hidden /> Guardado en el vault: <b className="mono">{money(acc.saved)}</b>
        </div>
      )}
      {acc.mindset && <div className="sm-hint">{acc.mindset}</div>}
      {acc.trades > 0 && acc.trades < 30 && <div className="sm-hint">Menos de 30 operaciones cerradas: todavía sin conclusiones.</div>}
    </section>
  )
}

/** One account as a tappable box of the combined view (it opens that account). */
function AccountBox({ acc, onOpen }: { acc: SimpleAccount; onOpen: () => void }) {
  const change = acc.equity - acc.initial
  const open = acc.positions.filter((p) => !p.hold).length
  return (
    <button type="button" className={`sm-box acct-${acc.tone}`} onClick={onOpen}>
      <span className="sm-box-head">
        <span className={`sm-acct-dot t-${acc.tone}`} aria-hidden />
        <b>{acc.name}</b>
        <span className="sm-box-base">base {money(acc.initial)}</span>
        <ChevronRight size={16} className="sm-chev-inline" aria-hidden />
      </span>
      <span className="sm-box-value mono">{money(acc.equity)}</span>
      <span className="sm-box-row">
        <span className={`sm-pill mono ${tone(change)}`}>{money(change, true)}</span>
        <span>
          {fmtInt(open)} {open === 1 ? 'abierta' : 'abiertas'} · {fmtInt(acc.wins)} de {fmtInt(acc.trades)} ganaron
        </span>
      </span>
      {acc.target !== null && (
        <span className="sm-box-goal">
          <span className="sm-goal-bar">
            <span style={{ width: `${Math.max(1, Math.min(100, acc.progress ?? 0))}%` }} />
          </span>
          <span className="tiny">
            Reto {money(acc.target)} · <b>{(acc.progress ?? 0).toFixed(1).replace('.', ',')} %</b>
            {acc.goalNote ? ` · ${acc.goalNote}` : ''}
          </span>
        </span>
      )}
      {acc.saved > 0 && (
        <span className="sm-box-saved">
          <PiggyBank size={13} aria-hidden /> {money(acc.saved)} en el vault
        </span>
      )}
    </button>
  )
}

function VaultBox({ snap }: { snap: AutopilotSnapshot }) {
  const v = snap.vault
  const last = v?.entries[0]
  return (
    <section className="sm-box acct-vault sm-box-static">
      <span className="sm-box-head">
        <PiggyBank size={16} aria-hidden />
        <b>Vault</b>
        <span className={`sm-status ${v?.enabled ? 'on' : ''}`}>{v?.enabled ? 'Guardando' : 'Apagado'}</span>
      </span>
      <span className="sm-box-value mono">{money(v?.total ?? 0)}</span>
      <span className="sm-box-row">
        <span>{last ? `Último: +${money(last.amount)} de la ${accountLabel(last.account_id, twinsOf(snap), last.name).replace('Cuenta ', '')} ${fmtAgo(last.ts * 1000)}` : 'Guarda lo ganado ya cerrado por encima de cada base.'}</span>
      </span>
      {!v?.enabled && <span className="sm-hint">Actívalo en el modo Pro (Cuenta demo → NEXUS automática).</span>}
    </section>
  )
}

function CreateTwinBox({ onCreated }: { onCreated: (s: AutopilotSnapshot) => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  return (
    <section className="sm-box acct-1k sm-box-static">
      <span className="sm-box-head">
        <span className="sm-acct-dot t-1k" aria-hidden />
        <b>Cuenta 1k</b>
      </span>
      <span className="sm-hint">Una segunda cuenta de 1.000 $ con las mismas señales y mentalidad de cuenta pequeña.</span>
      <button
        type="button"
        className="sm-cta"
        disabled={busy}
        onClick={async () => {
          setBusy(true)
          setErr(null)
          try {
            const s = normalizeAutopilot(await api.paper.autopilot.createTwin())
            if (s) onCreated(s)
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e))
          } finally {
            setBusy(false)
          }
        }}
      >
        <Plus size={15} /> {busy ? 'Creando…' : 'Crear la cuenta de 1k'}
      </button>
      {err && <span className="sm-hint neg">{err}</span>}
    </section>
  )
}

function NowCard({ snap, acc, open }: { snap: AutopilotSnapshot; acc: SimpleAccount; open: Position[] }) {
  const now = nexusNow(snap, acc.id, open)
  return (
    <section className={`sm-now ${now.tone}`}>
      <span className="sm-now-dot" aria-hidden />
      <div>
        <b>{now.title}</b>
        <p>{now.detail}</p>
      </div>
    </section>
  )
}

function TodayCard({ history }: { history: Position[] }) {
  const t = todayOf(history)
  return (
    <section className="sm-today">
      <span className="sm-label">Hoy</span>
      {t.closed ? (
        <div className="sm-today-row">
          <b className={`mono ${tone(t.pnl)}`}>{money(t.pnl, true)}</b>
          <span>
            en {fmtInt(t.closed)} {t.closed === 1 ? 'operación cerrada' : 'operaciones cerradas'} · {fmtInt(t.wins)} de {fmtInt(t.closed)} ganaron
          </span>
        </div>
      ) : (
        <span className="sm-today-empty">Todavía no cerró ninguna operación hoy.</span>
      )}
    </section>
  )
}

export default function SimpleView({ wsStatus, onPro, onSignOut }: { wsStatus: WsStatus; onPro: () => void; onSignOut?: () => void }) {
  const [snap, setSnap] = useState<AutopilotSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [accId, setAccId] = useState<number | null>(null) // null: the combined view (10k, 1k and the vault)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [tab, setTab] = useState<Tab>('home')
  const [full, setFull] = useState(false)
  const [help, setHelp] = useState(false)
  const [serverHealth, setServerHealth] = useState<'ok' | 'warn' | 'bad'>('ok')
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

  const scroller = useRef<HTMLDivElement>(null)
  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 }) // the app scrolls inside .sm-app (the page body never scrolls)
  }, [tab, accId])

  const accounts = useMemo(() => (snap ? accountsOf(snap) : []), [snap])
  const nexus = accounts.filter((a) => a.tone !== 'other' && a.tone !== 'vault') // the combined view: 10k + the small accounts
  const combined = accId === null
  const acc = combined ? null : (accounts.find((a) => a.id === accId) ?? null)
  const twinList = twinsOf(snap)
  const toneFor = (p: Position) => toneOf(p.account_id, twinList)
  const [historyFilter, setHistoryFilter] = useState<'all' | AccountTone>('all')
  const byTime = (field: 'entry_time' | 'exit_time') => (a: Position, b: Position) => ((b[field] ?? 0) as number) - ((a[field] ?? 0) as number)
  const allOpen = nexus.flatMap((a) => a.positions.filter((p) => !p.hold)).sort(byTime('entry_time'))
  const allClosed = nexus.flatMap((a) => a.history).sort(byTime('exit_time'))
  const totalNexus = nexus.reduce((s, a) => s + a.equity, 0) + (snap?.vault?.total ?? 0)
  const detail = useMemo(() => {
    if (detailId === null) return null
    for (const a of accounts) {
      const p = a.positions.find((x) => x.id === detailId) ?? a.history.find((x) => x.id === detailId)
      if (p) return p
    }
    return null
  }, [accounts, detailId])
  const onDetail = (p: Position) => {
    setFull(false)
    setDetailId(p.id)
  }
  const trades = acc ? acc.positions.filter((p) => !p.hold) : allOpen
  const holdings = (acc ?? nexus[0] ?? null)?.positions.filter((p) => p.hold) ?? []
  const holdValue = holdings.reduce((s, p) => s + (Number.isFinite(p.pnl) ? p.pnl : 0), 0)

  const tabs: { id: Tab; label: string; icon: ReactNode; count?: number }[] = [
    { id: 'home', label: 'Inicio', icon: <Activity size={20} />, count: trades.length || undefined },
    { id: 'history', label: 'Historial', icon: <History size={20} /> },
    { id: 'calendar', label: 'Calendario', icon: <CalendarDays size={20} /> },
    { id: 'portfolio', label: 'Cartera', icon: <PieChart size={20} />, count: holdings.length || undefined },
    { id: 'server', label: 'Servidor', icon: <Server size={20} /> },
  ]

  return (
    <div className="sm-app" ref={scroller}>
      <header className="sm-bar">
        <span className="brand-mark">N</span>
        <div className="sm-bar-title">
          <b>NEXUS</b>
          <span className={`sm-live ${wsStatus}`}>
            <i />
            {wsStatus === 'open' ? 'En vivo' : wsStatus === 'connecting' ? 'Conectando…' : 'Sin conexión'}
          </span>
        </div>
        <button type="button" className="sm-icon-btn" onClick={() => setHelp(true)} title="¿Cómo leer esto?" aria-label="¿Cómo leer esto?">
          <CircleHelp size={18} />
        </button>
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

      {accounts.length > 0 && tab !== 'server' && tab !== 'calendar' && (
        <div className="sm-accounts" role="tablist" aria-label="Cuentas demo">
          <button
            type="button"
            role="tab"
            aria-selected={combined}
            className={`sm-acc acct-all ${combined ? 'active' : ''}`}
            onClick={() => setAccId(null)}
          >
            <span>
              <span className="sm-acct-dot t-10k" aria-hidden />
              <span className="sm-acct-dot t-1k" aria-hidden />
              <span className="sm-acct-dot t-vault" aria-hidden /> Todo NEXUS
            </span>
            <b className="mono">{money(totalNexus)}</b>
          </button>
          {accounts.map((a) => (
            <button
              key={a.id}
              type="button"
              role="tab"
              aria-selected={a.id === acc?.id}
              className={`sm-acc acct-${a.tone} ${a.id === acc?.id ? 'active' : ''}`}
              onClick={() => setAccId(a.id)}
            >
              <span>
                <span className={`sm-acct-dot t-${a.tone}`} aria-hidden /> {a.name}
              </span>
              <b className={`mono ${tone(a.equity - a.initial)}`}>{money(a.equity)}</b>
            </button>
          ))}
        </div>
      )}

      <main className="sm-content">
        {tab === 'server' && <ServerView wsStatus={wsStatus} onHealth={setServerHealth} />}
        {tab === 'calendar' && <CalendarView toneOfAccount={(id) => toneOf(id, twinList)} />}
        {!snap && !error && tab !== 'server' && tab !== 'calendar' && <Loading label="Cargando la cuenta…" />}
        {error && !snap && <div className="errorbox">No se pudo cargar la cuenta: {error}</div>}
        {snap && combined && tab === 'home' && (
          <>
            <div className="sm-stack">
              {nexus.map((a) => (
                <AccountBox key={a.id} acc={a} onOpen={() => setAccId(a.id)} />
              ))}
              {!twinList.some((t) => t.initial >= 1000) && <CreateTwinBox onCreated={setSnap} />}
              <VaultBox snap={snap} />
            </div>
            <NowCard snap={snap} acc={nexus[0]} open={allOpen.filter((p) => p.account_id === nexus[0]?.id)} />
            <TodayCard history={allClosed} />
            <h2 className="sm-h">
              Operaciones abiertas <span className="sm-count">{fmtInt(allOpen.length)}</span>
            </h2>
            <List
              items={allOpen}
              onDetail={onDetail}
              toneFor={toneFor}
              showAccount
              grid
              empty="Sin operaciones abiertas en ninguna cuenta. NEXUS abre una cuando su señal al cierre de una vela lo indica."
            />
            {allClosed.length > 0 && (
              <Fold
                id="closed-all"
                title="Últimas cerradas"
                count={Math.min(allClosed.length, 6)}
                action={
                  <button type="button" className="sm-link" onClick={() => setTab('history')}>
                    Ver todas <ChevronRight size={14} />
                  </button>
                }
              >
                <List items={allClosed.slice(0, 6)} onDetail={onDetail} toneFor={toneFor} showAccount empty={null} />
              </Fold>
            )}
          </>
        )}
        {snap && combined && tab === 'history' && (
          <>
            <h2 className="sm-h sm-h-first">
              Historial <span className="sm-count">{fmtInt(allClosed.length)}</span>
            </h2>
            <div className="sm-filter" role="group" aria-label="Filtrar por cuenta">
              {(['all', '10k', ...twinList.map((t) => twinTone(t.initial))] as Array<'all' | AccountTone>).map((f) => (
                <button key={f} type="button" className={`sm-filter-btn ${f !== 'all' ? `t-${f}` : ''} ${historyFilter === f ? 'active' : ''}`} onClick={() => setHistoryFilter(f)}>
                  {f === 'all' ? 'Todas' : TONE_LABEL[f].replace('Cuenta ', '')}
                </button>
              ))}
            </div>
            <List
              items={historyFilter === 'all' ? allClosed : allClosed.filter((p) => toneFor(p) === historyFilter)}
              onDetail={onDetail}
              toneFor={toneFor}
              showAccount
              empty="Todavía ninguna operación cerrada."
            />
          </>
        )}
        {snap && combined && tab === 'portfolio' && (
          <>
            <h2 className="sm-h sm-h-first">
              Cartera de la cuenta 10k <span className="sm-count">{fmtInt(holdings.length)}</span>
            </h2>
            <p className="sm-hint">Comprar y mantener: no busca objetivos; se queda hasta que la vendas o toque su stop de emergencia.</p>
            <List items={holdings} onDetail={onDetail} toneFor={toneFor} empty={<><Briefcase size={18} /> No hay cartera de largo plazo ahora.</>} />
          </>
        )}
        {acc && tab === 'home' && (
          <>
            <Hero acc={acc} />
            {snap && <NowCard snap={snap} acc={acc} open={trades} />}
            <TodayCard history={acc.history} />
            {acc.note && (
              <div className="sm-note">
                <span className="sm-note-label">Última decisión{acc.noteAt ? ` · ${fmtAgo(acc.noteAt)}` : ''}</span>
                <span>{acc.note}</span>
              </div>
            )}
            <h2 className="sm-h">
              Operaciones abiertas <span className="sm-count">{fmtInt(trades.length)}</span>
            </h2>
            <List items={trades} onDetail={onDetail} toneFor={toneFor} grid empty="Sin operaciones abiertas. NEXUS abre una cuando su señal al cierre de una vela lo indica." />
            {acc.history.length > 0 && (
              <Fold
                id="closed-one"
                title="Últimas cerradas"
                count={Math.min(acc.history.length, 6)}
                action={
                  <button type="button" className="sm-link" onClick={() => setTab('history')}>
                    Ver todas <ChevronRight size={14} />
                  </button>
                }
              >
                <List items={acc.history.slice(0, 6)} onDetail={onDetail} toneFor={toneFor} empty={null} />
              </Fold>
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
            <List items={acc.history} onDetail={onDetail} toneFor={toneFor} empty="Todavía ninguna operación cerrada." />
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
              {t.id === 'server' && (wsStatus !== 'open' || serverHealth !== 'ok') && (
                <span className={`sm-health ${wsStatus !== 'open' ? 'bad' : serverHealth}`} aria-label="Hay avisos del servidor" />
              )}
            </span>
            <span>{t.label}</span>
          </button>
        ))}
      </nav>

      {full ? (
        <TradeDetailModal p={detail} onClose={() => setDetailId(null)} />
      ) : (
        <TradeSheet p={detail} onClose={() => setDetailId(null)} onFull={() => setFull(true)} />
      )}
      <HelpSheet open={help} onClose={() => setHelp(false)} />
    </div>
  )
}
