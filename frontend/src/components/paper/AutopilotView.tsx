// "NEXUS automática": NEXUS's own demo account (account 3), which trades on its own with NEXUS's closed-bar signals and
// the Trade Gate. The view shows what it is doing (watched symbols, latest scan, decision log), its open positions and
// history, its stats (winners as "x de n", never a bare percentage), how it trades (on/off, source, timeframe, limits)
// and a reset. Simulated only: never real money (BrokerCard: the optional mirror in an Alpaca PAPER account). Live updates come from the default WS 'paper' topic as
// {type: 'paper_autopilot'} (the user's {type: 'paper'} messages are account 1 and never feed this view); a GET every
// 30 s while the page is visible is the fallback.
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { BookOpen, Bot, CalendarDays, Info, PowerOff, Radar, RefreshCw, RotateCcw, Scale, ScrollText, SlidersHorizontal, TriangleAlert, Wallet, X } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type {
  AutopilotBrake,
  AutopilotComparison,
  AutopilotHealth,
  AutopilotJournal,
  AutopilotJournalBucket,
  AutopilotJournalVerdict,
  AutopilotLast,
  AutopilotLogEntry,
  AutopilotPausedContext,
  AutopilotScan,
  AutopilotSnapshot,
  AutopilotSource,
  AutopilotStatus,
  AutopilotStockInstrument,
  AutopilotUpdate,
  AutopilotWatch,
  AutopilotWeekly,
  AutopilotWhy,
  Position,
  ServerMessage,
  Timeframe,
} from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { socket } from '../../api/ws'
import { actionTone, DIRECTION_LABEL, fmtAgo, fmtDateTime, fmtInt, fmtNum, fmtR, splitSymbol, type Tone } from '../../lib/format'
import { isOne, plural } from '../../lib/plural'
import { useApp } from '../../store'
import { Badge, Button, Card, Disclaimer, Empty, ErrorBox, Field, Loading, Modal, NumberInput, Select, Spinner, Toggle } from '../ui'
import SymbolSearch from '../SymbolSearch'
import { asTimeframe, fmtMoney, isNum, useInterval } from '../markets/opsUtils'
import BrokerCard from './BrokerCard'
import PaperStats from './PaperStats'
import PositionsTable from './PositionsTable'
import HistoryTable from './HistoryTable'
import RiskPanel from './RiskPanel'
import { normalizeRisk } from './ordersUtils'
import { isAccount, marketOf, normalizePositions } from './paperUtils'
import { useMediaQuery } from './useMediaQuery'
import './autopilot.css'

/** NEXUS's own demo account (contract: account_id 3). */
export const AUTOPILOT_ACCOUNT_ID = 3
const POLL_MS = 30_000
const LOG_MAX = 30
const MAX_SYMBOLS = 30 // same limit as the server
const MIN_BALANCE = 100
const MAX_BALANCE = 100_000_000
const BALANCE_PRESETS = [1_000, 10_000, 50_000, 100_000]
const TF_OPTIONS = TIMEFRAMES.map((t) => ({ value: t, label: t }))
/** Origin label of NEXUS's positions in the shared position / history tables. */
const ORIGIN_LABELS: Record<string, string> = { autopilot: 'NEXUS' }

const SOURCE_OPTIONS: { value: AutopilotSource; label: string; title: string }[] = [
  { value: 'scanner', label: 'Escáner', title: 'Los mejores candidatos del último escaneo' },
  { value: 'list', label: 'Lista fija', title: 'Solo los símbolos de tu lista' },
  { value: 'both', label: 'Ambos', title: 'Tu lista y los mejores candidatos del escáner' },
]
/** How it trades stocks and funds (autopilot.stock_instrument). */
const INSTRUMENT_OPTIONS: { value: AutopilotStockInstrument; label: string }[] = [
  { value: 'auto', label: 'NEXUS elige: acciones, y un put para bajar' },
  { value: 'shares', label: 'Solo acciones (también en corto)' },
  { value: 'options', label: 'Opciones: calls y puts' },
]
const WHY_INFO: Record<string, { label: string; tone: Tone }> = {
  escáner: { label: 'Escáner', tone: 'accent' },
  lista: { label: 'Lista', tone: 'muted' },
  'posición abierta': { label: 'Posición abierta', tone: 'purple' },
}
/** Label and tone of each kind of decision in the log. */
export const KIND_INFO: Record<AutopilotLogEntry['kind'], { label: string; tone: Tone }> = {
  open: { label: 'Abre', tone: 'accent' },
  close: { label: 'Cierra', tone: 'purple' },
  skip: { label: 'Descarta', tone: 'muted' },
  info: { label: 'Nota', tone: 'info' },
}

// ----------------------------------------------------------------------------- parsing (GET, POST answers, socket)

const isStr = (v: unknown): v is string => typeof v === 'string'
const numOr = (v: unknown, fallback: number): number => (isNum(v) ? v : fallback)

/** NEXUS's read of the last decided bar of a watched symbol, or null (none yet, or malformed). */
export function normalizeLast(raw: unknown): AutopilotLast | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const tf = asTimeframe(isStr(o.tf) ? o.tf : null)
  if (!isNum(o.bar_time) || !tf || !isStr(o.label) || !o.label) return null
  return { bar_time: o.bar_time, tf, action: isStr(o.action) && o.action ? o.action : null, label: o.label }
}

function normalizeWatch(raw: unknown): AutopilotWatch[] {
  if (!Array.isArray(raw)) return []
  const out: AutopilotWatch[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const o = item as Record<string, unknown>
    if (!isStr(o.symbol) || !o.symbol) continue
    const direction = o.direction === 'long' || o.direction === 'short' ? o.direction : null
    out.push({ symbol: o.symbol, why: (isStr(o.why) ? o.why : '') as AutopilotWhy, direction, last: normalizeLast(o.last) })
  }
  return out
}

function normalizeLog(raw: unknown): AutopilotLogEntry[] {
  if (!Array.isArray(raw)) return []
  const out: AutopilotLogEntry[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const o = item as Record<string, unknown>
    if (!isNum(o.ts) || !isStr(o.text)) continue
    const kind = o.kind === 'open' || o.kind === 'close' || o.kind === 'skip' ? o.kind : 'info'
    out.push({ ts: o.ts, symbol: isStr(o.symbol) ? o.symbol : '', kind, text: o.text })
  }
  return out.sort((a, b) => b.ts - a.ts).slice(0, LOG_MAX) // newest first
}

function normalizeScan(raw: unknown): AutopilotScan | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.id)) return null
  return { id: o.id, finished_at: isNum(o.finished_at) ? o.finished_at : null, opportunities: Math.max(0, numOr(o.opportunities, 0)) }
}

export function normalizeStatus(raw: unknown): AutopilotStatus | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (typeof o.enabled !== 'boolean') return null
  return {
    enabled: o.enabled,
    account_id: numOr(o.account_id, AUTOPILOT_ACCOUNT_ID),
    source: o.source === 'list' || o.source === 'both' ? o.source : 'scanner',
    tf: asTimeframe(isStr(o.tf) ? o.tf : null) ?? '1h',
    symbols: Array.isArray(o.symbols) ? o.symbols.filter((s): s is string => isStr(s) && s !== '') : [],
    max_positions: numOr(o.max_positions, 5),
    risk_pct: numOr(o.risk_pct, 1),
    scanner_top: numOr(o.scanner_top, 10),
    watching: normalizeWatch(o.watching),
    scan: normalizeScan(o.scan),
    log: normalizeLog(o.log),
    alerts: o.alerts === true,
    alert_channels: Math.max(0, numOr(o.alert_channels, 0)),
    initial_balance: numOr(o.initial_balance, 1000),
    max_exposure_pct: numOr(o.max_exposure_pct, 100),
    max_drawdown_pct: numOr(o.max_drawdown_pct, 20),
    futures_margin_pct: numOr(o.futures_margin_pct, 50),
    stock_instrument: o.stock_instrument === 'shares' || o.stock_instrument === 'options' ? o.stock_instrument : 'auto',
    options_premium_pct: numOr(o.options_premium_pct, 10),
    learn_guard: o.learn_guard !== false,
    paused_contexts: normalizePaused(o.paused_contexts),
    health: normalizeHealth(o.health),
    weekly: normalizeWeekly(o.weekly),
    brake: normalizeBrake(o.brake),
    note: isStr(o.note) ? o.note : '',
  }
}

/** Defensive parse of an AutopilotSnapshot: null without a valid account or status. */
export function normalizeAutopilot(raw: unknown): AutopilotSnapshot | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const autopilot = normalizeStatus(o.autopilot)
  if (!isAccount(o.account) || !autopilot) return null
  const account = { ...o.account, auto_symbols: Array.isArray(o.account.auto_symbols) ? o.account.auto_symbols : [] }
  return {
    account,
    positions: normalizePositions(o.positions),
    history: normalizePositions(o.history),
    risk: normalizeRisk(o.risk),
    autopilot,
  }
}

function normalizePaused(raw: unknown): AutopilotPausedContext[] {
  if (!Array.isArray(raw)) return []
  const out: AutopilotPausedContext[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const o = item as Record<string, unknown>
    const ci = Array.isArray(o.ci) && o.ci.length === 2 && isNum(o.ci[0]) && isNum(o.ci[1]) ? ([o.ci[0], o.ci[1]] as [number, number]) : null
    if (!isStr(o.dimension) || !isStr(o.label) || !isStr(o.key) || !isStr(o.bucket_label) || !isNum(o.mean_r) || !ci) continue
    out.push({ dimension: o.dimension, label: o.label, key: o.key, bucket_label: o.bucket_label, n: Math.max(0, Math.round(numOr(o.n, 0))),
      wins: Math.max(0, Math.round(numOr(o.wins, 0))), mean_r: o.mean_r, ci })
  }
  return out
}

/** «Aprendió de sus pérdidas»: the contexts where it does not open now, with their evidence. */
export function LearnedNotice({ contexts, enabled }: { contexts: AutopilotPausedContext[]; enabled: boolean }) {
  if (!enabled || contexts.length === 0) return null
  return (
    <div className="notice ap-note" role="note">
      <Info size={15} style={{ flex: 'none', marginTop: 1 }} />
      <span>
        Aprendió de sus pérdidas y no opera en:{' '}
        {contexts
          .map((c) => `«${c.label}: ${c.bucket_label}» (${fmtInt(c.wins)} de ${fmtInt(c.n)} ganadoras en 90 días, R medio ${fmtR(c.mean_r)}, IC 99 % ${fmtR(c.ci[0])} a ${fmtR(c.ci[1])})`)
          .join('; ')}
        . Lo vuelve a probar cuando esas operaciones tengan más de 90 días.
      </span>
    </div>
  )
}

/** Its health (all clear when missing: an older server). */
export function normalizeHealth(raw: unknown): AutopilotHealth {
  const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const d = o.downtime && typeof o.downtime === 'object' ? (o.downtime as Record<string, unknown>) : null
  return {
    ok: o.ok !== false,
    last_decision: isNum(o.last_decision) ? o.last_decision : null,
    stale: Array.isArray(o.stale) ? o.stale.filter(isStr) : [],
    downtime: d && isNum(d.from) && isNum(d.to) ? { from: d.from, to: d.to } : null,
    text: isStr(o.text) ? o.text : '',
  }
}

function normalizeWeekly(raw: unknown): AutopilotWeekly | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.week_start) || !isNum(o.week_end) || !isStr(o.text)) return null
  const int = (v: unknown) => Math.max(0, Math.round(numOr(v, 0)))
  return {
    week_start: o.week_start, week_end: o.week_end, trades: int(o.trades), wins: int(o.wins),
    total_r: isNum(o.total_r) ? o.total_r : null, mean_r: isNum(o.mean_r) ? o.mean_r : null, pnl: numOr(o.pnl, 0),
    text: o.text, generated_at: numOr(o.generated_at, 0),
  }
}

function normalizeBrake(raw: unknown): AutopilotBrake | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.limit_pct) || !isNum(o.drawdown_pct)) return null
  return { limit_pct: o.limit_pct, drawdown_pct: Math.max(0, o.drawdown_pct), active: o.active === true }
}

/** Defensive parse of an AutopilotComparison: null without its verdict and texts. */
export function normalizeComparison(raw: unknown): AutopilotComparison | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const verdicts = ['none', 'insufficient', 'no_edge', 'better', 'worse'] as const
  const verdict = verdicts.find((v) => v === o.verdict)
  if (!verdict || !isStr(o.text) || !isStr(o.verdict_label)) return null
  const side = (v: unknown) => (v && typeof v === 'object' ? (v as Record<string, unknown>) : {})
  const nx = side(o.nexus)
  const rnd = side(o.random)
  const ci = Array.isArray(o.ci95) && o.ci95.length === 2 && isNum(o.ci95[0]) && isNum(o.ci95[1]) ? ([o.ci95[0], o.ci95[1]] as [number, number]) : null
  const int = (v: unknown) => Math.max(0, Math.round(numOr(v, 0)))
  return {
    generated_at: numOr(o.generated_at, 0),
    window_bars: int(o.window_bars),
    trades: int(o.trades),
    compared: int(o.compared),
    missing: int(o.missing),
    days: int(o.days),
    nexus: { n: int(nx.n), wins: int(nx.wins), mean_r: isNum(nx.mean_r) ? nx.mean_r : null },
    random: { entries: int(rnd.entries), wins: int(rnd.wins), mean_r: isNum(rnd.mean_r) ? rnd.mean_r : null },
    diff_r: isNum(o.diff_r) ? o.diff_r : null,
    ci95: ci,
    verdict,
    verdict_label: o.verdict_label,
    text: o.text,
    note: isStr(o.note) ? o.note : '',
  }
}

const JOURNAL_VERDICTS: AutopilotJournalVerdict[] = ['none', 'small', 'unclear', 'loses', 'wins']
const JOURNAL_TONE: Record<AutopilotJournalVerdict, Tone> = { none: 'muted', small: 'muted', unclear: 'muted', loses: 'neg', wins: 'accent' }

function pair(v: unknown): [number, number] | null {
  return Array.isArray(v) && v.length === 2 && isNum(v[0]) && isNum(v[1]) ? [v[0], v[1]] : null
}

function journalVerdict(v: unknown): AutopilotJournalVerdict {
  return JOURNAL_VERDICTS.find((x) => x === v) ?? 'none'
}

/** Defensive parse of an AutopilotJournal: null without its counts and texts. */
export function normalizeJournal(raw: unknown): AutopilotJournal | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isNum(o.trades) || !Array.isArray(o.groups) || !isStr(o.verdict_label)) return null
  const int = (v: unknown) => Math.max(0, Math.round(numOr(v, 0)))
  const opt = (v: unknown) => (isNum(v) ? v : null)
  const bucket = (b: unknown): AutopilotJournalBucket | null => {
    if (!b || typeof b !== 'object') return null
    const x = b as Record<string, unknown>
    if (!isStr(x.key) || !isStr(x.label)) return null
    const verdict = journalVerdict(x.verdict)
    return { key: x.key, label: x.label, n: int(x.n), wins: int(x.wins), mean_r: opt(x.mean_r), ci95: pair(x.ci95), verdict,
      verdict_label: isStr(x.verdict_label) ? x.verdict_label : '' }
  }
  const groups = o.groups
    .map((g) => {
      if (!g || typeof g !== 'object') return null
      const x = g as Record<string, unknown>
      if (!isStr(x.dimension) || !isStr(x.label) || !Array.isArray(x.buckets)) return null
      return { dimension: x.dimension, label: x.label,
        buckets: x.buckets.map(bucket).filter((b): b is AutopilotJournalBucket => b !== null) }
    })
    .filter((g): g is AutopilotJournal['groups'][number] => g !== null)
  return {
    generated_at: numOr(o.generated_at, 0), trades: int(o.trades), wins: int(o.wins), mean_r: opt(o.mean_r), ci95: pair(o.ci95),
    verdict: journalVerdict(o.verdict), verdict_label: o.verdict_label, mae_r_losers: opt(o.mae_r_losers),
    mfe_r_losers: opt(o.mfe_r_losers), mae_r_winners: opt(o.mae_r_winners), mfe_r_winners: opt(o.mfe_r_winners),
    groups, insights: Array.isArray(o.insights) ? o.insights.filter(isStr) : [], note: isStr(o.note) ? o.note : '',
  }
}

/** «Diario de operaciones»: what its trades have in common (cause, session, regime, news…), «x de n» and the mean R,
 *  with the backend's verdicts verbatim (nothing concluded below 30 trades). */
export function JournalCard({ journal, loading, error, onRefresh }: { journal: AutopilotJournal | null; loading: boolean; error: string | null; onRefresh: () => void }) {
  return (
    <Card
      title={
        <>
          <BookOpen size={14} /> Diario de operaciones {journal && <Badge tone={JOURNAL_TONE[journal.verdict]}>{journal.verdict_label}</Badge>}
        </>
      }
      sub="Qué tienen en común sus operaciones: causa, sesión, régimen, noticias, filtro y mercado"
      actions={
        <Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} onClick={onRefresh} loading={loading} title="Volver a analizar sus operaciones cerradas">
          Recalcular
        </Button>
      }
    >
      {error && <ErrorBox error={error} onRetry={onRefresh} />}
      {!journal ? (
        !error && <Loading label="Analizando sus operaciones…" />
      ) : (
        <div className="col gap-2">
          {journal.trades > 0 && (
            <div className="small">
              {fmtInt(journal.wins)} de {fmtInt(journal.trades)} ganadoras · R medio {fmtR(journal.mean_r)}
              {journal.ci95 ? ` · IC 95 %: ${fmtR(journal.ci95[0])} a ${fmtR(journal.ci95[1])}` : ''}
            </div>
          )}
          {journal.insights.length > 0 && (
            <ul className="ap-rules small">
              {journal.insights.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          )}
          {journal.trades > 0 &&
            journal.groups
              .filter((g) => g.buckets.length > 0)
              .map((g) => (
                <div key={g.dimension} className="table-wrap">
                  <table className="table ap-table">
                    <thead>
                      <tr>
                        <th>{g.label}</th>
                        <th>Ganadoras</th>
                        <th>R medio</th>
                        <th>Veredicto</th>
                      </tr>
                    </thead>
                    <tbody>
                      {g.buckets.map((b) => (
                        <tr key={b.key}>
                          <td>{b.label}</td>
                          <td>
                            {fmtInt(b.wins)} de {fmtInt(b.n)}
                          </td>
                          <td className="mono">{fmtR(b.mean_r)}</td>
                          <td>
                            <Badge tone={JOURNAL_TONE[b.verdict]}>{b.verdict_label}</Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
          <div className="tiny muted">{journal.note}</div>
        </div>
      )}
    </Card>
  )
}

const VERDICT_TONE: Record<AutopilotComparison['verdict'], Tone> = {
  none: 'muted',
  insufficient: 'muted',
  no_edge: 'warn',
  better: 'accent',
  worse: 'neg',
}

/** "Cómo gestiona el dinero": its rules in plain words, with its own settings (never a promise of profit). */
export function MoneyCard({ status, dailyLossPct }: { status: AutopilotStatus; dailyLossPct: number | null }) {
  const pct = (v: number, d = 1) => `${fmtNum(v, Number.isInteger(v) ? 0 : d)} %`
  const rules = [
    `Arriesga el ${pct(status.risk_pct)} de su capital en cada operación: lo que perdería si salta el stop.`,
    `Como mucho ${plural(status.max_positions, 'posición', 'posiciones')} a la vez, una por símbolo, y con el límite de posiciones por grupo correlacionado de tu riesgo de cartera.`,
    status.max_exposure_pct > 0
      ? `Cuenta de efectivo: no tiene invertido más del ${pct(status.max_exposure_pct, 0)} de su capital en acciones, fondos, cripto y primas de opciones (si no cabe entera, compra menos).`
      : 'Sin tope de exposición propio: solo el límite general de 5 veces el capital por posición.',
    'Cierra 1/3 en el primer objetivo y sube el stop a la entrada; otro 1/3 en el segundo, y deja correr el resto con un stop dinámico.',
    'Sale antes si NEXUS cambia de opinión al cierre de una vela, y nunca abre en un mercado cerrado.',
    status.futures_margin_pct > 0
      ? `Futuros de índices (micro o mini): contratos enteros, solo si uno cabe en su riesgo por operación y su margen aproximado en el ${pct(status.futures_margin_pct, 0)} del capital; con una cuenta pequeña casi nunca caben.`
      : 'No opera futuros (margen máximo en futuros: 0 %).',
    stocksRule(status, pct),
    status.learn_guard
      ? 'Aprende de sus pérdidas: deja de operar en un contexto (sesión, régimen, noticias, puntuación del filtro, dirección o mercado) donde sus operaciones de los últimos 90 días pierden con evidencia fuerte (30 o más y el intervalo del 99 % bajo cero).'
      : 'No aprende de sus pérdidas: «Aprender de sus pérdidas» está desactivado.',
    status.max_drawdown_pct > 0
      ? `Se frena si pierde un ${pct(status.max_drawdown_pct, 0)} desde su saldo inicial${dailyLossPct ? ` o un ${pct(dailyLossPct)} en el día` : ''}.`
      : `Sin freno de pérdidas acumuladas${dailyLossPct ? `; sí se frena si pierde un ${pct(dailyLossPct)} en el día` : ''}.`,
  ]
  return (
    <Card
      title={
        <>
          <Wallet size={14} /> Cómo gestiona el dinero
        </>
      }
      sub={`Cuenta simulada que empezó con ${fmtMoney(status.initial_balance)}`}
    >
      <ul className="ap-rules small">
        {rules.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      <div className="tiny muted">
        Estas reglas limitan las pérdidas y el tamaño de cada operación; no crean ventaja ni garantizan ganancias. La validación publicada no encontró ventaja.
      </div>
    </Card>
  )
}

/** Its rule for stocks and funds: shares, options (theoretical prices) or its own choice. */
function stocksRule(s: AutopilotStatus, pct: (v: number, d?: number) => string): string {
  if (s.stock_instrument === 'shares') return 'Acciones: compra acciones y también vende en corto (simulado).'
  const how =
    s.stock_instrument === 'options'
      ? 'solo opciones, un call para apostar al alza y un put a la baja'
      : 'compra acciones para apostar al alza y un put para apostar a la baja (una cuenta de efectivo no vende en corto)'
  if (s.options_premium_pct <= 0) {
    return s.stock_instrument === 'options'
      ? 'Acciones: no las opera (solo opciones, y su prima máxima es 0 %).'
      : 'Acciones: compra acciones para apostar al alza; no apuesta a la baja (prima máxima de opciones: 0 %).'
  }
  return `Acciones: ${how}. Las opciones tienen precio teórico (Black-Scholes con la volatilidad de 20 días, no cotizaciones reales): paga como mucho el ${pct(s.options_premium_pct, 0)} de su capital en primas por operación, con el stop en −50 % de la prima, y las cierra 7 días antes de su vencimiento.`
}

/** Its health notice: watched markets without recent decisions, and a stop of the server or the PC it detected. */
export function HealthNotice({ health, enabled }: { health: AutopilotHealth; enabled: boolean }) {
  if (!enabled || (health.ok && !health.downtime)) return null
  return (
    <>
      {!health.ok && (
        <div className="notice ap-brake" role="alert">
          <TriangleAlert size={15} style={{ flex: 'none', marginTop: 1 }} />
          <span>{health.text}</span>
        </div>
      )}
      {health.downtime && (
        <div className="notice ap-note" role="note">
          <PowerOff size={15} style={{ flex: 'none', marginTop: 1 }} />
          <span>
            Estuvo detenida de {fmtDateTime(health.downtime.from)} a {fmtDateTime(health.downtime.to)} (servidor apagado o PC suspendido): mientras tanto
            no decidió. Para que opere sola, deja el servidor en marcha y el PC sin suspenderse.
          </span>
        </div>
      )}
    </>
  )
}

/** «Informe semanal»: its last weekly report, verbatim from the backend. */
export function WeeklyCard({ weekly }: { weekly: AutopilotWeekly | null }) {
  return (
    <Card
      title={
        <>
          <CalendarDays size={14} /> Informe semanal
        </>
      }
      sub="Cada lunes, la semana anterior; también por Telegram o correo si activaste sus avisos"
    >
      {weekly ? (
        <p className="small">{weekly.text}</p>
      ) : (
        <span className="tiny muted">Todavía no hay informe: el primero llega el lunes siguiente a una semana completa activa.</span>
      )}
    </Card>
  )
}

/** The brake notice: why it opens nothing new and what to do. */
export function BrakeNotice({ brake }: { brake: AutopilotBrake | null }) {
  if (!brake?.active) return null
  return (
    <div className="notice ap-brake" role="alert">
      <TriangleAlert size={15} style={{ flex: 'none', marginTop: 1 }} />
      <span>
        Freno de pérdidas activo: la cuenta está un {fmtNum(brake.drawdown_pct, 1)} % por debajo de su saldo inicial (límite {fmtNum(brake.limit_pct, 0)} %). No abre
        operaciones nuevas; las abiertas siguen con su stop y sus objetivos. Reiníciala o cambia el freno en «Cómo opera».
      </span>
    </div>
  )
}

/** The 95 % interval as shown (the backend's rule: none below 10 trades, «muy incierto» below 30). */
export function compareInterval(c: AutopilotComparison): string {
  if (c.compared < 10) return 'sin intervalo: menos de 10 operaciones'
  if (!c.ci95) return 'sin intervalo'
  const text = `${fmtR(c.ci95[0])} a ${fmtR(c.ci95[1])}`
  return c.compared < 30 ? `${text} · muy incierto` : text
}

/** "Frente a entrar al azar": NEXUS automática against random entries in the same markets and moments. */
export function CompareCard({ cmp, loading, error, onRefresh }: { cmp: AutopilotComparison | null; loading: boolean; error: string | null; onRefresh: () => void }) {
  return (
    <Card
      title={
        <>
          <Scale size={14} /> Frente a entrar al azar {cmp && <Badge tone={VERDICT_TONE[cmp.verdict]}>{cmp.verdict_label}</Badge>}
        </>
      }
      sub="¿Opera mejor que entrar al azar en los mismos mercados y momentos?"
      actions={
        <Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} onClick={onRefresh} loading={loading} title="Volver a calcular con los datos de ahora">
          Recalcular
        </Button>
      }
    >
      {error && <ErrorBox error={error} onRetry={onRefresh} />}
      {!cmp ? (
        !error && <Loading label="Comparando con entradas al azar…" />
      ) : (
        <div className="col gap-2">
          <p className="small ap-compare-text">{cmp.text}</p>
          {cmp.compared > 0 && (
            <>
              <div className="table-wrap">
                <table className="table ap-table">
                  <thead>
                    <tr>
                      <th />
                      <th>R medio por operación</th>
                      <th>Ganadoras</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>NEXUS automática</td>
                      <td className="mono">{fmtR(cmp.nexus.mean_r)}</td>
                      <td>
                        {fmtInt(cmp.nexus.wins)} de {fmtInt(cmp.nexus.n)}
                      </td>
                    </tr>
                    <tr>
                      <td>Entrar al azar</td>
                      <td className="mono">{fmtR(cmp.random.mean_r)}</td>
                      <td>
                        {fmtInt(cmp.random.wins)} de {fmtInt(cmp.random.entries)} entradas
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div className="tiny">
                Diferencia: <span className="mono">{fmtR(cmp.diff_r)}</span> por operación · IC 95 %: {compareInterval(cmp)} ·{' '}
                {fmtInt(cmp.compared)} de {fmtInt(cmp.trades)} operaciones con referencia, en {plural(cmp.days, 'día', 'días')}
              </div>
            </>
          )}
          {cmp.note && <div className="tiny muted">{cmp.note}</div>}
        </div>
      )}
    </Card>
  )
}

/** The snapshot a socket message carries for this view: only {type: 'paper_autopilot'} of NEXUS's account. The user's
 *  {type: 'paper'} messages (account 1) never feed it. */
export function autopilotFromMessage(msg: ServerMessage | null | undefined): AutopilotSnapshot | null {
  if (!msg || msg.type !== 'paper_autopilot') return null
  const snap = normalizeAutopilot(msg)
  return snap && snap.account.id === AUTOPILOT_ACCOUNT_ID ? snap : null
}

/** Live snapshots of NEXUS's account ('paper' is a default topic: listen only). Returns the unsubscribe function. */
export function subscribeAutopilot(onSnapshot: (snap: AutopilotSnapshot) => void): () => void {
  return socket.on('paper_autopilot', (msg) => {
    const snap = autopilotFromMessage(msg)
    if (snap) onSnapshot(snap)
  })
}

// ----------------------------------------------------------------------------- settings draft (controls card)

/** Editable settings of the controls card (a number is null while its input is empty). */
export interface AutopilotDraft {
  source: AutopilotSource
  symbols: string[]
  tf: Timeframe
  max_positions: number | null
  risk_pct: number | null
  scanner_top: number | null
  max_exposure_pct: number | null
  max_drawdown_pct: number | null
  futures_margin_pct: number | null
  stock_instrument: AutopilotStockInstrument
  options_premium_pct: number | null
}

type NumField =
  | 'max_positions'
  | 'risk_pct'
  | 'scanner_top'
  | 'max_exposure_pct'
  | 'max_drawdown_pct'
  | 'futures_margin_pct'
  | 'options_premium_pct'

/** The server's limits for each number (same wording as its 400 answers). */
const RANGES: Record<NumField, { lo: number; hi: number; int: boolean; what: string }> = {
  max_positions: { lo: 1, hi: 20, int: true, what: 'El máximo de posiciones' },
  risk_pct: { lo: 0.1, hi: 5, int: false, what: 'El riesgo por operación (%)' },
  scanner_top: { lo: 1, hi: 30, int: true, what: 'El número de candidatos del escáner' },
  max_exposure_pct: { lo: 0, hi: 500, int: false, what: 'La exposición máxima (%)' },
  max_drawdown_pct: { lo: 0, hi: 90, int: false, what: 'El freno de pérdidas (%)' },
  futures_margin_pct: { lo: 0, hi: 100, int: false, what: 'El margen máximo en futuros (%)' },
  options_premium_pct: { lo: 0, hi: 50, int: false, what: 'La prima máxima por operación (%)' },
}

/** The list is used by the sources list and both; the scanner candidates by scanner and both. */
const usesList = (source: AutopilotSource) => source !== 'scanner'
const usesScanner = (source: AutopilotSource) => source !== 'list'

/** Numbers shown for a source: the scanner candidates only when the scanner is used. */
const numFields = (source: AutopilotSource): NumField[] => [
  'max_positions',
  'risk_pct',
  ...(usesScanner(source) ? (['scanner_top'] as NumField[]) : []),
  'max_exposure_pct',
  'max_drawdown_pct',
  'futures_margin_pct',
  'options_premium_pct',
]

export function draftFrom(s: AutopilotStatus): AutopilotDraft {
  return {
    source: s.source,
    symbols: [...s.symbols],
    tf: s.tf,
    max_positions: s.max_positions,
    risk_pct: s.risk_pct,
    scanner_top: s.scanner_top,
    max_exposure_pct: s.max_exposure_pct,
    max_drawdown_pct: s.max_drawdown_pct,
    futures_margin_pct: s.futures_margin_pct,
    stock_instrument: s.stock_instrument,
    options_premium_pct: s.options_premium_pct,
  }
}

const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i])

/** What POST /api/paper/autopilot gets: the visible fields that differ from the saved settings (the symbol list only in
 *  list mode, the scanner candidates only in scanner mode). Empty numbers are left out: validateDraft reports them. */
export function draftChanges(d: AutopilotDraft, saved: AutopilotDraft): AutopilotUpdate {
  const out: AutopilotUpdate = {}
  if (d.source !== saved.source) out.source = d.source
  if (d.tf !== saved.tf) out.tf = d.tf
  if (d.stock_instrument !== saved.stock_instrument) out.stock_instrument = d.stock_instrument
  for (const k of numFields(d.source)) {
    const v = d[k]
    if (v !== null && v !== saved[k]) out[k] = v
  }
  if (usesList(d.source) && !sameList(d.symbols, saved.symbols)) out.symbols = [...d.symbols]
  return out
}

/** Unsaved edits in the visible fields (an emptied number counts). */
export function draftDirty(d: AutopilotDraft, saved: AutopilotDraft): boolean {
  return Object.keys(draftChanges(d, saved)).length > 0 || numFields(d.source).some((k) => d[k] === null)
}

/** Spanish problem with the visible fields (the server's limits), or null. */
export function validateDraft(d: AutopilotDraft): string | null {
  for (const k of numFields(d.source)) {
    const v = d[k]
    const r = RANGES[k]
    if (v === null || !(v >= r.lo && v <= r.hi) || (r.int && !Number.isInteger(v))) {
      const digits = r.int ? 0 : 1
      return `${r.what} debe ser ${r.int ? 'un número entero' : 'un número'} entre ${fmtNum(r.lo, digits)} y ${fmtNum(r.hi, digits)}.`
    }
  }
  if (usesList(d.source) && d.symbols.length > MAX_SYMBOLS) return `Como máximo ${MAX_SYMBOLS} símbolos en la lista.`
  return null
}

/** Key of the saved settings: the draft follows the server when they change, not on every live update. */
const settingsKey = (s: AutopilotStatus) => JSON.stringify(draftFrom(s))

export interface AutopilotControlsProps {
  status: AutopilotStatus
  /** A change is being saved: every control is disabled until the answer. */
  saving: boolean
  /** Spanish detail of the last refused change (HTTP 400), or null. */
  error: string | null
  /** Sends the changed fields; resolves with the saved status, or null when the server refused them. */
  onSave: (update: AutopilotUpdate) => Promise<AutopilotStatus | null>
}

/** How NEXUS's account trades. The on/off switch applies at once; the other fields are a draft saved with "Guardar". */
export const AutopilotControls = memo(function AutopilotControls({ status, saving, error, onSave }: AutopilotControlsProps) {
  const key = settingsKey(status)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const saved = useMemo(() => draftFrom(status), [key])
  const [draft, setDraft] = useState<AutopilotDraft>(saved)
  const [turning, setTurning] = useState<boolean | null>(null) // on / off being saved, shown at once
  const [alerting, setAlerting] = useState<boolean | null>(null) // its alerts switch being saved
  const [learning, setLearning] = useState<boolean | null>(null) // its learn-from-losses switch being saved
  const [submitting, setSubmitting] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const prevSaved = useRef(saved)
  const sourceLabelId = useId()

  // Follow server-side changes (another tab, the terminal) unless there are unsaved edits here.
  useEffect(() => {
    const prev = prevSaved.current
    if (prev === saved) return
    prevSaved.current = saved
    setDraft((d) => (draftDirty(d, prev) ? d : saved))
  }, [saved])

  const dirty = draftDirty(draft, saved)
  const problem = validateDraft(draft)
  const enabled = turning ?? status.enabled
  const alertsOn = alerting ?? status.alerts
  const learnOn = learning ?? status.learn_guard
  const shownError = localError ?? error

  const patch = (p: Partial<AutopilotDraft>) => {
    setLocalError(null)
    setDraft((d) => ({ ...d, ...p }))
  }

  const turn = async (next: boolean) => {
    setLocalError(null)
    setTurning(next)
    try {
      await onSave({ enabled: next })
    } finally {
      setTurning(null)
    }
  }

  const turnAlerts = async (next: boolean) => {
    setLocalError(null)
    setAlerting(next)
    try {
      await onSave({ alerts: next })
    } finally {
      setAlerting(null)
    }
  }

  const turnLearning = async (next: boolean) => {
    setLocalError(null)
    setLearning(next)
    try {
      await onSave({ learn_guard: next })
    } finally {
      setLearning(null)
    }
  }

  const submit = async () => {
    if (!dirty || problem) return
    setLocalError(null)
    setSubmitting(true)
    try {
      const res = await onSave(draftChanges(draft, saved))
      if (res) setDraft(draftFrom(res)) // as the server stored it (e.g. symbols resolved to EXCHANGE:TICKER)
    } finally {
      setSubmitting(false)
    }
  }

  const addSymbol = (s: string) => {
    const sym = s.trim().toUpperCase()
    if (!sym || draft.symbols.includes(sym)) return
    if (draft.symbols.length >= MAX_SYMBOLS) {
      setLocalError(`Como máximo ${MAX_SYMBOLS} símbolos en la lista.`)
      return
    }
    patch({ symbols: [...draft.symbols, sym] })
  }

  return (
    <Card
      title={
        <>
          <SlidersHorizontal size={14} /> Cómo opera
        </>
      }
      sub="Solo cambia la cuenta de NEXUS: tu cuenta no se toca"
      actions={saving ? <Spinner /> : undefined}
    >
      <fieldset className="ap-fieldset" disabled={saving}>
        <Toggle
          checked={enabled}
          onChange={(v) => void turn(v)}
          label={<span className={enabled ? 'pos bold' : 'text-2'}>{enabled ? 'Activa: NEXUS opera sola' : 'Pausada'}</span>}
        />
        {!enabled && <div className="tiny muted">Pausada: no abre posiciones nuevas; las abiertas siguen con su stop y sus objetivos.</div>}

        <Toggle
          checked={alertsOn}
          onChange={(v) => void turnAlerts(v)}
          label={<span className={alertsOn ? 'bold' : 'text-2'}>Avisarme por Telegram o correo cuando opere</span>}
        />
        <div className={`tiny ${alertsOn && status.alert_channels <= 0 ? 'warn' : 'muted'}`}>
          {!alertsOn
            ? 'Desactivado: sus operaciones no se avisan fuera de la app.'
            : status.alert_channels > 0
              ? 'Avisa de sus aperturas, cierres y stops (operaciones simuladas), según el nivel de avisos de Ajustes › Alertas.'
              : 'Ningún canal de avisos está activo: configura Telegram o correo en Ajustes › Alertas.'}
        </div>

        <Toggle
          checked={learnOn}
          onChange={(v) => void turnLearning(v)}
          label={<span className={learnOn ? 'bold' : 'text-2'}>Aprender de sus pérdidas</span>}
        />
        <div className="tiny muted">
          {learnOn
            ? 'Deja de operar donde sus operaciones de los últimos 90 días pierden con evidencia fuerte (30 o más, intervalo del 99 % bajo cero).'
            : 'Desactivado: opera en todos los contextos aunque pierda en alguno.'}
        </div>

        <div className="field">
          <span className="label" id={sourceLabelId}>
            Qué vigila
          </span>
          <div className="btn-group pp-type" role="group" aria-labelledby={sourceLabelId}>
            {SOURCE_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                className={`btn btn-sm ${draft.source === o.value ? 'active' : ''}`}
                aria-pressed={draft.source === o.value}
                title={o.title}
                onClick={() => patch({ source: o.value })}
              >
                {o.label}
              </button>
            ))}
          </div>
          <span className="tiny muted">
            {draft.source === 'scanner'
              ? 'Los mejores candidatos del último escaneo, el automático o uno que lances en Escáner: primero las oportunidades TOMAR (filtro TOMAR con señal de NEXUS) y después los mercados que el filtro aprueba a la espera de una señal. Solo mercados en dólares. Si un escaneo no encuentra ninguno, no vigila nada: «Ambos» vigila además tu lista.'
              : draft.source === 'both'
                ? 'Tu lista (acciones, cripto…) y además los mejores candidatos del último escaneo. En cada vela cerrada NEXUS decide si entra y en qué dirección: solo con su señal y el filtro TOMAR.'
                : 'Solo los símbolos de la lista (acciones, cripto…): en cada vela cerrada NEXUS decide si entra y en qué dirección. Las acciones solo se operan con el mercado abierto.'}
          </span>
        </div>

        {usesList(draft.source) && (
          <Field label={`Símbolos (${draft.symbols.length})`}>
            {draft.symbols.length > 0 ? (
              <div className="pp-chips">
                {draft.symbols.map((s) => (
                  <span key={s} className="pp-chip" title={s}>
                    {splitSymbol(s).ticker}
                    <button
                      type="button"
                      aria-label={`Quitar ${s}`}
                      title={`Quitar ${s}`}
                      onClick={() => patch({ symbols: draft.symbols.filter((x) => x !== s) })}
                    >
                      <X size={11} />
                    </button>
                  </span>
                ))}
              </div>
            ) : (
              <span className="tiny muted">Ningún símbolo todavía.</span>
            )}
            <SymbolSearch onSelect={addSymbol} placeholder="Añadir símbolo…" compact />
          </Field>
        )}

        <div className="ap-grid2">
          <Field label="Temporalidad" hint="Velas en las que decide">
            <Select value={draft.tf} onChange={(tf) => patch({ tf })} options={TF_OPTIONS} />
          </Field>
          <Field label="Máximo de posiciones" hint="De 1 a 20, una por símbolo">
            <NumberInput value={draft.max_positions} onChange={(v) => patch({ max_positions: v })} min={1} max={20} step={1} />
          </Field>
          <Field label="Riesgo por operación (%)" hint="Del capital, hasta el stop: de 0,1 a 5">
            <NumberInput value={draft.risk_pct} onChange={(v) => patch({ risk_pct: v })} min={0.1} max={5} step={0.1} />
          </Field>
          {usesScanner(draft.source) && (
            <Field label="Candidatos del escáner" hint="Oportunidades del último escaneo que vigila: de 1 a 30">
              <NumberInput value={draft.scanner_top} onChange={(v) => patch({ scanner_top: v })} min={1} max={30} step={1} />
            </Field>
          )}
          <Field label="Exposición máxima (%)" hint="Lo invertido en acciones, fondos, cripto y primas, del capital: 100 = sin apalancamiento">
            <NumberInput value={draft.max_exposure_pct} onChange={(v) => patch({ max_exposure_pct: v })} min={0} max={500} step={10} />
          </Field>
          <Field label="Freno de pérdidas (%)" hint="Deja de abrir operaciones si cae este % desde su saldo inicial (0 = sin freno)">
            <NumberInput value={draft.max_drawdown_pct} onChange={(v) => patch({ max_drawdown_pct: v })} min={0} max={90} step={5} />
          </Field>
          <Field label="Margen máximo en futuros (%)" hint="Del capital, como garantía aproximada de los contratos (0 = no opera futuros)">
            <NumberInput value={draft.futures_margin_pct} onChange={(v) => patch({ futures_margin_pct: v })} min={0} max={100} step={10} />
          </Field>
          <Field label="Acciones y fondos" hint="Con qué opera sus señales: las opciones tienen precio teórico">
            <Select value={draft.stock_instrument} onChange={(v) => patch({ stock_instrument: v })} options={INSTRUMENT_OPTIONS} />
          </Field>
          <Field label="Prima máxima por operación (%)" hint="Del capital, lo más que paga por opciones (0 = no compra opciones)">
            <NumberInput value={draft.options_premium_pct} onChange={(v) => patch({ options_premium_pct: v })} min={0} max={50} step={1} />
          </Field>
        </div>

        {problem && <div className="tiny warn">{problem}</div>}
        {draft.source === 'list' && draft.symbols.length === 0 && (
          <div className="tiny warn">Con la lista vacía NEXUS no vigila nada: añade al menos un símbolo.</div>
        )}
        {shownError && (
          <div className="tiny neg" role="alert">
            {shownError}
          </div>
        )}

        <div className="row gap-2 wrap">
          <Button size="sm" variant="primary" onClick={() => void submit()} disabled={!dirty || !!problem} loading={submitting}>
            Guardar cambios
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setLocalError(null)
              setDraft(saved)
            }}
            disabled={!dirty}
          >
            Descartar
          </Button>
          {dirty && <span className="tiny warn">Cambios sin guardar</span>}
        </div>
      </fieldset>
    </Card>
  )
})

// ----------------------------------------------------------------------------- what it watches · decision log

/** Spanish empty state of the watch list, by cause. */
export function emptyWatchText(s: AutopilotStatus): string {
  if (!s.enabled) return 'Pausada: no vigila ningún símbolo. Actívala en «Cómo opera» para que NEXUS empiece a operar.'
  if (s.source === 'list') return s.symbols.length ? 'Preparando la lista de símbolos…' : 'La lista está vacía: añade símbolos en «Cómo opera».'
  if (!s.scan) return 'Cuando termine un escaneo, NEXUS vigilará sus mejores candidatos.'
  if (s.scan.finished_at === null) return 'Esperando a que termine el escaneo en curso.'
  if (s.source === 'scanner')
    return 'El último escaneo no encontró candidatos (ni oportunidades TOMAR ni mercados que el filtro apruebe): no vigila nada hasta el próximo. Con «Ambos» vigila además tu lista.'
  return 'Nada que vigilar ahora mismo.'
}

function ScanLine({ scan }: { scan: AutopilotScan | null }) {
  if (!scan) return <div className="ap-scan tiny muted">Sin escaneos terminados todavía.</div>
  return (
    <div className="ap-scan small">
      <span className="mono">Escaneo #{scan.id}</span>
      {scan.finished_at !== null ? (
        <span className="muted" title={fmtDateTime(scan.finished_at)}>
          terminado {fmtAgo(scan.finished_at)}
        </span>
      ) : (
        <span className="muted">aún en curso</span>
      )}
      <Badge tone={scan.opportunities > 0 ? 'accent' : 'muted'}>{plural(scan.opportunities, 'oportunidad TOMAR', 'oportunidades TOMAR')}</Badge>
    </div>
  )
}

/** "Qué está vigilando": the candidates (and why), the direction when it is known and, for the scanner, its scan. */
export const WatchCard = memo(function WatchCard({ status, onOpenChart }: { status: AutopilotStatus; onOpenChart: (symbol: string) => void }) {
  const { watching } = status
  const scanPart = `${plural(status.scanner_top, 'candidato', 'candidatos')} del último escaneo`
  const sub =
    status.source === 'scanner'
      ? `Hasta ${scanPart} · decide en velas de ${status.tf}`
      : status.source === 'both'
        ? `Tu lista y hasta ${scanPart} · decide en velas de ${status.tf}`
        : `Lista fija · decide en velas de ${status.tf}`
  return (
    <Card
      flush
      title={
        <>
          <Radar size={14} /> Qué está vigilando <Badge tone={watching.length ? 'accent' : 'muted'}>{fmtInt(watching.length)}</Badge>
        </>
      }
      sub={sub}
    >
      {status.source !== 'list' && <ScanLine scan={status.scan} />}
      {watching.length > 0 ? (
        <div className="table-wrap">
          <table className="table ap-table">
            <thead>
              <tr>
                <th>Símbolo</th>
                <th>Motivo</th>
                <th title="Dirección de la oportunidad o de la posición abierta">Dirección</th>
                <th title="Lo que dijo NEXUS al cierre de la última vela que decidió (la mayoría no da entrada: ESPERAR o MANTENER)">
                  Última vela
                </th>
              </tr>
            </thead>
            <tbody>
              {watching.map((w) => {
                const { exchange, ticker } = splitSymbol(w.symbol)
                const why = WHY_INFO[w.why]
                return (
                  <tr key={`${w.symbol}-${w.why}`}>
                    <td>
                      <div className="pp-sym">
                        <button type="button" onClick={() => onOpenChart(w.symbol)} title={`Abrir ${w.symbol} en el gráfico`}>
                          {ticker}
                        </button>
                        <span className="pp-sub">{exchange || '—'}</span>
                      </div>
                    </td>
                    <td>{w.why ? <Badge tone={why?.tone ?? 'muted'}>{why?.label ?? w.why}</Badge> : <span className="tiny muted">—</span>}</td>
                    <td>
                      {w.direction ? (
                        <Badge tone={w.direction === 'long' ? 'pos' : 'neg'}>{DIRECTION_LABEL[w.direction]}</Badge>
                      ) : (
                        <span className="tiny muted" title="Sin dirección fija: la decide la señal de NEXUS en cada vela cerrada">
                          según la señal
                        </span>
                      )}
                    </td>
                    <td>
                      <LastRead last={w.last ?? null} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="ap-empty tiny muted">{emptyWatchText(status)}</div>
      )}
    </Card>
  )
})

/** NEXUS's read of the last decided bar: its action and the bar ("vela de las 14:00"), or a dash before the first one. */
function LastRead({ last }: { last: AutopilotLast | null }) {
  if (!last) {
    return (
      <span className="tiny muted" title="Aún no ha cerrado ninguna vela de esta temporalidad desde que lo vigila">
        —
      </span>
    )
  }
  return (
    <span className="ap-last" title={`Vela de ${last.tf} que abrió ${fmtDateTime(last.bar_time)}`}>
      {last.action ? <Badge tone={actionTone(last.action)}>{last.label}</Badge> : <span className="tiny warn">{last.label}</span>}
      <span className="tiny muted mono">{logTime(last.bar_time)}</span>
    </span>
  )
}

/** "14:03" today, "30/10 14:03" another day. */
function logTime(ms: number): string {
  const d = new Date(ms)
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay
    ? d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString('es-ES', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

const isoTime = (ms: number): string | undefined => {
  const d = new Date(ms)
  return Number.isFinite(d.getTime()) ? d.toISOString() : undefined
}

/** The latest decisions of NEXUS's account (newest first), each with the tone of its kind. */
export const DecisionLog = memo(function DecisionLog({ log, onOpenChart }: { log: AutopilotLogEntry[]; onOpenChart: (symbol: string) => void }) {
  return (
    <Card
      flush
      title={
        <>
          <ScrollText size={14} /> Decisiones recientes <Badge tone="muted">{fmtInt(log.length)}</Badge>
        </>
      }
      sub={`Qué hizo NEXUS en su cuenta y por qué (las ${LOG_MAX} últimas)`}
    >
      {log.length > 0 ? (
        <ol className="ap-log">
          {log.map((e, i) => {
            const kind = KIND_INFO[e.kind] ?? KIND_INFO.info
            const ticker = e.symbol && e.symbol !== '—' ? splitSymbol(e.symbol).ticker : null
            return (
              <li key={`${e.ts}-${i}`} className={`ap-log-row ap-tone-${kind.tone}`}>
                <time className="ap-log-time mono" dateTime={isoTime(e.ts)} title={fmtDateTime(e.ts)}>
                  {logTime(e.ts)}
                </time>
                <Badge tone={kind.tone}>{kind.label}</Badge>
                {ticker && (
                  <button type="button" className="ap-sym" onClick={() => onOpenChart(e.symbol)} title={`Abrir ${e.symbol} en el gráfico`}>
                    {ticker}
                  </button>
                )}
                <span className="ap-log-text">{e.text}</span>
              </li>
            )
          })}
        </ol>
      ) : (
        <div className="ap-empty tiny muted">Sin decisiones todavía: aparecerán al cerrar la próxima vela de los símbolos que vigila.</div>
      )}
    </Card>
  )
})

// ----------------------------------------------------------------------------- reset

/** Confirmation to reset NEXUS's account with a chosen initial balance (POST /api/paper/autopilot/reset). */
export function AutopilotResetModal(props: {
  open: boolean
  currentInitial: number | null
  openPositions: number
  onClose: () => void
  onDone: (snap: AutopilotSnapshot | null) => void
}) {
  const { open, currentInitial, openPositions, onClose, onDone } = props
  const [balance, setBalance] = useState<number | null>(currentInitial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setBalance(isNum(currentInitial) && currentInitial > 0 ? currentInitial : 10_000)
      setError(null)
    }
  }, [open, currentInitial])

  const invalid = balance === null || !(balance >= MIN_BALANCE && balance <= MAX_BALANCE)

  const confirm = async () => {
    if (invalid || balance === null) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.paper.autopilot.reset(balance)
      onDone(normalizeAutopilot(res))
      onClose()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const positionsText =
    openPositions <= 0
      ? 'Se cerrarán todas sus posiciones'
      : isOne(openPositions)
        ? 'Se cerrará su posición abierta'
        : `Se cerrarán sus ${fmtInt(openPositions)} posiciones abiertas`

  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onClose}
      width={460}
      title={
        <span className="row gap-2">
          <RotateCcw size={16} /> Reiniciar la cuenta de NEXUS
        </span>
      }
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancelar
          </Button>
          <Button variant="danger" onClick={() => void confirm()} loading={busy} disabled={invalid}>
            Reiniciar
          </Button>
        </>
      }
    >
      <div className="col gap-3">
        <div className="notice">
          <TriangleAlert size={15} style={{ flex: 'none', marginTop: 1 }} />
          <span>
            {positionsText} y se borrarán el historial y las decisiones de la cuenta de NEXUS (simulada). Tu cuenta no cambia y el
            aprendizaje de NEXUS y del filtro no se pierde.
          </span>
        </div>
        <Field label="Balance inicial ($)" hint={`Entre ${fmtMoney(MIN_BALANCE)} y ${fmtMoney(MAX_BALANCE)}`}>
          <NumberInput value={balance} onChange={setBalance} min={MIN_BALANCE} max={MAX_BALANCE} step={1000} placeholder="10000" />
        </Field>
        <div className="row gap-1 wrap">
          {BALANCE_PRESETS.map((v) => (
            <Button key={v} size="sm" variant="ghost" active={balance === v} onClick={() => setBalance(v)}>
              {fmtMoney(v)}
            </Button>
          ))}
        </div>
        {error && <ErrorBox error={error} onRetry={() => void confirm()} />}
      </div>
    </Modal>
  )
}

// ----------------------------------------------------------------------------- the view

/** `initial`: a snapshot shown before the first answer (the view always loads its own as well). */
export default function AutopilotView({ initial = null }: { initial?: AutopilotSnapshot | null }) {
  const notify = useApp((s) => s.notify)
  const [snap, setSnapState] = useState<AutopilotSnapshot | null>(initial)
  const [loading, setLoading] = useState(!initial)
  const [error, setError] = useState<string | null>(null)
  const [loadedAt, setLoadedAt] = useState<number | null>(() => (initial ? Date.now() : null))
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [closingId, setClosingId] = useState<number | null>(null)
  const [confirmAll, setConfirmAll] = useState(false)
  const [closingAll, setClosingAll] = useState(false)
  const [resetOpen, setResetOpen] = useState(false)
  const [, setTick] = useState(0)
  const alive = useRef(true)
  const seq = useRef(0)
  const snapRef = useRef<AutopilotSnapshot | null>(initial)
  const freshAt = useRef(0) // when the last live / POST snapshot arrived: a GET sent before it answers older data
  const [cmp, setCmp] = useState<AutopilotComparison | null>(null)
  const [cmpLoading, setCmpLoading] = useState(false)
  const [cmpError, setCmpError] = useState<string | null>(null)
  const cmpSeq = useRef(0)
  const [journal, setJournal] = useState<AutopilotJournal | null>(null)
  const [journalLoading, setJournalLoading] = useState(false)
  const [journalError, setJournalError] = useState<string | null>(null)
  const journalSeq = useRef(0)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const show = useCallback((s: AutopilotSnapshot) => {
    snapRef.current = s
    setSnapState(s)
    setLoadedAt(Date.now())
  }, [])

  /** A snapshot newer than any GET in flight (socket message, answer of a change). */
  const showFresh = useCallback(
    (s: AutopilotSnapshot) => {
      freshAt.current = Date.now()
      show(s)
    },
    [show],
  )

  const load = useCallback(
    async (quiet = false) => {
      const id = ++seq.current
      const sentAt = Date.now()
      if (!quiet) setLoading(true)
      try {
        const s = normalizeAutopilot(await api.paper.autopilot.get())
        if (!alive.current || id !== seq.current) return
        if (!s) throw new Error('Respuesta de la cuenta de NEXUS no válida')
        if (freshAt.current <= sentAt) show(s)
        setError(null)
      } catch (err) {
        if (!alive.current || id !== seq.current) return
        setError(errorMessage(err))
      } finally {
        if (alive.current && id === seq.current) setLoading(false)
      }
    },
    [show],
  )

  useEffect(() => {
    void load()
  }, [load])
  // Fallback while the page is visible (the socket carries every change); back on the tab, refresh at once.
  useInterval(() => {
    if (document.visibilityState !== 'hidden') void load(true)
  }, POLL_MS)
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') void load(true)
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [load])
  useInterval(() => setTick((t) => t + 1), 15_000) // keeps "hace X" labels fresh
  useEffect(
    () =>
      subscribeAutopilot((s) => {
        if (alive.current) showFresh(s)
      }),
    [showFresh],
  )

  // ------------------------------------------------------------------ actions
  const openSymbol = useCallback((symbol: string) => useApp.getState().openChart(symbol, snapRef.current?.autopilot.tf), [])
  // an option opens the chart of its stock
  const openPosition = useCallback((p: Position) => useApp.getState().openChart(marketOf(p).chart, asTimeframe(p.tf)), [])

  const save = useCallback(
    async (update: AutopilotUpdate): Promise<AutopilotStatus | null> => {
      setSaving(true)
      setSaveError(null)
      try {
        const s = normalizeAutopilot(await api.paper.autopilot.update(update))
        if (!s) throw new Error('Respuesta de la cuenta de NEXUS no válida')
        if (alive.current) showFresh(s)
        if (update.alerts !== undefined)
          notify(s.autopilot.alerts ? 'Avisos de NEXUS automática activados' : 'Avisos de NEXUS automática desactivados', 'info')
        else if (update.enabled === undefined) notify('Cambios de NEXUS automática guardados', 'success')
        else notify(s.autopilot.enabled ? 'NEXUS automática activada: opera sola en su cuenta demo' : 'NEXUS automática pausada', 'info')
        return s.autopilot
      } catch (err) {
        if (alive.current) setSaveError(errorMessage(err))
        return null
      } finally {
        if (alive.current) setSaving(false)
      }
    },
    [notify, showFresh],
  )

  const closePosition = useCallback(
    async (p: Position) => {
      setClosingId(p.id)
      try {
        const s = normalizeAutopilot(await api.paper.autopilot.close({ position_id: p.id }))
        if (!s) void load(true)
        else if (alive.current) showFresh(s)
        const c = s?.history.find((h) => h.id === p.id)
        notify(
          c
            ? `Cerrada ${marketOf(c).ticker} ${c.instrument?.kind === 'option' ? marketOf(c).side.toLowerCase() : c.side === 'long' ? 'largo' : 'corto'} de NEXUS · ${fmtR(c.r_multiple)} · ${fmtMoney(c.pnl, true)}`
            : `Posición #${p.id} de NEXUS cerrada`,
          'trade',
        )
      } catch (err) {
        notify(`No se pudo cerrar la posición: ${errorMessage(err)}`, 'error')
      } finally {
        if (alive.current) setClosingId(null)
      }
    },
    [load, notify, showFresh],
  )

  const closeAll = useCallback(async () => {
    const wereOpen = new Set((snapRef.current?.positions ?? []).map((p) => p.id))
    setClosingAll(true)
    try {
      const s = normalizeAutopilot(await api.paper.autopilot.close({ all: true }))
      if (!s) void load(true)
      else if (alive.current) showFresh(s)
      const closed = s ? s.history.filter((h) => wereOpen.has(h.id)) : []
      const pnl = closed.reduce((acc, p) => acc + (isNum(p.pnl) ? p.pnl : 0), 0)
      notify(
        s ? `${plural(closed.length, 'posición de NEXUS cerrada', 'posiciones de NEXUS cerradas')} · ${fmtMoney(pnl, true)}` : 'Posiciones de NEXUS cerradas',
        'trade',
      )
      if (alive.current) setConfirmAll(false)
    } catch (err) {
      notify(`No se pudieron cerrar las posiciones: ${errorMessage(err)}`, 'error')
    } finally {
      if (alive.current) setClosingAll(false)
    }
  }, [load, notify, showFresh])

  const onReset = useCallback(
    (s: AutopilotSnapshot | null) => {
      if (s) showFresh(s)
      else void load(true)
      notify('Cuenta de NEXUS reiniciada', 'success')
    },
    [load, notify, showFresh],
  )

  const status = snap?.autopilot ?? null
  const positions = snap?.positions ?? []
  const history = snap?.history ?? []

  // Its comparison with random entries: on open, whenever a trade closes, and on demand ("Recalcular")
  const loadCmp = useCallback(async (fresh: boolean) => {
    const id = ++cmpSeq.current
    setCmpLoading(true)
    try {
      const c = normalizeComparison(await api.paper.autopilot.compare(fresh))
      if (!alive.current || id !== cmpSeq.current) return
      if (!c) throw new Error('Respuesta de la comparación no válida')
      setCmp(c)
      setCmpError(null)
    } catch (err) {
      if (alive.current && id === cmpSeq.current) setCmpError(errorMessage(err))
    } finally {
      if (alive.current && id === cmpSeq.current) setCmpLoading(false)
    }
  }, [])
  // Its journal: on open, whenever a trade closes, and on demand ("Recalcular")
  const loadJournal = useCallback(async (fresh: boolean) => {
    const id = ++journalSeq.current
    setJournalLoading(true)
    try {
      const j = normalizeJournal(await api.paper.autopilot.journal(fresh))
      if (!alive.current || id !== journalSeq.current) return
      if (!j) throw new Error('Respuesta del diario no válida')
      setJournal(j)
      setJournalError(null)
    } catch (err) {
      if (alive.current && id === journalSeq.current) setJournalError(errorMessage(err))
    } finally {
      if (alive.current && id === journalSeq.current) setJournalLoading(false)
    }
  }, [])
  const closedCount = snap ? snap.account.trades : null
  useEffect(() => {
    if (closedCount !== null) {
      void loadCmp(false)
      void loadJournal(false)
    }
  }, [closedCount, loadCmp, loadJournal])
  const wide = useMediaQuery('(min-width: 1700px)')

  // ------------------------------------------------------------------ blocks
  const positionsCard = (
    <Card
      flush
      title={
        <>
          Posiciones abiertas <Badge tone={positions.length ? 'accent' : 'muted'}>{fmtInt(positions.length)}</Badge>
        </>
      }
      sub="Abiertas por NEXUS. Puedes cerrarlas a mano (a mercado, simulado): el resultado cuenta en esta cuenta."
      actions={
        positions.length > 1 ? (
          <Button size="sm" variant="ghost" onClick={() => setConfirmAll(true)} disabled={closingId !== null || closingAll}>
            Cerrar todas
          </Button>
        ) : undefined
      }
    >
      {positions.length > 0 ? (
        <PositionsTable positions={positions} closingId={closingId} onClose={closePosition} onOpenChart={openPosition} originLabels={ORIGIN_LABELS} />
      ) : (
        <Empty icon={<Wallet size={22} />} title="Sin posiciones abiertas">
          {status?.enabled
            ? 'NEXUS abrirá una cuando su señal al cierre de una vela sea COMPRAR o VENDER y el filtro diga TOMAR.'
            : 'Pausada: actívala en «Cómo opera» para que NEXUS empiece a operar.'}
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
      sub="Operaciones cerradas de NEXUS: motivo de salida, R y P&L"
    >
      {history.length > 0 ? (
        <HistoryTable history={history} onOpenChart={openPosition} originLabels={ORIGIN_LABELS} />
      ) : (
        <Empty title="Sin operaciones cerradas todavía">Cuando NEXUS cierre operaciones verás aquí su resultado.</Empty>
      )}
    </Card>
  )

  const compareCard = <CompareCard cmp={cmp} loading={cmpLoading} error={cmpError} onRefresh={() => void loadCmp(true)} />
  const journalCard = <JournalCard journal={journal} loading={journalLoading} error={journalError} onRefresh={() => void loadJournal(true)} />
  const controls = status ? <AutopilotControls status={status} saving={saving} error={saveError} onSave={save} /> : null
  const moneyCard = status ? <MoneyCard status={status} dailyLossPct={snap?.risk?.daily_loss_limit_pct ?? null} /> : null
  const weeklyCard = status ? <WeeklyCard weekly={status.weekly} /> : null
  const watchCard = status ? <WatchCard status={status} onOpenChart={openSymbol} /> : null
  const logCard = status ? <DecisionLog log={status.log} onOpenChart={openSymbol} /> : null

  // ------------------------------------------------------------------ render
  return (
    <>
      <div className="page-header">
        <div className="col gap-0">
          <h1 className="pp-title row gap-2">
            <Bot size={18} /> NEXUS automática
            {status && <Badge tone={status.enabled ? 'pos' : 'muted'}>{status.enabled ? 'Activa' : 'Pausada'}</Badge>}
          </h1>
          <span className="sub">
            Cuenta demo de NEXUS (simulada){loadedAt ? ` · actualizado ${fmtAgo(loadedAt)}` : ''}
          </span>
        </div>
        <span className="header-spacer" />
        <Button icon={<RefreshCw size={14} />} onClick={() => void load()} loading={loading && !!snap} title="Recargar la cuenta de NEXUS">
          Actualizar
        </Button>
        <Button icon={<RotateCcw size={14} />} variant="ghost" onClick={() => setResetOpen(true)} disabled={!snap} title="Empezar de cero con un balance nuevo">
          Reiniciar cuenta
        </Button>
      </div>

      <div className="ap-intro">
        <p className="ap-explain">
          Cuenta 100 % simulada que <b>opera sola</b>: NEXUS abre una posición solo cuando su señal al cierre de una vela es COMPRAR o
          VENDER y el filtro dice TOMAR, y la cierra con sus reglas de salida. Usa los mismos stops «hasta aquí», objetivos y límites de
          riesgo de cartera que tu cuenta demo. Solo opera mientras el servidor de NEXUS está en marcha y nunca usa dinero real: si
          activas el espejo, repite sus posiciones en tu cuenta de práctica de Alpaca, también sin dinero real.
        </p>
        {status?.note ? (
          <div className="notice ap-note" role="note">
            <Info size={15} style={{ flex: 'none', marginTop: 1 }} />
            <span>{status.note}</span>
          </div>
        ) : null}
      </div>

      {!snap ? (
        error ? (
          <ErrorBox error={error} onRetry={() => void load()} />
        ) : (
          <Loading label="Cargando la cuenta de NEXUS…" />
        )
      ) : (
        <>
          {error && <ErrorBox error={`${error} · mostrando los últimos datos conocidos`} onRetry={() => void load()} />}
          <BrakeNotice brake={status?.brake ?? null} />
          {status && <HealthNotice health={status.health} enabled={status.enabled} />}
          {status && <LearnedNotice contexts={status.paused_contexts} enabled={status.learn_guard} />}
          <PaperStats account={snap.account} winTally />
          {wide ? (
            <div className="pp-layout">
              <div className="pp-main">
                {positionsCard}
                {compareCard}
                {journalCard}
                {logCard}
                {historyCard}
              </div>
              <div className="pp-side">
                {controls}
                {moneyCard}
                {weeklyCard}
                <BrokerCard />
                {watchCard}
                <RiskPanel risk={snap.risk} />
              </div>
            </div>
          ) : (
            <>
              {positionsCard}
              {compareCard}
              {journalCard}
              <div className="ap-cols">
                <div className="ap-col">
                  {controls}
                  {moneyCard}
                  {weeklyCard}
                  <BrokerCard />
                </div>
                <div className="ap-col">
                  {watchCard}
                  {logCard}
                </div>
              </div>
              <RiskPanel risk={snap.risk} />
              {historyCard}
            </>
          )}
        </>
      )}

      <Disclaimer />

      <AutopilotResetModal
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
        title="Cerrar todas las posiciones de NEXUS"
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
            ? 'Se cerrará a precio de mercado la posición abierta de la cuenta de NEXUS (operación simulada).'
            : `Se cerrarán a precio de mercado las ${fmtInt(positions.length)} posiciones abiertas de la cuenta de NEXUS (operación simulada).`}{' '}
          El resultado cuenta en su cuenta; tu cuenta no cambia.
        </p>
      </Modal>
    </>
  )
}
