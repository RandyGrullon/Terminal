// "Espejo en Alpaca (práctica)": NEXUS automática repeated in the user's Alpaca PAPER account (backend app.broker):
// whether it is on and connected, the practice account, what it mirrored (or why not) and the price difference with
// the simulation (positive = the practice fill was worse). Never real money. Polls GET /api/broker/status every 30 s.
import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeftRight, RefreshCw, Settings as SettingsIcon } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { BrokerAccount, BrokerMirrorEntry, BrokerStats, BrokerStatus } from '../../api/types'
import { DIRECTION_LABEL, fmtAgo, fmtNum, fmtPct, fmtPrice } from '../../lib/format'
import { useApp } from '../../store'
import { Badge, Button, Card, Empty } from '../ui'
import { fmtMoney, isNum, useInterval } from '../markets/opsUtils'

const POLL_MS = 30_000
const SHOW = 8

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback)

function normalizeEntry(raw: unknown): BrokerMirrorEntry | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (typeof o.pos_id !== 'number' || typeof o.symbol !== 'string') return null
  return {
    pos_id: o.pos_id,
    symbol: o.symbol,
    alpaca_symbol: typeof o.alpaca_symbol === 'string' ? o.alpaca_symbol : null,
    side: o.side === 'short' ? 'short' : 'long',
    status: str(o.status, 'skipped'),
    status_label: str(o.status_label, str(o.status)),
    reason: typeof o.reason === 'string' && o.reason ? o.reason : null,
    sim_qty: num(o.sim_qty) ?? 0,
    held: num(o.held) ?? 0,
    sim_entry: num(o.sim_entry) ?? 0,
    fill_entry: num(o.fill_entry),
    sim_exit: num(o.sim_exit),
    fill_exit: num(o.fill_exit),
    slip_entry_bps: num(o.slip_entry_bps),
    slip_exit_bps: num(o.slip_exit_bps),
    sim_result_pct: num(o.sim_result_pct),
    mirror_result_pct: num(o.mirror_result_pct),
    opened_at: num(o.opened_at),
    closed_at: num(o.closed_at),
  }
}

function normalizeStats(raw: unknown): BrokerStats {
  const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const int = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? Math.max(0, Math.round(v)) : 0)
  return {
    entries: int(o.entries),
    mirrored: int(o.mirrored),
    skipped: int(o.skipped),
    rejected: int(o.rejected),
    closed: int(o.closed),
    slip_entry_bps: num(o.slip_entry_bps),
    slip_entry_n: int(o.slip_entry_n),
    slip_exit_bps: num(o.slip_exit_bps),
    slip_exit_n: int(o.slip_exit_n),
    result_diff_pct: num(o.result_diff_pct),
    result_n: int(o.result_n),
    text: str(o.text),
  }
}

/** Defensive parse of a BrokerStatus: null when it is not one. */
export function normalizeBroker(raw: unknown): BrokerStatus | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (typeof o.enabled !== 'boolean') return null
  const a = o.account && typeof o.account === 'object' ? (o.account as Record<string, unknown>) : null
  const account: BrokerAccount | null = a
    ? { equity: num(a.equity), cash: num(a.cash), buying_power: num(a.buying_power), status: str(a.status), currency: str(a.currency, 'USD') }
    : null
  return {
    enabled: o.enabled,
    provider: str(o.provider, 'alpaca'),
    paper: o.paper !== false,
    configured: o.configured === true,
    connected: o.connected === true,
    account,
    error: typeof o.error === 'string' && o.error ? o.error : null,
    last_sync_ms: num(o.last_sync_ms),
    mirror_stocks: o.mirror_stocks !== false,
    mirror_crypto: o.mirror_crypto !== false,
    entries: Array.isArray(o.entries) ? o.entries.map(normalizeEntry).filter((e): e is BrokerMirrorEntry => e !== null) : [],
    stats: normalizeStats(o.stats),
    note: str(o.note),
  }
}

/** "+3,2 pb" (positive = worse than the simulation). */
export function fmtBps(v: number | null): string {
  if (!isNum(v)) return '—'
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${fmtNum(Math.abs(v), 1)} pb`
}

const STATUS_TONE: Record<string, 'pos' | 'neg' | 'warn' | 'info' | 'muted'> = {
  working: 'info',
  open: 'pos',
  waiting: 'warn',
  closed: 'muted',
  skipped: 'muted',
  rejected: 'neg',
  error: 'neg',
}

export interface BrokerViewProps {
  status: BrokerStatus | null
  error: string | null
  syncing: boolean
  onSync: () => void
  onSettings: () => void
}

/** The card without its state (rendered by the tests). */
export function BrokerView({ status, error, syncing, onSync, onSettings }: BrokerViewProps) {
  const on = !!status?.enabled
  const badge = !status ? null : !on ? (
    <Badge tone="muted">Desactivado</Badge>
  ) : status.connected ? (
    <Badge tone="pos">Conectado</Badge>
  ) : (
    <Badge tone="warn">Sin conexión</Badge>
  )
  return (
    <Card
      className="ap-broker"
      title={
        <span className="row gap-2">
          <ArrowLeftRight size={15} /> Espejo en Alpaca (práctica) {badge}
        </span>
      }
      sub="Repite sus posiciones en tu cuenta de práctica: sin dinero real"
      actions={
        on ? (
          <Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} onClick={onSync} loading={syncing} title="Sincronizar ahora con NEXUS automática">
            Sincronizar
          </Button>
        ) : undefined
      }
    >
      {error && <div className="notice ap-brake small">{error}</div>}
      {!status ? null : !on ? (
        <div className="col gap-2">
          <p className="small muted" style={{ margin: 0 }}>
            Si tienes una cuenta de práctica (paper trading) de Alpaca, NEXUS automática puede repetir en ella sus posiciones de acciones y
            ETF de EE. UU. y de cripto en dólares. Así ves la diferencia entre el precio simulado y el de una orden con cotizaciones reales.
            Nunca usa dinero real.
          </p>
          <Button size="sm" icon={<SettingsIcon size={13} />} onClick={onSettings}>
            Configurarlo en Ajustes
          </Button>
        </div>
      ) : (
        <div className="col gap-2">
          {status.error && <div className="notice ap-brake small">{status.error}</div>}
          {status.account && (
            <div className="small">
              Capital de práctica <b>{fmtMoney(status.account.equity)}</b> · efectivo {fmtMoney(status.account.cash)}
              {status.last_sync_ms ? <span className="muted"> · sincronizado {fmtAgo(status.last_sync_ms)}</span> : null}
            </div>
          )}
          <div className="small">{status.stats.text}</div>
          {status.entries.length === 0 ? (
            <Empty title="Sin posiciones replicadas todavía" />
          ) : (
            <div className="ap-broker-list">
              {status.entries.slice(0, SHOW).map((e) => (
                <div key={e.pos_id} className="ap-broker-row small">
                  <div className="row gap-2">
                    <b>{e.alpaca_symbol ?? e.symbol}</b>
                    <span className="muted">{DIRECTION_LABEL[e.side === 'short' ? 'short' : 'long']}</span>
                    <Badge tone={STATUS_TONE[e.status] ?? 'muted'}>{e.status_label}</Badge>
                  </div>
                  {e.fill_entry !== null ? (
                    <div className="muted">
                      Entrada NEXUS {fmtPrice(e.sim_entry)} · Alpaca {fmtPrice(e.fill_entry)} ({fmtBps(e.slip_entry_bps)})
                      {e.fill_exit !== null ? ` · salida ${fmtPrice(e.sim_exit)} / ${fmtPrice(e.fill_exit)} (${fmtBps(e.slip_exit_bps)})` : ''}
                      {e.mirror_result_pct !== null && e.sim_result_pct !== null
                        ? ` · resultado ${fmtPct(e.sim_result_pct)} simulado, ${fmtPct(e.mirror_result_pct)} en Alpaca`
                        : ''}
                    </div>
                  ) : null}
                  {e.reason && <div className="muted">{e.reason}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {status?.note && <div className="tiny muted" style={{ marginTop: 8 }}>{status.note}</div>}
    </Card>
  )
}

export default function BrokerCard() {
  const [status, setStatus] = useState<BrokerStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const s = normalizeBroker(await api.broker.status())
      if (!alive.current) return
      if (!s) throw new Error('Respuesta del espejo no válida')
      setStatus(s)
      setError(null)
    } catch (err) {
      if (alive.current) setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])
  useInterval(() => void load(), POLL_MS)

  const sync = useCallback(async () => {
    setSyncing(true)
    try {
      const s = normalizeBroker(await api.broker.sync())
      if (alive.current && s) {
        setStatus(s)
        setError(null)
      }
    } catch (err) {
      if (alive.current) setError(errorMessage(err))
    } finally {
      if (alive.current) setSyncing(false)
    }
  }, [])

  return <BrokerView status={status} error={error} syncing={syncing} onSync={() => void sync()} onSettings={() => useApp.getState().setPage('settings')} />
}
