// "Reto" in NEXUS automática: a target equity by a deadline, optionally in the aggressive mode. The server itself
// watches the account and, at the target or the deadline, closes its trades (never its buy-and-hold portfolio) and goes
// back to its saved settings or pauses (app/sim/autopilot_goal.py), so nothing outside NEXUS has to stay open but its
// server. While it runs: how far it has come, what is left and the time left; afterwards: how the last one ended.
import { useState } from 'react'
import { Flag, Square, Trophy } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { AutopilotGoal, AutopilotGoalResult, AutopilotSnapshot, AutopilotStatus } from '../../api/types'
import { fmtDateTime } from '../../lib/format'
import { Badge, Button, Card, Field, Meter, NumberInput, Select, Toggle } from '../ui'
import { fmtMoney, isNum } from '../markets/opsUtils'

const isStr = (v: unknown): v is string => typeof v === 'string'
const num = (v: unknown, fallback: number): number => (isNum(v) ? v : fallback)

/** Defensive parse of AutopilotStatus.goal. */
export function normalizeGoal(raw: unknown): AutopilotGoal | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.target) || !isNum(o.start_equity) || !isNum(o.equity)) return null
  return {
    target: o.target,
    until: isNum(o.until) ? o.until : null,
    started_at: num(o.started_at, 0),
    start_equity: o.start_equity,
    aggressive: o.aggressive === true,
    then: o.then === 'pause' ? 'pause' : 'normal',
    equity: o.equity,
    pnl: num(o.pnl, o.equity - o.start_equity),
    remaining: Math.max(0, num(o.remaining, o.target - o.equity)),
    progress_pct: Math.min(100, Math.max(0, num(o.progress_pct, 0))),
    seconds_left: isNum(o.seconds_left) ? Math.max(0, o.seconds_left) : null,
    mode_text: isStr(o.mode_text) && o.mode_text ? o.mode_text : null,
    note: isStr(o.note) ? o.note : '',
  }
}

/** Defensive parse of AutopilotStatus.goal_last. */
export function normalizeGoalResult(raw: unknown): AutopilotGoalResult | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (o.outcome !== 'reached' && o.outcome !== 'time' && o.outcome !== 'cancelled' && o.outcome !== 'brake') return null
  if (!isNum(o.target) || !isNum(o.end_equity) || !isStr(o.text)) return null
  return {
    outcome: o.outcome,
    target: o.target,
    until: isNum(o.until) ? o.until : null,
    started_at: num(o.started_at, 0),
    ended_at: num(o.ended_at, 0),
    start_equity: num(o.start_equity, 0),
    end_equity: o.end_equity,
    pnl: num(o.pnl, 0),
    pnl_pct: num(o.pnl_pct, 0),
    trades: Math.max(0, Math.round(num(o.trades, 0))),
    wins: Math.max(0, Math.round(num(o.wins, 0))),
    losses: Math.max(0, Math.round(num(o.losses, 0))),
    closed: Math.max(0, Math.round(num(o.closed, 0))),
    aggressive: o.aggressive === true,
    late: o.late === true,
    text: o.text,
  }
}

/** "2 h 05 min" / "12 min" / "menos de 1 min". */
export function fmtLeft(seconds: number): string {
  if (seconds < 60) return 'menos de 1 min'
  const m = Math.floor(seconds / 60)
  const h = Math.floor(m / 60)
  return h > 0 ? `${h} h ${String(m % 60).padStart(2, '0')} min` : `${m} min`
}

/** "17:00" in the browser's zone. */
function fmtClock(unixS: number): string {
  return new Date(unixS * 1000).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
}

/** Unix seconds of the next "HH:MM" (today, or tomorrow when it already passed); null for an invalid value. */
export function deadlineFrom(hhmm: string, now: Date = new Date()): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm.trim())
  if (!m) return null
  const h = Number(m[1])
  const min = Number(m[2])
  if (h > 23 || min > 59) return null
  const d = new Date(now)
  d.setHours(h, min, 0, 0)
  if (d.getTime() <= now.getTime()) d.setDate(d.getDate() + 1)
  return Math.floor(d.getTime() / 1000)
}

const OUTCOME: Record<AutopilotGoalResult['outcome'], { label: string; tone: 'pos' | 'warn' | 'muted' }> = {
  reached: { label: 'Meta cumplida', tone: 'pos' },
  time: { label: 'Se acabó el tiempo', tone: 'warn' },
  cancelled: { label: 'Terminado a mano', tone: 'muted' },
  brake: { label: 'Freno de pérdidas', tone: 'warn' },
}

export interface GoalDraft {
  target: number | null
  time: string // "HH:MM" or '' (no deadline)
  aggressive: boolean
  then: 'normal' | 'pause'
}

export interface GoalViewProps {
  goal: AutopilotGoal | null
  last: AutopilotGoalResult | null
  enabled: boolean // the account is on (a challenge of a paused account opens nothing)
  draft: GoalDraft | null // the form while starting one (null: closed)
  busy: boolean
  error: string | null
  onDraft: (d: GoalDraft | null) => void
  onStart: () => void
  onStop: (close: boolean) => void
}

/** The card without its state (rendered by the tests). */
export function GoalView({ goal, last, enabled, draft, busy, error, onDraft, onStart, onStop }: GoalViewProps) {
  const outcome = last ? OUTCOME[last.outcome] : null
  return (
    <Card
      className="ap-goal"
      title={
        <span className="row gap-2">
          <Flag size={15} /> Reto
        </span>
      }
      sub={goal ? 'En marcha: NEXUS cierra sola al llegar a la meta o a la hora límite' : 'Una meta con hora límite: NEXUS cierra sola al llegar'}
      actions={
        goal ? (
          <>
            <Button size="sm" variant="ghost" icon={<Square size={13} />} onClick={() => onStop(false)} disabled={busy}
              title="Termina el reto ahora: vuelve a sus ajustes; las operaciones abiertas siguen con su stop y sus objetivos">
              Terminar
            </Button>
            <Button size="sm" variant="danger" onClick={() => onStop(true)} disabled={busy}
              title="Termina el reto y cierra ya sus operaciones (la cartera no se toca)">
              Terminar y cerrar
            </Button>
          </>
        ) : draft ? null : (
          <Button size="sm" variant="primary" icon={<Trophy size={13} />} onClick={() => onDraft({ target: null, time: '17:00', aggressive: false, then: 'normal' })}>
            Empezar un reto
          </Button>
        )
      }
    >
      {error && <div className="notice ap-brake small" role="alert">{error}</div>}
      {goal && (
        <div className="ap-goal-run">
          <div className="ap-goal-line">
            <span>
              Meta <b className="mono">{fmtMoney(goal.target)}</b>
              {goal.until !== null ? (
                <>
                  {' '}antes de las <b>{fmtClock(goal.until)}</b>
                </>
              ) : null}
            </span>
            {goal.aggressive && <Badge tone="warn">Modo agresivo</Badge>}
          </div>
          <Meter value={goal.progress_pct / 100} tone={goal.pnl >= 0 ? 'pos' : 'neg'} height={8}
            title={`${goal.progress_pct.toLocaleString('es-ES', { maximumFractionDigits: 1 })} % del camino`} />
          <div className="ap-goal-figures tiny">
            <span>
              Empezó con <b className="mono">{fmtMoney(goal.start_equity)}</b>
            </span>
            <span>
              Ahora <b className="mono">{fmtMoney(goal.equity)}</b> ({fmtMoney(goal.pnl, true)})
            </span>
            <span>
              Faltan <b className="mono">{fmtMoney(goal.remaining)}</b>
            </span>
            {goal.seconds_left !== null && (
              <span>
                Quedan <b>{fmtLeft(goal.seconds_left)}</b>
              </span>
            )}
          </div>
          <p className="tiny muted" style={{ margin: 0 }}>
            Al llegar a la meta{goal.until !== null ? ' o a la hora límite' : ''} cierra sus operaciones (la cartera se queda) y{' '}
            {goal.then === 'pause' ? 'se pausa' : 'vuelve a sus ajustes normales'}. Lo hace el servidor de NEXUS: basta con que siga
            encendido.
          </p>
          {goal.mode_text && <p className="tiny" style={{ margin: 0 }}>{goal.mode_text}</p>}
          {!enabled && <div className="notice ap-brake small">La cuenta está pausada: actívala para que opere durante el reto.</div>}
          {goal.note && <p className="tiny muted" style={{ margin: 0 }}>{goal.note}</p>}
        </div>
      )}
      {!goal && draft && (
        <div className="ap-goal-form">
          <Field label="Meta (valor de la cuenta, $)" hint="Mayor que su valor actual">
            <NumberInput value={draft.target} min={0} step={100} onChange={(v) => onDraft({ ...draft, target: v })} />
          </Field>
          <Field label="Hora límite" hint="Hoy (o mañana si ya pasó); vacía = sin hora límite">
            <input className="input mono" type="time" value={draft.time} onChange={(e) => onDraft({ ...draft, time: e.target.value })} />
          </Field>
          <Field label="Al terminar">
            <Select
              value={draft.then}
              onChange={(v) => onDraft({ ...draft, then: v })}
              options={[
                { value: 'normal', label: 'Seguir con sus ajustes' },
                { value: 'pause', label: 'Pausar la cuenta' },
              ]}
            />
          </Field>
          <Field label="Modo agresivo" hint="Velas rápidas, 4 % por operación, lotes, 20x; tus ajustes no cambian">
            <Toggle checked={draft.aggressive} onChange={(v) => onDraft({ ...draft, aggressive: v })} label={draft.aggressive ? 'Sí' : 'No'} />
          </Field>
          <div className="row gap-2" style={{ alignSelf: 'end' }}>
            <Button variant="primary" onClick={onStart} loading={busy} disabled={!isNum(draft.target) || draft.target <= 0}>
              Empezar
            </Button>
            <Button variant="ghost" onClick={() => onDraft(null)} disabled={busy}>
              Cancelar
            </Button>
          </div>
          <p className="tiny muted" style={{ margin: 0, gridColumn: '1 / -1' }}>
            Un reto no crea ventaja: la validación no la encontró, y más riesgo solo hace que llegue antes a la meta o al freno de
            pérdidas. Simulado, sin dinero real.
          </p>
        </div>
      )}
      {!goal && !draft && !last && (
        <p className="tiny muted" style={{ margin: 0 }}>
          Ponle una meta y una hora: al llegar a cualquiera de las dos, NEXUS cierra sola sus operaciones y vuelve a sus ajustes.
        </p>
      )}
      {last && outcome && (
        <div className="ap-goal-last">
          <Badge tone={outcome.tone}>{outcome.label}</Badge>
          <span className="tiny">{last.text}</span>
          {last.ended_at > 0 && <span className="tiny muted">{fmtDateTime(last.ended_at * 1000)}</span>}
        </div>
      )}
    </Card>
  )
}

/** The card with its state: starts and ends a challenge and hands the server's snapshot to the page. */
export default function AutopilotGoalCard({ status, onSnapshot }: { status: AutopilotStatus; onSnapshot: (s: AutopilotSnapshot) => void }) {
  const [draft, setDraft] = useState<GoalDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (call: () => Promise<AutopilotSnapshot>) => {
    setBusy(true)
    setError(null)
    try {
      onSnapshot(await call())
      setDraft(null)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const start = () => {
    if (!draft || !isNum(draft.target)) return
    const until = draft.time ? deadlineFrom(draft.time) : null
    if (draft.time && until === null) {
      setError('La hora límite no es válida')
      return
    }
    void run(() => api.paper.autopilot.startGoal({ target_equity: draft.target as number, until, aggressive: draft.aggressive, then: draft.then }))
  }

  const stop = (close: boolean) => {
    if (close && !window.confirm('¿Terminar el reto y cerrar ya sus operaciones a precio de mercado? Su cartera no se toca.')) return
    void run(() => api.paper.autopilot.stopGoal(close))
  }

  return (
    <GoalView
      goal={status.goal}
      last={status.goal_last}
      enabled={status.enabled}
      draft={draft}
      busy={busy}
      error={error}
      onDraft={(d) => {
        setDraft(d)
        setError(null)
      }}
      onStart={start}
      onStop={stop}
    />
  )
}
