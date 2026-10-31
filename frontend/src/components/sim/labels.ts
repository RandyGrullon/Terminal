// Spanish labels and small display helpers for the Simulation page (FE-SIM). The exit-advice helpers at the end are
// shared with the chart's signal panel.
import type { ChartMarker, NexusWarning, SimRegime, SimState } from '../../api/types'
import { REGIME_LABEL, fmtPct, fmtProb, type Tone } from '../../lib/format'

export const SIM_REGIME_OPTIONS: { value: SimRegime; label: string }[] = [
  { value: 'auto', label: 'Auto' },
  { value: 'bull', label: 'Alcista' },
  { value: 'bear', label: 'Bajista' },
  { value: 'range', label: 'Lateral' },
  { value: 'volatile', label: 'Volátil' },
  { value: 'crash', label: 'Crash' },
]

const SIM_REGIME_LABEL: Record<string, string> = {
  auto: 'Auto',
  bull: 'Alcista',
  bear: 'Bajista',
  range: 'Lateral',
  volatile: 'Volátil',
  crash: 'Crash',
}

export const SIM_REGIME_VALUES: SimRegime[] = SIM_REGIME_OPTIONS.map((o) => o.value)

/** Label for a simulator regime ("bull") or a NEXUS regime ("trend_up"); unknown values are shown as-is. */
export function simRegimeLabel(regime: string | null | undefined): string {
  if (!regime) return '—'
  return SIM_REGIME_LABEL[regime] ?? (REGIME_LABEL as Record<string, string>)[regime] ?? regime
}

export function simRegimeTone(regime: string | null | undefined): Tone {
  switch (regime) {
    case 'bull':
    case 'trend_up':
      return 'pos'
    case 'bear':
    case 'trend_down':
    case 'crash':
      return 'neg'
    case 'volatile':
      return 'warn'
    case 'range':
      return 'info'
    default:
      return 'muted'
  }
}

export const SIM_MODE_LABEL: Record<'synthetic' | 'replay', string> = {
  synthetic: 'Sintético',
  replay: 'Replay histórico',
}

export const MC_MODE_LABEL: Record<'bootstrap' | 'synthetic', string> = {
  bootstrap: 'Bootstrap (operaciones reales)',
  synthetic: 'Sintético (mercados simulados)',
}

export function simStatus(state: SimState | null | undefined): { label: string; tone: Tone; key: 'live' | 'paused' | 'off' } {
  if (!state || !state.running) return { label: 'Detenido', tone: 'muted', key: 'off' }
  if (state.paused) return { label: 'En pausa', tone: 'warn', key: 'paused' }
  return { label: 'En marcha', tone: 'pos', key: 'live' }
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Rates in the contract are fractions (0..1); values above 1 are treated as percent units defensively. */
export function asFraction(v: number | null | undefined): number | null {
  if (!isNum(v)) return null
  return Math.abs(v) > 1.000001 ? v / 100 : v
}

export function fmtRate(v: number | null | undefined, digits = 1): string {
  return fmtProb(asFraction(v), digits)
}

/** Drawdowns are always shown as negative percentages, whatever sign convention the backend uses. */
export function ddValue(v: number | null | undefined): number | null {
  if (!isNum(v)) return null
  return -Math.abs(v) || 0
}

export function fmtDd(v: number | null | undefined, digits = 2): string {
  return fmtPct(ddValue(v), digits)
}

export function fmtSpeed(v: number | null | undefined): string {
  if (!isNum(v)) return '—'
  return `${v.toLocaleString('es-ES', { maximumFractionDigits: 1 })} velas/s`
}

export const MARKER_KIND_LABEL: Record<string, string> = {
  BUY: 'Entrada en largo',
  SELL: 'Entrada en corto',
  TP1: 'Objetivo 1 alcanzado',
  TP2: 'Objetivo 2 alcanzado',
  TP3: 'Objetivo 3 alcanzado',
  EXIT_SL: 'Salida por stop loss',
  EXIT_TRAIL: 'Salida por stop dinámico',
  EXIT_EARLY: 'Salida anticipada (la operación se torció)',
  EXIT_REVERSE: 'Salida por señal contraria',
  EXIT_TIME: 'Salida por tiempo agotado',
  EXIT_SIGNAL: 'Salida por señal',
  REJECTED_LONG: 'Largo rechazado por el filtro',
  REJECTED_SHORT: 'Corto rechazado por el filtro',
  WARN: 'Aviso',
}

export function markerTitle(m: ChartMarker): string {
  return MARKER_KIND_LABEL[m.kind] ?? m.text ?? m.kind
}

export function isRejectedMarker(m: ChartMarker): boolean {
  return typeof m.kind === 'string' && m.kind.startsWith('REJECTED')
}

export function warningTone(w: NexusWarning): Tone {
  return w.severity === 'critical' ? 'neg' : w.severity === 'warn' ? 'warn' : 'info'
}

// ----------------------------------------------------------------------------- exit advice for NEXUS' open position

/** NEXUS' early-exit threshold (EARLY_EV in app/engine/nexus_decide.py): it closes before the stop when the EV of
 *  holding is at or below this value with the score against. Used to explain the rule, never to decide. */
export const EARLY_EXIT_EV = -0.1

export function isExitAction(action: string | null | undefined): boolean {
  return action === 'EXIT_LONG' || action === 'EXIT_SHORT'
}

/** exit = NEXUS' own exit (action EXIT_*): "CERRAR AHORA"; watch = negative EV of holding while NEXUS keeps the
 *  position: an amber "Vigila la salida" with the EV, never an order to close; hold = otherwise (also on the bar of
 *  NEXUS' own entry, BUY / SELL: the engine does not warn about the exit next to the entry it has just decided). */
export type ExitAdvice = 'exit' | 'watch' | 'hold'

export function exitAdvice(action: string | null | undefined, evHold: number | null | undefined): ExitAdvice {
  if (isExitAction(action)) return 'exit'
  if (action === 'BUY' || action === 'SELL') return 'hold'
  return isNum(evHold) && evHold < 0 ? 'watch' : 'hold'
}

/** NEXUS warnings as shown in a panel. While NEXUS keeps the position, an EARLY_EXIT warning (older servers: negative
 *  EV of holding, "vigila la salida") is a caution (amber), not the red early-exit alert reserved for a real EXIT_*
 *  action. It and the engine's WATCH_EXIT ("Vigila la salida: …") are dropped when the panel's position block already
 *  shows that negative EV (`watchShown`, see exitAdvice) so the same caution is not listed twice. */
export function shownWarnings(
  warnings: readonly NexusWarning[],
  action: string | null | undefined,
  watchShown = false,
): NexusWarning[] {
  if (isExitAction(action)) return [...warnings]
  return warnings
    .filter((w) => !(watchShown && (w.code === 'EARLY_EXIT' || w.code === 'WATCH_EXIT')))
    .map((w) => (w.code === 'EARLY_EXIT' && w.severity === 'critical' ? { ...w, severity: 'warn' as const } : w))
}
