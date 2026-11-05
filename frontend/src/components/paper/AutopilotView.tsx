// "NEXUS automática": NEXUS's own demo account (account 3), which trades on its own with NEXUS's closed-bar signals and
// the Trade Gate. The view shows what it is doing (watched symbols, latest scan, decision log), its open positions and
// history, its stats (winners as "x de n", never a bare percentage), how it trades (on/off, source, timeframe, limits)
// and a reset. Simulated only: nothing is ever sent to a broker. Live updates come from the default WS 'paper' topic as
// {type: 'paper_autopilot'} (the user's {type: 'paper'} messages are account 1 and never feed this view); a GET every
// 30 s while the page is visible is the fallback.
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { Bot, Info, Radar, RefreshCw, RotateCcw, ScrollText, SlidersHorizontal, TriangleAlert, Wallet, X } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type {
  AutopilotLast,
  AutopilotLogEntry,
  AutopilotScan,
  AutopilotSnapshot,
  AutopilotSource,
  AutopilotStatus,
  AutopilotUpdate,
  AutopilotWatch,
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
import PaperStats from './PaperStats'
import PositionsTable from './PositionsTable'
import HistoryTable from './HistoryTable'
import RiskPanel from './RiskPanel'
import { normalizeRisk } from './ordersUtils'
import { isAccount, normalizePositions } from './paperUtils'
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
  { value: 'scanner', label: 'Escáner', title: 'Las mejores oportunidades TOMAR del último escaneo' },
  { value: 'list', label: 'Lista fija', title: 'Solo los símbolos de tu lista' },
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
    source: o.source === 'list' ? 'list' : 'scanner',
    tf: asTimeframe(isStr(o.tf) ? o.tf : null) ?? '1h',
    symbols: Array.isArray(o.symbols) ? o.symbols.filter((s): s is string => isStr(s) && s !== '') : [],
    max_positions: numOr(o.max_positions, 5),
    risk_pct: numOr(o.risk_pct, 1),
    scanner_top: numOr(o.scanner_top, 10),
    watching: normalizeWatch(o.watching),
    scan: normalizeScan(o.scan),
    log: normalizeLog(o.log),
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
}

type NumField = 'max_positions' | 'risk_pct' | 'scanner_top'

/** The server's limits for each number (same wording as its 400 answers). */
const RANGES: Record<NumField, { lo: number; hi: number; int: boolean; what: string }> = {
  max_positions: { lo: 1, hi: 20, int: true, what: 'El máximo de posiciones' },
  risk_pct: { lo: 0.1, hi: 5, int: false, what: 'El riesgo por operación (%)' },
  scanner_top: { lo: 1, hi: 30, int: true, what: 'El número de candidatos del escáner' },
}

/** Numbers shown for a source: the scanner candidates only in scanner mode. */
const numFields = (source: AutopilotSource): NumField[] =>
  source === 'scanner' ? ['max_positions', 'risk_pct', 'scanner_top'] : ['max_positions', 'risk_pct']

export function draftFrom(s: AutopilotStatus): AutopilotDraft {
  return { source: s.source, symbols: [...s.symbols], tf: s.tf, max_positions: s.max_positions, risk_pct: s.risk_pct, scanner_top: s.scanner_top }
}

const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i])

/** What POST /api/paper/autopilot gets: the visible fields that differ from the saved settings (the symbol list only in
 *  list mode, the scanner candidates only in scanner mode). Empty numbers are left out: validateDraft reports them. */
export function draftChanges(d: AutopilotDraft, saved: AutopilotDraft): AutopilotUpdate {
  const out: AutopilotUpdate = {}
  if (d.source !== saved.source) out.source = d.source
  if (d.tf !== saved.tf) out.tf = d.tf
  for (const k of numFields(d.source)) {
    const v = d[k]
    if (v !== null && v !== saved[k]) out[k] = v
  }
  if (d.source === 'list' && !sameList(d.symbols, saved.symbols)) out.symbols = [...d.symbols]
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
  if (d.source === 'list' && d.symbols.length > MAX_SYMBOLS) return `Como máximo ${MAX_SYMBOLS} símbolos en la lista.`
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
              ? 'Las mejores oportunidades TOMAR del último escaneo (filtro TOMAR con señal de NEXUS), el automático o uno que lances en Escáner. Después NEXUS decide en su propia temporalidad.'
              : 'Solo los símbolos de la lista: en cada vela cerrada NEXUS decide si entra y en qué dirección.'}
          </span>
        </div>

        {draft.source === 'list' && (
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
          {draft.source === 'scanner' && (
            <Field label="Candidatos del escáner" hint="Oportunidades del último escaneo que vigila: de 1 a 30">
              <NumberInput value={draft.scanner_top} onChange={(v) => patch({ scanner_top: v })} min={1} max={30} step={1} />
            </Field>
          )}
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
  if (!s.scan) return 'Cuando termine un escaneo, NEXUS vigilará sus mejores oportunidades TOMAR.'
  if (s.scan.finished_at === null) return 'Esperando a que termine el escaneo en curso.'
  if (s.scan.opportunities <= 0)
    return 'El último escaneo no encontró ninguna oportunidad TOMAR (filtro TOMAR con señal de NEXUS): no abrirá nada nuevo hasta que un escaneo encuentre alguna.'
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
  const sub =
    status.source === 'scanner'
      ? `Hasta ${plural(status.scanner_top, 'oportunidad TOMAR', 'oportunidades TOMAR')} del último escaneo · decide en velas de ${status.tf}`
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
      {status.source === 'scanner' && <ScanLine scan={status.scan} />}
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
  const openPosition = useCallback((p: Position) => useApp.getState().openChart(p.symbol, asTimeframe(p.tf)), [])

  const save = useCallback(
    async (update: AutopilotUpdate): Promise<AutopilotStatus | null> => {
      setSaving(true)
      setSaveError(null)
      try {
        const s = normalizeAutopilot(await api.paper.autopilot.update(update))
        if (!s) throw new Error('Respuesta de la cuenta de NEXUS no válida')
        if (alive.current) showFresh(s)
        if (update.enabled === undefined) notify('Cambios de NEXUS automática guardados', 'success')
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
            ? `Cerrada ${splitSymbol(c.symbol).ticker} ${c.side === 'long' ? 'largo' : 'corto'} de NEXUS · ${fmtR(c.r_multiple)} · ${fmtMoney(c.pnl, true)}`
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

  const controls = status ? <AutopilotControls status={status} saving={saving} error={saveError} onSave={save} /> : null
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
          riesgo de cartera que tu cuenta demo. Solo opera mientras el servidor de NEXUS está en marcha y nunca envía nada a un bróker.
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
          <PaperStats account={snap.account} winTally />
          {wide ? (
            <div className="pp-layout">
              <div className="pp-main">
                {positionsCard}
                {logCard}
                {historyCard}
              </div>
              <div className="pp-side">
                {controls}
                {watchCard}
                <RiskPanel risk={snap.risk} />
              </div>
            </div>
          ) : (
            <>
              {positionsCard}
              <div className="ap-cols">
                <div className="ap-col">{controls}</div>
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
