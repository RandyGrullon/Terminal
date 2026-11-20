// NEXUS automática's accounts side by side, right under the 10k account's hero (Pro mode): the 10k account (blue), the
// 1k account (yellow, the small-account mindset) and the vault (green, the realized excess over each account's initial
// balance, set aside). Also the defensive parsers of AutopilotSnapshot.twin / .vault, shared with the simple mode, and
// the accounts' colors (ACCOUNT_TONE), used by both modes so a trade always wears its account's color.
import { useState } from 'react'
import { PiggyBank, Plus, Wallet } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type {
  AutopilotBrake,
  AutopilotLogEntry,
  AutopilotMainGoal,
  AutopilotSnapshot,
  AutopilotTwin,
  AutopilotVault,
  Position,
} from '../../api/types'
import { fmtAgo, fmtPct } from '../../lib/format'
import { useApp } from '../../store'
import { Badge, Button } from '../ui'
import { fmtMoney, isNum } from '../markets/opsUtils'
import { TradeTile } from './DemoKit'
import { isAccount, normalizePositions } from './paperUtils'
import './nexus-accounts.css'

export type AccountTone = '10k' | '1k' | '500' | '100' | 'vault' | 'other'

/** The small accounts as a tone source: their ids and initial balances (or a single id, the 1k account). */
export type TwinsKey = number | null | undefined | ReadonlyArray<{ id: number; initial: number }>

/** The color of a small account by its size: 1k yellow, 500 orange, 100 pink. */
export function twinTone(initial: number): AccountTone {
  return initial >= 1000 ? '1k' : initial >= 300 ? '500' : '100'
}

/** The account's color key: 3 = the 10k account (blue), the small accounts by size, any other (challenges): neutral. */
export function toneOf(accountId: number, twins: TwinsKey): AccountTone {
  if (accountId === 3) return '10k'
  if (typeof twins === 'number') return accountId === twins ? '1k' : 'other'
  const t = twins?.find((x) => x.id === accountId)
  return t ? twinTone(t.initial) : 'other'
}

/** «Cuenta 1k» / «Cuenta 500» / «Cuenta 100»: a small account's short name from its initial balance. */
export function twinLabel(initial: number): string {
  return initial >= 1000 && initial % 1000 === 0 ? `Cuenta ${initial / 1000}k` : `Cuenta ${Math.round(initial)}`
}

/** «Cuenta 10k» / «Cuenta 1k» / «Cuenta 500»… for NEXUS's accounts, else the server's name (a challenge account). */
export function accountLabel(accountId: number, twins: TwinsKey, fallback: string): string {
  if (accountId === 3) return TONE_LABEL['10k']
  if (typeof twins === 'number') return accountId === twins ? TONE_LABEL['1k'] : fallback
  const t = twins?.find((x) => x.id === accountId)
  return t ? twinLabel(t.initial) : fallback
}

export const TONE_LABEL: Record<AccountTone, string> = {
  '10k': 'Cuenta 10k',
  '1k': 'Cuenta 1k',
  '500': 'Cuenta 500',
  '100': 'Cuenta 100',
  vault: 'Vault',
  other: 'Reto',
}

/** The running small accounts of a snapshot (twins, or the single twin of an older server). */
export function twinsOf(snap: { twins?: AutopilotTwin[] | null; twin?: AutopilotTwin | null } | null | undefined): AutopilotTwin[] {
  if (snap?.twins && snap.twins.length) return snap.twins
  return snap?.twin ? [snap.twin] : []
}

const isStr = (v: unknown): v is string => typeof v === 'string'

export function normalizeTwin(
  raw: unknown,
  normalizeLog: (raw: unknown) => AutopilotLogEntry[],
  normalizeBrake: (raw: unknown) => AutopilotBrake | null,
): AutopilotTwin | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.id) || !isStr(o.name) || !isNum(o.initial) || !isAccount(o.account)) return null
  return {
    id: o.id,
    name: o.name,
    status: isStr(o.status) ? o.status : 'running',
    kind: 'twin',
    initial: o.initial,
    brake_pct: isNum(o.brake_pct) ? o.brake_pct : 25,
    started_at: isNum(o.started_at) ? o.started_at : 0,
    account: { ...o.account, auto_symbols: Array.isArray(o.account.auto_symbols) ? o.account.auto_symbols : [] },
    positions: normalizePositions(o.positions),
    history: normalizePositions(o.history),
    log: normalizeLog(o.log),
    brake: normalizeBrake(o.brake) ?? { limit_pct: isNum(o.brake_pct) ? o.brake_pct : 25, drawdown_pct: 0, active: false },
    mindset: isStr(o.mindset) ? o.mindset : '',
    target: isNum(o.target) ? o.target : null,
    progress_pct: isNum(o.progress_pct) ? Math.min(100, Math.max(0, o.progress_pct)) : null,
    remaining: isNum(o.remaining) ? o.remaining : null,
    reached_at: isNum(o.reached_at) ? o.reached_at : null,
  }
}

export function normalizeMainGoal(raw: unknown): AutopilotMainGoal | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.target) || !isNum(o.total)) return null
  return {
    target: o.target,
    total: o.total,
    saved: isNum(o.saved) ? o.saved : 0,
    initial: isNum(o.initial) ? o.initial : 0,
    progress_pct: isNum(o.progress_pct) ? Math.min(100, Math.max(0, o.progress_pct)) : 0,
    remaining: isNum(o.remaining) ? o.remaining : 0,
    reached: o.reached === true,
  }
}

export function normalizeVault(raw: unknown): AutopilotVault | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.total)) return null
  const rows = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is Record<string, unknown> => !!x && typeof x === 'object') : [])
  return {
    enabled: o.enabled === true,
    total: o.total,
    by_account: rows(o.by_account)
      .filter((r) => isNum(r.account_id) && isNum(r.amount))
      .map((r) => ({ account_id: r.account_id as number, name: isStr(r.name) ? r.name : `Cuenta ${r.account_id}`, amount: r.amount as number })),
    entries: rows(o.entries)
      .filter((r) => isNum(r.ts) && isNum(r.amount) && isNum(r.account_id))
      .map((r) => ({
        ts: r.ts as number,
        account_id: r.account_id as number,
        name: isStr(r.name) ? r.name : `Cuenta ${r.account_id}`,
        amount: r.amount as number,
        base: isNum(r.base) ? r.base : 0,
      })),
    note: isStr(o.note) ? o.note : '',
  }
}

/** What an account put in the vault. */
export function vaultOf(vault: AutopilotVault | null | undefined, accountId: number): number {
  return vault?.by_account.find((r) => r.account_id === accountId)?.amount ?? 0
}

const signTone = (v: number) => (v > 0.004 ? 'pos' : v < -0.004 ? 'neg' : '')

interface MiniProps {
  tone: AccountTone
  title: string
  equity: number
  initial: number
  unrealized: number
  wins: number
  trades: number
  open: number
  saved: number
  note?: string | null
  goal?: { target: number; progress: number; label: string } | null
}

/** One account as a compact box: its value, its change from its base, what is in play and what it saved. */
export function AccountMini({ tone, title, equity, initial, unrealized, wins, trades, open, saved, note, goal }: MiniProps) {
  const change = equity - initial
  return (
    <div className={`nx-acct nx-${tone}`}>
      <div className="nx-acct-head">
        <span className="nx-dot" aria-hidden />
        <b>{title}</b>
        <span className="nx-base">base {fmtMoney(initial)}</span>
      </div>
      <div className="nx-acct-value mono">{fmtMoney(equity)}</div>
      <div className={`nx-acct-change mono ${signTone(change)}`}>
        {fmtMoney(change, true)} · {fmtPct(initial ? (change / initial) * 100 : 0)}
      </div>
      <div className="nx-acct-figs">
        <span>
          En juego <b className={`mono ${signTone(unrealized)}`}>{fmtMoney(unrealized, true)}</b>
        </span>
        <span>
          Abiertas <b className="mono">{open}</b>
        </span>
        <span>
          Ganadoras <b className="mono">{wins} de {trades}</b>
        </span>
        <span>
          Al vault <b className="mono pos">{fmtMoney(saved)}</b>
        </span>
      </div>
      {goal && (
        <div className="nx-goal">
          <div className="nx-goal-bar">
            <span style={{ width: `${Math.max(1, Math.min(100, goal.progress))}%` }} />
          </div>
          <span>
            {goal.label} · <b>{goal.progress.toFixed(1).replace('.', ',')} %</b>
          </span>
        </div>
      )}
      {note && <p className="nx-acct-note">{note}</p>}
    </div>
  )
}

export function VaultMini({ vault, twinId, onToggle, busy }: { vault: AutopilotVault | null; twinId?: TwinsKey; onToggle?: (on: boolean) => void; busy?: boolean }) {
  const last = vault?.entries[0]
  return (
    <div className="nx-acct nx-vault">
      <div className="nx-acct-head">
        <PiggyBank size={15} aria-hidden />
        <b>Vault</b>
        <Badge tone={vault?.enabled ? 'pos' : 'muted'}>{vault?.enabled ? 'Guardando' : 'Apagado'}</Badge>
      </div>
      <div className="nx-acct-value mono">{fmtMoney(vault?.total ?? 0)}</div>
      <div className="nx-acct-change">guardado, fuera de las cuentas</div>
      <div className="nx-acct-figs">
        {(vault?.by_account ?? []).map((r) => (
          <span key={r.account_id}>
            De la {accountLabel(r.account_id, twinId, r.name).replace('Cuenta ', '')} <b className="mono pos">{fmtMoney(r.amount)}</b>
          </span>
        ))}
        {last && (
          <span>
            Último {fmtAgo(last.ts * 1000)} <b className="mono pos">+{fmtMoney(last.amount)}</b>
          </span>
        )}
      </div>
      {onToggle && (
        <Button size="sm" variant={vault?.enabled ? 'ghost' : 'primary'} loading={busy} onClick={() => onToggle(!vault?.enabled)}>
          {vault?.enabled ? 'Dejar de guardar' : 'Empezar a guardar'}
        </Button>
      )}
      <p className="nx-acct-note">
        Cada cuenta vuelve a su base y lo ganado ya cerrado queda aquí: no opera y una pérdida después no le quita nada.
      </p>
    </div>
  )
}

/** Pro mode, under the 10k account's hero: the small accounts (1k, 500, 100…) and the vault, and their open trades. */
export default function NexusAccountsPanel({
  snap,
  onSnapshot,
  onDetail,
  onShow,
}: {
  snap: AutopilotSnapshot
  onSnapshot: (s: AutopilotSnapshot) => void
  onDetail: (p: Position) => void
  onShow: (p: Position) => void
}) {
  const notify = useApp((s) => s.notify)
  const [busy, setBusy] = useState<string | null>(null)
  const twins = twinsOf(snap)
  const has1k = twins.some((t) => t.initial >= 1000)
  const a = snap.account
  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key)
    try {
      await fn()
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setBusy(null)
    }
  }
  const createTwin = () =>
    run('twin', async () => {
      const s = await api.paper.autopilot.createTwin()
      onSnapshot(s)
      notify('Cuenta de 1k creada: opera con las mismas señales y su propia mentalidad', 'success')
    })
  const toggleVault = (on: boolean) =>
    run('vault', async () => {
      await api.settings.update({ 'autopilot.vault': on })
      onSnapshot(await api.paper.autopilot.get())
      notify(on ? 'El vault guardará la ganancia cerrada por encima de cada base' : 'El vault deja de guardar (lo guardado se queda)', 'info')
    })
  const closeTwinTrade = (p: Position) =>
    run(`close-${p.id}`, async () => {
      onSnapshot(await api.paper.autopilot.close({ position_id: p.id, account_id: p.account_id }))
    })
  const closingId = busy?.startsWith('close-') ? Number(busy.slice(6)) : null
  return (
    <div className="nx-panel">
      <div className="nx-accts">
        <AccountMini
          tone="10k"
          title="Cuenta 10k"
          equity={a.equity}
          initial={a.initial_balance}
          unrealized={a.unrealized_pnl}
          wins={a.wins}
          trades={a.trades}
          open={snap.positions.filter((p) => !p.hold).length}
          saved={vaultOf(snap.vault, a.id)}
          goal={
            snap.main_goal
              ? { target: snap.main_goal.target, progress: snap.main_goal.progress_pct, label: `Reto ${fmtMoney(snap.main_goal.target)} (cuenta + vault: ${fmtMoney(snap.main_goal.total)})` }
              : null
          }
        />
        {twins.map((twin) => (
          <AccountMini
            key={twin.id}
            tone={twinTone(twin.initial)}
            title={twinLabel(twin.initial)}
            equity={twin.account.equity}
            initial={twin.initial}
            unrealized={twin.account.unrealized_pnl}
            wins={twin.account.wins}
            trades={twin.account.trades}
            open={twin.positions.length}
            saved={vaultOf(snap.vault, twin.id)}
            goal={twin.target !== null ? { target: twin.target, progress: twin.progress_pct ?? 0, label: `Reto ${fmtMoney(twin.target)}${twin.reached_at ? ' · ¡alcanzado!' : ''}` } : null}
            note={twin.brake.active ? `Freno activo: no abre nuevas (−${twin.brake.drawdown_pct.toFixed(1)} %)` : twin.mindset}
          />
        ))}
        {!has1k && (
          <div className="nx-acct nx-1k nx-empty">
            <div className="nx-acct-head">
              <Wallet size={15} aria-hidden />
              <b>Cuenta 1k</b>
            </div>
            <p className="nx-acct-note">
              Una segunda cuenta de 1.000 $ con las mismas señales y una mentalidad de cuenta pequeña: como mucho 2 operaciones a la vez, 2 % por operación, sin
              modo agresivo.
            </p>
            <Button size="sm" icon={<Plus size={13} />} loading={busy === 'twin'} onClick={createTwin}>
              Crear la cuenta de 1k
            </Button>
          </div>
        )}
        <VaultMini vault={snap.vault} twinId={twins} onToggle={toggleVault} busy={busy === 'vault'} />
      </div>
      {twins
        .filter((twin) => twin.positions.length > 0)
        .map((twin) => {
          const t = twinTone(twin.initial)
          const label = twinLabel(twin.initial)
          return (
            <div className="nx-twin-trades" key={twin.id}>
              <h4>
                <span className={`nx-dot nx-dot-${t}`} aria-hidden /> Operaciones abiertas de la {label.toLowerCase()}
              </h4>
              <div className={`dk-tiles nx-tiles-${t}`}>
                {twin.positions.map((p) => (
                  <TradeTile key={p.id} p={p} closingId={closingId} tag={label} onClose={(x) => void closeTwinTrade(x)} onShow={onShow} onDetail={onDetail} />
                ))}
              </div>
            </div>
          )
        })}
    </div>
  )
}
