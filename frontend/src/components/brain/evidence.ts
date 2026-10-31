// Honest reading of the Trade Gate's track record ("¿sirve el filtro?"): always "x de n", a 95 % interval and a
// comparison with the trades that WERE taken (not a fixed 50 %), so a handful of outcomes is never presented as
// proof (ARCHITECTURE §0.9). Shared by the chart's signal panel, the backtest "Efecto del filtro" card and the
// learning page.
import { fmtInt, fmtProb, type Tone } from '../../lib/format'
import { pluralWord } from '../../lib/plural'

/** Below this many resolved cases (on either side of the comparison) no conclusion is drawn. */
export const MIN_EVIDENCE_N = 30
const Z95 = 1.959964

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Rate as a fraction 0..1 (values above 1 are treated as percentages, defensively). */
function fraction(v: number | null | undefined): number | null {
  if (!isNum(v)) return null
  const f = Math.abs(v) > 1.000001 ? v / 100 : v
  return Math.min(1, Math.max(0, f))
}

function count(v: number | null | undefined): number | null {
  return isNum(v) && v >= 0 ? Math.round(v) : null
}

/** Wilson score interval (95 %) of `k` successes in `n` trials. */
export function wilson(k: number, n: number): [number, number] | null {
  if (!(n > 0) || !(k >= 0) || k > n) return null
  const p = k / n
  const z2 = Z95 * Z95
  const den = 1 + z2 / n
  const center = (p + z2 / (2 * n)) / den
  const half = (Z95 * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / den
  return [Math.max(0, center - half), Math.min(1, center + half)]
}

/** Newcombe's hybrid score interval (95 %) of p1 − p2 for two independent proportions. */
export function diffInterval(k1: number, n1: number, k2: number, n2: number): [number, number] | null {
  const a = wilson(k1, n1)
  const b = wilson(k2, n2)
  if (!a || !b) return null
  const p1 = k1 / n1
  const p2 = k2 / n2
  return [p1 - p2 - Math.hypot(p1 - a[0], b[1] - p2), p1 - p2 + Math.hypot(a[1] - p1, p2 - b[0])]
}

/** Cases with a known outcome: `wins` of `n`. */
export interface Tally {
  n: number
  wins: number
}

function tally(rate: number | null, n: number | null): Tally | null {
  if (rate === null || n === null || n <= 0) return null
  return { n, wins: Math.min(n, Math.max(0, Math.round(rate * n))) }
}

export type FilterVerdict =
  | 'none' // no rejected case with a known outcome yet
  | 'insufficient' // too few cases, nothing to compare with, or the 95 % interval of the difference includes zero
  | 'discards_worse' // the rejected trades win clearly LESS often than the taken ones
  | 'discards_better' // they win clearly MORE often: the filter throws away better trades

/** Why the evidence is not enough. */
export type EvidenceGap = 'unknown_n' | 'small_n' | 'no_benchmark' | 'small_benchmark' | 'overlap'

export interface FilterEvidence {
  verdict: FilterVerdict
  gap: EvidenceGap | null
  /** Rejected (avoided) signals, resolved or not. */
  total: number | null
  /** Fraction of the resolved rejected ones that would have won. */
  rate: number | null
  /** Resolved rejected cases (null when the server does not say how many). */
  rejected: Tally | null
  /** Win rate of the taken trades (the benchmark) and its tally when the sample size is known. */
  takenRate: number | null
  taken: Tally | null
}

export interface EvidenceInput {
  /** Rejected (or avoided) signals, resolved or not. */
  total?: number | null
  /** Of them, how many have a known outcome. */
  resolved?: number | null
  /** Fraction of the resolved ones that won / would have won. */
  winRate?: number | null
  /** Taken trades with a known outcome and their win rate. */
  takenN?: number | null
  takenWinRate?: number | null
}

export function filterEvidence(input: EvidenceInput): FilterEvidence {
  const total = count(input.total)
  const rate = fraction(input.winRate)
  const resolved = count(input.resolved)
  const takenRate = fraction(input.takenWinRate)
  const rejected = tally(rate, resolved)
  const taken = tally(takenRate, count(input.takenN))
  const base = { total, rate, rejected, takenRate, taken }
  if (rate === null || resolved === 0 || total === 0) return { ...base, verdict: 'none', gap: null }
  const insufficient = (gap: EvidenceGap): FilterEvidence => ({ ...base, verdict: 'insufficient', gap })
  if (!rejected) return insufficient('unknown_n')
  if (rejected.n < MIN_EVIDENCE_N) return insufficient('small_n')
  if (takenRate === null) return insufficient('no_benchmark')
  if (taken && taken.n < MIN_EVIDENCE_N) return insufficient('small_benchmark')
  // Both sample sizes known: interval of the difference; otherwise the rejected interval against the taken rate.
  let ci: [number, number] | null
  if (taken) {
    ci = diffInterval(rejected.wins, rejected.n, taken.wins, taken.n)
  } else {
    const w = wilson(rejected.wins, rejected.n)
    ci = w ? [w[0] - takenRate, w[1] - takenRate] : null
  }
  if (!ci || (ci[0] <= 0 && ci[1] >= 0)) return insufficient('overlap')
  return { ...base, verdict: ci[1] < 0 ? 'discards_worse' : 'discards_better', gap: null }
}

/** "12 de 26" (`losses`: "14 de 26"); with `noun` the agreeing word follows: "1 de 1 verificada". Null without a tally. */
export function fmtOf(t: Tally | null, losses = false, noun?: [string, string]): string | null {
  if (!t) return null
  const s = `${fmtInt(losses ? t.n - t.wins : t.wins)} de ${fmtInt(t.n)}`
  return noun ? `${s} ${pluralWord(t.n, noun[0], noun[1])}` : s
}

/** "12 de 26 · 46 %" (or "46 %" when the denominator is unknown). `losses` counts the other side. */
export function fmtTally(t: Tally | null, rate: number | null, losses = false): string {
  if (t) {
    const k = losses ? t.n - t.wins : t.wins
    return `${fmtInt(k)} de ${fmtInt(t.n)} · ${fmtProb(k / t.n, 0)}`
  }
  if (rate === null) return '—'
  return fmtProb(losses ? 1 - rate : rate, 0)
}

export function evidenceTone(e: FilterEvidence): Tone {
  return e.verdict === 'discards_worse' ? 'pos' : e.verdict === 'discards_better' ? 'neg' : 'muted'
}

/** signals = NEXUS signals rejected by the filter (chart, backtest); decisions = verified gate verdicts (learning). */
export type EvidenceContext = 'signals' | 'decisions'

function facts(e: FilterEvidence, ctx: EvidenceContext): string {
  const rejected = ctx === 'signals' ? 'De las señales rechazadas con resultado conocido' : 'De las operaciones evitadas (NO TOMAR) ya verificadas'
  const taken = ctx === 'signals' ? 'de las operaciones tomadas' : 'de las aprobadas (TOMAR)'
  const r = e.rejected
  let s = r
    ? `${rejected}, ${fmtInt(r.wins)} de ${fmtInt(r.n)} ${pluralWord(r.wins, 'habría', 'habrían')} ganado (${fmtProb(r.wins / r.n, 0)})`
    : `${rejected}, el ${fmtProb(e.rate, 0)} habría ganado`
  const t = e.taken
  if (t) s += `; ${taken}, ${pluralWord(t.wins, 'ganó', 'ganaron')} ${fmtInt(t.wins)} de ${fmtInt(t.n)} (${fmtProb(t.wins / t.n, 0)})`
  else if (e.takenRate !== null) s += `; ${taken}, ganó el ${fmtProb(e.takenRate, 0)}`
  return `${s}.`
}

/** Spanish explanation: what was measured ("x de n") and what it does — or does not yet — say about the filter. */
export function evidenceText(e: FilterEvidence, ctx: EvidenceContext): string {
  if (e.verdict === 'none') {
    return ctx === 'signals'
      ? 'Aún no hay señales rechazadas con resultado conocido: todavía no se puede medir el filtro.'
      : 'Aún no hay decisiones NO TOMAR verificadas: todavía no se puede medir el filtro.'
  }
  const n = e.rejected ? fmtInt(e.rejected.n) : null
  const taken = ctx === 'signals' ? 'tomadas' : 'aprobadas'
  const question =
    ctx === 'signals' ? 'si el filtro descarta peores señales que las que deja pasar' : 'si el filtro evita peores operaciones que las que aprueba'
  let verdict: string
  if (e.verdict === 'discards_worse') {
    verdict =
      ctx === 'signals'
        ? `Por ahora indica que el filtro descarta sobre todo señales peores que las que deja pasar (n = ${n}). Es una medición del pasado, no una garantía.`
        : `Por ahora indica que el filtro evita sobre todo operaciones peores que las que aprueba (n = ${n}). Es una medición del pasado, no una garantía.`
  } else if (e.verdict === 'discards_better') {
    verdict =
      ctx === 'signals'
        ? `Ojo: por ahora indica que las señales rechazadas ganan más a menudo que las tomadas (n = ${n}); aquí el filtro parece demasiado exigente.`
        : `Ojo: por ahora indica que las operaciones evitadas habrían ganado más a menudo que las aprobadas (n = ${n}); el filtro parece demasiado exigente.`
  } else if (e.gap === 'unknown_n') {
    verdict = `Aún sin evidencia suficiente${e.total !== null ? ` (n ≤ ${fmtInt(e.total)})` : ''}: falta el número de casos con resultado conocido.`
  } else if (e.gap === 'no_benchmark') {
    verdict = `Aún sin evidencia suficiente (n = ${n}): todavía no hay operaciones ${taken} con resultado con las que comparar.`
  } else if (e.gap === 'small_benchmark') {
    verdict = `Aún sin evidencia suficiente (n = ${n}, pero solo ${fmtInt(e.taken?.n)} ${taken} para comparar).`
  } else if (e.gap === 'overlap') {
    verdict = `Aún sin evidencia suficiente (n = ${n}): la diferencia con las ${taken} cabe en el margen de error.`
  } else {
    verdict = `Aún sin evidencia suficiente (n = ${n}) para saber ${question}.`
  }
  return `${facts(e, ctx)} ${verdict}`
}
