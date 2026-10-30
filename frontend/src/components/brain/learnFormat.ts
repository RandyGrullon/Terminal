// Formatting helpers and Spanish labels for the learning ("Aprendizaje") page.
import { fmtNum, fmtPct, type Tone } from '../../lib/format'

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Rates may arrive as fractions (0..1) or percentages (0..100); returns percent units. */
export function ratePct(v: number | null | undefined): number | null {
  if (!finite(v)) return null
  return Math.abs(v) <= 1 ? v * 100 : v
}

/** Probabilities may arrive as 0..1 or 0..100; returns 0..1. */
export function prob01(v: number | null | undefined): number | null {
  if (!finite(v)) return null
  return Math.abs(v) > 1 ? v / 100 : v
}

export function fmtRate(v: number | null | undefined, digits = 1): string {
  const p = ratePct(v)
  return p === null ? '—' : fmtPct(p, digits, false)
}

/** Hit-rate tone: green when clearly better than a coin flip, red when worse. */
export function rateTone(v: number | null | undefined, good = 55, bad = 50): Tone {
  const p = ratePct(v)
  if (p === null) return 'muted'
  if (p >= good) return 'pos'
  if (p < bad) return 'neg'
  return 'warn'
}

/** Brier score (0 = perfect, 0.25 = coin flip): lower is better. */
export function brierTone(b: number | null | undefined): Tone {
  if (!finite(b)) return 'muted'
  if (b <= 0.235) return 'pos'
  if (b > 0.255) return 'neg'
  return 'warn'
}

export function fmtBrier(b: number | null | undefined): string {
  return fmtNum(b, 3)
}

export const REGIME_ORDER = ['trend_up', 'trend_down', 'range', 'volatile']

/** Labels of the Trade Gate checks (ARCHITECTURE §8). */
export const GATE_CHECK_LABEL: Record<string, string> = {
  trend_alignment: 'Tendencia mayor a favor',
  regime: 'Régimen de mercado',
  momentum: 'Momento (MACD / RSI)',
  room_to_target: 'Espacio hasta el objetivo',
  risk_reward: 'Riesgo / beneficio',
  overextension: 'Precio sobreextendido',
  volatility: 'Volatilidad',
  volume: 'Volumen',
  probability: 'Probabilidad aprendida',
  edge_history: 'Historial de casos similares',
  expected_value: 'Valor esperado',
  recent_performance: 'Racha reciente',
  confluence: 'Confluencia de expertos',
}

export function prettyKey(id: string): string {
  const s = id.replace(/[_.|]+/g, ' ').trim()
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : id
}

/** Accepts unix seconds or ms and returns ms. */
export function toMs(t: number | null | undefined): number | null {
  if (!finite(t)) return null
  return t < 1e11 ? t * 1000 : t
}

/** "en 2 h", "en 35 min", "vencida". */
export function fmtIn(ms: number | null | undefined): string {
  if (!finite(ms)) return '—'
  const s = Math.round((ms - Date.now()) / 1000)
  if (s <= 0) return 'vencida'
  if (s < 60) return `en ${s} s`
  if (s < 3600) return `en ${Math.round(s / 60)} min`
  if (s < 86400) return `en ${Math.round(s / 3600)} h`
  return `en ${Math.round(s / 86400)} d`
}
