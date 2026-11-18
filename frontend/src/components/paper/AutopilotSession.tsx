// "Horario de mercados" in NEXUS automática: which markets are open now and when each one opens or closes (crypto and
// gold 24/7, the New York exchange with the NYSE holidays, CME index futures), and its New York session mode: the
// aggressive mode on its own from the New York open to its close, its saved settings the rest of the time
// (autopilot.session_aggressive, app/sim/market_session.py). Times in the browser's zone.
import { Clock } from 'lucide-react'
import type { AutopilotMarket, AutopilotSession, AutopilotStatus } from '../../api/types'
import { Badge, Card, Toggle } from '../ui'
import { isNum } from '../markets/opsUtils'
import { fmtLeft } from './AutopilotGoal'

const isStr = (v: unknown): v is string => typeof v === 'string'
const MARKET_KEYS = ['crypto', 'ny', 'cme'] as const

/** Defensive parse of AutopilotStatus.session (an older server sends none: closed, with the setting given). */
export function normalizeSession(raw: unknown, enabled = false): AutopilotSession {
  const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const on = typeof o.enabled === 'boolean' ? o.enabled : enabled
  const open = o.open === true
  return {
    enabled: on,
    active: on && open && o.active !== false,
    open,
    opens_at: isNum(o.opens_at) ? o.opens_at : 0,
    closes_at: isNum(o.closes_at) ? o.closes_at : 0,
    early_close: o.early_close === true,
    known: o.known !== false,
    mode_text: isStr(o.mode_text) ? o.mode_text : '',
  }
}

/** Defensive parse of AutopilotStatus.markets. */
export function normalizeMarkets(raw: unknown): AutopilotMarket[] {
  if (!Array.isArray(raw)) return []
  const out: AutopilotMarket[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const o = item as Record<string, unknown>
    const key = MARKET_KEYS.find((k) => k === o.key)
    if (!key || !isStr(o.label) || typeof o.open !== 'boolean') continue
    out.push({ key, label: o.label, open: o.open, opens_at: isNum(o.opens_at) ? o.opens_at : null,
      closes_at: isNum(o.closes_at) ? o.closes_at : null, note: isStr(o.note) ? o.note : '' })
  }
  return out
}

/** "hoy 10:30" / "mañana 10:30" / "lun 30/11 10:30" in the browser's zone. */
export function fmtWhen(unixS: number, nowMs: number = Date.now()): string {
  const d = new Date(unixS * 1000)
  const now = new Date(nowMs)
  const clock = d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((day(d) - day(now)) / 86_400_000)
  if (diff === 0) return `hoy ${clock}`
  if (diff === 1) return `mañana ${clock}`
  const wd = d.toLocaleDateString('es-ES', { weekday: 'short' }).replace('.', '')
  return `${wd} ${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')} ${clock}`
}

/** "abierto · cierra hoy 17:00 (en 2 h 05 min)" / "cerrado · abre mañana 10:30" / "abierto siempre". */
export function marketWhen(m: AutopilotMarket, nowMs: number = Date.now()): string {
  if (m.open && m.closes_at !== null) return `cierra ${fmtWhen(m.closes_at, nowMs)} (en ${fmtLeft(Math.max(0, m.closes_at - nowMs / 1000))})`
  if (!m.open && m.opens_at !== null) return `abre ${fmtWhen(m.opens_at, nowMs)} (en ${fmtLeft(Math.max(0, m.opens_at - nowMs / 1000))})`
  return m.open ? 'no cierra' : 'cerrado'
}

export interface SessionViewProps {
  session: AutopilotSession
  markets: AutopilotMarket[]
  enabled: boolean // the account is on
  saving: boolean
  nowMs?: number
  onToggle: (on: boolean) => void
}

/** The card without its state (rendered by the tests). */
export function SessionView({ session, markets, enabled, saving, nowMs = Date.now(), onToggle }: SessionViewProps) {
  const state = !session.enabled
    ? 'Apagado: opera con sus ajustes normales a cualquier hora.'
    : session.active
      ? `Sesión abierta: modo agresivo hasta ${fmtWhen(session.closes_at, nowMs)}; luego vuelve sola a sus ajustes normales.`
      : `Sesión cerrada: ahora opera con sus ajustes normales; el modo agresivo empieza ${fmtWhen(session.opens_at, nowMs)}.`
  return (
    <Card
      className="ap-session"
      title={
        <span className="row gap-2">
          <Clock size={15} /> Horario de mercados
        </span>
      }
      sub="Qué mercado está abierto y cuándo abre o cierra (tu hora)"
    >
      <div className="ap-session-toggle">
        <Toggle checked={session.enabled} onChange={onToggle} disabled={saving}
          label={<b>Modo agresivo en la sesión de Nueva York</b>} />
        {session.active && <Badge tone="warn">Activo ahora</Badge>}
      </div>
      <p className="tiny" style={{ margin: '4px 0 0' }}>
        {state}
        {session.early_close ? ' Esa sesión cierra antes (13:00 de Nueva York).' : ''}
      </p>
      {session.enabled && session.mode_text && <p className="tiny muted" style={{ margin: '4px 0 0' }}>{session.mode_text}</p>}
      {session.enabled && !enabled && <div className="notice ap-brake small">La cuenta está pausada: actívala para que opere en la sesión.</div>}
      {!session.known && <p className="tiny muted" style={{ margin: '4px 0 0' }}>Los festivos de la NYSE de este año aún no están en NEXUS: solo salta los fines de semana.</p>}
      {markets.length > 0 && (
        <ul className="ap-markets">
          {markets.map((m) => (
            <li key={m.key}>
              <Badge tone={m.open ? 'pos' : 'muted'}>{m.open ? 'Abierto' : 'Cerrado'}</Badge>
              <span className="ap-market-name">{m.label}</span>
              <span className="tiny">{marketWhen(m, nowMs)}</span>
              <span className="tiny muted">{m.note}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="tiny muted" style={{ margin: '6px 0 0' }}>
        Acciones y futuros de la CME: NEXUS aún no los opera (solo hay cotizaciones con 10–15 min de retraso y sin historial). En
        la sesión opera cripto y oro, que suelen moverse más cuando abre Nueva York. Nada indica que operar en ese horario dé
        ventaja: simulado, sin dinero real.
      </p>
    </Card>
  )
}

/** The card with the page's save (the setting autopilot.session_aggressive). */
export default function AutopilotSessionCard({ status, saving, onToggle }: { status: AutopilotStatus; saving: boolean; onToggle: (on: boolean) => void }) {
  // an older server (or a partial status) may send neither: closed, no markets
  const session = status.session ?? normalizeSession(undefined, status.session_aggressive === true)
  return <SessionView session={session} markets={status.markets ?? []} enabled={status.enabled} saving={saving} onToggle={onToggle} />
}
