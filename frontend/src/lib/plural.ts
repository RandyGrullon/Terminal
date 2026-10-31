// Spanish singular / plural for counts shown in the UI: "1 vela" but "0 velas", "2 velas", "1.234 velas".
import { fmtInt } from './format'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** True only for a count of exactly one (as displayed, after rounding): in Spanish every other number, 0 included,
 *  takes the plural. */
export function isOne(n: number | null | undefined): boolean {
  return isNum(n) && Math.abs(Math.round(n)) === 1
}

/** The word (or phrase) that agrees with `n`: pluralWord(1, 'vela', 'velas') -> 'vela'. */
export function pluralWord(n: number | null | undefined, singular: string, pluralForm: string): string {
  return isOne(n) ? singular : pluralForm
}

/** Count plus the agreeing word: plural(1, 'vela', 'velas') -> '1 vela', plural(0, 'vela', 'velas') -> '0 velas'. */
export function plural(n: number | null | undefined, singular: string, pluralForm: string): string {
  return `${fmtInt(n)} ${pluralWord(n, singular, pluralForm)}`
}
