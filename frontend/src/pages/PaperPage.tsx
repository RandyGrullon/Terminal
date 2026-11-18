// Cuenta demo, with an account switch at the top (remembered per browser): "Mi cuenta" and "NEXUS automática".
// Mi cuenta: simulated (paper) trading account 1, friendly first (components/paper/DemoKit): the value of the account in
// big, then three sections (remembered per browser): «Resumen» (a tile per open position with its path to the target,
// pending orders and the latest moves), «Operar» (the order ticket with the Trade Gate verdict before opening,
// auto-trading, the portfolio risk) and «Detalle» (stats, the full tables of positions, orders and history). Reset. Live updates arrive over the socket ('paper' topic: account, positions and the
// optional `orders` / `risk`); the account is also polled every 20 s as a fallback.
// NEXUS automática: NEXUS's own demo account (id 3, components/paper/AutopilotView), which trades on its own. Only
// the selected account is mounted, so only its requests, polling and socket listener run.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Bot, CirclePlus, History as HistoryIcon, Hourglass, LayoutDashboard, ListChecks, OctagonAlert, RefreshCw, RotateCcw, Wallet } from 'lucide-react'
import { api, errorMessage } from '../api/client'
import type { PaperLiveExtra, PaperOrder, PaperRisk, PaperSnapshot, Position } from '../api/types'
import { socket } from '../api/ws'
import { fmtAgo, fmtInt, fmtR, splitSymbol } from '../lib/format'
import { isOne, plural } from '../lib/plural'
import { useApp } from '../store'
import { Badge, Button, Card, Disclaimer, Empty, ErrorBox, Loading, Modal } from '../components/ui'
import { asTimeframe, fmtMoney, useInterval, usePersistentState } from '../components/markets/opsUtils'
import PaperStats from '../components/paper/PaperStats'
import PositionsTable from '../components/paper/PositionsTable'
import HistoryTable from '../components/paper/HistoryTable'
import OrderCard from '../components/paper/OrderCard'
import AutoTradeCard, { type AutoState } from '../components/paper/AutoTradeCard'
import ResetModal from '../components/paper/ResetModal'
import PendingOrdersTable from '../components/paper/PendingOrdersTable'
import RiskPanel from '../components/paper/RiskPanel'
import { paperOrdersApi } from '../components/paper/ordersApi'
import { normalizeOrders, normalizeRisk } from '../components/paper/ordersUtils'
import { adviceLevel, isAccount, normalizePositions, normalizeSnapshot } from '../components/paper/paperUtils'
import { AccountHero, ActivityFeed, Block, SectionTabs, TradeTile, activityOf, type SectionTab } from '../components/paper/DemoKit'
import AutopilotView from '../components/paper/AutopilotView'
import '../components/paper/paper.css'
import '../components/paper/demo.css'

const POLL_MS = 20_000
const RELOAD_DEBOUNCE_MS = 600
const RECENT_ORDERS = 20

// ----------------------------------------------------------------------------- account switch

export type PaperAccountView = 'mine' | 'nexus'
/** Per-browser memory of the selected account (localStorage, wrapped in try/catch by usePersistentState). */
const PAPER_ACCOUNT_KEY = 'nexus.paper.account.v1'
const isAccountView = (v: unknown): v is PaperAccountView => v === 'mine' || v === 'nexus'

const ACCOUNTS: { id: PaperAccountView; label: string; sub: string; icon: ReactNode }[] = [
  { id: 'mine', label: 'Mi cuenta', sub: 'Tú decides cada operación', icon: <Wallet size={17} /> },
  { id: 'nexus', label: 'NEXUS automática', sub: 'Opera sola con sus señales', icon: <Bot size={17} /> },
]

function AccountSwitch({ value, onChange }: { value: PaperAccountView; onChange: (v: PaperAccountView) => void }) {
  return (
    <div className="col gap-1">
      <div className="dk-switch" role="tablist" aria-label="Cuenta demo">
        {ACCOUNTS.map((a) => (
          <button key={a.id} type="button" role="tab" aria-selected={a.id === value} className={`dk-switch-btn${a.id === value ? ' active' : ''}`} onClick={() => onChange(a.id)}>
            <span className="dk-switch-icon">{a.icon}</span>
            <span className="dk-switch-text">
              <b>{a.label}</b>
              <span>{a.sub}</span>
            </span>
          </button>
        ))}
      </div>
      <p className="dk-switch-note">Dos cuentas demo separadas y simuladas: lo que NEXUS hace en la suya nunca toca la tuya.</p>
    </div>
  )
}

export default function PaperPage() {
  const [view, setView] = usePersistentState<PaperAccountView>(PAPER_ACCOUNT_KEY, 'mine', isAccountView)
  return (
    <div className="page pp-page">
      <AccountSwitch value={view} onChange={setView} />
      {view === 'nexus' ? <AutopilotView /> : <MyAccount />}
    </div>
  )
}

/** Sections of «Mi cuenta» (remembered per browser). */
export type MyAccountSection = 'resumen' | 'operar' | 'detalle'
const MY_SECTION_KEY = 'nexus.paper.mine.section.v1'
const isMySection = (v: unknown): v is MyAccountSection => v === 'resumen' || v === 'operar' || v === 'detalle'
const ORIGIN_TAG: Record<string, string> = { manual: 'Manual', auto: 'Auto-trading', sim: 'Simulación', script: 'Script' }

// ----------------------------------------------------------------------------- Mi cuenta (account 1)

export function MyAccount({ section }: { section?: MyAccountSection } = {}) {
  const notify = useApp((s) => s.notify)
  const [snap, setSnapState] = useState<PaperSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loadedAt, setLoadedAt] = useState<number | null>(null)
  const [closingId, setClosingId] = useState<number | null>(null)
  const [confirmAll, setConfirmAll] = useState(false)
  const [closingAll, setClosingAll] = useState(false)
  const [resetOpen, setResetOpen] = useState(false)
  const [orders, setOrdersState] = useState<PaperOrder[]>([])
  const [recentOrders, setRecentOrders] = useState<PaperOrder[]>([])
  const [risk, setRisk] = useState<PaperRisk | null>(null)
  const [cancellingId, setCancellingId] = useState<number | null>(null)
  const [confirmCancelAll, setConfirmCancelAll] = useState(false)
  const [, setTick] = useState(0)
  const [storedSection, setSection] = usePersistentState<MyAccountSection>(MY_SECTION_KEY, 'resumen', isMySection)
  const shown = section ?? storedSection
  const ordersRef = useRef<PaperOrder[]>([])

  const setOrders = useCallback((next: PaperOrder[]) => {
    ordersRef.current = next
    setOrdersState(next)
  }, [])

  const alive = useRef(true)
  const seq = useRef(0)
  const snapRef = useRef<PaperSnapshot | null>(null)
  const reloadTimer = useRef<number | null>(null)

  const setSnap = useCallback((next: PaperSnapshot | null) => {
    snapRef.current = next
    setSnapState(next)
  }, [])

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      if (reloadTimer.current) window.clearTimeout(reloadTimer.current)
    }
  }, [])

  const load = useCallback(
    async (quiet = false) => {
      const id = ++seq.current
      if (!quiet) setLoading(true)
      try {
        // pending orders and the risk panel are optional: a failure there never hides the account
        const [res, ordersRes, riskRes] = await Promise.all([
          api.paper.account(),
          paperOrdersApi.list('all', RECENT_ORDERS).catch(() => null),
          paperOrdersApi.risk().catch(() => null),
        ])
        if (!alive.current || id !== seq.current) return
        const s = normalizeSnapshot(res)
        if (!s) throw new Error('Respuesta de la cuenta demo no válida')
        setSnap(s)
        if (ordersRes) {
          const all = normalizeOrders(ordersRes.orders)
          setOrders(all.filter((o) => o.status === 'pending'))
          setRecentOrders(all.filter((o) => o.status !== 'pending'))
        }
        if (riskRes) setRisk(normalizeRisk(riskRes))
        setError(null)
        setLoadedAt(Date.now())
      } catch (err) {
        if (!alive.current || id !== seq.current) return
        setError(errorMessage(err))
      } finally {
        if (alive.current && id === seq.current) setLoading(false)
      }
    },
    [setSnap, setOrders],
  )

  const scheduleReload = useCallback(() => {
    if (reloadTimer.current) return
    reloadTimer.current = window.setTimeout(() => {
      reloadTimer.current = null
      if (alive.current) void load(true)
    }, RELOAD_DEBOUNCE_MS)
  }, [load])

  useEffect(() => {
    void load()
  }, [load])
  useInterval(() => void load(true), POLL_MS)
  useInterval(() => setTick((t) => t + 1), 15_000) // keeps "hace X" labels fresh

  // Live account + open positions ('paper' is a default topic: listen only). History is not in the message, so a
  // full reload is scheduled whenever a position disappears or the trade count changes.
  useEffect(() => {
    const off = socket.on('paper', (msg) => {
      if (!msg || !isAccount(msg.account) || msg.account.id !== 1) return
      const positions = normalizePositions(msg.positions)
      const prev = snapRef.current
      const ids = new Set(positions.map((p) => p.id))
      const closedSome = !!prev && prev.positions.some((p) => !ids.has(p.id))
      const account = { ...msg.account, auto_symbols: Array.isArray(msg.account.auto_symbols) ? msg.account.auto_symbols : [] }
      setSnap(prev ? { ...prev, account, positions } : { account, positions, history: [] })
      // optional fields (pending orders + portfolio risk): an order that left the list went to the recent ones
      const extra = msg as unknown as PaperLiveExtra
      let ordersGone = false
      if (Array.isArray(extra.orders)) {
        const next = normalizeOrders(extra.orders)
        const ids = new Set(next.map((o) => o.id))
        ordersGone = ordersRef.current.some((o) => !ids.has(o.id))
        setOrders(next)
      }
      if (extra.risk !== undefined) setRisk(normalizeRisk(extra.risk))
      setLoadedAt(Date.now())
      if (!prev || closedSome || ordersGone || prev.account.trades !== account.trades) scheduleReload()
    })
    return off
  }, [scheduleReload, setSnap, setOrders])

  // ------------------------------------------------------------------ actions
  const openChart = useCallback((p: Position) => useApp.getState().showTrade(p), [])

  const closePosition = useCallback(
    async (p: Position) => {
      setClosingId(p.id)
      try {
        const res = await api.paper.close({ position_id: p.id })
        const closed = normalizePositions(res?.closed)
        const c = closed.find((x) => x.id === p.id) ?? closed[0]
        notify(
          c
            ? `Cerrada ${splitSymbol(c.symbol).ticker} ${c.side === 'long' ? 'largo' : 'corto'} · ${fmtR(c.r_multiple)} · ${fmtMoney(c.pnl, true)}`
            : `Posición #${p.id} cerrada`,
          'trade',
        )
      } catch (err) {
        notify(`No se pudo cerrar la posición: ${errorMessage(err)}`, 'error')
      } finally {
        if (alive.current) setClosingId(null)
        void load(true)
      }
    },
    [load, notify],
  )

  const closeAll = useCallback(async () => {
    setClosingAll(true)
    try {
      const res = await api.paper.close({ all: true })
      const closed = normalizePositions(res?.closed)
      const pnl = closed.reduce((acc, p) => acc + (Number.isFinite(p.pnl) ? p.pnl : 0), 0)
      notify(`${plural(closed.length, 'posición cerrada', 'posiciones cerradas')} · ${fmtMoney(pnl, true)}`, 'trade')
      if (alive.current) setConfirmAll(false)
    } catch (err) {
      notify(`No se pudieron cerrar las posiciones: ${errorMessage(err)}`, 'error')
    } finally {
      if (alive.current) setClosingAll(false)
      void load(true)
    }
  }, [load, notify])

  const cancelOrder = useCallback(
    async (o: PaperOrder) => {
      setCancellingId(o.id)
      try {
        await paperOrdersApi.cancel({ order_id: o.id })
        notify(`Orden demo #${o.id} cancelada: ${o.label.toLowerCase()} ${splitSymbol(o.symbol).ticker}`, 'info')
      } catch (err) {
        notify(`No se pudo cancelar la orden: ${errorMessage(err)}`, 'error')
      } finally {
        if (alive.current) setCancellingId(null)
        void load(true)
      }
    },
    [load, notify],
  )

  const cancelAllOrders = useCallback(async () => {
    setCancellingId(-1)
    try {
      const res = await paperOrdersApi.cancel({ all: true })
      const n = Array.isArray(res?.cancelled) ? res.cancelled.length : 0
      notify(plural(n, 'orden pendiente cancelada', 'órdenes pendientes canceladas'), 'info')
      if (alive.current) setConfirmCancelAll(false)
    } catch (err) {
      notify(`No se pudieron cancelar las órdenes: ${errorMessage(err)}`, 'error')
    } finally {
      if (alive.current) setCancellingId(null)
      void load(true)
    }
  }, [load, notify])

  const openOrderChart = useCallback((o: PaperOrder) => useApp.getState().openChart(o.symbol, asTimeframe(o.tf)), [])

  const onAutoChanged = useCallback(
    (auto: AutoState) => {
      const prev = snapRef.current
      if (!prev) return
      setSnap({ ...prev, account: { ...prev.account, auto_trade: auto.enabled, auto_symbols: auto.symbols, auto_tf: auto.tf } })
    },
    [setSnap],
  )

  const onOpened = useCallback(() => void load(true), [load])
  const onReset = useCallback(
    (s: PaperSnapshot | null) => {
      if (s) {
        setSnap(s)
        setLoadedAt(Date.now())
      }
      void load(true)
      notify('Cuenta demo reiniciada', 'success')
    },
    [load, notify, setSnap],
  )

  const positions = snap?.positions ?? []
  const history = snap?.history ?? []
  const toClose = useMemo(() => positions.filter((p) => adviceLevel(p) === 'close').length, [positions])
  const activity = useMemo(() => activityOf(positions, history, 10), [positions, history])

  // ------------------------------------------------------------------ blocks
  const positionsCard = (
    <Card
      flush
      title={
        <>
          Posiciones abiertas <Badge tone={positions.length ? 'accent' : 'muted'}>{fmtInt(positions.length)}</Badge>
          {toClose > 0 && (
            <Badge tone="neg" title="Posiciones en las que NEXUS recomienda cerrar ya">
              <OctagonAlert size={11} /> {toClose} para cerrar
            </Badge>
          )}
        </>
      }
      sub="SL «hasta aquí», objetivos parciales y consejo de NEXUS en vivo"
      actions={
        positions.length > 1 ? (
          <Button size="sm" variant="ghost" onClick={() => setConfirmAll(true)} disabled={closingId !== null || closingAll}>
            Cerrar todas
          </Button>
        ) : undefined
      }
    >
      {positions.length > 0 ? (
        <PositionsTable positions={positions} closingId={closingId} onClose={closePosition} onOpenChart={openChart} />
      ) : (
        <Empty icon={<Wallet size={22} />} title="Sin posiciones abiertas">
          Abre una orden demo con el filtro como guía, o activa el auto-trading.
        </Empty>
      )}
    </Card>
  )

  const historyCard = (
    <Card
      flush
      title={
        <>
          Historial <Badge tone="muted">{fmtInt(history.length)}</Badge>
        </>
      }
      sub="Operaciones cerradas: motivo de salida, R y P&L · resultado según el filtro"
    >
      {history.length > 0 ? (
        <HistoryTable history={history} onOpenChart={openChart} />
      ) : (
        <Empty title="Sin operaciones cerradas todavía">Cuando cierres operaciones verás aquí su resultado.</Empty>
      )}
    </Card>
  )

  const ordersCard =
    orders.length > 0 || recentOrders.length > 0 ? (
      <Card
        flush
        title={
          <>
            <Hourglass size={14} /> Órdenes pendientes <Badge tone={orders.length ? 'accent' : 'muted'}>{fmtInt(orders.length)}</Badge>
          </>
        }
        sub="Límite y stop simulados: al ejecutarse se consultan otra vez el filtro (si no responde, la orden se cancela) y los límites de riesgo"
        actions={
          orders.length > 1 ? (
            <Button size="sm" variant="ghost" onClick={() => setConfirmCancelAll(true)} disabled={cancellingId !== null}>
              Cancelar todas
            </Button>
          ) : undefined
        }
      >
        {orders.length === 0 && <div className="tiny muted pp-orders-empty">Sin órdenes pendientes.</div>}
        <PendingOrdersTable orders={orders} recent={recentOrders} cancellingId={cancellingId} onCancel={cancelOrder} onOpenChart={openOrderChart} />
      </Card>
    ) : null

  const closeAllButton =
    positions.length > 1 ? (
      <Button size="sm" variant="ghost" onClick={() => setConfirmAll(true)} disabled={closingId !== null || closingAll}>
        Cerrar todas
      </Button>
    ) : undefined
  const tiles = (
    <Block
      title={
        <span className="row gap-2">
          <Wallet size={16} /> Posiciones abiertas <Badge tone={positions.length ? 'accent' : 'muted'}>{fmtInt(positions.length)}</Badge>
          {toClose > 0 && (
            <Badge tone="neg" title="Posiciones en las que NEXUS recomienda cerrar ya">
              <OctagonAlert size={11} /> {toClose} para cerrar
            </Badge>
          )}
        </span>
      }
      sub="Cada una con su camino del stop al objetivo y el consejo de NEXUS en vivo."
      actions={closeAllButton}
    >
      {positions.length > 0 ? (
        <div className="dk-tiles">
          {positions.map((p) => (
            <TradeTile key={p.id} p={p} closingId={closingId} tag={`${ORIGIN_TAG[p.origin] ?? 'Manual'} · ${p.tf}`} onClose={(x) => void closePosition(x)} onShow={openChart} />
          ))}
        </div>
      ) : (
        <div className="dk-empty">
          <b>Sin posiciones abiertas</b>
          Abre una orden demo en «Operar», con el filtro como guía, o activa el auto-trading.
          <div style={{ marginTop: 10 }}>
            <Button variant="primary" size="sm" icon={<CirclePlus size={14} />} onClick={() => setSection('operar')}>
              Abrir una operación
            </Button>
          </div>
        </div>
      )}
    </Block>
  )

  const sectionTabs: SectionTab<MyAccountSection>[] = [
    { id: 'resumen', label: 'Resumen', icon: <LayoutDashboard size={15} />, count: positions.length },
    { id: 'operar', label: 'Operar', icon: <CirclePlus size={15} /> },
    { id: 'detalle', label: 'Detalle', icon: <ListChecks size={15} />, count: orders.length },
  ]

  let body: ReactNode = null
  if (snap && shown === 'operar') {
    body = (
      <div className="dk-cols">
        <div className="dk-stack">
          <OrderCard onOpened={onOpened} />
        </div>
        <div className="dk-stack">
          <AutoTradeCard account={snap.account} onChanged={onAutoChanged} />
          <RiskPanel risk={risk} />
        </div>
      </div>
    )
  } else if (snap && shown === 'detalle') {
    body = (
      <div className="dk-stack">
        <PaperStats account={snap.account} />
        {positionsCard}
        {ordersCard}
        <RiskPanel risk={risk} />
        {historyCard}
      </div>
    )
  } else if (snap) {
    body = (
      <div className="dk-overview">
        <div className="dk-overview-main">
          {tiles}
          {ordersCard}
        </div>
        <div className="dk-overview-side">
          <Block
            title={
              <span className="row gap-2">
                <HistoryIcon size={16} /> Últimos movimientos
              </span>
            }
            sub="Toca uno para verlo en el gráfico"
          >
            <ActivityFeed items={activity} onShow={openChart} empty="Todavía no has abierto ninguna operación en esta cuenta." />
          </Block>
        </div>
      </div>
    )
  }

  // ------------------------------------------------------------------ render (inside PaperPage's .page container)
  return (
    <>
      <header className="dk-head">
        <div className="col gap-0" style={{ minWidth: 0 }}>
          <h1 className="pp-title">
            <Wallet size={20} /> Cuenta demo
          </h1>
          <span className="sub">
            Operaciones simuladas (papel){snap ? ` · cuenta ${snap.account.name || 'Principal'}` : ''}
            {loadedAt ? ` · actualizado ${fmtAgo(loadedAt)}` : ''}
          </span>
        </div>
        <div className="dk-head-actions">
          <Button variant="primary" icon={<CirclePlus size={14} />} onClick={() => setSection('operar')} disabled={!snap} title="Abrir una orden demo">
            Nueva orden
          </Button>
          <Button icon={<RefreshCw size={14} />} variant="ghost" onClick={() => void load()} loading={loading && !!snap} title="Recargar la cuenta">
            Actualizar
          </Button>
          <Button icon={<RotateCcw size={14} />} variant="ghost" onClick={() => setResetOpen(true)} disabled={!snap} title="Empezar de cero con un balance nuevo">
            Reiniciar cuenta
          </Button>
        </div>
      </header>

      {!snap ? (
        error ? (
          <ErrorBox error={error} onRetry={() => void load()} />
        ) : (
          <Loading label="Cargando cuenta demo…" />
        )
      ) : (
        <>
          {error && <ErrorBox error={`${error} · mostrando los últimos datos conocidos`} onRetry={() => void load()} />}
          <AccountHero account={snap.account} history={history} label="Valor de tu cuenta demo" />
          <SectionTabs label="Secciones de tu cuenta demo" tabs={sectionTabs} value={shown} onChange={setSection} />
          {body}
        </>
      )}

      <Disclaimer />

      <ResetModal
        open={resetOpen}
        currentInitial={snap?.account.initial_balance ?? null}
        openPositions={positions.length}
        onClose={() => setResetOpen(false)}
        onDone={onReset}
      />

      <Modal
        open={confirmCancelAll}
        onClose={() => (cancellingId !== null ? undefined : setConfirmCancelAll(false))}
        width={440}
        title="Cancelar todas las órdenes pendientes"
        footer={
          <>
            <Button onClick={() => setConfirmCancelAll(false)} disabled={cancellingId !== null}>
              Volver
            </Button>
            <Button variant="danger" onClick={() => void cancelAllOrders()} loading={cancellingId === -1}>
              Cancelar {fmtInt(orders.length)}
            </Button>
          </>
        }
      >
        <p className="small" style={{ margin: 0 }}>
          Se cancelarán las {fmtInt(orders.length)} órdenes límite / stop pendientes de la cuenta demo (órdenes simuladas). Las posiciones abiertas no
          cambian.
        </p>
      </Modal>

      <Modal
        open={confirmAll}
        onClose={() => (closingAll ? undefined : setConfirmAll(false))}
        width={440}
        title="Cerrar todas las posiciones"
        footer={
          <>
            <Button onClick={() => setConfirmAll(false)} disabled={closingAll}>
              Cancelar
            </Button>
            <Button variant="danger" onClick={() => void closeAll()} loading={closingAll}>
              Cerrar {fmtInt(positions.length)}
            </Button>
          </>
        }
      >
        <p className="small" style={{ margin: 0 }}>
          {isOne(positions.length)
            ? 'Se cerrará a precio de mercado la posición abierta de la cuenta demo (operación simulada).'
            : `Se cerrarán a precio de mercado las ${fmtInt(positions.length)} posiciones abiertas de la cuenta demo (operación simulada).`}
        </p>
      </Modal>
    </>
  )
}
