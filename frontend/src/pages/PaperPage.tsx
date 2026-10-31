// Cuenta demo: simulated (paper) trading account 1. Stats, open positions with the live NEXUS advice, history, a new
// order ticket that shows the Trade Gate verdict before opening, auto-trading and account reset.
// Live updates arrive over the socket ('paper' topic); the account is also polled every 20 s as a fallback.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { OctagonAlert, RefreshCw, RotateCcw, Wallet } from 'lucide-react'
import { api, errorMessage } from '../api/client'
import type { PaperSnapshot, Position } from '../api/types'
import { socket } from '../api/ws'
import { fmtAgo, fmtInt, fmtR, splitSymbol } from '../lib/format'
import { isOne, plural } from '../lib/plural'
import { useApp } from '../store'
import { Badge, Button, Card, Disclaimer, Empty, ErrorBox, Loading, Modal } from '../components/ui'
import { asTimeframe, fmtMoney, useInterval } from '../components/markets/opsUtils'
import PaperStats from '../components/paper/PaperStats'
import PositionsTable from '../components/paper/PositionsTable'
import HistoryTable from '../components/paper/HistoryTable'
import OrderCard from '../components/paper/OrderCard'
import AutoTradeCard, { type AutoState } from '../components/paper/AutoTradeCard'
import ResetModal from '../components/paper/ResetModal'
import { adviceLevel, isAccount, normalizePositions, normalizeSnapshot } from '../components/paper/paperUtils'
import { useMediaQuery } from '../components/paper/useMediaQuery'
import '../components/paper/paper.css'

const POLL_MS = 20_000
const RELOAD_DEBOUNCE_MS = 600

export default function PaperPage() {
  const notify = useApp((s) => s.notify)
  const [snap, setSnapState] = useState<PaperSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loadedAt, setLoadedAt] = useState<number | null>(null)
  const [closingId, setClosingId] = useState<number | null>(null)
  const [confirmAll, setConfirmAll] = useState(false)
  const [closingAll, setClosingAll] = useState(false)
  const [resetOpen, setResetOpen] = useState(false)
  const [, setTick] = useState(0)

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
        const res = await api.paper.account()
        if (!alive.current || id !== seq.current) return
        const s = normalizeSnapshot(res)
        if (!s) throw new Error('Respuesta de la cuenta demo no válida')
        setSnap(s)
        setError(null)
        setLoadedAt(Date.now())
      } catch (err) {
        if (!alive.current || id !== seq.current) return
        setError(errorMessage(err))
      } finally {
        if (alive.current && id === seq.current) setLoading(false)
      }
    },
    [setSnap],
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
      setLoadedAt(Date.now())
      if (!prev || closedSome || prev.account.trades !== account.trades) scheduleReload()
    })
    return off
  }, [scheduleReload, setSnap])

  // ------------------------------------------------------------------ actions
  const openChart = useCallback((p: Position) => useApp.getState().openChart(p.symbol, asTimeframe(p.tf)), [])

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
  const wide = useMediaQuery('(min-width: 1700px)')

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

  const tickets = snap ? (
    <>
      <OrderCard onOpened={onOpened} />
      <AutoTradeCard account={snap.account} onChanged={onAutoChanged} />
    </>
  ) : null

  // ------------------------------------------------------------------ render
  return (
    <div className="page pp-page">
      <div className="page-header">
        <div className="col gap-0">
          <h1 className="pp-title row gap-2">
            <Wallet size={18} /> Cuenta demo
          </h1>
          <span className="sub">
            Operaciones simuladas (papel){snap ? ` · cuenta ${snap.account.name || 'Principal'}` : ''}
            {loadedAt ? ` · actualizado ${fmtAgo(loadedAt)}` : ''}
          </span>
        </div>
        <span className="header-spacer" />
        <Button icon={<RefreshCw size={14} />} onClick={() => void load()} loading={loading && !!snap} title="Recargar la cuenta">
          Actualizar
        </Button>
        <Button icon={<RotateCcw size={14} />} variant="ghost" onClick={() => setResetOpen(true)} disabled={!snap} title="Empezar de cero con un balance nuevo">
          Reiniciar cuenta
        </Button>
      </div>

      {!snap ? (
        error ? (
          <ErrorBox error={error} onRetry={() => void load()} />
        ) : (
          <Loading label="Cargando cuenta demo…" />
        )
      ) : (
        <>
          {error && <ErrorBox error={`${error} · mostrando los últimos datos conocidos`} onRetry={() => void load()} />}
          <PaperStats account={snap.account} />

          {wide ? (
            <div className="pp-layout">
              <div className="pp-main">
                {positionsCard}
                {historyCard}
              </div>
              <div className="pp-side">{tickets}</div>
            </div>
          ) : (
            <>
              {positionsCard}
              <div className="pp-tickets">{tickets}</div>
              {historyCard}
            </>
          )}
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
    </div>
  )
}
