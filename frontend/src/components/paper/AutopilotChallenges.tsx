// "Cuentas de reto" in NEXUS automática: extra simulated accounts, each with its own balance, target, optional deadline
// and loss brake, traded by NEXUS's brain (account 3's candidates and signals) in the aggressive mode and sized on its
// own equity (app/sim/autopilot_challenges.py). The server ends each one by itself at its target, its deadline or its
// brake, closing its trades. Here: create one, follow each (progress, open trades, last trades, its decisions), end it
// or delete it with its trades.
import { useState } from 'react'
import { Plus, Target, Trash2 } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { AutopilotBrake, AutopilotChallenge, AutopilotLogEntry, AutopilotSnapshot, Position } from '../../api/types'
import { fmtDateTime, fmtR } from '../../lib/format'
import { plural } from '../../lib/plural'
import { Badge, Button, Card, Field, Meter, NumberInput } from '../ui'
import { fmtMoney, isNum } from '../markets/opsUtils'
import { normalizeGoalResult, fmtLeft } from './AutopilotGoal'
import { isAccount, marketOf, normalizePositions } from './paperUtils'
import TradeDetailModal, { detailProps } from './TradeDetailModal'

const isStr = (v: unknown): v is string => typeof v === 'string'
const STATUSES = ['running', 'reached', 'time', 'brake', 'cancelled'] as const

export const STATUS_LABEL: Record<AutopilotChallenge['status'], { label: string; tone: 'info' | 'pos' | 'warn' | 'neg' | 'muted' }> = {
  running: { label: 'En marcha', tone: 'info' },
  reached: { label: 'Meta cumplida', tone: 'pos' },
  time: { label: 'Se acabó el tiempo', tone: 'warn' },
  brake: { label: 'Freno de pérdidas', tone: 'neg' },
  cancelled: { label: 'Terminado a mano', tone: 'muted' },
}

/** Defensive parse of AutopilotSnapshot.challenges (the log and brake parsers come from the page). */
export function normalizeChallenges(
  raw: unknown,
  normalizeLog: (raw: unknown) => AutopilotLogEntry[],
  normalizeBrake: (raw: unknown) => AutopilotBrake | null,
): AutopilotChallenge[] {
  if (!Array.isArray(raw)) return []
  const out: AutopilotChallenge[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const o = item as Record<string, unknown>
    const status = STATUSES.find((s) => s === o.status)
    if (!isNum(o.id) || !isStr(o.name) || !status || !isNum(o.initial) || !isNum(o.target) || !isAccount(o.account)) continue
    out.push({
      id: o.id,
      name: o.name,
      status,
      initial: o.initial,
      target: o.target,
      until: isNum(o.until) ? o.until : null,
      brake_pct: isNum(o.brake_pct) ? o.brake_pct : 50,
      aggressive: o.aggressive !== false,
      started_at: isNum(o.started_at) ? o.started_at : 0,
      ended_at: isNum(o.ended_at) ? o.ended_at : null,
      account: { ...o.account, auto_symbols: Array.isArray(o.account.auto_symbols) ? o.account.auto_symbols : [] },
      positions: normalizePositions(o.positions),
      history: normalizePositions(o.history),
      log: normalizeLog(o.log),
      brake: normalizeBrake(o.brake) ?? { limit_pct: isNum(o.brake_pct) ? o.brake_pct : 50, drawdown_pct: 0, active: false },
      progress_pct: Math.min(100, Math.max(0, isNum(o.progress_pct) ? o.progress_pct : 0)),
      remaining: Math.max(0, isNum(o.remaining) ? o.remaining : 0),
      seconds_left: isNum(o.seconds_left) ? Math.max(0, o.seconds_left) : null,
      result: normalizeGoalResult(o.result),
    })
  }
  return out
}

/** "0,05 lotes" / "1 lote" / "3 contratos" / "0,5 unidades": the size of an open trade. */
export function sizeText(p: Position): string {
  if (p.lot && isNum(p.lot.lots)) {
    const n = p.lot.lots
    return `${n.toLocaleString('es-ES', { maximumFractionDigits: 2 })} ${n === 1 ? 'lote' : 'lotes'}`
  }
  const contracts = p.instrument?.contracts
  if (isNum(contracts)) return plural(contracts, 'contrato', 'contratos')
  return `${p.qty_open.toLocaleString('es-ES', { maximumFractionDigits: 4 })} unidades`
}

/** «objetivo 3.683,37 (1,6R)» / «sin objetivo: …» of an open trade (its plan). */
export function planText(p: Position): string {
  const plan = p.plan
  if (!plan) return '—'
  if (plan.target !== null) {
    const r = isNum(plan.target_r) ? ` (${plan.target_r.toLocaleString('es-ES', { maximumFractionDigits: 1 })}R)` : ''
    return `objetivo ${plan.target.toLocaleString('es-ES', { maximumFractionDigits: 2 })}${r}`
  }
  return `sin objetivo: ${plan.no_target ?? 'sale con su stop y sus reglas'}`
}

export interface ChallengeDraft {
  initial: number | null
  target: number | null
  brake: number | null
  until: string // datetime-local value or ''
  name: string
}

export interface ChallengesViewProps {
  challenges: AutopilotChallenge[]
  enabled: boolean // NEXUS automática is on (its challenge accounts trade its candidates)
  draft: ChallengeDraft | null
  busy: number | 'new' | null // what is being saved
  error: string | null
  nowMs?: number
  onDraft: (d: ChallengeDraft | null) => void
  onCreate: () => void
  onStop: (c: AutopilotChallenge) => void
  onDelete: (c: AutopilotChallenge) => void
  onDetail?: (p: Position) => void
}

const EMPTY: ChallengeDraft = { initial: null, target: null, brake: 50, until: '', name: '' }

function ChallengeRow({ c, busy, onStop, onDelete, onDetail }: { c: AutopilotChallenge; busy: boolean; onStop: () => void; onDelete: () => void; onDetail?: (p: Position) => void }) {
  const st = STATUS_LABEL[c.status]
  const a = c.account
  const pnl = a.equity - c.initial
  const running = c.status === 'running'
  return (
    <li className="ap-ch">
      <div className="ap-ch-head">
        <b className="ap-ch-name">{c.name}</b>
        <Badge tone={st.tone}>{st.label}</Badge>
        {running && c.brake.active && <Badge tone="neg">Freno activo</Badge>}
        <span className="ap-ch-actions">
          {running && (
            <Button size="sm" variant="ghost" onClick={onStop} disabled={busy} title="Termina el reto ahora y cierra sus operaciones">
              Terminar
            </Button>
          )}
          <Button size="sm" variant="ghost" icon={<Trash2 size={13} />} onClick={onDelete} disabled={busy} title="Borra la cuenta y sus operaciones">
            Borrar
          </Button>
        </span>
      </div>
      <Meter value={c.progress_pct / 100} tone={pnl >= 0 ? 'pos' : 'neg'} height={7}
        title={`${c.progress_pct.toLocaleString('es-ES', { maximumFractionDigits: 1 })} % del camino`} />
      <div className="ap-ch-figures tiny">
        <span>
          Empezó con <b className="mono">{fmtMoney(c.initial)}</b>
        </span>
        <span>
          Ahora <b className="mono">{fmtMoney(a.equity)}</b> ({fmtMoney(pnl, true)})
        </span>
        <span>
          Meta <b className="mono">{fmtMoney(c.target)}</b>
        </span>
        {running && (
          <span>
            Faltan <b className="mono">{fmtMoney(c.remaining)}</b>
          </span>
        )}
        <span>{a.trades ? `${a.wins} de ${a.trades} cerradas ganadoras` : 'sin operaciones cerradas'}</span>
        <span>{plural(c.positions.length, 'abierta', 'abiertas')}</span>
        {c.seconds_left !== null && <span>Quedan {fmtLeft(c.seconds_left)}</span>}
        <span className="muted">Freno al {c.brake_pct.toLocaleString('es-ES')} %</span>
      </div>
      {c.result && <p className="tiny" style={{ margin: 0 }}>{c.result.text}</p>}
      <details className="ap-ch-more">
        <summary className="tiny">Ver operaciones y decisiones</summary>
        {c.positions.length > 0 && (
          <table className="ap-ch-table tiny">
            <tbody>
              {c.positions.map((p) => {
                const m = marketOf(p)
                return (
                  <tr key={p.id} className={onDetail ? 'pp-row-click' : undefined} {...detailProps(p, onDetail)} title={p.plan ? `Por qué: ${p.plan.why}` : undefined}>
                    <td>{m.ticker}</td>
                    <td>{m.side}</td>
                    <td>{sizeText(p)}</td>
                    <td>{planText(p)}</td>
                    <td className={`mono ${p.pnl >= 0 ? 'pos' : 'neg'}`}>{fmtMoney(p.pnl, true)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        {c.history.length > 0 && (
          <>
            <div className="tiny muted" style={{ marginTop: 6 }}>Últimas cerradas</div>
            <table className="ap-ch-table tiny">
              <tbody>
                {c.history.map((p) => (
                  <tr key={p.id} className={onDetail ? 'pp-row-click' : undefined} {...detailProps(p, onDetail)}>
                    <td>{marketOf(p).ticker}</td>
                    <td>{p.exit_label ?? '—'}</td>
                    <td className="mono">{fmtR(p.r_multiple)}</td>
                    <td className={`mono ${p.pnl >= 0 ? 'pos' : 'neg'}`}>{fmtMoney(p.pnl, true)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        {c.log.length > 0 && (
          <ul className="ap-ch-log tiny">
            {c.log.map((e, i) => (
              <li key={`${e.ts}-${i}`}>
                <span className="muted">{fmtDateTime(e.ts)}</span> {e.symbol !== '—' ? <b>{e.symbol.split(':').pop()}</b> : null} {e.text}
              </li>
            ))}
          </ul>
        )}
        {c.positions.length === 0 && c.history.length === 0 && c.log.length === 0 && (
          <div className="tiny muted">Todavía nada: decide al cierre de cada vela.</div>
        )}
      </details>
    </li>
  )
}

/** The card without its state (rendered by the tests). */
export function ChallengesView({ challenges, enabled, draft, busy, error, onDraft, onCreate, onStop, onDelete, onDetail }: ChallengesViewProps) {
  const running = challenges.filter((c) => c.status === 'running').length
  const valid = !!draft && isNum(draft.initial) && isNum(draft.target) && draft.initial > 0 && draft.target > draft.initial
  return (
    <Card
      className="ap-challenges"
      title={
        <span className="row gap-2">
          <Target size={15} /> Cuentas de reto
        </span>
      }
      sub={challenges.length ? `${plural(running, 'en marcha', 'en marcha')} · cada una con su saldo, su meta y su freno` : 'Varias cuentas, cada una con su saldo y su meta'}
      actions={
        draft ? null : (
          <Button size="sm" variant="primary" icon={<Plus size={13} />} onClick={() => onDraft({ ...EMPTY })}>
            Nueva cuenta
          </Button>
        )
      }
    >
      {error && <div className="notice ap-brake small" role="alert">{error}</div>}
      {draft && (
        <div className="ap-goal-form" style={{ marginBottom: 10 }}>
          <Field label="Saldo inicial ($)">
            <NumberInput value={draft.initial} min={1} step={10} onChange={(v) => onDraft({ ...draft, initial: v })} />
          </Field>
          <Field label="Meta ($)" hint="Mayor que el saldo inicial">
            <NumberInput value={draft.target} min={1} step={10} onChange={(v) => onDraft({ ...draft, target: v })} />
          </Field>
          <Field label="Freno de pérdidas (%)" hint="Deja de abrir si cae este % (5–95)">
            <NumberInput value={draft.brake} min={5} max={95} step={5} onChange={(v) => onDraft({ ...draft, brake: v })} />
          </Field>
          <Field label="Hora límite" hint="Opcional; vacía = sin hora límite">
            <input className="input mono" type="datetime-local" value={draft.until} onChange={(e) => onDraft({ ...draft, until: e.target.value })} />
          </Field>
          <Field label="Nombre" hint="Opcional">
            <input className="input" maxLength={40} value={draft.name} placeholder="Reto 10 $ → 100 $" onChange={(e) => onDraft({ ...draft, name: e.target.value })} />
          </Field>
          <div className="row gap-2" style={{ alignSelf: 'end' }}>
            <Button variant="primary" onClick={onCreate} loading={busy === 'new'} disabled={!valid}>
              Crear
            </Button>
            <Button variant="ghost" onClick={() => onDraft(null)} disabled={busy === 'new'}>
              Cancelar
            </Button>
          </div>
        </div>
      )}
      {!enabled && challenges.some((c) => c.status === 'running') && (
        <div className="notice ap-brake small">NEXUS automática está pausada: sus cuentas de reto tampoco operan hasta que la actives.</div>
      )}
      {challenges.length === 0 && !draft && (
        <p className="tiny muted" style={{ margin: 0 }}>
          Crea cuentas con su propio saldo y su meta (por ejemplo 10 $ → 100 $). NEXUS las opera con sus mismas señales en modo agresivo
          y las cierra sola al llegar a la meta, a la hora límite o al freno.
        </p>
      )}
      {challenges.length > 0 && (
        <ul className="ap-ch-list">
          {challenges.map((c) => (
            <ChallengeRow key={c.id} c={c} busy={busy === c.id} onStop={() => onStop(c)} onDelete={() => onDelete(c)} onDetail={onDetail} />
          ))}
        </ul>
      )}
      <p className="tiny muted" style={{ margin: '8px 0 0' }}>
        Cada cuenta usa el mismo cerebro (los candidatos y señales de NEXUS automática) con velas de 5m, 15m y 1h, un 4 % de riesgo por
        operación en lotes y 20x, y su límite de pérdida diaria igual a su freno. El tamaño sale de su propio capital: una cuenta pequeña
        solo cabe en monedas baratas. Un reto no crea ventaja (la validación no la encontró). Simulado, sin dinero real.
      </p>
    </Card>
  )
}

/** The card with its state: creates, ends and deletes challenge accounts and hands the server's snapshot to the page. */
export default function AutopilotChallengesCard({ challenges, enabled, onSnapshot }: { challenges: AutopilotChallenge[]; enabled: boolean; onSnapshot: (s: AutopilotSnapshot) => void }) {
  const [draft, setDraft] = useState<ChallengeDraft | null>(null)
  const [busy, setBusy] = useState<number | 'new' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [detailId, setDetailId] = useState<number | null>(null)
  const detail = detailId === null ? null : ((challenges ?? []).flatMap((c) => [...c.positions, ...c.history]).find((x) => x.id === detailId) ?? null)

  const run = async (what: number | 'new', call: () => Promise<AutopilotSnapshot>) => {
    setBusy(what)
    setError(null)
    try {
      onSnapshot(await call())
      if (what === 'new') setDraft(null)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const create = () => {
    if (!draft || !isNum(draft.initial) || !isNum(draft.target)) return
    const until = draft.until ? Math.floor(new Date(draft.until).getTime() / 1000) : null
    if (draft.until && !isNum(until)) {
      setError('La hora límite no es válida')
      return
    }
    void run('new', () =>
      api.paper.autopilot.createChallenge({
        initial_balance: draft.initial as number,
        target_equity: draft.target as number,
        brake_pct: isNum(draft.brake) ? draft.brake : null,
        until,
        name: draft.name.trim() || null,
      }),
    )
  }

  return (
    <>
    <ChallengesView
      challenges={challenges ?? []}
      onDetail={(p) => setDetailId(p.id)}
      enabled={enabled}
      draft={draft}
      busy={busy}
      error={error}
      onDraft={(d) => {
        setDraft(d)
        setError(null)
      }}
      onCreate={create}
      onStop={(c) => {
        if (window.confirm(`¿Terminar «${c.name}» ahora? Se cierran sus operaciones a precio de mercado (simulado).`))
          void run(c.id, () => api.paper.autopilot.stopChallenge(c.id, true))
      }}
      onDelete={(c) => {
        if (window.confirm(`¿Borrar «${c.name}» y todas sus operaciones? No se puede deshacer.`))
          void run(c.id, () => api.paper.autopilot.deleteChallenge(c.id))
      }}
    />
    <TradeDetailModal p={detail} onClose={() => setDetailId(null)} />
    </>
  )
}
